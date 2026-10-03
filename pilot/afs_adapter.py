"""Narrow AI File Sorter 1.9.2 review-only adapter.

Only ``categorize --review-only`` is supported here. Apply/move operations are
not exposed. A source-write sandbox and before/after stat check are additional
defences; missing zero-mutation counters fail closed.
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from photo_workflow import _stat_signature


DEFAULT_APP = Path('/Volumes/AI File Sorter for Mac M1/AI File Sorter for Mac M1.app/Contents/MacOS/AIFileSorter')
DEFAULT_LOCAL_GEMMA_STORAGE = Path('/Volumes/T7_1T/local-photo-studio/aifs-ab/models')
_AFS_FORBIDDEN_LABEL_CHARS = re.compile(r'[<>:"/\\|?*／＼]')
_AFS_PROJECT_CATEGORY_ALIASES = {
    '截图／屏幕内容': 'Screenshots_and_screen_content',
    '真人照片': 'People_photos',
    '宠物与其他动物': 'Pets_and_other_animals',
    '风景与建筑': 'Landscapes_and_architecture',
    '食物与物品': 'Food_and_objects',
    '动漫与插画': 'Anime_and_illustrations',
    '海报与表情包': 'Posters_and_memes',
    '文档与票据': 'Documents_and_receipts',
    '待确认／其他': 'Review_and_other',
}


def afs_category_aliases(categories):
    """Return ASCII AFS labels mapped to the project's labels.

    AFS 1.9.2 checks UTF-8 labels byte-by-byte with ``std::iscntrl``. Chinese
    characters can therefore be rejected as control bytes; use stable ASCII
    labels only for this external call and restore the project label after it.
    """
    aliases = {}
    for category in categories:
        project_label = str(category).strip()
        alias = _AFS_PROJECT_CATEGORY_ALIASES.get(project_label)
        if alias is None and project_label.isascii():
            alias = _AFS_FORBIDDEN_LABEL_CHARS.sub('_and_', project_label).strip('_')
        if alias is None:
            raise ValueError(
                f'AI File Sorter needs an explicit ASCII alias for project category: {project_label!r}')
        if (not project_label or not alias or len(alias.encode('utf-8')) > 80 or
                any(ord(char) < 32 for char in alias)):
            raise ValueError(f'project category cannot be represented safely for AI File Sorter: {project_label!r}')
        previous = aliases.get(alias)
        if previous is not None and previous != project_label:
            raise ValueError(f'project categories collide after AI File Sorter label normalization: {alias!r}')
        aliases[alias] = project_label
    return aliases


def ensure_local_gemma_configuration(config_root, model_storage):
    """Select Gemma only in this integration's isolated AFS config directory."""
    app_config = Path(config_root) / 'AIFileSorter'
    app_config.mkdir(parents=True, exist_ok=True)
    config_path = app_config / 'config.ini'
    required = {
        'LLMChoice': 'Local_4b_Gemma',
        'LlmStorageDir': str(Path(model_storage).resolve()),
        'VisualModelId': 'gemma-3-4b-it',
    }

    values = {}
    if config_path.exists():
        section = ''
        for line in config_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                section = stripped[1:-1]
            elif section == 'Settings' and '=' in stripped:
                key, value = stripped.split('=', 1)
                values[key.strip()] = value.strip()
        for key, value in required.items():
            existing = values.get(key)
            if existing is not None and existing != value:
                raise ValueError(f'AI File Sorter integration config conflicts with local Gemma setting: {key}')

    missing = {key: value for key, value in required.items() if key not in values}
    if missing:
        with config_path.open('a' if config_path.exists() else 'w') as stream:
            if config_path.exists() and config_path.stat().st_size:
                stream.write('\n')
            stream.write('[Settings]\n')
            for key, value in missing.items():
                stream.write(f'{key} = {value}\n')
    return config_path


def parse_review(status, review, requested_path, *, category_aliases=None):
    if status.get('operation') != 'categorize':
        raise ValueError('AI File Sorter did not report categorize review mode')
    apply = status.get('apply') or status
    if apply.get('movedCount') != 0 or apply.get('renamedCount') != 0:
        raise ValueError('AI File Sorter reported a file move or rename')
    if status.get('reviewRequired') is not True:
        raise ValueError('AI File Sorter did not keep the result in review-only state')
    if review.get('kind') != 'aifs.headlessReviewPlan':
        raise ValueError('AI File Sorter review file does not match the documented review-plan contract')
    entries = review.get('entries') or (review.get('review') or {}).get('entries') or []
    requested = Path(requested_path).resolve()

    def matches_requested_photo(entry):
        entry_path = entry.get('filePath')
        if not entry_path:
            return False
        resolved_entry_path = Path(entry_path).resolve()
        if resolved_entry_path == requested:
            return True
        # AFS 1.9.2 review plans store the containing directory in filePath
        # and the basename separately in fileName.
        return (resolved_entry_path == requested.parent and
                entry.get('fileName') == requested.name)

    matches = [entry for entry in entries if matches_requested_photo(entry)]
    if len(matches) != 1:
        raise ValueError('AI File Sorter review result did not uniquely match the selected photo')
    entry = matches[0]
    category = (entry.get('canonicalCategory') or entry.get('category') or '').strip()
    subcategory = (entry.get('canonicalSubcategory') or entry.get('subcategory') or '').strip()
    if not category:
        raise ValueError('AI File Sorter review result contains no category')
    if category_aliases is not None:
        if category not in category_aliases:
            raise ValueError('AI File Sorter returned a category outside the approved project categories')
        category = category_aliases[category]
    return {'category': category, 'subcategory': subcategory or None,
            'short': None, 'long': entry.get('learningContext') or None,
            'model': 'AI File Sorter 1.9.2 / Gemma 3 4B',
            'details': {'fromCache': bool(entry.get('fromCache')),
                        'usedConsistencyHints': bool(entry.get('usedConsistencyHints')),
                        'learningContext': entry.get('learningContext') or None,
                        'suggestedNameIgnored': bool(entry.get('suggestedName')),
                        'reviewOnly': True}}


