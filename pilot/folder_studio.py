"""Review bridge for one explicitly selected ordinary photo folder.

Scanning, thumbnails and model inference never alter source files. Optional
Codex analysis sends only confirmed temporary previews off-device. The sole
source-file write path is an explicit approval passed to apply_approved.
"""

import argparse
import base64
import json
import os
import plistlib
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from photo_workflow import _clean_label, apply_approved, plan_names, scan_folder
from photo_index import DEFAULT_INDEX, PhotoIndex
from duplicate_detection import analyze_duplicates, duplicate_candidate_groups
from afs_adapter import run_review as afs_categorize_review
from vision_groups import load_vision_results
from vision_groups import GROUPS, classify_scan


SOURCE = Path('/Volumes/PhotoSource/Photos')
STATE = Path('/Volumes/T7_1T/local-photo-studio/photo-studio-runtime')
LEGACY_STATE = Path('/Volumes/T7_1T/local-photo-studio/folder-studio')
AB = Path('/Volumes/T7_1T/local-photo-studio/aifs-ab')
QWEN_RESULTS = AB / 'qwen3-vl-results.json'
AFS_RESULTS = AB / 'aifs-gemma-review.json'
AFS_EXTENDED = AB / 'aifs-gemma-long-review.json'
QWEN_MORE = STATE / 'qwen-results.json'
AFS_MORE = STATE / 'afs-missing-review.json'
AFS_TRANSLATIONS = STATE / 'afs-short-zh.json'
EXPECTED_VOLUME = 'B671ED4A-D881-3718-86FF-4BEF8BA75590'
EXPECTED_STATE_VOLUME = 'E70D0032-CFF1-499D-81A2-DA2A661FCAD5'
INDEX_PATH = DEFAULT_INDEX
PERSON_REVIEW_STATE = Path('/Volumes/T7_1T/local-photo-studio/immich-sample/dual-review')
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.tif', '.tiff', '.webp'}
CODEX_RESULTS = STATE / 'codex-review.json'
VISION_RESULTS = STATE / 'vision-results.jsonl'
CODEX_MODES = {'all', 'fallback'}
PROMPT = ('只根据画面可见内容，用简体中文，回答画面描述、建议备注、类别、截图判断、'
          '可见人数和不确定信息。不要猜姓名、具体地点或身份；类别必须从给出的预设中选择。')
IMMICH_LIBRARY_ID = 'bc3d3ef5-5052-470e-8027-5d20c0e5198e'
IMMICH_EXTERNAL_ROOT = '/external/hidden-pictures/'


def volume_uuid(source=SOURCE):
    mount = source.resolve()
    while not os.path.ismount(mount) and mount != mount.parent:
        mount = mount.parent
    result = subprocess.run(['diskutil', 'info', '-plist', str(mount)],
                            capture_output=True, check=True, timeout=15)
    import plistlib
    return plistlib.loads(result.stdout).get('VolumeUUID')


def check_source(source=SOURCE, expected_volume=EXPECTED_VOLUME):
    if source.is_symlink() or not source.is_dir() or source.suffix.lower() == '.photoslibrary':
        raise ValueError('照片来源未挂载或不是普通文件夹')
    if expected_volume and volume_uuid(source) != expected_volume:
        raise ValueError('照片卷身份与已授权来源不符')
    return source.resolve()


def choose_local_photo_folder():
    """Open the native macOS folder chooser; the browser never supplies a path."""
    picker = Path(__file__).with_name('local_folder_picker.swift')
    completed = subprocess.run(['swift', str(picker)], capture_output=True,
                               text=True, check=True, timeout=300)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError('图库选择器没有返回有效结果') from error
    if result.get('cancelled') is True:
        return None
    selected_path = result.get('path')
    if not isinstance(selected_path, str) or not selected_path:
        raise ValueError('图库选择器没有返回文件夹路径')
    return Path(selected_path)


def ensure_external_state(state=STATE):
    """Verify the exact external APFS volume before creating or writing state."""
    mount = Path('/Volumes/T7_1T')
    if not os.path.ismount(mount):
        raise ValueError('T7_1T 未挂载；为避免写入内部盘，不创建或写入运行数据')
    completed = subprocess.run(['diskutil', 'info', '-plist', str(mount)],
                               capture_output=True, check=True, timeout=15)
    info = plistlib.loads(completed.stdout)
    if (info.get('VolumeUUID') != EXPECTED_STATE_VOLUME or
            info.get('FilesystemType') != 'apfs' or not info.get('WritableVolume')):
        raise ValueError('T7_1T 的卷身份、格式或可写状态与预期不符')
    state = Path(state)
    if not state.resolve().is_relative_to(mount):
        raise ValueError('运行数据只能保存在 T7_1T')
    state.mkdir(parents=True, exist_ok=True)
    return state


def load_model_results(qwen_path=QWEN_RESULTS, afs_path=AFS_RESULTS,
                       afs_extended_path=AFS_EXTENDED):
    result = {}
    for qwen_file in (qwen_path, QWEN_MORE if qwen_path == QWEN_RESULTS else None):
        if qwen_file is None or not qwen_file.exists():
            continue
        for row in json.loads(qwen_file.read_text())['results']:
            fields = {}
            for line in row['response'].splitlines():
                if '：' in line:
                    key, value = line.split('：', 1)
                    fields[key.strip()] = value.strip()
            result.setdefault(row['file'], {})['qwen'] = {
                'model': 'qwen3-vl:8b', 'short': fields.get('建议文件名'),
                'long': fields.get('画面描述'), 'category': fields.get('类别'),
                'people': fields.get('可见人数'), 'uncertainty': fields.get('不确定')}
    for afs_file in (afs_path, AFS_MORE if afs_path == AFS_RESULTS else None):
        if afs_file is None or not afs_file.exists():
            continue
        for row in json.loads(afs_file.read_text())['entries']:
            result.setdefault(row['fileName'], {})['afs'] = {
                'model': 'AI File Sorter 1.9.2 / Gemma 3 4B',
                'short': Path(row['suggestedName']).stem if row.get('suggestedName') else None,
                'extended': None, 'category': row.get('category') or None}
    if afs_extended_path.exists():
        for row in json.loads(afs_extended_path.read_text())['entries']:
            if row.get('suggestedName'):
                result.setdefault(row['fileName'], {}).setdefault('afs', {
                    'model': 'AI File Sorter 1.9.2 / Gemma 3 4B', 'short': None,
                    'extended': None, 'category': None})['extended'] = Path(row['suggestedName']).stem
    if afs_path == AFS_RESULTS and AFS_TRANSLATIONS.exists():
        for filename, translated in json.loads(AFS_TRANSLATIONS.read_text()).items():
            if filename in result and 'afs' in result[filename]:
                result[filename]['afs']['translatedShort'] = translated
    return result


def fallback_reason(item):
    """Conservative local-only decision; different wording is not disagreement."""
    qwen = item.get('models', {}).get('qwen') or {}
    afs = item.get('models', {}).get('afs') or {}
    if not qwen.get('short') and not afs.get('short'):
        return '两个本地模型都没有给出名称'
    if not qwen.get('long') and not afs.get('extended'):
        return '两个本地模型都没有给出详细画面描述'
    if (qwen.get('category') in {'人物', '地点', '活动', '物品', '截图', '其他'}
            and afs.get('category') in {'人物', '地点', '活动', '物品', '截图', '其他'}
            and qwen['category'] != afs['category']):
        return '两个本地模型的主题类别冲突'
    uncertainty = str(qwen.get('uncertainty') or '')
    if (any(phrase in uncertainty for phrase in ('无法识别主体', '无法判断画面', '看不清主体',
                                                '主体不明', '难以辨认主体'))
            and not afs.get('extended')):
        return '本地模型无法辨认画面主体，另一模型未提供详细解释'
    return None


def codex_targets(batch, mode):
    if mode not in CODEX_MODES:
        raise ValueError('Codex 模式必须是全量或兜底')
    return [{'relativePath': item['relativePath'],
             'reason': '你选择了全量分析' if mode == 'all' else reason}
            for item in batch['items']
            if Path(item['originalName']).suffix.lower() in IMAGE_EXTENSIONS
            and not item.get('models', {}).get('codex')
            if (reason := ('你选择了全量分析' if mode == 'all' else fallback_reason(item)))]


