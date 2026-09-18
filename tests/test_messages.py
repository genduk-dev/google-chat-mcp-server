import asyncio
import datetime
import unittest
from unittest import mock

import google_chat

SPACE = 'spaces/S'


def msg(mid, thread='T1', text='hi', time='2026-09-18T09:00:00.123456Z', **extra):
    return {'name': f'{SPACE}/messages/{mid}', 'sender': {'name': 'users/1', 'type': 'HUMAN'},
            'text': text, 'createTime': time, 'thread': {'name': f'{SPACE}/threads/{thread}'}, **extra}


class ListSpaceMessagesTest(unittest.TestCase):
    def setUp(self):
        self.chat = mock.MagicMock()
        self.pages = []
        self.chat.spaces().messages().list.side_effect = lambda **kw: mock.Mock(
            execute=mock.Mock(return_value=self.pages.pop(0)))
        for target, kw in [('get_credentials', {'return_value': object()}),
                           ('_get_service', {'return_value': self.chat}),
                           ('get_user_display_name', {'return_value': 'Husni'})]:
            p = mock.patch.object(google_chat, target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def run_list(self, **kwargs):
        return asyncio.run(google_chat.list_space_messages(SPACE, **kwargs))

    def list_kwargs(self):
        return [c.kwargs for c in self.chat.spaces().messages().list.call_args_list]

    def test_groups_by_thread_and_drops_empty_or_implied_fields(self):
        self.pages = [{'messages': [msg('T1', 'T1', 'root'), msg('T2', 'T2', 'other'),
                                    msg('T1.a', 'T1', 'reply', threadReply=True,
                                        lastUpdateTime='2026-09-18T10:00:00.5Z')]}]
        result = self.run_list(start_date=datetime.datetime(2026, 9, 18, tzinfo=datetime.timezone.utc))
        self.assertEqual([t['thread'] for t in result['threads']], [f'{SPACE}/threads/T1', f'{SPACE}/threads/T2'])
        self.assertEqual(result['threads'][0]['messages'], [
            {'id': 'T1', 'root': True, 'sender': 'Husni', 'time': '2026-09-18T09:00:00Z', 'text': 'root'},
            {'id': 'T1.a', 'sender': 'Husni', 'time': '2026-09-18T09:00:00Z', 'edited': '2026-09-18T10:00:00Z',
             'text': 'reply'},
        ])
        self.assertNotIn('truncated', result)

    def test_thread_filter_is_sent_to_the_api(self):
        self.pages = [{'messages': [msg('T1')]}]
        self.run_list(thread_name=f'{SPACE}/threads/T1')
        self.assertEqual(self.list_kwargs()[0]['filter'], f'thread.name = {SPACE}/threads/T1')

    def test_thread_and_date_filters_combine(self):
        self.pages = [{'messages': []}]
        self.run_list(start_date=datetime.datetime(2026, 9, 18, tzinfo=datetime.timezone.utc),
                      thread_name=f'{SPACE}/threads/T1')
        self.assertIn(f' AND thread.name = {SPACE}/threads/T1', self.list_kwargs()[0]['filter'])

    def test_thread_of_another_space_is_rejected_before_any_request(self):
        with self.assertRaises(ValueError):
            self.run_list(thread_name='spaces/OTHER/threads/T1')
        self.assertEqual(self.list_kwargs(), [])

    def test_limit_reads_newest_first_and_returns_them_oldest_first(self):
        self.pages = [{'messages': [msg('m3', text='3'), msg('m2', text='2')], 'nextPageToken': 'more'}]
        result = self.run_list(limit=2)
        kwargs = self.list_kwargs()
        self.assertEqual(len(kwargs), 1)
        self.assertEqual((kwargs[0]['orderBy'], kwargs[0]['pageSize']), ('createTime DESC', 2))
        self.assertEqual([m['text'] for m in result['threads'][0]['messages']], ['2', '3'])
        self.assertNotIn('truncated', result)

    def test_limit_out_of_range_is_rejected(self):
        for bad in (0, 1001):
            with self.assertRaises(ValueError):
                self.run_list(limit=bad)

    def test_hitting_the_cap_without_a_limit_is_marked_truncated(self):
        self.pages = [{'messages': [msg(f'm{i}') for i in range(1000)], 'nextPageToken': 'more'}]
        self.assertTrue(self.run_list(start_date=datetime.datetime(2026, 9, 18, tzinfo=datetime.timezone.utc))['truncated'])

    def test_after_filter_and_more_flag(self):
        self.pages = [{'messages': [msg('m2'), msg('m1')], 'nextPageToken': 'older'}]
        result = self.run_list(limit=2, after='2026-09-18T09:00:00Z')
        self.assertEqual(self.list_kwargs()[0]['filter'], 'createTime > "2026-09-18T09:00:00Z"')
        self.assertTrue(result['more'])

    def test_optional_fields_are_compacted(self):
        self.pages = [{'messages': [msg(
            'T1.b', quotedMessageMetadata={'name': f'{SPACE}/messages/T1.a'},
            threadReply=True,
            emojiReactionSummaries=[
                {'emoji': {'unicode': '👍'}, 'reactionCount': 2},
                {'emoji': {'customEmoji': {'uid': 'u1', 'emojiName': ':shrek-scream:'}}, 'reactionCount': 3},
                {'emoji': {'customEmoji': {'uid': 'u2', 'emojiName': ':party-parrot:'}}, 'reactionCount': 1},
            ],
            clientAssignedMessageId=f'{google_chat.APP_MESSAGE_PREFIX}x')]}]
        m = self.run_list(limit=1)['threads'][0]['messages'][0]
        self.assertEqual((m['quoted'], m['reactions'], m['sender_type'], m['sent_by_app']),
                         ('T1.a', {'👍': 2, ':shrek-scream:': 3, ':party-parrot:': 1}, 'BOT', True))
        self.assertNotIn('root', m)



class MessageTextTest(unittest.TestCase):
    def test_labelled_link_is_rendered_as_markdown(self):
        m = {'text': 'Pisang -> kalau ini kebaca?',
             'formattedText': '<https://pisang.example/q#abc|Pisang> -> kalau ini kebaca?'}
        self.assertEqual(google_chat.message_text(m), '[Pisang](https://pisang.example/q#abc) -> kalau ini kebaca?')

    def test_mentions_use_the_name_written_in_the_message(self):
        # The emoji before the mention is two UTF-16 units, which is how Chat counts offsets,
        # and the annotation's displayName is stale for a removed account.
        m = {'text': '🎣 @Andri @all see Notes',
             'formattedText': '🎣 <users/42> <users/all> see <https://docs.google.com/d/1/edit|Notes>',
             'annotations': [{'type': 'USER_MENTION', 'startIndex': 3, 'length': 6,
                              'userMention': {'user': {'name': 'users/42', 'displayName': 'Deleted User'}}}]}
        self.assertEqual(google_chat.message_text(m), '🎣 @Andri @all see [Notes](https://docs.google.com/d/1/edit)')

    def test_mention_falls_back_to_display_name_when_the_span_is_not_a_mention(self):
        m = {'text': 'hi', 'formattedText': 'hi <users/42>',
             'annotations': [{'type': 'USER_MENTION', 'startIndex': 0, 'length': 2,
                              'userMention': {'user': {'name': 'users/42', 'displayName': 'Andri'}}}]}
        self.assertEqual(google_chat.message_text(m), 'hi @Andri')

    def test_all_mention_keeps_the_senders_wording(self):
        m = {'text': 'hi @semua', 'formattedText': 'hi <users/all>',
             'annotations': [{'type': 'USER_MENTION', 'startIndex': 3, 'length': 6,
                              'userMention': {'user': {}}}]}
        self.assertEqual(google_chat.message_text(m), 'hi @semua')

    def test_mention_without_a_display_name_keeps_the_user_id(self):
        self.assertEqual(google_chat.message_text({'formattedText': 'hi <users/42>'}), 'hi @users/42')

    def test_repeated_labels_keep_their_own_links(self):
        m = {'text': 'here or here', 'formattedText': 'here or <https://x/2|here>'}
        self.assertEqual(google_chat.message_text(m), 'here or [here](https://x/2)')

    def test_custom_emoji_is_named_instead_of_a_replacement_char(self):
        m = {'text': 'thanks \ufffd', 'formattedText': 'thanks <customEmojis/:sungkem:>'}
        self.assertEqual(google_chat.message_text(m), 'thanks :sungkem:')

    def test_chat_formatting_and_literal_brackets_are_kept(self):
        m = {'text': 'use <b> tag', 'formattedText': '*use* `<b>` tag'}
        self.assertEqual(google_chat.message_text(m), '*use* `<b>` tag')

    def test_message_without_formatted_text_falls_back_to_text(self):
        self.assertEqual(google_chat.message_text({'text': 'plain'}), 'plain')
        self.assertEqual(google_chat.message_text({}), '')

    def test_drive_attachment_carries_its_file_id(self):
        a = {'contentName': 'Notes', 'contentType': 'application/vnd.google-apps.document',
             'driveDataRef': {'driveFileId': 'F1'}, 'source': 'DRIVE_FILE'}
        self.assertEqual(google_chat._attachment_fields(a)['driveFileId'], 'F1')
        self.assertNotIn('driveFileId', google_chat._attachment_fields({'attachmentDataRef': {'resourceName': 'R'}}))


class ToChatMarkupTest(unittest.TestCase):
    def test_markdown_link_bold_and_strike_become_chat_markup(self):
        self.assertEqual(google_chat.to_chat_markup('see [Pisang](https://p.example/q#a) **now** ~~old~~'),
                         'see <https://p.example/q#a|Pisang> *now* ~old~')

    def test_chat_markup_and_bare_urls_pass_through(self):
        text = 'ok <https://x/1|done> *bold* _it_ https://docs.google.com/d/1 <users/42>'
        self.assertEqual(google_chat.to_chat_markup(text), text)

    def test_code_is_left_literal(self):
        text = 'run `[a](https://x/1)` and\n```\n**not bold** [b](https://x/2)\n```\nthen [c](https://x/3)'
        self.assertEqual(google_chat.to_chat_markup(text),
                         'run `[a](https://x/1)` and\n```\n**not bold** [b](https://x/2)\n```\nthen <https://x/3|c>')

    def test_non_http_brackets_are_not_links(self):
        self.assertEqual(google_chat.to_chat_markup('array[0](x) and [todo]'), 'array[0](x) and [todo]')

    def test_rendered_text_round_trips_to_the_same_markup(self):
        m = {'formattedText': 'see <https://p.example/q|Pisang> *now*'}
        self.assertEqual(google_chat.to_chat_markup(google_chat.message_text(m)), m['formattedText'])


class SpaceUnreadTest(unittest.TestCase):
    ME, OTHER = 'users/me1', 'users/o1'

    def run_unread(self, space, read_state, *pages):
        http = mock.Mock()
        http.get.side_effect = [mock.Mock(json=mock.Mock(return_value=p)) for p in (read_state, *pages)]
        with mock.patch.object(google_chat, '_http', return_value=http):
            return google_chat._space_unread(None, space, self.ME), http

    def space(self, **kw):
        return {'name': SPACE, 'displayName': 'Ops', 'spaceType': 'SPACE',
                'lastActiveTime': '2026-09-18T10:00:00.5Z', **kw}

    def test_space_read_after_its_last_activity_is_skipped_without_listing(self):
        result, http = self.run_unread(self.space(), {'lastReadTime': '2026-09-18T10:00:01Z'}, {})
        self.assertIsNone(result)
        self.assertEqual(http.get.call_count, 1)

    def test_counts_only_others_messages_after_the_read_marker(self):
        page = {'messages': [{'sender': {'name': self.OTHER}}, {'sender': {'name': self.ME}}]}
        result, http = self.run_unread(self.space(), {'lastReadTime': '2026-09-18T09:00:00Z'}, page)
        self.assertEqual((result['unread'], result['name'], result['last_read']), (1, 'Ops', '2026-09-18T09:00:00Z'))
        self.assertEqual(http.get.call_args.kwargs['params']['filter'], 'createTime > "2026-09-18T09:00:00Z"')

    def test_only_own_messages_means_nothing_unread(self):
        page = {'messages': [{'sender': {'name': self.ME}}]}
        self.assertIsNone(self.run_unread(self.space(), {'lastReadTime': '2026-09-18T09:00:00Z'}, page)[0])

    def test_more_than_a_hundred_is_capped_and_dm_is_named_after_senders(self):
        page = {'messages': [{'sender': {'name': self.OTHER, 'displayName': 'Andri'}}] * 100, 'nextPageToken': 'x'}
        result, http = self.run_unread(self.space(displayName='', spaceType='DIRECT_MESSAGE'), {}, page)
        self.assertEqual((result['unread'], result['name'], result['last_read']), ('100+', 'Andri', None))
        self.assertEqual(http.get.call_count, 2)

    def test_pages_past_a_page_of_your_own_messages(self):
        mine = {'messages': [{'sender': {'name': self.ME}}] * 100, 'nextPageToken': 'p2'}
        theirs = {'messages': [{'sender': {'name': self.OTHER}}]}
        result, http = self.run_unread(self.space(), {'lastReadTime': '2026-09-18T09:00:00Z'}, mine, theirs)
        self.assertEqual(result['unread'], 1)
        self.assertEqual(http.get.call_args.kwargs['params']['pageToken'], 'p2')


    def test_epoch_last_active_is_unknown_so_the_space_is_still_checked(self):
        page = {'messages': [{'sender': {'name': self.OTHER}, 'createTime': '2026-09-18T09:30:00.1Z'}]}
        result, _ = self.run_unread(self.space(lastActiveTime='1970-01-01T00:00:00Z'),
                                    {'lastReadTime': '2026-09-18T09:00:00Z'}, page)
        self.assertEqual((result['unread'], result['latest']), (1, '2026-09-18T09:30:00Z'))


class SpaceEventsTest(unittest.TestCase):
    def setUp(self):
        self.chat = mock.MagicMock()
        for target, value in [('get_credentials', object()), ('_get_service', self.chat),
                              ('get_user_display_name', 'Husni')]:
            patcher = mock.patch.object(google_chat, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_events(self, pages, **kw):
        self.chat.spaces().spaceEvents().list.side_effect = [
            mock.Mock(execute=mock.Mock(return_value=p)) for p in pages]
        return asyncio.run(google_chat.list_space_events(SPACE, **kw))

    def test_filter_wraps_types_when_combined_with_time(self):
        self.run_events([{}], start_time='2026-09-01')
        self.assertEqual(
            self.chat.spaces().spaceEvents().list.call_args.kwargs['filter'],
            'start_time="2026-09-01T00:00:00Z" AND (event_types:"google.workspace.chat.message.v1.updated"'
            ' OR event_types:"google.workspace.chat.message.v1.deleted"'
            ' OR event_types:"google.workspace.chat.reaction.v1.created"'
            ' OR event_types:"google.workspace.chat.reaction.v1.deleted")')

    def test_single_type_without_time_is_a_bare_filter(self):
        self.run_events([{}], event_types=['message.deleted'])
        self.assertEqual(self.chat.spaces().spaceEvents().list.call_args.kwargs['filter'],
                         'event_types:"google.workspace.chat.message.v1.deleted"')

    def test_unknown_type_and_bad_time_are_rejected_before_any_request(self):
        with self.assertRaises(ValueError):
            self.run_events([], event_types=['message.edited'])
        with self.assertRaises(ValueError):
            self.run_events([], start_time='yesterday')
        self.chat.spaces().spaceEvents().list.assert_not_called()

    def test_entries_for_edit_deletion_reaction_and_batch(self):
        edited = msg('T.M1', text='new text', lastUpdateTime='2026-09-18T09:05:00Z')
        gone = {'name': f'{SPACE}/messages/T.M2', 'deleteTime': '2026-09-18T09:06:00.1Z',
                'deletionMetadata': {'deletionType': 'CREATOR'}}
        page = {'spaceEvents': [
            {'eventTime': '2026-09-18T09:05:00Z', 'eventType': 'google.workspace.chat.message.v1.updated',
             'messageUpdatedEventData': {'message': edited}},
            {'eventTime': '2026-09-18T09:06:00Z', 'eventType': 'google.workspace.chat.message.v1.deleted',
             'messageDeletedEventData': {'message': gone}},
            {'eventTime': '2026-09-18T09:07:00Z', 'eventType': 'google.workspace.chat.reaction.v1.batchCreated',
             'reactionBatchCreatedEventData': {'reactions': [
                 {'reaction': {'name': f'{SPACE}/messages/T.M1/reactions/r1', 'user': {'name': 'users/1'},
                               'emoji': {'unicode': '👍'}}},
                 {'reaction': {'name': f'{SPACE}/messages/T.M1/reactions/r2',
                               'emoji': {'customEmoji': {'emojiName': ':party:'}}}}]}},
        ]}
        events = self.run_events([page])['events']
        self.assertEqual((events[0]['type'], events[0]['id'], events[0]['text'], events[0]['edited']),
                         ('message.updated', 'T.M1', 'new text', '2026-09-18T09:05:00Z'))
        self.assertEqual(events[1], {'time': '2026-09-18T09:06:00Z', 'type': 'message.deleted', 'id': 'T.M2',
                                     'deleted': '2026-09-18T09:06:00Z', 'deletion': 'CREATOR'})
        self.assertEqual(events[2], {'time': '2026-09-18T09:07:00Z', 'type': 'reaction.created',
                                     'message': 'T.M1', 'user': 'Husni', 'emoji': '👍'})
        self.assertEqual((events[3]['type'], events[3]['emoji'], 'user' in events[3]),
                         ('reaction.created', ':party:', False))

    @staticmethod
    def deleted(time):
        return {'eventTime': time, 'eventType': 'google.workspace.chat.message.v1.deleted',
                'messageDeletedEventData': {'message': {'name': f'{SPACE}/messages/X', 'deleteTime': 'x'}}}

    @staticmethod
    def batch(time, n):
        return {'eventTime': time, 'eventType': 'google.workspace.chat.message.v1.batchDeleted',
                'messageBatchDeletedEventData': {'messages': [
                    {'message': {'name': f'{SPACE}/messages/B{i}', 'deleteTime': 'x'}} for i in range(n)]}}

    def test_limit_never_splits_events_of_one_time(self):
        page = {'spaceEvents': [self.deleted('2026-09-18T09:00:00.1Z'), self.deleted('2026-09-18T09:00:00.2Z')],
                'nextPageToken': 'n'}
        result = self.run_events([page, {'spaceEvents': [self.deleted('2026-09-18T09:00:00.3Z')] * 2}], limit=3)
        # Taking 3 would split the two events at .3, so the cut moves back before them.
        self.assertEqual((len(result['events']), result['next_start_time']), (2, '2026-09-18T09:00:00.2Z'))

    def test_a_batch_is_never_split_by_the_limit(self):
        result = self.run_events([{'spaceEvents': [self.deleted('2026-09-18T09:00:00.1Z'),
                                                   self.batch('2026-09-18T09:00:00.2Z', 3)]}], limit=2)
        self.assertEqual((len(result['events']), result['next_start_time']), (1, '2026-09-18T09:00:00.1Z'))

    def test_a_batch_larger_than_the_limit_is_returned_whole(self):
        result = self.run_events([{'spaceEvents': [self.batch('2026-09-18T09:00:00.2Z', 3),
                                                   self.deleted('2026-09-18T09:00:00.3Z')]}], limit=2)
        self.assertEqual((len(result['events']), result['next_start_time']), (3, '2026-09-18T09:00:00.2Z'))

    def test_everything_within_the_limit_has_no_continuation(self):
        result = self.run_events([{'spaceEvents': [self.deleted('2026-09-18T09:00:00.1Z')] * 3}], limit=3)
        self.assertNotIn('next_start_time', result)


class PinsAndLookupsTest(unittest.TestCase):
    def test_message_space_rejects_non_message_names(self):
        self.assertEqual(google_chat._message_space(f'{SPACE}/messages/T.M'), SPACE)
        for bad in [SPACE, f'{SPACE}/threads/T', 'messages/M']:
            with self.assertRaises(ValueError):
                google_chat._message_space(bad)

    def test_unpin_derives_the_pin_name_from_the_message(self):
        with mock.patch.object(google_chat, 'get_credentials', return_value=object()), \
                mock.patch.object(google_chat, '_chat_request', return_value={}) as request:
            asyncio.run(google_chat.unpin_message(f'{SPACE}/messages/T.M'))
        request.assert_called_once_with(mock.ANY, 'DELETE', f'{SPACE}/messagePins/T.M')

    def test_chat_request_raises_with_googles_message(self):
        resp = mock.Mock(ok=False, status_code=403, json=mock.Mock(
            return_value={'error': {'message': 'Permission denied'}}))
        with mock.patch.object(google_chat, '_http', return_value=mock.Mock(request=mock.Mock(return_value=resp))):
            with self.assertRaisesRegex(Exception, r'\(403\): Permission denied'):
                google_chat._chat_request(None, 'GET', 'spaces:findGroupChats')

    def lookup(self, outcome):
        people = mock.MagicMock()
        if isinstance(outcome, Exception):
            people.people().get().execute.side_effect = outcome
        else:
            people.people().get().execute.return_value = outcome
        with mock.patch.object(google_chat, '_get_service', return_value=people), \
                mock.patch.dict(google_chat._user_display_name_cache, clear=True):
            name = google_chat.get_user_display_name({'name': 'users/9', 'type': 'HUMAN'}, None)
            return name, dict(google_chat._user_display_name_cache)

    def http_error(self, status):
        from googleapiclient.errors import HttpError
        return HttpError(mock.Mock(status=status), b'{}')

    def test_name_lookup_caches_only_lasting_answers(self):
        self.assertEqual(self.lookup({'names': [{'displayName': 'Dewi'}]}), ('Dewi', {'users/9': 'Dewi'}))
        self.assertEqual(self.lookup({'names': []}), ('users/9', {'users/9': 'users/9'}))        # external, no name
        self.assertEqual(self.lookup(self.http_error(404)), ('users/9', {'users/9': 'users/9'}))  # deleted
        self.assertEqual(self.lookup(self.http_error(503)), ('users/9', {}))                     # transient
        self.assertEqual(self.lookup(self.http_error(429)), ('users/9', {}))
        self.assertEqual(self.lookup(ConnectionError('reset')), ('users/9', {}))

    def test_error_detail_reads_both_error_shapes(self):
        def resp(body, text='raw'):
            return mock.Mock(json=mock.Mock(return_value=body), text=text)
        self.assertEqual(google_chat._error_detail(resp({'error': {'message': 'denied'}})), 'denied')
        self.assertEqual(google_chat._error_detail(resp([{'error': {'message': 'Invalid resource name'}}])),
                         'Invalid resource name')
        broken = mock.Mock(json=mock.Mock(side_effect=ValueError), text='<html>502</html>')
        self.assertEqual(google_chat._error_detail(broken), '<html>502</html>')

    def test_get_space_keeps_useful_fields_and_only_restricted_permissions(self):
        chat = mock.MagicMock()
        chat.spaces().get().execute.return_value = {
            'name': SPACE, 'type': 'ROOM', 'displayName': 'Ops', 'spaceType': 'SPACE', 'customer': 'customers/C1',
            'spaceThreadingState': 'THREADED_MESSAGES', 'spaceHistoryState': 'HISTORY_OFF',
            'spaceDetails': {'description': 'On-call'}, 'membershipCount': {'joinedDirectHumanUserCount': 4},
            'accessSettings': {'accessState': 'PRIVATE'}, 'createTime': '2023-02-17T02:44:34.85Z',
            'lastActiveTime': '1970-01-01T00:00:00Z', 'spaceUri': 'https://chat.google.com/room/S',
            'permissionSettings': {
                'postMessages': {'managersAllowed': True, 'assistantManagersAllowed': True, 'membersAllowed': True},
                'manageApps': {'managersAllowed': True}}}
        with mock.patch.object(google_chat, 'get_credentials', return_value=object()), \
                mock.patch.object(google_chat, '_get_service', return_value=chat):
            space = asyncio.run(google_chat.get_space(SPACE))
        self.assertEqual(space, {'space': SPACE, 'name': 'Ops', 'type': 'SPACE', 'description': 'On-call',
                                 'members': 4, 'history_off': True, 'created': '2023-02-17T02:44:34Z',
                                 'uri': 'https://chat.google.com/room/S', 'restricted': {'manageApps': ['managers']}})

    def test_get_member_reports_a_non_member_without_invented_fields(self):
        chat = mock.MagicMock()
        chat.spaces().members().get().execute.return_value = {'name': f'{SPACE}/members/9', 'state': 'NOT_A_MEMBER'}
        with mock.patch.object(google_chat, 'get_credentials', return_value=object()), \
                mock.patch.object(google_chat, '_get_service', return_value=chat):
            self.assertEqual(asyncio.run(google_chat.get_member(SPACE, 'users/9')),
                             {'user_id': 'users/9', 'state': 'NOT_A_MEMBER'})

    def test_members_are_listed_once_and_a_failed_listing_raises(self):
        chat = mock.MagicMock()
        chat.spaces().members().list.return_value.execute.return_value = {'memberships': [
            {'member': {'name': 'users/7', 'displayName': 'Dewi', 'type': 'HUMAN'}, 'role': 'ROLE_MANAGER'}]}
        with mock.patch.object(google_chat, 'get_credentials', return_value=object()), \
                mock.patch.object(google_chat, '_get_service', return_value=chat):
            members = asyncio.run(google_chat.list_space_members(SPACE))
            self.assertEqual(members, [{'user_id': 'users/7', 'display_name': 'Dewi', 'mention': '<users/7>',
                                        'type': 'HUMAN', 'role': 'ROLE_MANAGER'}])
            self.assertEqual(chat.spaces().members().list.call_count, 1)
            chat.spaces().members().list.return_value.execute.side_effect = RuntimeError('404')
            with self.assertRaisesRegex(Exception, '404'):
                asyncio.run(google_chat.list_space_members(SPACE))

class ReviewFixesTest(unittest.TestCase):
    def test_markdown_link_keeps_parentheses_in_the_url(self):
        self.assertEqual(google_chat.to_chat_markup('[Foo](https://en.wikipedia.org/wiki/Foo_(bar)) done'),
                         '<https://en.wikipedia.org/wiki/Foo_(bar)|Foo> done')

    def pins_with(self, status):
        resp = mock.Mock(status_code=status, ok=status < 400, json=mock.Mock(return_value=msg('T.M')))
        resp.raise_for_status.side_effect = None if status < 400 else RuntimeError(f'{status}')
        with mock.patch.object(google_chat, 'get_credentials', return_value=object()), \
                mock.patch.object(google_chat, '_chat_request',
                                  return_value={'messagePins': [{'message': f'{SPACE}/messages/T.M'}]}), \
                mock.patch.object(google_chat, '_http', return_value=mock.Mock(get=mock.Mock(return_value=resp))), \
                mock.patch.object(google_chat, 'get_user_display_name', return_value='Husni'):
            return asyncio.run(google_chat.list_pinned_messages(SPACE))

    def test_pin_you_cannot_read_is_unavailable_but_a_server_error_raises(self):
        self.assertEqual(self.pins_with(404)['pins'], [{'id': 'T.M', 'unavailable': 404}])
        self.assertEqual(self.pins_with(200)['pins'][0]['text'], 'hi')
        with self.assertRaisesRegex(RuntimeError, '503'):
            self.pins_with(503)

    def test_self_id_is_looked_up_again_for_another_account(self):
        people = mock.MagicMock()
        people.people().get().execute.side_effect = [{'resourceName': 'people/1'}, {'resourceName': 'people/2'}]
        with mock.patch.object(google_chat, '_get_service', return_value=people), \
                mock.patch.dict(google_chat._self_id_cache, clear=True):
            a, b = mock.Mock(refresh_token='ra'), mock.Mock(refresh_token='rb')
            self.assertEqual([google_chat.self_user_id(a), google_chat.self_user_id(a),
                              google_chat.self_user_id(b)], ['users/1', 'users/1', 'users/2'])


class GetSpacesTest(unittest.TestCase):
    def test_compact_filtered_and_most_recent_first(self):
        chat = mock.MagicMock()
        chat.spaces().list.return_value.execute.return_value = {'spaces': [
            {'name': 'spaces/old', 'displayName': 'Ops Old', 'spaceType': 'SPACE',
             'lastActiveTime': '2026-01-01T00:00:00Z', 'membershipCount': {}, 'spaceUri': 'u'},
            {'name': 'spaces/new', 'displayName': 'ops new', 'spaceType': 'SPACE',
             'lastActiveTime': '2026-09-18T00:00:00.5Z'},
            {'name': 'spaces/gone', 'displayName': 'Ops gone', 'spaceType': 'SPACE',
             'lastActiveTime': '1970-01-01T00:00:00Z'},
            {'name': 'spaces/dm', 'spaceType': 'DIRECT_MESSAGE', 'lastActiveTime': '2026-09-18T01:00:00Z'}]}
        with mock.patch.object(google_chat, 'get_credentials', return_value=object()), \
                mock.patch.object(google_chat, '_get_service', return_value=chat):
            result = asyncio.run(google_chat.list_chat_spaces(query='OPS', limit=2))
            self.assertEqual(result, {'total': 3, 'spaces': [
                {'space': 'spaces/new', 'name': 'ops new', 'type': 'SPACE', 'last_active': '2026-09-18T00:00:00Z'},
                {'space': 'spaces/old', 'name': 'Ops Old', 'type': 'SPACE', 'last_active': '2026-01-01T00:00:00Z'}]})
            asyncio.run(google_chat.list_chat_spaces(space_type='GROUP_CHAT'))
            self.assertEqual(chat.spaces().list.call_args.kwargs['filter'], 'spaceType = "GROUP_CHAT"')
            with self.assertRaises(ValueError):
                asyncio.run(google_chat.list_chat_spaces(space_type='ROOM'))


class SearchErrorTest(unittest.TestCase):
    def search_failing_with(self, status):
        error = google_chat.ChatApiError('POST', 'spaces/-/messages:search', status, 'API says no')
        with mock.patch.object(google_chat, 'get_credentials', return_value=object()), \
                mock.patch.object(google_chat, '_chat_request', side_effect=error):
            with self.assertRaises(Exception) as ctx:
                asyncio.run(google_chat.search_space_messages('deploy'))
        return str(ctx.exception)

    def test_status_specific_messages_keep_googles_detail(self):
        self.assertIn('Developer Preview', self.search_failing_with(403))
        self.assertIn('rejected filter', self.search_failing_with(400))
        self.assertIn('HTTP 500 API says no', self.search_failing_with(500))


if __name__ == '__main__':
    unittest.main()
