"""Exercise the isolated preview's public responses, using generated assets only."""
import json
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from xml.etree import ElementTree

from immich_selection_preview import ASSETS, PreviewHandler


class SelectionPreviewResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), PreviewHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def fetch(self, path):
        with urlopen(self.base + path, timeout=5) as response:
            self.assertEqual(response.status, 200)
            return response.read()

    def test_detail_does_not_masquerade_as_internal_timeline_asset(self):
        # The frontend uses ratio as its TimelineAsset discriminator. An ISO
        # date plus ratio skips conversion and makes Luxon throw Invalid unit 0.
        for asset_id in ASSETS:
            with self.subTest(asset_id=asset_id):
                detail = json.loads(self.fetch(f'/api/assets/{asset_id}'))
                self.assertNotIn('ratio', detail)
                self.assertEqual(detail['type'], 'IMAGE')
                for field in ('localDateTime', 'fileCreatedAt', 'createdAt'):
                    self.assertIsInstance(detail[field], str)
                    datetime.fromisoformat(detail[field].replace('Z', '+00:00'))
                self.assertGreater(detail['exifInfo']['exifImageWidth'], 0)

    def test_thumbnails_match_declared_dimensions_without_letterboxing(self):
        for asset_id, asset in ASSETS.items():
            with self.subTest(asset_id=asset_id):
                svg = ElementTree.fromstring(self.fetch(f'/api/assets/{asset_id}/thumbnail'))
                self.assertEqual(int(svg.attrib['width']), asset['width'])
                self.assertEqual(int(svg.attrib['height']), asset['height'])
                self.assertEqual(svg.attrib['preserveAspectRatio'], 'xMidYMid slice')


if __name__ == '__main__':
    unittest.main()