def confirmed_person_names(inventory, state=PERSON_REVIEW_STATE):
    """Use only user-named, unambiguous presets for this exact folder.

    The saved face report is a pilot index. Filename collisions and membership
    in more than one person preset make the identity ambiguous, so those files
    deliberately receive no person prefix.
    """
    report_path, presets_path = state / 'report.json', state / 'presets.json'
    if not report_path.is_file() or not presets_path.is_file():
        return {}
    report = json.loads(report_path.read_text())
    if report.get('source') != inventory['source']:
        return {}
    by_name = {}
    for photo in report.get('photos', []):
        by_name.setdefault(photo['name'], []).append(photo['id'])
    by_relative_name = {}
    for item in inventory['items']:
        by_relative_name.setdefault(item['originalName'], []).append(item['relativePath'])
    memberships = {}
    for preset in json.loads(presets_path.read_text()):
        name = _clean_label(preset.get('name', ''))
        if not name:
            continue
        for asset_id in preset.get('assetIds', []):
            memberships.setdefault(asset_id, set()).add(name)
    result = {}
    for original, asset_ids in by_name.items():
        paths = by_relative_name.get(original, [])
        if len(asset_ids) == len(paths) == 1:
            names = memberships.get(asset_ids[0], set())
            if len(names) == 1:
                result[paths[0]] = next(iter(names))
    return result


def people_review_snapshot(batch, state=PERSON_REVIEW_STATE):
    """Expose saved face review only when it belongs to the current folder."""
    report_path = state / 'report.json'
    if not report_path.is_file():
        return {'available': False, 'reason': '尚无本文件夹的人物审核结果'}
    report = json.loads(report_path.read_text())
    if report.get('source') != batch['source']:
        return {'available': False, 'reason': '人物审核结果属于另一个文件夹'}
    names = {}
    for item in batch['items']:
        names.setdefault(item['originalName'], []).append(item['relativePath'])
    report_names = {}
    for photo in report.get('photos', []):
        report_names.setdefault(photo['name'], []).append(photo)
    photos = [{**rows[0], 'relativePath': names[name][0]}
              for name, rows in report_names.items()
              if len(rows) == len(names.get(name, [])) == 1]
    ids = {row['id'] for row in photos}
    pairs = [row for row in report.get('pairs', [])
             if row['aId'] in ids and row['bId'] in ids]
    decisions_path, presets_path = state / 'decisions.json', state / 'presets.json'
    decisions = json.loads(decisions_path.read_text()) if decisions_path.exists() else {}
    presets = json.loads(presets_path.read_text()) if presets_path.exists() else []
    presets = [{**preset, 'assetIds': [asset for asset in preset['assetIds'] if asset in ids]}
               for preset in presets]
    from dual_review import review_view
    view = review_view({'photos': photos, 'pairs': pairs}, decisions)
    return {'available': True, 'photos': photos, 'pairs': pairs,
            'decisions': decisions, 'presets': presets, **view}


def save_person_review_json(state, filename, value):
    """Keep the write pinned to the mounted review directory, never recreate it."""
    if state == PERSON_REVIEW_STATE and not os.path.ismount('/Volumes/T7_1T'):
        raise ValueError('人物审核卷已断开')
    directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    temporary = '.' + filename + '.' + secrets.token_hex(4)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, filename, src_dir_fd=directory,
                       dst_dir_fd=directory)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(directory)


def save_people_review_action(batch, state, body):
    """Persist explicit user review decisions outside the source folder."""
    snapshot = people_review_snapshot(batch, state)
    if not snapshot['available']:
        raise ValueError(snapshot['reason'])
    asset_ids = {photo['id'] for photo in snapshot['photos']}
    action = body.get('action')
    if action == 'decision':
        from dual_review import validate_decision
        kind, key, value = body.get('kind'), body.get('key'), body.get('value')
        valid_keys = (asset_ids if kind == 'photo' else
                      {pair['key'] for pair in snapshot['pairs']} if kind == 'pair' else set())
        if not validate_decision(kind, key, value) or key not in valid_keys:
            raise ValueError('无效的人物判断')
        decisions = {**snapshot['decisions'], kind + ':' + key: {
            'value': value, 'at': datetime.now(timezone.utc).isoformat()}}
        save_person_review_json(state, 'decisions.json', decisions)
    elif action in {'create', 'add', 'remove'}:
        selected = body.get('assetIds')
        if (not isinstance(selected, list) or not selected or
                len(set(selected)) != len(selected) or not set(selected) <= asset_ids):
            raise ValueError('请选择当前文件夹中的照片')
        presets_path = state / 'presets.json'
        presets = json.loads(presets_path.read_text()) if presets_path.exists() else []
        if action == 'create':
            name = body.get('name')
            if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or
                    any(ord(char) < 32 for char in name)):
                raise ValueError('人物名称无效')
            name = name.strip()
            if any(preset['name'].casefold() == name.casefold() for preset in presets):
                raise ValueError('人物名称已存在')
            presets.append({'id': str(uuid.uuid4()), 'name': name,
                            'assetIds': selected,
                            'createdAt': datetime.now(timezone.utc).isoformat()})
        else:
            preset = next((row for row in presets if row['id'] == body.get('presetId')), None)
            if preset is None:
                raise ValueError('找不到人物分组')
            if action == 'add':
                preset['assetIds'] = list(dict.fromkeys(preset['assetIds'] + selected))
            else:
                preset['assetIds'] = [asset for asset in preset['assetIds'] if asset not in selected]
        save_person_review_json(state, 'presets.json', presets)
    else:
        raise ValueError('未知的人物操作')
    return people_review_snapshot(batch, state)


def build_batch(source=SOURCE, model_results=None, *, expected_volume=EXPECTED_VOLUME,
                vision_path=None, person_state=None, index=None):
    vision_path = VISION_RESULTS if vision_path is None else vision_path
    person_state = PERSON_REVIEW_STATE if person_state is None else person_state
    root = check_source(source, expected_volume)
    inventory = scan_folder(root)
    legacy_vision_results = load_vision_results(root, inventory['items'], vision_path)
    # Legacy JSON keyed only by basename is intentionally not imported: two
    # folders can contain different files with the same name. New analyses are
    # loaded by version ID from the project's own index instead.
    models = model_results or {}
    indexed = index.reconcile_inventory(expected_volume, root, inventory['items']) if index else None
    state_by_version = (index.item_state([entry['versionId'] for entry in indexed['assets'].values()])
                        if indexed else {})
    codex_saved = {}
    if CODEX_RESULTS.exists():
        persisted = json.loads(CODEX_RESULTS.read_text())
        if persisted.get('source') == str(root):
            codex_saved = persisted.get('entries', {})
    person_names = confirmed_person_names(inventory, person_state)
    plan = plan_names(inventory, person_names)
    plan_by_path = {item['relativePath']: item for item in plan['items']}
    for item in plan['items']:
        indexed_item = indexed['assets'].get(item['relativePath']) if indexed else None
        saved = state_by_version.get(indexed_item['versionId'], {}) if indexed_item else {}
        item['assetId'] = indexed_item.get('assetId') if indexed_item else None
        item['versionId'] = indexed_item.get('versionId') if indexed_item else None
        item['humanCategory'] = (saved.get('decision') or {}).get('category')
        item['models'] = saved.get('models', {})
        if item['relativePath'] in models:
            item['models'] = {**item['models'], **models[item['relativePath']]}
        elif item['originalName'] in models and model_results is not None:
            # Compatibility for unit tests and explicit one-off adapters only;
            # production model results are always persisted by version ID.
            item['models'] = {**item['models'], **models[item['originalName']]}
        item['vision'] = saved.get('vision') or legacy_vision_results.get(item['relativePath'])
        item['virtualGroup'] = (item['humanCategory'] or
                                (item['vision']['category'] if item['vision'] else None) or
                                item['models'].get('qwen', {}).get('category') or
                                item['models'].get('afs', {}).get('category') or '待确认／其他')
        item['personName'] = person_names.get(item['relativePath'])
        stored = codex_saved.get(item['relativePath'], {})
        if stored.get('signature') == item['signature'] and stored.get('result'):
            item['models'] = {**item['models'], 'codex': stored['result']}
        item['category'] = (item['humanCategory'] or
                            item['models'].get('qwen', {}).get('category') or
                            item['models'].get('afs', {}).get('category') or
                            item['models'].get('codex', {}).get('category') or '未分类')
        item['contentOnlyName'] = None
        item['reason'] = ('拍摄时间取自 ' + str(item['captureTime']['source'])
                          if item['captureTime']['source'] else '没有可靠拍摄时间')
    plan['batchId'] = str(uuid.uuid4())
    plan['createdAt'] = datetime.now(timezone.utc).isoformat()
    plan['volumeUuid'] = expected_volume
    if indexed:
        plan['sourceId'] = indexed['sourceId']
        for item in plan['items']:
            item['identityNeedsReview'] = any(row['relativePath'] == item['relativePath']
                                               for row in indexed['unresolved'])
    return plan


