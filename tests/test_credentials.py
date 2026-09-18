import datetime
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import requests

import google_chat


def token(refresh, scopes):
    expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).replace(tzinfo=None)
    return {'token': f'access-{refresh}', 'refresh_token': refresh, 'client_id': 'c', 'client_secret': 's',
            'token_uri': 'https://oauth2.googleapis.com/token', 'scopes': scopes,
            'expiry': expiry.isoformat() + 'Z'}


class TokenFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / 'token.json'
        patcher = mock.patch.dict(google_chat.token_info, {'credentials': None, 'mtime': None,
                                                           'token_path': str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, data, mtime_ns):
        self.path.write_text(json.dumps(data))
        os.utime(self.path, ns=(mtime_ns, mtime_ns))

    def test_a_token_written_by_another_process_replaces_the_one_in_memory(self):
        self.write(token('old', ['a']), 1_000_000_000)
        self.assertEqual(google_chat.get_credentials().refresh_token, 'old')
        # A re-login in another session writes a new token with more scopes.
        self.write(token('new', ['a', 'b']), 2_000_000_000)
        creds = google_chat.get_credentials()
        self.assertEqual((creds.refresh_token, sorted(creds.scopes)), ('new', ['a', 'b']))

    def test_scopes_come_from_the_file(self):
        self.write(token('r', ['only-this']), 1_000_000_000)
        self.assertEqual(google_chat.get_credentials().scopes, ['only-this'])

    def test_saving_is_owner_only_and_is_not_reloaded_as_foreign(self):
        self.write(token('r', ['a']), 1_000_000_000)
        creds = google_chat.get_credentials()
        google_chat.save_credentials(creds)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with mock.patch.object(google_chat, '_load_token_file') as load:
            self.assertIs(google_chat.get_credentials(), creds)
        load.assert_not_called()



class LoopbackAuthTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.client = Path(self.dir.name) / 'credentials.json'
        self.client.write_text(json.dumps({'installed': {
            'client_id': 'c', 'client_secret': 's', 'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token', 'redirect_uris': ['http://localhost']}}))

    def start(self):
        url = google_chat.start_authentication(str(self.client))
        self.addCleanup(setattr, google_chat._auth_server, 'done', True)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return query['redirect_uri'][0], query['state'][0]

    def test_redirect_with_our_state_saves_the_token_by_itself(self):
        redirect, state = self.start()
        self.assertRegex(redirect, r'^http://localhost:\d+/$')
        with mock.patch.object(google_chat, '_exchange_code') as exchange:
            resp = requests.get(redirect, params={'state': state, 'code': 'abc'}, timeout=5)
        self.assertEqual(resp.status_code, 200)
        exchange.assert_called_once_with(mock.ANY, 'abc')

    def test_redirect_with_another_state_is_refused(self):
        redirect, _ = self.start()
        with mock.patch.object(google_chat, '_exchange_code') as exchange:
            resp = requests.get(redirect, params={'state': 'forged', 'code': 'abc'}, timeout=5)
        self.assertEqual(resp.status_code, 400)
        exchange.assert_not_called()


class WritePrivateTest(unittest.TestCase):
    def test_owner_only_and_no_shared_temp_name(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'token.json'
            seen = []
            real_open = os.open
            def spy(name, flags, mode=0o777):
                seen.append((str(name), mode))
                return real_open(name, flags, mode)
            with mock.patch.object(google_chat.os, 'open', side_effect=spy):
                google_chat.write_private(path, 'x')
                google_chat.write_private(path, 'y')
            self.assertEqual(path.read_text(), 'y')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual({mode for _, mode in seen}, {0o600})
            self.assertNotEqual(seen[0][0], seen[1][0])
            self.assertEqual(sorted(os.listdir(d)), ['token.json'])


class ManualCompletionTest(unittest.TestCase):
    def setUp(self):
        self.flow = mock.Mock()
        for name, value in [('_pending_auth_flow', self.flow), ('_pending_auth_state', 'S1'), ('_auth_server', None)]:
            patcher = mock.patch.object(google_chat, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_failed_exchange_can_be_retried(self):
        with mock.patch.object(google_chat, '_exchange_code', side_effect=[Exception('invalid_grant'),
                                                                           mock.Mock(expiry=None)]):
            with self.assertRaises(Exception):
                google_chat.complete_authentication('typo')
            self.assertTrue(google_chat.complete_authentication('good')['authenticated'])

    def test_callback_from_another_attempt_is_refused(self):
        with mock.patch.object(google_chat, '_exchange_code') as exchange:
            with self.assertRaisesRegex(Exception, 'another sign-in attempt'):
                google_chat.complete_authentication('http://localhost:1/?state=OLD&code=c')
        exchange.assert_not_called()


class UserNamesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for patcher in (mock.patch.dict(google_chat.token_info, {'token_path': str(Path(self.dir.name) / 'token.json')}),
                        mock.patch.dict(google_chat._user_display_name_cache, clear=True),
                        mock.patch.object(google_chat, '_user_names', {}),
                        mock.patch.object(google_chat, '_user_names_mtime', None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.people = mock.MagicMock()
        from googleapiclient.errors import HttpError
        self.people.people().get().execute.side_effect = HttpError(mock.Mock(status=404), b'{}')
        patcher = mock.patch.object(google_chat, '_get_service', return_value=self.people)
        patcher.start()
        self.addCleanup(patcher.stop)

    def name(self, sender):
        return google_chat.get_user_display_name({'type': 'HUMAN', **sender}, None)

    def test_saved_name_replaces_the_id_and_google_name_wins(self):
        self.assertEqual(self.name({'name': 'users/9'}), 'users/9')   # deleted account, no name yet
        result = google_chat.set_user_name('users/9', 'Budi (ex-ops)')
        self.assertEqual(result, {'user_id': 'users/9', 'name': 'Budi (ex-ops)', 'saved_names': 1})
        self.assertEqual(self.name({'name': 'users/9'}), 'Budi (ex-ops)')
        self.assertEqual(self.name({'name': 'users/8', 'displayName': 'Dewi'}), 'Dewi')
        path = Path(self.dir.name) / 'user_names.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_another_sessions_change_is_picked_up_including_removal(self):
        google_chat.set_user_name('users/9', 'Budi')
        self.assertEqual(self.name({'name': 'users/9'}), 'Budi')
        path = Path(self.dir.name) / 'user_names.json'
        path.write_text(json.dumps({'users/9': 'Budi Santoso'}))
        os.utime(path, ns=(10**18, 10**18))
        self.assertEqual(self.name({'name': 'users/9'}), 'Budi Santoso')
        path.write_text('{}')
        os.utime(path, ns=(2 * 10**18, 2 * 10**18))
        self.assertEqual(self.name({'name': 'users/9'}), 'users/9')

    def test_empty_name_removes_and_bad_ids_are_rejected(self):
        google_chat.set_user_name('users/9', 'Budi')
        self.assertEqual(google_chat.set_user_name('users/9', ' ')['saved_names'], 0)
        for bad in ['9', 'users/abc', 'spaces/9']:
            with self.assertRaises(ValueError):
                google_chat.set_user_name(bad, 'x')

if __name__ == '__main__':
    unittest.main()
