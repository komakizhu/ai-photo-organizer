"""Local mock API with generated assets for the Immich selection preview.

Pass --static-root to serve the isolated frontend and mock API together, or
run the API on a separate port. This fixture has no
connection to an Immich account, PhotoKit, or the user's photo folders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import urllib.parse


OWNER_ID = 'selection-preview-user'
MONTHS = [(2025, 10), (2025, 9), (2025, 8)]


def build_assets():
    result = {}
    serial = 1
    for year, month in MONTHS:
        for day in (25, 19, 12, 5):
            for index in range(9):
                created = datetime(year, month, day, 10 + index, 12, tzinfo=timezone.utc)
                asset_id = f'00000000-0000-4000-8000-{serial:012x}'
                ratio = (0.72, 1.0, 1.25, 1.6, 0.88, 1.4)[serial % 6]
                result[asset_id] = {
                    'id': asset_id,
                    'ownerId': OWNER_ID,
                    'ratio': ratio,
                    'thumbhash': None,
                    'createdAt': created.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                    'fileCreatedAt': created.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                    'localOffsetHours': 0,
                    'isFavorite': serial % 11 == 0,
                    'isTrashed': False,
                    'isImage': True,
                    'duration': None,
                    'projectionType': None,
                    'livePhotoVideoId': None,
                    'city': None,
                    'country': None,
                    'visibility': 'timeline',
                    'stack': None,
                    'width': 2400 if ratio >= 1 else int(2400 * ratio),
                    'height': int(2400 / ratio) if ratio >= 1 else 2400,
                    'originalFileName': f'Generated {serial:03d}.jpg',
                    'originalMimeType': 'image/jpeg',
                    'originalPath': f'/generated/{asset_id}.jpg',
                    'isArchived': False,
                    'isOffline': False,
                    'isEdited': False,
                    'hasMetadata': True,
                    'duplicateId': None,
                    'checksum': f'{serial:064x}',
                    'fileModifiedAt': created.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                    'updatedAt': created.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                    'exifInfo': {
                        'make': None,
                        'model': None,
                        'exifImageWidth': 2400 if ratio >= 1 else int(2400 * ratio),
                        'exifImageHeight': int(2400 / ratio) if ratio >= 1 else 2400,
                        'fileSizeInByte': 450_000,
                        'orientation': '1',
                        'dateTimeOriginal': created.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                        'modifyDate': created.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                        'timeZone': 'UTC',
                        'lensModel': None,
                        'fNumber': None,
                        'focalLength': None,
                        'iso': None,
                        'exposureTime': None,
                        'latitude': None,
                        'longitude': None,
                        'city': None,
                        'country': None,
                        'state': None,
                        'description': None,
                    },
                    'tags': [],
                    'people': [],
                }
                result[asset_id]['localDateTime'] = {
                    'year': year,
                    'month': month,
                    'day': day,
                    'hour': 10 + index,
                    'minute': 12,
                    'second': 0,
                    'millisecond': 0,
                }
                serial += 1
    return result


ASSETS = build_assets()


def timeline_bucket_response(assets):
    values = list(assets)
    return {
        'id': [asset['id'] for asset in values],
        'ownerId': [asset['ownerId'] for asset in values],
        'ratio': [asset['ratio'] for asset in values],
        'thumbhash': [asset['thumbhash'] for asset in values],
        'createdAt': [asset['createdAt'] for asset in values],
        'fileCreatedAt': [asset['fileCreatedAt'] for asset in values],
        'localOffsetHours': [asset['localOffsetHours'] for asset in values],
        'isFavorite': [asset['isFavorite'] for asset in values],
        'isTrashed': [asset['isTrashed'] for asset in values],
        'isImage': [asset['isImage'] for asset in values],
        'duration': [asset['duration'] for asset in values],
        'projectionType': [asset['projectionType'] for asset in values],
        'livePhotoVideoId': [asset['livePhotoVideoId'] for asset in values],
        'city': [asset['city'] for asset in values],
        'country': [asset['country'] for asset in values],
        'visibility': [asset['visibility'] for asset in values],
        'stack': [None for _ in values],
    }


def mock_user():
    return {
        'id': OWNER_ID,
        'email': 'preview@example.test',
        'name': '本地生成数据预览',
        'profileImagePath': '',
        'avatarColor': 'blue',
        'profileChangedAt': '2025-01-22T21:31:23.996Z',
        'storageLabel': 'preview',
        'shouldChangePassword': False,
        'isAdmin': True,
        'createdAt': '2025-01-22T21:31:23.996Z',
        'deletedAt': None,
        'updatedAt': '2025-01-22T21:31:23.996Z',
        'oauthId': '',
        'quotaSizeInBytes': None,
        'quotaUsageInBytes': 0,
        'status': 'active',
        'license': None,
    }


PREFERENCES = {
    'albums': {'defaultAssetOrder': 'desc'},
    'folders': {'enabled': True, 'sidebarWeb': True},
    'memories': {'enabled': False, 'duration': 5},
    'people': {'enabled': True, 'sidebarWeb': True},
    'sharedLinks': {'enabled': True, 'sidebarWeb': True},
    'ratings': {'enabled': False},
    'tags': {'enabled': True, 'sidebarWeb': True},
    'emailNotifications': {'enabled': False, 'albumInvite': False, 'albumUpdate': False},
    'download': {'archiveSize': 4_294_967_296, 'includeEmbeddedVideos': False},
    'purchase': {'showSupportBadge': False, 'hideBuyButtonUntil': None},
    'cast': {'gCastEnabled': False},
    'recentlyAdded': {'sidebarWeb': False},
}


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = 'GeneratedImmichPreview/1.0'

    def log_message(self, _format, *_args):
        return

    def reply(self, status, body, content_type='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        static_root = getattr(self.server, 'static_root', None)
        if static_root and not path.startswith('/api/'):
            candidate = (static_root / path.lstrip('/')).resolve()
            if not candidate.is_relative_to(static_root):
                return self.reply(404, {'message': 'not found'})
            if candidate.is_dir():
                candidate = candidate / 'index.html'
            if not candidate.is_file() and not Path(path).suffix:
                candidate = static_root / 'index.html'
            if not candidate.is_file():
                return self.reply(404, {'message': 'not found'})
            content_type = mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream'
            return self.reply(200, candidate.read_bytes(), content_type)

        if path == '/api/server/ping':
            return self.reply(200, {'res': 'pong'})
        if path == '/api/server/version':
            return self.reply(200, {'major': 3, 'minor': 2, 'patch': 2, 'name': 'generated-preview'})
        if path == '/api/users/me':
            return self.reply(200, mock_user())
        if path == '/api/users/me/preferences':
            return self.reply(200, PREFERENCES)
        if path == '/api/server/about':
            return self.reply(200, {'version': 'v3.2.2', 'licensed': False, 'repository': 'immich-app/immich'})
        if path == '/api/server/features':
            return self.reply(200, {
                'smartSearch': True, 'facialRecognition': True, 'duplicateDetection': True, 'map': False,
                'reverseGeocoding': False, 'importFaces': False, 'sidecar': True, 'search': True, 'trash': True,
                'oauth': False, 'oauthAutoLaunch': False, 'passwordLogin': True, 'configFile': False,
                'email': False, 'ocr': False,
            })
        if path == '/api/server/config':
            return self.reply(200, {
                'loginPageMessage': '', 'trashDays': 30, 'userDeleteDelay': 7, 'oauthButtonText': '',
                'isInitialized': True, 'isOnboarded': True, 'externalDomain': '', 'publicUsers': True,
                'maintenanceMode': False,
            })
        if path == '/api/server/media-types':
            return self.reply(200, {'video': ['.mp4', '.mov'], 'image': ['.jpg', '.jpeg', '.png'], 'sidecar': []})
        if path in {'/api/notifications', '/api/notifications/all', '/api/albums', '/api/memories'}:
            return self.reply(200, [])
        if path == '/api/server/storage':
            return self.reply(200, {
                'diskSize': '1.0 GiB', 'diskUse': '0.1 GiB', 'diskAvailable': '0.9 GiB',
                'diskSizeRaw': 1_073_741_824, 'diskUseRaw': 107_374_182, 'diskAvailableRaw': 966_367_642,
                'diskUsagePercentage': 10,
            })
        if path == '/api/timeline/buckets':
            grouped = {}
            for asset in ASSETS.values():
                month_key = asset['fileCreatedAt'][:7] + '-01T00:00:00.000Z'
                grouped[month_key] = grouped.get(month_key, 0) + 1
            return self.reply(200, [
                {'timeBucket': bucket, 'count': count}
                for bucket, count in sorted(grouped.items(), reverse=True)
            ])
        if path == '/api/timeline/bucket':
            bucket = query.get('timeBucket', [''])[0][:7]
            assets = [asset for asset in ASSETS.values() if asset['fileCreatedAt'].startswith(bucket)]
            assets.sort(key=lambda asset: asset['fileCreatedAt'], reverse=True)
            return self.reply(200, timeline_bucket_response(assets))
        if path.startswith('/api/assets/') and path.endswith('/thumbnail'):
            asset_id = path.split('/')[-2]
            asset = ASSETS.get(asset_id)
            if not asset:
                return self.reply(404, {'message': 'generated asset not found'})
            serial = int(asset_id[-4:], 16)
            hue = (serial * 47) % 360
            svg = (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{asset["width"]}" height="{asset["height"]}" '
                'viewBox="0 0 480 360" preserveAspectRatio="xMidYMid slice">'
                '<defs><linearGradient id="g" x1="0" x2="1" y1="0" y2="1">'
                f'<stop stop-color="hsl({hue} 72% 58%)"/><stop offset="1" stop-color="hsl({(hue + 74) % 360} 65% 28%)"/>'
                '</linearGradient></defs><rect width="480" height="360" fill="url(#g)"/>'
                '<circle cx="350" cy="100" r="48" fill="#fff" fill-opacity=".5"/>'
                '<path d="M0 290 135 150l84 88 58-54 203 176H0z" fill="#fff" fill-opacity=".3"/></svg>'
            ).encode('utf-8')
            return self.reply(200, svg, 'image/svg+xml')
        if path.startswith('/api/assets/'):
            asset_id = path.rsplit('/', 1)[-1]
            asset = ASSETS.get(asset_id)
            if asset:
                owner = mock_user()
                # Detail responses are AssetResponseDto, not internal TimelineAsset objects.
                # `ratio` is the frontend's TimelineAsset discriminator: leaking it makes
                # toTimelineAsset skip ISO-date conversion and crashes the viewer renderer.
                timeline_only = {'ratio', 'localOffsetHours', 'isImage', 'city', 'country', 'projectionType'}
                detail = {
                    key: value for key, value in asset.items()
                    if not key.startswith('_') and key not in timeline_only
                }
                detail.update({
                    'owner': {key: owner[key] for key in ('id', 'email', 'name', 'profileImagePath', 'profileChangedAt', 'avatarColor')},
                    'libraryId': f'library-{OWNER_ID}',
                    'type': 'IMAGE',
                    'localDateTime': asset['fileCreatedAt'],
                })
                return self.reply(200, detail)
            return self.reply(404, {'message': 'generated asset not found'})

        return self.reply(200, [])


def enable_local_demo_cookie(static_root: Path):
    index = static_root / 'index.html'
    source = index.read_text(encoding='utf-8')
    marker = '<head>'
    bootstrap = '<script>document.cookie="immich_is_authenticated=true; path=/; SameSite=Lax";</script>'
    if bootstrap not in source:
        index.write_text(source.replace(marker, marker + bootstrap, 1), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=4192)
    parser.add_argument('--static-root', type=Path)
    args = parser.parse_args()
    if args.static_root:
        enable_local_demo_cookie(args.static_root.resolve())
    server = ThreadingHTTPServer((args.host, args.port), PreviewHandler)
    server.static_root = args.static_root.resolve() if args.static_root else None
    print(f'Generated Immich API preview: http://{args.host}:{args.port} (assets={len(ASSETS)})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
