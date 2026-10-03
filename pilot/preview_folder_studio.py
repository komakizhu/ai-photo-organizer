"""Isolated frontend with generated ordinary-folder and Apple Photos samples.

Run after building with VITE_PHOTO_STUDIO_BRIDGE=http://127.0.0.1:8767.
Only a user-confirmed native folder selection changes the photo source.
No Immich server or uploads are connected; all state is outside photo folders.
"""
import argparse
import json
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import folder_studio
from apple_photos_api import ApplePhotosService, make_handler as make_apple_handler
from urllib.parse import urlsplit

STATIC = Path(__file__).resolve().parents[1] / 'vendor/immich-v3.2.2/web/build/apple-photos-preview/static'

USER = {
    'id': 'preview-user', 'email': 'preview@example.test', 'name': '本地预览',
    'isAdmin': True, 'createdAt': '2025-01-01T00:00:00Z', 'updatedAt': '2025-01-01T00:00:00Z',
    'profileChangedAt': '2025-01-01T00:00:00Z', 'profileImagePath': '', 'avatarColor': 'primary',
    'clusterGroupId': 'preview', 'deletedAt': None, 'license': None, 'oauthId': '',
    'quotaSizeInBytes': None, 'quotaUsageInBytes': 0, 'shouldChangePassword': False,
    'status': 'active', 'storageLabel': None,
}
PREFERENCES = {
    'albums': {'defaultAssetOrder': 'desc'}, 'cast': {'gCastEnabled': False},
    'download': {'archiveSize': 0, 'includeEmbeddedVideos': False},
    'emailNotifications': {'enabled': False, 'albumInvite': False, 'albumUpdate': False},
    'folders': {'enabled': True, 'sidebarWeb': True},
    'memories': {'enabled': False, 'sidebarWeb': False, 'duration': 0},
    'people': {'enabled': True, 'sidebarWeb': True},
    'purchase': {'hideBuyButtonUntil': '', 'showSupportBadge': False},
    'ratings': {'enabled': False}, 'recentlyAdded': {'sidebarWeb': True},
    'sharedLinks': {'enabled': True, 'sidebarWeb': True}, 'tags': {'enabled': True, 'sidebarWeb': True},
}


