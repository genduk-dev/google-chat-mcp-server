# Design: `search_messages` tool backed by Chat API `spaces.messages.search`

Date: 2026-07-30
Task: 712f02c8-d1c8-4a12-a94b-57b607bfd1e3
Status: approved (operator answered both open questions)

## Problem

The server exposes 12 tools but no way to find a message by content. Today the only
path is `get_messages(space_name, start_date)` — you must already know the space and
roughly when the message was sent, then scan. Cross-space "where did we discuss X?"
is impossible.

## What exists

Google Chat has a server-side full-text search endpoint:

```
POST https://chat.googleapis.com/v1/{parent=spaces/*}/messages:search
body: { "filter": "<string>", "pageSize": 1..100 (default 25), "pageToken": "...",
        "orderBy": "createTime desc" | "relevance", "view": "SEARCH_MESSAGES_VIEW_BASIC|FULL" }
```

Live-probed against this project's existing token (`~/.genduk/credentials/google-chat/token.json`):
`POST .../spaces/-/messages:search` with `{"filter": "test", "pageSize": 5}` → **HTTP 200**
with real cross-space results (name, sender, createTime, text, annotations, thread, space).

Two constraints that shape the design:

1. **Not in the discovery document.** `hasattr(service.spaces().messages(), 'search')`
   is `False`. It cannot go through `_get_service('chat','v1',creds)`; it must be a raw
   HTTP call.
2. **Developer Preview.** Access can be revoked or the payload schema changed by Google
   at any time. Errors must be legible, not raw JSON blobs.

No new OAuth scope: `chat.messages` (already in `SCOPES`) covers it.

## Design

### Tool surface

```python
# server.py
@mcp.tool()
async def search_messages(query: str, space_name: str = None,
                          limit: int = 50, page_token: str = None) -> Dict
```

- `query` — free-text search string, passed through as the API `filter`.
- `space_name` — `spaces/XXXX` to scope; `None` (default) searches every accessible
  space via `parent="spaces/-"`.
- `limit` — max results in **this** page of the response. Default **50**, clamped to
  `MAX_MESSAGES` (1000).
- `page_token` — opaque continuation token from a previous call's `nextPageToken`.

Returns `{"messages": [...], "nextPageToken": "..." | None}`.

`limit` is a deliberate addition beyond the task description's "paginate to MAX_MESSAGES".
A wide query across all spaces can trivially return four figures of messages; dumping
that into a model context is the wrong default. 50 is enough for "find the thread".

### Continuation, not re-fetch

With `limit` alone, a caller who strikes out on the first 50 has no way to see results
51-100 except re-calling with `limit=100` — which re-pages the API from the start and
returns the same first 50 again. The Chat API has `pageToken` and no offset, so the token
must be exposed to the caller.

Hence the return type is a **dict**, not `List[Dict]`. This is a deliberate deviation
from the task's acceptance criterion wording (`-> List[Dict]`): the criterion's intent is
"the tool is registered and callable and returns filtered messages", and `messages`
carries exactly that. The alternative — smuggling the token into a trailing pseudo-record
of the list — was rejected as it makes the list non-homogeneous.

`nextPageToken` is the token the API returned alongside the last page consumed, or `None`
when the result set is exhausted. Its value is opaque and must be passed back verbatim;
it is only valid for the same `query` + `space_name`.

### Scoping: parent, not filter

When `space_name` is given, scope by **parent** (`POST /v1/{space_name}/messages:search`)
rather than appending `AND space.name = "..."` to the filter string. The endpoint's URL
pattern is `{parent=spaces/*}`, so this is the API's own scoping mechanism and avoids
guessing whether `space.name` is a filterable field in preview filter syntax.

This diverges from the task description's suggested `AND space.name = "…"` composition.
It satisfies the same acceptance criterion ("passing a space_name scopes to that space")
by a more robust route. **Verification step required** (see plan step 1): if
`parent=spaces/XXXX` is rejected, fall back to `parent=spaces/-` plus the
`AND space.name = "…"` filter clause and record which one worked.

Likewise, the verified-working payload used a **bare** filter string (`"test"`), not
`text: "test"`. The plan probes both forms live and the implementation uses whichever
the API actually accepts; bare pass-through is the working default.

