import json
import os
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

    @mock.patch.object(channel, 'BOT_NAME', 'genduk')
    def test_mention_only_requires_a_standalone_case_insensitive_bot_mention(self):
        self.assertTrue(should_deliver(message('m1', text='hey @Genduk check this'), [OWNER], True))
        self.assertTrue(should_deliver(message('m1', text='@genduk: deploy'), [OWNER], True))
        self.assertFalse(should_deliver(message('m1', text='lunch?'), [OWNER], True))
        self.assertFalse(should_deliver(message('m1', text='ask @gendukku'), [OWNER], True))
        self.assertFalse(should_deliver(message('m1', text='genduk without the at sign'), [OWNER], True))
        self.assertTrue(should_deliver(message('m1', text='lunch?'), [OWNER], False))

    @mock.patch.object(channel, 'BOT_NAME', 'genduk')
    def test_mention_does_not_bypass_the_sender_allowlist(self):
        self.assertFalse(should_deliver(message('m1', sender=OTHER, text='@genduk run it'), [OWNER], True))


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


class BotNameTest(unittest.TestCase):
    def test_name_is_lowercased_and_must_fit_a_chat_message_id(self):
        from google_chat import _parse_bot_name
        self.assertEqual(_parse_bot_name(' Genduk '), 'genduk')
        self.assertEqual(_parse_bot_name('gchat-mcp'), 'gchat-mcp')
        for bad in ['', 'gen duk', 'genduk_bot', 'x' * 44]:
            with self.assertRaises(ValueError):
                _parse_bot_name(bad)
        self.assertEqual(len(f"client-{_parse_bot_name('x' * 43)}-{'0' * 12}"), 63)


class SenderFieldsTest(unittest.TestCase):
    def test_own_message_is_attributed_to_the_bot(self):
        import google_chat
        with mock.patch.object(google_chat, 'BOT_DISPLAY_NAME', 'Genduk'), \
                mock.patch.object(google_chat, 'get_user_display_name') as lookup:
            fields = google_chat._sender_fields(message('m1', client_id=f'{APP_MESSAGE_PREFIX}abc'), None)
        self.assertEqual(fields, {'sender': 'Genduk', 'sender_type': 'BOT', 'sent_by_app': True})
        lookup.assert_not_called()

    def test_human_message_keeps_its_sender(self):
        import google_chat
        with mock.patch.object(google_chat, 'get_user_display_name', return_value='Husni'):
            fields = google_chat._sender_fields(message('m1'), None)
        self.assertEqual(fields, {'sender': 'Husni', 'sender_type': 'HUMAN', 'sent_by_app': False})


class StoreTest(unittest.TestCase):
    def test_round_trip_and_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            store = ChannelStore(Path(d) / 'nested' / 'state.json')
            self.assertEqual(store.load(), {})
            store.save({SPACE: {'allowed_senders': [OWNER], 'mention_only': True}})
            self.assertEqual(store.load(), {SPACE: {'allowed_senders': [OWNER], 'mention_only': True}})
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)


class PollTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = ChannelStore(Path(self.dir.name) / 'state.json')
        self.store.save({SPACE: {'allowed_senders': [OWNER], 'mention_only': False}})
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

    @mock.patch.object(channel, 'BOT_NAME', 'genduk')
    def test_mention_only_set_by_watch_filters_the_poll(self):
        ch = Channel(self.store, 5)
        self.chat.spaces().get.return_value.execute.return_value = {}
        ch.watch(SPACE, [OWNER], mention_only=True)
        self.assertTrue(self.store.load()[SPACE]['mention_only'])
        self.list_returns([
            message('m1', text='just chatting', create_time='2026-09-18T07:00:01Z'),
            message('m2', text='@Genduk summarize', create_time='2026-09-18T07:00:02Z'),
        ])
        self.assertEqual([n['content'] for n in ch.poll_once()], ['@Genduk summarize'])

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
        self.assertFalse(result['mention_only'])
        self.assertEqual(ch.list_watched()['spaces'],
                         [{'space_name': SPACE, 'allowed_senders': [OWNER], 'mention_only': False}])
        self.assertEqual(ch.unwatch(SPACE), {'space_name': SPACE, 'removed': True})
        self.assertEqual(ch.list_watched()['spaces'], [])

    @mock.patch.object(channel, 'BOT_NAME', 'genduk')
    @mock.patch.object(channel, 'APP_MESSAGE_PREFIX', 'client-genduk-')
    def test_list_watched_reports_the_bot_identity(self):
        listed = Channel(self.store, 5).list_watched()
        self.assertEqual((listed['bot_name'], listed['mention'], listed['message_id_prefix']),
                         ('genduk', '@genduk', 'client-genduk-'))



