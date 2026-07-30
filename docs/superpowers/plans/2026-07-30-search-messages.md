# search_messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `search_messages` MCP tool that performs server-side full-text message search across one or all Google Chat spaces, via the Developer-Preview `spaces.messages.search` endpoint.

**Architecture:** The endpoint is absent from `google-api-python-client`'s discovery document, so it is called as a raw authenticated HTTP POST through a new private helper in `google_chat.py`, using the same `urllib.request` + `Bearer {creds.token}` pattern `download_attachment` already uses. A public async `search_space_messages()` owns parent selection, pagination, field filtering and preview-aware error wrapping; `server.py` registers a thin `search_messages` tool over it.

**Tech Stack:** Python 3.13, `uv`, `fastmcp`, `google-auth`, `google-api-python-client`, stdlib `urllib.request` + `json`.

**Spec:** `docs/superpowers/specs/2026-07-30-search-messages-design.md` — read it before starting.

## Global Constraints

- **No new dependencies.** `requests` is NOT in `pyproject.toml`; use stdlib `urllib.request`. Do not add anything to `dependencies`.
- **No new OAuth scope.** `https://www.googleapis.com/auth/chat.messages` is already in `SCOPES` (google_chat.py:16-22). Do not touch `SCOPES` — changing it invalidates the user's `token.json`.
- **Token path is external:** the running server uses `--token-path ~/.genduk/credentials/google-chat/token.json`. All manual verification must pass that path.
- **There is no test suite.** `tests/` is empty, `pytest` is not a dependency. Verification in this plan is by running real scripts against the live API — that is deliberate and matches the task's acceptance criteria. Do not introduce a test framework.
- **Error convention:** every public function in `google_chat.py` wraps failures as `raise Exception(f"Failed to <verb>: {str(e)}")`. Match it.
- `MAX_MESSAGES = 1000` (google_chat.py:66) is the hard result ceiling. `pageSize` accepted by the search API is 1..100.
- Return shape of the tool is a **dict** `{"messages": [...], "nextPageToken": str | None}` — NOT a bare list. This is an intentional deviation from the task ticket's `-> List[Dict]`; see the spec's "Continuation, not re-fetch" section.

---

### Task 1: Pin down the live API contract

Two things in the ticket are unverified guesses and everything downstream depends on them: whether the filter string is bare (`deploy`) or field-qualified (`text: "deploy"`), and whether `parent` may be a concrete space (`spaces/XXXX`) or must always be `spaces/-`. Settle both against the live API before writing any library code.

**Files:**
- Create: `/tmp/probe_search.py` (throwaway — do NOT commit)

**Interfaces:**
- Consumes: nothing.
- Produces: two facts recorded in the commit message and used by Task 2 — `FILTER_FORM` (`bare` or `qualified`) and `PARENT_SCOPING` (`concrete` or `dash-only`).

- [ ] **Step 1: Write the probe script**

```python
# /tmp/probe_search.py
import json, sys, urllib.request, urllib.error
sys.path.insert(0, '/Users/husni/.genduk/mcp-servers/google-chat-mcp-server')
import google_chat

google_chat.set_token_path('~/.genduk/credentials/google-chat/token.json')
creds = google_chat.get_credentials()
assert creds, "no credentials — authenticate first"

def probe(label, parent, body):
    url = f"https://chat.googleapis.com/v1/{parent}/messages:search"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {creds.token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req)
        payload = json.load(resp)
        n = len(payload.get('messages', payload.get('results', [])))
        print(f"[{label}] HTTP {resp.status} keys={list(payload)} count={n}")
        print(json.dumps(payload, indent=2)[:1500])
    except urllib.error.HTTPError as e:
        print(f"[{label}] HTTP {e.code}: {e.read().decode()[:600]}")

QUERY = "test"          # replace with a keyword you know exists in your spaces
SPACE = "spaces/-"      # replace with a real spaces/XXXX for the concrete-parent probe

probe("bare-filter/dash",      "spaces/-", {"filter": QUERY, "pageSize": 5})
probe("qualified-filter/dash", "spaces/-", {"filter": f'text: "{QUERY}"', "pageSize": 5})
probe("bare-filter/concrete",  SPACE,      {"filter": QUERY, "pageSize": 5})
```

- [ ] **Step 2: Run it**

Run: `cd /Users/husni/.genduk/mcp-servers/google-chat-mcp-server && uv run python /tmp/probe_search.py`

Expected: at least the `bare-filter/dash` probe returns HTTP 200 with a non-empty result array (this exact call was confirmed working on 2026-07-30). Note for each probe: status, the **top-level key holding the results** (`messages` vs `results` — the ticket and the prior probe disagree; whichever this prints is authoritative for Task 2), and whether `nextPageToken` is present.

- [ ] **Step 3: Record the findings**

