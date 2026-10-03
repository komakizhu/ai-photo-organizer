"""Public workflow tests use generated files, never the user's photos."""

import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from photo_workflow import apply_approved, capture_time, plan_names, scan_folder


class FolderWorkflowTests(unittest.TestCase):
    def test_scan_keeps_file_dates_separate_from_missing_capture_date(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            photo = source / 'no-exif.jpg'
            photo.write_bytes(b'fixture')
            inventory = scan_folder(source, lambda paths: {})
            item = inventory['items'][0]
            self.assertIsNone(item['captureTime']['value'])
            self.assertIsNotNone(item['fileTimes']['created'])
            self.assertIsNotNone(item['fileTimes']['modified'])
            self.assertEqual(item['sortTime']['source'], 'file_time')
            self.assertEqual(item['sortTime']['value'], min(
                item['fileTimes']['created'], item['fileTimes']['modified']))

    def test_scan_uses_capture_date_and_keeps_original_folder_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'Trip'
            folder.mkdir()
            photo = folder / 'IMG_001.JPG'
            photo.write_bytes(b'fixture')
            screenshot = root / 'screen.png'
            screenshot.write_bytes(b'fixture')
            before = sorted(str(p.relative_to(root)) for p in root.rglob('*'))
            def metadata(paths):
                self.assertEqual(set(paths), {photo.resolve(), screenshot.resolve()})
                return {str(photo.resolve()): {'EXIF:DateTimeOriginal': '2020:07:08 09:10:11',
                                               'EXIF:OffsetTimeOriginal': '+08:00'}}
            inventory = scan_folder(root, metadata)
            by_name = {item['originalName']: item for item in inventory['items']}
            self.assertEqual(by_name['IMG_001.JPG']['captureTime']['value'],
                             '2020-07-08 09:10:11')
            self.assertEqual(by_name['IMG_001.JPG']['folderHint'], 'Trip')
            self.assertIsNone(by_name['screen.png']['captureTime']['value'])
            self.assertEqual(before, sorted(str(p.relative_to(root)) for p in root.rglob('*')))

    def test_missing_capture_date_is_not_invented_from_file_mtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'IMG_1.jpg').write_bytes(b'fixture')
            inventory = scan_folder(root, lambda paths: {})
            plan = plan_names(inventory)
            self.assertIsNone(plan['items'][0]['suggestedName'])
            self.assertIn('拍摄时间缺失', plan['items'][0]['issue'])

    def test_name_plan_is_same_folder_and_resolves_same_second_collisions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('IMG_1.JPG', 'IMG_2.JPG'):
                (root / name).write_bytes(name.encode())
            def metadata(paths):
                return {str(path): {'EXIF:DateTimeOriginal': '2021:02:03 04:05:06'}
                        for path in paths}
            plan = plan_names(scan_folder(root, metadata),
                              {'IMG_1.JPG': '小林', 'IMG_2.JPG': '小林'})
            names = [item['suggestedName'] for item in plan['items']]
            self.assertEqual(names, ['小林_2021-02-03_04-05-06.jpg',
                                     '小林_2021-02-03_04-05-06_02.jpg'])

    def test_only_explicit_approval_renames_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'old folder'
            folder.mkdir()
            first = folder / 'a.jpg'
            second = folder / 'b.jpg'
            first.write_bytes(b'first')
            second.write_bytes(b'second')
            plan = plan_names(scan_folder(root, lambda paths: {
                str(path): {'EXIF:DateTimeOriginal': '2022:03:04 05:06:07'}
                for path in paths}))
            output = apply_approved(plan, {'old folder/a.jpg': 'new.jpg'})
            self.assertEqual(output[0]['status'], 'renamed')
            self.assertEqual((folder / 'new.jpg').read_bytes(), b'first')
            self.assertEqual(second.read_bytes(), b'second')
            self.assertFalse(first.exists())

    def test_changed_or_conflicting_file_fails_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / 'a.jpg'
            second = root / 'b.jpg'
            first.write_bytes(b'first')
            second.write_bytes(b'second')
            plan = plan_names(scan_folder(root, lambda paths: {}))
            conflict = apply_approved(plan, {'a.jpg': 'b.jpg'})
            self.assertEqual(conflict[0]['status'], 'failed')
            self.assertEqual(second.read_bytes(), b'second')
            first.write_bytes(b'changed')
            stale = apply_approved(plan, {'a.jpg': 'c.jpg'})
            self.assertEqual(stale[0]['status'], 'failed')
            self.assertFalse((root / 'c.jpg').exists())

    def test_path_escape_or_extension_change_is_never_approved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / 'a.jpg'
            original.write_bytes(b'fixture')
            plan = plan_names(scan_folder(root, lambda paths: {}))
            for destination in ('../escape.jpg', '/tmp/escape.jpg', 'new.png'):
                result = apply_approved(plan, {'a.jpg': destination})
                self.assertEqual(result[0]['status'], 'failed')
            self.assertTrue(original.exists())
            with self.assertRaises(ValueError):
                apply_approved(plan, {'outside.jpg': 'new.jpg'})

    def test_apple_library_and_symlinked_subfolder_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'Photos.photoslibrary').mkdir()
            (root / 'Photos.photoslibrary' / 'hidden.jpg').write_bytes(b'x')
            (root / 'link').symlink_to(root / 'Photos.photoslibrary')
            (root / 'normal.jpg').write_bytes(b'x')
            inventory = scan_folder(root, lambda paths: {})
            self.assertEqual([item['originalName'] for item in inventory['items']],
                             ['normal.jpg'])
            with self.assertRaises(ValueError):
                scan_folder(root / 'Photos.photoslibrary', lambda paths: {})

    def test_apply_rejects_a_forged_apple_photos_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            library = Path(temp, 'Photos.photoslibrary')
            library.mkdir()
            asset = library / 'asset.jpg'
            asset.write_bytes(b'fixture')
            forged = {'source': str(library.resolve()), 'items': [
                {'relativePath': 'asset.jpg', 'signature': {
                    'device': asset.stat().st_dev, 'inode': asset.stat().st_ino,
                    'size': asset.stat().st_size,
                    'modifiedNs': asset.stat().st_mtime_ns}}]}
            with self.assertRaises(ValueError):
                apply_approved(forged, {'asset.jpg': 'new.jpg'})
            self.assertTrue(asset.exists())


