'''
测试公共设施

- 导入任何 bustag 模块之前把 BUSTAG_DATA_DIR 指到临时目录，测试不读写仓库里的 data/。
- 默认禁止联网（在 socket 层拦截），只有标了 live 的测试放行。
- 每个测试有自己的数据目录、空的数据表，结束后还原 APP_CONFIG。
'''
import copy
import io
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode, urljoin
from wsgiref.util import setup_testing_defaults

import pytest

SESSION_DATA_DIR = tempfile.mkdtemp(prefix='bustag-test-')
os.environ['BUSTAG_DATA_DIR'] = SESSION_DATA_DIR

from bustag import util  # noqa: E402
from bustag.spider import db  # noqa: E402
from bustag.spider.fetcher import FetchError  # noqa: E402
from bustag.spider.parser import Tag  # noqa: E402

FIXTURES = Path(__file__).parent / 'fixtures'
TABLES = (db.ItemTag, db.ItemRate, db.LocalItem, db.Tag, db.Item)
PROXY_VARS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
              'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')
SOCKET_FUNCTIONS = ('create_connection', 'getaddrinfo', 'gethostbyname',
                    'gethostbyname_ex', 'gethostbyaddr')


class NetworkBlocked(RuntimeError):
    '''
    离线测试里出现了真实的网络访问。故意不继承 OSError：requests / urllib3 会把
    OSError 当成网络错误吞掉或重试，这里要让它直接冒出来
    '''


@pytest.fixture(scope='session', autouse=True)
def _session_data_dir():
    yield Path(SESSION_DATA_DIR)
    db.db.close()
    shutil.rmtree(SESSION_DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    '''
    禁止联网：清掉代理环境变量并设 NO_PROXY=*（requests 不再查系统代理设置，
    Windows 上查注册表代理例外时会做 DNS 查询），socket 的连接和域名解析一律抛
    NetworkBlocked。返回尝试记录的列表；测试结束时列表不为空就判失败
    '''
    attempts = []
    if request.node.get_closest_marker('live'):
        yield attempts
        return
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('NO_PROXY', '*')

    def guard(name):
        def blocked(*args, **kwargs):
            target = next((a for a in args if not isinstance(a, socket.socket)), None)
            attempts.append(f'{name}({target!r})')
            raise NetworkBlocked(f'测试里不允许联网：{name}({target!r})')
        return blocked

    monkeypatch.setattr(socket.socket, 'connect', guard('socket.connect'))
    monkeypatch.setattr(socket.socket, 'connect_ex', guard('socket.connect_ex'))
    for name in SOCKET_FUNCTIONS:
        monkeypatch.setattr(socket, name, guard('socket.' + name))
    yield attempts
    assert not attempts, f'测试尝试联网：{attempts}'


def _clear_tables():
    with db.db.atomic():
        for model in TABLES:
            model.delete().execute()


@pytest.fixture
def empty_tables():
    '''清空全部数据表，测试结束后再清一次'''
    _clear_tables()
    yield
    _clear_tables()


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch, empty_tables):
    '''
    每个测试独立的数据目录（含 model/），数据表为空；结束后还原 APP_CONFIG。
    数据库文件只在会话开始时建一次，放在会话临时目录里
    '''
    path = tmp_path / 'data'
    path.mkdir()
    monkeypatch.setattr(util, 'DATA_DIR', str(path))
    util.check_model_folder()
    saved_config = copy.deepcopy(util.APP_CONFIG)
    yield path
    util.APP_CONFIG.clear()
    util.APP_CONFIG.update(saved_config)


@pytest.fixture
def page():
    '''读取 tests/fixtures/ 下保存的网页样本'''
    def read(name):
        return (FIXTURES / name).read_text(encoding='utf-8')
    return read


@pytest.fixture
def make_item():
    '''
    入库一部影片并返回 db.Item。tags 是 (类型, 值) 的列表
    '''
    def make(fanhao, *, title=None, release_date='2026-01-01', cover=None, length='120', tags=()):
        meta = {
            'fanhao': fanhao,
            'title': title or f'作品 {fanhao}',
            'url': '/' + fanhao,
            'release_date': release_date,
            'cover_img_url': f'/pics/cover/{fanhao.lower()}_b.jpg' if cover is None else cover,
            'length': length,
        }
        tag_list = [Tag(type_, value, f'/{type_.rstrip(":")}/{value}') for type_, value in tags]
        item = db.save(meta, tag_list)
        assert item is not None, f'{fanhao} 已经在库里'
        return item
    return make


class WSGIResponse:
    def __init__(self, status, headers, body, errors):
        self.status = status
        self.headers = headers
        self.body = body
        self.errors = errors

    @property
    def text(self):
        return self.body.decode('utf-8')


