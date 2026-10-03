"""Generated-file tests; these never read the user's photo source."""

import tempfile
import threading
import unittest
import json
import io
import time
from pathlib import Path
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from folder_studio import (annotation_changes, approve_batch, build_batch,
                           check_source, choose_local_photo_folder, load_model_results,
                           make_handler, Service,
                           people_review_snapshot, save_people_review_action)
from photo_index import PhotoIndex


class FolderStudioTests(unittest.TestCase):
    def test_native_folder_picker_returns_only_a_user_selected_folder_or_cancel(self):
        with patch('folder_studio.subprocess.run', return_value=SimpleNamespace(
                stdout='{"path":"/tmp/selected photos"}')) as run:
            self.assertEqual(str(choose_local_photo_folder()), '/tmp/selected photos')
            self.assertEqual(run.call_args.args[0][0], 'swift')
            self.assertNotIn('shell', run.call_args.kwargs)
        with patch('folder_studio.subprocess.run', return_value=SimpleNamespace(
                stdout='{"cancelled":true}')):
            self.assertIsNone(choose_local_photo_folder())

    def test_switching_gallery_builds_a_new_source_scoped_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary, 'first-library')
            second = Path(temporary, 'second-library')
            first.mkdir()
            second.mkdir()
            state = Path(temporary, 'state')
            index_path = Path(temporary, 'index.sqlite')
            first_batch = {'batchId': 'batch-first', 'sourceId': 'source-first',
                           'source': str(first), 'volumeUuid': None, 'items': []}
            second_batch = {'batchId': 'batch-second', 'sourceId': 'source-second',
                            'source': str(second), 'volumeUuid': None, 'items': []}
            with patch('folder_studio.build_batch', side_effect=[first_batch, second_batch]):
                service = Service(state=state, source=first, expected_volume=None,
                                  index_path=index_path)
                changed = service.select_source_path(second, None,
                                                     batch_id='batch-first')
            self.assertFalse(changed['cancelled'])
            self.assertEqual(service.batch['sourceId'], 'source-second')
            self.assertEqual(service.batch['source'], str(second))
            self.assertFalse(service.can_rename_in_immich())
            self.assertEqual(json.loads((state / 'batch.json').read_text())['sourceId'],
                             'source-second')

    def test_gallery_switch_rejects_a_stale_batch_and_unavailable_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, 'library')
            source.mkdir()
            batch = {'batchId': 'batch-current', 'sourceId': 'source-current',
                     'source': str(source), 'volumeUuid': None, 'items': []}
            with patch('folder_studio.build_batch', return_value=batch):
                service = Service(state=Path(temporary, 'state'), source=source,
                                  expected_volume=None,
                                  index_path=Path(temporary, 'index.sqlite'))
                with self.assertRaisesRegex(ValueError, '批次已变化'):
                    service.select_source_path(source, None, batch_id='batch-old')
                with self.assertRaisesRegex(ValueError, '未挂载'):
                    service.select_source_path(Path(temporary, 'missing'), None,
                                               batch_id='batch-current')
            self.assertEqual(service.batch['batchId'], 'batch-current')

    def test_source_picker_http_requires_local_origin_token_and_batch(self):
        calls = []
        service = SimpleNamespace(
            origins={'http://127.0.0.1:2283'}, token='test-token',
            batch={'batchId': 'batch-1', 'sourceId': 'source-1'},
            source_capabilities=lambda: {'immichRename': False},
            ensure_state_available=lambda: None,
            select_source_from_picker=lambda batch_id: (
                calls.append(batch_id) or {'cancelled': True, 'batch': service.batch}))
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/v1/source/select'
        payload = json.dumps({'batchId': 'batch-1', 'source': '/tmp/attacker-path'}).encode()
        origin = {'Origin': 'http://127.0.0.1:2283', 'Content-Type': 'application/json'}
        try:
            with self.assertRaises(HTTPError) as missing_token:
                urlopen(Request(url, data=payload, headers=origin), timeout=5)
            self.assertEqual(missing_token.exception.code, 403)
            headers = {**origin, 'X-Photo-Studio-Token': 'test-token'}
            stale_payload = json.dumps({'batchId': 'batch-old'}).encode()
            with patch.object(service, 'select_source_from_picker',
                              side_effect=ValueError('图库批次已变化')):
                with self.assertRaises(HTTPError) as stale_batch:
                    urlopen(Request(url, data=stale_payload, headers=headers), timeout=5)
            self.assertEqual(stale_batch.exception.code, 400)
            with urlopen(Request(url, data=payload, headers=headers), timeout=5) as response:
                result = json.load(response)
            self.assertTrue(result['cancelled'])
            self.assertEqual(calls, ['batch-1'])
        finally:
            server.shutdown()
            server.server_close()

    def test_unmapped_selected_gallery_cannot_use_immich_rename_endpoint(self):
        service = SimpleNamespace(
            origins={'http://127.0.0.1:2283'}, token='test-token',
            lock=threading.Lock(), ensure_state_available=lambda: None,
            can_rename_in_immich=lambda: False)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.dumps({'batchId': 'batch-1', 'approvals': {'a.jpg': 'b.jpg'},
                                  'confirmation': '确认ExFAT原地改名'}).encode()
            request = Request(f'http://127.0.0.1:{server.server_port}/v1/approve',
                              data=payload, headers={
                                  'Origin': 'http://127.0.0.1:2283',
                                  'Content-Type': 'application/json',
                                  'X-Photo-Studio-Token': 'test-token'})
            with self.assertRaises(HTTPError) as blocked:
                urlopen(request, timeout=5)
            self.assertEqual(blocked.exception.code, 400)
            self.assertIn('改名已关闭', blocked.exception.read().decode())
        finally:
            server.shutdown()
            server.server_close()

    def test_people_preset_updates_virtual_state_without_touching_photo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, 'photos')
            root.mkdir()
            photo = root / 'a.jpg'
            photo.write_bytes(b'unchanged')
            state = Path(temporary, 'review')
            state.mkdir()
            (state / 'report.json').write_text(json.dumps({
                'source': str(root.resolve()), 'photos': [
                    {'id': 'asset-a', 'name': 'a.jpg', 'status': 'neither',
                     'immich': [], 'vision': []}], 'pairs': []}))
            batch = {'source': str(root.resolve()), 'items': [
                {'originalName': 'a.jpg', 'relativePath': 'a.jpg'}]}
            result = save_people_review_action(batch, state, {
                'action': 'create', 'name': '小林', 'assetIds': ['asset-a']})
            self.assertEqual(result['presets'][0]['name'], '小林')
            self.assertEqual(result['photos'][0]['relativePath'], 'a.jpg')
            self.assertEqual(photo.read_bytes(), b'unchanged')
            self.assertTrue(people_review_snapshot(batch, state)['available'])

    def test_people_review_http_requires_local_origin_and_token_for_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, 'photos')
            root.mkdir()
            photo = root / 'a.jpg'
            photo.write_bytes(b'unchanged')
            state = Path(temporary, 'review')
            state.mkdir()
            (state / 'report.json').write_text(json.dumps({
                'source': str(root.resolve()), 'photos': [
                    {'id': 'asset-a', 'name': 'a.jpg', 'status': 'neither',
                     'immich': [], 'vision': []}], 'pairs': []}))
            service = SimpleNamespace(
                origins={'http://127.0.0.1:2283'},
                batch={'batchId': 'batch-1', 'source': str(root.resolve()),
                       'items': [{'originalName': 'a.jpg', 'relativePath': 'a.jpg'}]},
                person_state=state, token='test-token', lock=threading.Lock(),
                ensure_state_available=lambda: None)
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/v1/people-review'
                with urlopen(Request(url, headers={'Origin': 'http://127.0.0.1:2283'})) as response:
                    self.assertTrue(json.load(response)['available'])
                payload = json.dumps({'batchId': 'batch-1', 'action': 'decision',
                                      'kind': 'photo', 'key': 'asset-a',
                                      'value': 'has-face'}).encode()
                unauthorized = Request(url, data=payload, headers={
                    'Origin': 'http://127.0.0.1:2283', 'Content-Type': 'application/json'})
                with self.assertRaises(HTTPError) as blocked:
                    urlopen(unauthorized)
                self.assertEqual(blocked.exception.code, 403)
                self.assertFalse((state / 'decisions.json').exists())
                authorized = Request(url, data=payload, headers={
                    'Origin': 'http://127.0.0.1:2283', 'Content-Type': 'application/json',
                    'X-Photo-Studio-Token': 'test-token'})
                with urlopen(authorized) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(json.loads((state / 'decisions.json').read_text())[
                    'photo:asset-a']['value'], 'has-face')
                self.assertEqual(photo.read_bytes(), b'unchanged')
            finally:
                server.shutdown()
                server.server_close()

    def test_vision_scan_endpoint_requires_batch_origin_and_token(self):
        calls = []
        service = SimpleNamespace(
            origins={'http://127.0.0.1:2283'}, token='test-token',
            batch={'batchId': 'batch-1'},
            vision={'running': False, 'status': 'idle'},
            ensure_state_available=lambda: None,
            start_vision_scan=None)

        def start_vision_scan():
            calls.append('started')
            service.vision = {'running': True, 'status': 'running', 'completed': 0,
                              'total': 1, 'errors': []}
            return service.vision

        service.start_vision_scan = start_vision_scan
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/v1/vision/analyze'
        origin = {'Origin': 'http://127.0.0.1:2283', 'Content-Type': 'application/json'}
        try:
            with self.assertRaises(HTTPError) as missing_token:
                urlopen(Request(url, data=json.dumps({'batchId': 'batch-1'}).encode(),
                                headers=origin), timeout=5)
            self.assertEqual(missing_token.exception.code, 403)
            headers = {**origin, 'X-Photo-Studio-Token': 'test-token'}
            with self.assertRaises(HTTPError) as stale_batch:
                urlopen(Request(url, data=json.dumps({'batchId': 'old-batch'}).encode(),
                                headers=headers), timeout=5)
            self.assertEqual(stale_batch.exception.code, 400)
            with urlopen(Request(url, data=json.dumps({'batchId': 'batch-1'}).encode(),
                                 headers=headers), timeout=5) as response:
                self.assertEqual(response.status, 202)
                self.assertTrue(json.load(response)['vision']['running'])
            self.assertEqual(calls, ['started'])
        finally:
            server.shutdown()
            server.server_close()

    def test_named_person_preset_is_used_only_when_unique_to_a_photo(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, 'photos')
            source.mkdir()
            for name in ('a.jpg', 'b.jpg'):
                (source / name).write_bytes(b'fixture')
            state = Path(temporary, 'people')
            state.mkdir()
            (state / 'report.json').write_text(json.dumps({
                'source': str(source.resolve()), 'photos': [
                    {'id': 'asset-a', 'name': 'a.jpg'},
                    {'id': 'asset-b', 'name': 'b.jpg'}]}))
            (state / 'presets.json').write_text(json.dumps([
                {'name': '小林', 'assetIds': ['asset-a', 'asset-b']},
                {'name': '小张', 'assetIds': ['asset-b']}]))
            with patch('folder_studio.scan_folder') as scan:
                from photo_workflow import scan_folder
                scan.return_value = scan_folder(source, lambda paths: {
                    str(path): {'EXIF:DateTimeOriginal': '2024:01:02 03:04:05'}
                    for path in paths})
                batch = build_batch(source, {}, expected_volume=None,
                                    person_state=state)
            by_name = {item['originalName']: item for item in batch['items']}
            self.assertEqual(by_name['a.jpg']['suggestedName'],
                             '小林_2024-01-02_03-04-05.jpg')
            self.assertEqual(by_name['b.jpg']['suggestedName'],
                             '2024-01-02_03-04-05.jpg')

    def test_batch_keeps_model_descriptions_out_of_disk_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            photo = source / 'IMG_1.jpg'
            photo.write_bytes(b'fixture')
            models = {'IMG_1.jpg': {
                'qwen': {'model': 'qwen3-vl:8b', 'short': '海边日落',
                         'long': '一人在海边看日落', 'category': '人物'},
                'afs': {'model': 'Gemma', 'short': 'beach', 'extended': '海边_人物'}}}
            with patch('folder_studio.scan_folder') as scan:
                from photo_workflow import scan_folder
                scan.return_value = scan_folder(source, lambda paths: {
                    str(photo.resolve()): {'EXIF:DateTimeOriginal': '2024:01:02 03:04:05'}})
                batch = build_batch(source, models, expected_volume=None)
            item = batch['items'][0]
            self.assertEqual(item['suggestedName'], '2024-01-02_03-04-05.jpg')
            self.assertEqual(item['models']['afs']['extended'], '海边_人物')
            self.assertEqual(item['models']['qwen']['long'], '一人在海边看日落')
            self.assertEqual(photo.read_bytes(), b'fixture')

    def test_no_rename_without_matching_batch_and_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            photo = source / 'a.jpg'
            photo.write_bytes(b'fixture')
            with patch('folder_studio.scan_folder') as scan:
                from photo_workflow import scan_folder
                scan.return_value = scan_folder(source, lambda paths: {})
                batch = build_batch(source, expected_volume=None, model_results={})
            rename = lambda old, new: old.rename(new)
            for batch_id, confirmation in [('wrong', '确认ExFAT原地改名'),
                                           (batch['batchId'], '')]:
                with self.assertRaises(ValueError):
                    approve_batch(batch, batch_id, {'a.jpg': 'b.jpg'}, confirmation,
                                  source=source, expected_volume=None, rename=rename)
            self.assertTrue(photo.exists())
            output = approve_batch(batch, batch['batchId'], {'a.jpg': 'b.jpg'},
                                   '确认ExFAT原地改名', source=source,
                                   expected_volume=None, rename=rename)
            self.assertEqual(output[0]['status'], 'renamed')
            self.assertEqual((source / 'b.jpg').read_bytes(), b'fixture')

    def test_missing_time_content_name_omits_time_placeholder(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / 'IMG_1.jpg').write_bytes(b'fixture')
            with patch('folder_studio.scan_folder') as scan:
                from photo_workflow import scan_folder
                scan.return_value = scan_folder(source, lambda paths: {})
                batch = build_batch(source, {'IMG_1.jpg': {'qwen': {
                    'short': '海边自拍', 'long': '海边一人'}}}, expected_volume=None)
            item = batch['items'][0]
            self.assertIsNone(item['suggestedName'])
            self.assertIsNone(item['contentOnlyName'])
            self.assertEqual(item['reason'], '没有可靠拍摄时间')
            self.assertIn('拍摄时间缺失', item['issue'])

    def test_vision_manifest_adds_virtual_group_without_changing_photo(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, 'photos')
            source.mkdir()
            photo = source / 'screen.png'
            photo.write_bytes(b'fixture')
            manifest = Path(temporary, 'vision.jsonl')
            import json
            manifest.write_text(json.dumps({
                'path': str(photo.resolve()), 'relative_path': 'screen.png',
                'filename': 'screen.png', 'extension': 'png', 'status': 'scanned',
                'file_size_bytes': photo.stat().st_size,
                'file_modified_unix': photo.stat().st_mtime,
                'vision_labels': [{'label': 'screenshot', 'confidence': 0.9}],
                'face_count': 0, 'human_count': 0, 'ocr_line_count': 1,
                'ocr_text_samples': ['消息'],
            }) + '\n')
            with patch('folder_studio.scan_folder') as scan:
                from photo_workflow import scan_folder
                scan.return_value = scan_folder(source, lambda paths: {})
                batch = build_batch(source, expected_volume=None, model_results={},
                                    vision_path=manifest)
            item = batch['items'][0]
            self.assertEqual(item['virtualGroup'], '截图／屏幕内容')
            self.assertEqual(item['vision']['ocrText'], ['消息'])
            self.assertEqual(photo.read_bytes(), b'fixture')

    def test_symlinked_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, 'photos')
            source.mkdir()
            link = Path(temporary, 'link')
            link.symlink_to(source)
            with self.assertRaises(ValueError):
                check_source(link, expected_volume=None)

    def test_immich_annotation_change_requires_attention(self):
        before = {'id': 'old', 'albums': ['album-1'], 'people': ['person-1'],
                  'description': 'favorite', 'tags': [], 'favorite': True}
        after = {**before, 'id': 'new', 'albums': []}
        changed = annotation_changes(before, after)
        self.assertEqual(set(changed), {'id', 'albums'})
        self.assertEqual(annotation_changes(before, before), {})

    def test_broken_log_stream_does_not_break_http_response(self):
        refreshed = []
        service = SimpleNamespace(
            origins={'http://127.0.0.1:2283'}, batch={'items': []}, token='test',
            source_capabilities=lambda: {'immichRename': True},
            analysis={}, codex={}, reconcile={}, duplicates={'status': 'idle'},
            refresh_batch_if_changed=lambda: refreshed.append(True))
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        class BrokenLog:
            def write(self, text):
                raise OSError('external log disappeared')

        try:
            request = Request(f'http://127.0.0.1:{server.server_port}/v1/batch',
                              headers={'Origin': 'http://127.0.0.1:2283'})
            with patch('sys.stderr', BrokenLog()), urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b'"items": []', response.read())
            self.assertEqual(refreshed, [True])
        finally:
            server.shutdown()
            server.server_close()

    def test_cached_batch_refreshes_changed_photo_version_before_group_display(self):
        from photo_workflow import scan_folder as scan_generated_folder

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, 'photos')
            root.mkdir()
            first, second = root / 'a.jpg', root / 'b.jpg'
            first.write_bytes(b'generated-photo-a')
            second.write_bytes(b'generated-photo-b')
            index = PhotoIndex(Path(temporary, 'index.sqlite3'))
            runtime = Path(temporary, 'runtime')
            service = object.__new__(Service)
            service.state = runtime
            service.person_state = Path(temporary, 'people')
            service.index = index
            service.lock = threading.Lock()

            with patch('folder_studio.scan_folder',
                       side_effect=lambda folder: scan_generated_folder(folder, lambda _: {})):
                service.batch = build_batch(root, expected_volume=None, index=index,
                                            person_state=service.person_state)
                old_batch_id = service.batch['batchId']
                self.assertEqual(service.refresh_batch_if_changed()['batchId'], old_batch_id)
                rows = {row['relativePath']: row for row in service.batch['items']}
                old_version = rows['a.jpg']['versionId']
                index.save_analysis(old_version, 'apple-vision', 'Apple Vision',
                                    category='人物', details={'reason': 'generated fixture'})
                index.save_duplicate_pair(service.batch['sourceId'], old_version,
                                          rows['b.jpg']['versionId'], 'visual', 0.9,
                                          {'distance': 2})
                self.assertEqual(len(Service.duplicate_groups(service, 'visual')), 1)

                first.write_bytes(b'generated-photo-a-replaced-with-new-version')
                refreshed = service.refresh_batch_if_changed()

            current = {row['relativePath']: row for row in refreshed['items']}
            self.assertNotEqual(refreshed['batchId'], old_batch_id)
            self.assertNotEqual(current['a.jpg']['versionId'], old_version)
            self.assertIsNone(current['a.jpg']['vision'])
            self.assertEqual(current['a.jpg']['virtualGroup'], '待确认／其他')
            self.assertEqual(Service.duplicate_groups(service, 'visual'), [])
            self.assertNotIn(old_version, [row['versionId'] for row in current.values()])

    def test_duplicate_group_endpoint_refreshes_inventory_before_returning_batch_id(self):
        refreshed = []

        class EmptyIndex:
            def list_duplicate_pairs(self, _source_id, kind=None):
                return []

        service = SimpleNamespace(
            origins={'http://127.0.0.1:2283'},
            batch={'batchId': 'old-batch', 'sourceId': 'source'},
            index=EmptyIndex(), duplicate_groups=lambda _kind: [],
            refresh_batch_if_changed=lambda: (
                refreshed.append(True), service.batch.update({'batchId': 'new-batch'})))
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f'http://127.0.0.1:{server.server_port}/v1/duplicate-groups?kind=visual',
                headers={'Origin': 'http://127.0.0.1:2283'})
            with urlopen(request, timeout=5) as response:
                result = json.load(response)
            self.assertEqual(result['batchId'], 'new-batch')
            self.assertEqual(refreshed, [True])
        finally:
            server.shutdown()
            server.server_close()

    def test_thumbnail_accepts_origin_only_referer_from_immich(self):
        from photo_workflow import _stat_signature
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            photo = source / 'a.jpg'
            photo.write_bytes(b'fixture')
            index = PhotoIndex(source / 'index.sqlite3')
            signature = _stat_signature(photo)
            indexed = index.reconcile_inventory('volume-A', source, [
                {'relativePath': 'a.jpg', 'signature': signature}])
            source_id = indexed['sourceId']
            version_id = indexed['assets']['a.jpg']['versionId']
            service = SimpleNamespace(
                origins={'http://127.0.0.1:2283'},
                batch={'source': str(source.resolve()), 'sourceId': source_id,
                       'volumeUuid': None, 'items': [
                    {'relativePath': 'a.jpg', 'versionId': version_id,
                     'signature': signature}]},
                state=source, thumbnail_limit=threading.BoundedSemaphore(1),
                index=index, ensure_state_available=lambda: None)
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            commands = []

            def make_preview(command, **kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b'fake-jpeg')

            try:
                url = f'http://127.0.0.1:{server.server_port}/v1/thumbnail/a.jpg'
                with patch('folder_studio.subprocess.run', side_effect=make_preview):
                    request = Request(url, headers={'Referer': 'http://127.0.0.1:2283/'})
                    try:
                        with urlopen(request, timeout=5) as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.read(), b'fake-jpeg')
                    except HTTPError as error:
                        self.fail(error.read().decode())
                    request = Request(url, headers={'Referer': 'http://other.example/'})
                    with self.assertRaises(HTTPError) as blocked:
                        urlopen(request, timeout=5)
                    self.assertEqual(blocked.exception.code, 403)
                    preview_url = f'http://127.0.0.1:{server.server_port}/v1/preview/a.jpg'
                    preview_request = Request(preview_url, headers={
                        'Referer': 'http://127.0.0.1:2283/'})
                    with urlopen(preview_request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), b'fake-jpeg')
                    self.assertIn('1600', commands[-1])
            finally:
                server.shutdown()
                server.server_close()

    def test_vision_manifest_import_is_bound_to_source_path_and_file_version(self):
        from photo_workflow import _stat_signature
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            source = temporary / 'photos'
            source.mkdir()
            photo = source / 'screen.png'
            photo.write_bytes(b'generated image fixture')
            state = temporary / 'runtime'
            state.mkdir()
            index = PhotoIndex(state / 'photo-index.sqlite3')
            signature = _stat_signature(photo)
            inventory = index.reconcile_inventory('volume-A', source, [
                {'relativePath': 'screen.png', 'signature': signature}])
            source_id = inventory['sourceId']
            asset = inventory['assets']['screen.png']
            service = object.__new__(Service)
            service.state = state
            service.index = index
            service.batch = {'sourceId': source_id}
            manifest = state / 'scan.jsonl'
            request = state / 'request.json'
            request.write_text(json.dumps({
                'sourceId': source_id, 'source': str(source.resolve()), 'volumeUuid': None,
                'snapshot': {'screen.png': {'versionId': asset['versionId'],
                                            'signature': signature}},
            }))
            manifest.write_text(json.dumps({
                'path': str(photo.resolve()), 'relative_path': 'screen.png',
                'filename': 'screen.png', 'extension': 'png', 'status': 'scanned',
                'file_size_bytes': signature['size'],
                'file_modified_unix': signature['modifiedNs'] / 1e9,
                'vision_labels': [{'label': 'screenshot', 'confidence': 0.9}],
                'face_count': 0, 'human_count': 0, 'ocr_line_count': 1,
                'ocr_text_samples': ['生成样例'],
            }) + '\n')
            current = {'screen.png': {'relativePath': 'screen.png',
                                      'versionId': asset['versionId'],
                                      'signature': signature}}
            imported, errors = service._import_vision_manifest(manifest, request, current)
            self.assertEqual(imported, {asset['versionId']})
            self.assertEqual(errors, [])
            saved = index.item_state([asset['versionId']])[asset['versionId']]['vision']
            self.assertEqual(saved['category'], '截图／屏幕内容')
            self.assertEqual(saved['reason'], '视觉识别为屏幕内容')

            service.batch = {'sourceId': 'different-source'}
            imported, _ = service._import_vision_manifest(manifest, request, current)
            self.assertEqual(imported, set())

            service.batch = {'sourceId': source_id}
            photo.write_bytes(b'changed generated fixture')
            imported, errors = service._import_vision_manifest(manifest, request, current)
            self.assertEqual(imported, set())
            self.assertTrue(any('发生变化' in row['error'] for row in errors))

    def test_duplicate_group_review_api_saves_atomically_and_rejects_stale_members(self):
        from photo_workflow import _stat_signature
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = []
            for name in ('a.jpg', 'b.jpg'):
                path = root / name
                path.write_bytes(f'generated {name}'.encode())
                photos.append({'relativePath': name, 'signature': _stat_signature(path)})
            index_path = root / 'index.sqlite3'
            index = PhotoIndex(index_path)
            indexed = index.reconcile_inventory('volume-A', root, photos)
            source_id = indexed['sourceId']
            service = object.__new__(Service)
            service.origins = {'http://127.0.0.1:2283'}
            service.token = 'test-token'
            service.lock = threading.Lock()
            service.index = index
            service.duplicates = {'running': False}
            service.ensure_state_available = lambda: None
            service.batch = {
                'batchId': 'batch-1', 'sourceId': source_id,
                'volumeUuid': None, 'source': str(root.resolve()),
                'items': [{
                    'relativePath': row['relativePath'],
                    'originalName': Path(row['relativePath']).name,
                    'signature': row['signature'],
                    'versionId': indexed['assets'][row['relativePath']]['versionId'],
                } for row in photos],
            }
            service.duplicate_groups = lambda kind: Service.duplicate_groups(service, kind)
            versions = [item['versionId'] for item in service.batch['items']]
            index.save_duplicate_pair(source_id, *versions, 'exact', 1.0, {'method': 'SHA-256'})
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f'http://127.0.0.1:{server.server_port}/v1/duplicate-groups/review'
            payload = {'batchId': 'batch-1', 'groups': [{
                'kind': 'exact', 'versionIds': versions, 'status': 'same',
                'keepVersionIds': [versions[0]],
            }]}
            headers = {'Origin': 'http://127.0.0.1:2283',
                       'Content-Type': 'application/json',
                       'X-Photo-Studio-Token': 'test-token'}
            try:
                with urlopen(Request(url, data=json.dumps(payload).encode(), headers=headers),
                             timeout=5) as response:
                    self.assertEqual(response.status, 200)
                reopened = PhotoIndex(index_path)
                saved = reopened.list_duplicate_group_reviews(source_id, 'exact')
                self.assertEqual(saved[0]['status'], 'same')
                self.assertEqual(saved[0]['keepVersionIds'], [versions[0]])

                changed_path = root / 'b.jpg'
                changed_path.write_bytes(b'new generated image version')
                new_inventory = reopened.reconcile_inventory('volume-A', root, [
                    {'relativePath': 'a.jpg', 'signature': _stat_signature(root / 'a.jpg')},
                    {'relativePath': 'b.jpg', 'signature': _stat_signature(changed_path)},
                ])
                service.batch['items'][1]['signature'] = _stat_signature(changed_path)
                service.batch['items'][1]['versionId'] = new_inventory['assets']['b.jpg']['versionId']
                with self.assertRaises(HTTPError) as stale:
                    urlopen(Request(url, data=json.dumps(payload).encode(), headers=headers),
                            timeout=5)
                self.assertEqual(stale.exception.code, 400)
                self.assertIn('已变化', stale.exception.read().decode())
            finally:
                server.shutdown()
                server.server_close()

    def test_visual_group_membership_change_leaves_old_review_for_history(self):
        from photo_workflow import _stat_signature
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = []
            for name in ('a.jpg', 'b.jpg', 'c.jpg'):
                path = root / name
                path.write_bytes(f'generated {name}'.encode())
                photos.append({'relativePath': name, 'signature': _stat_signature(path)})
            index = PhotoIndex(root / 'index.sqlite3')
            inventory = index.reconcile_inventory('volume-A', root, photos)
            source_id = inventory['sourceId']
            items = [{
                **row, 'originalName': Path(row['relativePath']).name,
                'versionId': inventory['assets'][row['relativePath']]['versionId'],
            } for row in photos]
            a, b, c = (inventory['assets'][name]['versionId']
                       for name in ('a.jpg', 'b.jpg', 'c.jpg'))
            index.save_duplicate_pair(source_id, a, b, 'visual', 0.9, {'method': 'haar'})
            index.save_duplicate_group_reviews(source_id, [{
                'kind': 'visual', 'versionIds': [a, b], 'status': 'same',
                'keepVersionIds': [a],
            }])
            service = object.__new__(Service)
            service.index = index
            service.batch = {'sourceId': source_id, 'items': items}
            before = service.duplicate_groups('visual')
            self.assertEqual(before[0]['review']['status'], 'same')

            index.save_duplicate_pair(source_id, b, c, 'visual', 0.8, {'method': 'haar'})
            after = service.duplicate_groups('visual')
            self.assertEqual(after[0]['versionIds'], sorted([a, b, c]))
            self.assertIsNone(after[0]['review'])
            history = index.list_duplicate_group_reviews(source_id, 'visual')
            self.assertEqual(history[0]['members'], sorted([a, b]))

    def test_vision_scan_resumes_interrupted_manifest_reports_failure_then_retries_it(self):
        from photo_workflow import _stat_signature
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            root = temporary / 'photos'
            root.mkdir()
            state = temporary / 'runtime'
            state.mkdir()
            source_rows = []
            for name in ('a.jpg', 'b.jpg'):
                photo = root / name
                photo.write_bytes(f'generated {name}'.encode())
                source_rows.append({'relativePath': name,
                                    'signature': _stat_signature(photo)})
            index_path = state / 'photo-index.sqlite3'
            index = PhotoIndex(index_path)
            inventory = index.reconcile_inventory('volume-A', root, source_rows)
            source_id = inventory['sourceId']
            items = [{
                'relativePath': row['relativePath'],
                'originalName': Path(row['relativePath']).name,
                'signature': row['signature'],
                'versionId': inventory['assets'][row['relativePath']]['versionId'],
            } for row in source_rows]
            batch = {'batchId': 'batch-1', 'sourceId': source_id,
                     'volumeUuid': None, 'source': str(root.resolve()), 'items': items}

            old_dir = state / 'vision' / source_id / 'interrupted-job'
            old_dir.mkdir(parents=True)
            old_manifest = old_dir / 'scan.jsonl'
            old_request = old_dir / 'request.json'
            snapshot = {item['relativePath']: {
                'versionId': item['versionId'], 'signature': item['signature']}
                for item in items}
            old_request.write_text(json.dumps({
                'sourceId': source_id, 'source': str(root.resolve()), 'volumeUuid': None,
                'snapshot': snapshot,
            }))
            old_rows = []
            for item in items:
                photo = root / item['relativePath']
                old_rows.append({
                    'path': str(photo.resolve()), 'relative_path': item['relativePath'],
                    'filename': item['originalName'], 'extension': 'jpg',
                    'status': 'scanned' if item['relativePath'] == 'a.jpg' else 'decode_error',
                    'error': 'generated corrupt-image fixture',
                    'file_size_bytes': item['signature']['size'],
                    'file_modified_unix': item['signature']['modifiedNs'] / 1e9,
                    'vision_labels': [], 'face_count': 0, 'human_count': 0,
                    'ocr_line_count': 0, 'ocr_text_samples': [],
                })
            old_manifest.write_text(''.join(json.dumps(row) + '\n' for row in old_rows))
            old_job_id = index.begin_job(source_id, 'vision', 'apple-vision', 2)
            index.progress_job(old_job_id, completed=1, errors=[], progress={
                'stage': 'vision', 'manifestPath': str(old_manifest),
                'requestPath': str(old_request), 'sourceId': source_id,
            })
            # Reopening the index simulates a service restart during the scan.
            index = PhotoIndex(index_path)
            self.assertEqual(index.job(old_job_id)['status'], 'interrupted')

            build = state / 'build'
            build.mkdir()
            binary = build / 'local_vision_scan'
            binary.write_text('test double')
            scanner = Path(__import__('folder_studio').__file__).with_name('local_vision_scan.swift')
            future = scanner.stat().st_mtime_ns + 1_000_000_000
            import os
            os.utime(binary, ns=(future, future))
            service = object.__new__(Service)
            service.state = state
            service.index = index
            service.batch = batch
            service.person_state = state / 'people'
            service.lock = threading.Lock()
            service.analysis = {'running': False}
            service.codex = {'running': False}
            service.duplicates = {'running': False}
            service.vision = {'running': False}
            service.ensure_state_available = lambda: state.mkdir(parents=True, exist_ok=True)
            scanned_paths = []

            def fake_process(status):
                def launch(command, **_kwargs):
                    manifest = Path(command[2])
                    paths = json.loads(Path(command[4]).read_text())
                    scanned_paths.append(paths)
                    item = next(row for row in items if row['relativePath'] == paths[0])
                    photo = root / item['relativePath']
                    row = {
                        'path': str(photo.resolve()), 'relative_path': item['relativePath'],
                        'filename': item['originalName'], 'extension': 'jpg',
                        'status': status, 'error': 'generated retry fixture' if status != 'scanned' else '',
                        'file_size_bytes': item['signature']['size'],
                        'file_modified_unix': item['signature']['modifiedNs'] / 1e9,
                        'vision_labels': [{'label': 'landscape', 'confidence': 0.9}],
                        'face_count': 0, 'human_count': 0, 'ocr_line_count': 0,
                        'ocr_text_samples': [],
                    }
                    manifest.write_text(json.dumps(row) + '\n')
                    return SimpleNamespace(
                        stderr=iter(['PROGRESS\t1\t1\t0\n']),
                        stdout=io.StringIO('Scanned 1 images'), wait=lambda: 0)
                return launch

            statuses = iter(['decode_error', 'scanned'])

            def launch_next(command, **kwargs):
                return fake_process(next(statuses))(command, **kwargs)

            def wait_for_worker():
                deadline = time.monotonic() + 5
                while service.vision['running'] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(service.vision['running'], 'Vision worker did not finish')

            with patch('folder_studio.build_batch', return_value=batch), \
                    patch('folder_studio.subprocess.Popen', side_effect=launch_next):
                service.start_vision_scan()
                wait_for_worker()
                first_job = index.latest_job(source_id, 'vision')
                self.assertEqual(first_job['status'], 'partial')
                self.assertEqual(first_job['progress'].get('scannerCompleted'), 1,
                                 first_job['progress'])
                self.assertTrue(any(row.get('relativePath') == 'b.jpg'
                                    for row in service.vision['errors']))
                self.assertEqual(scanned_paths[0], ['b.jpg'])

                service.start_vision_scan()
                wait_for_worker()
                second_job = index.latest_job(source_id, 'vision')
                self.assertEqual(second_job['status'], 'complete')
                self.assertEqual(scanned_paths[1], ['b.jpg'])
                states = index.item_state([item['versionId'] for item in items])
                self.assertTrue(all(states[item['versionId']]['vision']['status'] == 'complete'
                                    for item in items))

    def test_existing_model_results_load_four_distinct_name_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            qwen = directory / 'qwen.json'
            afs = directory / 'afs.json'
            extended = directory / 'extended.json'
            qwen.write_text('{"results":[{"file":"a.jpg","response":"画面描述：海边一人\\n建议文件名：海边人像\\n类别：人物"}]}')
            afs.write_text('{"entries":[{"fileName":"a.jpg","suggestedName":"beach_portrait.jpg"}]}')
            extended.write_text('{"entries":[{"fileName":"a.jpg","suggestedName":"海边_人像.jpg"}]}')
            result = load_model_results(qwen, afs, extended)['a.jpg']
            self.assertEqual(result['qwen']['short'], '海边人像')
            self.assertEqual(result['qwen']['long'], '海边一人')
            self.assertEqual(result['afs']['short'], 'beach_portrait')
            self.assertEqual(result['afs']['extended'], '海边_人像')


if __name__ == '__main__':
    unittest.main()
