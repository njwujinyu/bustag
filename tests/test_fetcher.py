'''
Fetcher：替换 requests 的传输层模拟网站响应，重定向、cookie 作用域、代理合并都走 requests 自己的代码；
time 和 random 换成假对象，重试退避和限速不真的等待
'''
import io
from dataclasses import dataclass, field

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.response import HTTPResponse

from bustag import util
from bustag.spider import fetcher as fetcher_module
from bustag.spider.fetcher import (
    HTML_ACCEPT, IMAGE_ACCEPT, NETWORK_HINT, USER_AGENT, BlockedError, Fetcher, FetchError,
    create_fetcher,
)

ROOT = 'https://www.javbus.com'
VERIFY_URL = 'https://www.javbus.com/doc/driver-verify?referer=https%3A%2F%2Fwww.javbus.com%2F'


@dataclass
class Reply:
    status: int = 200
    body: bytes = b''
    headers: dict = field(default_factory=dict)


@dataclass
class Sent:
    url: str
    headers: dict
    proxies: dict
    timeout: object


class FakeAdapter(HTTPAdapter):
    '''
    按完整地址返回预置响应并记录发出的请求。routes 的值是 Reply、异常实例，
    或它们的列表（按顺序逐个返回，最后一个重复使用）。没有预置的地址直接判失败
    '''

    def __init__(self, routes):
        super().__init__()
        self.routes = {url: list(v) if isinstance(v, list) else [v] for url, v in routes.items()}
        self.sent = []
        self.closed = False

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        self.sent.append(Sent(request.url, dict(request.headers), dict(proxies or {}), timeout))
        if request.url not in self.routes:
            raise AssertionError(f'没有预置响应的地址：{request.url}')
        replies = self.routes[request.url]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, BaseException):
            raise reply
        raw = HTTPResponse(body=io.BytesIO(reply.body), headers=reply.headers, status=reply.status,
                           preload_content=False, decode_content=False)
        return self.build_response(request, raw)

    def close(self):
        self.closed = True
        super().close()


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeRandom:
    '''抖动总是取上限，方便算出确切的等待时间'''

    def __init__(self):
        self.calls = []

    def uniform(self, low, high):
        self.calls.append((low, high))
        return high


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(fetcher_module, 'time', fake)
    return fake


@pytest.fixture(autouse=True)
def jitter(monkeypatch):
    fake = FakeRandom()
    monkeypatch.setattr(fetcher_module, 'random', fake)
    return fake


def make_fetcher(routes, **kwargs):
    kwargs.setdefault('delay', 0)
    fetcher = Fetcher(kwargs.pop('root_path', ROOT), **kwargs)
    adapter = FakeAdapter(routes)
    fetcher.session.mount('https://', adapter)
    fetcher.session.mount('http://', adapter)
    return fetcher, adapter


def html(text='<html></html>', status=200, headers=None):
    return Reply(status, text.encode('utf-8'), headers or {'Content-Type': 'text/html; charset=utf-8'})


def cookie_names(sent):
    header = sent.headers.get('Cookie')
    return set(header.split('; ')) if header else set()


class TestRequest:
    def test_html_request_headers_and_cookies(self):
        fetcher, adapter = make_fetcher({ROOT + '/': html('<p>ok</p>')})
        assert fetcher.get_html('/') == '<p>ok</p>'
        sent = adapter.sent[0]
        assert sent.headers['User-Agent'] == USER_AGENT
        assert sent.headers['Accept'] == HTML_ACCEPT
        assert sent.headers['Accept-Language'] == 'zh-CN,zh;q=0.9'
        assert 'Referer' not in sent.headers
        assert cookie_names(sent) == {'dv=1', 'existmag=mag'}
        assert sent.timeout == 20

    def test_cookies_are_sent_only_to_the_site_host(self):
        image = Reply(200, b'\xff\xd8' + b'0' * 2000, {'Content-Type': 'image/jpeg'})
        fetcher, adapter = make_fetcher({
            'https://pics.dmm.co.jp/digital/video/a/apl.jpg': image,
            ROOT + '/pics/cover/abc_b.jpg': image,
        })
        fetcher.get_bytes('https://pics.dmm.co.jp/digital/video/a/apl.jpg', referer=ROOT + '/')
        fetcher.get_bytes('/pics/cover/abc_b.jpg', referer=ROOT + '/')
        dmm, site = adapter.sent
        assert cookie_names(dmm) == set()
        assert cookie_names(site) == {'dv=1', 'existmag=mag'}
        for sent in (dmm, site):
            assert sent.headers['Accept'] == IMAGE_ACCEPT
            assert sent.headers['Referer'] == ROOT + '/'

    def test_cookies_follow_a_changed_root_path(self):
        fetcher = Fetcher('https://www.busjav.casa/')
        assert fetcher.root_path == 'https://www.busjav.casa'
        assert fetcher.session.cookies.get('dv', domain='www.busjav.casa') == '1'
        assert fetcher.session.cookies.get('existmag', domain='www.busjav.casa') == 'mag'

    @pytest.mark.parametrize('path, url', [
        ('/', ROOT + '/'),
        ('/page/2', ROOT + '/page/2'),
        ('ABC-123', ROOT + '/ABC-123'),
        ('https://pics.dmm.co.jp/a.jpg', 'https://pics.dmm.co.jp/a.jpg'),
    ])
    def test_url_for(self, path, url):
        assert Fetcher(ROOT + '/').url_for(path) == url

    def test_close_closes_the_session(self):
        fetcher, adapter = make_fetcher({})
        fetcher.close()
        assert adapter.closed


