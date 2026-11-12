'''
测试公共设施

- 导入任何 bustag 模块之前把 BUSTAG_DATA_DIR 指到临时目录，测试不读写仓库里的 data/。
- 默认禁止联网（在 socket 层拦截），只有标了 live 的测试放行。
- 每个测试有自己的数据目录、空的数据表，结束后还原 APP_CONFIG。
'''
import contextlib
import copy
import io
import os
import shutil
import socket
import sqlite3
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
from bustag.spider.fetcher import NotFoundError, RequestCancelled  # noqa: E402
from bustag.spider.parser import Tag  # noqa: E402

FIXTURES = Path(__file__).parent / 'fixtures'
TABLES = (db.ActressAlias, db.Actress, db.ItemTag, db.ItemRate, db.ItemScore, db.ActressLookup, db.LocalItem,
          db.ItemCandidate, db.FetchTask, db.ListSource, db.JobState, db.StarLink, db.Tag, db.Item)
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


@pytest.fixture(autouse=True)
def fresh_request_spacing(monkeypatch):
    '''Fetcher 在模块里按网站记着上一次请求的时间，抓取队列记着上一次的结果和正在做的事：每个测试从空的开始'''
    from bustag.spider import backfill, fetcher

    monkeypatch.setattr(fetcher, '_last_sent', {})
    monkeypatch.setattr(backfill, 'last_run', None)
    monkeypatch.setattr(backfill, 'activity', None)
    monkeypatch.setattr(backfill, '_unrecorded', None)


@pytest.fixture(autouse=True)
def quick_validation(monkeypatch):
    '''
    训练时的交叉验证只重复 2 次。程序里重复 10 次，次数只影响检验结果的稳定程度；
    要训练的测试很多，这样快得多
    '''
    from bustag.model import classifier

    monkeypatch.setattr(classifier, 'CV_REPEATS', 2)


@pytest.fixture
def locked_database():
    '''
    with locked_database(): 这一段运行期间，别的程序占着数据库的写锁（另一条连接执行了 BEGIN IMMEDIATE）。
    程序等锁的时间从 30 秒改成 0.2 秒，等不到就是 database is locked
    '''
    @contextlib.contextmanager
    def locked():
        waiting = db.db.timeout
        db.db.timeout = 0.2
        other = sqlite3.connect(db.db.database, isolation_level=None)
        other.execute('BEGIN IMMEDIATE')
        try:
            yield
        finally:
            other.execute('ROLLBACK')
            other.close()
            db.db.timeout = waiting

    return locked


@pytest.fixture
def fill_requests(monkeypatch):
    '''
    网页请求里只登记"有影片要补演员名"，由后台任务去补。这里把登记换成记录：每登记一次，返回的列表多一项
    '''
    from bustag.app import index

    calls = []
    monkeypatch.setattr(index, 'request_actress_fill', lambda: calls.append('fill'))
    return calls


@pytest.fixture
def page():
    '''读取 tests/fixtures/ 下保存的网页样本'''
    def read(name):
        return (FIXTURES / name).read_text(encoding='utf-8')
    return read


def detail_html(fanhao, release='2026-01-01', stars=(), genres=('标签一',)):
    '''
    编造的详情页，只有 parser.parse_item 用到的结构：标题、識別碼、发行日期、长度、封面、類別和演員的链接。
    stars 是 (名字, 演员页路径) 的列表；release 为空时页面上没有发行日期
    '''
    links = ''.join(f'<span class="genre"><a href="/genre/g{n}">{name}</a></span>' for n, name in enumerate(genres, 1))
    links += ''.join(f'<span class="genre"><a href="{path}">{name}</a></span>' for name, path in stars)
    released = f'<p><span class="header">發行日期:</span> {release}</p>' if release else ''
    return (f'<html><body><div class="container"><h3>{fanhao} 示例影片 {fanhao}</h3>'
            f'<div class="row movie"><div class="col-md-9 screencap">'
            f'<a class="bigImage" href="/pics/cover/{fanhao.lower()}_b.jpg"><img src="/pics/cover/{fanhao.lower()}_b.jpg"></a>'
            f'</div><div class="col-md-3 info">'
            f'<p><span class="header">識別碼:</span> <span>{fanhao}</span></p>{released}'
            f'<p><span class="header">長度:</span> 120分鐘</p>'
            f'<p><span class="header">製作商:</span> <a href="/studio/s1">片商一</a></p>'
            f'<p>{links}</p></div></div></div></body></html>')


