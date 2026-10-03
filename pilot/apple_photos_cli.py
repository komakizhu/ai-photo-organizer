"""Command-line client for the same read-only Apple Photos API used by the web UI."""
import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from apple_photos_api import RUNTIME, TOKEN_FILE, read_token, write_token


BASE = 'http://127.0.0.1:8768/v1/'
ORIGIN = 'http://127.0.0.1:2283'


def request(command, body=None, token=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    headers = {'Origin': ORIGIN, 'Accept': 'application/json'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    if token:
        headers['X-Photo-Studio-Token'] = token
    method = 'GET' if data is None else 'POST'
    try:
        with urlopen(Request(BASE + command, data=data, headers=headers, method=method), timeout=320) as response:
            content_type = response.headers.get_content_type()
            return json.loads(response.read()) if content_type == 'application/json' else {'ok': True}
    except HTTPError as error:
        try:
            detail = json.loads(error.read())
        except (ValueError, OSError):
            detail = {'error': str(error)}
        raise RuntimeError(detail.get('error', str(error))) from error
    except URLError as error:
        raise RuntimeError('Apple 图库本地服务未运行；请从照片整理中心点击 Apple 图库，或先启动本地服务。') from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='读取连接状态，不启动 PhotoKit 组件')
    sub.add_parser('connect', help='主动连接并请求 macOS 照片权限')
    assets = sub.add_parser('assets', help='读取一页已授权照片元数据')
    assets.add_argument('--screenshots', action='store_true')
    assets.add_argument('--start-date')
    assets.add_argument('--end-date')
    assets.add_argument('--cursor', help='上次响应中的 nextCursor JSON')
    sub.add_parser('cancel', help='取消当前加载并清除临时查询')
    sub.add_parser('disconnect', help='结束 PhotoKit 会话')
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            result = request('status')
        elif args.command == 'connect':
            result = request('connect', {})
            if result.get('token'):
                write_token(TOKEN_FILE, result['token'])
                result.pop('token', None)
        else:
            token = read_token(TOKEN_FILE)
            if not token:
                raise RuntimeError('没有有效的照片会话；请先运行 connect。')
            if args.command == 'assets':
                cursor = json.loads(args.cursor) if args.cursor else None
                result = request('assets', {'screenshots': args.screenshots,
                                            'startDate': args.start_date,
                                            'endDate': args.end_date,
                                            'cursor': cursor}, token)
            elif args.command == 'cancel':
                result = request('cancel', {}, token)
            else:
                result = request('disconnect', {}, token)
                TOKEN_FILE.unlink(missing_ok=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