class TestBlocked:
    def test_redirect_to_verify_page(self, page, clock):
        fetcher, adapter = make_fetcher({
            ROOT + '/': Reply(302, b'', {'Location': VERIFY_URL}),
            VERIFY_URL: Reply(200, page('driver-verify.html').encode('utf-8'),
                              {'Content-Type': 'text/html;Charset=utf-8;;charset=UTF-8'}),
        })
        with pytest.raises(BlockedError, match='年龄验证页'):
            fetcher.get_html('/')
        assert [sent.url for sent in adapter.sent] == [ROOT + '/', VERIFY_URL]
        assert clock.sleeps == []

    def test_landing_on_verify_page_directly(self, page):
        fetcher, _ = make_fetcher({VERIFY_URL: html(page('driver-verify.html'))})
        with pytest.raises(BlockedError, match='年龄验证页'):
            fetcher.get(VERIFY_URL)

    # 响应正文是最小化的拦截页特征字符串
    @pytest.mark.parametrize('reply, message', [
        (Reply(403, b'error code: 1010'), 'Cloudflare 1010'),
        (Reply(403, b'<title>Just a moment...</title>'), '人机验证'),
        (Reply(403, b'', {'cf-mitigated': 'challenge'}), '人机验证'),
        (Reply(403, b'Forbidden'), 'HTTP 403'),
    ])
    def test_forbidden_responses(self, reply, message, clock):
        fetcher, adapter = make_fetcher({ROOT + '/': reply})
        with pytest.raises(BlockedError, match=message):
            fetcher.get('/')
        assert len(adapter.sent) == 1
        assert clock.sleeps == []

    def test_blocked_error_is_a_fetch_error(self):
        assert issubclass(BlockedError, FetchError)


