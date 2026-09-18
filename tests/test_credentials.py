import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


if __name__ == '__main__':
    unittest.main()
