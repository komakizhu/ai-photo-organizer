"""Exercise the preview's real bridge using generated photos only."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen

import folder_studio
from preview_folder_studio import PreviewService, generate_photos


class PreviewBridgeTests(unittest.TestCase):
    def test_user_folder_selection_switches_real_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first, second, state = root / 'first', root / 'second', root / 'state'
            generate_photos(first)
            generate_photos(second)
            (second / 'sample-3-b.png').unlink()
            with patch.multiple(folder_studio, PERSON_REVIEW_STATE=state / 'people',
                                VISION_RESULTS=state / 'vision.jsonl',
                                CODEX_RESULTS=state / 'codex.json'), \
                    patch('folder_studio.volume_uuid', return_value='test-volume'), \
                    patch('folder_studio.choose_local_photo_folder', return_value=second):
                service = PreviewService(state=state, source=first,
                                         expected_volume='test-volume', index_path=state / 'index.sqlite')
                origin = 'http://127.0.0.1:4173'
                service.origins = {origin}
                server = ThreadingHTTPServer(('127.0.0.1', 0), folder_studio.make_handler(service))
                threading.Thread(target=server.serve_forever, daemon=True).start()
                base = f'http://127.0.0.1:{server.server_port}'
                old_batch = service.batch['batchId']
                try:
                    request = Request(base + '/v1/source/select',
                                      data=json.dumps({'batchId': old_batch}).encode(),
                                      headers={'Origin': origin, 'Content-Type': 'application/json',
                                               'X-Photo-Studio-Token': service.token})
                    with urlopen(request, timeout=10) as response:
                        selected = json.load(response)
                    self.assertFalse(selected['cancelled'])
                    self.assertEqual(selected['batch']['source'], str(second))
                    self.assertEqual(len(selected['batch']['items']), 5)
                    self.assertNotEqual(selected['batch']['batchId'], old_batch)
                    self.assertFalse(service.source_capabilities()['immichRename'])
                    with urlopen(Request(base + '/v1/batch', headers={'Origin': origin})) as response:
                        self.assertEqual(json.load(response)['batch']['source'], str(second))
                    self.assertTrue((state / 'batch.json').is_file())
                    self.assertFalse((second / 'batch.json').exists())
                finally:
                    server.shutdown()
                    server.server_close()
