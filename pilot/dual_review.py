"""Local, read-only Immich + macOS Vision sample review.

Only review decisions are written, outside the source photo folder. This is a
pilot interface, not a face-identification model or an Immich plugin.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = Path('/Volumes/PhotoSource/Photos')
STATE = Path('/Volumes/T7_1T/local-photo-studio/immich-sample/dual-review')
CREDENTIALS = ROOT / 'pilot/immich-sample/.admin-credentials.json'
REVIEW_TOKEN = CREDENTIALS.parent / '.review-token'
BASE = 'http://127.0.0.1:2283/api'
LIBRARY_ID = 'bc3d3ef5-5052-470e-8027-5d20c0e5198e'
OWNER_ID = '88a30c66-6bd1-40c9-960b-068d25c65c31'
UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
KEY = re.compile(r'^[\w.:-]+(?:\|[\w.:-]+)?$')


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    overlap = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(
        0, min(ay + ah, by + bh) - max(ay, by)
    )
    union = aw * ah + bw * bh - overlap
    return overlap / union if union else 0


def reconcile_boxes(immich, vision, threshold=0.35):
    candidates = sorted(
        ((iou(a['box'], b['box']), ai, bi)
         for ai, a in enumerate(immich) for bi, b in enumerate(vision)),
        reverse=True,
    )
    used_a, used_b, matches = set(), set(), []
    for score, ai, bi in candidates:
        if score < threshold or ai in used_a or bi in used_b:
            continue
        used_a.add(ai)
        used_b.add(bi)
        matches.append({'immich': ai, 'vision': bi, 'iou': round(score, 3)})
    immich_only = len(immich) - len(used_a)
    vision_only = len(vision) - len(used_b)
    status = ('neither' if not immich and not vision else
              'both' if matches and not immich_only and not vision_only else
              'disagreement')
    return {'status': status, 'matched': len(matches), 'matches': matches,
            'immichOnly': immich_only, 'visionOnly': vision_only}


def validate_decision(kind, key, value):
    allowed = {'photo': {'has-face', 'no-face', 'later'},
               'pair': {'same', 'different', 'later'}}
    return (kind in allowed and isinstance(key, str) and len(key) <= 250
            and KEY.fullmatch(key) is not None and value in allowed[kind])


def review_view(report, decisions):
    photos = {row['id']: row for row in report['photos']}
    parent = {asset_id: asset_id for asset_id in photos}

    def root(asset_id):
        while parent[asset_id] != asset_id:
            parent[asset_id] = parent[parent[asset_id]]
            asset_id = parent[asset_id]
        return asset_id

    for pair in report['pairs']:
        if decisions.get('pair:' + pair['key'], {}).get('value') == 'same':
            parent[root(pair['bId'])] = root(pair['aId'])

    groups = defaultdict(list)
    for asset_id in photos:
        groups[root(asset_id)].append(asset_id)
    different = set()
    conflicts = set()
    for pair in report['pairs']:
        if decisions.get('pair:' + pair['key'], {}).get('value') == 'different':
            a, b = root(pair['aId']), root(pair['bId'])
            if a == b:
                conflicts.add(a)
            else:
                different.add(tuple(sorted((a, b))))

    confirmed = [{'assetIds': asset_ids, 'conflict': group_id in conflicts}
                 for group_id, asset_ids in groups.items() if len(asset_ids) > 1]
    confirmed.sort(key=lambda item: (-len(item['assetIds']),
                                     photos[item['assetIds'][0]]['name']))
    queue_by_group = {}
    for pair in report['pairs']:
        if pair['distance'] >= 0.5 or 'pair:' + pair['key'] in decisions:
            continue
        a, b = root(pair['aId']), root(pair['bId'])
        group_key = tuple(sorted((a, b)))
        if a == b or group_key in different or a in conflicts or b in conflicts:
            continue
        pa = photos[pair['aId']]['immich']
        pb = photos[pair['bId']]['immich']
        if (len(pa) == len(pb) == 1 and pa[0].get('personId') and
                pa[0]['personId'] == pb[0].get('personId')):
            continue
        if group_key not in queue_by_group or pair['distance'] < queue_by_group[group_key]['distance']:
            queue_by_group[group_key] = pair
    return {'confirmedGroups': confirmed,
            'reviewQueue': sorted(queue_by_group.values(), key=lambda pair: pair['distance'])}


def api(path, token, body=None):
    headers = {'Authorization': 'Bearer ' + token}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)


def login():
    creds = json.loads(CREDENTIALS.read_text())
    req = urllib.request.Request(
        BASE + '/auth/login',
        data=json.dumps({'email': creds['email'], 'password': creds['password']}).encode(),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)['accessToken']


def collect(token):
    vision = json.loads(subprocess.check_output(
        ['swift', str(ROOT / 'pilot/face_candidates.swift'), str(SOURCE)],
        timeout=180,
    ))
    if vision['errors']:
        raise RuntimeError('Vision scan had errors: ' + str(vision['errors']))
    if vision['images'] != 40:
        raise RuntimeError(f'Expected 40 authorized image samples, got {vision["images"]}')
    assets = api('/search/metadata', token, {'size': 100, 'libraryId': LIBRARY_ID})['assets']['items']
    assets = [a for a in assets if a['type'] == 'IMAGE' and a['libraryId'] == LIBRARY_ID]
    if len(assets) != 40:
        raise RuntimeError(f'Expected 40 Immich image assets, got {len(assets)}')
    vf = defaultdict(list)
    for face in vision['faces']:
        x, y, w, h = face['box']
        vf[face['file']].append({'face': face['face'], 'confidence': face['confidence'],
                                 'box': [x, 1 - y - h, w, h]})
    names = {a['originalFileName'] for a in assets}
    if names != {p.name for p in SOURCE.iterdir()
                 if p.is_file() and not p.name.startswith('._')
                 and p.suffix.lower() in {'.jpg', '.jpeg', '.png', '.heic'}}:
        raise RuntimeError('Immich and authorized source filename sets differ')
    people = api('/people', token)['people']
    labels = {p['id']: f'Immich 组 {n} · {p["id"][:8]}'
              for n, p in enumerate(people, 1)}
    rows = []
    for asset in sorted(assets, key=lambda a: a['originalFileName'].casefold()):
        if not asset['originalPath'].startswith('/external/hidden-pictures/'):
            raise RuntimeError('Unexpected Immich original path')
        raw_faces = api('/faces?id=' + urllib.parse.quote(asset['id']), token)
        im = []
        for face in raw_faces:
            width, height = face['imageWidth'], face['imageHeight']
            person = face.get('person')
            im.append({'id': face['id'], 'personId': person['id'] if person else None,
                       'personLabel': labels.get(person['id'],
                                                 '未显示人物记录 ' + person['id'][:8]) if person else '未分组',
                       'box': [face['boundingBoxX1'] / width,
                               face['boundingBoxY1'] / height,
                               (face['boundingBoxX2'] - face['boundingBoxX1']) / width,
                               (face['boundingBoxY2'] - face['boundingBoxY1']) / height]})
        name = asset['originalFileName']
        ve = vf[name]
        rows.append({'id': asset['id'], 'name': name,
                     'immich': im, 'vision': ve,
                     **reconcile_boxes(im, ve)})
    by_name = {r['name']: r for r in rows}
    pairs = []
    for pair in vision['closestPairs']:
        a, b = by_name.get(pair['a']), by_name.get(pair['b'])
        # Full-photo thumbnails do not disambiguate which person is being
        # compared in a group image, so only offer pair decisions for single-face photos.
        if not a or not b or len(a['vision']) != 1 or len(b['vision']) != 1:
            continue
        def person_for(row, face_number):
            vision_index = next((i for i, f in enumerate(row['vision'])
                                 if f['face'] == face_number), None)
            match = next((m for m in row['matches']
                          if m['vision'] == vision_index), None)
            return (row['immich'][match['immich']]['personLabel'] if match else
                    'Immich 未匹配此脸')
        key = f"{pair['a']}:{pair['aFace']}|{pair['b']}:{pair['bFace']}"
        pairs.append({'key': key, 'a': pair['a'], 'aFace': pair['aFace'], 'aId': a['id'],
                      'b': pair['b'], 'bFace': pair['bFace'], 'bId': b['id'],
                      'distance': round(pair['distance'], 3),
                      'aPerson': person_for(a, pair['aFace']),
                      'bPerson': person_for(b, pair['bFace'])})
        if len(pairs) == 30:
            break
    counts = Counter(r['status'] for r in rows)
    return {'generatedAt': datetime.now(timezone.utc).isoformat(),
            'source': str(SOURCE), 'imageCount': len(rows),
            'visionFaceCount': vision['faceCount'],
            'immichFaceCount': sum(len(r['immich']) for r in rows),
            'counts': dict(counts), 'photos': rows, 'pairs': pairs,
            'notes': ['一致只表示两个引擎在同一位置检出脸，不代表身份一致。',
                      'Vision 相似距离不是人脸身份概率；候选必须人工审核。',
                      '决定只保存在本地审核记录，不训练模型、不合并 Immich 人物、不改原图。']}


def save_json(path, value, *, create_parent=False):
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def review_token(path=REVIEW_TOKEN):
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(32))
    token = path.read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise RuntimeError('Invalid local review token')
    return token


def serve(report, immich_token, port, state_dir=STATE, token_path=REVIEW_TOKEN):
    session = review_token(token_path)
    state_device = state_dir.stat().st_dev
    decisions_path = state_dir / 'decisions.json'
    presets_path = state_dir / 'presets.json'
    decisions = json.loads(decisions_path.read_text()) if decisions_path.exists() else {}
    presets = json.loads(presets_path.read_text()) if presets_path.exists() else []
    lock = threading.Lock()
    asset_ids_set = {r['id'] for r in report['photos']}
    html = (ROOT / 'pilot/dual_review.html').read_bytes()

    def state_available():
        try:
            return (state_dir.is_dir() and state_dir.stat().st_dev == state_device and
                    (state_dir != STATE or os.path.ismount('/Volumes/T7_1T')))
        except OSError:
            return False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *args):
            # The one-time URL contains a session token; never print it in access logs.
            pass

        def authorized(self):
            return ('dual_review=' + session) in self.headers.get('Cookie', '')

        def reply(self, code, body, content_type='application/json'):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path)
            if path.path == '/' and urllib.parse.parse_qs(path.query).get('token') == [session]:
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header('Location', '/')
                self.send_header('Set-Cookie',
                                 f'dual_review={session}; HttpOnly; SameSite=Strict; Path=/')
                self.end_headers()
                return
            if not self.authorized():
                self.reply(HTTPStatus.UNAUTHORIZED, b'{"error":"unauthorized"}')
                return
            if path.path == '/':
                self.reply(HTTPStatus.OK, html, 'text/html; charset=utf-8')
            elif path.path == '/api/report':
                with lock:
                    payload = {**report, 'decisions': dict(decisions),
                               'presets': [dict(preset) for preset in presets],
                               **review_view(report, decisions)}
                self.reply(HTTPStatus.OK, json.dumps(payload, ensure_ascii=False).encode())
            elif path.path.startswith('/thumb/'):
                asset_id = path.path.removeprefix('/thumb/')
                if asset_id not in asset_ids_set or not UUID.fullmatch(asset_id):
                    self.reply(HTTPStatus.NOT_FOUND, b'{}')
                    return
                if immich_token:
                    req = urllib.request.Request(
                        BASE + '/assets/' + asset_id + '/thumbnail?size=preview',
                        headers={'Authorization': 'Bearer ' + immich_token},
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=25) as image:
                            self.reply(HTTPStatus.OK, image.read(),
                                       image.headers.get('Content-Type', 'image/jpeg'))
                    except urllib.error.HTTPError:
                        self.reply(HTTPStatus.BAD_GATEWAY, b'{}')
                else:
                    preview = (STATE.parent / 'upload/thumbs' / OWNER_ID /
                               asset_id[:2] / asset_id[2:4] /
                               (asset_id + '_preview.jpeg'))
                    if not preview.is_file():
                        self.reply(HTTPStatus.NOT_FOUND, b'{}')
                        return
                    self.reply(HTTPStatus.OK, preview.read_bytes(), 'image/jpeg')
            else:
                self.reply(HTTPStatus.NOT_FOUND, b'{}')

        def do_POST(self):
            if self.path not in {'/api/decision', '/api/presets'} or not self.authorized():
                self.reply(HTTPStatus.NOT_FOUND, b'{}')
                return
            expected_action = 'preset' if self.path == '/api/presets' else 'decision'
            if (self.headers.get('Origin') != f'http://127.0.0.1:{port}' or
                    self.headers.get('X-Review-Action') != expected_action):
                self.reply(HTTPStatus.FORBIDDEN, b'{}')
                return
            if self.path == '/api/presets':
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if length > 4096:
                        raise ValueError('too large')
                    item = json.loads(self.rfile.read(length))
                    action = item['action']
                    asset_ids = item['assetIds']
                    if (action not in {'create', 'add', 'remove'} or
                            not isinstance(asset_ids, list) or
                            (action == 'create' and not asset_ids) or
                            len(asset_ids) > len(asset_ids_set) or
                            len(set(asset_ids)) != len(asset_ids) or
                            not set(asset_ids).issubset(asset_ids_set)):
                        raise ValueError('invalid preset')
                    if action == 'create':
                        name = item['name'].strip()
                        if not 1 <= len(name) <= 40 or any(ord(char) < 32 for char in name):
                            raise ValueError('invalid preset name')
                    else:
                        preset_id = item['presetId']
                        if not isinstance(preset_id, str) or not UUID.fullmatch(preset_id):
                            raise ValueError('invalid preset id')
                except (ValueError, KeyError, TypeError, AttributeError):
                    self.reply(HTTPStatus.BAD_REQUEST, b'{"error":"invalid preset"}')
                    return
                with lock:
                    if not state_available():
                        self.reply(HTTPStatus.SERVICE_UNAVAILABLE,
                                   b'{"error":"review volume unavailable"}')
                        return
                    updated = json.loads(json.dumps(presets))
                    if action == 'create':
                        if any(preset['name'].casefold() == name.casefold() for preset in updated):
                            self.reply(HTTPStatus.CONFLICT, b'{"error":"duplicate name"}')
                            return
                        preset = {'id': str(uuid.uuid4()), 'name': name,
                                  'assetIds': asset_ids,
                                  'createdAt': datetime.now(timezone.utc).isoformat()}
                        updated.append(preset)
                    else:
                        preset = next((p for p in updated if p['id'] == preset_id), None)
                        if preset is None:
                            self.reply(HTTPStatus.NOT_FOUND, b'{"error":"unknown preset"}')
                            return
                        if action == 'add':
                            preset['assetIds'] = list(dict.fromkeys(preset['assetIds'] + asset_ids))
                        else:
                            preset['assetIds'] = [id_ for id_ in preset['assetIds']
                                                  if id_ not in asset_ids]
                    try:
                        save_json(presets_path, updated)
                    except OSError:
                        self.reply(HTTPStatus.SERVICE_UNAVAILABLE,
                                   b'{"error":"review volume unavailable"}')
                        return
                    presets[:] = updated
                self.reply(HTTPStatus.OK, json.dumps({'preset': preset}, ensure_ascii=False).encode())
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length > 2048:
                    raise ValueError('too large')
                item = json.loads(self.rfile.read(length))
                kind, key, value = item['kind'], item['key'], item['value']
                valid_keys = (asset_ids_set if kind == 'photo' else
                              {p['key'] for p in report['pairs']} if kind == 'pair' else set())
                if not validate_decision(kind, key, value) or key not in valid_keys:
                    raise ValueError('invalid decision')
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                self.reply(HTTPStatus.BAD_REQUEST, b'{"error":"invalid decision"}')
                return
            with lock:
                if not state_available():
                    self.reply(HTTPStatus.SERVICE_UNAVAILABLE,
                               b'{"error":"review volume unavailable"}')
                    return
                updated = {**decisions, kind + ':' + key: {
                    'value': value, 'at': datetime.now(timezone.utc).isoformat()}}
                try:
                    save_json(decisions_path, updated)
                except OSError:
                    self.reply(HTTPStatus.SERVICE_UNAVAILABLE,
                               b'{"error":"review volume unavailable"}')
                    return
                decisions.clear()
                decisions.update(updated)
            self.reply(HTTPStatus.OK, b'{"ok":true}')

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    port = server.server_address[1]
    print(f'Review page: http://127.0.0.1:{port}/?token={session}', flush=True)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--serve-report', action='store_true',
                        help='serve the saved local report without Immich or Docker')
    parser.add_argument('--state-dir', type=Path, default=STATE)
    parser.add_argument('--token-path', type=Path, default=REVIEW_TOKEN,
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.state_dir == STATE and not os.path.ismount('/Volumes/T7_1T'):
        raise SystemExit('T7_1T review volume is not mounted')
    if args.serve_report:
        report_path = args.state_dir / 'report.json'
        if not report_path.is_file():
            raise SystemExit('Saved report volume is not mounted')
        serve(json.loads(report_path.read_text()), None, args.port, args.state_dir,
              args.token_path)
        return
    if not SOURCE.is_dir() or not STATE.parent.is_dir():
        raise SystemExit('Authorized source or T7 review state volume is not mounted')
    token = login()
    report = collect(token)
    save_json(STATE / 'report.json', report, create_parent=True)
    print('Report:', STATE / 'report.json', flush=True)
    print('Summary:', report['counts'], flush=True)
    if not args.build_only:
        serve(report, token, args.port, token_path=args.token_path)


if __name__ == '__main__':
    main()