Write the three answers into the spec file under a new `## Probe results (YYYY-MM-DD)` section at the end:
1. `RESULT_KEY` — the top-level key holding messages.
2. `FILTER_FORM` — `bare` if the bare-filter probe works; `qualified` only if bare fails and qualified succeeds.
3. `PARENT_SCOPING` — `concrete` if `spaces/XXXX` returned 200; `dash-only` if it 400/404'd.

If `PARENT_SCOPING` is `dash-only`, add one line: "space_name scoping falls back to `parent=spaces/-` with `AND space.name = \"<space_name>\"` appended to the filter."

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-07-30-search-messages-design.md
git commit -m "docs: record live spaces.messages.search probe results

Pins the filter form, parent scoping, and result key that the
implementation depends on, rather than guessing from the ticket."
```

---

### Task 2: `_chat_api_post` + `search_space_messages` in google_chat.py

**Files:**
- Modify: `google_chat.py` — add both functions after `list_space_messages` (ends at line 463), before `send_space_message`.
- Create: `/tmp/verify_search.py` (throwaway — do NOT commit)

**Interfaces:**
- Consumes: `get_credentials()`, `get_user_display_name(sender, creds)`, `FILTER_MESSAGES`, `MAX_MESSAGES`, `APP_MESSAGE_PREFIX` — all already in `google_chat.py`. Probe findings from Task 1.
- Produces:
  - `_chat_api_post(path: str, body: Dict, creds) -> Dict`
  - `async search_space_messages(query: str, space_name: Optional[str] = None, limit: int = 50, page_token: Optional[str] = None) -> Dict` returning `{"messages": List[Dict], "nextPageToken": Optional[str]}`

- [ ] **Step 1: Add the raw POST helper**

Insert after line 463 (end of `list_space_messages`). `import json` at the top of the file if not already present — check line 1-11 first; if absent, add `import json` after `import datetime`.

```python
def _chat_api_post(path: str, body: Dict, creds: Credentials) -> Dict:
    """POST to a Chat API v1 endpoint that the discovery client does not expose.

    Used for methods missing from google-api-python-client's discovery document
    (currently spaces.messages.search, a Developer Preview method). Mirrors the
    raw-HTTP pattern in download_attachment.

    Args:
        path: API path below /v1/, e.g. 'spaces/-/messages:search'
        body: JSON request body
        creds: Valid credentials (get_credentials() has already refreshed them)

    Returns:
        Parsed JSON response

    Raises:
        urllib.error.HTTPError: propagated unchanged so callers can inspect .code
    """
    url = f"https://chat.googleapis.com/v1/{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode('utf-8'),
        headers={
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)
```

- [ ] **Step 2: Add the search function**

Insert immediately after `_chat_api_post`. Replace `RESULT_KEY`, and the parent/filter construction, with what Task 1 actually established — the code below assumes `RESULT_KEY = "messages"`, `FILTER_FORM = bare`, `PARENT_SCOPING = concrete`. If Task 1 found `dash-only`, replace the `parent = ...` line with `parent = "spaces/-"` and append `f' AND space.name = "{space_name}"'` to `filter_str` when `space_name` is set.

```python
async def search_space_messages(query: str,
                                space_name: Optional[str] = None,
                                limit: int = 50,
                                page_token: Optional[str] = None) -> Dict:
    """Full-text search across Google Chat messages via spaces.messages.search.

    Args:
        query: Free-text search string, passed through as the API filter
        space_name: Optional 'spaces/XXXX' to scope the search; None searches
                    every accessible space
        limit: Max messages in this page of results (clamped to 1..MAX_MESSAGES)
        page_token: Opaque continuation token from a previous call's nextPageToken

    Returns:
        {'messages': [...], 'nextPageToken': str or None}

    Raises:
        Exception: If authentication fails, preview access is unavailable, or the
                   API request fails
    """
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    limit = max(1, min(limit, MAX_MESSAGES))
    parent = space_name or "spaces/-"
    filter_str = query

    results = []
    next_token = page_token
    try:
        while len(results) < limit:
            body = {
                'filter': filter_str,
                'pageSize': min(100, limit - len(results)),
            }
            if next_token:
                body['pageToken'] = next_token

            response = _chat_api_post(f"{parent}/messages:search", body, creds)
            results.extend(response.get('messages', []))
            next_token = response.get('nextPageToken')
            if not next_token:
                break
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:500]
        if e.code in (403, 404):
            raise Exception(
                "Failed to search messages: the spaces.messages.search method is in "
                "Google Workspace Developer Preview and this account/project appears "
                f"to have lost access (HTTP {e.code}). Other tools are unaffected. "
                f"API said: {detail}"
            )
        if e.code == 400:
            raise Exception(
                f"Failed to search messages: API rejected filter {filter_str!r} "
                f"(HTTP 400). API said: {detail}"
            )
        raise Exception(f"Failed to search messages: HTTP {e.code} {detail}")
    except Exception as e:
        raise Exception(f"Failed to search messages: {str(e)}")

    if not FILTER_MESSAGES:
        return {'messages': results, 'nextPageToken': next_token}

    filtered_messages = []
    for msg in results:
        sender = msg.get('sender', {})
        display_name = get_user_display_name(sender, creds) if sender else 'Unknown'
        client_msg_id = msg.get('clientAssignedMessageId', '')
        name = msg.get('name', '')
        space = msg.get('space', {}).get('name') or '/'.join(name.split('/')[:2])
        filtered_messages.append({
            'name': name,
            'space': space,
            'sender': display_name,
            'sender_type': sender.get('type', 'HUMAN'),
            'sent_by_app': client_msg_id.startswith(APP_MESSAGE_PREFIX) if client_msg_id else False,
            'createTime': msg.get('createTime'),
            'text': msg.get('text'),
            'thread': msg.get('thread'),
        })

    return {'messages': filtered_messages, 'nextPageToken': next_token}
