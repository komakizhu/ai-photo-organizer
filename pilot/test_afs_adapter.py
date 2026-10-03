"""Contract tests for AFS review-only parsing; no app or photos are opened."""

import tempfile
import unittest
from pathlib import Path

from afs_adapter import (afs_category_aliases, ensure_local_gemma_configuration,
                         parse_review)


class AFSAdapterTests(unittest.TestCase):
    def test_isolated_configuration_selects_local_gemma_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_storage = root / 'models'
            config_path = ensure_local_gemma_configuration(root / 'config', model_storage)
            before = config_path.read_text()
            self.assertIn('LLMChoice = Local_4b_Gemma', before)
            self.assertIn(f'LlmStorageDir = {model_storage.resolve()}', before)
            self.assertIn('VisualModelId = gemma-3-4b-it', before)
            ensure_local_gemma_configuration(root / 'config', model_storage)
            self.assertEqual(config_path.read_text(), before)

    def test_isolated_configuration_rejects_a_remote_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'config' / 'AIFileSorter'
            root.mkdir(parents=True)
            (root / 'config.ini').write_text('[Settings]\nLLMChoice = Remote_OpenAI\n')
            with self.assertRaisesRegex(ValueError, 'conflicts with local Gemma'):
                ensure_local_gemma_configuration(root.parent, Path(temporary) / 'models')

    def test_isolated_configuration_rejects_a_different_local_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'config' / 'AIFileSorter'
            root.mkdir(parents=True)
            (root / 'config.ini').write_text('[Settings]\nLLMChoice = Local_3b\n')
            with self.assertRaisesRegex(ValueError, 'conflicts with local Gemma'):
                ensure_local_gemma_configuration(root.parent, Path(temporary) / 'models')

    def test_accepts_one_whitelisted_category_and_zero_mutations(self):
        path = Path('/tmp/generated-fixture.jpg')
        result = parse_review({
            'operation': 'categorize', 'status': 'review_required', 'reviewRequired': True,
            'movedCount': 0, 'renamedCount': 0, 'entryCount': 1,
        }, {'kind': 'aifs.headlessReviewPlan', 'entries': [{
            'filePath': str(path), 'category': '截图', 'subcategory': '聊天',
            'learningContext': '画面包含聊天界面的文字。',
            'suggestedName': 'ignored.jpg',
        }]}, path)
        self.assertEqual(result['category'], '截图')
        self.assertEqual(result['subcategory'], '聊天')
        self.assertTrue(result['details']['reviewOnly'])
        self.assertTrue(result['details']['suggestedNameIgnored'])

    def test_project_categories_are_safely_aliased_and_restored(self):
        aliases = afs_category_aliases(['截图／屏幕内容', '待确认／其他', '真人照片'])
        self.assertEqual(aliases, {
            'Screenshots_and_screen_content': '截图／屏幕内容',
            'Review_and_other': '待确认／其他',
            'People_photos': '真人照片',
        })
        path = Path('/tmp/generated-fixture.jpg')
        result = parse_review({
            'operation': 'categorize', 'movedCount': 0, 'renamedCount': 0,
            'reviewRequired': True,
        }, {'kind': 'aifs.headlessReviewPlan', 'entries': [{
            'filePath': str(path), 'category': 'Screenshots_and_screen_content',
            'subcategory': 'Screen_content',
        }]}, path, category_aliases=aliases)
        self.assertEqual(result['category'], '截图／屏幕内容')

    def test_project_categories_reject_alias_collisions_and_unknown_results(self):
        with self.assertRaisesRegex(ValueError, 'collide'):
            afs_category_aliases(['scenery/architecture', 'scenery|architecture'])
        with self.assertRaisesRegex(ValueError, 'explicit ASCII alias'):
            afs_category_aliases(['自定义类别'])
        aliases = afs_category_aliases(['真人照片'])
        path = Path('/tmp/generated-fixture.jpg')
        with self.assertRaisesRegex(ValueError, 'outside the approved'):
            parse_review({
                'operation': 'categorize', 'movedCount': 0, 'renamedCount': 0,
                'reviewRequired': True,
            }, {'kind': 'aifs.headlessReviewPlan', 'entries': [{
                'filePath': str(path), 'category': 'People', 'subcategory': 'Portrait',
            }]}, path, category_aliases=aliases)

    def test_review_plan_directory_and_filename_identify_requested_photo(self):
        path = Path('/tmp/fixture-folder/generated-fixture.jpg')
        result = parse_review({
            'operation': 'categorize', 'movedCount': 0, 'renamedCount': 0,
            'reviewRequired': True,
        }, {'kind': 'aifs.headlessReviewPlan', 'entries': [{
            'filePath': str(path.parent), 'fileName': path.name,
            'category': 'People_photos', 'subcategory': 'Portrait',
        }]}, path, category_aliases={'People_photos': '真人照片'})
        self.assertEqual(result['category'], '真人照片')

    def test_review_plan_does_not_match_same_filename_in_another_directory(self):
        path = Path('/tmp/fixture-folder/generated-fixture.jpg')
        with self.assertRaisesRegex(ValueError, 'uniquely match'):
            parse_review({
                'operation': 'categorize', 'movedCount': 0, 'renamedCount': 0,
                'reviewRequired': True,
            }, {'kind': 'aifs.headlessReviewPlan', 'entries': [{
                'filePath': '/tmp/other-folder', 'fileName': path.name,
                'category': 'People_photos', 'subcategory': 'Portrait',
            }]}, path, category_aliases={'People_photos': '真人照片'})

    def test_rejects_move_or_rename_counts(self):
        path = Path('/tmp/generated-fixture.jpg')
        for status in ({'operation': 'categorize', 'movedCount': 1, 'renamedCount': 0},
                       {'operation': 'categorize', 'movedCount': 0, 'renamedCount': 1}):
            with self.assertRaisesRegex(ValueError, 'move or rename'):
                parse_review(status, {'entries': [{'filePath': str(path), 'category': '截图'}]}, path)

    def test_rejects_missing_mutation_counters_and_wrong_photo(self):
        path = Path('/tmp/generated-fixture.jpg')
        with self.assertRaisesRegex(ValueError, 'move or rename'):
            parse_review({'operation': 'categorize'}, {'entries': []}, path)
        with self.assertRaisesRegex(ValueError, 'uniquely match'):
            parse_review({'operation': 'categorize', 'movedCount': 0, 'renamedCount': 0,
                          'reviewRequired': True},
                         {'kind': 'aifs.headlessReviewPlan',
                          'entries': [{'filePath': '/tmp/other.jpg', 'category': '人物'}]}, path)

    def test_rejects_wrong_operation_and_empty_category(self):
        path = Path('/tmp/generated-fixture.jpg')
        with self.assertRaisesRegex(ValueError, 'categorize'):
            parse_review({'operation': 'rename', 'movedCount': 0, 'renamedCount': 0},
                         {'entries': []}, path)
        with self.assertRaisesRegex(ValueError, 'no category'):
            parse_review({'operation': 'categorize', 'movedCount': 0, 'renamedCount': 0,
                          'reviewRequired': True},
                         {'kind': 'aifs.headlessReviewPlan',
                          'entries': [{'filePath': str(path)}]}, path)


if __name__ == '__main__':
    unittest.main()