class TestRetry:
    def test_not_found_is_not_retried(self, clock):
        fetcher, adapter = make_fetcher({ROOT + '/NOPE-001': Reply(404, b'')})
        with pytest.raises(FetchError, match=r'页面不存在（HTTP 404）') as info:
            fetcher.get('/NOPE-001')
        assert not isinstance(info.value, BlockedError)
        assert len(adapter.sent) == 1
        assert clock.sleeps == []

    def test_other_client_errors_are_not_retried(self):
        fetcher, adapter = make_fetcher({ROOT + '/': Reply(410, b'')})
        with pytest.raises(FetchError, match=r'请求失败（HTTP 410）'):
            fetcher.get('/')
        assert len(adapter.sent) == 1

    def test_server_errors_are_retried_with_backoff(self, clock):
        fetcher, adapter = make_fetcher({ROOT + '/': [Reply(503), Reply(502), html('ok')]})
        assert fetcher.get_html('/') == 'ok'
        assert len(adapter.sent) == 3
        assert clock.sleeps == [2, 4]

    def test_gives_up_after_the_last_retry(self, clock):
        fetcher, adapter = make_fetcher({ROOT + '/': Reply(500)})
        with pytest.raises(FetchError) as info:
            fetcher.get('/')
        assert not isinstance(info.value, BlockedError)
        assert str(info.value) == f'HTTP 500，已重试 3 次：{ROOT}/。{NETWORK_HINT}'
        assert len(adapter.sent) == 4
        assert clock.sleeps == [2, 4, 8]

    def test_backoff_is_capped_at_30_seconds(self, clock):
        fetcher, _ = make_fetcher({ROOT + '/': Reply(500)}, retries=6)
        with pytest.raises(FetchError):
            fetcher.get('/')
        assert clock.sleeps == [2, 4, 8, 16, 30, 30]

    def test_too_many_requests_becomes_blocked(self, clock):
        fetcher, adapter = make_fetcher({ROOT + '/': Reply(429)})
        with pytest.raises(BlockedError, match='HTTP 429'):
            fetcher.get('/')
        assert len(adapter.sent) == 4
        assert clock.sleeps == [2, 4, 8]

    def test_too_many_requests_then_success(self):
        fetcher, _ = make_fetcher({ROOT + '/': [Reply(429), html('ok')]})
        assert fetcher.get_html('/') == 'ok'

    def test_connection_error_then_success(self, clock):
        fetcher, _ = make_fetcher({ROOT + '/': [requests.ConnectionError('reset'), html('ok')]})
        assert fetcher.get_html('/') == 'ok'
        assert clock.sleeps == [2]

    @pytest.mark.parametrize('error, name', [
        (requests.ConnectionError('refused'), 'ConnectionError'),
        (requests.exceptions.ReadTimeout('slow'), 'ReadTimeout'),
    ])
    def test_network_errors_give_up_with_a_hint(self, error, name):
        fetcher, adapter = make_fetcher({ROOT + '/': error}, retries=1)
        with pytest.raises(FetchError) as info:
            fetcher.get('/')
        message = str(info.value)
        assert message.startswith(f'网络连接失败（{name}: ')
        assert '已重试 1 次' in message
        assert message.endswith(NETWORK_HINT)
        assert len(adapter.sent) == 2


class TestProxy:
    def test_configured_proxy_is_used_for_every_request(self, monkeypatch):
        monkeypatch.delenv('NO_PROXY')
        monkeypatch.setenv('HTTPS_PROXY', 'http://env-proxy:8080')
        proxy = 'http://127.0.0.1:7890'
        fetcher, adapter = make_fetcher({ROOT + '/': html(), ROOT + '/page/2': html()}, proxy=proxy)
        fetcher.get('/')
        fetcher.get('/page/2')
        assert [sent.proxies.get('https') for sent in adapter.sent] == [proxy, proxy]
        assert fetcher.proxies == {'http': proxy, 'https': proxy}

    def test_environment_proxy_is_used_when_none_is_configured(self, monkeypatch):
        monkeypatch.delenv('NO_PROXY')
        monkeypatch.setenv('HTTPS_PROXY', 'http://env-proxy:8080')
        fetcher, adapter = make_fetcher({ROOT + '/': html()})
        fetcher.get('/')
        assert fetcher.proxies is None
        assert adapter.sent[0].proxies.get('https') == 'http://env-proxy:8080'

    def test_socks_proxy_without_pysocks(self):
        error = requests.exceptions.InvalidSchema('Missing dependencies for SOCKS support.')
        fetcher, adapter = make_fetcher({ROOT + '/': error}, proxy='socks5://127.0.0.1:1080')
        with pytest.raises(FetchError, match=r'pip install "requests\[socks\]"'):
            fetcher.get('/')
        assert len(adapter.sent) == 1

    @pytest.mark.parametrize('proxy, shown, secret', [
        ('http://ghp_T0KEN@127.0.0.1:99999', 'http://***@127.0.0.1:99999', 'ghp_T0KEN'),
        ('http://alice:s3cret@127.0.0.1:99999', 'http://alice:***@127.0.0.1:99999', 's3cret'),
    ], ids=['user-only', 'user-and-password'])
    def test_invalid_proxy_message_hides_credentials(self, proxy, shown, secret):
        # 端口超出范围时 urllib3 的报错里带着完整的代理地址
        fetcher = Fetcher(ROOT, proxy=proxy, delay=0, retries=0)
        with pytest.raises(FetchError, match='网址或代理地址无效') as info:
            fetcher.get('/')
        assert shown in str(info.value)
        assert secret not in str(info.value)

    def test_network_error_mentioning_the_proxy_hides_credentials(self):
        proxy = 'http://ghp_T0KEN@127.0.0.1:7890'
        error = requests.exceptions.ProxyError(f'Cannot connect to proxy {proxy}')
        fetcher, _ = make_fetcher({ROOT + '/': error}, proxy=proxy, retries=0)
        with pytest.raises(FetchError) as info:
            fetcher.get('/')
        assert 'Cannot connect to proxy http://***@127.0.0.1:7890' in str(info.value)
        assert 'ghp_T0KEN' not in str(info.value)


