"""Standalone local API for read-only Apple Photos browsing."""
import argparse
import base64
import json
import os
import secrets
import socket
import stat
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


APP_PATH = Path.home() / 'Library/Caches/LocalPhotoStudio/ApplePhotosBridge/ApplePhotosBridge.app'
RUNTIME = Path.home() / 'Library/Caches/LocalPhotoStudio/ApplePhotosBridge/runtime'
SOCKET = RUNTIME / 'bridge.sock'
TOKEN_FILE = RUNTIME / 'session-token'
DEFAULT_ORIGINS = {'http://127.0.0.1:2283', 'http://localhost:2283',
                   'http://127.0.0.1:3000', 'http://localhost:3000'}
MAX_BODY = 1_000_000


def secure_runtime(runtime=RUNTIME):
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    return runtime


def write_token(path, token):
    path = Path(path)
    secure_runtime(path.parent)
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(token)
    os.replace(temporary, path)
    path.chmod(0o600)


def read_token(path):
    path = Path(path)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            return None
        return path.read_text(encoding='utf-8')
    except OSError:
        return None


class ApplePhotosService:
    def __init__(self, *, app_path=APP_PATH, runtime=RUNTIME, origins=None,
                 port=8768, native_call=None, launcher=None):
        self.app_path = Path(app_path)
        self.runtime = secure_runtime(Path(runtime))
        self.socket_path = self.runtime / 'bridge.sock'
        self.token_path = self.runtime / 'session-token'
        self.origins = set(origins or DEFAULT_ORIGINS)
        self.port = port
        self.allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        self.token = None
        self.connected = False
        self.authorization = 'unknown'
        self.native_call = native_call or self._native_call
        self.launcher = launcher or self._launch_native
        self.lock = threading.RLock()
        self.thumbnail_slots = threading.BoundedSemaphore(4)

    def _launch_native(self):
        if not self.app_path.is_dir():
            raise RuntimeError('PhotoKit 组件尚未构建；请运行 pilot/build_apple_photos_bridge.sh')
        subprocess.Popen(['open', '-gj', str(self.app_path), '--args', str(self.socket_path)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)

    def _native_call(self, request, timeout=10):
        payload = json.dumps(request, ensure_ascii=False).encode('utf-8') + b'\n'
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        try:
            deadline = time.monotonic() + min(timeout, 10)
            while True:
                try:
                    client.connect(str(self.socket_path))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError('PhotoKit 后台组件未能启动；请检查构建状态')
                    time.sleep(0.1)
            client.sendall(payload)
            chunks = bytearray()
            while len(chunks) <= 8_000_000:
                block = client.recv(65536)
                if not block:
                    break
                chunks.extend(block)
                if b'\n' in block:
                    break
            line = bytes(chunks).split(b'\n', 1)[0]
            if not line:
                raise RuntimeError('PhotoKit 组件返回空响应')
            return json.loads(line)
        finally:
            client.close()

    def status(self):
        with self.lock:
            if not self.connected:
                return {'connected': False, 'authorization': self.authorization,
                        'mode': 'native',
                        'message': '点击连接 Apple 图库后，macOS 会请求照片访问权限。' if self.authorization == 'unknown' else self.permission_message(self.authorization)}
            try:
                result = self.native_call({'command': 'status'})
            except (OSError, RuntimeError) as error:
                self._clear_session(close_native=False)
                return {'connected': False, 'authorization': 'unknown', 'error': str(error)}
            if not result.get('ok'):
                return {'connected': False, 'authorization': 'unknown',
                        'error': result.get('error', '无法读取权限状态')}
            status = result.get('status', 'unknown')
            self.authorization = status
            if status not in ('authorized', 'limited'):
                self._clear_session(close_native=True)
            return {'connected': status in ('authorized', 'limited'),
                    'mode': 'native',
                    'authorization': status,
                    'message': self.permission_message(status)}

    @staticmethod
    def permission_message(status):
        return {
            'not_determined': '点击连接后，macOS 会请求照片访问权限。',
            'denied': '照片访问已拒绝；请在系统设置的隐私与安全性中为 Apple Photos Bridge 开启权限。',
            'restricted': '此 Mac 当前限制了照片图库访问。',
            'authorized': '已获照片图库访问权限。',
            'limited': '当前仅能访问部分照片。',
        }.get(status, '照片图库权限状态未知。')

    def connect(self):
        with self.lock:
            self.launcher()
            response = self.native_call({'command': 'connect'}, timeout=310)
            status = response.get('status', 'unknown')
            self.authorization = status
            if not response.get('ok'):
                raise RuntimeError(response.get('error', '照片授权失败'))
            if status not in ('authorized', 'limited'):
                self.connected = False
                self.token = None
                self.token_path.unlink(missing_ok=True)
                try:
                    self.native_call({'command': 'disconnect'})
                except (OSError, RuntimeError):
                    pass
                return {'connected': False, 'authorization': status,
                        'message': self.permission_message(status)}
            self.token = secrets.token_urlsafe(32)
            write_token(self.token_path, self.token)
            self.connected = True
            return {'connected': True, 'authorization': status,
                    'mode': 'native',
                    'message': self.permission_message(status), 'token': self.token}

    def _clear_session(self, *, close_native):
        with self.lock:
            self._clear_session_locked(close_native=close_native)

    def _clear_session_locked(self, *, close_native):
        if close_native and self.connected:
            try:
                self.native_call({'command': 'disconnect'})
            except (OSError, RuntimeError):
                pass
        self.connected = False
        self.token = None
        self.token_path.unlink(missing_ok=True)

    def disconnect(self):
        with self.lock:
            self._clear_session_locked(close_native=True)
        return {'connected': False, 'authorization': self.authorization}

    def authorized(self, supplied):
        with self.lock:
            stored = read_token(self.token_path)
            return bool(self.connected and self.token and stored == self.token and
                        secrets.compare_digest(supplied or '', self.token))

    def call_native(self, command, **values):
        if command == 'thumbnail':
            if not self.thumbnail_slots.acquire(timeout=10):
                raise TimeoutError('缩略图请求过多，请稍后重试')
            try:
                response = self.native_call({'command': command, **values}, timeout=50)
            finally:
                self.thumbnail_slots.release()
        else:
            response = self.native_call({'command': command, **values}, timeout=40)
        if not response.get('ok'):
            code = response.get('code', 400)
            if code == 403:
                self._clear_session(close_native=True)
            raise ApplePhotosError(response.get('error', 'PhotoKit 请求失败'), code)
        return response


class ApplePhotosError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status if status in (400, 403, 404, 409, 424, 503) else 400


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            # Asset identifiers and request bodies may contain private library data.
            return

        def send_json(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            origin = self.headers.get('Origin')
            if origin in service.origins:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Photo-Studio-Token')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Vary', 'Origin')
            self.end_headers()
            self.wfile.write(body)

        def send_error_json(self, error):
            status = error.status if isinstance(error, ApplePhotosError) else 400
            self.send_json({'error': str(error)}, status)

        def _valid_origin(self):
            origin = self.headers.get('Origin')
            if origin not in service.origins:
                return False
            host = self.headers.get('Host', '').lower()
            return host in service.allowed_hosts and self.client_address[0] in ('127.0.0.1', '::1')

        def do_OPTIONS(self):
            if not self._valid_origin():
                return self.send_json({'error': 'origin or host not allowed'}, 403)
            self.send_json({}, 200)

        def do_GET(self):
            if not self._valid_origin():
                return self.send_json({'error': 'origin or host not allowed'}, 403)
            if self.path != '/v1/status':
                return self.send_json({'error': 'not found'}, 404)
            return self.send_json(service.status())

        def do_POST(self):
            if not self._valid_origin():
                return self.send_json({'error': 'origin or host not allowed'}, 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 1 or length > MAX_BODY:
                    raise ApplePhotosError('请求大小无效', 400)
                request = json.loads(self.rfile.read(length))
                command = self.path.removeprefix('/v1/')
                if command == 'connect':
                    return self.send_json(service.connect())
                token_required = command in ('assets', 'thumbnail', 'cancel', 'disconnect')
                if command not in ('assets', 'thumbnail', 'cancel', 'disconnect'):
                    return self.send_json({'error': 'not found'}, 404)
                if token_required and not service.authorized(self.headers.get('X-Photo-Studio-Token')):
                    return self.send_json({'error': '照片会话未授权'}, 403)
                if command == 'disconnect':
                    service.disconnect()
                    return self.send_json({'connected': False})
                if command == 'cancel':
                    service.call_native('cancel')
                    return self.send_json({'cancelled': True})
                if command == 'assets':
                    cursor = request.get('cursor')
                    if cursor is not None and not isinstance(cursor, dict):
                        raise ApplePhotosError('分页游标无效', 400)
                    if not isinstance(request.get('screenshots', False), bool):
                        raise ApplePhotosError('截图筛选值无效', 400)
                    for key in ('startDate', 'endDate'):
                        if request.get(key) is not None and not isinstance(request[key], str):
                            raise ApplePhotosError('日期格式无效', 400)
                    response = service.call_native(
                        'assets', screenshots=bool(request.get('screenshots', False)),
                        startDate=request.get('startDate') or '', endDate=request.get('endDate') or '',
                        cursor=cursor)
                    return self.send_json({key: value for key, value in response.items() if key != 'generation'})
                asset_id = request.get('assetId')
                if not isinstance(asset_id, str) or not asset_id or len(asset_id) > 1024:
                    raise ApplePhotosError('照片标识无效', 400)
                response = service.call_native('thumbnail', assetId=asset_id)
                data = base64.b64decode(response['data'], validate=True)
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                origin = self.headers.get('Origin')
                if origin in service.origins:
                    self.send_header('Access-Control-Allow-Origin', origin)
                    self.send_header('Vary', 'Origin')
                self.end_headers()
                self.wfile.write(data)
            except (ApplePhotosError, TimeoutError, OSError, ValueError, RuntimeError) as error:
                return self.send_error_json(error)

    return Handler


def serve(port=8768, origins=None, app_path=APP_PATH, runtime=RUNTIME):
    service = ApplePhotosService(port=port, origins=origins, app_path=app_path, runtime=runtime)
    server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(service))
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        service.disconnect()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--origin', action='append', dest='origins')
    args = parser.parse_args()
    try:
        serve(port=args.port, origins=args.origins)
    except OSError as error:
        if error.errno == 48:
            parser.error(f'端口 {args.port} 已被占用；未终止或修改其他进程')
        raise


if __name__ == '__main__':
    main()