### Transport: `urllib.request`, not `requests`

`requests` is **not** a project dependency (`pyproject.toml` lists only fastmcp,
google-auth, google-auth-oauthlib, google-api-python-client, fastapi, uvicorn). The
project already has a raw-HTTP precedent — `download_attachment` (google_chat.py:874-880)
builds a `urllib.request.Request` with `Authorization: Bearer {creds.token}`. Mirror
that, with a JSON body and `Content-Type: application/json`. No new dependency.

Credentials come from the existing `get_credentials()`, which already refreshes and
persists the token before returning, so `creds.token` is fresh.

### Pagination

Seed the loop with the caller's `page_token` if given. Loop on `pageToken` with
`pageSize = min(100, limit - len(accumulated))`, accumulating until `len(results) >= limit`
or the API stops returning a `nextPageToken`. `MAX_MESSAGES` remains the hard ceiling via
the `limit` clamp.

Because `pageSize` is capped to exactly what is still needed, the last page consumed never
overshoots `limit`, so no truncation is required and the `nextPageToken` returned to the
caller always points at the true next unseen result. Return `nextPageToken` as `None`
when the final response carried no `nextPageToken`.

### Field filtering

Reuse the existing `FILTER_MESSAGES` convention (honoring `--raw-messages`). When
filtering is on, each result maps to the same shape `list_space_messages` produces:

`name, sender, sender_type, sent_by_app, createTime, text, thread` — plus **`space`**,
which the other tools omit because they are single-space by construction. A cross-space
result set is meaningless without it; `space` is taken from the result's `space.name`
(falling back to parsing the `spaces/X/messages/Y` prefix of `name`).

When `FILTER_MESSAGES` is False, still unwrap each `results[i].message` — return the
raw, unfiltered message objects, not the raw `{"results": [{"message": {...}}]}`
API response shape.

### Sender display names

**Do not call `prefetch_space_members`.** It is per-space; a cross-space result set
would trigger one `memberships.list` sweep per distinct space. Instead call the existing
`get_user_display_name(sender, creds)` per message, which checks the global
`_user_display_name_cache`, then the sender's inline `displayName`, then a People API
lookup, then falls back to the raw id. Names still resolve; the lookup path is just
lazier and cache-warmed by any prior tool call.

### Error handling

Wrap the HTTP call. Distinguish the Developer-Preview failure modes from generic ones:

- **403 / 404 on the search endpoint** → raise with a message naming the cause
  explicitly: the `spaces.messages.search` method is in Google Workspace Developer
  Preview and this account/project may have lost preview access; other tools are
  unaffected.
- **400** → surface the API's own `error.message` (this is how the malformed-payload
  case was diagnosed originally), prefixed with the filter that was sent.
- Anything else → `Failed to search messages: {e}`, matching the existing
  `raise Exception(f"Failed to …")` convention throughout `google_chat.py`.

## Components

| Unit | Location | Responsibility |
|---|---|---|
| `_chat_api_post(path, body, creds)` | google_chat.py (private) | One raw authenticated JSON POST to `chat.googleapis.com/v1`; returns parsed dict, raises on HTTP error with the API's message preserved. |
| `search_space_messages(query, space_name, limit, page_token)` | google_chat.py (public async) | Parent selection, pagination loop, limit clamp, field filtering, sender resolution, preview-aware error wrapping. Returns `{"messages": [...], "nextPageToken": ...}`. |
| `search_messages(...)` | server.py | MCP tool registration + docstring; thin delegate. |

The private POST helper is separate so a future second preview-only endpoint reuses it,
and so the pagination logic can be read without HTTP plumbing interleaved.

## Testing