class RunTest(unittest.TestCase):
    def test_nothing_is_polled_or_sent_before_the_client_is_initialized(self):
        import anyio
        ch = Channel(ChannelStore(Path(tempfile.mkdtemp()) / 'state.json'), 0.01)
        sent = []

        class Stream:
            async def send(self, message):
                sent.append(message)

        async def scenario():
            initialized = anyio.Event()
            with mock.patch.object(ch, 'try_acquire', return_value=True), \
                    mock.patch.object(ch, 'poll_once', return_value=[{'content': 'x', 'meta': {}}]) as poll, \
                    mock.patch.object(ch, '_save_cursors'):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(ch.run, Stream(), initialized)
                    await anyio.sleep(0.05)
                    self.assertEqual((poll.call_count, sent), (0, []))
                    initialized.set()
                    await anyio.sleep(0.05)
                    tg.cancel_scope.cancel()
            self.assertGreater(len(sent), 0)

        anyio.run(scenario)


class PollerLockTest(unittest.TestCase):
    """Two Channel objects on the same state path stand in for two channel sessions."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = Path(self.dir.name) / 'channel_state.json'
        ChannelStore(self.state).save({SPACE: {'allowed_senders': [OWNER], 'mention_only': False}})

    def channel(self):
        return Channel(ChannelStore(self.state), 5)

    def exit_process(self, ch):
        ch.release()

    def lease(self):
        return json.loads((Path(self.dir.name) / 'channel.lease').read_text())

    def age_lease(self, by):
        lease = self.lease()
        heartbeat = channel.datetime.datetime.fromisoformat(lease['heartbeat']) - by
        lease['heartbeat'] = heartbeat.isoformat()
        (Path(self.dir.name) / 'channel.lease').write_text(json.dumps(lease))

    def test_only_one_session_polls_and_the_other_takes_over_when_it_exits(self):
        a, b = self.channel(), self.channel()
        self.assertTrue(a.try_acquire())
        self.assertFalse(b.try_acquire())
        status = b.poller_status()
        self.assertEqual((status['active_here'], status['holder_pid']), (False, os.getpid()))
        self.exit_process(a)
        self.assertTrue(b.try_acquire())
        self.assertTrue(b.poller_status()['active_here'])

    def test_every_poll_renews_the_heartbeat(self):
        a = self.channel()
        a.try_acquire()
        self.age_lease(channel.datetime.timedelta(seconds=30))
        before = self.lease()['heartbeat']
        self.assertTrue(a.try_acquire())
        self.assertGreater(self.lease()['heartbeat'], before)

    def test_a_crashed_holder_is_replaced_at_once(self):
        a, b = self.channel(), self.channel()
        a.try_acquire()
        with mock.patch.object(channel.os, 'kill', side_effect=ProcessLookupError):
            self.assertTrue(b.try_acquire())

    def test_a_hung_holder_is_replaced_and_stands_down_when_it_wakes(self):
        a, b = self.channel(), self.channel()
        a.try_acquire()
        self.assertFalse(b.try_acquire())
        self.age_lease(channel.LEASE_TTL)   # a stopped renewing
        self.assertTrue(b.try_acquire())
        self.assertFalse(a.try_acquire())   # a wakes up, sees b's lease
        self.assertFalse(a.active)
        self.assertTrue(b.try_acquire())

    def test_release_leaves_another_holders_lease_alone(self):
        a, b = self.channel(), self.channel()
        a.try_acquire()
        self.age_lease(channel.LEASE_TTL)
        b.try_acquire()
        a.release()   # a still thinks it is active
        self.assertEqual(self.lease()['token'], b._token)

    def test_contended_lease_lock_skips_this_poll(self):
        a = self.channel()
        import fcntl
        fd = os.open(a.lease_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertFalse(a.try_acquire())

    def write_saved(self, cursors, saved_at):
        (Path(self.dir.name) / 'channel_cursors.json').write_text(
            json.dumps({'saved_at': saved_at.isoformat(), 'cursors': cursors}))

    def test_takeover_resumes_from_the_previous_pollers_cursor(self):
        a = self.channel()
        a.try_acquire()
        a.cursors[SPACE] = '2026-09-18T06:00:00Z'
        a._save_cursors()
        self.exit_process(a)
        b = self.channel()
        b.try_acquire()
        self.assertEqual(b.cursors[SPACE], '2026-09-18T06:00:00Z')

    def test_quiet_space_keeps_its_old_cursor_when_the_poller_saved_recently(self):
        # The last message is hours old, but the poller was alive a moment ago.
        now = channel.datetime.datetime.now(channel.datetime.timezone.utc)
        self.write_saved({SPACE: '2026-01-01T00:00:00Z'}, now)
        b = self.channel()
        b.try_acquire()
        self.assertEqual(b.cursors[SPACE], '2026-01-01T00:00:00Z')

    def test_cursors_of_a_poller_gone_too_long_are_not_resumed(self):
        old = channel.datetime.datetime.now(channel.datetime.timezone.utc) - channel.datetime.timedelta(hours=1)
        self.write_saved({SPACE: '2026-01-01T00:00:00Z'}, old)
        b = self.channel()
        b.try_acquire()
        self.assertNotIn(SPACE, b.cursors)

    def test_standby_watch_cursor_is_dropped_on_takeover(self):
        # A standby's watch() hours ago must not replay everything since then.
        b = self.channel()
        b.cursors[SPACE] = '2026-09-18T01:00:00Z'
        b.try_acquire()
        self.assertNotIn(SPACE, b.cursors)

    def test_unchanged_cursors_are_resaved_as_a_heartbeat(self):
        a = self.channel()
        a.try_acquire()
        a.cursors[SPACE] = '2026-09-18T06:00:00Z'
        a._save_cursors()
        path = Path(self.dir.name) / 'channel_cursors.json'
        first = json.loads(path.read_text())['saved_at']
        a._saved_at -= channel.CURSOR_HEARTBEAT
        a._save_cursors()
        self.assertGreater(json.loads(path.read_text())['saved_at'], first)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_saved_cursors_drop_unwatched_spaces(self):
        a = self.channel()
        a.try_acquire()
        a.cursors = {SPACE: '2026-09-18T00:00:00Z', 'spaces/GONE': '2026-09-18T00:00:00Z'}
        a._save_cursors()
        saved = json.loads((Path(self.dir.name) / 'channel_cursors.json').read_text())
        self.assertEqual(list(saved['cursors']), [SPACE])

    def test_stale_lease_is_reported_without_a_holder(self):
        a = self.channel()
        a.try_acquire()
        for error in (ProcessLookupError, PermissionError):   # PermissionError: PID reused by another user
            with mock.patch.object(channel.os, 'kill', side_effect=error):
                status = self.channel().poller_status()
            self.assertEqual((status['holder_pid'], status['stale']), (None, True))


if __name__ == '__main__':
    unittest.main()
