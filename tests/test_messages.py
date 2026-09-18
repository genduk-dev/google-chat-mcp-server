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
                           ('prefetch_space_members', {}),
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

if __name__ == '__main__':
    unittest.main()
