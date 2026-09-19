import json
import unittest
from unittest import mock

import gate
from gate import HOLD, INTERJECT, REACT, REPLY, Gate, Jev, JevError, Message, Policy, decide, shortcut

POLICY = Policy(name='Genduk', description='An AI software engineer on the team.', aliases=['Nduk'])
ENV = {'CHANNEL_GATE': 'jev', 'CHANNEL_GATE_DESCRIPTION': 'An AI software engineer on the team.', 'OPENROUTER_API_KEY': 'k'}
QUESTIONS = gate.questions('Genduk')


def answers(addressed=0.0, wants_reply=0.0, could_help=0.0, natural_to_join=0.0, personal=0.0,
            audience='group', reaction='none', reaction_p=1.0):
    return {'addressed': {'type': 'noul', 'noul': addressed},
            'wants_reply': {'type': 'noul', 'noul': wants_reply},
            'audience': {'type': 'choice', 'choice': audience, 'probabilities': {audience: 1.0}},
            'could_help': {'type': 'noul', 'noul': could_help},
            'natural_to_join': {'type': 'score', 'score': natural_to_join * 2,
                                'legend': {'0': 'no', '1': 'somewhat', '2': 'clearly'}},
            'personal': {'type': 'noul', 'noul': personal},
            'reaction': {'type': 'choice', 'choice': reaction, 'probabilities': {reaction: reaction_p}}}


def response(status, body):
    r = mock.MagicMock(status_code=status, text=json.dumps(body))
    r.json.return_value = body
    return r


class DecideTest(unittest.TestCase):
    """The rule, over the scores Jev gave hand-written chats in the probes."""

    def check(self, expected, **s):
        d = decide(POLICY, gate.scores(answers(**s)))
        self.assertEqual((d.action, d.reason, d.emoji), expected if isinstance(expected, tuple) else (expected, '', ''))

    def test_a_request_by_name_is_a_reply(self):
        self.check(REPLY, addressed=0.96, wants_reply=0.90, could_help=0.97, personal=0.23)

    def test_a_follow_up_to_the_bot_is_a_reply(self):
        self.check(REPLY, addressed=0.95, wants_reply=0.92, could_help=0.96, personal=0.32)

    def test_thanks_to_the_bot_is_a_reaction(self):
        self.check((REACT, '', '🙏'), addressed=0.96, wants_reply=0.20, could_help=0.05, personal=0.25, reaction='pray')

    def test_an_ok_to_the_bot_without_a_fitting_emoji_gets_a_thumbs_up(self):
        self.check((REACT, '', '👍'), addressed=0.9, wants_reply=0.1)

    def test_a_reaction_to_the_bot_takes_jev_s_emoji_even_when_unsure(self):
        self.check((REACT, '', '😂'), addressed=0.97, wants_reply=0.29, reaction='laugh', reaction_p=0.75)

    def test_an_open_problem_between_others_is_chimed_in_on(self):
        self.check((INTERJECT, 'help', ''), addressed=0.03, wants_reply=0.54, could_help=0.95, personal=0.14)

    def test_banter_the_agent_would_naturally_join_is_joined(self):
        self.check((INTERJECT, 'join', ''), natural_to_join=0.9, reaction='laugh')

    def test_a_joke_it_stays_out_of_still_gets_a_laugh(self):
        self.check((REACT, '', '😂'), natural_to_join=0.5, reaction='laugh', reaction_p=0.9)
        self.check(HOLD, natural_to_join=0.5, reaction='laugh', reaction_p=0.6)

    def test_small_talk_is_held(self):
        self.check(HOLD, addressed=0.03, wants_reply=0.18, could_help=0.33, personal=0.45)

    def test_a_stale_ambiguous_follow_up_is_held(self):
        self.check(HOLD, addressed=0.43, wants_reply=0.51, could_help=0.51, personal=0.27)

    def test_a_personal_conversation_is_never_chimed_in_on(self):
        self.check(HOLD, addressed=0.03, wants_reply=0.14, could_help=0.9, natural_to_join=0.9, personal=0.94,
                   reaction='heart')


class ShortcutTest(unittest.TestCase):
    def test_a_mention_or_a_reply_to_the_agent_skips_jev(self):
        jev = mock.MagicMock()
        g = Gate(POLICY, jev)
        d = g.judge('group', [Message('Ana', 'lunch?', 30), Message('Budi', '@genduk cek', 5, mentions_agent=True, new=True)])
        self.assertEqual((d.action, d.bypass), (REPLY, 'mention'))
        self.assertEqual(shortcut([Message('Ana', 'why?', 5, replies_to_agent=True, new=True)]), 'reply_to_agent')
        jev.ask.assert_not_called()

    def test_another_bot_or_the_agent_itself_is_no_shortcut(self):
        self.assertEqual(shortcut([Message('ci', '@genduk done', 5, mentions_agent=True, from_bot=True, new=True)]), '')
        self.assertEqual(shortcut([Message('Genduk', 'see @genduk', 5, mentions_agent=True, from_agent=True, new=True)]), '')