@pytest.fixture
def detail_page():
    '''返回生成编造详情页的函数 detail_html(番号, release=…, stars=…, genres=…)'''
    return detail_html


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


def avwiki_page(slug):
    '''编造的 av-wiki 女优页路径'''
    return f'/av-actress/{slug}/'


class Cast:
    '''
    编造演员数据的几个办法。地址 /star/<编号> 是 JavBus 的演员；/av-actress/<slug>/ 是 av-wiki 的女优页
    '''

    @staticmethod
    def movie(fanhao, *stars, genres=('标签一',)):
        '''入库一部没有评分的影片（像 JavBus 抓来的那样），stars 是 (名字, 地址)，返回 db.Item'''
        meta = {'fanhao': fanhao, 'title': f'示例影片 {fanhao}', 'url': '/' + fanhao, 'release_date': '2026-01-01',
                'cover_img_url': '', 'length': '120'}
        tags = [Tag('genre', name, f'/genre/{name}') for name in genres]
        item = db.save(meta, tags + [Tag('star', name, url) for name, url in stars])
        assert item is not None, f'{fanhao} 已经在库里'
        return item

    @staticmethod
    def legacy(fanhao, slug, *names):
        '''
        旧的存法：av-wiki 补名时这位女优的每个名字各是一条 star 标签（地址都是她的女优页），按顺序挂在这部影片上；
        名字已经有一条 star 标签（不管地址是什么）时共用那一条
        '''
        item = db.Item.get_by_fanhao(fanhao)
        for name in names:
            tag = db.Tag.saveit(Tag('star', name, avwiki_page(slug)))
            db.ItemTag.saveit(item, tag)

    @staticmethod
    def actress(name, slug=None, *aliases, javbus='', state=None):
        '''新的存法：一位女优，正式名字 name（没有这条 star 标签时新建，地址是她的女优页或 JavBus 演员页），别名 aliases'''
        url = avwiki_page(slug) if slug else javbus
        tag = db.Tag.saveit(Tag('star', name, url))
        return db.add_actress(tag, avwiki=avwiki_page(slug) if slug else None, javbus=javbus,
                              state=state or db.ACTRESS_PAGE, aliases=aliases)

    @staticmethod
    def link(fanhao, *actresses):
        '''把这几位女优的正式名字挂到影片上'''
        item = db.Item.get_by_fanhao(fanhao)
        for actress in actresses:
            db.ItemTag.saveit(item, db.Tag.get_by_id(db.Actress.get_by_id(actress.id).tag_id))

    @staticmethod
    def stars_of(fanhao):
        '''影片挂着的 star 标签的值，按挂上的先后'''
        query = (db.ItemTag.select(db.Tag.value).join(db.Tag)
                 .where((db.ItemTag.item == fanhao) & (db.Tag.type_ == 'star')).order_by(db.ItemTag.id))
        return [value for (value,) in query.tuples()]


@pytest.fixture
def cast():
    '''编造演员数据的办法，见 Cast'''
    return Cast


@pytest.fixture
def sql_folds():
    '''SQL 里的 bustag_fold(列) 被调用的值：每调一次就是 SQLite 为一行调回 Python 一次'''
    calls = []
    connection = db.db.connection()

    def counting(value):
        calls.append(value)
        return db._fold_in_sql(value)

    connection.create_function('bustag_fold', 1, counting, deterministic=True)
    yield calls
    connection.create_function('bustag_fold', 1, db._fold_in_sql, deterministic=True)


@pytest.fixture
def queries(monkeypatch):
    '''从这里开始执行的每一条 SQL：[(语句, 参数的元组)]'''
    seen = []
    real = db.db.execute_sql

    def recording(sql, params=None, *args, **kwargs):
        seen.append((sql, tuple(params or ())))
        return real(sql, params, *args, **kwargs)

    monkeypatch.setattr(db.db, 'execute_sql', recording)
    return seen


