"""Private SQLite index for reviewable photo-analysis state.

The index contains metadata and model output only; image bytes and thumbnails
stay at the selected source. Asset identity is path-scoped until an approved
rename or a unique content-hash match proves continuity.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


APP_DATA = Path.home() / 'Library' / 'Application Support' / 'LocalPhotoStudio'
DEFAULT_INDEX = APP_DATA / 'photo-index.sqlite3'
SCHEMA_VERSION = 3


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def hash_file(path, chunk_size=1024 * 1024):
    """Return a streaming SHA-256 without creating a file beside the source."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(chunk_size), b''):
            digest.update(block)
    return digest.hexdigest()


class PhotoIndex:
    def __init__(self, path=DEFAULT_INDEX):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._backup_before_schema_change()
        self._initialize()

    def _backup_before_schema_change(self):
        """Keep an existing SQLite index recoverable before schema creation/migration."""
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return
        try:
            with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as source:
                version = source.execute('PRAGMA user_version').fetchone()[0]
        except sqlite3.DatabaseError as error:
            raise RuntimeError('existing photo index is not a readable SQLite database') from error
        if version > SCHEMA_VERSION:
            raise RuntimeError(f'photo index schema {version} is newer than supported {SCHEMA_VERSION}')
        if version == SCHEMA_VERSION:
            return
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = self.path.with_name(f'{self.path.stem}.pre-v{version}-{stamp}.sqlite3')
        with sqlite3.connect(self.path) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        try:
            backup.chmod(0o600)
        except OSError:
            pass

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA busy_timeout = 10000')
        return connection

    def _initialize(self):
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    volume_uuid TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(volume_uuid, root_path)
                );
                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    relative_path TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_asset_per_path
                    ON assets(source_id, relative_path) WHERE active = 1;
                CREATE TABLE IF NOT EXISTS versions (
                    version_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
                    signature_json TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    sha256 TEXT,
                    fingerprint_json TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    time_info_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS versions_sha256
                    ON versions(sha256) WHERE active = 1;
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_version_per_asset
                    ON versions(asset_id) WHERE active = 1;
                CREATE TABLE IF NOT EXISTS analyses (
                    analysis_id TEXT PRIMARY KEY,
                    version_id TEXT NOT NULL REFERENCES versions(version_id),
                    engine TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    category TEXT,
                    short_text TEXT,
                    long_text TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id, engine)
                );
                CREATE TABLE IF NOT EXISTS category_presets (
                    category TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    description TEXT NOT NULL DEFAULT '',
                    examples_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    version_id TEXT NOT NULL REFERENCES versions(version_id),
                    kind TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT 'human',
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id, kind)
                );
                CREATE TABLE IF NOT EXISTS duplicate_pairs (
                    pair_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    version_a TEXT NOT NULL REFERENCES versions(version_id),
                    version_b TEXT NOT NULL REFERENCES versions(version_id),
                    kind TEXT NOT NULL CHECK(kind IN ('exact', 'visual')),
                    score REAL NOT NULL,
                    reason_json TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT 'suggested',
                    updated_at TEXT NOT NULL,
                    CHECK(version_a < version_b),
                    UNIQUE(version_a, version_b, kind)
                );
                CREATE TABLE IF NOT EXISTS duplicate_group_reviews (
                    review_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    kind TEXT NOT NULL CHECK(kind IN ('exact', 'visual')),
                    members_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('same', 'different', 'uncertain', 'discard')),
                    keep_version_ids_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_id, kind, members_json)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    kind TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    errors_json TEXT NOT NULL DEFAULT '[]',
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS job_progress (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                    progress_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
                    kind TEXT NOT NULL,
                    from_path TEXT NOT NULL,
                    to_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS unresolved_paths (
                    unresolved_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    relative_path TEXT NOT NULL,
                    candidate_asset_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
            ''')
            version_columns = {row['name'] for row in db.execute('PRAGMA table_info(versions)')}
            if 'time_info_json' not in version_columns:
                db.execute("ALTER TABLE versions ADD COLUMN time_info_json TEXT NOT NULL DEFAULT '{}'")
            db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        with self._connect() as db:
            db.execute("UPDATE jobs SET status='interrupted', finished_at=? WHERE status='running'",
                       (utc_now(),))

    def source_id(self, volume_uuid, root_path):
        root = str(Path(root_path).resolve())
        volume = str(volume_uuid or 'unverified-volume')
        now = utc_now()
        with self._connect() as db:
            row = db.execute('SELECT source_id FROM sources WHERE volume_uuid=? AND root_path=?',
                             (volume, root)).fetchone()
            if row:
                db.execute('UPDATE sources SET last_seen_at=? WHERE source_id=?', (now, row['source_id']))
                return row['source_id']
            source_id = str(uuid.uuid4())
            db.execute('INSERT INTO sources VALUES (?, ?, ?, ?, ?)',
                       (source_id, volume, root, now, now))
            return source_id

    def reconcile_inventory(self, volume_uuid, root_path, items, batch_id=None):
        """Upsert one complete read-only inventory and return stable project IDs.

        ``sha256`` is optional and should be provided only by an explicit
        duplicate/identity pass. Missing path changes are never guessed from a
        basename; an external rename is linked only by a unique SHA-256 match.
        """
        source_id = self.source_id(volume_uuid, root_path)
        now = utc_now()
        batch_id = batch_id or str(uuid.uuid4())
        prepared = []
        for item in items:
            relative = str(item['relativePath'])
            if Path(relative).is_absolute() or '..' in Path(relative).parts:
                raise ValueError('relative path escapes source')
            signature = item['signature']
            prepared.append({**item, 'relativePath': relative,
                             'signatureJson': canonical_json(signature),
                             'timeInfoJson': canonical_json({
                                 'captureTime': item.get('captureTime'),
                                 'fileTimes': item.get('fileTimes')}),
                             'sha256': item.get('sha256')})
        result, seen_asset_ids, unresolved = {}, set(), []
        with self._connect() as db:
            db.execute('UPDATE sources SET last_seen_at=? WHERE source_id=?', (now, source_id))
            for item in prepared:
                row = db.execute('''SELECT a.asset_id, v.version_id, v.signature_json,
                                          v.sha256, v.size, v.modified_ns, v.time_info_json
                                   FROM assets a JOIN versions v ON v.asset_id=a.asset_id
                                   WHERE a.source_id=? AND a.relative_path=?
                                     AND a.active=1 AND v.active=1''',
                                 (source_id, item['relativePath'])).fetchone()
                if row and row['signature_json'] == item['signatureJson']:
                    asset_id, version_id = row['asset_id'], row['version_id']
                    digest = item['sha256'] if item['sha256'] else row['sha256']
                    if digest != row['sha256'] or row['time_info_json'] != item['timeInfoJson']:
                        db.execute('UPDATE versions SET sha256=?, time_info_json=? WHERE version_id=?',
                                   (digest, item['timeInfoJson'], version_id))
                elif row and item['sha256'] and row['sha256'] == item['sha256']:
                    asset_id, version_id = row['asset_id'], row['version_id']
                    db.execute('UPDATE versions SET signature_json=?, size=?, modified_ns=?, time_info_json=? WHERE version_id=?',
                               (item['signatureJson'], item['signature'].get('size', 0),
                                item['signature'].get('modifiedNs', 0), item['timeInfoJson'], version_id))
                else:
                    if row:
                        db.execute('UPDATE assets SET active=0 WHERE asset_id=?', (row['asset_id'],))
                        db.execute('UPDATE versions SET active=0 WHERE version_id=?', (row['version_id'],))
                    asset_id = None
                    if item['sha256']:
                        candidates = db.execute('''SELECT a.asset_id, a.relative_path, v.version_id
                            FROM assets a JOIN versions v ON v.asset_id=a.asset_id
                            WHERE a.source_id=? AND a.active=1 AND v.active=1 AND v.sha256=?
                              AND a.last_seen_at<>?''',
                            (source_id, item['sha256'], now)).fetchall()
                        # A unique hash is continuity evidence only if no other
                        # new path in this same scan has the same content.
                        new_hash_paths = sum(1 for candidate in prepared
                                             if candidate['sha256'] == item['sha256'])
                        if len(candidates) == new_hash_paths == 1:
                            candidate = candidates[0]
                            asset_id, version_id = candidate['asset_id'], candidate['version_id']
                            db.execute('UPDATE assets SET relative_path=?, last_seen_at=? WHERE asset_id=?',
                                       (item['relativePath'], now, asset_id))
                            db.execute('UPDATE versions SET signature_json=?, size=?, modified_ns=?, time_info_json=? WHERE version_id=?',
                                       (item['signatureJson'], item['signature'].get('size', 0),
                                        item['signature'].get('modifiedNs', 0), item['timeInfoJson'],
                                        version_id))
                            db.execute('INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                                       (str(uuid.uuid4()), asset_id, 'unique-content-relocation',
                                        candidate['relative_path'], item['relativePath'], 'matched',
                                        canonical_json({'sha256': item['sha256']}), now))
                        elif candidates:
                            unresolved.append({'relativePath': item['relativePath'],
                                               'candidateAssetIds': [candidate['asset_id']
                                                                     for candidate in candidates]})
                    if asset_id is None:
                        asset_id, version_id = str(uuid.uuid4()), str(uuid.uuid4())
                        db.execute('INSERT INTO assets VALUES (?, ?, ?, 1, ?, ?)',
                                   (asset_id, source_id, item['relativePath'], now, now))
                        signature = item['signature']
                        db.execute('''INSERT INTO versions
                            (version_id, asset_id, signature_json, size, modified_ns, sha256,
                             fingerprint_json, active, created_at, time_info_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)''',
                            (version_id, asset_id, item['signatureJson'],
                             signature.get('size', 0), signature.get('modifiedNs', 0),
                             item['sha256'], None, now, item['timeInfoJson']))
                db.execute('UPDATE assets SET last_seen_at=? WHERE asset_id=?', (now, asset_id))
                seen_asset_ids.add(asset_id)
                result[item['relativePath']] = {'assetId': asset_id, 'versionId': version_id}
            # Any present active path absent from this complete inventory is
            # retained as inactive history, never silently matched by name.
            if seen_asset_ids:
                placeholders = ','.join('?' for _ in seen_asset_ids)
                db.execute(f'''UPDATE assets SET active=0 WHERE source_id=? AND active=1
                               AND asset_id NOT IN ({placeholders})''',
                           (source_id, *seen_asset_ids))
                db.execute(f'''UPDATE versions SET active=0 WHERE asset_id IN
                               (SELECT asset_id FROM assets WHERE source_id=? AND active=0)''',
                           (source_id,))
            elif not prepared:
                db.execute('UPDATE assets SET active=0 WHERE source_id=?', (source_id,))
                db.execute('''UPDATE versions SET active=0 WHERE asset_id IN
                              (SELECT asset_id FROM assets WHERE source_id=?)''', (source_id,))
            for row in unresolved:
                db.execute('INSERT INTO unresolved_paths VALUES (?, ?, ?, ?, ?, NULL)',
                           (str(uuid.uuid4()), source_id, row['relativePath'],
                            canonical_json(row['candidateAssetIds']), now))
        return {'sourceId': source_id, 'batchId': batch_id, 'assets': result,
                'unresolved': unresolved}

    def record_approved_rename(self, asset_id, old_path, new_path, *, details=None):
        """Preserve identity only after the caller reports a completed approved rename."""
        if Path(old_path).parent != Path(new_path).parent or Path(old_path).name == Path(new_path).name:
            raise ValueError('rename must stay in the same directory')
        now = utc_now()
        with self._connect() as db:
            row = db.execute('SELECT source_id, relative_path FROM assets WHERE asset_id=? AND active=1',
                             (asset_id,)).fetchone()
            if not row or row['relative_path'] != old_path:
                raise ValueError('asset identity changed before rename record')
            collision = db.execute('SELECT 1 FROM assets WHERE source_id=? AND relative_path=? AND active=1',
                                   (row['source_id'], new_path)).fetchone()
            if collision:
                raise ValueError('rename destination already indexed')
            db.execute('UPDATE assets SET relative_path=?, last_seen_at=? WHERE asset_id=?',
                       (new_path, now, asset_id))
            db.execute('INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (str(uuid.uuid4()), asset_id, 'approved-rename', old_path, new_path,
                        'complete', canonical_json(details or {}), now))

    def save_analysis(self, version_id, engine, model, *, status='complete', category=None,
                      short_text=None, long_text=None, details=None):
        if engine not in {'afs-gemma', 'ollama-qwen', 'apple-vision'}:
            raise ValueError('unknown local analysis engine')
        with self._connect() as db:
            row = db.execute('SELECT 1 FROM versions WHERE version_id=? AND active=1',
                             (version_id,)).fetchone()
            if not row:
                raise ValueError('photo version is no longer current')
            analysis_id = str(uuid.uuid4())
            db.execute('''INSERT INTO analyses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version_id, engine) DO UPDATE SET model=excluded.model,
                status=excluded.status, category=excluded.category,
                short_text=excluded.short_text, long_text=excluded.long_text,
                details_json=excluded.details_json, created_at=excluded.created_at''',
                (analysis_id, version_id, engine, model, status, category, short_text,
                 long_text, canonical_json(details or {}), utc_now()))
            return analysis_id

    def set_presets(self, categories):
        categories = list(categories)
        names = [str(category.get('name', '')).strip().casefold() for category in categories]
        if len(names) > 80 or len(set(names)) != len(names):
            raise ValueError('duplicate or excessive categories')
        now = utc_now()
        with self._connect() as db:
            db.execute('UPDATE category_presets SET enabled=0, updated_at=?', (now,))
            for category in categories:
                name = str(category.get('name', '')).strip()
                if not name or len(name) > 80:
                    raise ValueError('invalid category name')
                db.execute('''INSERT INTO category_presets
                    (category, enabled, description, examples_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(category) DO UPDATE SET enabled=excluded.enabled,
                    description=excluded.description, examples_json=excluded.examples_json,
                    updated_at=excluded.updated_at''',
                    (name, bool(category.get('enabled', True)),
                     str(category.get('description', ''))[:400],
                     canonical_json(category.get('examples', [])), now))

    def get_presets(self):
        with self._connect() as db:
            return [dict(row) | {'enabled': bool(row['enabled']),
                                 'examples': json.loads(row['examples_json'])}
                    for row in db.execute('SELECT * FROM category_presets ORDER BY category')]

    def ensure_default_presets(self, categories):
        with self._connect() as db:
            count = db.execute('SELECT COUNT(*) FROM category_presets').fetchone()[0]
        if not count:
            self.set_presets([{'name': name} for name in categories])

    def item_state(self, version_ids):
        """Return saved analyses and the human decision for the requested versions."""
        if not version_ids:
            return {}
        placeholders = ','.join('?' for _ in version_ids)
        output = {version_id: {'models': {}, 'decision': None, 'vision': None}
                  for version_id in version_ids}
        with self._connect() as db:
            for row in db.execute(
                    f'SELECT version_id, time_info_json FROM versions WHERE version_id IN ({placeholders})',
                    list(version_ids)):
                output[row['version_id']]['timeInfo'] = json.loads(row['time_info_json'])
            for row in db.execute(f'SELECT * FROM analyses WHERE version_id IN ({placeholders})',
                                  list(version_ids)):
                result = {
                    'model': row['model'], 'category': row['category'],
                    'short': row['short_text'], 'long': row['long_text'],
                    'details': json.loads(row['details_json']),
                    'status': row['status']}
                if row['engine'] == 'apple-vision':
                    details = result['details']
                    output[row['version_id']]['vision'] = {
                        'category': row['category'],
                        'tags': details.get('tags', []),
                        'reason': details.get('reason', 'Apple Vision 分类建议'),
                        'needsReview': bool(details.get('needsReview', True)),
                        'ocrText': details.get('ocrText', []),
                        'status': row['status'],
                    }
                else:
                    engine = 'afs' if row['engine'] == 'afs-gemma' else 'qwen'
                    output[row['version_id']]['models'][engine] = result
            for row in db.execute(f"SELECT * FROM decisions WHERE kind='category' AND version_id IN ({placeholders})",
                                  list(version_ids)):
                output[row['version_id']]['decision'] = json.loads(row['value_json'])
        return output

    def review_category(self, version_id, category, *, note='', source='human'):
        if source != 'human':
            raise ValueError('only explicit human decisions are authoritative')
        now, decision_id = utc_now(), str(uuid.uuid4())
        with self._connect() as db:
            db.execute('''INSERT INTO decisions VALUES (?, ?, 'category', ?, ?, ?)
                ON CONFLICT(version_id, kind) DO UPDATE SET value_json=excluded.value_json,
                actor=excluded.actor, created_at=excluded.created_at''',
                (decision_id, version_id, canonical_json({'category': category, 'note': note}),
                 source, now))
        return decision_id

    def approved_examples(self, limit=12):
        """Return short, user-corrected text hints only; never include image bytes."""
        with self._connect() as db:
            rows = db.execute('''SELECT d.value_json, an.long_text, an.short_text, an.created_at
                FROM decisions d JOIN versions v ON v.version_id=d.version_id
                LEFT JOIN analyses an ON an.version_id=v.version_id AND an.engine='ollama-qwen'
                WHERE d.kind='category' AND d.actor='human' AND v.active=1
                ORDER BY d.created_at DESC LIMIT ?''', (limit,)).fetchall()
        result = []
        for row in rows:
            decision = json.loads(row['value_json'])
            description = row['long_text'] or row['short_text']
            if decision.get('category') and description:
                result.append({'category': decision['category'],
                               'description': str(description)[:160]})
        return result

    def save_fingerprint(self, version_id, fingerprint):
        with self._connect() as db:
            db.execute('UPDATE versions SET fingerprint_json=? WHERE version_id=? AND active=1',
                       (canonical_json(fingerprint), version_id))

    def save_duplicate_pair(self, source_id, version_a, version_b, kind, score, reason,
                            decision='suggested'):
        if kind not in {'exact', 'visual'} or decision not in {'suggested', 'not_duplicate', 'confirmed'}:
            raise ValueError('invalid duplicate candidate')
        a, b = sorted((version_a, version_b))
        with self._connect() as db:
            pair_id = str(uuid.uuid4())
            db.execute('''INSERT INTO duplicate_pairs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version_a, version_b, kind) DO UPDATE SET score=excluded.score,
                reason_json=excluded.reason_json,
                decision=CASE WHEN duplicate_pairs.decision IN ('not_duplicate', 'confirmed')
                    THEN duplicate_pairs.decision ELSE excluded.decision END,
                updated_at=excluded.updated_at''',
                (pair_id, source_id, a, b, kind, float(score), canonical_json(reason), decision, utc_now()))

    def decide_duplicate(self, version_a, version_b, kind, decision):
        if decision not in {'not_duplicate', 'confirmed', 'suggested'}:
            raise ValueError('invalid duplicate decision')
        a, b = sorted((version_a, version_b))
        with self._connect() as db:
            db.execute('''UPDATE duplicate_pairs SET decision=?, updated_at=?
                          WHERE version_a=? AND version_b=? AND kind=?''',
                       (decision, utc_now(), a, b, kind))

    def decide_duplicate_pair(self, pair_id, decision):
        if decision not in {'not_duplicate', 'confirmed', 'suggested'}:
            raise ValueError('invalid duplicate decision')
        with self._connect() as db:
            changed = db.execute('UPDATE duplicate_pairs SET decision=?, updated_at=? WHERE pair_id=?',
                                 (decision, utc_now(), pair_id)).rowcount
            if not changed:
                raise ValueError('duplicate candidate no longer exists')

    def list_duplicate_pairs(self, source_id, kind=None):
        query = '''SELECT p.*, a.relative_path AS path_a, b.relative_path AS path_b,
                          va.size AS size_a, vb.size AS size_b
                   FROM duplicate_pairs p
                   JOIN versions va ON va.version_id=p.version_a
                   JOIN assets a ON a.asset_id=va.asset_id
                   JOIN versions vb ON vb.version_id=p.version_b
                   JOIN assets b ON b.asset_id=vb.asset_id
                   WHERE p.source_id=? AND va.active=1 AND vb.active=1
                     AND a.active=1 AND b.active=1'''
        args = [source_id]
        if kind:
            query += ' AND p.kind=?'
            args.append(kind)
        query += ' ORDER BY p.kind, p.score DESC'
        with self._connect() as db:
            return [dict(row) | {'reason': json.loads(row['reason_json'])}
                    for row in db.execute(query, args)]

    def active_version_matches(self, source_id, relative_path, version_id, signature):
        """Confirm a background result still belongs to this active file version."""
        with self._connect() as db:
            row = db.execute('''SELECT v.signature_json FROM assets a
                JOIN versions v ON v.asset_id=a.asset_id
                WHERE a.source_id=? AND a.relative_path=? AND a.active=1
                  AND v.version_id=? AND v.active=1''',
                (source_id, relative_path, version_id)).fetchone()
        return bool(row and row['signature_json'] == canonical_json(signature))

    def save_analysis_if_current(self, source_id, relative_path, version_id, signature,
                                 engine, model, *, status='complete', category=None,
                                 short_text=None, long_text=None, details=None):
        """Atomically bind a result to the still-active source path and version."""
        if engine not in {'afs-gemma', 'ollama-qwen', 'apple-vision'}:
            raise ValueError('unknown local analysis engine')
        with self._connect() as db:
            current = db.execute('''SELECT v.version_id FROM assets a
                JOIN versions v ON v.asset_id=a.asset_id
                WHERE a.source_id=? AND a.relative_path=? AND a.active=1
                  AND v.version_id=? AND v.active=1 AND v.signature_json=?''',
                (source_id, relative_path, version_id, canonical_json(signature))).fetchone()
            if not current:
                return False
            analysis_id = str(uuid.uuid4())
            db.execute('''INSERT INTO analyses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version_id, engine) DO UPDATE SET model=excluded.model,
                status=excluded.status, category=excluded.category,
                short_text=excluded.short_text, long_text=excluded.long_text,
                details_json=excluded.details_json, created_at=excluded.created_at''',
                (analysis_id, version_id, engine, model, status, category, short_text,
                 long_text, canonical_json(details or {}), utc_now()))
            return True

    def list_duplicate_group_reviews(self, source_id, kind=None):
        query = '''SELECT kind, members_json, status, keep_version_ids_json, updated_at
                   FROM duplicate_group_reviews WHERE source_id=?'''
        args = [source_id]
        if kind:
            query += ' AND kind=?'
            args.append(kind)
        with self._connect() as db:
            return [
                {'kind': row['kind'],
                 'members': json.loads(row['members_json']),
                 'status': row['status'],
                 'keepVersionIds': json.loads(row['keep_version_ids_json']),
                 'updatedAt': row['updated_at']}
                for row in db.execute(query, args)
            ]

    def save_duplicate_group_reviews(self, source_id, reviews):
        """Save a batch of explicit group decisions in one transaction."""
        prepared = []
        allowed_statuses = {'same', 'different', 'uncertain', 'discard'}
        for review in reviews:
            kind = review.get('kind')
            status = review.get('status')
            members = review.get('versionIds')
            keeps = review.get('keepVersionIds')
            if kind not in {'exact', 'visual'} or status not in allowed_statuses:
                raise ValueError('invalid duplicate group decision')
            if (not isinstance(members, list) or not members or
                    any(not isinstance(value, str) or not value for value in members) or
                    len(set(members)) != len(members)):
                raise ValueError('invalid duplicate group members')
            if (not isinstance(keeps, list) or
                    any(not isinstance(value, str) for value in keeps) or
                    len(set(keeps)) != len(keeps) or not set(keeps).issubset(members)):
                raise ValueError('invalid duplicate group keep selection')
            if (status == 'same' and not keeps) or (status != 'same' and keeps):
                raise ValueError('keep selection does not match group decision')
            prepared.append((kind, sorted(members), status, sorted(keeps)))
        if not prepared:
            raise ValueError('no duplicate group decisions supplied')

        with self._connect() as db:
            seen = set()
            for kind, members, status, keeps in prepared:
                members_json = canonical_json(members)
                key = (kind, members_json)
                if key in seen:
                    raise ValueError('duplicate group supplied more than once')
                seen.add(key)
                placeholders = ','.join('?' for _ in members)
                active = {row['version_id'] for row in db.execute(
                    f'''SELECT v.version_id FROM versions v JOIN assets a ON a.asset_id=v.asset_id
                        WHERE a.source_id=? AND a.active=1 AND v.active=1
                          AND v.version_id IN ({placeholders})''',
                    [source_id, *members])}
                if active != set(members):
                    raise ValueError('duplicate group contains a photo version outside this source')
                db.execute('''INSERT INTO duplicate_group_reviews
                    (review_id, source_id, kind, members_json, status,
                     keep_version_ids_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, kind, members_json) DO UPDATE SET
                    status=excluded.status,
                    keep_version_ids_json=excluded.keep_version_ids_json,
                    updated_at=excluded.updated_at''',
                    (str(uuid.uuid4()), source_id, kind, members_json, status,
                     canonical_json(keeps), utc_now()))

    def begin_job(self, source_id, kind, engine, total):
        job_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)',
                       (job_id, source_id, kind, engine, 'running', total, '[]', utc_now()))
            db.execute('INSERT INTO job_progress VALUES (?, ?)', (job_id, '{}'))
        return job_id

    def progress_job(self, job_id, *, completed, errors=None, progress=None):
        with self._connect() as db:
            db.execute('UPDATE jobs SET completed=?, errors_json=? WHERE job_id=? AND status="running"',
                       (completed, canonical_json(errors or []), job_id))
            db.execute('INSERT INTO job_progress VALUES (?, ?) ON CONFLICT(job_id) DO UPDATE SET progress_json=excluded.progress_json',
                       (job_id, canonical_json(progress or {})))

    def finish_job(self, job_id, *, status, completed, errors=None):
        if status not in {'complete', 'partial', 'failed', 'interrupted'}:
            raise ValueError('invalid job status')
        with self._connect() as db:
            db.execute('UPDATE jobs SET status=?, completed=?, errors_json=?, finished_at=? WHERE job_id=?',
                       (status, completed, canonical_json(errors or []), utc_now(), job_id))

    def job(self, job_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['errors'] = json.loads(result.pop('errors_json'))
            progress = db.execute('SELECT progress_json FROM job_progress WHERE job_id=?',
                                  (job_id,)).fetchone()
            result['progress'] = json.loads(progress['progress_json']) if progress else {}
            return result

    def latest_job(self, source_id, kind):
        with self._connect() as db:
            row = db.execute('''SELECT * FROM jobs WHERE source_id=? AND kind=?
                                ORDER BY started_at DESC LIMIT 1''',
                             (source_id, kind)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['errors'] = json.loads(result.pop('errors_json'))
            progress = db.execute('SELECT progress_json FROM job_progress WHERE job_id=?',
                                  (row['job_id'],)).fetchone()
            result['progress'] = json.loads(progress['progress_json']) if progress else {}
            return result


def exact_duplicate_groups(rows):
    """Return only byte-identical groups; visual similarity is a separate pass."""
    grouped = {}
    for row in rows:
        if row.get('sha256'):
            grouped.setdefault(row['sha256'], []).append(row)
    return [members for members in grouped.values() if len(members) > 1]