class QuestionsTest(unittest.TestCase):
    def test_the_questions_name_the_agent(self):
        self.assertIn('Genduk', QUESTIONS['addressed']['instructions'])
        self.assertEqual(set(QUESTIONS['reaction']['criteria']) - {'none'}, set(gate.EMOJI))


class StateTest(unittest.TestCase):
    def test_state_carries_the_agent_and_only_the_flags_that_are_set(self):
        s = gate.state(POLICY, 'group', [
            Message('Genduk', 'deployed', 150, thread='T1', from_agent=True),
            Message('Ana', 'x' * 500, 15, thread='T1', new=True, agent_action='stayed_silent')])
        self.assertEqual(s['agent'], {'name': 'Genduk', 'description': 'An AI software engineer on the team.',
                                      'nicknames': ['Nduk']})
        self.assertEqual(s['messages'][0], {'sender': 'Genduk', 'text': 'deployed', 'ago_seconds': 150, 'thread': 'T1',
                                            'from_agent': True})
        self.assertEqual(len(s['messages'][1]['text']), gate.TEXT_CHARS)
        self.assertTrue(s['messages'][1]['new'])
        self.assertEqual(s['messages'][1]['agent_action'], 'stayed_silent')


class JevTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.jev = Jev(ENV, sleep=self.sleeps.append)

    def test_the_body_and_the_key_go_to_the_decisions_endpoint(self):
        with mock.patch.object(gate.requests, 'post', return_value=response(200, {'answers': answers()})) as post:
            self.jev.ask({'messages': []}, QUESTIONS)
        self.assertEqual(post.call_args.args[0], 'https://openrouter.ai/api/alpha/decisions')
        self.assertEqual(post.call_args.kwargs['headers'], {'Authorization': 'Bearer k'})
        body = post.call_args.kwargs['json']
        self.assertEqual((body['model'], set(body['questions'])), ('typesafe/jev-1.13', set(QUESTIONS)))

    def test_overload_is_retried_briefly(self):
        replies = [response(529, {}), response(429, {}), response(200, {'answers': answers()})]
        with mock.patch.object(gate.requests, 'post', side_effect=replies):
            self.jev.ask({}, QUESTIONS)
        self.assertEqual(self.sleeps, [1, 2])

    def test_a_lasting_overload_or_another_error_fails_loud(self):
        with mock.patch.object(gate.requests, 'post', return_value=response(529, {})):
            with self.assertRaises(JevError):
                self.jev.ask({}, QUESTIONS)
        with mock.patch.object(gate.requests, 'post', return_value=response(400, {'error': 'bad'})):
            with self.assertRaisesRegex(JevError, '400'):
                self.jev.ask({}, QUESTIONS)
        self.assertEqual(self.sleeps, [1, 2])   # a 400 is not retried

    def test_an_answer_missing_a_question_fails_loud(self):
        partial = answers()
        del partial['personal']
        with mock.patch.object(gate.requests, 'post', return_value=response(200, {'answers': partial})):
            with self.assertRaisesRegex(JevError, 'every question'):
                self.jev.ask({}, QUESTIONS)

    def test_a_network_error_fails_loud(self):
        with mock.patch.object(gate.requests, 'post', side_effect=gate.requests.ConnectionError('down')):
            with self.assertRaisesRegex(JevError, 'down'):
                self.jev.ask({}, QUESTIONS)


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

    def test_a_proxy_url_needs_no_key_and_gets_no_authorization_header(self):
        env = {k: v for k, v in ENV.items() if k != 'OPENROUTER_API_KEY'}
        jev = Jev({**env, 'CHANNEL_GATE_URL': 'https://openrouter.int.exe.xyz/api/alpha/decisions'})
        with mock.patch.object(gate.requests, 'post', return_value=response(200, {'answers': answers()})) as post:
            jev.ask({}, QUESTIONS)
        self.assertEqual(post.call_args.args[0], 'https://openrouter.int.exe.xyz/api/alpha/decisions')
        self.assertEqual(post.call_args.kwargs['headers'], {})

    def test_the_key_can_come_from_a_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.key') as f:
            f.write('from-file\n')
            f.flush()
            env = {k: v for k, v in ENV.items() if k != 'OPENROUTER_API_KEY'}
            self.assertEqual(Jev({**env, 'CHANNEL_GATE_KEY_FILE': f.name}).key, 'from-file')


if __name__ == '__main__':
    unittest.main()
