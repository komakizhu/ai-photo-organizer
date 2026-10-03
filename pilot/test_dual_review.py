import unittest
import json
import os
import select
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from dual_review import reconcile_boxes, validate_decision


class ReconcileTests(unittest.TestCase):
    def test_same_location_is_both_detected(self):
        immich = [{'box': [0.1, 0.1, 0.3, 0.3], 'id': 'i1'}]
        vision = [{'box': [0.12, 0.12, 0.3, 0.3], 'face': 1}]
        result = reconcile_boxes(immich, vision)
        self.assertEqual(result['status'], 'both')
        self.assertEqual(result['matched'], 1)
        self.assertEqual(result['immichOnly'], 0)
        self.assertEqual(result['visionOnly'], 0)

    def test_disjoint_boxes_need_review(self):
        result = reconcile_boxes(
            [{'box': [0.1, 0.1, 0.2, 0.2]}],
            [{'box': [0.7, 0.7, 0.2, 0.2]}],
        )
        self.assertEqual(result['status'], 'disagreement')
        self.assertEqual(result['immichOnly'], 1)
        self.assertEqual(result['visionOnly'], 1)

    def test_neither_detected_is_explicit(self):
        self.assertEqual(reconcile_boxes([], [])['status'], 'neither')

    def test_decisions_reject_unknown_values(self):
        self.assertTrue(validate_decision('photo', 'some-id', 'has-face'))
        self.assertTrue(validate_decision('pair', 'a:1|b:1', 'same'))
        self.assertFalse(validate_decision('pair', 'a:1|b:1', 'delete'))
        self.assertFalse(validate_decision('photo', '../bad', 'has-face'))


class SavedReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.token_dir = tempfile.TemporaryDirectory(prefix='dual-review-token-')

    def tearDown(self):
        self.token_dir.cleanup()

    def start_service(self, state_dir=None):
        report = Path('/Volumes/T7_1T/local-photo-studio/immich-sample/dual-review/report.json')
        if state_dir is None:
            if os.environ.get('PHOTO_STUDIO_RUN_REAL_SAMPLE') != '1':
                self.skipTest('real sample report is opt-in; generated fixtures remain the default')
            if not report.exists():
                self.skipTest('authorized sample report is not mounted')
        args = ([sys.executable, str(Path(__file__).with_name('dual_review.py')),
                 '--serve-report', '--port', '0'] +
                (['--state-dir', str(state_dir)] if state_dir else []) +
                ['--token-path', str(Path(self.token_dir.name, 'review.token'))])
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        ready, _, _ = select.select([process.stdout], [], [], 20)
        if not ready:
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            self.fail(f'review service did not start; stdout={stdout!r}; stderr={stderr!r}')
        url = process.stdout.readline().strip().removeprefix('Review page: ')
        if not url.startswith('http://127.0.0.1:'):
            process.terminate()
            _, stderr = process.communicate(timeout=5)
            self.fail(f'review service returned unexpected output {url!r}; stderr={stderr!r}')
        return process, url

    def stop_service(self, process):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()

    def test_saved_review_is_accessible_without_immich(self):
        process, url = self.start_service()
        try:
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor())
            opener.open(url, timeout=5).read()
            saved = json.load(opener.open(url.split('/?')[0] + '/api/report', timeout=5))
            self.assertEqual(saved['imageCount'], 40)
            first = saved['photos'][0]['id']
            thumbnail = opener.open(url.split('/?')[0] + '/thumb/' + first, timeout=15)
            self.assertEqual(thumbnail.status, 200)
            self.assertTrue(thumbnail.headers['Content-Type'].startswith('image/'))
        finally:
            self.stop_service(process)

    def test_review_link_survives_service_restart(self):
        first, first_url = self.start_service()
        self.stop_service(first)
        second, second_url = self.start_service()
        try:
            self.assertEqual(first_url.split('token=')[1], second_url.split('token=')[1])
        finally:
            self.stop_service(second)

    def test_review_report_collapses_confirmed_pairs_and_skips_far_pairs(self):
        ids = ['11111111-1111-1111-1111-111111111111',
               '22222222-2222-2222-2222-222222222222',
               '33333333-3333-3333-3333-333333333333',
               '44444444-4444-4444-4444-444444444444']
        names = ['a.jpg', 'b.jpg', 'c.jpg', 'd.jpg']
        photos = [{'id': id_, 'name': name, 'status': 'both',
                   'immich': [{'personId': 'p1' if i < 2 else 'p'+str(i)}],
                   'vision': [{'face': 1}]}
                  for i, (id_, name) in enumerate(zip(ids, names))]
        def pair(i, j, distance):
            return {'key': names[i]+':1|'+names[j]+':1', 'aId': ids[i],
                    'bId': ids[j], 'a': names[i], 'b': names[j],
                    'distance': distance, 'aPerson': 'a', 'bPerson': 'b'}
        report = {'imageCount': 4, 'photos': photos,
                  'pairs': [pair(0, 1, .3), pair(0, 2, .42),
                            pair(1, 2, .45), pair(0, 3, .5)]}
        decisions = {'pair:a.jpg:1|b.jpg:1': {'value': 'same'}}
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'report.json').write_text(json.dumps(report))
            Path(temp, 'decisions.json').write_text(json.dumps(decisions))
            process, url = self.start_service(temp)
            try:
                opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor())
                opener.open(url, timeout=5)
                view = json.load(opener.open(url.split('/?')[0] + '/api/report'))
                self.assertEqual(view['confirmedGroups'][0]['assetIds'], ids[:2])
                self.assertEqual([p['key'] for p in view['reviewQueue']],
                                 ['a.jpg:1|c.jpg:1'])
            finally:
                self.stop_service(process)

    def test_create_person_preset_and_find_its_members_on_page(self):
        ids = ['11111111-1111-1111-1111-111111111111',
               '22222222-2222-2222-2222-222222222222']
        report = {'imageCount': 2, 'photos': [{'id': id_, 'name': str(i)+'.jpg',
                                              'immich': [], 'vision': []}
                                             for i, id_ in enumerate(ids)], 'pairs': []}
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'report.json').write_text(json.dumps(report))
            process, url = self.start_service(temp)
            try:
                base = url.split('/?')[0]
                opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor())
                opener.open(url, timeout=5)
                request = urllib.request.Request(
                    base + '/api/presets',
                    data=json.dumps({'action': 'create', 'name': '小林',
                                     'assetIds': ids}).encode(),
                    headers={'Content-Type': 'application/json',
                             'X-Review-Action': 'preset', 'Origin': base},
                )
                response = json.load(opener.open(request, timeout=5))
                self.assertEqual(response['preset']['name'], '小林')
                view = json.load(opener.open(base + '/api/report', timeout=5))
                self.assertEqual(view['presets'][0]['assetIds'], ids)
            finally:
                self.stop_service(process)

    def test_person_preset_can_gain_and_lose_photos_without_moving_originals(self):
        ids = ['11111111-1111-1111-1111-111111111111',
               '22222222-2222-2222-2222-222222222222']
        report = {'imageCount': 2, 'photos': [{'id': id_, 'name': str(i)+'.jpg',
                                              'immich': [], 'vision': []}
                                             for i, id_ in enumerate(ids)], 'pairs': []}
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'report.json').write_text(json.dumps(report))
            process, url = self.start_service(temp)
            try:
                base = url.split('/?')[0]
                opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor())
                opener.open(url, timeout=5)
                def action(payload):
                    request = urllib.request.Request(
                        base + '/api/presets', data=json.dumps(payload).encode(),
                        headers={'Content-Type': 'application/json',
                                 'X-Review-Action': 'preset', 'Origin': base})
                    return json.load(opener.open(request, timeout=5))
                preset_id = action({'action': 'create', 'name': '朋友',
                                    'assetIds': [ids[0]]})['preset']['id']
                action({'action': 'add', 'presetId': preset_id, 'assetIds': [ids[1]]})
                view = json.load(opener.open(base + '/api/report'))
                self.assertEqual(view['presets'][0]['assetIds'], ids)
                action({'action': 'remove', 'presetId': preset_id, 'assetIds': [ids[0]]})
                view = json.load(opener.open(base + '/api/report'))
                self.assertEqual(view['presets'][0]['assetIds'], [ids[1]])
            finally:
                self.stop_service(process)

    def test_disconnected_state_volume_does_not_fall_back_to_another_directory(self):
        id_ = '11111111-1111-1111-1111-111111111111'
        report = {'imageCount': 1, 'photos': [{'id': id_, 'name': 'a.jpg',
                                              'immich': [], 'vision': []}], 'pairs': []}
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp, 'state')
            state_dir.mkdir()
            Path(state_dir, 'report.json').write_text(json.dumps(report))
            process, url = self.start_service(state_dir)
            try:
                base = url.split('/?')[0]
                opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor())
                opener.open(url, timeout=5)
                state_dir.rename(Path(temp, 'disconnected'))
                request = urllib.request.Request(
                    base + '/api/presets',
                    data=json.dumps({'action': 'create', 'name': '小林',
                                     'assetIds': [id_]}).encode(),
                    headers={'Content-Type': 'application/json',
                             'X-Review-Action': 'preset', 'Origin': base})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(request, timeout=5)
                self.assertEqual(error.exception.code, 503)
                self.assertFalse(state_dir.exists())
            finally:
                self.stop_service(process)


if __name__ == '__main__':
    unittest.main()