```

Note: `prefetch_space_members` is deliberately NOT called — results span many spaces and it is per-space. `get_user_display_name` resolves names on its own (cache → inline displayName → People API → raw id). Do not add it.

Add `import urllib.error` alongside the existing `urllib.request` import at the top if it is not already there.

- [ ] **Step 3: Write the verification script**

```python
# /tmp/verify_search.py
import asyncio, sys
sys.path.insert(0, '/Users/husni/.genduk/mcp-servers/google-chat-mcp-server')
import google_chat

google_chat.set_token_path('~/.genduk/credentials/google-chat/token.json')

QUERY = "test"   # a keyword you know exists across >1 space

async def main():
    r = await google_chat.search_space_messages(QUERY, limit=3)
    print("A. limit=3 ->", len(r['messages']), "msgs, nextPageToken:", bool(r['nextPageToken']))
    assert len(r['messages']) <= 3
    assert all('space' in m and 'sender' in m for m in r['messages'])
    spaces = {m['space'] for m in r['messages']}
    print("   spaces seen:", spaces)

    r2 = await google_chat.search_space_messages(QUERY, limit=3, page_token=r['nextPageToken'])
    names1 = {m['name'] for m in r['messages']}
    names2 = {m['name'] for m in r2['messages']}
    print("B. page 2 ->", len(r2['messages']), "msgs, overlap with page 1:", len(names1 & names2))
    assert not (names1 & names2), "pagination returned duplicates"

    r6 = await google_chat.search_space_messages(QUERY, limit=6)
    print("C. limit=6 ->", len(r6['messages']),
          "same as page1+page2:", [m['name'] for m in r6['messages']] == [m['name'] for m in r['messages'] + r2['messages']])

    one = list(spaces)[0]
    rs = await google_chat.search_space_messages(QUERY, space_name=one, limit=5)
    print("D. scoped to", one, "->", {m['space'] for m in rs['messages']})
    assert all(m['space'] == one for m in rs['messages'])

    rn = await google_chat.search_space_messages("zzqqxx-no-such-keyword-9182", limit=5)
    print("E. nonsense query ->", rn)
    assert rn['messages'] == [] and rn['nextPageToken'] is None

    google_chat.set_filter_messages(False)
    rr = await google_chat.search_space_messages(QUERY, limit=2)
    print("F. raw mode keys:", sorted(rr['messages'][0].keys())[:8] if rr['messages'] else "no results")

    rc = await google_chat.search_space_messages(QUERY, limit=5000)
    print("G. limit=5000 ->", len(rc['messages']), "(must be <= 1000)")
    assert len(rc['messages']) <= 1000

asyncio.run(main())
```

- [ ] **Step 4: Run it**

Run: `cd /Users/husni/.genduk/mcp-servers/google-chat-mcp-server && uv run python /tmp/verify_search.py`

Expected: all seven checks (A–G) print and no assertion fires. Specifically A shows ≤3 messages each carrying `space` and `sender`; B shows overlap `0`; C prints `True`; D shows a single space; E prints the empty result; G prints ≤1000.

If B fails with duplicates, the `pageSize = min(100, limit - len(results))` clamp or the `next_token` seeding is wrong — fix before proceeding, do not paper over it.

- [ ] **Step 5: Commit**

```bash
git add google_chat.py
git commit -m "feat: add search_space_messages via raw Chat API search endpoint

