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


if __name__ == '__main__':
    unittest.main()