def _sandbox_path(path):
    # Seatbelt string literals use quoted paths. Reject control characters so
    # a selected filename cannot alter the generated profile syntax.
    value = str(Path(path).resolve())
    if any(ord(char) < 32 for char in value):
        raise ValueError('unsupported control character in path')
    return value.replace('\\', '\\\\').replace('"', '\\"')


def run_review(image, root, categories, *, state, app=DEFAULT_APP, runner=subprocess.run):
    root, image, state, app = map(Path, (root, image, state, app))
    app = Path(os.environ.get('PHOTO_STUDIO_AFS_EXECUTABLE', str(app)))
    root = root.resolve()
    if image.is_symlink() or not image.resolve().is_relative_to(root):
        raise ValueError('selected image is outside the approved source folder')
    if not image.is_file() or not app.is_file():
        raise FileNotFoundError('AI File Sorter app or selected image is unavailable')
    if not os.path.ismount('/Volumes/T7_1T'):
        raise ValueError('AI File Sorter state volume is not mounted')
    if not state.resolve().is_relative_to(Path('/Volumes/T7_1T')):
        raise ValueError('AI File Sorter state must stay on T7_1T')
    volume = subprocess.run(['diskutil', 'info', '-plist', '/Volumes/T7_1T'],
                            capture_output=True, check=True, timeout=15)
    import plistlib
    info = plistlib.loads(volume.stdout)
    if (info.get('VolumeUUID') != 'E70D0032-CFF1-499D-81A2-DA2A661FCAD5' or
            info.get('FilesystemType') != 'apfs'):
        raise ValueError('AI File Sorter state volume identity or format changed')
    before = _stat_signature(image)

    with tempfile.TemporaryDirectory(prefix='afs-review-', dir=state) as temp:
        temp = Path(temp)
        config_dir = state / 'afs-integration' / 'config'
        config_dir.mkdir(parents=True, exist_ok=True)
        model_storage = Path(os.environ.get('AI_FILE_SORTER_LLM_STORAGE_DIR',
                                            str(DEFAULT_LOCAL_GEMMA_STORAGE)))
        if not (model_storage / 'gemma-3-4b-it' / 'model.gguf').is_file():
            raise FileNotFoundError('AI File Sorter local Gemma model is unavailable on T7_1T')
        if not (model_storage / 'gemma-3-4b-it' / 'mmproj.gguf').is_file():
            raise FileNotFoundError('AI File Sorter local Gemma vision projector is unavailable on T7_1T')
        ensure_local_gemma_configuration(config_dir, model_storage)
        settings_path = temp / 'settings.json'
        status_path = temp / 'status.json'
        review_path = temp / 'review.json'
        category_aliases = afs_category_aliases(categories)
        settings_path.write_text(json.dumps({
            'useWhitelist': True,
            'allowedCategories': list(category_aliases),
            'categorizeFiles': True,
            'categorizeDirectories': False,
            'includeSubdirectories': False,
            'analyzeImagesByContent': True,
            'processImagesOnly': True,
            'offerRenameImages': False,
            'renameImagesOnly': False,
            'addImageDateToCategory': False,
            'addImageDatePlaceToFilename': False,
            'language': 'Chinese Simplified',
            'categoryLanguage': 'Simplified Chinese',
        }, ensure_ascii=False))
        source_path = _sandbox_path(root)
        profile = ('(version 1) (allow default) (deny network*) '
                   f'(deny file-write* (subpath "{source_path}"))')
        command = ['sandbox-exec', '-p', profile, str(app), '--headless',
                   '--operation', 'categorize', '--review-only',
                   '--path', str(image.resolve()), '--no-include-subdirectories',
                   '--status-file', str(status_path), '--review-file', str(review_path),
                   '--settings-overrides-file', str(settings_path)]
        env = dict(os.environ,
                   AI_FILE_SORTER_CONFIG_DIR=str(config_dir),
                   AI_FILE_SORTER_LLM_STORAGE_DIR=str(model_storage),
                   QT_QPA_PLATFORM='offscreen')
        runner(command, env=env, capture_output=True, text=True, check=True, timeout=900)
        if _stat_signature(image) != before:
            raise ValueError('source photo changed during AI File Sorter review-only analysis')
        if not status_path.is_file() or not review_path.is_file():
            raise ValueError('AI File Sorter did not write its review/status records')
        status = json.loads(status_path.read_text())
        review = json.loads(review_path.read_text())
        return parse_review(status, review, image, category_aliases=category_aliases)