def approve_batch(batch, batch_id, approvals, confirmation, *, source=SOURCE,
                  expected_volume=EXPECTED_VOLUME, rename=None):
    if batch_id != batch['batchId'] or confirmation != '确认ExFAT原地改名':
        raise ValueError('审核批次或确认文字不匹配')
    if not approvals:
        raise ValueError('没有选择任何照片')
    root = check_source(source, expected_volume)
    if root != Path(batch['source']):
        raise ValueError('照片根目录已变化')
    if rename is None:
        return apply_approved(batch, approvals)
    return apply_approved(batch, approvals, rename=rename)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(4))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def analysis_prompt(categories, examples):
    lines = [PROMPT, f"可选类别：{'、'.join(categories)}",
             '格式：画面描述：一到两句；建议备注：短词组；类别：一个预设；'
             '截图：是/否/无法判断；可见人数：整数或无法判断；不确定：一句话。']
    if examples:
        lines.append('以下是用户确认过的分类参考，只用于保持分类习惯，不要照抄画面描述：')
        lines.extend(f"已确认类别 {row['category']}；参考描述：{row['description']}"
                     for row in examples)
    return '\n'.join(lines)


def parse_qwen_result(response):
    fields = {}
    for line in response.splitlines():
        if '：' in line:
            key, value = line.split('：', 1)
            fields[key.strip()] = value.strip()
    return {'category': fields.get('类别'),
            'short': fields.get('建议备注') or fields.get('建议文件名'),
            'long': fields.get('画面描述'),
            'details': {'screenshot': fields.get('截图'),
                        'people': fields.get('可见人数'),
                        'uncertainty': fields.get('不确定'), 'raw': response}}