class WSGIClient:
    '''
    直接调用 WSGI 应用，不开端口。响应头名转成小写。5xx 直接判失败（allow_server_error=True 时除外），
    并附上应用写到 wsgi.errors 的异常栈
    '''

    def __init__(self, app):
        self.app = app

    def request(self, method, path, body=b'', content_type=None, headers=None, allow_server_error=False):
        environ = {}
        setup_testing_defaults(environ)
        path_part, _, query = path.partition('?')
        errors = io.StringIO()
        environ.update({
            'REQUEST_METHOD': method,
            'PATH_INFO': path_part,
            'QUERY_STRING': query,
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'wsgi.input': io.BytesIO(body),
            'wsgi.errors': errors,
            'CONTENT_LENGTH': str(len(body)),
        })
        if content_type:
            environ['CONTENT_TYPE'] = content_type
        for name, value in (headers or {}).items():
            environ['HTTP_' + name.upper().replace('-', '_')] = value
        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured['status'] = status
            captured['headers'] = response_headers

        result = self.app(environ, start_response)
        try:
            content = b''.join(result)
        finally:
            if hasattr(result, 'close'):
                result.close()
        status = int(captured['status'].split()[0])
        response = WSGIResponse(status, {k.lower(): v for k, v in captured['headers']},
                                content, errors.getvalue())
        if not allow_server_error:
            assert status < 500, f'{method} {path} -> {captured["status"]}\n{response.errors}'
        return response

    def get(self, path, headers=None, allow_server_error=False):
        return self.request('GET', path, headers=headers, allow_server_error=allow_server_error)

    def post(self, path, data=None, headers=None):
        body = urlencode(data or {}).encode('utf-8')
        return self.request('POST', path, body, 'application/x-www-form-urlencoded', headers)

    def post_multipart(self, path, parts):
        '''
        parts 是 [(name, value)]，按表单里元素的先后排列（浏览器按这个顺序提交）。
        value 是字符串时为普通字段，是 (文件名, 字节) 时为上传的文件
        '''
        boundary = 'bustag-test-boundary'
        out = io.BytesIO()
        for name, value in parts:
            if isinstance(value, tuple):
                filename, content = value
                out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                          f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                          .encode('utf-8'))
                out.write(content)
                out.write(b'\r\n')
            else:
                out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                          f'{value}\r\n'.encode('utf-8'))
        out.write(f'--{boundary}--\r\n'.encode('utf-8'))
        return self.request('POST', path, out.getvalue(),
                            f'multipart/form-data; boundary={boundary}')


@pytest.fixture
def wsgi():
    '''调用 bustag.app.index.app 的客户端'''
    from bustag.app.index import app
    return WSGIClient(app)


class FakeFetcher:
    '''
    代替 Fetcher：按路径（或完整地址）返回 routes 里的内容，并按顺序记录请求。
    值可以是 html 字符串、(字节, content_type)、异常实例，或接收路径、返回前三者之一的函数；
    routes 里没有时用 default，default 也没有就抛 404 的 FetchError
    '''

    def __init__(self, routes=None, default=None, root_path='https://www.javbus.com'):
        self.routes = dict(routes or {})
        self.default = default
        self.root_path = root_path
        self.requests = []
        self.referers = []
        self.closed = False

    def url_for(self, path_or_url):
        return urljoin(self.root_path + '/', path_or_url)

    def _respond(self, key, referer):
        self.requests.append(key)
        self.referers.append(referer)
        value = self.routes.get(key, self.default)
        if callable(value):
            value = value(key)
        if value is None:
            raise FetchError(f'页面不存在（HTTP 404）：{self.url_for(key)}')
        if isinstance(value, BaseException):
            raise value
        return value

    def get_html(self, path_or_url, referer=None):
        return self._respond(path_or_url, referer)

    def get_bytes(self, url, referer=None):
        return self._respond(url, referer)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_fetcher():
    '''返回 FakeFetcher 类：fake_fetcher({'/': html, '/ABC-123': html})'''
    return FakeFetcher


@pytest.fixture
def cover_fetcher(monkeypatch):
    '''
    封面下载改走一个 FakeFetcher（routes 为空，测试里按完整地址添加），
    并换掉按线程缓存 fetcher 的 threading.local、失败记录和下载中的番号表。
    create_fetcher 收到的参数记在 created
    '''
    from bustag.spider import covers

    fake = FakeFetcher()
    fake.created = []

    def create_fetcher(**overrides):
        fake.created.append(overrides)
        return fake

    monkeypatch.setattr(covers, 'create_fetcher', create_fetcher)
    monkeypatch.setattr(covers, '_local', threading.local())
    monkeypatch.setattr(covers, '_failures', {})
    monkeypatch.setattr(covers, '_inflight', {})
    return fake
