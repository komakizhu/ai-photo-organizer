"""Native Haar probe tests on generated images only."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from duplicate_detection import duplicate_candidate_groups, fingerprint_one, haar_similarity


HAAR_BINARY = Path(os.environ.get(
    'HAAR_FINGERPRINT_BINARY',
    '/Volumes/T7_1T/local-photo-studio/photo-studio-build/haar_fingerprint'))
SIDE = 128


def write_ppm(path, variant):
    pixels = bytearray()
    for y in range(SIDE):
        for x in range(SIDE):
            if variant == 'different':
                value = 30 if (x + y) % 12 < 6 else 220
            else:
                value = 35 + (x * 2 + y) % 120
                if 12 < x < 82 and 16 < y < 74:
                    value = 195
                if (x - 94) ** 2 + (y - 42) ** 2 < 18 ** 2:
                    value = 55
                if variant == 'bright':
                    value = min(255, value + 9)
            pixels.extend((value, max(0, value - 3), min(255, value + 4)))
    path.write_bytes(b'P6\n128 128\n255\n' + pixels)


@unittest.skipUnless(HAAR_BINARY.is_file() and shutil.which('sips'),
                     'compile the Haar helper and use macOS sips first')
class HaarCandidateTests(unittest.TestCase):
    def test_recompressed_similar_and_distinct_generated_images(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            paths = {}
            for variant in ('base', 'bright', 'different'):
                ppm, jpg = temp / f'{variant}.ppm', temp / f'{variant}.jpg'
                write_ppm(ppm, variant)
                subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '82',
                                str(ppm), '--out', str(jpg)],
                               capture_output=True, check=True, timeout=30)
                paths[variant] = jpg
            features = {name: fingerprint_one(HAAR_BINARY, path)
                        for name, path in paths.items()}
            similar = haar_similarity(features['base'], features['bright'])
            different = haar_similarity(features['base'], features['different'])
            self.assertGreaterEqual(similar, 0.60, f'similar={similar}')
            self.assertLess(different, 0.60, f'different={different}')


class DuplicateGroupTests(unittest.TestCase):
    def test_visual_components_follow_edges_and_keep_rejected_pairs_out(self):
        pairs = [
            {'kind': 'visual', 'version_a': 'a', 'version_b': 'b',
             'path_a': 'a.jpg', 'path_b': 'b.jpg', 'decision': 'suggested'},
            {'kind': 'visual', 'version_a': 'b', 'version_b': 'c',
             'path_a': 'b.jpg', 'path_b': 'c.jpg', 'decision': 'suggested'},
            {'kind': 'visual', 'version_a': 'c', 'version_b': 'd',
             'path_a': 'c.jpg', 'path_b': 'd.jpg', 'decision': 'suggested'},
            {'kind': 'visual', 'version_a': 'd', 'version_b': 'e',
             'path_a': 'd.jpg', 'path_b': 'e.jpg', 'decision': 'not_duplicate'},
        ]
        groups = duplicate_candidate_groups(pairs)
        self.assertEqual([group['versionIds'] for group in groups], [['a', 'b', 'c', 'd']])
        self.assertEqual(len(groups[0]['edges']), 3)

    def test_exact_components_remain_distinct_from_visual_edges(self):
        pairs = [
            {'kind': 'exact', 'version_a': 'a', 'version_b': 'b',
             'path_a': 'a.jpg', 'path_b': 'b.jpg', 'decision': 'suggested'},
            {'kind': 'visual', 'version_a': 'b', 'version_b': 'c',
             'path_a': 'b.jpg', 'path_b': 'c.jpg', 'decision': 'suggested'},
        ]
        groups = duplicate_candidate_groups(pairs)
        self.assertEqual([(group['kind'], group['versionIds']) for group in groups], [
            ('exact', ['a', 'b']), ('visual', ['b', 'c'])])


if __name__ == '__main__':
    unittest.main()
