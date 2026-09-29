import gc
import tracemalloc
import unittest

from google.oauth2.credentials import Credentials

import google_chat


def creds(token):
    # build() reads the bundled discovery document, so nothing here reaches Google.
    return Credentials(token=token)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        google_chat._service_cache.clear()
        self.addCleanup(google_chat._service_cache.clear)

    def test_collections_are_built_once(self):
        chat = google_chat._get_service('chat', 'v1', creds('t1'))
        self.assertIs(chat.spaces(), chat.spaces())
        self.assertIs(chat.spaces().messages(), chat.spaces().messages())
        self.assertIs(chat.spaces().messages().reactions(), chat.spaces().messages().reactions())

    def test_methods_still_build_requests(self):
        chat = google_chat._get_service('chat', 'v1', creds('t1'))
        req = chat.spaces().messages().list(parent='spaces/S', pageSize=5)
        self.assertIsInstance(req, google_chat._RetryingHttpRequest)
        self.assertIn('/v1/spaces/S/messages', req.uri)
        self.assertIn('pageSize=5', req.uri)
        self.assertTrue(callable(chat.spaces().messages().list_next))

    def test_generated_docstrings_are_dropped(self):
        messages = google_chat._get_service('chat', 'v1', creds('t1')).spaces().messages()
        self.assertIsNone(messages.create.__doc__)
        self.assertIsNone(messages.update.__doc__)

    def test_a_new_token_gets_a_new_service(self):
        old = google_chat._get_service('chat', 'v1', creds('t1'))
        new = google_chat._get_service('chat', 'v1', creds('t2'))
        self.assertIsNot(old, new)
        self.assertEqual(len(google_chat._service_cache), 1)

    def test_polling_does_not_pile_up_memory(self):
        # Each spaces().messages() used to render 24 MiB of docstrings into a cycle
        # that waited for a full GC; a channel held a gigabyte of them.
        chat = google_chat._get_service('chat', 'v1', creds('t1'))
        chat.spaces().messages().list(parent='spaces/S')
        chat.spaces().spaceEvents().list(parent='spaces/S', filter='x')
        gc.collect()
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        base = tracemalloc.get_traced_memory()[0]
        for _ in range(20):
            chat.spaces().messages().list(parent='spaces/S')
            chat.spaces().spaceEvents().list(parent='spaces/S', filter='x')
        self.assertLess(tracemalloc.get_traced_memory()[0] - base, 2 ** 20)


if __name__ == '__main__':
    unittest.main()