class MetadataTests(unittest.TestCase):
    def test_exiftool_exififd_capture_time_and_offset(self):
        result = capture_time({'ExifIFD:DateTimeOriginal': '2026:06:25 12:26:44',
                               'ExifIFD:OffsetTimeOriginal': '+08:00'})
        self.assertEqual(result['value'], '2026-06-25 12:26:44')
        self.assertEqual(result['timezone'], '+08:00')
        self.assertFalse(result['uncertain'])

    def test_invalid_camera_date_is_unconfirmed(self):
        self.assertIsNone(capture_time({'EXIF:DateTimeOriginal':
                                        '2024:13:44 99:00:00'})['value'])

    def test_conflicting_exif_and_xmp_dates_require_review(self):
        result = capture_time({'EXIF:DateTimeOriginal': '2024:01:02 03:04:05',
                               'XMP-exif:DateTimeOriginal': '2025:01:02 03:04:05'})
        self.assertIsNone(result['value'])
        self.assertEqual(result['issue'], 'conflicting capture times')

    def test_embedded_xmp_timezone_is_preserved(self):
        result = capture_time({'XMP-exif:DateTimeOriginal':
                               '2024-01-02T03:04:05+08:00'})
        self.assertEqual(result['timezone'], '+08:00')
        self.assertFalse(result['uncertain'])

    @unittest.skipUnless(shutil.which('exiftool'), 'ExifTool is not installed')
    def test_real_exiftool_reads_generated_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp, 'fixture.png')
            def chunk(kind, data):
                return (struct.pack('>I', len(data)) + kind + data +
                        struct.pack('>I', zlib.crc32(kind + data)))
            image.write_bytes(
                b'\x89PNG\r\n\x1a\n' +
                chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)) +
                chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00\xff')) +
                chunk(b'IEND', b''))
            completed = subprocess.run(
                ['exiftool', '-overwrite_original',
                 '-XMP-exif:DateTimeOriginal=2024:01:02 03:04:05+08:00',
                 str(image)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            inventory = scan_folder(temp)
            self.assertEqual(inventory['items'][0]['captureTime']['value'],
                             '2024-01-02 03:04:05')
            self.assertEqual(inventory['items'][0]['captureTime']['timezone'], '+08:00')


if __name__ == '__main__':
    unittest.main()
