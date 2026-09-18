import asyncio
import unittest
from unittest import mock

import google_chat


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        self.chat = mock.MagicMock()
        for target, value in [('get_credentials', object()), ('_get_service', self.chat)]:
            patcher = mock.patch.object(google_chat, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)


class CustomEmojiTest(Base):
    def setUp(self):
        super().setUp()
        self.chat.customEmojis().list.return_value.execute.return_value = {
            'customEmojis': [{'emojiName': ':party-parrot:', 'uid': 'u1', 'name': 'customEmojis/u1'},
                             {'emojiName': ':ship-it:', 'uid': 'u2', 'name': 'customEmojis/u2'}]}
        patcher = mock.patch.dict(google_chat._custom_emoji_uids, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_list_and_filter(self):
        self.assertEqual(run(google_chat.list_custom_emojis('SHIP')), {'total': 1, 'emojis': [':ship-it:']})

    def test_reaction_body_for_unicode_and_custom(self):
        self.assertEqual(google_chat._reaction_emoji('👍', None), {'unicode': '👍'})
        self.assertEqual(google_chat._reaction_emoji(':ship-it:', None), {'customEmoji': {'name': 'customEmojis/u2'}})
        with self.assertRaisesRegex(ValueError, 'list_custom_emojis'):
            google_chat._reaction_emoji(':nope:', None)


class StatusTest(Base):
    def test_validation_happens_before_any_request(self):
        for kwargs in [{'state': 'busy'}, {'state': 'dnd', 'minutes': 0}, {'status_text': 'lunch'},
                       {'state': 'dnd'}, {'status_text': 'lunch', 'status_emoji': '🍜'},
                       {'status_text': 'x' * 65, 'status_emoji': '🍜'}, {}]:
            with self.assertRaises(ValueError, msg=kwargs):
                run(google_chat.set_my_status(**kwargs))

    def test_dnd_for_a_while_with_a_custom_status(self):
        calls = []

        def request(creds, method, path, **kw):
            calls.append((method, path, kw))
            return {'state': 'DO_NOT_DISTURB', 'doNotDisturbMetadata': {'expirationTime': '2026-09-18T10:00:00.5Z'},
                    'customStatus': {'emoji': {'unicode': '🎧'}, 'text': 'focus'}}

        with mock.patch.object(google_chat, '_chat_request', side_effect=request):
            status = run(google_chat.set_my_status('dnd', 30, 'focus', '🎧'))
        self.assertEqual(calls[0], ('POST', 'users/me/availability:markAsDoNotDisturb', {'json': {'ttl': '1800s'}}))
        self.assertEqual(calls[1][:2], ('PATCH', 'users/me/availability'))
        self.assertEqual(calls[1][2]['params'], {'updateMask': 'customStatus'})
        self.assertEqual(calls[1][2]['json']['customStatus']['ttl'], '1800s')
        self.assertEqual(status, {'state': 'do_not_disturb', 'dnd_until': '2026-09-18T10:00:00Z',
                                  'custom_status': {'emoji': '🎧', 'text': 'focus'}})


class NotificationsTest(Base):
    def test_patch_sends_only_what_changes(self):
        patch = self.chat.users().spaces().spaceNotificationSetting().patch
        patch.return_value.execute.return_value = {'notificationSetting': 'FOR_YOU', 'muteSetting': 'MUTED'}
        result = run(google_chat.set_space_notifications('spaces/S', muted=True))
        self.assertEqual(patch.call_args.kwargs['updateMask'], 'mute_setting')
        self.assertEqual(patch.call_args.kwargs['body'], {'muteSetting': 'MUTED'})
        self.assertEqual(result, {'space': 'spaces/S', 'notifications': 'for_you', 'muted': True})
        with self.assertRaises(ValueError):
            run(google_chat.set_space_notifications('spaces/S', notifications='loud'))


class CreateSpaceTest(Base):
    def test_rules_per_space_type(self):
        for args in [('ROOM', ['users/1']), ('SPACE', ['users/1']), ('GROUP_CHAT', ['users/1']),
                     ('DIRECT_MESSAGE', ['users/1', 'users/2']), ('GROUP_CHAT', ['users/1', 'users/2'], 'Named'),
                     ('DIRECT_MESSAGE', ['someone@x.com'])]:
            with self.assertRaises(ValueError, msg=args):
                run(google_chat.create_space(*args))
        self.chat.spaces().setup.assert_not_called()

    def test_setup_body_is_idempotent_and_compact_result(self):
        setup = self.chat.spaces().setup
        setup.return_value.execute.return_value = {'name': 'spaces/NEW', 'displayName': 'Ops', 'spaceType': 'SPACE',
                                                   'spaceUri': 'https://chat.google.com/room/NEW'}
        result = run(google_chat.create_space('SPACE', ['users/a@x.com'], 'Ops', 'On-call'))
        body = setup.call_args.kwargs['body']
        self.assertEqual(body['space'], {'spaceType': 'SPACE', 'displayName': 'Ops', 'spaceDetails': {'description': 'On-call'}})
        self.assertEqual(body['memberships'], [{'member': {'name': 'users/a@x.com', 'type': 'HUMAN'}}])
        self.assertTrue(body['requestId'])
        self.assertEqual(result, {'space': 'spaces/NEW', 'name': 'Ops', 'type': 'SPACE',
                                  'uri': 'https://chat.google.com/room/NEW'})


if __name__ == '__main__':
    unittest.main()
