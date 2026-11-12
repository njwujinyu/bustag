'''
访问 JavBus 的 HTTP 客户端：固定请求头和 cookie、限速、重试、识别拦截
'''
import random
import threading
import time
from urllib.parse import urljoin, urlparse

import requests

from bustag.util import APP_CONFIG, hide_actress, hide_credentials, logger, redact_url

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36')
HTML_ACCEPT = ('text/html,application/xhtml+xml,application/xml;q=0.9,'
               'image/avif,image/webp,image/apng,*/*;q=0.8')
IMAGE_ACCEPT = 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8'
COOKIES = {'dv': '1', 'existmag': 'mag'}
VERIFY_PATH = '/doc/driver-verify'
MAX_BACKOFF = 30
# 一个答复也没收到（连不上、超时、代理不通）时，错误文字末尾的建议；网站答复了的错误不带
NETWORK_HINT = '请检查网络、代理设置（config.ini 的 [options] proxy）和 root_path。'
# 同一个网站的请求，不管来自哪个 Fetcher，两次之间都要隔开：后台任务一个接一个地跑、各自建 Fetcher，
# 前一个任务的最后一个请求和后一个任务的第一个请求之间也要有间隔。只在同一组（Fetcher 的 lane）里隔开：
# (组, 主机名) -> 上一次请求的时刻（time.perf_counter()），只记 delay 大于 0 的 Fetcher 发的请求；
# 还没发出去、已经排好时刻的，记的是排好的那个时刻。排时刻、记时刻都用 perf_counter，不用 time.monotonic()：
# Windows 上后者每 15.6 毫秒才走一格，前一个请求记下的和下一个请求排时刻时读的落在不同的格里，间隔会短不到一格
_last_sent = {}
_spacing_lock = threading.Lock()


class FetchError(Exception):
    '''
    请求失败：网络或代理不通、服务器错误、页面不存在等
    '''


class BlockedError(FetchError):
    '''
    请求被网站拦截（年龄验证页、Cloudflare 等），换个页面重试也没用
    '''


class NotFoundError(FetchError):
    '''
    网站答复了，但没有这个页面（HTTP 404）
    '''


class RequestCancelled(Exception):
    '''
    调用方给的 proceed 在等完间隔、要发请求之前说了不发：这个请求没有发出去。不是网站或网络的问题，所以不是 FetchError
    '''


class NetworkError(FetchError):
    '''
    没有收到网站的任何答复：连不上、超时、代理不通
    '''


def blocked_reason(resp):
    '''
    响应是拦截页时返回原因和建议，否则返回 None
    '''
    locations = [r.headers.get('Location', '') for r in resp.history]
    if any(VERIFY_PATH in url for url in locations + [resp.url]):
        return ('被年龄验证页拦截（跳转到了 /doc/driver-verify）。'
                '请换一个代理节点或稍后再试；仍不行时请检查 root_path 是否为 JavBus 官方地址。')
    if resp.status_code == 403:
        text = resp.text[:5000]
        if 'error code: 1010' in text:
            return ('Cloudflare 1010：请求特征被拦截（请求头被识别为程序）。'
                    '请换一个代理节点或稍后再试。')
        if resp.headers.get('cf-mitigated') == 'challenge' or 'Just a moment' in text:
            return 'Cloudflare 人机验证页拦截。请换一个代理节点或稍后再试。'
        return 'HTTP 403：访问被拒绝，可能被 Cloudflare 拦截。请换一个代理节点或稍后再试。'
    return None


