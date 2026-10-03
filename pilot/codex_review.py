"""Run one explicitly approved, downsized image through ChatGPT-authenticated Codex.

The caller supplies only a temporary preview. This module never receives the
source folder path and refuses API-key authentication to avoid metered billing.
"""

import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path


CODEX_APP = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
CATEGORIES = {'人物', '地点', '活动', '物品', '截图', '其他'}
SCHEMA = {
    'type': 'object',
    'properties': {
        'short': {'type': 'string'},
        'long': {'type': 'string'},
        'category': {'type': 'string'},
        'people': {'type': 'string'},
        'uncertainty': {'type': 'string'},
    },
    'required': ['short', 'long', 'category', 'people', 'uncertainty'],
    'additionalProperties': False,
}
PROMPT = ('只分析随本条消息附上的图片，不调用工具，不查找其他文件。'
          '用简体中文输出 JSON：short 为 2 到 10 字的建议短名，long 为最多 80 字的可见画面描述，'
          'category 只能是人物、地点、活动、物品、截图、其他，people 为可见人数或无法判断，'
          'uncertainty 写不确定之处。不要猜测姓名、具体身份或无法从画面证实的地点。')


def codex_binary():
    binary = shutil.which('codex')
    if binary:
        return binary
    if CODEX_APP.is_file():
        return str(CODEX_APP)
    raise ValueError('未找到本机 Codex 程序')


def normalize_result(message):
    text = message.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    fields = json.loads(text)
    if not isinstance(fields, dict) or set(fields) != set(SCHEMA['required']):
        raise ValueError('Codex 返回字段不完整')
    if any(not isinstance(value, str) for value in fields.values()):
        raise ValueError('Codex 返回字段类型错误')
    if not fields['short'].strip() or not fields['long'].strip() or fields['category'] not in CATEGORIES:
        raise ValueError('Codex 返回内容无效')
    return {'model': 'Codex / ChatGPT', **{key: value.strip() for key, value in fields.items()}}


class AppServer:
    def __init__(self, binary, cwd, timeout):
        self.process = subprocess.Popen([binary, 'app-server', '--stdio'], cwd=cwd,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
        os.set_blocking(self.process.stdout.fileno(), False)
        self.pending = b''
        self.messages = []
        self.deadline = time.monotonic() + timeout
        self.next_id = 0

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except OSError:
                pass

    def send(self, method, params=None, *, request=True):
        message = {'method': method, 'params': params or {}}
        if request:
            self.next_id += 1
            message['id'] = self.next_id
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        self.process.stdin.flush()
        return message.get('id')

    def receive(self):
        while not self.messages:
            if self.process.poll() is not None:
                raise ValueError('Codex 服务意外退出')
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Codex 分析超时')
            readable, _, _ = select.select([self.process.stdout], [], [], min(remaining, 1))
            if not readable:
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ValueError('Codex 服务关闭连接')
            self.pending += chunk
            while b'\n' in self.pending:
                line, self.pending = self.pending.split(b'\n', 1)
                if line:
                    self.messages.append(json.loads(line))
        return self.messages.pop(0)

    def response(self, request_id):
        while True:
            message = self.receive()
            if message.get('id') == request_id:
                if 'error' in message:
                    raise ValueError('Codex 接口错误：' + str(message['error'].get('message')))
                return message['result']
            if 'id' in message and 'method' in message:
                # A photo-description request must not request interactive tools.
                self.send_error(message['id'])

    def send_error(self, request_id):
        self.process.stdin.write((json.dumps({'id': request_id, 'error': {
            'code': -32000, 'message': 'Photo review does not permit tools'}}) + '\n').encode())
        self.process.stdin.flush()


def analyze_preview(preview, *, timeout=240):
    """Return validated structured output, or fail without changing any photo."""
    preview = Path(preview).resolve()
    if not preview.is_file() or preview.suffix.lower() not in {'.jpg', '.jpeg', '.png'}:
        raise ValueError('Codex 只能接收已生成的临时图片预览')
    server = AppServer(codex_binary(), str(preview.parent), timeout)
    try:
        server.response(server.send('initialize', {'clientInfo': {
            'name': 'local_photo_studio', 'title': 'Local Photo Studio', 'version': '0.1.0'}}))
        server.send('initialized', request=False)
        account = server.response(server.send('account/read'))
        if (account.get('account') or {}).get('type') != 'chatgpt':
            raise ValueError('仅允许 ChatGPT 登录；API Key 可能产生额外费用，已拒绝')
        seed = server.response(server.send('thread/start', {
            'cwd': str(preview.parent), 'approvalPolicy': 'never',
            'sandbox': 'readOnly', 'serviceName': 'local_photo_studio'}))['thread']
        thread = server.response(server.send('thread/fork', {
            'threadId': seed['id'], 'ephemeral': True}))['thread']
        request_id = server.send('turn/start', {
            'threadId': thread['id'],
            'input': [{'type': 'text', 'text': PROMPT},
                      {'type': 'localImage', 'path': str(preview)}],
            'cwd': str(preview.parent),
            'sandboxPolicy': {'type': 'readOnly', 'access': {
                'type': 'restricted', 'includePlatformDefaults': True,
                'readableRoots': [str(preview.parent)]}},
            'approvalPolicy': 'never', 'outputSchema': SCHEMA,
        })
        server.response(request_id)
        message_text = None
        while True:
            event = server.receive()
            if 'id' in event and 'method' in event:
                server.send_error(event['id'])
                raise ValueError('Codex 请求了额外工具权限，已终止')
            item = event.get('params', {}).get('item', {})
            if event.get('method') == 'item/started' and item.get('type') in {
                    'commandExecution', 'fileChange', 'dynamicToolCall'}:
                raise ValueError('Codex 尝试调用工具，已终止')
            if event.get('method') == 'item/completed' and item.get('type') == 'agentMessage':
                message_text = item.get('text')
            if event.get('method') == 'turn/completed':
                turn = event.get('params', {}).get('turn', {})
                if turn.get('status') != 'completed':
                    raise ValueError('Codex 分析未完成：' + str((turn.get('error') or {}).get('message')))
                if not message_text:
                    raise ValueError('Codex 未返回可用结果')
                result = normalize_result(message_text)
                # The seed contains no photo; the photo turn lived only in the
                # in-memory fork, so batch work does not flood Codex's chat list.
                try:
                    server.response(server.send('thread/delete', {'threadId': seed['id']}))
                except (ValueError, TimeoutError):
                    pass
                return result
    finally:
        server.close()
