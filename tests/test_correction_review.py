"""Explicit local review: loopback access, blind display and enum-only saves."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from anatomy.correction_review import make_server

PRIVATE = 'alice@example.com /Users/alice/private </script><script>alert(1)</script>'


class LocalReview(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, 'reviews.jsonl')
        self.output = open(self.path, 'w+', encoding='utf-8')
        self.server, self.state, self.url = make_server([
            {'key': 'a' * 64, 'text': PRIVATE, 'tail': 'previous ' + PRIVATE},
            {'key': 'b' * 64, 'text': 'A second question', 'tail': ''}], self.output)
        self.prefix = self.url.split(str(self.server.server_port), 1)[1]
        self.origin = 'http://127.0.0.1:%d' % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.output.close()
        self.directory.cleanup()

    def request(self, method, path=None, payload=None, **headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        body = json.dumps(payload) if payload is not None else None
        if method == 'POST':
            headers = {'Origin': self.origin, 'Content-Type': 'application/json', **headers}
        connection.request(method, path or self.prefix + 'item', body, headers)
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_explicit_browser_get_is_blind_and_html_has_no_private_text(self):
        code, body, headers = self.request('GET')
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data['user'], PRIVATE)
        self.assertNotIn('label', data)
        self.assertNotIn('key', data)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        code, html, _ = self.request('GET', self.prefix)
        self.assertEqual(code, 200)
        self.assertNotIn(PRIVATE.encode(), html)
        self.assertIn(b'.textContent=data.user', html)

    def test_answers_persist_only_opaque_key_and_label(self):
        code, body, _ = self.request('POST', payload={'position': 0, 'label': 'correction'})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['reviewed'], 1)
        self.output.seek(0)
        saved = self.output.read()
        self.assertEqual(json.loads(saved), {'key': 'a' * 64, 'label': 'correction'})
        self.assertNotIn(PRIVATE, saved)
        self.assertEqual(self.state.current()['user'], 'A second question')

    def test_token_host_and_origin_required(self):
        self.assertEqual(self.request('GET', '/item')[0], 404)
        self.assertEqual(self.request('GET', Host='evil.example')[0], 403)
        self.assertEqual(self.request('POST', payload={'position': 0, 'label': 'new'}, Origin='https://evil.example')[0], 403)
        self.assertEqual(len(self.state.answers), 0)

    def test_invalid_or_stale_answers_cannot_change_saved_review(self):
        for payload in [{'position': True, 'label': 'new'}, {'position': 0, 'label': PRIVATE},
                        {'position': 0, 'label': 'new', 'extra': PRIVATE}, {'position': 1, 'label': 'new'}]:
            self.assertEqual(self.request('POST', payload=payload)[0], 400)
        self.assertEqual(self.request('POST', payload={'position': 0, 'label': 'unknown'})[0], 200)
        self.assertEqual(self.request('POST', payload={'position': 0, 'label': 'new'})[0], 400)
        self.assertEqual(self.state.answers[0]['label'], 'unknown')

    def test_completed_review_stops_displaying_excerpts(self):
        self.request('POST', payload={'position': 0, 'label': 'new'})
        code, body, _ = self.request('POST', payload={'position': 1, 'label': 'unknown'})
        self.assertEqual(code, 200)
        done = json.loads(body)
        self.assertTrue(done['done'])
        self.assertNotIn('user', done)
        self.assertNotIn('previous_assistant', done)
        self.assertEqual(self.request('POST', payload={'position': 2, 'label': 'new'})[0], 400)

    def test_storage_failure_freezes_review_instead_of_saving_conflicting_retry(self):
        with mock.patch('anatomy.correction_review.os.fsync', side_effect=OSError('disk failure')):
            self.assertEqual(self.request('POST', payload={'position': 0, 'label': 'correction'})[0], 500)
        self.assertEqual(self.request('POST', payload={'position': 0, 'label': 'new'})[0], 500)
        self.assertEqual(self.request('GET')[0], 500)
        self.assertTrue(self.state.failed)
        self.output.seek(0)
        lines = self.output.read().splitlines()
        self.assertLessEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['label'], 'correction')


if __name__ == '__main__':
    unittest.main()