@pytest.fixture
def write_model(data_dir):
    '''
    在数据目录里写一份能用的模型文件，返回文件路径。tags 是"标签值 → (idf, 系数)"，
    report 里写要换掉的检验结果（likes、dislikes、like_recall 等）
    '''
    from bustag.model import persist

    def write(tags=None, intercept=0.0, **report):
        fields = {'likes': 150, 'dislikes': 60, 'folds': 5, 'repeats': 10, 'like_recall': 0.9,
                  'dislike_recall': 0.8, 'auc': 0.95, **report}
        model = persist.SavedModel(intercept, tags or {'g1': (1.0, 1.0), 's1': (1.0, -1.0)},
                                   persist.Report(**fields), '2026-11-06 12:00:00')
        path = data_dir / persist.MODEL_FILE
        persist.save_model(str(path), model)
        return path
    return write


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

    def request(self, method, path, body=b'', content_type=None, headers=None, allow_server_error=False,
                environ=None):
        '''environ 里是服务器自己放进 WSGI 环境的东西（例如 waitress.client_disconnected）'''
        environ = dict(environ or {})
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

    def get(self, path, headers=None, allow_server_error=False, environ=None):
        return self.request('GET', path, headers=headers, allow_server_error=allow_server_error, environ=environ)

    def post(self, path, data=None, headers=None, allow_server_error=False):
        '''data 是字典，或者 (名字, 值) 的列表（同一个名字要提交几个值时）'''
        body = urlencode(data or {}).encode('utf-8')
        return self.request('POST', path, body, 'application/x-www-form-urlencoded', headers, allow_server_error)

    def post_multipart(self, path, parts, headers=None):
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
                            f'multipart/form-data; boundary={boundary}', headers)


@pytest.fixture
def wsgi():
    '''调用 bustag.app.index.app 的客户端'''
    from bustag.app.index import app
    return WSGIClient(app)


@pytest.fixture
def served():
    '''调用交给 WSGI 服务器的那个应用（bustag.app.index.application）的客户端'''
    from bustag.app.index import application
    return WSGIClient(application)


class FakeFetcher:
    '''
    代替 Fetcher：按路径（或完整地址）返回 routes 里的内容，并按顺序记录请求。
    值可以是 html 字符串、(字节, content_type)、异常实例，或接收路径、返回前三者之一的函数；
    routes 里没有时用 default，default 也没有就抛 404 的 NotFoundError。
    和 Fetcher 一样：发请求之前先"等间隔"（waiting 不空时调一下，测试里在这期间做别的线程会做的事，例如网页上点取消），
    等完以后再问 get_html 的 proceed，说不发就抛 RequestCancelled、不记这个请求
    '''

    def __init__(self, routes=None, default=None, root_path='https://www.javbus.com'):
        self.routes = dict(routes or {})
        self.default = default
        self.root_path = root_path
        self.requests = []
        self.referers = []
        self.closed = False
        self.waiting = None

    def url_for(self, path_or_url):
        return urljoin(self.root_path + '/', path_or_url)

    def _respond(self, key, referer):
        self.requests.append(key)
        self.referers.append(referer)
        value = self.routes.get(key, self.default)
        if callable(value):
            value = value(key)
        if value is None:
            raise NotFoundError(f'页面不存在（HTTP 404）：{self.url_for(key)}')
        if isinstance(value, BaseException):
            raise value
        return value

    def get_html(self, path_or_url, referer=None, proceed=None):
        if self.waiting is not None:
            self.waiting(path_or_url)
        if proceed is not None and not proceed():
            raise RequestCancelled(f'请求取消了：{path_or_url}')
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
    并换掉按线程缓存 fetcher 的 threading.local、失败记录、连不上的图片站的记录和下载中的番号表。
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
    monkeypatch.setattr(covers, '_unreachable', {})
    monkeypatch.setattr(covers, '_inflight', {})
    return fake
