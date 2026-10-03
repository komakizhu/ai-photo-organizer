"""Generated-manifest tests; no private photographs are opened."""

import json
import tempfile
import unittest
from pathlib import Path

from vision_groups import classify_scan, load_vision_results


class VisionGroupsTests(unittest.TestCase):
    def test_screenshot_with_a_face_is_grouped_as_screenshot(self):
        result = classify_scan({
            'status': 'scanned', 'filename': 'random.png', 'extension': 'png',
            'face_count': 1, 'human_count': 1, 'ocr_line_count': 6,
            'vision_labels': [{'label': 'screenshot', 'confidence': 0.92}],
        })
        self.assertEqual(result['category'], '截图／屏幕内容')
        self.assertIn('有人', result['tags'])

    def test_unreadable_image_stays_unconfirmed(self):
        result = classify_scan({'status': 'decode_error', 'filename': 'cat.jpg'})
        self.assertEqual(result['category'], '待确认／其他')
        self.assertTrue(result['needsReview'])

    def test_manifest_never_attaches_a_stale_or_out_of_scope_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, 'photos')
            root.mkdir()
            photo = root / 'screen.png'
            photo.write_bytes(b'image')
            outside = Path(temporary, 'outside.png')
            outside.write_bytes(b'outside')
            manifest = Path(temporary, 'vision.jsonl')
            base = {'status': 'scanned', 'filename': 'screen.png', 'extension': 'png',
                    'vision_labels': [{'label': 'screenshot', 'confidence': 0.9}]}
            rows = [
                {**base, 'path': str(photo), 'relative_path': 'screen.png',
                 'file_size_bytes': 5, 'file_modified_unix': photo.stat().st_mtime},
                {**base, 'path': str(outside), 'relative_path': 'outside.png',
                 'file_size_bytes': 7, 'file_modified_unix': outside.stat().st_mtime},
            ]
            manifest.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            inventory = [{'relativePath': 'screen.png', 'signature': {
                'size': 5, 'modifiedNs': photo.stat().st_mtime_ns}}]
            found = load_vision_results(root, inventory, manifest)
            self.assertEqual(found['screen.png']['category'], '截图／屏幕内容')
            photo.write_bytes(b'changed')
            inventory[0]['signature']['size'] = 7
            inventory[0]['signature']['modifiedNs'] = photo.stat().st_mtime_ns
            self.assertEqual(load_vision_results(root, inventory, manifest), {})


if __name__ == '__main__':
    unittest.main()
