"""Read-only exact and Haar-wavelet duplicate candidate analysis."""

import json
import subprocess
from itertools import combinations
from pathlib import Path

from photo_index import exact_duplicate_groups, hash_file
from photo_workflow import _relative_photo_path, _stat_signature


IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.tif', '.tiff', '.webp'}
MAX_ITEMS = 500
VISUAL_THRESHOLD = 0.60


def fingerprint_one(binary, path):
    completed = subprocess.run([str(binary), str(path)], capture_output=True,
                               text=True, check=True, timeout=60)
    rows = json.loads(completed.stdout)
    if len(rows) != 1 or rows[0].get('path') != str(path):
        raise ValueError('Haar helper returned an unexpected result')
    return rows[0]['fingerprint']


def haar_similarity(first, second):
    """Signed coefficient overlap in [0, 1]; a candidate score, not identity."""
    left = {int(item['index']): int(item['sign']) for item in first}
    right = {int(item['index']): int(item['sign']) for item in second}
    if not left or not right:
        return 0.0
    agreeing = sum(1 for index in left.keys() & right.keys()
                   if left[index] == right[index])
    return agreeing / max(len(left), len(right))


def duplicate_candidate_groups(pairs):
    """Build exact groups and visual connected components from current pairs.

    A visual component is a graph component: members can be connected through
    another photo without every pair in the group being visually similar.
    Explicitly rejected visual edges are omitted from the graph.
    """
    groups = []
    for kind in ('exact', 'visual'):
        edges = [pair for pair in pairs if pair['kind'] == kind and
                 not (kind == 'visual' and pair['decision'] == 'not_duplicate')]
        adjacency = {}
        for edge in edges:
            a, b = edge['version_a'], edge['version_b']
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)
        unseen = set(adjacency)
        while unseen:
            start = min(unseen)
            stack, members = [start], set()
            while stack:
                current = stack.pop()
                if current in members:
                    continue
                members.add(current)
                unseen.discard(current)
                stack.extend(adjacency[current] - members)
            member_ids = sorted(members)
            member_set = set(member_ids)
            group_edges = [edge for edge in edges
                           if edge['version_a'] in member_set and
                           edge['version_b'] in member_set]
            groups.append({'kind': kind, 'versionIds': member_ids,
                           'edges': group_edges})
    groups.sort(key=lambda group: (
        group['kind'] != 'exact',
        min((edge['path_a'] for edge in group['edges']), default=''),
        group['versionIds']))
    return groups


def analyze_duplicates(root, batch, index, *, haar_binary, on_progress=None,
                       max_items=MAX_ITEMS, threshold=VISUAL_THRESHOLD):
    """Hash and fingerprint one already enumerated folder; never writes source files."""
    root = Path(root).resolve()
    photos = [row for row in batch['items']
              if Path(row['relativePath']).suffix.lower() in IMAGE_EXTENSIONS]
    if len(photos) > max_items:
        raise ValueError(f'本次重复检查最多处理 {max_items} 张图片；请缩小文件夹范围')

    processed, errors = [], []
    total = len(photos)
    for number, row in enumerate(photos, 1):
        relative = row['relativePath']
        try:
            path = _relative_photo_path(root, relative)
            if _stat_signature(path) != row['signature']:
                raise ValueError('文件在审核后发生变化')
            digest = hash_file(path)
            if _stat_signature(path) != row['signature']:
                raise ValueError('计算 SHA-256 时文件发生变化')
            fingerprint = fingerprint_one(haar_binary, path)
            if _stat_signature(path) != row['signature']:
                raise ValueError('计算视觉指纹时文件发生变化')
            processed.append({**row, 'sha256': digest, 'fingerprint': fingerprint})
        except Exception as error:
            errors.append({'relativePath': relative, 'error': str(error)})
        if on_progress:
            on_progress(number, total, errors)

    hashes = {row['relativePath']: row['sha256'] for row in processed}
    inventory = []
    for row in batch['items']:
        entry = {'relativePath': row['relativePath'], 'signature': row['signature']}
        if row['relativePath'] in hashes:
            entry['sha256'] = hashes[row['relativePath']]
        inventory.append(entry)
    indexed = index.reconcile_inventory(batch.get('volumeUuid'), root, inventory)
    source_id = indexed['sourceId']
    by_path = {row['relativePath']: {**row, **indexed['assets'][row['relativePath']]}
               for row in processed}

    exact_groups = exact_duplicate_groups(processed)
    exact_pair_keys = set()
    for group in exact_groups:
        for first, second in combinations(group, 2):
            left, right = by_path[first['relativePath']], by_path[second['relativePath']]
            index.save_duplicate_pair(source_id, left['versionId'], right['versionId'],
                                      'exact', 1.0,
                                      {'method': 'SHA-256', 'digest': first['sha256']})
            exact_pair_keys.add(frozenset((left['versionId'], right['versionId'])))

    visual_pairs = []
    for row in processed:
        item = by_path[row['relativePath']]
        index.save_fingerprint(item['versionId'], row['fingerprint'])
    for first, second in combinations(processed, 2):
        left, right = by_path[first['relativePath']], by_path[second['relativePath']]
        if first['sha256'] == second['sha256'] or frozenset((left['versionId'], right['versionId'])) in exact_pair_keys:
            continue
        score = haar_similarity(first['fingerprint'], second['fingerprint'])
        if score >= threshold:
            reason = {'method': 'independent Haar-wavelet coefficient overlap',
                      'threshold': threshold,
                      'sharedSignedFeatures': round(score, 4)}
            index.save_duplicate_pair(source_id, left['versionId'], right['versionId'],
                                      'visual', score, reason)
            visual_pairs.append({'pathA': first['relativePath'],
                                 'pathB': second['relativePath'],
                                 'assetIdA': left['assetId'], 'assetIdB': right['assetId'],
                                 'versionIdA': left['versionId'], 'versionIdB': right['versionId'],
                                 'score': round(score, 4), 'reason': reason})

    exact_result = [[row['relativePath'] for row in group] for group in exact_groups]
    return {'sourceId': source_id, 'exactGroups': exact_result,
            'visualPairs': visual_pairs, 'errors': errors,
            'analyzed': len(processed), 'total': total}