class Fetcher:
    '''
    一个实例只在一个线程里用。两次请求之间至少间隔 delay 秒（另加少量随机抖动），对同一个网站的请求，
    同一组（lane）的别的实例（delay 也大于 0 的）发的也算在内；连接错误、429、5xx 按指数退避重试 retries 次。
    抓取、补全、上传番号用默认的一组；封面自己一组：封面之间留一个小间隔，但不拖住抓取的间隔，也不被抓取拖住。
    '''

    def __init__(self, root_path, proxy=None, delay=1.5, timeout=20, retries=3, lane=''):
        self.root_path = root_path.rstrip('/')
        self.proxy = proxy
        self.proxies = {'http': proxy, 'https': proxy} if proxy else None
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.lane = lane
        self._last_request = None
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept': HTML_ACCEPT,
            'Accept-Language': 'zh-CN,zh;q=0.9',
        })
        host = urlparse(self.root_path).hostname or ''
        for name, value in COOKIES.items():
            self.session.cookies.set(name, value, domain=host, path='/')

    def url_for(self, path_or_url):
        return urljoin(self.root_path + '/', path_or_url)

    def close(self):
        self.session.close()

    def _throttle(self, host):
        '''等到可以对 host 发下一个请求的时候。先在锁里排好时刻再睡，两个线程不会挑中同一个时刻'''
        if self.delay <= 0:
            return
        jitter = random.uniform(0, self.delay * 0.3)
        key = (self.lane, host)
        with _spacing_lock:
            now = time.perf_counter()
            last = [moment for moment in (self._last_request, _last_sent.get(key)) if moment is not None]
            start = max(now, max(last) + self.delay + jitter) if last else now
            _last_sent[key] = start
        # 睡醒了还没到点（睡眠偶尔醒得早）就接着睡，到了点才发
        remaining = start - now
        while remaining > 0:
            time.sleep(remaining)
            remaining = start - time.perf_counter()

    def _sent(self, host):
        self._last_request = time.perf_counter()
        if self.delay > 0:
            key = (self.lane, host)
            with _spacing_lock:
                _last_sent[key] = max(_last_sent.get(key, self._last_request), self._last_request)

    def _error_text(self, ex):
        '''
        异常的文字。requests / urllib3 有时把代理地址原样写进去（例如端口不对时），里面的认证信息要隐去：
        这段文字不只进日志，还会显示在页面上、记进数据库。代理可能是这里指定的、配置里的，也可能来自环境变量。
        文字里也会带上请求的路径，女优页的路径里有演员的名字，同样隐去
        '''
        text = str(ex)
        if self.proxy:
            text = text.replace(self.proxy, redact_url(self.proxy))
        return hide_actress(hide_credentials(text))

    def _send(self, url, headers, proceed=None):
        host = (urlparse(url).hostname or '').lower()
        self._throttle(host)
        shown = hide_actress(url)
        if proceed is not None and not proceed():
            # 间隔排好的时刻留着，所以不会让后面的请求比原来更早
            raise RequestCancelled(f'请求没有发出去：{shown}')
        try:
            return self.session.get(url, headers=headers, proxies=self.proxies,
                                    timeout=self.timeout)
        except requests.exceptions.InvalidSchema as ex:
            if 'SOCKS' in str(ex):
                raise FetchError('使用 socks 代理需要先安装 PySocks：pip install "requests[socks]"') from ex
            raise FetchError(f'网址无效：{shown}（{self._error_text(ex)}）。请检查 config.ini 的 root_path。') from ex
        except (requests.exceptions.MissingSchema, requests.exceptions.InvalidURL) as ex:
            raise FetchError(f'网址或代理地址无效：{shown}（{self._error_text(ex)}）。'
                             '请检查 config.ini 的 root_path 和 proxy。') from ex
        finally:
            self._sent(host)

    def get(self, path_or_url, referer=None, accept=None, proceed=None):
        '''
        GET 一个路径或完整网址，返回状态码为 200 的 requests.Response。
        被拦截时抛 BlockedError；一个答复也没收到时抛 NetworkError，文字末尾带检查网络的建议；其他失败抛 FetchError。
        proceed 不空时，每次发请求之前（等完间隔以后）调一下，返回假就不发，抛 RequestCancelled：
        等间隔的几秒里这个请求可能已经不需要了（取消排队）
        '''
        url = self.url_for(path_or_url)
        # 写进日志和报错里的地址：女优页的地址里有演员的名字，只留到 /av-actress/
        shown = hide_actress(url)
        headers = {}
        if referer:
            headers['Referer'] = referer
        if accept:
            headers['Accept'] = accept
        reason = ''
        status = None
        for attempt in range(self.retries + 1):
            if attempt:
                wait = min(2 ** attempt, MAX_BACKOFF)
                logger.info(f'{reason}，{wait} 秒后第 {attempt} 次重试：{shown}')
                time.sleep(wait)
            try:
                resp = self._send(url, headers, proceed)
            except FetchError:
                raise
            except requests.RequestException as ex:
                reason = f'网络连接失败（{type(ex).__name__}: {self._error_text(ex)}）'
                status = None
                continue
            status = resp.status_code
            logger.debug(f'请求 {shown} 返回 HTTP {status}，用时 {resp.elapsed.total_seconds():.2f} 秒')
            blocked = blocked_reason(resp)
            if blocked:
                raise BlockedError(blocked)
            if status == 200:
                return resp
            reason = f'HTTP {status}'
            if status == 404:
                raise NotFoundError(f'页面不存在（HTTP 404）：{shown}')
            if status != 429 and status < 500:
                raise FetchError(f'请求失败（HTTP {status}）：{shown}')
        if status == 429:
            raise BlockedError('HTTP 429：请求太频繁，被网站限流。请稍后再试，或在 config.ini 的 [download] 里调大 delay。')
        retried = f'，已重试 {self.retries} 次' if self.retries else ''
        if status is None:
            raise NetworkError(f'{reason}{retried}：{shown}。{NETWORK_HINT}')
        raise FetchError(f'{reason}{retried}：{shown}')

    def get_html(self, path_or_url, proceed=None):
        resp = self.get(path_or_url, proceed=proceed)
        if not resp.encoding or resp.encoding.lower() == 'iso-8859-1':
            resp.encoding = 'utf-8'
        return resp.text

    def get_bytes(self, url, referer=None):
        '''
        返回 (内容, content_type)，content_type 已去掉参数并转成小写，如 image/jpeg
        '''
        resp = self.get(url, referer=referer, accept=IMAGE_ACCEPT)
        content_type = resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
        return resp.content, content_type


def create_fetcher(**overrides):
    '''
    按 config.ini 创建 Fetcher，关键字参数覆盖对应的配置（如 delay=0、retries=1）
    '''
    options = {
        'root_path': APP_CONFIG['download.root_path'],
        'proxy': APP_CONFIG.get('options.proxy', '').strip() or None,
    }
    options.update(overrides)
    if 'delay' not in options:
        options['delay'] = float(APP_CONFIG['download.delay'])
    return Fetcher(**options)
