"""Decide whether a group chat message wakes the agent, with TypeSafe's Jev.

Knows nothing about Google Chat: the channel maps its messages to Message and
acts on the Decision. That boundary is what lets the same rules run behind
another chat network later.

The agent is a member of the chat, not a help desk: it answers what is for it,
reacts with an emoji where a person would, and joins in, to help or just to
talk, where that would feel natural.

Two shortcuts never ask Jev: a message that mentions the agent or replies to
one of its messages. Everyone in the chat can count on those reaching it,
whatever Jev thinks and whether or not Jev answers. Everything else is one
call per batch, with the recent conversation as the state and typed questions
about it, and the rule in decide() turns the answers into one action.
"""
import dataclasses
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

REPLY = 'reply'
REACT = 'react'
INTERJECT = 'interject'
HOLD = 'hold'

TEXT_CHARS = 400

# Emoji a reaction may use, by the name Jev chooses.
EMOJI = {'thumbs_up': '👍', 'laugh': '😂', 'heart': '❤️', 'pray': '🙏'}


def questions(name: str) -> Dict:
    """The typed questions, about the agent by its name."""
    return {
        'addressed': {
            'type': 'noul',
            'instructions': f'Is the newest message addressed to {name}, or a follow-up to something {name} said?',
            'criteria': {'true': f'It speaks to {name}, by name, by nickname, or by continuing an exchange with {name}.',
                         'false': 'It speaks to another person, to the group, or to nobody.'},
        },
        'wants_reply': {
            'type': 'noul',
            'instructions': f'Would the people in this chat expect {name} to answer the newest message now?',
        },
        'audience': {
            'type': 'choice',
            'instructions': 'Who is the newest message for?',
            'criteria': {'agent': f'{name}.', 'person': 'A specific other person.',
                         'group': 'The whole group.', 'nobody': 'Nobody in particular, such as a reaction or small talk.'},
        },
        # Without the last clause, work questions tagged to a colleague scored as
        # open problems for the agent in a replay of a work space.
        'could_help': {
            'type': 'noul',
            'instructions': ('Is there an open question or problem in the recent conversation that nobody has answered '
                             f'yet and that {name}, as described, could help with? A question that mentions or names '
                             'someone is for them, unless the conversation description says anyone may answer.'),
        },
        # A yes/no "would it feel natural to join" came out near 0.9 for every chat in
        # the probe, a private one included. Graded levels tell them apart.
        'natural_to_join': {
            'type': 'score',
            'instructions': f'How invited would a coworker like {name} feel to jump into the conversation now?',
            'criteria': ['Not at all: it is between other people, private, or over.',
                         'Somewhat: they could, but staying quiet is just as natural.',
                         'Clearly: open banter, a joke to the room, or a question to everyone.'],
        },
        'personal': {
            'type': 'noul',
            'instructions': f'Is this a personal or sensitive conversation where {name} chiming in would be unwelcome?',
        },
        'reaction': {
            'type': 'choice',
            'instructions': 'Which emoji reaction would a friendly coworker put on the newest message, if any?',
            'criteria': {'none': 'No reaction fits.', 'thumbs_up': 'An acknowledgement, an ok, or agreement.',
                         'laugh': 'Something funny: a joke, banter, or laughter.',
                         'heart': 'Something warm: good news, appreciation, or a kind word.',
                         'pray': 'Thanks, or a wish for luck.'},
        },
    }


@dataclasses.dataclass
class Message:
    """One chat message as the gate sees it, whatever network it came from."""
    sender: str
    text: str
    ago_seconds: int
    thread: str = ''
    from_agent: bool = False
    from_bot: bool = False
    mentions_agent: bool = False
    replies_to_agent: bool = False
    # It @-mentions someone other than the agent.
    mentions_others: bool = False
    # Its sender edited it after sending.
    edited: bool = False
    new: bool = False
    # What the agent did about this message: 'stayed_silent' or 'reacted'.
    agent_action: str = ''

    def state(self) -> Dict:
        out = {'sender': self.sender, 'text': self.text[:TEXT_CHARS], 'ago_seconds': self.ago_seconds}
        for key in ('thread', 'agent_action'):
            if getattr(self, key):
                out[key] = getattr(self, key)
        for key in ('from_agent', 'from_bot', 'mentions_agent', 'replies_to_agent', 'mentions_others', 'edited',
                    'new'):
            if getattr(self, key):
                out[key] = True
        return out


