import tempfile
import unittest
from pathlib import Path
from unittest import mock

import channel
from channel import Channel, ChannelStore, should_deliver, to_notification
from google_chat import APP_MESSAGE_PREFIX

OWNER = 'users/111'
OTHER = 'users/222'
SPACE = 'spaces/AAA'


def message(name, sender=OWNER, text='hi', create_time='2026-09-18T06:00:01.000000Z', client_id=None):
    msg = {'name': f'{SPACE}/messages/{name}', 'sender': {'name': sender, 'type': 'HUMAN'},
           'text': text, 'createTime': create_time, 'thread': {'name': f'{SPACE}/threads/T1'}}
    if client_id:
        msg['clientAssignedMessageId'] = client_id
    return msg


class GateTest(unittest.TestCase):
    def test_allowed_sender_is_delivered(self):
        self.assertTrue(should_deliver(message('m1'), [OWNER]))

    def test_sender_not_on_allowlist_is_dropped(self):
        self.assertFalse(should_deliver(message('m1', sender=OTHER), [OWNER]))

    def test_own_reply_is_dropped_even_from_allowed_sender(self):
        msg = message('m1', client_id=f'{APP_MESSAGE_PREFIX}abc')
        self.assertFalse(should_deliver(msg, [OWNER]))

    def test_trigger_requires_a_standalone_case_insensitive_mention(self):
        self.assertTrue(should_deliver(message('m1', text='hey @Claude check this'), [OWNER], '@claude'))
        self.assertTrue(should_deliver(message('m1', text='@claude: deploy'), [OWNER], '@claude'))
        self.assertFalse(should_deliver(message('m1', text='lunch?'), [OWNER], '@claude'))
        self.assertFalse(should_deliver(message('m1', text='ask @claudette'), [OWNER], '@claude'))

    def test_trigger_does_not_bypass_the_sender_allowlist(self):
        self.assertFalse(should_deliver(message('m1', sender=OTHER, text='@claude run it'), [OWNER], '@claude'))


class NotificationTest(unittest.TestCase):
    def test_meta_keys_are_identifiers_and_carry_routing(self):
        params = to_notification(message('m1', text='deploy it'), SPACE, 'Husni')
        self.assertEqual(params['content'], 'deploy it')
        self.assertEqual(params['meta']['chat_id'], SPACE)
        self.assertEqual(params['meta']['thread_name'], f'{SPACE}/threads/T1')
        self.assertEqual(params['meta']['sender_id'], OWNER)
        for key in params['meta']:
            self.assertRegex(key, r'^[A-Za-z0-9_]+$')

    def test_attachment_names_are_appended(self):
        msg = message('m1', text='see file')
        msg['attachment'] = [{'contentName': 'log.txt'}]
        self.assertEqual(to_notification(msg, SPACE, 'x')['content'], 'see file\n[attachments: log.txt]')


class StoreTest(unittest.TestCase):
    def test_round_trip_and_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            store = ChannelStore(Path(d) / 'nested' / 'state.json')
            self.assertEqual(store.load(), {})
            store.save({SPACE: {'allowed_senders': [OWNER], 'trigger': '@claude'}})
            self.assertEqual(store.load(), {SPACE: {'allowed_senders': [OWNER], 'trigger': '@claude'}})
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)


class PollTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = ChannelStore(Path(self.dir.name) / 'state.json')
        self.store.save({SPACE: {'allowed_senders': [OWNER], 'trigger': None}})
        self.chat = mock.MagicMock()
        patches = [
            mock.patch.object(channel, 'get_credentials', return_value=object()),
            mock.patch.object(channel, '_get_service', return_value=self.chat),
            mock.patch.object(channel, 'get_user_display_name', return_value='Husni'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.dir.cleanup()

    def list_returns(self, messages):
        self.chat.spaces().messages().list.return_value.execute.return_value = {'messages': messages}

    def test_first_poll_only_sets_cursor_so_history_is_not_replayed(self):
        ch = Channel(self.store, 5)
        self.list_returns([message('old')])
        self.assertEqual(ch.poll_once(), [])
        self.assertIn(SPACE, ch.cursors)

    def test_delivers_gated_messages_and_advances_cursor(self):
        ch = Channel(self.store, 5)
        ch.cursors[SPACE] = '2026-09-18T06:00:00Z'
        self.list_returns([
            message('m1', text='from owner', create_time='2026-09-18T06:00:01Z'),
            message('m2', sender=OTHER, text='from other', create_time='2026-09-18T06:00:02Z'),
            message('m3', text='claude reply', create_time='2026-09-18T06:00:03Z',
                    client_id=f'{APP_MESSAGE_PREFIX}x'),
        ])
        out = ch.poll_once()
        self.assertEqual([n['content'] for n in out], ['from owner'])
        self.assertEqual(ch.cursors[SPACE], '2026-09-18T06:00:03Z')
        kwargs = self.chat.spaces().messages().list.call_args.kwargs
        self.assertEqual(kwargs['filter'], 'createTime > "2026-09-18T06:00:00Z"')

    def test_trigger_set_by_watch_filters_the_poll(self):
        ch = Channel(self.store, 5)
        self.chat.spaces().get.return_value.execute.return_value = {}
        ch.watch(SPACE, [OWNER], '  @claude ')
        self.assertEqual(self.store.load()[SPACE]['trigger'], '@claude')
        self.list_returns([
            message('m1', text='just chatting', create_time='2026-09-18T07:00:01Z'),
            message('m2', text='@claude summarize', create_time='2026-09-18T07:00:02Z'),
        ])
        self.assertEqual([n['content'] for n in ch.poll_once()], ['@claude summarize'])

    def test_failed_space_keeps_its_cursor(self):
        ch = Channel(self.store, 5)
        ch.cursors[SPACE] = '2026-09-18T06:00:00Z'
        self.chat.spaces().messages().list.return_value.execute.side_effect = RuntimeError('503')
        self.assertEqual(ch.poll_once(), [])
        self.assertEqual(ch.cursors[SPACE], '2026-09-18T06:00:00Z')

    def test_watch_defaults_allowlist_to_self_and_unwatch_removes(self):
        ch = Channel(ChannelStore(Path(self.dir.name) / 'fresh.json'), 5)
        self.chat.spaces().get.return_value.execute.return_value = {'displayName': 'Claude'}
        with mock.patch.object(channel, 'self_user_id', return_value=OWNER):
            result = ch.watch(SPACE)
        self.assertEqual(result['allowed_senders'], [OWNER])
        self.assertIsNone(result['trigger'])
        self.assertEqual(ch.list_watched(),
                         {'spaces': [{'space_name': SPACE, 'allowed_senders': [OWNER], 'trigger': None}]})
        self.assertEqual(ch.unwatch(SPACE), {'space_name': SPACE, 'removed': True})
        self.assertEqual(ch.list_watched(), {'spaces': []})


if __name__ == '__main__':
    unittest.main()
