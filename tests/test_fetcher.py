'''
Fetcher：替换 requests 的传输层模拟网站响应，重定向、cookie 作用域、代理合并都走 requests 自己的代码；
time 和 random 换成假对象，重试退避和限速不真的等待
'''
import io
import logging
import types
from dataclasses import dataclass, field

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.response import HTTPResponse

from bustag import util
from bustag.spider import fetcher as fetcher_module
from bustag.spider.fetcher import (
    HTML_ACCEPT, IMAGE_ACCEPT, NETWORK_HINT, USER_AGENT, BlockedError, Fetcher, FetchError,
    NetworkError, NotFoundError, RequestCancelled, create_fetcher,
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

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class CoarseClock(FakeClock):
    '''
    Windows 上 time.monotonic() 每 15.625 毫秒才走一格（GetTickCount64），time.perf_counter() 是精确的。
    两个读数出自同一个真实时间 now，sleep 睡的是真实时间
    '''
    TICK = 0.015625

    def monotonic(self):
        return self.now // self.TICK * self.TICK


class EarlyClock(FakeClock):
    '''睡醒得早：每次 sleep 只睡够要求的九成'''

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds * 0.9


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
        with pytest.raises(NotFoundError, match=r'页面不存在（HTTP 404）') as info:
            fetcher.get('/NOPE-001')
        assert isinstance(info.value, FetchError) and not isinstance(info.value, BlockedError)
        assert len(adapter.sent) == 1
        assert clock.sleeps == []

    def test_other_client_errors_are_not_retried(self):
        fetcher, adapter = make_fetcher({ROOT + '/': Reply(410, b'')})
        with pytest.raises(FetchError, match=r'请求失败（HTTP 410）') as info:
            fetcher.get('/')
        assert not isinstance(info.value, NotFoundError)
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
        assert not isinstance(info.value, (BlockedError, NetworkError))
        assert str(info.value) == f'HTTP 500，已重试 3 次：{ROOT}/'
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
        with pytest.raises(NetworkError) as info:
            fetcher.get('/')
        assert isinstance(info.value, FetchError)
        message = str(info.value)
        assert message.startswith(f'网络连接失败（{name}: ')
        assert '已重试 1 次' in message
        assert message.endswith(NETWORK_HINT)
        assert len(adapter.sent) == 2

    def test_a_single_attempt_does_not_talk_about_retries(self):
        fetcher, adapter = make_fetcher({ROOT + '/': Reply(503)}, retries=0)
        with pytest.raises(FetchError) as info:
            fetcher.get('/')
        assert str(info.value) == f'HTTP 503：{ROOT}/'
        assert len(adapter.sent) == 1

    @pytest.mark.parametrize('status', [500, 502, 503])
    def test_an_answer_with_an_error_status_does_not_come_with_the_network_advice(self, status):
        fetcher, _ = make_fetcher({ROOT + '/': Reply(status)}, retries=1)
        with pytest.raises(FetchError) as info:
            fetcher.get('/')
        assert not isinstance(info.value, NetworkError)
        assert str(info.value) == f'HTTP {status}，已重试 1 次：{ROOT}/'

    def test_the_last_attempt_decides_whether_the_advice_comes_with_the_error(self):
        answered_last, _ = make_fetcher({ROOT + '/': [requests.ConnectionError('reset'), Reply(503)]}, retries=1)
        with pytest.raises(FetchError) as info:
            answered_last.get('/')
        assert not isinstance(info.value, NetworkError)
        assert NETWORK_HINT not in str(info.value)
        silent_last, _ = make_fetcher({ROOT + '/': [Reply(503), requests.ConnectionError('reset')]}, retries=1)
        with pytest.raises(NetworkError) as info:
            silent_last.get('/')
        assert str(info.value).endswith(NETWORK_HINT)


ACTRESS_PAGE = 'https://av-wiki.net/av-actress/mingzi-jia/'
ACTRESS_PAGE_SHOWN = 'https://av-wiki.net/av-actress/…/'


class TestNamesStayOutOfMessages:
    '''av-wiki 女优页的地址里有演员名字的罗马字：报错和日志里只留到 /av-actress/'''

    @pytest.mark.parametrize('reply, message', [
        (Reply(404, b''), f'页面不存在（HTTP 404）：{ACTRESS_PAGE_SHOWN}'),
        (Reply(410, b''), f'请求失败（HTTP 410）：{ACTRESS_PAGE_SHOWN}'),
        (Reply(503), f'HTTP 503：{ACTRESS_PAGE_SHOWN}'),
    ], ids=['gone', 'refused', 'server-error'])
    def test_error_messages(self, reply, message):
        fetcher, _ = make_fetcher({ACTRESS_PAGE: reply}, retries=0)
        with pytest.raises(FetchError) as info:
            fetcher.get(ACTRESS_PAGE)
        assert str(info.value) == message

    def test_a_network_error_that_quotes_the_path(self):
        # requests / urllib3 的报错里带着请求的路径
        error = requests.ConnectionError("HTTPSConnectionPool(host='av-wiki.net', port=443): Max retries exceeded "
                                         "with url: /av-actress/mingzi-jia/ (Caused by NewConnectionError('refused'))")
        fetcher, _ = make_fetcher({ACTRESS_PAGE: error}, retries=0)
        with pytest.raises(NetworkError) as info:
            fetcher.get(ACTRESS_PAGE)
        assert 'mingzi' not in str(info.value)
        assert 'Max retries exceeded with url: /av-actress/…/ (Caused by' in str(info.value)
        assert str(info.value).endswith(f'：{ACTRESS_PAGE_SHOWN}。{NETWORK_HINT}')

    def test_log_lines(self, caplog):
        caplog.set_level(logging.DEBUG, logger='bustag')
        fetcher, _ = make_fetcher({ACTRESS_PAGE: [Reply(503), html('ok')]})
        assert fetcher.get_html(ACTRESS_PAGE) == 'ok'
        assert 'mingzi' not in caplog.text
        assert f'HTTP 503，2 秒后第 1 次重试：{ACTRESS_PAGE_SHOWN}' in caplog.messages
        assert sum(message.startswith(f'请求 {ACTRESS_PAGE_SHOWN} 返回 HTTP ') for message in caplog.messages) == 2

    def test_the_request_itself_goes_to_the_full_address(self):
        fetcher, adapter = make_fetcher({ACTRESS_PAGE: html('ok')})
        assert fetcher.get_html(ACTRESS_PAGE) == 'ok'
        assert [sent.url for sent in adapter.sent] == [ACTRESS_PAGE]

    def test_other_addresses_are_written_in_full(self):
        search = 'https://av-wiki.net/?s=ABC-001'
        fetcher, _ = make_fetcher({ROOT + '/ABC-001': Reply(404, b''), search: Reply(404, b'')}, retries=0)
        for url in (ROOT + '/ABC-001', search):
            with pytest.raises(NotFoundError) as info:
                fetcher.get(url)
            assert str(info.value) == f'页面不存在（HTTP 404）：{url}'


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

    @pytest.mark.parametrize('error, kind', [
        (requests.exceptions.InvalidURL('Failed to parse: {proxy}'), FetchError),
        (requests.exceptions.ProxyError('Cannot connect to proxy {proxy}'), NetworkError),
    ], ids=['invalid-address', 'no-answer'])
    @pytest.mark.parametrize('proxy, shown, secret', [
        ('http://envuser:EnvSecret9@127.0.0.1:99999', 'http://envuser:***@127.0.0.1:99999', 'EnvSecret9'),
        ('http://ghp_T0KEN@127.0.0.1:99999', 'http://***@127.0.0.1:99999', 'ghp_T0KEN'),
    ], ids=['user-and-password', 'user-only'])
    def test_credentials_of_a_proxy_from_the_environment_are_hidden(self, caplog, error, kind, proxy, shown,
                                                                     secret):
        # 配置里没有写代理时用的是环境变量里的那个，程序自己并不知道它的地址
        error = type(error)(str(error).format(proxy=proxy))
        fetcher, _ = make_fetcher({ROOT + '/': error}, retries=1)
        with caplog.at_level(logging.INFO), pytest.raises(kind) as info:
            fetcher.get('/')
        assert shown in str(info.value)
        assert secret not in str(info.value)
        assert secret not in caplog.text


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

    def test_requests_of_two_fetchers_to_the_same_site_are_spaced_too(self, clock, jitter):
        # 后台任务一个接一个地跑、各自建 Fetcher：前一个任务的最后一个请求和后一个任务的第一个请求之间也要隔开
        first, _ = make_fetcher({ROOT + '/': html()}, delay=1.5)
        second, _ = make_fetcher({ROOT + '/': html()}, delay=1.5)
        first.get('/')
        second.get('/')
        assert clock.sleeps == [pytest.approx(1.95)]
        clock.now += 60
        first.get('/')
        assert len(clock.sleeps) == 1

    def test_another_site_does_not_wait(self, clock, jitter):
        crawl, _ = make_fetcher({ROOT + '/': html()}, delay=1.5)
        wiki, _ = make_fetcher({'https://av-wiki.net/': html()}, root_path='https://av-wiki.net', delay=1.5)
        crawl.get('/')
        wiki.get('/')
        assert clock.sleeps == []

    def test_a_fetcher_without_delay_neither_waits_nor_holds_others_up(self, clock, jitter):
        covers, _ = make_fetcher({ROOT + '/': html()}, delay=0)
        crawl, _ = make_fetcher({ROOT + '/': html()}, delay=1.5)
        covers.get('/')
        crawl.get('/')
        covers.get('/')
        assert clock.sleeps == []

    def test_requests_of_one_lane_are_spaced_by_the_delay_of_that_lane(self, clock, jitter):
        first, _ = make_fetcher({ROOT + '/': html()}, delay=0.3, lane='covers')
        second, _ = make_fetcher({ROOT + '/': html()}, delay=0.3, lane='covers')
        first.get('/')
        second.get('/')
        # 0.3 秒间隔 + 抖动上限 0.3 * 0.3
        assert clock.sleeps == [pytest.approx(0.39)]
        assert jitter.calls[-1] == (0, pytest.approx(0.09))

    def test_a_lane_neither_waits_for_the_crawl_nor_holds_it_up(self, clock, jitter):
        crawl, _ = make_fetcher({ROOT + '/': html()}, delay=1.5)
        covers, _ = make_fetcher({ROOT + '/': html()}, delay=0.3, lane='covers')
        crawl.get('/')
        covers.get('/')
        assert clock.sleeps == []
        covers.get('/')
        assert clock.sleeps == [pytest.approx(0.39)]
        # 抓取只和它自己上一次的请求隔开（1.5 + 抖动 0.45），封面的请求不算在里面
        crawl.get('/')
        assert clock.sleeps == [pytest.approx(0.39), pytest.approx(1.95 - 0.39)]

    def test_the_wait_is_kept_by_the_site_of_each_address(self, clock, jitter):
        # 一个 Fetcher 也会请求完整的地址（av-wiki 的女优页）：按那个地址的网站算
        wiki, _ = make_fetcher({'https://av-wiki.net/a': html(), ROOT + '/': html()}, delay=1.5)
        crawl, _ = make_fetcher({ROOT + '/': html()}, delay=1.5)
        wiki.get('https://av-wiki.net/a')
        crawl.get('/')
        assert clock.sleeps == []


class TestSpacingMeasuredPrecisely:
    '''
    相邻两次真实发出去的请求，用精确的时钟量，间隔不能比 delay 短：排时刻和记"发出去了"用的时钟本身不能是粗的
    （Windows 上的 time.monotonic() 每 15.625 毫秒才走一格，两个读数落在不同的格里就会差出一格来）
    '''

    @staticmethod
    def record_sends(adapter, clock, moments):
        '''把每个请求真正交给传输层的时刻（精确的时钟）记进 moments'''
        send = adapter.send

        def record(request, **kwargs):
            moments.append(clock.perf_counter())
            return send(request, **kwargs)

        adapter.send = record

    @pytest.fixture
    def no_jitter(self, monkeypatch):
        '''抖动取 0：间隔就是 delay 本身，计时的误差不会被抖动盖住'''
        monkeypatch.setattr(fetcher_module, 'random', types.SimpleNamespace(uniform=lambda low, high: 0))

    @pytest.mark.parametrize('same_fetcher', [True, False])
    @pytest.mark.parametrize('think', [0.0, 0.0005, 0.001, 0.003, 0.012])
    @pytest.mark.parametrize('into_the_tick', [0.0, 0.004, 0.008, 0.012, 0.015])
    def test_two_requests_are_never_closer_than_the_delay(self, monkeypatch, no_jitter, into_the_tick, think,
                                                          same_fetcher):
        clock = CoarseClock()
        monkeypatch.setattr(fetcher_module, 'time', clock)
        moments = []
        first, adapter = make_fetcher({ROOT + '/': html()}, delay=0.15)
        self.record_sends(adapter, clock, moments)
        if same_fetcher:
            second = first
        else:
            second, other_adapter = make_fetcher({ROOT + '/': html()}, delay=0.15)
            self.record_sends(other_adapter, clock, moments)
        clock.now = 1000.0 + into_the_tick
        first.get('/')
        clock.now += think
        second.get('/')
        before, after = moments
        assert after - before >= 0.15 - 1e-9

    def test_a_wake_up_before_the_time_is_followed_by_another_sleep(self, monkeypatch, no_jitter):
        clock = EarlyClock()
        monkeypatch.setattr(fetcher_module, 'time', clock)
        moments = []
        fetcher, adapter = make_fetcher({ROOT + '/': html()}, delay=1.5)
        self.record_sends(adapter, clock, moments)
        fetcher.get('/')
        fetcher.get('/')
        assert moments[1] - moments[0] >= 1.5 - 1e-9
        assert len(clock.sleeps) > 1


class TestProceed:
    '''调用方可以在等完间隔、真正发请求之前再说一次发不发（取消排队要用）：说不发就不发，间隔照旧'''

    def test_a_request_called_off_after_the_wait_is_not_sent(self, clock, jitter):
        fetcher, adapter = make_fetcher({ROOT + '/': html()}, delay=1.5)
        fetcher.get('/')
        asked_at = []

        def proceed():
            asked_at.append(clock.now)
            return False

        with pytest.raises(RequestCancelled):
            fetcher.get('/', proceed=proceed)
        # 间隔（1.5 秒 + 抖动上限 0.45）等完以后才问，问了说不发
        assert clock.sleeps == [pytest.approx(1.95)]
        assert asked_at == [pytest.approx(1000 + 1.95)]
        assert len(adapter.sent) == 1

    def test_a_request_that_goes_on_is_sent_as_usual(self, clock, jitter):
        fetcher, adapter = make_fetcher({ROOT + '/': html('<p>ok</p>')}, delay=1.5)
        fetcher.get('/')
        assert fetcher.get_html('/', proceed=lambda: True) == '<p>ok</p>'
        assert clock.sleeps == [pytest.approx(1.95)]
        assert len(adapter.sent) == 2

    def test_get_html_passes_it_on(self):
        fetcher, adapter = make_fetcher({ROOT + '/': html()})
        with pytest.raises(RequestCancelled):
            fetcher.get_html('/', proceed=lambda: False)
        assert adapter.sent == []

    def test_a_fetcher_without_delay_is_asked_too(self):
        fetcher, adapter = make_fetcher({ROOT + '/': html()}, delay=0)
        with pytest.raises(RequestCancelled):
            fetcher.get('/', proceed=lambda: False)
        assert adapter.sent == []

    def test_what_nobody_asks_is_sent(self):
        fetcher, adapter = make_fetcher({ROOT + '/': html()})
        fetcher.get('/')
        assert len(adapter.sent) == 1

    def test_the_next_request_still_waits_a_whole_delay_after_a_called_off_one(self, clock, jitter):
        # 排好的时刻留着：取消的那个请求没有发出去，但后面的请求不会比原来更早
        fetcher, adapter = make_fetcher({ROOT + '/': html()}, delay=1.5)
        fetcher.get('/')
        with pytest.raises(RequestCancelled):
            fetcher.get('/', proceed=lambda: False)
        fetcher.get('/')
        assert clock.sleeps == [pytest.approx(1.95), pytest.approx(1.95)]
        assert len(adapter.sent) == 2

    def test_a_called_off_request_is_not_a_fetch_error(self):
        # 不是网站的问题：不会被当成网络错误重试，也不会被调用方当成"读不到"
        assert not issubclass(RequestCancelled, FetchError)


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
