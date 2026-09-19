"""Decide whether a group chat message wakes the agent, with TypeSafe's Jev.

Knows nothing about Google Chat: the channel maps its messages to Message and
acts on the Decision. That boundary is what lets the same rules run behind
another chat network later.

Two shortcuts never ask Jev: a message that mentions the assistant or replies
to one of its messages. Everyone in the chat can count on those reaching it,
whatever Jev thinks and whether or not Jev answers. Everything else is one
call per batch, with the recent conversation as the state and five typed
questions, and the rule in decide() turns the answers into one action.
"""
import dataclasses
import logging
import os
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

QUESTIONS = {
    'addressed': {
        'type': 'noul',
        'instructions': 'Is the newest message addressed to the assistant, or a follow-up to something the assistant said?',
        'criteria': {'true': 'It speaks to the assistant, by name, by alias, or by continuing an exchange with it.',
                     'false': 'It speaks to another person, to the group, or to nobody.'},
    },
    'wants_reply': {
        'type': 'noul',
        'instructions': 'Would the people in this chat expect the assistant to answer the newest message now?',
    },
    'audience': {
        'type': 'choice',
        'instructions': 'Who is the newest message for?',
        'criteria': {'assistant': 'The assistant.', 'person': 'A specific other person.',
                     'group': 'The whole group.', 'nobody': 'Nobody in particular, such as a reaction or small talk.'},
    },
    'could_help': {
        'type': 'noul',
        'instructions': ('Is there an open question or problem in the recent conversation that nobody has answered '
                         'yet and that the assistant, as described, could help with?'),
    },
    'personal': {
        'type': 'noul',
        'instructions': 'Is this a personal or sensitive conversation where an outsider chiming in would be unwelcome?',
    },
}


@dataclasses.dataclass
class Message:
    """One chat message as the gate sees it, whatever network it came from."""
    sender: str
    text: str
    ago_seconds: int
    thread: str = ''
    from_assistant: bool = False
    from_bot: bool = False
    mentions_assistant: bool = False
    replies_to_assistant: bool = False
    new: bool = False
    # What the assistant did about this message: 'stayed_silent' or 'reacted'.
    assistant_action: str = ''

    def state(self) -> Dict:
        out = {'sender': self.sender, 'text': self.text[:TEXT_CHARS], 'ago_seconds': self.ago_seconds}
        for key in ('thread', 'assistant_action'):
            if getattr(self, key):
                out[key] = getattr(self, key)
        for key in ('from_assistant', 'from_bot', 'mentions_assistant', 'replies_to_assistant', 'new'):
            if getattr(self, key):
                out[key] = True
        return out


@dataclasses.dataclass
class Policy:
    """Who the assistant is and how eager it is. Read once from the environment."""
    name: str
    description: str
    aliases: List[str] = dataclasses.field(default_factory=list)
    reply: float = 0.6
    interject: float = 0.8
    personal: float = 0.5
    interject_quiet: float = 30.0
    interject_cooldown: float = 900.0

    @classmethod
    def from_env(cls, name: str, env=os.environ) -> 'Policy':
        description = env.get('CHANNEL_GATE_DESCRIPTION', '').strip()
        if not description:
            raise ValueError('CHANNEL_GATE=jev needs CHANNEL_GATE_DESCRIPTION: what the assistant is and what it can '
                             'help with, which is all Jev knows when it decides whether to chime in')
        aliases = [a.strip() for a in env.get('CHANNEL_GATE_ALIASES', '').split(',') if a.strip()]

        def number(key: str, default: float) -> float:
            return float(env.get(key, default))
        return cls(name=name, description=description, aliases=aliases,
                   reply=number('CHANNEL_GATE_REPLY', cls.reply),
                   interject=number('CHANNEL_GATE_INTERJECT', cls.interject),
                   personal=number('CHANNEL_GATE_PERSONAL', cls.personal),
                   interject_quiet=number('CHANNEL_GATE_INTERJECT_QUIET', cls.interject_quiet),
                   interject_cooldown=number('CHANNEL_GATE_INTERJECT_COOLDOWN', cls.interject_cooldown))


@dataclasses.dataclass
class Decision:
    action: str
    # Set when a shortcut decided and Jev was not asked: 'mention' or 'reply_to_assistant'.
    bypass: str = ''
    scores: Dict = dataclasses.field(default_factory=dict)


def shortcut(new: List[Message]) -> str:
    """The shortcut that lets a batch through without Jev, or ''."""
    for msg in new:
        if msg.from_bot or msg.from_assistant:
            continue
        if msg.mentions_assistant:
            return 'mention'
        if msg.replies_to_assistant:
            return 'reply_to_assistant'
    return ''


def state(policy: Policy, kind: str, messages: List[Message]) -> Dict:
    assistant = {'name': policy.name, 'description': policy.description}
    if policy.aliases:
        assistant['aliases'] = policy.aliases
    return {'assistant': assistant, 'conversation': {'kind': kind},
            'messages': [m.state() for m in messages]}


def scores(answers: Dict) -> Dict:
    """Jev's answers flattened to numbers, plus the audience it chose."""
    out = {}
    for key, answer in answers.items():
        if answer.get('type') == 'noul':
            out[key] = answer['noul']
        elif answer.get('type') == 'choice':
            out[key] = answer['choice']
            out[f'{key}_p'] = answer.get('probabilities', {})
    return out


def decide(policy: Policy, s: Dict) -> str:
    """The action for a batch Jev scored.

    A reply needs the chat to expect one. Addressed but not expecting a reply is
    thanks or an ok: a reaction, so nobody waits and the agent is not woken for
    it. Chiming in needs an open problem the assistant can help with, in a
    conversation that is not personal.
    """
    if s['wants_reply'] >= policy.reply:
        return REPLY
    if s['addressed'] >= policy.reply:
        return REACT
    if s['could_help'] >= policy.interject and s['personal'] < policy.personal:
        return INTERJECT
    return HOLD


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
        if not self.key:
            raise ValueError('CHANNEL_GATE=jev needs CHANNEL_GATE_KEY_FILE or OPENROUTER_API_KEY')
        self.url = env.get('CHANNEL_GATE_URL', 'https://openrouter.ai/api/alpha/decisions')
        self.model = env.get('CHANNEL_GATE_MODEL', 'typesafe/jev-1.13')
        self.sleep = sleep

    def ask(self, s: Dict, questions: Dict = QUESTIONS) -> Dict:
        """Jev's answers keyed like the questions. Raises JevError when it has none.

        Retries only what the API documents as transient, and briefly: the poller
        blocks the event loop meanwhile, and must stay well under its lease.
        """
        body = {'model': self.model, 'state': s, 'questions': questions}
        headers = {'Authorization': f'Bearer {self.key}'}
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

    def judge(self, kind: str, messages: List[Message]) -> Decision:
        """The decision for the messages marked new, given the ones before them."""
        bypass = shortcut([m for m in messages if m.new])
        if bypass:
            return Decision(REPLY, bypass=bypass)
        s = scores(self.jev.ask(state(self.policy, kind, messages)))
        return Decision(decide(self.policy, s), scores=s)


def from_env(name: str, env=os.environ) -> Optional[Gate]:
    """The gate CHANNEL_GATE asks for: None for 'rules' (the default), a Gate for 'jev'."""
    mode = env.get('CHANNEL_GATE', 'rules')
    if mode == 'rules':
        return None
    if mode != 'jev':
        raise ValueError(f"CHANNEL_GATE must be 'rules' or 'jev', not {mode!r}")
    return Gate(Policy.from_env(name, env), Jev(env))
