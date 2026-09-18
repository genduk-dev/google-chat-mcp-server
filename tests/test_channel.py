import asyncio
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

    def test_space_display_name_is_added_when_known(self):
        params = to_notification(message('m1'), SPACE, 'Husni', space_title='Ruang Ngopi')
        self.assertEqual(params['meta']['space_display_name'], 'Ruang Ngopi')
        self.assertEqual(params['meta']['chat_id'], SPACE)

    def test_space_display_name_is_left_out_when_unknown(self):
        self.assertNotIn('space_display_name', to_notification(message('m1'), SPACE, 'Husni')['meta'])

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
        self.events([])

    def tearDown(self):
        self.dir.cleanup()

    def events(self, events):
        self.chat.spaces().spaceEvents().list.return_value.execute.return_value = {'spaceEvents': events}

    def list_returns(self, messages):
        self.chat.spaces().messages().list.return_value.execute.return_value = {'messages': messages}

    def test_first_poll_only_sets_cursor_so_history_is_not_replayed(self):
        ch = Channel(self.store, 5)
        self.list_returns([message('old')])
        self.assertEqual(ch.poll_once(), [])
        self.assertIn(SPACE, ch.cursors)

    def test_delivery_names_the_space(self):
        ch = Channel(self.store, 5)
        ch.cursors[SPACE] = '2026-09-18T06:00:00Z'
        self.list_returns([message('m1', create_time='2026-09-18T06:00:01Z')])
        with mock.patch.object(channel, 'space_display_name', return_value='Husni'):
            out = ch.poll_once()
        self.assertEqual(out[0]['meta']['space_display_name'], 'Husni')

    def test_delivery_survives_a_failed_space_lookup(self):
        ch = Channel(self.store, 5)
        ch.cursors[SPACE] = '2026-09-18T06:00:00Z'
        self.list_returns([message('m1', create_time='2026-09-18T06:00:01Z')])
        with mock.patch.object(channel, 'space_display_name', side_effect=RuntimeError('boom')):
            out = ch.poll_once()
        self.assertEqual([n['content'] for n in out], ['hi'])
        self.assertNotIn('space_display_name', out[0]['meta'])

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

    def threaded(self, name, thread, text, time, **kw):
        msg = message(name, text=text, create_time=time, **kw)
        msg['thread'] = {'name': f'{SPACE}/threads/{thread}'}
        return msg

    @mock.patch.object(channel, 'BOT_NAME', 'genduk')
    def test_mention_only_thread_followups_need_no_mention(self):
        self.store.save({SPACE: {'allowed_senders': [OWNER], 'mention_only': True}})
        ch = Channel(self.store, 5)
        ch.cursors[SPACE] = '2026-09-18T06:00:00Z'
        self.list_returns([
            self.threaded('m1', 'A', '@genduk check the deploy', '2026-09-18T06:00:01Z'),
            self.threaded('m2', 'A', 'the staging one', '2026-09-18T06:10:00Z'),          # follow-up
            self.threaded('m3', 'B', 'unrelated chatter', '2026-09-18T06:10:01Z'),        # other thread
            self.threaded('m4', 'A', 'still there?', '2026-09-18T06:41:00Z'),             # 31 min later
            self.threaded('m5', 'C', 'reply from Claude', '2026-09-18T06:42:00Z',
                          client_id=f'{APP_MESSAGE_PREFIX}x'),
            self.threaded('m6', 'C', 'thanks, one more', '2026-09-18T06:43:00Z'),         # after our reply
        ])
        self.assertEqual([n['content'] for n in ch.poll_once()],
                         ['@genduk check the deploy', 'the staging one', 'thanks, one more'])

    @staticmethod
    def edit_event(msg, time):
        return {'eventTime': time, 'eventType': 'google.workspace.chat.message.v1.updated',
                'messageUpdatedEventData': {'message': msg}}

    @mock.patch.object(channel, 'BOT_NAME', 'genduk')
    def test_edits_pass_the_same_gate_and_arrive_marked_edited(self):
        self.store.save({SPACE: {'allowed_senders': [OWNER], 'mention_only': True}})
        ch = Channel(self.store, 5)
        ch.cursors[SPACE] = ch.edit_cursors[SPACE] = '2026-09-18T06:00:00Z'
        self.list_returns([])
        added = self.threaded('m1', 'A', '@genduk deploy staging', '2026-09-18T05:00:00Z', )
        added['lastUpdateTime'] = '2026-09-18T06:00:05Z'
        plain = self.threaded('m2', 'B', 'still no mention', '2026-09-18T05:00:00Z')
        plain['lastUpdateTime'] = '2026-09-18T06:00:06Z'
        ours = self.threaded('m3', 'C', '@genduk prompt *Allowed*', '2026-09-18T05:00:00Z',
                             client_id=f'{APP_MESSAGE_PREFIX}p')
        ours['lastUpdateTime'] = '2026-09-18T06:00:07Z'
        gone = dict(added, name=f'{SPACE}/messages/m4', deleteTime='2026-09-18T06:00:08Z')
        self.events([self.edit_event(m, m['lastUpdateTime']) for m in (added, plain, ours)] +
                    [self.edit_event(gone, '2026-09-18T06:00:08Z')])
        out = ch.poll_once()
        self.assertEqual([(n['content'], n['meta']['edited'], n['meta']['edited_at']) for n in out],
                         [('@genduk deploy staging', 'true', '2026-09-18T06:00:05Z')])
        self.assertEqual(ch.edit_cursors[SPACE], '2026-09-18T06:00:08Z')
        kwargs = self.chat.spaces().spaceEvents().list.call_args.kwargs
        self.assertIn('start_time="2026-09-18T06:00:00Z"', kwargs['filter'])

    def test_a_message_created_and_edited_between_polls_is_delivered_once(self):
        ch = Channel(self.store, 5)
        ch.cursors[SPACE] = ch.edit_cursors[SPACE] = '2026-09-18T06:00:00Z'
        fresh = message('m1', text='edited already', create_time='2026-09-18T06:00:01Z')
        fresh['lastUpdateTime'] = '2026-09-18T06:00:02Z'
        self.list_returns([fresh])
        self.events([self.edit_event(fresh, '2026-09-18T06:00:02Z')])
        self.assertEqual([n['content'] for n in ch.poll_once()], ['edited already'])

    def test_edit_of_a_message_listed_in_the_same_second_is_not_delivered_twice(self):
        ch = Channel(self.store, 5)
        ch.cursors[SPACE] = ch.edit_cursors[SPACE] = '2026-09-18T06:00:03Z'   # _now() without a fraction
        fresh = message('m1', text='edited already', create_time='2026-09-18T06:00:03.500000Z')
        fresh['lastUpdateTime'] = '2026-09-18T06:00:04Z'
        self.list_returns([fresh])
        self.events([self.edit_event(fresh, '2026-09-18T06:00:04Z')])
        self.assertEqual([n['content'] for n in ch.poll_once()], ['edited already'])

    def test_first_poll_starts_edit_tracking_from_now(self):
        ch = Channel(self.store, 5)
        self.list_returns([])
        ch.poll_once()
        self.assertIn(SPACE, ch.edit_cursors)
        self.chat.spaces().spaceEvents().list.assert_not_called()

    def test_active_threads_survive_a_takeover(self):
        ch = Channel(self.store, 5)
        ch.cursors[SPACE] = '2026-09-18T06:00:00Z'
        now = channel.datetime.datetime.now(channel.datetime.timezone.utc)
        recent = now.isoformat().replace('+00:00', 'Z')
        ch.active_threads = {f'{SPACE}/threads/A': recent, f'{SPACE}/threads/OLD': '2026-01-01T00:00:00Z'}
        ch._save_cursors()
        saved = json.loads(ch.cursor_path.read_text())
        self.assertEqual(list(saved['threads']), [f'{SPACE}/threads/A'])   # expired one pruned
        other = Channel(self.store, 5)
        other._resume_cursors()
        self.assertEqual(other.active_threads, {f'{SPACE}/threads/A': recent})

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


class PermissionRelayTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = Path(self.dir.name) / 'channel_state.json'
        ChannelStore(self.state).save({SPACE: {'allowed_senders': [OWNER], 'mention_only': True}})

    def channel(self):
        return Channel(ChannelStore(self.state), 5)

    def test_verdict_format(self):
        cases = {'yes abcde': ('abcde', 'allow'), 'n ABCDE': ('abcde', 'deny'), 'Y abcde ': ('abcde', 'allow'),
                 '@genduk no qwert': ('qwert', 'deny')}
        for text, expected in cases.items():
            self.assertEqual(channel.parse_verdict(message('m', text=text), [OWNER]), expected, text)
        for text in ['yes', 'approve abcde', 'yes abcdl', 'yes abcdef', 'sure yes abcde']:
            self.assertIsNone(channel.parse_verdict(message('m', text=text), [OWNER]), text)

    def test_verdict_needs_an_allowed_sender_and_not_our_own_message(self):
        self.assertIsNone(channel.parse_verdict(message('m', sender=OTHER, text='yes abcde'), [OWNER]))
        own = message('m', text='yes abcde', client_id=f'{APP_MESSAGE_PREFIX}x')
        self.assertIsNone(channel.parse_verdict(own, [OWNER]))

    def test_prompt_shows_untrusted_fields_as_code(self):
        text = channel.permission_prompt({'request_id': 'abcde', 'tool_name': 'Bash',
                                          'description': 'ping <users/all> `x`', 'input_preview': '{"command": "ls"}'})
        self.assertIn("`ping <users/all> 'x'`", text)
        self.assertIn('```\n{"command": "ls"}\n```', text)
        self.assertTrue(text.endswith('Reply `yes abcde` to allow or `no abcde` to deny.'))

    def test_long_mcp_description_and_preview_fit_one_chat_message(self):
        doc = 'Send a message to a Google Chat space, optionally with file attachments. ' + 'Formatting ' * 200
        preview = '{"command": "' + 'x' * 5000 + ' && rm -rf build"}'
        text = channel.permission_prompt({'request_id': 'abcde', 'tool_name': 'mcp__g__send_message',
                                          'description': doc, 'input_preview': preview})
        self.assertIn('`Send a message to a Google Chat space, optionally with file attachments.`', text)
        self.assertIn('rm -rf build', text)   # the end of the command survives
        self.assertIn('chars cut', text)
        self.assertLess(len(text), 4096)

    def test_request_without_a_chat_thread_is_not_relayed(self):
        ch = self.channel()
        with mock.patch.object(channel, 'send_space_message') as send:
            asyncio.run(ch.on_permission_request({'request_id': 'abcde', 'tool_name': 'Bash'}))
        send.assert_not_called()

    def ask(self, owner, rid='abcde', recorded=None):
        owner.last_thread = (SPACE, f'{SPACE}/threads/T1', channel.datetime.datetime.now(channel.datetime.timezone.utc))

        async def post(*args, **kwargs):
            if recorded is not None:  # what another poller would see while the post is in flight
                recorded.append(json.loads(owner.permissions_path.read_text()))
            return {'name': f'{SPACE}/messages/P1'}
        sent = mock.AsyncMock(side_effect=post)
        with mock.patch.object(channel, 'send_space_message', sent):
            asyncio.run(owner.on_permission_request({'request_id': rid, 'tool_name': 'Bash',
                                                     'description': 'd', 'input_preview': 'p'}))
        return sent

    def test_answer_read_by_another_poller_reaches_the_session_that_asked(self):
        asker, poller = self.channel(), self.channel()
        sent = self.ask(asker)
        self.assertEqual(sent.call_args.kwargs['thread_name'], f'{SPACE}/threads/T1')
        self.assertIsNone(poller._record_verdict('spaces/OTHER', 'abcde', 'allow', 'Husni'))  # wrong space
        self.assertEqual(poller._record_verdict(SPACE, 'abcde', 'allow', 'Husni')['message'], f'{SPACE}/messages/P1')
        self.assertIsNone(poller._record_verdict(SPACE, 'abcde', 'deny', 'Husni'))  # first answer wins
        self.assertEqual(poller._take_verdicts(), [])  # not the poller's request
        self.assertEqual(asker._take_verdicts(), [('abcde', 'allow')])
        self.assertEqual(asker._take_verdicts(), [])  # delivered once

    def test_request_is_recorded_before_the_prompt_is_posted(self):
        asker, seen = self.channel(), []
        self.ask(asker, recorded=seen)
        self.assertIn('abcde', seen[0])
        self.assertEqual(json.loads(asker.permissions_path.read_text())['abcde']['message'], f'{SPACE}/messages/P1')

    def test_no_relay_to_a_thread_the_conversation_left_long_ago(self):
        ch = self.channel()
        ch.last_thread = (SPACE, f'{SPACE}/threads/T1',
                          channel.datetime.datetime.now(channel.datetime.timezone.utc) - channel.FOLLOWUP_WINDOW
                          - channel.datetime.timedelta(seconds=1))
        with mock.patch.object(channel, 'send_space_message') as send:
            asyncio.run(ch.on_permission_request({'request_id': 'abcde', 'tool_name': 'Bash'}))
        send.assert_not_called()

    def test_multiline_description_stays_inside_code(self):
        text = channel.permission_prompt({'request_id': 'abcde', 'tool_name': 'Web\nFetch',
                                          'description': 'Fetch a page.\nSee [docs](https://evil.example) <users/all>',
                                          'input_preview': 'p'})
        first_line = text.split('\n')[0]
        self.assertEqual(first_line, "🔐 Claude wants to use `Web Fetch`: "
                                     "`Fetch a page. See [docs](https://evil.example) <users/all>`")
        from google_chat import to_chat_markup
        self.assertEqual(to_chat_markup(first_line), first_line)   # nothing converted outside code

    def test_unanswered_requests_expire(self):
        asker = self.channel()
        self.ask(asker)
        with mock.patch.object(channel, 'PERMISSION_TTL', channel.datetime.timedelta(0)):
            self.assertIsNone(asker._record_verdict(SPACE, 'abcde', 'allow', 'Husni'))

    def test_poll_turns_an_answer_into_a_verdict_not_chat(self):
        ch = self.channel()
        ch.cursors[SPACE] = '2026-09-18T06:00:00Z'
        chat = mock.MagicMock()
        chat.spaces().messages().list.return_value.execute.return_value = {'messages': [
            message('m1', text='yes abcde', create_time='2026-09-18T06:00:01Z')]}
        chat.spaces().spaceEvents().list.return_value.execute.return_value = {}
        with mock.patch.object(channel, 'get_credentials', return_value=object()), \
                mock.patch.object(channel, '_get_service', return_value=chat), \
                mock.patch.object(channel, 'get_user_display_name', return_value='Husni'):
            self.assertEqual(ch.poll_once(), [])
        self.assertEqual(ch._answered, [(SPACE, 'abcde', 'allow', 'Husni')])


class ReviewTwoFixesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = ChannelStore(Path(self.dir.name) / 'state.json')

    def test_poll_with_nothing_watched_leaves_no_stale_answers(self):
        ch = Channel(self.store, 5)
        ch._answered = [('x', 'y', 'allow', 'z')]
        self.assertEqual(ch.poll_once(), [])
        self.assertEqual(ch._answered, [])

    def test_results_are_dropped_and_cursors_rolled_back_when_the_lease_moved(self):
        import anyio
        ch = Channel(self.store, 0.01)
        ch.cursors = {SPACE: 'before'}
        sent = []

        class Stream:
            async def send(self, message):
                sent.append(message)

        def poll():
            ch.cursors[SPACE] = 'after'
            return [{'content': 'x', 'meta': {}}]

        async def scenario():
            event = anyio.Event()
            event.set()
            with mock.patch.object(ch, 'try_acquire', side_effect=[True, False] + [False] * 50), \
                    mock.patch.object(ch, 'poll_once', side_effect=poll), \
                    mock.patch.object(ch, '_save_cursors') as save:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(ch.run, Stream(), event)
                    await anyio.sleep(0.05)
                    tg.cancel_scope.cancel()
            save.assert_not_called()

        anyio.run(scenario)
        self.assertEqual((sent, ch.cursors[SPACE]), ([], 'before'))


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
