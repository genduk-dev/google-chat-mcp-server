import unittest
from unittest import mock

from googleapiclient.errors import HttpError
from googleapiclient.http import HttpMockSequence

import google_chat

OK = ({'status': '200'}, '{"name": "spaces/S/messages/M"}')
LIMITED = ({'status': '429'}, '{"error": {"code": 429, "message": "Resource has been exhausted"}}')
UNAVAILABLE = ({'status': '503'}, '{"error": {"code": 503, "message": "unavailable"}}')


def request(method, responses):
    http = HttpMockSequence(list(responses))
    req = google_chat._RetryingHttpRequest(http, lambda resp, content: content, 'https://chat.googleapis.com/v1/x',
                                           method=method)
    return req, http


class RetryPolicyTest(unittest.TestCase):
    def test_429_retries_any_method_but_5xx_only_reads(self):
        self.assertTrue(google_chat._should_retry('POST', 429))
        self.assertTrue(google_chat._should_retry('get', 503))
        self.assertFalse(google_chat._should_retry('POST', 503))
        self.assertFalse(google_chat._should_retry('GET', 404))

    def test_delay_honours_retry_after_and_backs_off(self):
        self.assertEqual(google_chat._retry_delay(0, '7'), 7.0)
        self.assertEqual(google_chat._retry_delay(0, '999'), 30.0)
        self.assertTrue(1 <= google_chat._retry_delay(0) < 1.5)
        self.assertTrue(4 <= google_chat._retry_delay(2) < 4.5)


@mock.patch.object(google_chat.time, 'sleep')
class RetryingRequestTest(unittest.TestCase):
    def test_write_limited_by_429_is_retried_until_it_succeeds(self, sleep):
        req, _ = request('POST', [LIMITED, LIMITED, OK])
        self.assertIn(b'spaces/S/messages/M', req.execute())
        self.assertEqual(sleep.call_count, 2)

    def test_write_failing_with_5xx_is_not_repeated(self, sleep):
        req, _ = request('POST', [UNAVAILABLE, OK])
        with self.assertRaises(HttpError):
            req.execute()
        sleep.assert_not_called()

    def test_read_failing_with_5xx_is_retried(self, sleep):
        req, _ = request('GET', [UNAVAILABLE, OK])
        req.execute()
        self.assertEqual(sleep.call_count, 1)

    def test_gives_up_after_the_last_attempt(self, sleep):
        req, _ = request('POST', [LIMITED] * (google_chat.RETRY_ATTEMPTS + 1))
        with self.assertRaises(HttpError) as ctx:
            req.execute()
        self.assertEqual(ctx.exception.resp.status, 429)
        self.assertEqual(sleep.call_count, google_chat.RETRY_ATTEMPTS)


class RawHttpRetryTest(unittest.TestCase):
    def test_session_policy_matches(self):
        session = google_chat._http(mock.Mock())
        retry = session.get_adapter('https://chat.googleapis.com').max_retries
        self.assertIsInstance(retry, google_chat._ChatRetry)
        self.assertTrue(retry.is_retry('POST', 429))
        self.assertFalse(retry.is_retry('POST', 502))
        self.assertTrue(retry.is_retry('GET', 502))
        self.assertFalse(retry.raise_on_status)
        response = mock.Mock(headers={'Retry-After': '120'})
        response.headers = {'Retry-After': '120'}
        self.assertEqual(retry.get_retry_after(response), 30.0)


class RawHttpRetryIntegrationTest(unittest.TestCase):
    """The real requests/urllib3 stack against a local server that rate-limits first."""

    def serve(self, statuses):
        import http.server
        import threading
        import requests
        from requests.adapters import HTTPAdapter
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def respond(self):
                hits.append(self.command)
                status = statuses[min(len(hits), len(statuses)) - 1]
                self.send_response(status)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

            do_GET = do_POST = respond

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        session = requests.Session()
        self.addCleanup(session.close)
        session.mount('http://', HTTPAdapter(max_retries=google_chat._ChatRetry(
            total=google_chat.RETRY_ATTEMPTS, connect=0, read=0, allowed_methods=None, raise_on_status=False)))
        return session, f'http://127.0.0.1:{server.server_address[1]}/', hits

    @mock.patch.object(google_chat, '_retry_delay', return_value=0)
    def test_post_rate_limited_twice_then_succeeds(self, _):
        session, url, hits = self.serve([429, 429, 200])
        self.assertEqual(session.post(url, json={}).status_code, 200)
        self.assertEqual(hits, ['POST'] * 3)

    @mock.patch.object(google_chat, '_retry_delay', return_value=0)
    def test_post_5xx_is_returned_not_repeated(self, _):
        session, url, hits = self.serve([503, 200])
        self.assertEqual(session.post(url, json={}).status_code, 503)
        self.assertEqual(hits, ['POST'])

    @mock.patch.object(google_chat, '_retry_delay', return_value=0)
    def test_exhausted_retries_hand_back_the_last_response(self, _):
        session, url, hits = self.serve([429])
        self.assertEqual(session.get(url).status_code, 429)
        self.assertEqual(len(hits), google_chat.RETRY_ATTEMPTS + 1)


if __name__ == '__main__':
    unittest.main()