@dataclasses.dataclass
class Policy:
    """Who the agent is and how eager it is. Read once from the environment."""
    name: str
    description: str
    aliases: List[str] = dataclasses.field(default_factory=list)
    reply: float = 0.6
    interject: float = 0.8
    join: float = 0.85
    react: float = 0.8
    personal: float = 0.5
    interject_quiet: float = 30.0

    @classmethod
    def from_env(cls, name: str, env=os.environ) -> 'Policy':
        description = env.get('CHANNEL_GATE_DESCRIPTION', '').strip()
        if not description:
            raise ValueError('CHANNEL_GATE=jev needs CHANNEL_GATE_DESCRIPTION: who the agent is in the chat and what '
                             'it does, which is all Jev knows when it decides whether the agent would join in')
        aliases = [a.strip() for a in env.get('CHANNEL_GATE_ALIASES', '').split(',') if a.strip()]

        def number(key: str, default: float) -> float:
            return float(env.get(key, default))
        return cls(name=name, description=description, aliases=aliases,
                   reply=number('CHANNEL_GATE_REPLY', cls.reply),
                   interject=number('CHANNEL_GATE_INTERJECT', cls.interject),
                   join=number('CHANNEL_GATE_JOIN', cls.join),
                   react=number('CHANNEL_GATE_REACT', cls.react),
                   personal=number('CHANNEL_GATE_PERSONAL', cls.personal),
                   interject_quiet=number('CHANNEL_GATE_INTERJECT_QUIET', cls.interject_quiet))


@dataclasses.dataclass
class Decision:
    action: str
    # Set when a shortcut decided and Jev was not asked: 'mention' or 'reply_to_agent'.
    bypass: str = ''
    scores: Dict = dataclasses.field(default_factory=dict)
    # For INTERJECT, why: 'help' (an open problem) or 'join' (the talk itself).
    reason: str = ''
    # For REACT, the emoji.
    emoji: str = ''


def shortcut(new: List[Message]) -> str:
    """The shortcut that lets a batch through without Jev, or ''."""
    for msg in new:
        if msg.from_bot or msg.from_agent:
            continue
        if msg.mentions_agent:
            return 'mention'
        if msg.replies_to_agent:
            return 'reply_to_agent'
    return ''


# An empty message (an attachment alone) or one of custom emoji shortcodes only.
TRIVIAL_RE = re.compile(r'^\s*(:[\w+-]+:\s*)*$')


def trivial(new: List[Message]) -> bool:
    """Nothing in the batch to judge, so it is held without asking Jev."""
    return all(TRIVIAL_RE.match(m.text) for m in new)


def state(policy: Policy, kind: str, messages: List[Message], norms: str = '') -> Dict:
    """norms: how this conversation works, in the operator's words: what it is for, and
    whether anyone may jump in. The same text tells two chats' questions apart."""
    agent = {'name': policy.name, 'description': policy.description}
    if policy.aliases:
        agent['nicknames'] = policy.aliases
    conversation = {'kind': kind}
    if norms:
        conversation['description'] = norms
    return {'agent': agent, 'conversation': conversation, 'messages': [m.state() for m in messages]}


def scores(answers: Dict) -> Dict:
    """Jev's answers flattened to numbers from 0 to 1, plus each choice and its probabilities."""
    out = {}
    for key, answer in answers.items():
        if answer.get('type') == 'noul':
            out[key] = answer['noul']
        elif answer.get('type') == 'score':
            out[key] = answer['score'] / max(len(answer.get('legend', {})) - 1, 1)
        elif answer.get('type') == 'choice':
            out[key] = answer['choice']
            out[f'{key}_p'] = answer.get('probabilities', {})
    return out


