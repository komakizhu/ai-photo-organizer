"""Small, source-scoped folder workflow for the future unified photo UI.

This module does not open Apple Photos, call a cloud model, or infer identities.
Scanning and planning are read-only. Only ``apply_approved`` can rename files,
and it accepts explicit per-item approvals for same-directory destinations.
"""

import ctypes
import errno
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.tif', '.tiff', '.webp',
              '.dng', '.cr2', '.nef', '.arw', '.mov', '.mp4', '.m4v'}
EXIF_TAGS = ('ExifIFD:DateTimeOriginal', 'EXIF:DateTimeOriginal',
             'XMP-exif:DateTimeOriginal',
             'XMP:DateTimeOriginal', 'QuickTime:CreateDate')
DATE_PATTERN = re.compile(r'^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})')
SAFE_LABEL = re.compile(r'[^\w\-\u4e00-\u9fff]+', re.UNICODE)


def _stat_signature(path):
    stat = path.lstat()
    if not path.is_file() or path.is_symlink():
        raise ValueError('not a regular photo file')
    return {'device': stat.st_dev, 'inode': stat.st_ino,
            'size': stat.st_size, 'modifiedNs': stat.st_mtime_ns}


def _relative_photo_path(root, relative):
    if not isinstance(relative, str):
        raise ValueError('invalid relative path')
    path = Path(relative)
    if (path.is_absolute() or not path.parts or
            any(part in ('', '.', '..') or part.lower().endswith('.photoslibrary')
                for part in path.parts)):
        raise ValueError('path outside selected source')
    target = root / path
    if target.parent.resolve() != (root / path.parent).resolve() or not target.resolve().is_relative_to(root):
        raise ValueError('path outside selected source')
    return target


def find_photos(source):
    """Enumerate only this ordinary folder; never enter symlinks or Photos libraries."""
    root = Path(source)
    if root.is_symlink() or not root.is_dir() or root.suffix.lower() == '.photoslibrary':
        raise ValueError('select an ordinary, mounted folder')
    root = root.resolve()
    for directory, child_dirs, filenames in os.walk(root, followlinks=False):
        child_dirs[:] = sorted(name for name in child_dirs
                               if not (Path(directory, name).is_symlink() or
                                       name.lower().endswith('.photoslibrary')))
        for name in sorted(filenames):
            path = Path(directory, name)
            if (not name.startswith('._') and path.suffix.lower() in EXTENSIONS
                    and not path.is_symlink() and path.is_file()):
                yield path


def exiftool_metadata(paths):
    """Read camera/media tags in bounded batches; never use filesystem dates."""
    result = {}
    for start in range(0, len(paths), 40):
        batch = paths[start:start + 40]
        command = ['exiftool', '-json', '-G1', '-DateTimeOriginal',
                   '-OffsetTimeOriginal', '-CreateDate', *map(str, batch)]
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=90, check=True)
        for entry in json.loads(completed.stdout):
            result[entry['SourceFile']] = entry
    return result


def capture_time(metadata):
    """Return original wall-clock time and provenance, never a filesystem fallback."""
    found = []
    for tag in EXIF_TAGS:
        value = metadata.get(tag)
        if not isinstance(value, str):
            continue
        match = DATE_PATTERN.match(value)
        if not match:
            continue
        try:
            date = datetime(*map(int, match.groups()))
        except ValueError:
            continue
        suffix = value[match.end():]
        embedded_offset = re.search(r'(Z|[+-]\d{2}:\d{2})$', suffix)
        offset = (metadata.get('ExifIFD:OffsetTimeOriginal') or
                  metadata.get('EXIF:OffsetTimeOriginal')
                  if tag.startswith(('ExifIFD:', 'EXIF:')) else None)
        offset = offset if isinstance(offset, str) and offset else (
            embedded_offset.group(1) if embedded_offset else None)
        found.append((date, tag, offset))
    if not found:
        return {'value': None, 'source': None, 'timezone': None,
                'uncertain': True, 'issue': 'missing capture time'}
    if len({date for date, _, _ in found}) > 1:
        return {'value': None, 'source': None, 'timezone': None,
                'uncertain': True, 'issue': 'conflicting capture times'}
    date, tag, offset = found[0]
    return {'value': date.strftime('%Y-%m-%d %H:%M:%S'), 'source': tag,
            'timezone': offset, 'uncertain': not bool(offset), 'issue': None}


def scan_folder(source, metadata_reader=exiftool_metadata):
    root = Path(source).resolve()
    paths = list(find_photos(source))
    metadata = metadata_reader(paths) if paths else {}
    rows = []
    for path in paths:
        relative = str(path.relative_to(root))
        stat = path.stat()
        created = getattr(stat, 'st_birthtime', None)
        file_times = {
            'created': datetime.fromtimestamp(created).astimezone().isoformat(timespec='seconds') if created else None,
            'modified': datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec='seconds'),
        }
        capture = capture_time(metadata.get(str(path), {}))
        fallback_times = [value for value in file_times.values() if value]
        rows.append({'relativePath': relative, 'originalName': path.name,
                     'folderHint': str(path.parent.relative_to(root)),
                     'signature': _stat_signature(path),
                     'captureTime': capture,
                     'fileTimes': file_times,
                     'sortTime': {'value': capture['value'], 'source': 'capture'} if capture['value']
                     else {'value': min(fallback_times) if fallback_times else None,
                           'source': 'file_time' if fallback_times else None}})
    return {'source': str(root), 'items': rows}