def qwen_analyze_image(path, categories=None, examples=None, *, state=STATE):
    state = ensure_external_state(state)
    categories = categories or list(GROUPS)
    with tempfile.TemporaryDirectory(dir=state) as temporary:
        preview = Path(temporary, 'preview.jpg')
        subprocess.run(['sips', '-s', 'format', 'jpeg', '-Z', '1024', str(path),
                        '--out', str(preview)], capture_output=True, check=True, timeout=60)
        image = base64.b64encode(preview.read_bytes()).decode('ascii')
    request = urllib.request.Request('http://127.0.0.1:11434/api/generate',
        data=json.dumps({'model': 'qwen3-vl:8b',
                         'prompt': analysis_prompt(categories, examples or []), 'images': [image],
                         'stream': False}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.load(response)['response']


def immich_assets(token):
    from dual_review import api
    rows = api('/search/metadata', token,
               {'size': 100, 'libraryId': IMMICH_LIBRARY_ID})['assets']['items']
    return {row['originalPath']: row for row in rows
            if row.get('libraryId') == IMMICH_LIBRARY_ID}


def immich_annotations(token, asset_id):
    from dual_review import api
    detail = api('/assets/' + asset_id, token)
    albums = []
    for album in api('/albums', token):
        expanded = api('/albums/' + album['id'], token)
        if any(asset['id'] == asset_id for asset in expanded.get('assets', [])):
            albums.append(album['id'])
    return {'id': detail['id'],
            'albums': sorted(albums),
            'people': sorted(person['id'] for person in detail.get('people', [])),
            'tags': sorted(tag['id'] for tag in detail.get('tags', [])),
            'description': (detail.get('exifInfo') or {}).get('description'),
            'favorite': detail.get('isFavorite'),
            'visibility': detail.get('visibility')}


def immich_before(approvals):
    from dual_review import login
    token = login()
    assets = immich_assets(token)
    before = {}
    for relative in approvals:
        path = IMMICH_EXTERNAL_ROOT + relative
        asset = assets.get(path)
        if asset is None:
            raise ValueError('Immich 中找不到待改名资产：' + relative)
        before[relative] = immich_annotations(token, asset['id'])
    return before


def annotation_changes(before, after):
    return {key: {'before': value, 'after': after.get(key)}
            for key, value in before.items() if after.get(key) != value}


def immich_scan_and_compare(renamed, before):
    from dual_review import BASE, login
    token = login()
    request = urllib.request.Request(
        BASE + '/libraries/' + IMMICH_LIBRARY_ID + '/scan',
        data=b'', method='POST', headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request, timeout=25):
        pass
    output = []
    for attempt in range(25):
        assets = immich_assets(token)
        if all(IMMICH_EXTERNAL_ROOT + row['newRelativePath'] in assets for row in renamed):
            break
        time.sleep(5)
    for row in renamed:
        old = row['relativePath']
        new = row['newRelativePath']
        asset = assets.get(IMMICH_EXTERNAL_ROOT + new)
        if asset is None:
            output.append({'old': old, 'new': new, 'status': 'needs_attention',
                           'reason': '重扫后尚未在 Immich 找到新路径'})
            continue
        after = immich_annotations(token, asset['id'])
        changed = annotation_changes(before[old], after)
        output.append({'old': old, 'new': new,
                       'status': 'verified' if not changed else 'needs_attention',
                       'changed': changed})
    return output


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # A remounted external log volume must never turn a valid HTTP
            # request into an empty response (send_response logs first).
            try:
                super().log_message(format, *args)
            except (OSError, ValueError):
                pass

        def _origin(self):
            origin = self.headers.get('Origin', '')
            return origin if origin in service.origins else None

        def _headers(self, status=200, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            if self._origin():
                self.send_header('Access-Control-Allow-Origin', self._origin())
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Photo-Studio-Token')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Vary', 'Origin')
            self.end_headers()

        def _json(self, value, status=200):
            self._headers(status)
            self.wfile.write(json.dumps(value, ensure_ascii=False).encode())

        def _allowed(self):
            return self.client_address[0] in ('127.0.0.1', '::1') and self._origin() is not None

        def do_OPTIONS(self):
            self._headers(204 if self._allowed() else 403)

        def do_GET(self):
            referer = self.headers.get('Referer', '')
            # Browsers normally send only the origin (not /organize) as the
            # Referer when an <img> crosses from port 2283 to port 8766.
            from urllib.parse import urlsplit
            parsed_referer = urlsplit(referer)
            referer_origin = (f'{parsed_referer.scheme}://{parsed_referer.netloc}'
                              if parsed_referer.scheme and parsed_referer.netloc else '')
            image_referer = (self.path.startswith(('/v1/thumbnail/', '/v1/preview/')) and
                             self.client_address[0] in ('127.0.0.1', '::1') and
                             referer_origin in service.origins)
            if not self._allowed() and not image_referer:
                return self._json({'error': 'origin not allowed'}, 403)
            if self.path == '/v1/batch':
                try:
                    service.refresh_batch_if_changed()
                except (ValueError, OSError, subprocess.SubprocessError) as error:
                    return self._json({'error': str(error)}, 409)
                return self._json({'batch': service.batch, 'token': service.token,
                                   'sourceCapabilities': service.source_capabilities(),
                                   'analysis': service.analysis, 'codex': service.codex,
                                   'reconcile': service.reconcile,
                                   'duplicates': service.duplicates,
                                   'vision': getattr(service, 'vision', {
                                       'running': False, 'status': 'idle', 'completed': 0,
                                       'total': 0, 'errors': []})})
            if self.path == '/v1/categories':
                return self._json({'categories': service.index.get_presets()})
            if self.path == '/v1/duplicates/status':
                return self._json({'duplicates': service.duplicates})
            if self.path.startswith('/v1/duplicates?'):
                from urllib.parse import parse_qs, urlsplit
                kind = parse_qs(urlsplit(self.path).query).get('kind', [None])[0]
                if kind not in (None, 'exact', 'visual'):
                    return self._json({'error': 'unknown duplicate kind'}, 400)
                try:
                    service.refresh_batch_if_changed()
                except (ValueError, OSError, subprocess.SubprocessError) as error:
                    return self._json({'error': str(error)}, 409)
                return self._json({'pairs': service.index.list_duplicate_pairs(
                    service.batch.get('sourceId'), kind=kind)})
            if self.path.startswith('/v1/duplicate-groups?'):
                from urllib.parse import parse_qs, urlsplit
                kind = parse_qs(urlsplit(self.path).query).get('kind', ['exact'])[0]
                if kind not in ('exact', 'visual'):
                    return self._json({'error': 'unknown duplicate kind'}, 400)
                try:
                    service.refresh_batch_if_changed()
                except (ValueError, OSError, subprocess.SubprocessError) as error:
                    return self._json({'error': str(error)}, 409)
                pairs = service.index.list_duplicate_pairs(
                    service.batch.get('sourceId'), kind=kind)
                legacy_decisions = [pair for pair in pairs
                                    if pair['decision'] in {'confirmed', 'not_duplicate'}]
                return self._json({'groups': service.duplicate_groups(kind),
                                   'legacyDecisions': legacy_decisions,
                                   'batchId': service.batch['batchId']})
            if self.path == '/v1/people-review':
                return self._json(people_review_snapshot(service.batch, service.person_state))
            if self.path.startswith('/v1/codex/preview?'):
                import urllib.parse
                mode = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('mode', [''])[0]
                try:
                    return self._json({'batchId': service.batch['batchId'], 'mode': mode,
                                       'targets': codex_targets(service.batch, mode)})
                except ValueError as error:
                    return self._json({'error': str(error)}, 400)
            if self.path.startswith(('/v1/thumbnail/', '/v1/preview/')):
                preview_size = 360 if self.path.startswith('/v1/thumbnail/') else 1600
                prefix = '/v1/thumbnail/' if preview_size == 360 else '/v1/preview/'
                relative = self.path.removeprefix(prefix)
                import urllib.parse
                relative = urllib.parse.unquote(relative)
                row = next((item for item in service.batch['items']
                            if item['relativePath'] == relative), None)
                if (row is None or not row.get('versionId') or
                        Path(relative).suffix.lower() not in IMAGE_EXTENSIONS):
                    return self._json({'error': 'image not in batch'}, 404)
                try:
                    service.ensure_state_available()
                    batch = service.batch
                    from photo_workflow import _relative_photo_path, _stat_signature
                    root = check_source(Path(batch['source']), batch['volumeUuid'])
                    image = _relative_photo_path(root, relative)
                    if _stat_signature(image) != row['signature']:
                        raise ValueError('file changed')
                    with service.thumbnail_limit:
                        with tempfile.TemporaryDirectory(dir=service.state) as temporary:
                            preview = Path(temporary, 'preview.jpg')
                            subprocess.run(['sips', '-s', 'format', 'jpeg', '-Z', str(preview_size),
                                            str(image), '--out', str(preview)],
                                           capture_output=True, check=True, timeout=60)
                            if (_stat_signature(image) != row['signature'] or
                                    not service.index.active_version_matches(
                                        batch['sourceId'], relative, row['versionId'], row['signature'])):
                                raise ValueError('photo version changed while creating preview')
                            data = preview.read_bytes()
                    self._headers(200, 'image/jpeg')
                    return self.wfile.write(data)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    return self._json({'error': str(error)}, 409)
            self._json({'error': 'not found'}, 404)

        def do_POST(self):
            if not self._allowed() or self.headers.get('X-Photo-Studio-Token') != service.token:
                return self._json({'error': 'not authorized'}, 403)
            try:
                service.ensure_state_available()
                length = int(self.headers.get('Content-Length', 0))
                if length < 1 or length > 100_000:
                    raise ValueError('invalid request size')
                body = json.loads(self.rfile.read(length))
                if self.path == '/v1/source/select':
                    result = service.select_source_from_picker(body.get('batchId'))
                    return self._json({**result,
                                       'sourceCapabilities': service.source_capabilities()})
                if self.path == '/v1/people-review':
                    with service.lock:
                        if body.get('batchId') != service.batch['batchId']:
                            raise ValueError('审核批次已变化')
                        result = save_people_review_action(service.batch,
                                                           service.person_state, body)
                        if body.get('action') in {'create', 'add', 'remove'}:
                            service.batch = build_batch(
                                Path(service.batch['source']),
                                expected_volume=service.batch['volumeUuid'],
                                person_state=service.person_state,
                                index=service.index)
                            save_json(service.state / 'batch.json', service.batch)
                    return self._json({'review': result, 'batchId': service.batch['batchId']})
                if self.path == '/v1/approve':
                    with service.lock:
                        if not service.can_rename_in_immich():
                            raise ValueError('当前图库未配置为 Immich 外部图库，改名已关闭')
                        if getattr(service, 'vision', {}).get('running'):
                            raise ValueError('Apple Vision 扫描进行中，请完成后再审批改名')
                        if service.codex['running']:
                            raise ValueError('Codex 分析未完成，暂不能批准改名')
                        if service.reconcile['status'] == 'running':
                            raise ValueError('Immich 重扫核对尚未完成')
                        if service.reconcile['status'] == 'needs_attention':
                            raise ValueError('上次重扫发现附加信息变化，已暂停改名')
                        approvals = body.get('approvals')
                        if not isinstance(approvals, dict) or not approvals:
                            raise ValueError('没有选择任何照片')
                        if service.reconcile['status'] != 'verified' and len(approvals) > 2:
                            raise ValueError('首次请只批准最多 2 个文件，先验证 Immich 信息保留')
                        if (body.get('batchId') != service.batch['batchId'] or
                                body.get('confirmation') != '确认ExFAT原地改名'):
                            raise ValueError('审核批次或确认文字不匹配')
                        before = immich_before(approvals)
                        intent = {'at': datetime.now(timezone.utc).isoformat(),
                                  'batchId': service.batch['batchId'],
                                  'approvals': approvals, 'immichBefore': before,
                                  'status': 'started'}
                        audit_path = service.state / ('approval-' + uuid.uuid4().hex + '.json')
                        save_json(audit_path, intent)
                        result = approve_batch(service.batch, body.get('batchId'),
                            body.get('approvals'), body.get('confirmation'),
                            source=Path(service.batch['source']),
                            expected_volume=service.batch['volumeUuid'])
                        for row in result:
                            if row.get('status') != 'renamed':
                                continue
                            item = next((candidate for candidate in service.batch['items']
                                         if candidate['relativePath'] == row['relativePath']), None)
                            if item and item.get('assetId'):
                                try:
                                    service.index.record_approved_rename(
                                        item['assetId'], row['relativePath'], row['newRelativePath'],
                                        details={'batchId': service.batch['batchId'],
                                                 'approvedName': row['approvedName']})
                                except Exception as identity_error:
                                    row['identityStatus'] = 'needs_attention'
                                    row['identityError'] = str(identity_error)
                        save_json(audit_path, {**intent, 'status': 'finished', 'results': result})
                        service.batch = build_batch(
                            Path(service.batch['source']),
                            expected_volume=service.batch['volumeUuid'],
                            person_state=service.person_state, index=service.index)
                        save_json(service.state / 'batch.json', service.batch)
                        service.start_reconcile(result, before)
                    return self._json({'results': result, 'batch': service.batch,
                                       'rescan': service.reconcile})
                if self.path == '/v1/analyze':
                    if (service.analysis['running'] or service.codex['running'] or
                            service.duplicates['running'] or getattr(service, 'vision', {}).get('running')):
                        raise ValueError('图像分析已在运行')
                    if body.get('batchId') != service.batch['batchId']:
                        raise ValueError('审核批次已变化')
                    service.start_analysis()
                    return self._json({'analysis': service.analysis}, 202)
                if self.path == '/v1/vision/analyze':
                    if body.get('batchId') != service.batch['batchId']:
                        raise ValueError('审核批次已变化')
                    service.start_vision_scan()
                    return self._json({'vision': service.vision}, 202)
                if self.path == '/v1/category-review':
                    if body.get('batchId') != service.batch['batchId']:
                        raise ValueError('审核批次已变化')
                    item = next((row for row in service.batch['items']
                                 if row.get('versionId') == body.get('versionId')), None)
                    category = body.get('category')
                    allowed = {row['category'] for row in service.index.get_presets() if row['enabled']}
                    if not item or category not in allowed:
                        raise ValueError('照片或分类不属于当前批次')
                    service.index.review_category(item['versionId'], category,
                                                  note=str(body.get('note') or '')[:400])
                    with service.lock:
                        service.batch = build_batch(Path(service.batch['source']),
                                                    expected_volume=service.batch['volumeUuid'],
                                                    index=service.index)
                        save_json(service.state / 'batch.json', service.batch)
                    return self._json({'batch': service.batch})
                if self.path == '/v1/categories':
                    if body.get('batchId') != service.batch['batchId']:
                        raise ValueError('审核批次已变化')
                    categories = body.get('categories')
                    if not isinstance(categories, list) or not categories or len(categories) > 80:
                        raise ValueError('请提供 1 到 80 个分类')
                    service.index.set_presets(categories)
                    return self._json({'categories': service.index.get_presets()})
                if self.path == '/v1/duplicates/analyze':
                    if body.get('batchId') != service.batch['batchId']:
                        raise ValueError('审核批次已变化')
                    service.start_duplicate_scan()
                    return self._json({'duplicates': service.duplicates}, 202)
                if self.path == '/v1/duplicates/decision':
                    if body.get('batchId') != service.batch['batchId']:
                        raise ValueError('审核批次已变化')
                    pair = next((row for row in service.index.list_duplicate_pairs(
                        service.batch.get('sourceId')) if row['pair_id'] == body.get('pairId')), None)
                    if not pair:
                        raise ValueError('重复候选不属于当前照片来源')
                    service.index.decide_duplicate_pair(pair['pair_id'], body.get('decision'))
                    return self._json({'pairs': service.index.list_duplicate_pairs(
                        service.batch.get('sourceId'), pair['kind'])})
                if self.path == '/v1/duplicate-groups/review':
                    with service.lock:
                        if body.get('batchId') != service.batch['batchId']:
                            raise ValueError('审核批次已变化')
                        reviews = body.get('groups')
                        if not isinstance(reviews, list) or not 1 <= len(reviews) <= 6:
                            raise ValueError('每次请提交 1 到 6 组审核结果')
                        if service.duplicates['running']:
                            raise ValueError('查重扫描进行中，请完成后刷新候选组再保存')
                        current_groups = {
                            (group['kind'], tuple(group['versionIds'])): group
                            for kind in ('exact', 'visual')
                            for group in service.duplicate_groups(kind)
                        }
                        root = check_source(Path(service.batch['source']),
                                            service.batch['volumeUuid'])
                        from photo_workflow import _relative_photo_path, _stat_signature
                        for review in reviews:
                            if not isinstance(review, dict):
                                raise ValueError('审核组格式无效')
                            kind = review.get('kind')
                            members = review.get('versionIds')
                            if (kind not in ('exact', 'visual') or not isinstance(members, list) or
                                    not members or any(not isinstance(value, str) for value in members)):
                                raise ValueError('审核组身份无效')
                            key = (kind, tuple(sorted(members)))
                            if key not in current_groups:
                                raise ValueError('候选组成员或照片版本已变化，请刷新后重新审核')
                            group = current_groups[key]
                            for member in group['members']:
                                path = _relative_photo_path(root, member['relativePath'])
                                if (_stat_signature(path) != member['signature'] or
                                        not service.index.active_version_matches(
                                        service.batch['sourceId'], member['relativePath'],
                                        member['versionId'], member['signature'])):
                                    raise ValueError('照片版本已变化，请刷新后重新审核')
                        service.index.save_duplicate_group_reviews(
                            service.batch['sourceId'], reviews)
                    return self._json({'saved': len(reviews)})
                if self.path == '/v1/codex/analyze':
                    if body.get('batchId') != service.batch['batchId']:
                        raise ValueError('审核批次已变化')
                    mode = body.get('mode')
                    targets = codex_targets(service.batch, mode)
                    paths = [target['relativePath'] for target in targets]
                    if body.get('paths') != paths or body.get('confirmation') != '确认发送到Codex':
                        raise ValueError('发送清单或确认文字不匹配')
                    service.start_codex(mode, targets)
                    return self._json({'codex': service.codex}, 202)
                return self._json({'error': 'not found'}, 404)
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                return self._json({'error': str(error)}, 400)

    return Handler


class Service:
    def __init__(self, state=STATE, source=SOURCE, expected_volume=EXPECTED_VOLUME,
                 index_path=INDEX_PATH):
        self.state = Path(state)
        self.person_state = PERSON_REVIEW_STATE
        self.default_source = Path(source).resolve()
        self.default_volume = expected_volume
        self.ensure_state_available()
        self.index = PhotoIndex(index_path)
        self.index.ensure_default_presets(GROUPS)
        self.batch = build_batch(source, expected_volume=expected_volume, index=self.index)
        self.initial_source_id = self.batch['sourceId']
        save_json(self.state / 'batch.json', self.batch)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.thumbnail_limit = threading.BoundedSemaphore(4)
        self.origins = {'http://127.0.0.1:3000', 'http://localhost:3000',
                        'http://127.0.0.1:2283', 'http://localhost:2283'}
        self._load_source_jobs(self.batch['sourceId'])
        self.codex = {'running': False, 'completed': 0, 'total': 0,
                      'mode': None, 'errors': []}
        self.reconcile = self._load_reconcile(self.batch['sourceId'])

    @staticmethod
    def _job_view(job):
        if not job:
            return {'running': False, 'completed': 0, 'total': 0, 'errors': [],
                    'status': 'idle', 'jobId': None, 'engines': {}}
        return {'running': job['status'] == 'running',
                'completed': job['completed'], 'total': job['total'],
                'errors': job['errors'], 'status': job['status'],
                'jobId': job['job_id'],
                'engines': job.get('progress', {}).get('engines', {})}

    def ensure_state_available(self):
        if Path('/Volumes/T7_1T') in self.state.resolve().parents:
            ensure_external_state(self.state)
        else:
            self.state.mkdir(parents=True, exist_ok=True)

    def _load_source_jobs(self, source_id):
        self.analysis = self._job_view(self.index.latest_job(source_id, 'analysis'))
        self.vision = self._job_view(self.index.latest_job(source_id, 'vision'))
        latest_duplicates = self.index.latest_job(source_id, 'duplicates')
        self.duplicates = self._job_view(latest_duplicates)
        if not latest_duplicates:
            self.duplicates.update({'exactGroups': [], 'visualPairs': []})

    def _reconcile_path(self, source_id):
        return self.state / 'reconcile' / f'{source_id}.json'

    def _load_reconcile(self, source_id):
        path = self._reconcile_path(source_id)
        legacy_path = self.state / 'immich-reconcile.json'
        if path.exists():
            saved = json.loads(path.read_text())
        elif source_id == self.initial_source_id and legacy_path.exists():
            saved = json.loads(legacy_path.read_text())
        else:
            saved = {'status': 'idle', 'results': []}
        if saved.get('status') == 'running':
            return {'status': 'needs_attention', 'results': [],
                    'error': '服务中断，需人工核对上一次重扫'}
        return saved

    def _source_work_running(self):
        return (self.analysis.get('running') or self.codex.get('running') or
                self.duplicates.get('running') or self.vision.get('running') or
                self.reconcile.get('status') == 'running')

    def source_capabilities(self):
        return {'immichRename': (
            Path(self.batch['source']).resolve() == self.default_source and
            self.batch.get('volumeUuid') == self.default_volume)}

    def select_source_from_picker(self, batch_id):
        with self.lock:
            if batch_id != self.batch.get('batchId'):
                raise ValueError('图库批次已变化，请刷新后重试')
            if self._source_work_running():
                raise ValueError('当前任务完成后才能切换图库')
        selected = choose_local_photo_folder()
        if selected is None:
            return {'cancelled': True, 'batch': self.batch}
        selected = check_source(selected, expected_volume=None)
        selected_volume = volume_uuid(selected)
        if not selected_volume:
            raise ValueError('无法确认所选图库所在磁盘的身份')
        return self.select_source_path(selected, selected_volume, batch_id=batch_id)

    def select_source_path(self, source, selected_volume, *, batch_id=None):
        """Switch only after validating the chosen root and building its inventory."""
        root = check_source(Path(source), selected_volume)
        with self.lock:
            if batch_id is not None and batch_id != self.batch.get('batchId'):
                raise ValueError('图库批次已变化，请刷新后重试')
            if self._source_work_running():
                raise ValueError('当前任务完成后才能切换图库')
            self.ensure_state_available()
            next_batch = build_batch(root, expected_volume=selected_volume,
                                     person_state=self.person_state, index=self.index)
            self.batch = next_batch
            self._load_source_jobs(next_batch['sourceId'])
            self.codex = {'running': False, 'completed': 0, 'total': 0,
                          'mode': None, 'errors': []}
            self.reconcile = self._load_reconcile(next_batch['sourceId'])
            save_json(self.state / 'batch.json', self.batch)
        return {'cancelled': False, 'batch': self.batch}

    def can_rename_in_immich(self):
        return self.source_capabilities()['immichRename']

    def refresh_batch_if_changed(self):
        """Reconcile the cached batch when source membership or file versions changed."""
        from photo_workflow import _stat_signature, find_photos

        with self.lock:
            previous = self.batch
        root = check_source(Path(previous['source']), previous['volumeUuid'])
        current = {str(path.relative_to(root)): _stat_signature(path)
                   for path in find_photos(root)}
        cached = {row['relativePath']: row['signature']
                  for row in previous.get('items', [])}
        if current == cached:
            return previous
        return self.refresh_batch()

    def refresh_batch(self):
        self.ensure_state_available()
        with self.lock:
            self.batch = build_batch(Path(self.batch['source']),
                                     expected_volume=self.batch['volumeUuid'],
                                     person_state=self.person_state,
                                     index=self.index)
            save_json(self.state / 'batch.json', self.batch)
        return self.batch

    def start_reconcile(self, results, before):
        renamed = [row for row in results if row['status'] == 'renamed']
        if not renamed:
            self.reconcile = {'status': 'no_changes', 'results': []}
            return
        source_id = self.batch['sourceId']
        reconcile_path = self._reconcile_path(source_id)
        self.reconcile = {'status': 'running', 'results': []}
        self.ensure_state_available()
        save_json(reconcile_path, self.reconcile)

        def worker():
            try:
                checked = immich_scan_and_compare(renamed, before)
                status = ('verified' if all(row['status'] == 'verified' for row in checked)
                          else 'needs_attention')
                self.reconcile = {'status': status, 'results': checked}
            except Exception as error:
                self.reconcile = {'status': 'needs_attention', 'results': [],
                                  'error': str(error)}
            try:
                self.ensure_state_available()
                save_json(reconcile_path, self.reconcile)
            except Exception:
                # Never let a disconnected mountpoint become an internal-disk write.
                pass

        threading.Thread(target=worker, daemon=True).start()

    def duplicate_groups(self, kind):
        """Return only candidate components whose full versions are current."""
        if kind not in {'exact', 'visual'}:
            raise ValueError('unknown duplicate kind')
        source_id = self.batch.get('sourceId')
        items = {row.get('versionId'): row for row in self.batch.get('items', [])
                 if row.get('versionId')}
        pairs = self.index.list_duplicate_pairs(source_id, kind=kind)
        reviews = self.index.list_duplicate_group_reviews(source_id, kind=kind)
        review_by_members = {
            tuple(sorted(review['members'])): review for review in reviews
        }
        result = []
        for group in duplicate_candidate_groups(pairs):
            if group['kind'] != kind or any(version_id not in items
                                            for version_id in group['versionIds']):
                continue
            members = []
            for version_id in group['versionIds']:
                item = items[version_id]
                members.append({'versionId': version_id,
                                'relativePath': item['relativePath'],
                                'name': item['originalName'],
                                'size': item['signature']['size'],
                                'signature': item['signature']})
            edges = [{
                'versionA': edge['version_a'], 'versionB': edge['version_b'],
                'pathA': edge['path_a'], 'pathB': edge['path_b'],
                'score': edge['score'], 'reason': edge['reason'],
                'decision': edge['decision']}
                for edge in group['edges']]
            review = review_by_members.get(tuple(group['versionIds']))
            result.append({'kind': kind, 'versionIds': group['versionIds'],
                           'members': members, 'edges': edges,
                           'review': review})
        return result

    def start_analysis(self):
        with self.lock:
            self.ensure_state_available()
            if (self.analysis['running'] or self.codex['running'] or
                    self.duplicates['running'] or self.vision['running']):
                raise ValueError('已有分析任务正在运行')
            root = check_source(Path(self.batch['source']), self.batch['volumeUuid'])
            batch = self.batch
            categories = [row['category'] for row in self.index.get_presets() if row['enabled']]
            tasks = []
            for row in batch['items']:
                if Path(row['originalName']).suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                if not row.get('versionId'):
                    continue
                for engine in ('afs', 'qwen'):
                    saved = row.get('models', {}).get(engine) or {}
                    if saved.get('status') != 'complete':
                        tasks.append((row, engine))
            source_id = batch['sourceId']
            job_id = self.index.begin_job(source_id, 'analysis', 'local-models', len(tasks))
            self.analysis = {'running': bool(tasks), 'completed': 0, 'total': len(tasks),
                             'errors': [], 'status': 'running' if tasks else 'complete',
                             'jobId': job_id,
                             'engines': {'afs': {'completed': 0, 'total': 0},
                                         'qwen': {'completed': 0, 'total': 0}}}
            for _, engine in tasks:
                self.analysis['engines'][engine]['total'] += 1
            if not tasks:
                self.index.finish_job(job_id, status='complete', completed=0)
                return

        def worker():
            errors = []
            completed = 0
            try:
                for row, engine in tasks:
                    self.ensure_state_available()
                    try:
                        current_root = check_source(root, batch['volumeUuid'])
                        from photo_workflow import _relative_photo_path, _stat_signature
                        image = _relative_photo_path(current_root, row['relativePath'])
                        if _stat_signature(image) != row['signature']:
                            raise ValueError('文件在审核后发生变化')
                        if engine == 'afs':
                            result = afs_categorize_review(
                                image, current_root, categories, state=self.state)
                            self.index.save_analysis(
                                row['versionId'], 'afs-gemma', result['model'],
                                status='complete', category=result['category'],
                                short_text=result.get('short'), long_text=result.get('long'),
                                details={**result.get('details', {}),
                                         'subcategory': result.get('subcategory')})
                        else:
                            response = qwen_analyze_image(
                                image, categories, self.index.approved_examples(), state=self.state)
                            parsed = parse_qwen_result(response)
                            self.index.save_analysis(
                                row['versionId'], 'ollama-qwen', 'qwen3-vl:8b',
                                status='complete', category=parsed.get('category'),
                                short_text=parsed.get('short'), long_text=parsed.get('long'),
                                details=parsed.get('details'))
                        if _stat_signature(image) != row['signature']:
                            raise ValueError('模型分析期间文件发生变化')
                    except Exception as error:
                        errors.append({'file': row['relativePath'], 'engine': engine,
                                       'error': str(error)})
                    completed += 1
                    self.analysis['completed'] = completed
                    self.analysis['errors'] = list(errors)
                    self.analysis['engines'][engine]['completed'] += 1
                    self.index.progress_job(
                        job_id, completed=completed, errors=errors,
                        progress={'engines': self.analysis['engines']})
            except Exception as error:
                errors.append({'file': '(任务)', 'error': str(error)})
            finally:
                status = ('complete' if not errors else
                          'partial' if completed else 'failed')
                try:
                    self.index.finish_job(job_id, status=status, completed=completed,
                                          errors=errors)
                    self.ensure_state_available()
                    self.refresh_batch()
                except Exception as error:
                    errors.append({'file': '(任务收尾)', 'error': str(error)})
                    status = 'partial'
                self.analysis.update({'running': False, 'status': status,
                                      'completed': completed, 'errors': errors})

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _same_signature(first, second):
        return (isinstance(first, dict) and isinstance(second, dict) and
                first.get('size') == second.get('size') and
                first.get('modifiedNs') == second.get('modifiedNs'))

    def _state_file(self, value):
        path = Path(value).expanduser().resolve()
        if not path.is_relative_to(self.state.resolve()):
            raise ValueError('Vision 任务文件必须位于本地状态目录')
        return path

    def _import_vision_manifest(self, manifest_path, request_path, current_by_path):
        """Import only rows matching the request snapshot and current active version."""
        manifest_path = self._state_file(manifest_path)
        request_path = self._state_file(request_path)
        request = json.loads(request_path.read_text(encoding='utf-8'))
        root = check_source(Path(request['source']), request.get('volumeUuid'))
        if request.get('sourceId') != self.batch.get('sourceId'):
            return set(), []
        snapshot = request.get('snapshot', {})
        imported, errors = set(), []
        from photo_workflow import _relative_photo_path, _stat_signature
        with manifest_path.open(encoding='utf-8') as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                    relative = str(row['relative_path'])
                    old = snapshot.get(relative)
                    current = current_by_path.get(relative)
                    if not old or not current:
                        continue
                    if (old.get('versionId') != current.get('versionId') or
                            not self._same_signature(old.get('signature'), current.get('signature'))):
                        continue
                    path = _relative_photo_path(root, relative)
                    if _stat_signature(path) != current['signature']:
                        errors.append({'relativePath': relative, 'error': '扫描期间文件发生变化'})
                        continue
                    if (Path(row['path']).resolve() != root / relative or
                            row.get('file_size_bytes') != current['signature']['size'] or
                            abs(float(row.get('file_modified_unix', 0)) -
                                current['signature']['modifiedNs'] / 1e9) > 0.001):
                        continue
                    if row.get('status') != 'scanned':
                        errors.append({'relativePath': relative,
                                       'error': str(row.get('error') or row.get('status') or 'Vision 扫描失败')})
                        continue
                    result = classify_scan(row)
                    saved = self.index.save_analysis_if_current(
                        request['sourceId'], relative, current['versionId'],
                        current['signature'], 'apple-vision', 'Apple Vision',
                        status='complete', category=result['category'],
                        short_text=None, long_text=None,
                        details={'tags': result['tags'], 'reason': result['reason'],
                                 'needsReview': result['needsReview'],
                                 'ocrText': result['ocrText'],
                                 'faceCount': result['faceCount'],
                                 'humanCount': result['humanCount'],
                                 'visionLabels': row.get('vision_labels', [])})
                    if saved:
                        imported.add(current['versionId'])
                except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                    errors.append({'file': f'(扫描行 {line_number})', 'error': str(error)})
        return imported, errors

    def start_vision_scan(self):
        with self.lock:
            self.ensure_state_available()
            if (self.analysis['running'] or self.codex['running'] or
                    self.duplicates['running'] or self.vision['running']):
                raise ValueError('已有分析任务正在运行')
            batch = self.batch
            root = check_source(Path(batch['source']), batch['volumeUuid'])
            photos = [row for row in batch['items']
                      if Path(row['relativePath']).suffix.lower() in IMAGE_EXTENSIONS
                      and row.get('versionId')]
            if not photos:
                raise ValueError('当前批次没有可扫描的图片')

            previous = self.index.latest_job(batch['sourceId'], 'vision')
            if previous and previous['status'] in {'interrupted', 'partial', 'failed'}:
                progress = previous.get('progress', {})
                old_manifest, old_request = progress.get('manifestPath'), progress.get('requestPath')
                if old_manifest and old_request:
                    try:
                        self._import_vision_manifest(
                            old_manifest, old_request,
                            {row['relativePath']: row for row in photos})
                    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                        # A stale or unreadable checkpoint is ignored; current versions are rescanned.
                        pass

            states = self.index.item_state([row['versionId'] for row in photos])
            pending = [row for row in photos
                       if (states.get(row['versionId'], {}).get('vision') or {}).get('status') != 'complete']
            if not pending:
                self.vision = {'running': False, 'completed': len(photos), 'total': len(photos),
                               'errors': [], 'status': 'complete',
                               'jobId': previous['job_id'] if previous else None}
                return

            job_id = self.index.begin_job(batch['sourceId'], 'vision', 'apple-vision', len(photos))
            task_dir = self.state / 'vision' / batch['sourceId'] / job_id
            task_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = task_dir / 'scan.jsonl'
            request_path = task_dir / 'request.json'
            paths_path = task_dir / 'paths.json'
            snapshot = {row['relativePath']: {
                'versionId': row['versionId'], 'signature': row['signature']}
                for row in photos}
            save_json(request_path, {'sourceId': batch['sourceId'], 'source': str(root),
                                     'volumeUuid': batch['volumeUuid'], 'snapshot': snapshot})
            save_json(paths_path, [row['relativePath'] for row in pending])
            already_complete = len(photos) - len(pending)
            self.vision = {'running': True, 'completed': already_complete,
                           'total': len(photos), 'errors': [], 'status': 'running',
                           'jobId': job_id}
            job_progress = {'stage': 'vision', 'manifestPath': str(manifest_path),
                            'requestPath': str(request_path), 'sourceId': batch['sourceId']}
            self.index.progress_job(job_id, completed=already_complete,
                                    progress=job_progress)

        def worker():
            errors = []
            status = 'failed'
            completed = already_complete
            try:
                self.ensure_state_available()
                build_dir = self.state / 'build'
                build_dir.mkdir(parents=True, exist_ok=True)
                binary = build_dir / 'local_vision_scan'
                scanner = Path(__file__).with_name('local_vision_scan.swift')
                if not binary.is_file() or binary.stat().st_mtime_ns < scanner.stat().st_mtime_ns:
                    subprocess.run(['swiftc', '-O', str(scanner), '-o', str(binary)],
                                   capture_output=True, check=True, timeout=180)
                command = [str(binary), str(root), str(manifest_path),
                           '--paths-file', str(paths_path), '--workers', '4']
                process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, bufsize=1)
                for output_line in process.stderr:
                    fields = output_line.rstrip('\n').split('\t')
                    if len(fields) == 4 and fields[0] == 'PROGRESS':
                        try:
                            processed = int(fields[1])
                            completed = already_complete + processed
                            self.vision.update({'completed': completed})
                            progress = {**job_progress, 'scannerCompleted': processed,
                                        'scannerTotal': int(fields[2]),
                                        'scannerErrors': int(fields[3])}
                            self.index.progress_job(job_id, completed=completed,
                                                    errors=errors, progress=progress)
                        except ValueError:
                            continue
                stdout = process.stdout.read() if process.stdout else ''
                return_code = process.wait()
                if return_code != 0:
                    errors.append({'file': '(扫描器)',
                                   'error': f'Apple Vision 扫描器退出码 {return_code}'})
                imported, import_errors = self._import_vision_manifest(
                    manifest_path, request_path,
                    {row['relativePath']: row for row in photos})
                errors.extend(import_errors)
                states = self.index.item_state([row['versionId'] for row in photos])
                completed = sum((states.get(row['versionId'], {}).get('vision') or {}).get('status') == 'complete'
                                for row in photos)
                reported = {row.get('relativePath') for row in errors
                            if row.get('relativePath')}
                for row in photos:
                    if ((states.get(row['versionId'], {}).get('vision') or {}).get('status') != 'complete' and
                            row['relativePath'] not in reported):
                        errors.append({'relativePath': row['relativePath'],
                                       'error': '没有可用的 Vision 结果；再次启动会重试'})
                if not imported and not stdout.strip() and not errors:
                    errors.append({'file': '(任务)', 'error': '扫描器没有返回结果'})
                status = 'complete' if completed == len(photos) and not errors else (
                    'partial' if completed or errors else 'failed')
            except Exception as error:
                errors.append({'file': '(任务)', 'error': str(error)})
                status = 'partial' if completed else 'failed'
            finally:
                try:
                    self.index.finish_job(job_id, status=status, completed=completed,
                                          errors=errors)
                    self.ensure_state_available()
                    with self.lock:
                        self.batch = build_batch(Path(batch['source']),
                                                 expected_volume=batch['volumeUuid'],
                                                 person_state=self.person_state,
                                                 index=self.index)
                        save_json(self.state / 'batch.json', self.batch)
                except Exception as error:
                    errors.append({'file': '(任务收尾)', 'error': str(error)})
                    status = 'partial'
                self.vision.update({'running': False, 'status': status,
                                    'completed': completed, 'total': len(photos),
                                    'errors': errors})

        threading.Thread(target=worker, daemon=True).start()
        return self.vision

    def start_duplicate_scan(self):
        with self.lock:
            self.ensure_state_available()
            if (self.analysis['running'] or self.codex['running'] or
                    self.duplicates['running'] or self.vision['running']):
                raise ValueError('已有分析任务正在运行')
            root = check_source(Path(self.batch['source']), self.batch['volumeUuid'])
            batch = self.batch
            job_id = self.index.begin_job(batch['sourceId'], 'duplicates', 'sha256+haar',
                                          sum(Path(row['relativePath']).suffix.lower() in IMAGE_EXTENSIONS
                                              for row in batch['items']))
            total = self.index.job(job_id)['total']
            self.duplicates = {'running': True, 'status': 'running', 'completed': 0,
                               'total': total, 'errors': [], 'exactGroups': [],
                               'visualPairs': [], 'jobId': job_id}

        def worker():
            errors = []
            completed = 0
            try:
                binary = Path('/Volumes/T7_1T/local-photo-studio/photo-studio-build/haar_fingerprint')
                if not binary.is_file():
                    raise FileNotFoundError('本机 Haar 检测程序尚未构建')

                def progress(done, count, item_errors):
                    nonlocal completed, errors
                    self.ensure_state_available()
                    completed, errors = done, list(item_errors)
                    self.duplicates.update({'completed': done, 'total': count,
                                            'errors': errors})
                    self.index.progress_job(job_id, completed=done, errors=errors,
                                            progress={'stage': 'sha256+haar'})

                result = analyze_duplicates(root, batch, self.index,
                                            haar_binary=binary, on_progress=progress)
                errors = result['errors']
                status = 'complete' if not errors else 'partial'
                self.duplicates.update({'exactGroups': result['exactGroups'],
                                        'visualPairs': result['visualPairs']})
            except Exception as error:
                errors.append({'file': '(任务)', 'error': str(error)})
                status = 'failed'
            finally:
                try:
                    self.index.finish_job(job_id, status=status, completed=completed,
                                          errors=errors)
                except Exception as error:
                    errors.append({'file': '(任务收尾)', 'error': str(error)})
                    status = 'partial'
                self.duplicates.update({'running': False, 'status': status,
                                        'completed': completed, 'errors': errors})

        threading.Thread(target=worker, daemon=True).start()

    def start_codex(self, mode, targets):
        with self.lock:
            self.ensure_state_available()
            if (self.codex['running'] or self.analysis['running'] or
                    self.duplicates['running'] or self.vision['running']):
                raise ValueError('已有图像分析正在运行')
            if not targets:
                raise ValueError('没有需要交给 Codex 的照片')
            check_source(Path(self.batch['source']), self.batch['volumeUuid'])
            batch = self.batch
            self.codex = {'running': True, 'completed': 0, 'total': len(targets),
                          'mode': mode, 'errors': []}

        def worker():
            from codex_review import analyze_preview
            from photo_workflow import _relative_photo_path, _stat_signature
            try:
                for target in targets:
                    relative = target['relativePath']
                    row = next(item for item in batch['items'] if item['relativePath'] == relative)
                    try:
                        root = check_source(Path(batch['source']), batch['volumeUuid'])
                        image = _relative_photo_path(root, relative)
                        if _stat_signature(image) != row['signature']:
                            raise ValueError('文件在审核后发生变化')
                        self.ensure_state_available()
                        with tempfile.TemporaryDirectory(prefix='local-photo-codex-',
                                                         dir=self.state) as temporary:
                            preview = Path(temporary, 'preview.jpg')
                            subprocess.run(['sips', '-s', 'format', 'jpeg', '-Z', '1024',
                                            str(image), '--out', str(preview)],
                                           capture_output=True, check=True, timeout=60)
                            subprocess.run(['exiftool', '-all=', '-overwrite_original',
                                            str(preview)], capture_output=True,
                                           check=True, timeout=30)
                            if _stat_signature(image) != row['signature']:
                                raise ValueError('文件在预览生成期间发生变化')
                            result = analyze_preview(preview)
                        with self.lock:
                            self.ensure_state_available()
                            saved = ({'source': str(root), 'entries': {}}
                                     if not CODEX_RESULTS.exists() else json.loads(CODEX_RESULTS.read_text()))
                            if saved.get('source') != str(root):
                                saved = {'source': str(root), 'entries': {}}
                            saved['entries'][relative] = {'signature': row['signature'],
                                                          'result': result}
                            save_json(CODEX_RESULTS, saved)
                            current = next((item for item in self.batch['items']
                                            if item['relativePath'] == relative), None)
                            if current and current['signature'] == row['signature']:
                                current['models'] = {**current['models'], 'codex': result}
                                save_json(self.state / 'batch.json', self.batch)
                    except Exception as error:
                        self.codex['errors'].append({'file': relative, 'error': str(error)})
                    self.codex['completed'] += 1
            finally:
                self.codex['running'] = False
                try:
                    with self.lock:
                        self.ensure_state_available()
                        self.batch = build_batch(Path(batch['source']),
                                                 expected_volume=batch['volumeUuid'],
                                                 person_state=self.person_state,
                                                 index=self.index)
                        save_json(self.state / 'batch.json', self.batch)
                except Exception as error:
                    self.codex['errors'].append({'file': '(批次刷新)', 'error': str(error)})

        threading.Thread(target=worker, daemon=True).start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    arguments = parser.parse_args()
    service = Service()
    server = ThreadingHTTPServer(('127.0.0.1', arguments.port), make_handler(service))
    apple_server = None
    apple_service = None
    try:
        from apple_photos_api import ApplePhotosService, make_handler as make_apple_handler
        apple_service = ApplePhotosService()
        apple_server = ThreadingHTTPServer(('127.0.0.1', 8768),
                                           make_apple_handler(apple_service))
        apple_server.daemon_threads = True
        threading.Thread(target=apple_server.serve_forever, daemon=True).start()
        print('Apple Photos API ready on 127.0.0.1:8768; PhotoKit remains disconnected', flush=True)
    except OSError as error:
        print(f'Apple Photos API unavailable on 127.0.0.1:8768: {error}; existing folder service continues',
              flush=True)
    print(f'Folder studio ready on 127.0.0.1:{arguments.port}; {len(service.batch["items"])} items', flush=True)
    try:
        server.serve_forever()
    finally:
        if apple_server is not None:
            apple_server.shutdown()
            apple_server.server_close()
        if apple_service is not None:
            apple_service.disconnect()


if __name__ == '__main__':
    main()