def decide(policy: Policy, s: Dict) -> Decision:
    """The action for a batch Jev scored.

    A reply needs the chat to expect one. Addressed but not expecting a reply is
    thanks or an ok: a reaction, so nobody waits and the agent is not woken for
    it. Nothing personal is joined. Otherwise the agent joins in where there is
    an open problem it can help with, or where joining the talk would feel
    natural, and a message it stays out of can still get a reaction a person
    would give, such as a laugh at a joke.
    """
    confident = s.get('reaction_p', {}).get(s['reaction'], 0) >= policy.react
    if s['wants_reply'] >= policy.reply:
        return Decision(REPLY, scores=s)
    if s['addressed'] >= policy.reply:
        return Decision(REACT, scores=s, emoji=EMOJI.get(s['reaction'], EMOJI['thumbs_up']))
    if s['personal'] >= policy.personal:
        return Decision(HOLD, scores=s)
    if s['could_help'] >= policy.interject:
        return Decision(INTERJECT, scores=s, reason='help')
    if s['natural_to_join'] >= policy.join:
        return Decision(INTERJECT, scores=s, reason='join')
    if confident and s['reaction'] in EMOJI:
        return Decision(REACT, scores=s, emoji=EMOJI[s['reaction']])
    return Decision(HOLD, scores=s)


class JevError(Exception):
    pass


class Jev:
    """The decisions endpoint. OpenRouter's by default; TypeSafe's own takes the same body."""
    RETRY_STATUS = (429, 529)
    TIMEOUT = 10
    BACKOFF = (1, 2)

    def __init__(self, env=os.environ, sleep=time.sleep):
        # A file keeps the key out of the environment the agent's own shell inherits.
        key_file = env.get('CHANNEL_GATE_KEY_FILE', '')
        self.key = Path(key_file).expanduser().read_text().strip() if key_file else env.get('OPENROUTER_API_KEY', '')
        # Another URL may be a proxy that adds the key itself (an exe.dev integration),
        # so the key stays off the machine. OpenRouter's own endpoint always needs one.
        if not self.key and not env.get('CHANNEL_GATE_URL'):
            raise ValueError('CHANNEL_GATE=jev needs CHANNEL_GATE_KEY_FILE, OPENROUTER_API_KEY or CHANNEL_GATE_URL')
        self.url = env.get('CHANNEL_GATE_URL', 'https://openrouter.ai/api/alpha/decisions')
        self.model = env.get('CHANNEL_GATE_MODEL', 'typesafe/jev-1.13')
        self.sleep = sleep

    def ask(self, s: Dict, questions: Dict) -> Dict:
        """Jev's answers keyed like the questions. Raises JevError when it has none.

        Retries only what the API documents as transient, and briefly: the poller
        blocks the event loop meanwhile, and must stay well under its lease.
        """
        body = {'model': self.model, 'state': s, 'questions': questions}
        headers = {'Authorization': f'Bearer {self.key}'} if self.key else {}
        for attempt in range(len(self.BACKOFF) + 1):
            try:
                response = requests.post(self.url, json=body, headers=headers, timeout=self.TIMEOUT)
            except requests.RequestException as e:
                raise JevError(f'Jev request failed: {e}') from e
            if response.status_code in self.RETRY_STATUS and attempt < len(self.BACKOFF):
                self.sleep(self.BACKOFF[attempt])
                continue
            if response.status_code != 200:
                raise JevError(f'Jev answered {response.status_code}: {response.text[:300]}')
            answers = response.json().get('answers')
            if not isinstance(answers, dict) or set(questions) - set(answers):
                raise JevError(f'Jev answered without every question: {response.text[:300]}')
            return answers
        raise AssertionError('unreachable')


class Gate:
    def __init__(self, policy: Policy, jev: Jev):
        self.policy = policy
        self.jev = jev

    def judge(self, kind: str, messages: List[Message], norms: str = '') -> Decision:
        """The decision for the messages marked new, given the ones before them."""
        new = [m for m in messages if m.new]
        bypass = shortcut(new)
        if bypass:
            return Decision(REPLY, bypass=bypass)
        if trivial(new):
            return Decision(HOLD)
        s = state(self.policy, kind, messages, norms)
        return decide(self.policy, scores(self.jev.ask(s, questions(self.policy.name))))


def from_env(name: str, env=os.environ) -> Optional[Gate]:
    """The gate CHANNEL_GATE asks for: None for 'rules' (the default), a Gate for 'jev'."""
    mode = env.get('CHANNEL_GATE', 'rules')
    if mode == 'rules':
        return None
    if mode != 'jev':
        raise ValueError(f"CHANNEL_GATE must be 'rules' or 'jev', not {mode!r}")
    return Gate(Policy.from_env(name, env), Jev(env))