class WebPreview(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, format, *args):
        print('WEB', self.path, format % args, flush=True)

    def end_headers(self):
        self.send_header('Set-Cookie', 'immich_is_authenticated=1; Path=/; SameSite=Lax')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def translate_path(self, path):
        translated = Path(super().translate_path(path))
        request_path = urlsplit(path).path
        if translated.is_file():
            return str(translated)
        if request_path.startswith(('/_app/', '/favicon', '/apple-icon')):
            return str(translated)
        return str(STATIC / 'index.html')

    def send_json(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.endswith('/users/me'):
            return self.send_json(USER)
        if path.endswith('/users/me/preferences'):
            return self.send_json(PREFERENCES)
        if path.endswith('/server/config'):
            return self.send_json({'externalDomain': '', 'isInitialized': True, 'isOnboarded': True,
                                   'loginPageMessage': '', 'maintenanceMode': False,
                                   'mapDarkStyleUrl': '', 'mapLightStyleUrl': '', 'minFaces': 3,
                                   'oauthButtonText': '', 'publicUsers': False, 'trashDays': 30,
                                   'userDeleteDelay': 30})
        if path.endswith('/server/features'):
            return self.send_json({'configFile': True, 'duplicateDetection': True, 'email': False,
                                   'facialRecognition': True, 'importFaces': False, 'map': False,
                                   'oauth': False, 'oauthAutoLaunch': False, 'ocr': False,
                                   'passwordLogin': True, 'realtimeTranscoding': False,
                                   'reverseGeocoding': False, 'search': True, 'sidecar': False,
                                   'smartSearch': False, 'trash': True, 'watch': False})
        if path == '/api/server/ping':
            return self.send_json({'res': 'pong'})
        if path == '/api/server/version':
            return self.send_json({'major': 3, 'minor': 2, 'patch': 2})
        if path == '/api/users/me/statistics':
            return self.send_json({'photos': 0, 'videos': 0, 'usage': 0})
        if path in ('/api/notifications', '/api/albums'):
            return self.send_json([])
        if path == '/api/server/media-types':
            return self.send_json({'image': [], 'video': [], 'sidecar': []})
        if path == '/api/server/about':
            return self.send_json({'version': '3.2.2', 'versionUrl': '', 'licensed': False})
        if path == '/api/server/storage':
            return self.send_json({'diskSize': '0 B', 'diskUse': '0 B', 'diskAvailable': '0 B',
                                   'diskSizeRaw': 0, 'diskUseRaw': 0, 'diskAvailableRaw': 0, 'diskUsagePercentage': 0})
        if path.startswith('/api/'):
            return self.send_json({'error': '此预览仅提供本地照片整理'}, 404)
        return super().do_GET()


def generate_photos(root):
    root.mkdir(parents=True, exist_ok=True)
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    for i in range(3):
        width, height = 320, 240
        rows = b''.join(b'\0' + bytes((80 + i * 40, 140 + y // 4, 190)) * width
                        for y in range(height))
        data = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
        for suffix in ('a', 'b'):
            path = root / f'sample-{i + 1}-{suffix}.png'
            if not path.exists():
                path.write_bytes(data)


def make_mock_apple_handler(photo_root, origin='http://127.0.0.1:4173'):
    """Serve synthetic PhotoKit-shaped rows without importing or launching PhotoKit."""
    records = [
        {'assetId': f'preview-asset-{index}',
         'creationDate': f'2026-09-{30 - index:02d}T12:00:00Z',
         'width': 320, 'height': 240, 'screenshot': index in (1, 4),
         'file': photo_root / f'sample-{(index - 1) % 3 + 1}-a.png'}
        for index in range(1, 7)
    ]
    state = {'connected': False, 'token': 'generated-preview-token'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def respond(self, body, status=200, content_type='application/json'):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            if self.headers.get('Origin') == origin:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Photo-Studio-Token')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Vary', 'Origin')
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            return (self.client_address[0] in ('127.0.0.1', '::1') and
                    self.headers.get('Origin') == origin and
                    self.headers.get('X-Photo-Studio-Token') == state['token'] and
                    state['connected'])

        def do_OPTIONS(self):
            if self.headers.get('Origin') != origin:
                return self.respond({'error': 'origin not allowed'}, 403)
            self.respond({})

        def do_GET(self):
            if self.headers.get('Origin') != origin or self.path != '/v1/status':
                return self.respond({'error': 'origin not allowed'}, 403)
            self.respond({'connected': state['connected'], 'mode': 'mock',
                          'authorization': 'authorized' if state['connected'] else 'unknown',
                          'message': '模拟图库已连接。页面仅使用生成图片。' if state['connected'] else
                                     '本地预览；点击连接使用生成的模拟数据。'})

        def do_POST(self):
            if self.headers.get('Origin') != origin or self.client_address[0] not in ('127.0.0.1', '::1'):
                return self.respond({'error': 'origin not allowed'}, 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                request = json.loads(self.rfile.read(length)) if 0 < length <= 1_000_000 else {}
                if self.path == '/v1/connect':
                    state['connected'] = True
                    return self.respond({'connected': True, 'authorization': 'authorized', 'mode': 'mock',
                                         'message': '模拟图库已连接。页面仅使用生成图片。',
                                         'token': state['token']})
                if not self.authorized():
                    return self.respond({'error': '模拟照片会话未授权'}, 403)
                if self.path == '/v1/disconnect':
                    state['connected'] = False
                    return self.respond({'connected': False})
                if self.path == '/v1/cancel':
                    return self.respond({'cancelled': True})
                if self.path == '/v1/assets':
                    rows = records
                    if request.get('screenshots'):
                        rows = [row for row in rows if row['screenshot']]
                    start, end = request.get('startDate'), request.get('endDate')
                    if start and end:
                        rows = [row for row in rows if start <= row['creationDate'][:10] <= end]
                    offset = (request.get('cursor') or {}).get('offset', 0)
                    page = rows[offset:offset + 3]
                    following = {'offset': offset + 3} if offset + 3 < len(rows) else None
                    return self.respond({'assets': [{key: row[key] for key in
                                                     ('assetId', 'creationDate', 'width', 'height', 'screenshot')}
                                                    for row in page],
                                         'nextCursor': following, 'hasMore': following is not None})
                if self.path == '/v1/thumbnail':
                    row = next((item for item in records if item['assetId'] == request.get('assetId')), None)
                    if row is None:
                        return self.respond({'error': '照片已从模拟图库移除'}, 404)
                    return self.respond(row['file'].read_bytes(), content_type='image/png')
                return self.respond({'error': 'not found'}, 404)
            except (OSError, ValueError, TypeError) as error:
                return self.respond({'error': str(error)}, 400)

    return Handler

class PreviewService(folder_studio.Service):
    def source_capabilities(self):
        return {'immichRename': False}


def main():
    global STATIC
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--static', type=Path, default=STATIC)
    parser.add_argument('--state', type=Path, default=Path.home() / 'Library/Caches/PhotoStudioPreviewSession')
    parser.add_argument('--apple-photos', choices=('mock', 'native'), default='mock',
                        help='native enables explicit PhotoKit connection; startup does not request permission')
    args = parser.parse_args()
    STATIC = args.static.resolve()
    if not (STATIC / 'index.html').is_file():
        parser.error('Build the isolated static frontend first')
    state = args.state.resolve()
    source = state.parent / (state.name + '-generated-photos')
    generate_photos(source)
    # Keep legacy imports and model exports isolated from production state.
    folder_studio.STATE = state
    folder_studio.PERSON_REVIEW_STATE = state / 'people'
    for name in ('CODEX_RESULTS', 'VISION_RESULTS', 'QWEN_RESULTS', 'AFS_RESULTS',
                 'QWEN_MORE', 'AFS_MORE', 'AFS_TRANSLATIONS'):
        setattr(folder_studio, name, state / (name.lower() + '.json'))
    service = PreviewService(state=state, source=source,
                             expected_volume=folder_studio.volume_uuid(source),
                             index_path=state / 'index.sqlite')
    service.origins = {'http://127.0.0.1:4173', 'http://localhost:4173'}
    web = ThreadingHTTPServer(('127.0.0.1', 4173), WebPreview)
    bridge = ThreadingHTTPServer(('127.0.0.1', 8767), folder_studio.make_handler(service))
    apple_service = ApplePhotosService(origins=service.origins) if args.apple_photos == 'native' else None
    apple_handler = make_apple_handler(apple_service) if apple_service else make_mock_apple_handler(source)
    apple_bridge = ThreadingHTTPServer(('127.0.0.1', 8768), apple_handler)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    threading.Thread(target=apple_bridge.serve_forever, daemon=True).start()
    print(f'Local preview: http://127.0.0.1:4173/apple-photos; Apple source: {args.apple_photos}', flush=True)
    try:
        web.serve_forever()
    finally:
        bridge.shutdown()
        bridge.server_close()
        apple_bridge.shutdown()
        apple_bridge.server_close()
        if apple_service:
            apple_service.disconnect()
        web.server_close()

if __name__ == '__main__':
    main()