def _clean_label(value):
    if not isinstance(value, str):
        raise ValueError('label must be text')
    return SAFE_LABEL.sub('-', value.strip()).strip('-_')[:48]


def plan_names(inventory, person_names=None):
    """Suggest date-only names, optionally prefixed by a confirmed person name."""
    person_names = person_names or {}
    occupied = defaultdict(set)
    for row in inventory['items']:
        parent = str(Path(row['relativePath']).parent)
        occupied[parent].add(row['originalName'].casefold())
    proposals = []
    for row in inventory['items']:
        relative = row['relativePath']
        date = row['captureTime']['value']
        proposal = {**row, 'suggestedName': None, 'issue': None}
        if not date:
            proposal['issue'] = ('拍摄时间冲突，需人工确认'
                                 if row['captureTime'].get('issue') == 'conflicting capture times'
                                 else '拍摄时间缺失，需人工确认')
        else:
            time_part = date[:19].replace(':', '-').replace(' ', '_')
            person = _clean_label(person_names.get(relative, ''))
            stem = (person + '_' if person else '') + time_part
            suffix = Path(row['originalName']).suffix.lower()
            parent = str(Path(relative).parent)
            proposed = stem + suffix
            index = 2
            while proposed.casefold() in occupied[parent] and proposed != row['originalName']:
                proposed = f'{stem}_{index:02d}{suffix}'
                index += 1
            occupied[parent].add(proposed.casefold())
            proposal['suggestedName'] = proposed
        proposals.append(proposal)
    return {'source': inventory['source'], 'items': proposals}


def _rename_exclusive(old, new):
    if sys.platform != 'darwin':
        raise OSError(errno.ENOTSUP, 'exclusive rename requires macOS')
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(old), os.fsencode(new), 0x00000004) == 0:
        return
    code = ctypes.get_errno()
    if code not in (errno.ENOTSUP, errno.EOPNOTSUPP):
        raise OSError(code, os.strerror(code), str(new))

    # macOS ExFAT rejects RENAME_EXCL. Reserve the name with O_EXCL, verify
    # that the reservation is still ours, and replace only that empty marker.
    # This is not fully atomic against a hostile concurrent remover; the UI
    # explicitly discloses the remaining race before real-photo approval.
    descriptor = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    marker = os.fstat(descriptor)
    try:
        current = Path(new).lstat()
        if (current.st_dev, current.st_ino, current.st_size) != (
                marker.st_dev, marker.st_ino, 0):
            raise OSError(errno.EBUSY, 'destination reservation changed', str(new))
        os.rename(old, new)
    except BaseException:
        try:
            current = Path(new).lstat()
            if (current.st_dev, current.st_ino, current.st_size) == (
                    marker.st_dev, marker.st_ino, 0):
                Path(new).unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)


def apply_approved(plan, approvals, *, rename=_rename_exclusive):
    """Rename only explicitly approved items, with per-item safety checks.

    The caller must persist the returned log outside the photo source. Failed
    entries do not authorize retries or changes to unapproved entries.
    """
    root = Path(plan['source'])
    if (root.is_symlink() or not root.is_dir() or root.resolve() != root or
            any(part.lower().endswith('.photoslibrary') for part in root.parts)):
        raise ValueError('selected source is unavailable or changed')
    if not isinstance(approvals, dict):
        raise ValueError('approvals must map relative paths to chosen names')
    allowed = {row['relativePath']: row for row in plan['items']}
    if not set(approvals).issubset(allowed):
        raise ValueError('approval contains an item outside the reviewed batch')
    results = []
    for relative, new_name in approvals.items():
        row = allowed[relative]
        result = {'relativePath': relative, 'approvedName': new_name, 'status': 'failed'}
        try:
            if (not isinstance(new_name, str) or new_name in ('', '.', '..') or
                    '/' in new_name or '\\' in new_name or '\0' in new_name or
                    any(ord(char) < 32 for char in new_name) or
                    new_name != Path(new_name).name):
                raise ValueError('invalid same-folder name')
            original = _relative_photo_path(root, relative)
            target = original.with_name(new_name)
            if original.suffix.lower() != target.suffix.lower():
                raise ValueError('file extension changed')
            if _stat_signature(original) != row['signature']:
                raise ValueError('file changed since review')
            if original == target:
                result['status'] = 'unchanged'
            else:
                if target.exists() or target.is_symlink():
                    raise FileExistsError('destination already exists')
                rename(original, target)
                result['status'] = 'renamed'
                result['newRelativePath'] = str(target.relative_to(root))
        except (OSError, ValueError) as error:
            result['error'] = str(error)
        results.append(result)
    return results
