"""Exercise the Apple Photos HTTP boundary with synthetic PhotoKit responses."""
import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apple_photos_api import ApplePhotosService, make_handler


class ApplePhotosAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.calls = []
        self.launches = []

        def native(request, timeout=10):
            self.calls.append(request)
            command = request['command']
            if command in ('connect', 'status'):
                return {'ok': True, 'status': 'authorized'}
            if command == 'assets':
                return {'ok': True, 'assets': [{'assetId': 'synthetic-1'}],
                        'nextCursor': None, 'hasMore': False}
            if command == 'thumbnail':
                return {'ok': True, 'data': base64.b64encode(b'generated-thumbnail').decode()}
            return {'ok': True}

        self.origin = 'http://127.0.0.1:4173'
        probe = ThreadingHTTPServer(('127.0.0.1', 0), lambda *_: None)
        self.port = probe.server_address[1]
        probe.server_close()
        self.service = ApplePhotosService(
            runtime=os.path.join(self.temporary.name, 'runtime'),
            origins={self.origin}, port=self.port, native_call=native,
            launcher=lambda: self.launches.append(True),
        )
        self.server = ThreadingHTTPServer(('127.0.0.1', self.port), make_handler(self.service))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.port}/v1/'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, *, body=None, token=None, origin=None, host=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {'Origin': origin or self.origin}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        if token:
            headers['X-Photo-Studio-Token'] = token
        if host:
            headers['Host'] = host
        return urlopen(Request(self.base + path, data=data, headers=headers,
                               method='GET' if data is None else 'POST'), timeout=3)

    def connect(self):
        with self.request('connect', body={}) as response:
            return json.load(response)

    def test_status_does_not_launch_component_or_request_access(self):
        with self.request('status') as response:
            result = json.load(response)
        self.assertEqual(result['authorization'], 'unknown')
        self.assertEqual(result['mode'], 'native')
        self.assertEqual(self.launches, [])
        self.assertEqual(self.calls, [])

    def test_connect_is_explicit_and_stores_private_session_token(self):
        result = self.connect()
        self.assertTrue(result['connected'])
        self.assertEqual(result['mode'], 'native')
        self.assertEqual(self.launches, [True])
        self.assertEqual([call['command'] for call in self.calls], ['connect'])
        mode = os.stat(self.service.token_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(result['token'], self.service.token)

    def test_assets_require_session_token_and_forward_filters_and_cursor(self):
        result = self.connect()
        with self.assertRaises(HTTPError) as caught:
            self.request('assets', body={'screenshots': True})
        self.assertEqual(caught.exception.code, 403)
        with self.request('assets', body={'screenshots': True, 'startDate': '2026-09-01',
                                          'endDate': '2026-09-30',
                                          'cursor': {'query': 'q', 'offset': 60}},
                          token=result['token']) as response:
            assets = json.load(response)
        self.assertEqual(assets['assets'][0]['assetId'], 'synthetic-1')
        forwarded = self.calls[-1]
        self.assertEqual(forwarded['command'], 'assets')
        self.assertTrue(forwarded['screenshots'])
        self.assertEqual(forwarded['cursor']['offset'], 60)

    def test_thumbnail_returns_image_bytes_and_never_puts_asset_id_in_url(self):
        result = self.connect()
        with self.request('thumbnail', body={'assetId': 'generated-asset'},
                          token=result['token']) as response:
            self.assertEqual(response.headers.get_content_type(), 'image/jpeg')
            self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), self.origin)
            self.assertEqual(response.headers.get('Vary'), 'Origin')
            self.assertEqual(response.read(), b'generated-thumbnail')
            self.assertNotIn('generated-asset', response.url)

    def test_foreign_origin_and_host_are_rejected(self):
        for arguments in ({'origin': 'http://attacker.example'},
                          {'host': 'attacker.example:8768'}):
            with self.assertRaises(HTTPError) as caught:
                self.request('connect', body={}, **arguments)
            self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.launches, [])

    def test_cancel_and_disconnect_clear_session(self):
        result = self.connect()
        with self.request('cancel', body={}, token=result['token']) as response:
            self.assertTrue(json.load(response)['cancelled'])
        with self.request('disconnect', body={}, token=result['token']) as response:
            self.assertFalse(json.load(response)['connected'])
        self.assertFalse(self.service.connected)
        self.assertFalse(self.service.token_path.exists())

    def test_mutating_or_unknown_library_routes_are_not_exposed(self):
        self.connect()
        with self.assertRaises(HTTPError) as caught:
            self.request('delete', body={}, token=self.service.token)
        self.assertEqual(caught.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
