"""Exercise the native Vision scanner using generated pixels only."""

import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path


SCANNER = Path(__file__).with_name('local_vision_scan.swift')


def write_generated_png(path, width=192, height=192):
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend(((x * 3 + y) % 256, (y * 2 + x) % 256,
                        (x + y * 3) % 256))
        rows.append(bytes(row))

    def chunk(kind, data):
        checksum = zlib.crc32(kind + data) & 0xffffffff
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', checksum)

    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    payload = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) +
               chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b''))
    path.write_bytes(payload)


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'),
                     'Apple Vision integration requires macOS and swiftc')
class LocalVisionScannerTests(unittest.TestCase):
    def test_scans_generated_image_and_keeps_output_outside_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, 'generated-photos')
            state = Path(temporary, 'private-state')
            root.mkdir()
            state.mkdir()
            photo = root / 'generated-pattern.png'
            write_generated_png(photo)
            original_bytes = photo.read_bytes()
            original_stat = photo.stat()
            binary = state / 'local_vision_scan'
            output = state / 'scan.jsonl'

            subprocess.run(['swiftc', '-O', str(SCANNER), '-o', str(binary)],
                           capture_output=True, check=True, timeout=180)
            result = subprocess.run([str(binary), str(root), str(output), '--workers', '2'],
                                    capture_output=True, text=True, check=True, timeout=120)

            rows = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['relative_path'], 'generated-pattern.png')
            self.assertEqual(rows[0]['status'], 'scanned')
            self.assertIn('PROGRESS\t1\t1\t0', result.stderr)
            self.assertIn('DONE\t1\t1\t0', result.stderr)
            self.assertEqual(photo.read_bytes(), original_bytes)
            self.assertEqual(photo.stat().st_size, original_stat.st_size)
            self.assertEqual(output.parent.resolve(), state.resolve())


if __name__ == '__main__':
    unittest.main()
