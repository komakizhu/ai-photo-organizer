"""Generated-data tests for stable identity and review state."""

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from photo_index import PhotoIndex, exact_duplicate_groups, hash_file


def signature(size, modified=10, inode=1):
    return {'device': 2, 'inode': inode, 'size': size, 'modifiedNs': modified}


class PhotoIndexTests(unittest.TestCase):
    def test_path_scoped_ids_survive_rescan_and_do_not_collide_by_basename(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp, 'index.sqlite3')
            root = Path(temp, 'source')
            root.mkdir()
            index = PhotoIndex(db_path)
            first = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'one/photo.jpg', 'signature': signature(10)},
                {'relativePath': 'two/photo.jpg', 'signature': signature(10, inode=2)},
            ])
            second = PhotoIndex(db_path).reconcile_inventory('volume-A', root, [
                {'relativePath': 'one/photo.jpg', 'signature': signature(10)},
                {'relativePath': 'two/photo.jpg', 'signature': signature(10, inode=2)},
            ])
            ids = first['assets']
            self.assertEqual(ids, second['assets'])
            self.assertNotEqual(ids['one/photo.jpg']['assetId'], ids['two/photo.jpg']['assetId'])
            self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)

    def test_user_approved_rename_keeps_asset_and_version_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = PhotoIndex(root / 'index.sqlite3')
            initial = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'a.jpg', 'signature': signature(10)}])
            ids = initial['assets']['a.jpg']
            index.record_approved_rename(ids['assetId'], 'a.jpg', 'b.jpg',
                                         details={'batchId': 'approved-1'})
            next_scan = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'b.jpg', 'signature': signature(10)}])
            self.assertEqual(ids, next_scan['assets']['b.jpg'])

    def test_external_rename_is_linked_only_by_a_unique_content_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = PhotoIndex(root / 'index.sqlite3')
            digest = hashlib.sha256(b'generated-fixture').hexdigest()
            first = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'old.jpg', 'signature': signature(18), 'sha256': digest}])
            second = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'new.jpg', 'signature': signature(18, modified=11), 'sha256': digest}])
            self.assertEqual(first['assets']['old.jpg']['assetId'],
                             second['assets']['new.jpg']['assetId'])
            self.assertEqual(second['unresolved'], [])

    def test_ambiguous_hash_never_guesses_which_duplicate_was_renamed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = PhotoIndex(root / 'index.sqlite3')
            digest = hashlib.sha256(b'same-generated-fixture').hexdigest()
            first = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'a.jpg', 'signature': signature(22), 'sha256': digest},
                {'relativePath': 'b.jpg', 'signature': signature(22, inode=2), 'sha256': digest},
            ])
            second = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'c.jpg', 'signature': signature(22, modified=11), 'sha256': digest},
            ])
            self.assertNotEqual(first['assets']['a.jpg']['assetId'],
                                second['assets']['c.jpg']['assetId'])
            self.assertEqual(len(second['unresolved']), 1)
            self.assertEqual(len(second['unresolved'][0]['candidateAssetIds']), 2)

    def test_models_and_human_category_decision_are_version_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = PhotoIndex(root / 'index.sqlite3')
            asset = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'x.jpg', 'signature': signature(8)}])['assets']['x.jpg']
            index.save_analysis(asset['versionId'], 'afs-gemma', 'Gemma 3 4B',
                                category='截图', short_text='聊天截图',
                                details={'reviewOnly': True})
            index.save_analysis(asset['versionId'], 'ollama-qwen', 'qwen3-vl:8b',
                                category='文档', long_text='聊天应用截图')
            index.set_presets([{'name': '截图', 'description': '屏幕截取'}])
            index.review_category(asset['versionId'], '聊天记录', note='用户纠正')
            self.assertEqual(index.get_presets()[0]['description'], '屏幕截取')
            with index._connect() as db:
                rows = db.execute('SELECT engine, category FROM analyses ORDER BY engine').fetchall()
                decision = db.execute('SELECT value_json, actor FROM decisions').fetchone()
            self.assertEqual([(row['engine'], row['category']) for row in rows],
                             [('afs-gemma', '截图'), ('ollama-qwen', '文档')])
            self.assertEqual(decision['actor'], 'human')
            self.assertIn('聊天记录', decision['value_json'])

    def test_capture_and_file_time_provenance_is_persisted_with_the_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = PhotoIndex(root / 'index.sqlite3')
            item = {'relativePath': 'x.jpg', 'signature': signature(8),
                    'captureTime': {'value': None, 'source': None,
                                    'uncertain': True, 'issue': 'missing capture time'},
                    'fileTimes': {'created': '2024-01-01T10:00:00+08:00',
                                  'modified': '2024-01-02T10:00:00+08:00'}}
            asset = index.reconcile_inventory('volume-A', root, [item])['assets']['x.jpg']
            state = index.item_state([asset['versionId']])[asset['versionId']]
            self.assertIsNone(state['timeInfo']['captureTime']['value'])
            self.assertEqual(state['timeInfo']['fileTimes']['created'],
                             '2024-01-01T10:00:00+08:00')
            item['fileTimes']['modified'] = '2024-01-03T10:00:00+08:00'
            index.reconcile_inventory('volume-A', root, [item])
            state = index.item_state([asset['versionId']])[asset['versionId']]
            self.assertEqual(state['timeInfo']['fileTimes']['modified'],
                             '2024-01-03T10:00:00+08:00')

    def test_stale_photo_version_rejects_new_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = PhotoIndex(root / 'index.sqlite3')
            first = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'x.jpg', 'signature': signature(8)}])['assets']['x.jpg']
            second = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'x.jpg', 'signature': signature(9, modified=12)}])['assets']['x.jpg']
            self.assertNotEqual(first['versionId'], second['versionId'])
            with self.assertRaises(ValueError):
                index.save_analysis(first['versionId'], 'ollama-qwen', 'qwen3-vl:8b')

    def test_exact_duplicate_groups_are_separate_from_visual_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            a, b, c = (Path(temp, name) for name in ('a.jpg', 'b.jpg', 'c.jpg'))
            a.write_bytes(b'identical fixture')
            b.write_bytes(b'identical fixture')
            c.write_bytes(b'different fixture')
            rows = [{'path': str(path), 'sha256': hash_file(path)} for path in (a, b, c)]
            groups = exact_duplicate_groups(rows)
            self.assertEqual([[Path(row['path']).name for row in group] for group in groups],
                             [['a.jpg', 'b.jpg']])

    def test_false_positive_duplicate_decision_survives_rescoring(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = PhotoIndex(root / 'index.sqlite3')
            assets = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'a.jpg', 'signature': signature(1)},
                {'relativePath': 'b.jpg', 'signature': signature(1, inode=2)},
            ])['assets']
            a, b = (assets[path]['versionId'] for path in ('a.jpg', 'b.jpg'))
            source_id = index.source_id('volume-A', root)
            index.save_duplicate_pair(source_id, a, b, 'visual', 0.7, {'method': 'haar'})
            index.decide_duplicate(a, b, 'visual', 'not_duplicate')
            index.save_duplicate_pair(source_id, a, b, 'visual', 0.8, {'method': 'haar'})
            pairs = index.list_duplicate_pairs(source_id, 'visual')
            self.assertEqual(pairs[0]['decision'], 'not_duplicate')
            self.assertEqual(pairs[0]['score'], 0.8)

    def test_confirmed_pair_decision_survives_rescoring(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = PhotoIndex(root / 'index.sqlite3')
            assets = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'a.jpg', 'signature': signature(1)},
                {'relativePath': 'b.jpg', 'signature': signature(1, inode=2)},
            ])['assets']
            a, b = (assets[path]['versionId'] for path in ('a.jpg', 'b.jpg'))
            source_id = index.source_id('volume-A', root)
            index.save_duplicate_pair(source_id, a, b, 'visual', 0.7, {'method': 'haar'})
            index.decide_duplicate(a, b, 'visual', 'confirmed')
            index.save_duplicate_pair(source_id, a, b, 'visual', 0.9, {'method': 'haar'})
            pairs = index.list_duplicate_pairs(source_id, 'visual')
            self.assertEqual(pairs[0]['decision'], 'confirmed')
            self.assertEqual(pairs[0]['score'], 0.9)

    def test_duplicate_group_review_round_trips_and_new_membership_stays_unreviewed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = PhotoIndex(root / 'index.sqlite3')
            assets = index.reconcile_inventory('volume-A', root, [
                {'relativePath': name, 'signature': signature(1, inode=number)}
                for number, name in enumerate(('a.jpg', 'b.jpg', 'c.jpg'), 1)
            ])['assets']
            source_id = index.source_id('volume-A', root)
            a, b, c = (assets[name]['versionId'] for name in ('a.jpg', 'b.jpg', 'c.jpg'))
            original = {'kind': 'visual', 'versionIds': [b, a], 'status': 'same',
                        'keepVersionIds': [b]}
            index.save_duplicate_group_reviews(source_id, [original])
            reopened = PhotoIndex(root / 'index.sqlite3')
            rows = reopened.list_duplicate_group_reviews(source_id, 'visual')
            self.assertEqual(rows[0]['members'], sorted([a, b]))
            self.assertEqual(rows[0]['keepVersionIds'], [b])
            self.assertEqual(rows[0]['status'], 'same')
            self.assertNotIn(sorted([a, c]), [row['members'] for row in rows])

    def test_vision_result_save_requires_current_path_version_and_signature(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = PhotoIndex(root / 'index.sqlite3')
            original = signature(20, modified=10)
            asset = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'a.png', 'signature': original}])['assets']['a.png']
            source_id = index.source_id('volume-A', root)
            self.assertTrue(index.save_analysis_if_current(
                source_id, 'a.png', asset['versionId'], original,
                'apple-vision', 'Apple Vision', category='截图／屏幕内容',
                details={'reason': 'visual cue'}))
            changed = signature(22, modified=11)
            next_asset = index.reconcile_inventory('volume-A', root, [
                {'relativePath': 'a.png', 'signature': changed}])['assets']['a.png']
            self.assertNotEqual(asset['versionId'], next_asset['versionId'])
            self.assertFalse(index.save_analysis_if_current(
                source_id, 'a.png', asset['versionId'], original,
                'apple-vision', 'Apple Vision', category='wrong version'))
            self.assertIsNone(index.item_state([next_asset['versionId']])
                              [next_asset['versionId']]['vision'])

    def test_job_interruption_is_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = PhotoIndex(root / 'index.sqlite3')
            source_id = index.source_id('volume-A', root)
            job_id = index.begin_job(source_id, 'analysis', 'ollama-qwen', 3)
            index.progress_job(job_id, completed=1,
                               errors=[{'reason': 'external volume disconnected'}],
                               progress={'engines': {'qwen': {'completed': 1, 'total': 3}}})
            resumed = PhotoIndex(root / 'index.sqlite3')
            job = resumed.job(job_id)
            self.assertEqual(job['status'], 'interrupted')
            self.assertEqual(job['completed'], 1)
            self.assertIn('external volume disconnected', job['errors'][0]['reason'])
            self.assertEqual(job['progress']['engines']['qwen']['completed'], 1)

    def test_existing_index_is_backed_up_before_schema_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, 'index.sqlite3')
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE legacy_marker (value TEXT)')
                db.execute("INSERT INTO legacy_marker VALUES ('preserve-me')")
                db.execute('PRAGMA user_version=0')
            PhotoIndex(path)
            backups = list(Path(temp).glob('index.pre-v0-*.sqlite3'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(backups[0]) as db:
                self.assertEqual(db.execute('SELECT value FROM legacy_marker').fetchone()[0],
                                 'preserve-me')

    def test_v1_index_is_backed_up_and_migrated_with_time_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, 'index.sqlite3')
            PhotoIndex(path)
            with sqlite3.connect(path) as db:
                db.execute('ALTER TABLE versions DROP COLUMN time_info_json')
                db.execute('PRAGMA user_version=1')
            migrated = PhotoIndex(path)
            backups = list(Path(temp).glob('index.pre-v1-*.sqlite3'))
            self.assertEqual(len(backups), 1)
            with migrated._connect() as db:
                columns = {row[1] for row in db.execute('PRAGMA table_info(versions)')}
            self.assertIn('time_info_json', columns)


if __name__ == '__main__':
    unittest.main()
