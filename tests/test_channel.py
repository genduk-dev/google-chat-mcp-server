import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import channel
from channel import Channel, ChannelStore, Flags, is_own, mentions_bot, sender_allowed, to_notification
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
    def test_allowlist_or_everyone(self):
        self.assertTrue(sender_allowed(message('m1'), [OWNER]))
        self.assertFalse(sender_allowed(message('m1', sender=OTHER), [OWNER]))
        self.assertTrue(sender_allowed(message('m1', sender=OTHER), None))

    def test_own_message_is_recognized_by_its_client_id(self):
        self.assertTrue(is_own(message('m1', client_id=f'{APP_MESSAGE_PREFIX}abc')))
        self.assertFalse(is_own(message('m1')))

    @mock.patch.object(channel, 'BOT_NAME', 'genduk')
    def test_mention_is_a_standalone_case_insensitive_token(self):
        self.assertTrue(mentions_bot('hey @Genduk check this'))
        self.assertTrue(mentions_bot('@genduk: deploy'))
        self.assertFalse(mentions_bot('lunch?'))
        self.assertFalse(mentions_bot('ask @gendukku'))
        self.assertFalse(mentions_bot('genduk without the at sign'))
        self.assertFalse(mentions_bot('@all lunch?'))

    def test_another_bot_never_addresses_ours(self):
        self.assertTrue(Flags(mentioned=True).addressed)
        self.assertTrue(Flags(replying_to_bot=True).addressed)
        self.assertFalse(Flags(mentioned=True, bot_sender=True).addressed)
        self.assertFalse(Flags(operator=True).addressed)


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
            mock.patch.object(channel, 'self_user_id', return_value=OWNER),
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
        out = ch.poll_once()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['content'],
                         'Earlier, not sent to you before:\n'
                         f'[Husni (operator), 07:00Z, {SPACE}/threads/T1]\njust chatting\n\n'
                         'New:\n'
                         f'[Husni (operator, mentions you), 07:00Z, {SPACE}/threads/T1]\n@Genduk summarize')
        self.assertEqual(out[0]['meta']['mentioned'], 'true')
        self.assertEqual(out[0]['meta']['presence'], 'active')

    def threaded(self, name, thread, text, time, **kw):
        msg = message(name, text=text, create_time=time, **kw)
        msg['thread'] = {'name': f'{SPACE}/threads/{thread}'}
        return msg

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
        plain['lastUpdateTime'] = '2026-09-18T06:00:04Z'   # while idle, before the mention
        ours = self.threaded('m3', 'C', '@genduk prompt *Allowed*', '2026-09-18T05:00:00Z',
                             client_id=f'{APP_MESSAGE_PREFIX}p')
        ours['lastUpdateTime'] = '2026-09-18T06:00:07Z'
        gone = dict(added, name=f'{SPACE}/messages/m4', deleteTime='2026-09-18T06:00:08Z')
        self.events([self.edit_event(m, m['lastUpdateTime']) for m in (plain, added, ours)] +
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

    def test_presence_is_not_resumed_by_a_takeover(self):
        ch = Channel(self.store, 5)
        ch.cursors[SPACE] = '2026-09-18T06:00:00Z'
        ch.states[SPACE] = channel.SpaceState()
        ch.states[SPACE].address(channel.datetime.datetime.now(channel.datetime.timezone.utc))
        ch._save_cursors()
        self.assertNotIn('threads', json.loads(ch.cursor_path.read_text()))
        ch._resume_cursors()
        self.assertEqual(ch.states, {})
        self.assertEqual(ch.cursors, {SPACE: '2026-09-18T06:00:00Z'})

    def test_failed_space_keeps_its_cursor(self):
        ch = Channel(self.store, 5)
        ch.cursors[SPACE] = '2026-09-18T06:00:00Z'
        self.chat.spaces().messages().list.return_value.execute.side_effect = RuntimeError('503')
        self.assertEqual(ch.poll_once(), [])
        self.assertEqual(ch.cursors[SPACE], '2026-09-18T06:00:00Z')

    def test_watch_defaults_to_everyone_and_unwatch_removes(self):
        ch = Channel(ChannelStore(Path(self.dir.name) / 'fresh.json'), 5)
        self.chat.spaces().get.return_value.execute.return_value = {'displayName': 'Claude'}
        result = ch.watch(SPACE)
        self.assertIsNone(result['allowed_senders'])
        self.assertFalse(result['mention_only'])
        self.assertEqual(ch.list_watched()['spaces'],
                         [{'space_name': SPACE, 'allowed_senders': None, 'mention_only': False}])
        self.assertEqual(ch.unwatch(SPACE), {'space_name': SPACE, 'removed': True})
        self.assertEqual(ch.list_watched()['spaces'], [])

    @mock.patch.object(channel, 'BOT_NAME', 'genduk')
    @mock.patch.object(channel, 'APP_MESSAGE_PREFIX', 'client-genduk-')
    def test_list_watched_reports_the_bot_identity(self):
        listed = Channel(self.store, 5).list_watched()
        self.assertEqual((listed['bot_name'], listed['mention'], listed['message_id_prefix']),
                         ('genduk', '@genduk', 'client-genduk-'))



T0 = channel.datetime.datetime(2026, 9, 18, 7, 0, tzinfo=channel.datetime.timezone.utc)
NAMES = {OWNER: 'Husni', OTHER: 'Budi', 'users/bot': 'Deploy Bot'}


def at(seconds):
    return T0 + channel.datetime.timedelta(seconds=seconds)


def ts(seconds):
    return at(seconds).isoformat().replace('+00:00', 'Z')


class SpaceCase(unittest.TestCase):
    """A mention_only space open to everyone, polled with a controlled clock."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = ChannelStore(Path(self.dir.name) / 'state.json')
        self.store.save({SPACE: {'allowed_senders': None, 'mention_only': True}})
        self.chat = mock.MagicMock()
        self.chat.spaces().spaceEvents().list.return_value.execute.return_value = {}
        self.listed = []
        self.threads = {}
        self.chat.spaces().messages().list.side_effect = self.listing
        patches = [
            mock.patch.object(channel, 'get_credentials', return_value=object()),
            mock.patch.object(channel, '_get_service', return_value=self.chat),
            mock.patch.object(channel, 'get_user_display_name', side_effect=lambda sender, creds: NAMES[sender['name']]),
            mock.patch.object(channel, 'self_user_id', return_value=OWNER),
            mock.patch.object(channel, 'space_display_name', return_value='AI Gone Wild'),
            mock.patch.object(channel, 'BOT_NAME', 'genduk'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ch = Channel(self.store, 5)
        self.ch.cursors[SPACE] = self.ch.edit_cursors[SPACE] = ts(-60)

    def listing(self, **kwargs):
        call = mock.MagicMock()
        if kwargs['filter'].startswith('thread.name'):
            thread = kwargs['filter'].split(' ')[2]
            call.execute.return_value = {'messages': self.threads.get(thread, [])}
        else:
            call.execute.return_value = {'messages': self.listed}
        return call

    def m(self, name, sender, text, sec, thread='T1', reply=False, **extra):
        msg = {'name': f'{SPACE}/messages/{name}', 'sender': {'name': sender, 'type': 'BOT' if sender == 'users/bot' else 'HUMAN'},
               'text': text, 'createTime': ts(sec), 'thread': {'name': f'{SPACE}/threads/{thread}'}, **extra}
        if reply:
            msg['threadReply'] = True
        return msg

    def own(self, name, text, sec, thread='T1'):
        return self.m(name, OWNER, text, sec, thread, clientAssignedMessageId=f'{APP_MESSAGE_PREFIX}{name}')

    def poll(self, messages, now):
        self.listed = messages
        with mock.patch.object(self.ch, '_clock', return_value=at(now)):
            return self.ch.poll_once()

    def reactions(self):
        return [c.kwargs['parent'].split('/')[-1] for c in self.chat.spaces().messages().reactions().create.call_args_list]


class PresenceTest(SpaceCase):
    def test_idle_space_delivers_only_what_addresses_the_bot_with_what_came_before(self):
        self.assertEqual(self.poll([self.m('m1', OTHER, 'lunch?', 0)], 1), [])
        out = self.poll([self.m('m2', OTHER, '@genduk any ideas?', 2)], 3)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['content'],
                         'Earlier, not sent to you before:\n'
                         f'[Budi, 07:00Z, {SPACE}/threads/T1]\nlunch?\n\n'
                         'New:\n'
                         f'[Budi (mentions you), 07:00Z, {SPACE}/threads/T1]\n@genduk any ideas?')
        meta = out[0]['meta']
        self.assertEqual((meta['mentioned'], meta['presence'], meta['message_name']),
                         ('true', 'active', f'{SPACE}/messages/m2'))
        self.assertNotIn('sender_is_operator', meta)

    def test_present_bot_reads_everything_in_one_delivery_once_the_chat_pauses(self):
        self.assertEqual(len(self.poll([self.m('m1', OTHER, '@genduk ideas?', 0)], 1)), 1)
        self.assertEqual(self.poll([self.m('m2', OTHER, 'aku setuju', 10, thread='T2'),
                                    self.m('m3', OWNER, 'sama', 11, thread='T3')], 12), [])
        self.assertEqual(self.poll([], 15), [])          # 3 s of quiet
        out = self.poll([], 16)                          # 4 s
        self.assertEqual(out[0]['content'],
                         'New:\n'
                         f'[Budi, 07:00Z, {SPACE}/threads/T2]\naku setuju\n'
                         f'[Husni (operator), 07:00Z, {SPACE}/threads/T3]\nsama')
        meta = out[0]['meta']
        self.assertEqual((meta['sender_is_operator'], meta['thread_name'], meta['presence']),
                         ('true', f'{SPACE}/threads/T3', 'active'))
        self.assertNotIn('mentioned', meta)
        self.assertEqual(self.reactions(), ['m1'])       # only what addressed the bot

    def test_a_busy_chat_is_delivered_after_batch_max(self):
        self.poll([self.m('m1', OTHER, '@genduk hi', 0)], 1)
        for i, now in enumerate(range(2, 20, 3)):
            self.assertEqual(self.poll([self.m(f'n{i}', OTHER, 'more', now)], now), [])
        self.assertEqual(len(self.poll([self.m('last', OTHER, 'more', 22)], 22)), 1)

    def test_a_mention_brings_the_waiting_messages_with_it(self):
        self.poll([self.m('m1', OTHER, '@genduk hi', 0)], 1)
        self.poll([self.m('m2', OTHER, 'btw', 5)], 5)
        out = self.poll([self.m('m3', OTHER, '@genduk and this?', 6)], 6)
        self.assertEqual(len(out), 1)
        self.assertIn('btw', out[0]['content'].split('New:')[1])
        self.assertEqual(out[0]['meta']['message_name'], f'{SPACE}/messages/m3')

    def test_presence_ends_ten_minutes_after_the_bot_was_last_addressed_or_spoke(self):
        self.poll([self.m('m1', OTHER, '@genduk hi', 0)], 1)
        self.poll([self.own('b1', 'halo', 500)], 501)
        self.poll([self.m('m2', OTHER, 'still here', 1000)], 1000)
        self.assertEqual(len(self.poll([], 1005)), 1)     # 1000 is within 10 min of the bot's reply
        self.assertEqual(self.poll([self.m('m3', OTHER, 'hello?', 1101)], 1101), [])
        self.assertEqual(self.poll([], 1200), [])
        self.assertFalse(self.ch.busy())

    def test_the_bot_speaking_while_idle_does_not_start_presence(self):
        self.poll([self.own('b1', 'good morning', 0)], 1)
        self.assertEqual(self.poll([self.m('m1', OTHER, 'morning', 5)], 5), [])
        self.assertEqual(self.poll([], 30), [])

    def test_presence_ends_an_hour_after_it_began(self):
        self.poll([self.m('m1', OTHER, '@genduk hi', 0)], 1)
        for i, sec in enumerate(range(500, 3600, 500)):
            self.poll([self.own(f'b{i}', 'reply', sec)], sec + 1)
        self.assertEqual(self.poll([self.m('m2', OTHER, 'one more', 3601)], 3601), [])
        self.assertEqual(self.poll([], 3700), [])

    def test_leave_conversation_goes_idle_on_the_next_poll(self):
        self.poll([self.m('m1', OTHER, '@genduk makasih', 0)], 1)
        with mock.patch.object(self.ch, '_clock', return_value=at(2)):
            self.assertEqual(self.ch.leave(SPACE), {'space_name': SPACE, 'presence': 'idle'})
        self.assertEqual(self.poll([self.m('m2', OTHER, 'dah', 3)], 3), [])
        self.assertEqual(self.poll([], 30), [])
        self.assertEqual(len(self.poll([self.m('m3', OTHER, '@genduk lagi', 40)], 40)), 1)

    def test_leave_needs_a_watched_space(self):
        with self.assertRaises(ValueError):
            self.ch.leave('spaces/NOPE')

    def test_mute_lets_only_the_operator_addressing_the_bot_through(self):
        with mock.patch.object(self.ch, '_clock', return_value=at(0)):
            self.assertEqual(self.ch.mute(SPACE, 30)['muted_until'], ts(1800))
        out = self.poll([self.m('m1', OTHER, '@genduk hi', 1), self.m('m2', OWNER, 'hi', 2),
                         self.m('m3', OWNER, '@genduk you can talk again', 3)], 4)
        self.assertEqual([n['meta']['message_name'] for n in out], [f'{SPACE}/messages/m3'])
        with mock.patch.object(self.ch, '_clock', return_value=at(5)):
            self.assertIsNone(self.ch.mute(SPACE, 0)['muted_until'])
        self.assertNotIn('muted_until', self.store.load()[SPACE])
        self.assertEqual(len(self.poll([self.m('m4', OTHER, '@genduk hi', 6)], 6)), 1)

    def test_a_quote_reply_to_the_bot_addresses_it(self):
        self.poll([self.own('b1', 'the build is green', 0)], 1)
        quote = self.m('m1', OTHER, 'nice, why?', 700, quotedMessageMetadata={'name': f'{SPACE}/messages/b1'})
        out = self.poll([quote], 701)
        self.assertEqual(out[0]['meta']['replying_to_bot'], 'true')

    def test_a_quote_of_an_old_message_is_read_once_to_see_whose_it_is(self):
        old = self.own('b0', 'yesterday', -86400)
        self.chat.spaces().messages().get.return_value.execute.return_value = old
        quote = self.m('m1', OTHER, 'about this', 0, quotedMessageMetadata={'name': f'{SPACE}/messages/b0'})
        out = self.poll([quote], 1)
        self.assertEqual(out[0]['meta']['replying_to_bot'], 'true')
        self.assertIn('[you, 07:00Z', out[0]['content'])   # the quoted message is its context
        self.chat.spaces().messages().get.assert_called_once_with(name=f'{SPACE}/messages/b0')

    def test_another_bot_neither_starts_nor_keeps_presence(self):
        self.assertEqual(self.poll([self.m('m1', 'users/bot', '@genduk deploy done', 0)], 1), [])
        self.poll([self.m('m2', OTHER, '@genduk hi', 2)], 3)
        self.poll([self.m('m3', 'users/bot', '@genduk ping', 300)], 300)
        self.poll([], 310)
        self.assertEqual(self.poll([self.m('m4', OTHER, 'ok', 700)], 700), [])   # 698 s after Budi

    def test_too_many_deliveries_in_an_hour_end_presence(self):
        self.poll([self.m('m1', OTHER, '@genduk hi', 0)], 1)
        self.ch.states[SPACE].deliveries = [at(1)] * channel.HOURLY_DELIVERIES
        self.assertEqual(self.poll([self.m('m2', OTHER, 'chatter', 5)], 5), [])
        self.assertFalse(self.ch.states[SPACE].is_active(at(6)))
        self.assertEqual(len(self.poll([self.m('m3', OTHER, '@genduk still?', 7)], 7)), 1)

    def test_context_leaves_out_what_the_session_has_seen(self):
        self.poll([self.m('m1', OTHER, 'first', 0)], 0)
        self.poll([self.m('m2', OTHER, '@genduk hi', 1)], 1)
        with mock.patch.object(self.ch, '_clock', return_value=at(2)):
            self.ch.leave(SPACE)
        self.poll([self.m('m3', OTHER, 'second', 10)], 10)
        out = self.poll([self.m('m4', OTHER, '@genduk again', 20)], 20)
        earlier = out[0]['content'].split('New:')[0]
        self.assertIn('second', earlier)
        self.assertNotIn('first', earlier)
        self.assertNotIn('@genduk hi', earlier)

    def test_context_keeps_the_newest_and_says_how_many_it_left_out(self):
        self.poll([self.m(f'c{i}', OTHER, f'line {i}', i) for i in range(25)], 30)
        content = self.poll([self.m('m1', OTHER, '@genduk summarize', 40)], 40)[0]['content']
        self.assertIn('(5 older left out; get_messages has them)', content)
        self.assertNotIn('line 4\n', content)
        self.assertIn('line 5\n', content)

    def test_a_reply_in_a_thread_the_poller_never_saw_begin_fetches_it_once(self):
        self.threads[f'{SPACE}/threads/OLD'] = [self.m('r0', OTHER, 'the question from yesterday', -86400, thread='OLD')]
        first = self.m('m1', OTHER, '@genduk see above', 0, thread='OLD', reply=True)
        out = self.poll([first], 1)
        self.assertIn('the question from yesterday', out[0]['content'])
        self.poll([self.m('m2', OTHER, '@genduk and?', 5, thread='OLD', reply=True)], 6)
        thread_calls = [c for c in self.chat.spaces().messages().list.call_args_list
                        if c.kwargs['filter'].startswith('thread.name')]
        self.assertEqual(len(thread_calls), 1)
        self.assertEqual(thread_calls[0].kwargs['filter'],
                         f'thread.name = {SPACE}/threads/OLD AND createTime < "{ts(0)}"')

    def test_a_thread_reply_gets_its_thread_not_the_main_flow(self):
        self.poll([self.m('a', OTHER, 'main flow chatter', 0, thread='A'),
                   self.m('b0', OTHER, 'thread start', 1, thread='B'),
                   self.m('b1', OTHER, 'in the thread', 2, thread='B', reply=True)], 3)
        content = self.poll([self.m('b2', OTHER, '@genduk thoughts?', 4, thread='B', reply=True)], 5)[0]['content']
        self.assertIn('thread start', content)
        self.assertIn('in the thread', content)
        self.assertNotIn('main flow chatter', content)

    def test_allowlist_keeps_others_out_of_delivery_and_context(self):
        self.store.save({SPACE: {'allowed_senders': [OWNER], 'mention_only': True}})
        self.poll([self.m('m1', OTHER, 'secret plan', 0)], 0)
        self.assertEqual(self.poll([self.m('m2', OTHER, '@genduk run it', 1)], 1), [])
        out = self.poll([self.m('m3', OWNER, '@genduk hi', 2)], 2)
        self.assertEqual(out[0]['content'], '@genduk hi')

    def test_an_edit_of_a_message_the_session_saw_arrives_while_idle(self):
        self.poll([self.m('m1', OTHER, '@genduk deploy staging', 0)], 1)
        with mock.patch.object(self.ch, '_clock', return_value=at(2)):
            self.ch.leave(SPACE)
        edited = self.m('m1', OTHER, '@genduk deploy prod', 0, lastUpdateTime=ts(100))
        unseen = self.m('m9', OTHER, 'typo fixed', -5, lastUpdateTime=ts(99))
        self.chat.spaces().spaceEvents().list.return_value.execute.return_value = {'spaceEvents': [
            PollTest.edit_event(m, m['lastUpdateTime']) for m in (unseen, edited)]}
        out = self.poll([], 102)
        self.assertEqual([(n['content'], n['meta']['edited']) for n in out],
                         [('Before the edit:\n@genduk deploy staging\n\nAfter:\n@genduk deploy prod', 'true')])

    def test_an_edit_that_only_changes_spacing_or_case_is_dropped(self):
        self.poll([self.m('m1', OTHER, '@genduk deploy  staging', 0)], 1)
        edited = self.m('m1', OTHER, '@Genduk Deploy staging ', 0, lastUpdateTime=ts(20))
        self.chat.spaces().spaceEvents().list.return_value.execute.return_value = {'spaceEvents': [
            PollTest.edit_event(edited, edited['lastUpdateTime'])]}
        self.assertEqual(self.poll([], 21), [])
        typo = self.m('m1', OTHER, '@genduk deploy stagign', 0, lastUpdateTime=ts(30))
        self.chat.spaces().spaceEvents().list.return_value.execute.return_value = {'spaceEvents': [
            PollTest.edit_event(typo, typo['lastUpdateTime'])]}
        out = self.poll([], 31)
        self.assertTrue(out[0]['content'].startswith('Before the edit:\n@Genduk Deploy staging'))


class OpenSpaceTest(SpaceCase):
    """A space watched without mention_only: everything is for the bot."""

    def setUp(self):
        super().setUp()
        self.store.save({SPACE: {'allowed_senders': None, 'mention_only': False}})

    def test_every_message_is_delivered_and_acknowledged_at_once(self):
        out = self.poll([self.m('m1', OWNER, 'cek deploy', 0), self.m('m2', OWNER, 'yang staging', 1)], 2)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['meta']['sender_is_operator'], 'true')
        self.assertNotIn('presence', out[0]['meta'])
        self.assertEqual(self.reactions(), ['m1', 'm2'])
        self.assertFalse(self.ch.busy())

    def test_an_image_is_saved_and_its_path_delivered(self):
        msg = self.m('m1', OWNER, 'what is this?', 0, attachment=[
            {'contentName': 'shot.png', 'attachmentDataRef': {'resourceName': 'RES1'}},
            {'contentName': 'plan.gdoc', 'driveDataRef': {'driveFileId': 'DRIVE1'}},
            {'contentName': 'huge.mov', 'attachmentDataRef': {'resourceName': 'RES2'}}])

        def save(creds, resource, save_dir, name, max_bytes):
            self.assertEqual((save_dir, max_bytes), (str(self.ch.attachments_dir), channel.ATTACHMENT_MAX_BYTES))
            if resource == 'RES2':
                raise channel.AttachmentTooLarge('big')
            return {'path': f'{save_dir}/gchat-1.png', 'contentType': 'image/png'}
        with mock.patch.object(channel, 'save_attachment', side_effect=save):
            content = self.poll([msg], 1)[0]['content']
        self.assertEqual(content, 'what is this?\n'
                                  f'[attachment shot.png (image/png): {self.ch.attachments_dir}/gchat-1.png]\n'
                                  '[attachment plan.gdoc: Drive file https://drive.google.com/open?id=DRIVE1]\n'
                                  '[attachment huge.mov: over 20 MB, not saved]')
        self.assertEqual(self.ch.attachments_dir.stat().st_mode & 0o777, 0o700)

    def test_a_card_without_text_says_so(self):
        out = self.poll([self.m('m1', OWNER, '', 0, cardsV2=[{'cardId': 'c'}])], 1)
        self.assertEqual(out[0]['content'], '[a card]')

    def test_old_attachments_are_deleted(self):
        self.ch.attachments_dir.mkdir(mode=0o700)
        old = self.ch.attachments_dir / 'gchat-old.png'
        old.write_bytes(b'x')
        os.utime(old, (0, 0))
        self.ch._prune_attachments()
        self.assertFalse(old.exists())


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
            self.assertEqual(channel.parse_verdict(message('m', text=text), OWNER), expected, text)
        for text in ['yes', 'approve abcde', 'yes abcdl', 'yes abcdef', 'sure yes abcde']:
            self.assertIsNone(channel.parse_verdict(message('m', text=text), OWNER), text)

    def test_verdict_needs_the_operator_and_not_our_own_message(self):
        self.assertIsNone(channel.parse_verdict(message('m', sender=OTHER, text='yes abcde'), OWNER))
        own = message('m', text='yes abcde', client_id=f'{APP_MESSAGE_PREFIX}x')
        self.assertIsNone(channel.parse_verdict(own, OWNER))

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
                          channel.datetime.datetime.now(channel.datetime.timezone.utc) - channel.RELAY_WINDOW
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
                mock.patch.object(channel, 'get_user_display_name', return_value='Husni'), \
                mock.patch.object(channel, 'self_user_id', return_value=OWNER):
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


class GatedSpaceTest(SpaceCase):
    """A space behind the Jev gate, with Jev faked. SpaceCase watches it mention_only;
    test_mention_only_does_not_matter_behind_the_gate checks the other setting."""

    def setUp(self):
        super().setUp()
        from gate import Gate, Policy
        from tests.test_gate import answers
        self.answers = answers
        self.jev = mock.MagicMock()
        self.jev.ask.return_value = answers()
        self.ch.gate = Gate(Policy(name='Genduk', description='An engineering assistant.'), self.jev)

    def scored(self, **s):
        self.jev.ask.return_value = self.answers(**s)

    def log(self):
        return [json.loads(line) for line in self.ch.gate_log_path.read_text().splitlines()]

    def test_a_mention_is_delivered_at_once_without_asking_jev(self):
        out = self.poll([self.m('m1', OTHER, '@genduk cek staging', 0)], 1)
        self.assertEqual(out[0]['meta']['mentioned'], 'true')
        self.assertNotIn('presence', out[0]['meta'])
        self.jev.ask.assert_not_called()

    def test_small_talk_is_held_and_reaches_the_session_later_as_context(self):
        self.assertEqual(self.poll([self.m('m1', OTHER, 'makan di mana?', 0)], 1), [])
        self.assertEqual(self.poll([], 5), [])
        self.jev.ask.assert_called_once()
        self.assertEqual(self.ch.states[SPACE].silent, {f'{SPACE}/messages/m1': 'stayed_silent'})
        self.assertFalse(self.ch.busy())
        out = self.poll([self.m('m2', OTHER, '@genduk kamu?', 10)], 10)
        self.assertIn('makan di mana?', out[0]['content'].split('New:')[0])

    def test_a_batch_the_chat_expects_an_answer_to_is_delivered_and_acknowledged(self):
        self.scored(addressed=0.95, wants_reply=0.92)
        self.poll([self.m('m1', OTHER, 'Nduk, cek log', 0), self.m('m2', OTHER, 'yang staging', 1)], 2)
        out = self.poll([], 6)
        self.assertEqual(out[0]['meta']['gate'], 'reply')
        self.assertIn('yang staging', out[0]['content'])
        self.assertEqual(self.reactions(), ['m2'])
        entry = self.log()[-1]
        self.assertEqual((entry['event'], entry['action'], entry['names']),
                         ('decision', 'reply', [f'{SPACE}/messages/m1', f'{SPACE}/messages/m2']))

    def test_thanks_gets_a_reaction_and_no_turn(self):
        self.scored(addressed=0.96, wants_reply=0.2, reaction='pray')
        self.poll([self.m('m1', OTHER, 'mantap makasih', 0)], 1)
        self.assertEqual(self.poll([], 5), [])
        call = self.chat.spaces().messages().reactions().create.call_args
        self.assertEqual((call.kwargs['parent'], call.kwargs['body']),
                         (f'{SPACE}/messages/m1', {'emoji': {'unicode': '🙏'}}))

    def test_chiming_in_waits_for_a_longer_pause_and_starts_over_when_someone_speaks(self):
        self.scored(could_help=0.95, wants_reply=0.5)
        self.poll([self.m('m1', OTHER, 'CI merah, ada yang tau?', 0)], 1)
        self.assertEqual(self.poll([], 5), [])            # judged: chime in, after 30 s of quiet
        self.assertEqual(self.poll([], 20), [])
        self.poll([self.m('m2', OWNER, 'coba rerun', 25)], 25)
        self.assertEqual(self.poll([], 30), [])           # judged again: the batch grew
        self.assertEqual(self.jev.ask.call_count, 2)
        self.assertEqual(self.poll([], 50), [])
        out = self.poll([], 56)
        self.assertEqual((out[0]['meta']['gate'], out[0]['meta']['gate_reason']), ('interject', 'help'))
        self.assertEqual(self.reactions(), [])            # nobody asked, so nothing to acknowledge

    def test_banter_is_joined_with_its_reason(self):
        self.scored(natural_to_join=0.9, reaction='laugh')
        self.poll([self.m('m1', OTHER, 'wkwk deploy jumat sore', 0)], 1)
        out = self.poll([], 31)
        self.assertEqual((out[0]['meta']['gate'], out[0]['meta']['gate_reason']), ('interject', 'join'))

    def test_a_joke_gets_a_laugh_unless_the_bot_just_reacted_or_the_space_turned_reactions_off(self):
        self.scored(natural_to_join=0.5, reaction='laugh', reaction_p=0.9)
        self.poll([self.m('m1', OTHER, 'wkwk', 0)], 1)
        self.poll([], 5)
        self.poll([self.m('m2', OTHER, 'wkwkwk', 10)], 10)
        self.poll([], 15)
        self.assertEqual(self.reactions(), ['m1'])       # one reaction in the last three is enough
        self.store.save({SPACE: {'allowed_senders': None, 'mention_only': True, 'reactions': False}})
        for i, sec in enumerate(range(20, 60, 10)):
            self.poll([self.m(f'n{i}', OTHER, 'wkwk', sec)], sec)
            self.poll([], sec + 5)
        self.assertEqual(self.reactions(), ['m1'])

    def test_chiming_in_stops_once_the_bot_has_its_share_of_the_talk(self):
        self.scored(natural_to_join=0.95)
        chatter = [self.m(f'c{i}', OTHER, 'seru', i) for i in range(6)]
        mine = [self.own(f'b{i}', 'wkwk', 10 + i) for i in range(4)]
        self.poll(chatter + mine, 20)
        self.poll([self.m('m1', OTHER, 'lanjut', 30)], 30)
        self.assertEqual(self.poll([], 70), [])          # 4 of the last 10 are the bot's
        self.poll([self.m(f'd{i}', OTHER, 'lagi', 80 + i) for i in range(6)], 90)
        self.assertEqual(self.poll([], 130)[0]['meta']['gate_reason'], 'join')   # 3 of 10 now

    def test_the_space_norms_and_mentions_of_others_reach_jev(self):
        self.store.save({SPACE: {'allowed_senders': None, 'mention_only': True, 'norms': 'A casual AI chat.'}})
        tagged = self.m('m1', OTHER, '@Husni COD brp?', 0, annotations=[
            {'type': 'USER_MENTION', 'userMention': {'user': {'name': OWNER}}}])
        everyone = self.m('m2', OTHER, '@all rilis jam 3', 1, annotations=[
            {'type': 'USER_MENTION', 'userMention': {'user': {}}}])
        self.poll([tagged, everyone], 2)
        self.poll([], 6)
        s = self.jev.ask.call_args.args[0]
        self.assertEqual(s['conversation'], {'kind': 'group', 'description': 'A casual AI chat.'})
        self.assertEqual([m.get('mentions_others') for m in s['messages']], [True, None])

    def test_an_emoji_or_an_attachment_alone_is_held_without_asking_jev(self):
        self.poll([self.m('m1', OTHER, ':sungkem-ndlosor-kiri:', 0), self.m('m2', OTHER, '', 1)], 2)
        self.assertEqual(self.poll([], 6), [])
        self.jev.ask.assert_not_called()

    def test_watching_again_keeps_the_gate_settings(self):
        self.store.save({SPACE: {'allowed_senders': None, 'mention_only': True, 'norms': 'work', 'max_share': 0.1,
                                 'reactions': False, 'muted_until': ts(900)}})
        self.chat.spaces().get.return_value.execute.return_value = {'displayName': 'DINO'}
        with mock.patch.object(self.ch, '_clock', return_value=at(0)):
            self.ch.watch(SPACE, None, False)
        self.assertEqual(self.store.load()[SPACE], {'allowed_senders': None, 'mention_only': False, 'norms': 'work',
                                                    'max_share': 0.1, 'reactions': False})

    def test_jev_failing_leaves_the_batch_waiting_and_logged(self):
        from gate import JevError
        self.jev.ask.side_effect = JevError('Jev answered 503')
        self.poll([self.m('m1', OTHER, 'Nduk?', 0)], 1)
        self.assertEqual(self.poll([], 5), [])
        self.assertEqual(self.poll([], 20), [])           # not asked again before GATE_RETRY
        self.assertEqual(self.jev.ask.call_count, 1)
        self.assertEqual(self.log()[-1]['event'], 'error')
        self.jev.ask.side_effect = None
        self.scored(addressed=0.9, wants_reply=0.9)
        self.assertEqual(self.poll([], 36)[0]['meta']['gate'], 'reply')

    def test_jev_reads_the_bot_s_own_reply_and_what_it_held(self):
        self.poll([self.m('m1', OTHER, 'lunch?', 0)], 1)
        self.poll([], 5)
        self.poll([self.m('m2', OTHER, '@genduk deploy sukses?', 10)], 10)
        self.poll([self.own('b1', 'Sukses.', 20)], 20)
        self.poll([self.m('m3', OTHER, 'terus staging?', 30)], 30)
        self.poll([], 35)
        messages = self.jev.ask.call_args.args[0]['messages']
        self.assertEqual([(m['sender'], m.get('agent_action'), m.get('from_agent'), m.get('new')) for m in messages],
                         [('Budi', 'stayed_silent', None, None), ('Budi', None, None, None),
                          ('Genduk', None, True, None), ('Budi', None, None, True)])
        self.assertTrue(messages[1]['mentions_agent'])
        self.assertEqual(messages[-1]['ago_seconds'], 5)

    def test_leaving_holds_back_all_but_mentions_for_ten_minutes(self):
        with mock.patch.object(self.ch, '_clock', return_value=at(0)):
            self.ch.leave(SPACE)
        self.poll([self.m('m1', OTHER, 'Nduk?', 1)], 1)
        self.assertEqual(self.poll([], 10), [])
        self.jev.ask.assert_not_called()
        self.assertEqual(len(self.poll([self.m('m2', OTHER, '@genduk hi', 20)], 20)), 1)
        self.poll([self.m('m3', OTHER, 'Nduk, lagi?', 601)], 601)
        self.poll([], 606)
        self.jev.ask.assert_called_once()

    def test_mention_only_does_not_matter_behind_the_gate(self):
        self.store.save({SPACE: {'allowed_senders': None, 'mention_only': False}})
        self.assertEqual(self.poll([self.m('m1', OWNER, 'catatan buat diri sendiri', 0)], 1), [])
        self.assertEqual(self.poll([], 5), [])
        self.jev.ask.assert_called_once()
        self.assertEqual(self.reactions(), [])

    def test_list_watched_names_the_gate(self):
        self.ch.active = True
        self.assertEqual(self.ch.list_watched()['gate'], 'jev')
        self.assertNotIn('presence', self.ch.list_watched()['spaces'][0])


if __name__ == '__main__':
    unittest.main()