class TestInvalidAddress:
    def test_root_path_without_scheme(self):
        fetcher = Fetcher('www.javbus.com', delay=0)
        with pytest.raises(FetchError, match='网址或代理地址无效'):
            fetcher.get('/')

    def test_unsupported_scheme(self):
        fetcher = Fetcher('ftp://www.javbus.com', delay=0)
        with pytest.raises(FetchError, match='网址无效'):
            fetcher.get('/')


class TestContent:
    def test_html_without_charset_is_decoded_as_utf8(self):
        body = '<h3>識別碼</h3>'.encode('utf-8')
        fetcher, _ = make_fetcher({
            ROOT + '/a': Reply(200, body, {'Content-Type': 'text/html'}),
            ROOT + '/b': Reply(200, body, {}),
        })
        assert fetcher.get_html('/a') == '<h3>識別碼</h3>'
        assert fetcher.get_html('/b') == '<h3>識別碼</h3>'

    def test_declared_charset_is_respected(self):
        body = '<h3>識別碼</h3>'.encode('big5')
        fetcher, _ = make_fetcher({ROOT + '/': Reply(200, body, {'Content-Type': 'text/html; charset=big5'})})
        assert fetcher.get_html('/') == '<h3>識別碼</h3>'

    def test_get_bytes_returns_bare_content_type(self):
        data = b'\x89PNG' + b'0' * 2000
        fetcher, _ = make_fetcher({
            ROOT + '/a.png': Reply(200, data, {'Content-Type': 'Image/PNG; charset=binary'}),
            ROOT + '/b': Reply(200, b'xyz', {}),
        })
        assert fetcher.get_bytes('/a.png') == (data, 'image/png')
        assert fetcher.get_bytes('/b') == (b'xyz', '')


class TestThrottle:
    def test_requests_are_spaced_by_delay_plus_jitter(self, clock, jitter):
        fetcher, _ = make_fetcher({ROOT + '/': html()}, delay=1.5)
        fetcher.get('/')
        assert clock.sleeps == []
        fetcher.get('/')
        # 1.5 秒间隔 + 抖动上限 1.5 * 0.3
        assert clock.sleeps == [pytest.approx(1.95)]
        assert jitter.calls[-1] == (0, pytest.approx(0.45))
        clock.now += 60
        fetcher.get('/')
        assert len(clock.sleeps) == 1

    def test_zero_delay_never_waits(self, clock, jitter):
        fetcher, _ = make_fetcher({ROOT + '/': html()}, delay=0)
        for _ in range(3):
            fetcher.get('/')
        assert clock.sleeps == []
        assert jitter.calls == []


class TestCreateFetcher:
    def test_reads_config(self, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.root_path', 'https://www.busjav.casa')
        monkeypatch.setitem(util.APP_CONFIG, 'options.proxy', ' http://127.0.0.1:7890 ')
        monkeypatch.setitem(util.APP_CONFIG, 'download.delay', '2.5')
        fetcher = create_fetcher()
        assert fetcher.root_path == 'https://www.busjav.casa'
        assert fetcher.proxies == {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}
        assert fetcher.delay == 2.5
        assert fetcher.retries == 3

    def test_blank_proxy_and_overrides(self, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'options.proxy', '   ')
        fetcher = create_fetcher(delay=0, retries=1)
        assert fetcher.proxies is None
        assert fetcher.delay == 0
        assert fetcher.retries == 1
        assert fetcher.root_path == util.APP_CONFIG['download.root_path']

    def test_explicit_delay_does_not_read_config(self, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.delay', '不是数字')
        assert create_fetcher(delay=0).delay == 0

    def test_percent_in_proxy_from_config_file(self, data_dir):
        proxy = 'http://alice:p%40ss@127.0.0.1:7890'
        (data_dir / 'config.ini').write_text(f'[options]\nproxy = {proxy}\n', encoding='utf-8')
        util.load_config()
        assert create_fetcher().proxies == {'http': proxy, 'https': proxy}