`tests/` is empty and the project has no test suite; this design does not introduce one.
Verification is **manual against the live API**, which is also what the task's acceptance
criteria require ("manually verified against a real space returns actual matching
messages"). Concretely:

1. `search_messages("<known keyword>")` — returns `messages` from more than one space,
   each with a `space` field.
2. `search_messages("<known keyword>", space_name="spaces/<known>")` — every result's
   `space` equals that space.
3. `limit=3` returns exactly 3 messages; `limit=5000` does not exceed `MAX_MESSAGES`.
4. **Continuation:** call with `limit=3`, then re-call with the returned `nextPageToken`.
   The second page's message `name`s must not intersect the first page's, and the two
   pages concatenated must equal a single `limit=6` call.
5. Exhausting the result set returns `nextPageToken: None`.
6. `--raw-messages` mode returns unfiltered API objects under `messages`.
7. A nonsense query returns `{"messages": [], "nextPageToken": None}`, not an error.

## Out of scope

- Client-side keyword fallback if preview access disappears. The error message tells the
  user what happened; building a scan-every-space fallback is speculative work.
- `orderBy` / `view` parameters. Defaults are fine; add them when someone needs them.
- Searching by sender, date range, or attachment presence via filter syntax — the
  `query` string is passed through, so a caller who knows the syntax can already use it.

## Probe results (2026-07-30)

Live probe against `spaces.messages.search` (`chat.googleapis.com/v1/{parent}/messages:search`),
run from `/tmp/probe_search.py` (throwaway, not committed) with `QUERY = "kaam"` and
`SPACE = "spaces/AAQAuEymweE"` (a real space with a matching message).

1. `RESULT_KEY` = **`results`** (not `messages`). Each entry is `{"message": {...}}` — the
   actual message object is nested one level under `results[i].message`, not `results[i]`
   itself. `nextPageToken` was present at the top level.
2. `FILTER_FORM` = **`bare`**. `{"filter": "kaam"}` against `spaces/-` returned HTTP 200
   with 1 matching result. `{"filter": "text: \"kaam\""}` returned HTTP 400
   `INVALID_ARGUMENT: Invalid filter query: text: "kaam"` — the field-qualified form is
   rejected outright.
3. `PARENT_SCOPING` = **`dash-only`**. `parent="spaces/AAQAuEymweE"` with a bare filter
   returned HTTP 400: `Invalid parent. Specify 'spaces/-' to search across all spaces the
   user has access to. To limit the search to one or more spaces, use the 'space.name' or
   'space.display_name' in the 'filter' field.`

space_name scoping falls back to `parent=spaces/-` with `AND space.name = "<space_name>"` appended to the filter.

Raw probe output:

```
[bare-filter/dash] HTTP 200 keys=['results', 'nextPageToken'] count=1 nextPageToken=True
{
  "results": [
    {
      "message": {
        "name": "spaces/AAQAuEymweE/messages/u6mTJSSdfkw.u6mTJSSdfkw",
        "sender": {
          "name": "users/118395191287357300733",
          "type": "HUMAN"
        },
        "createTime": "2025-09-22T08:23:30.342815Z",
        "text": "kaam set heee",
        "thread": {
          "name": "spaces/AAQAuEymweE/threads/u6mTJSSdfkw"
        },
        "space": {
          "name": "spaces/AAQAuEymweE"
        },
        "argumentText": "kaam set heee",
        "formattedText": "kaam set heee"
      }
    }
  ],
  "nextPageToken": "TMKUjY9DRN6On_JVS7bZktNBY19N1ur_lkdC_o_jlEx4IVvGuPidQVTkiY-BSmxEbUwPQVlRTUDH7dDnQmyy7JeHqqjHLg=="
}
[qualified-filter/dash] HTTP 400: {
  "error": {
    "code": 400,
    "message": "Invalid filter query: text: \"kaam\"",
    "status": "INVALID_ARGUMENT",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.Help",
        "links": [
          {
            "description": "For example search query filters, see the search messages method reference documentation.",
            "url": "https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/search"
          }
        ]
      }
    ]
  }
}

[bare-filter/concrete] HTTP 400: {
  "error": {
    "code": 400,
    "message": "Invalid parent. Specify 'spaces/-' to search across all spaces the user has access to. To limit the search to one or more spaces, use the 'space.name' or 'space.display_name' in the 'filter' field.",
    "status": "INVALID_ARGUMENT",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.Help",
        "links": [
          {
            "description": "For example search query filters, see the search messages method reference documentation.",
            "url": "https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/search"
          }
        ]
      }
    ]
  }
}
```
