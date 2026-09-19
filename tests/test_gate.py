import json
import unittest
from unittest import mock

import gate
from gate import HOLD, INTERJECT, REACT, REPLY, Gate, Jev, JevError, Message, Policy, decide, shortcut

POLICY = Policy(name='Genduk', description='An engineering assistant.', aliases=['Nduk'])
ENV = {'CHANNEL_GATE': 'jev', 'CHANNEL_GATE_DESCRIPTION': 'An engineering assistant.', 'OPENROUTER_API_KEY': 'k'}


def answers(addressed=0.0, wants_reply=0.0, could_help=0.0, personal=0.0, audience='group'):
    return {'addressed': {'type': 'noul', 'noul': addressed},
            'wants_reply': {'type': 'noul', 'noul': wants_reply},
            'audience': {'type': 'choice', 'choice': audience, 'probabilities': {audience: 1.0}},
            'could_help': {'type': 'noul', 'noul': could_help},
            'personal': {'type': 'noul', 'noul': personal}}


def response(status, body):
    r = mock.MagicMock(status_code=status, text=json.dumps(body))
    r.json.return_value = body
    return r


class DecideTest(unittest.TestCase):
    """The rule, over the scores Jev gave six hand-written chats in the first probe."""

    def check(self, expected, **s):
        self.assertEqual(decide(POLICY, gate.scores(answers(**s))), expected)

    def test_a_request_by_name_is_a_reply(self):
        self.check(REPLY, addressed=0.96, wants_reply=0.90, could_help=0.97, personal=0.23)

    def test_a_follow_up_to_the_bot_is_a_reply(self):
        self.check(REPLY, addressed=0.95, wants_reply=0.92, could_help=0.96, personal=0.32)

    def test_thanks_to_the_bot_is_a_reaction(self):
        self.check(REACT, addressed=0.96, wants_reply=0.20, could_help=0.05, personal=0.25)

    def test_an_open_problem_between_others_is_chimed_in_on(self):
        self.check(INTERJECT, addressed=0.03, wants_reply=0.54, could_help=0.95, personal=0.14)

    def test_small_talk_is_held(self):
        self.check(HOLD, addressed=0.03, wants_reply=0.18, could_help=0.33, personal=0.45)

    def test_a_stale_ambiguous_follow_up_is_held(self):
        self.check(HOLD, addressed=0.43, wants_reply=0.51, could_help=0.51, personal=0.27)

    def test_a_personal_conversation_is_never_chimed_in_on(self):
        self.check(HOLD, addressed=0.03, wants_reply=0.14, could_help=0.9, personal=0.94)


class ShortcutTest(unittest.TestCase):
    def test_a_mention_or_a_reply_to_the_assistant_skips_jev(self):
        jev = mock.MagicMock()
        g = Gate(POLICY, jev)
        d = g.judge('group', [Message('Ana', 'lunch?', 30), Message('Budi', '@genduk cek', 5, mentions_assistant=True, new=True)])
        self.assertEqual((d.action, d.bypass), (REPLY, 'mention'))
        self.assertEqual(shortcut([Message('Ana', 'why?', 5, replies_to_assistant=True, new=True)]), 'reply_to_assistant')
        jev.ask.assert_not_called()

    def test_another_bot_or_the_assistant_itself_is_no_shortcut(self):
        self.assertEqual(shortcut([Message('ci', '@genduk done', 5, mentions_assistant=True, from_bot=True, new=True)]), '')
        self.assertEqual(shortcut([Message('Genduk', 'see @genduk', 5, mentions_assistant=True, from_assistant=True, new=True)]), '')