spaces.messages.search is absent from the discovery document, so it goes
through a raw authenticated POST. Returns a page plus its continuation
token so callers can advance without re-fetching earlier results."
```

---

### Task 3: Register the `search_messages` MCP tool

**Files:**
- Modify: `server.py` — add the tool after `get_messages` (ends at line 71), before `get_members` (line 73).

**Interfaces:**
- Consumes: `search_space_messages(query, space_name, limit, page_token) -> Dict` from Task 2.
- Produces: MCP tool `search_messages`.

- [ ] **Step 1: Add the tool**

Follow the file's existing convention: import the `google_chat` function inside the tool body (see `get_messages` at server.py:47).

```python
@mcp.tool()
async def search_messages(query: str,
                          space_name: str = None,
                          limit: int = 50,
                          page_token: str = None) -> Dict:
    """Full-text search for Google Chat messages by content.

    Searches every space you have access to by default, or a single space when
    space_name is given. Use this to find a message or thread when you know
    roughly what was said but not where or when — get_messages requires you to
    already know the space and date.

    Results come back one page at a time. If the page you get back is not enough,
    call again with page_token set to the nextPageToken you received — that
    continues where you left off instead of re-fetching what you already have.
    Reuse the same query and space_name when continuing; the token is only valid
    for that combination.

    Args:
        query: Text to search for
        space_name: Optional 'spaces/XXXX' to restrict the search to one space
        limit: Max messages in this page (default 50, capped at 1000)
        page_token: nextPageToken from a previous call, to fetch the next page

    Returns:
        {'messages': [...], 'nextPageToken': str or None} — nextPageToken is None
        when there are no further results

    Raises:
        Exception: If not authenticated, or if the search API is unavailable
    """
    from google_chat import search_space_messages
    return await search_space_messages(query, space_name, limit, page_token)
```

- [ ] **Step 2: Verify the tool is registered**

Run:
```bash
cd /Users/husni/.genduk/mcp-servers/google-chat-mcp-server && uv run python -c "
import asyncio, server
print(sorted(asyncio.run(server.mcp.get_tools()).keys()))
"
```
Expected: the printed list includes `search_messages`. If `get_tools()` is not available on this fastmcp version, fall back to `uv run python -c "import server; print(server.search_messages)"` — a bound tool object printing without ImportError is sufficient.

- [ ] **Step 3: Verify the server still boots**

Run: `cd /Users/husni/.genduk/mcp-servers/google-chat-mcp-server && timeout 5 uv run server.py --token-path ~/.genduk/credentials/google-chat/token.json; echo "exit=$?"`

Expected: no traceback; `exit=124` (timeout killed a healthy server) is the success signal. Any Python traceback is a failure.

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat: register search_messages MCP tool"
```

---

### Task 4: Document the tool

**Files:**
- Modify: `README.md:78-92` (Tools table)
- Modify: `CLAUDE.md:20` (the tool list in the Architecture section)

**Interfaces:**
- Consumes: the final tool signature from Task 3.
- Produces: nothing downstream.

- [ ] **Step 1: Add the README table row**

Insert after the `get_message(message_name)` row in the Tools table:

```markdown
| `search_messages(query, space_name?, limit?, page_token?)` | Full-text search across all spaces (or one). Returns `{messages, nextPageToken}`; pass the token back to page forward |
```

Do not fix the other rows in that table — several use stale tool names, but that is a separate concern.

- [ ] **Step 2: Add it to CLAUDE.md's tool list**

In the numbered "MCP Server (`server.py`)" item, append `search_messages` to the comma-separated tool list.

- [ ] **Step 3: Add the preview caveat to README**

Immediately below the Tools table (before the `**Mentions:**` line), add:

```markdown
> **Note:** `search_messages` uses the Chat API's `spaces.messages.search` endpoint,
> which is in Google Workspace Developer Preview. It works with the existing
> `chat.messages` scope and needs no re-auth, but Google may change or withdraw it.
> If that happens the tool raises a clear error and every other tool keeps working.
```

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document search_messages tool and its preview caveat"
```

---

## Self-review notes

- **Spec coverage:** tool surface + limit/page_token (Task 3), parent scoping and filter form (Task 1 → Task 2), urllib transport and no new deps (Task 2 step 1 + Global Constraints), pagination without truncation (Task 2 step 2), `space` field and FILTER_MESSAGES/`--raw-messages` (Task 2 step 2, verified F), no prefetch_space_members (Task 2 step 2 note), preview-aware 403/404/400 errors (Task 2 step 2), all seven spec test cases (Task 2 step 3, A–G). Out-of-scope items are not planned, correctly.
- **Type consistency:** `search_space_messages` keeps the same four-parameter signature and `{'messages', 'nextPageToken'}` return shape in Tasks 2, 3 and 4. `_chat_api_post(path, body, creds)` is defined and called with the same arity.
- **Known unknown, handled not hidden:** the top-level result key (`messages` vs `results`) and parent-scoping support are resolved by Task 1 before Task 2's code is written; Task 2 states its assumptions explicitly and tells the implementer exactly what to change if Task 1 disagreed.