class StateTest(unittest.TestCase):
    def test_state_carries_the_assistant_and_only_the_flags_that_are_set(self):
        s = gate.state(POLICY, 'group', [
            Message('Genduk', 'deployed', 150, thread='T1', from_assistant=True),
            Message('Ana', 'x' * 500, 15, thread='T1', new=True, assistant_action='stayed_silent')])
        self.assertEqual(s['assistant'], {'name': 'Genduk', 'description': 'An engineering assistant.', 'aliases': ['Nduk']})
        self.assertEqual(s['messages'][0], {'sender': 'Genduk', 'text': 'deployed', 'ago_seconds': 150, 'thread': 'T1',
                                            'from_assistant': True})
        self.assertEqual(len(s['messages'][1]['text']), gate.TEXT_CHARS)
        self.assertTrue(s['messages'][1]['new'])
        self.assertEqual(s['messages'][1]['assistant_action'], 'stayed_silent')


class JevTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.jev = Jev(ENV, sleep=self.sleeps.append)

    def test_the_body_and_the_key_go_to_the_decisions_endpoint(self):
        with mock.patch.object(gate.requests, 'post', return_value=response(200, {'answers': answers()})) as post:
            self.jev.ask({'messages': []})
        self.assertEqual(post.call_args.args[0], 'https://openrouter.ai/api/alpha/decisions')
        self.assertEqual(post.call_args.kwargs['headers'], {'Authorization': 'Bearer k'})
        body = post.call_args.kwargs['json']
        self.assertEqual((body['model'], set(body['questions'])), ('typesafe/jev-1.13', set(gate.QUESTIONS)))

    def test_overload_is_retried_briefly(self):
        replies = [response(529, {}), response(429, {}), response(200, {'answers': answers()})]
        with mock.patch.object(gate.requests, 'post', side_effect=replies):
            self.jev.ask({})
        self.assertEqual(self.sleeps, [1, 2])

    def test_a_lasting_overload_or_another_error_fails_loud(self):
        with mock.patch.object(gate.requests, 'post', return_value=response(529, {})):
            with self.assertRaises(JevError):
                self.jev.ask({})
        with mock.patch.object(gate.requests, 'post', return_value=response(400, {'error': 'bad'})):
            with self.assertRaisesRegex(JevError, '400'):
                self.jev.ask({})
        self.assertEqual(self.sleeps, [1, 2])   # a 400 is not retried

    def test_an_answer_missing_a_question_fails_loud(self):
        partial = answers()
        del partial['personal']
        with mock.patch.object(gate.requests, 'post', return_value=response(200, {'answers': partial})):
            with self.assertRaisesRegex(JevError, 'every question'):
                self.jev.ask({})

    def test_a_network_error_fails_loud(self):
        with mock.patch.object(gate.requests, 'post', side_effect=gate.requests.ConnectionError('down')):
            with self.assertRaisesRegex(JevError, 'down'):
                self.jev.ask({})


class FromEnvTest(unittest.TestCase):
    def test_rules_by_default(self):
        self.assertIsNone(gate.from_env('Genduk', {}))

    def test_jev_reads_its_policy(self):
        g = gate.from_env('Genduk', {**ENV, 'CHANNEL_GATE_ALIASES': 'Nduk, Genduk ', 'CHANNEL_GATE_INTERJECT': '0.9'})
        self.assertEqual((g.policy.aliases, g.policy.interject, g.policy.reply), (['Nduk', 'Genduk'], 0.9, 0.6))

    def test_a_bad_setup_fails_the_start(self):
        for env in ({'CHANNEL_GATE': 'llm'}, {**ENV, 'CHANNEL_GATE_DESCRIPTION': ' '},
                    {k: v for k, v in ENV.items() if k != 'OPENROUTER_API_KEY'}):
            with self.assertRaises(ValueError):
                gate.from_env('Genduk', env)

    def test_the_key_can_come_from_a_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.key') as f:
            f.write('from-file\n')
            f.flush()
            env = {k: v for k, v in ENV.items() if k != 'OPENROUTER_API_KEY'}
            self.assertEqual(Jev({**env, 'CHANNEL_GATE_KEY_FILE': f.name}).key, 'from-file')


if __name__ == '__main__':
    unittest.main()
