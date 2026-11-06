'''
访问 JavBus 的 HTTP 客户端：固定请求头和 cookie、限速、重试、识别拦截
'''
import random
import time
from urllib.parse import urljoin, urlparse

import requests

from bustag.util import APP_CONFIG, logger, redact_url

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36')
HTML_ACCEPT = ('text/html,application/xhtml+xml,application/xml;q=0.9,'
               'image/avif,image/webp,image/apng,*/*;q=0.8')
IMAGE_ACCEPT = 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8'
COOKIES = {'dv': '1', 'existmag': 'mag'}
VERIFY_PATH = '/doc/driver-verify'
MAX_BACKOFF = 30
NETWORK_HINT = '请检查网络、代理设置（config.ini 的 [options] proxy）和 root_path。'


class FetchError(Exception):
    '''
    请求失败：网络或代理不通、服务器错误、页面不存在等
    '''


class BlockedError(FetchError):
    '''
    请求被网站拦截（年龄验证页、Cloudflare 等），换个页面重试也没用
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
    一个实例只在一个线程里用。两次请求之间至少间隔 delay 秒（另加少量随机抖动），
    连接错误、429、5xx 按指数退避重试 retries 次。
    '''

    def __init__(self, root_path, proxy=None, delay=1.5, timeout=20, retries=3):
        self.root_path = root_path.rstrip('/')
        self.proxy = proxy
        self.proxies = {'http': proxy, 'https': proxy} if proxy else None
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
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

    def _throttle(self):
        if self._last_request is None or self.delay <= 0:
            return
        jitter = random.uniform(0, self.delay * 0.3)
        wait = self._last_request + self.delay + jitter - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _error_text(self, ex):
        '''异常的文字。requests / urllib3 有时把代理地址原样写进去（例如端口不对时），里面的认证信息要隐去'''
        text = str(ex)
        if self.proxy:
            text = text.replace(self.proxy, redact_url(self.proxy))
        return text

    def _send(self, url, headers):
        self._throttle()
        try:
            return self.session.get(url, headers=headers, proxies=self.proxies,
                                    timeout=self.timeout)
        except requests.exceptions.InvalidSchema as ex:
            if 'SOCKS' in str(ex):
                raise FetchError('使用 socks 代理需要先安装 PySocks：pip install "requests[socks]"') from ex
            raise FetchError(f'网址无效：{url}（{self._error_text(ex)}）。请检查 config.ini 的 root_path。') from ex
        except (requests.exceptions.MissingSchema, requests.exceptions.InvalidURL) as ex:
            raise FetchError(f'网址或代理地址无效：{url}（{self._error_text(ex)}）。'
                             '请检查 config.ini 的 root_path 和 proxy。') from ex
        finally:
            self._last_request = time.monotonic()

    def get(self, path_or_url, referer=None, accept=None):
        '''
        GET 一个路径或完整网址，返回状态码为 200 的 requests.Response。
        被拦截时抛 BlockedError，其他失败抛 FetchError。
        '''
        url = self.url_for(path_or_url)
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
                logger.info(f'{reason}，{wait} 秒后第 {attempt} 次重试：{url}')
                time.sleep(wait)
            try:
                resp = self._send(url, headers)
            except FetchError:
                raise
            except requests.RequestException as ex:
                reason = f'网络连接失败（{type(ex).__name__}: {self._error_text(ex)}）'
                status = None
                continue
            status = resp.status_code
            logger.debug(f'请求 {url} 返回 HTTP {status}，用时 {resp.elapsed.total_seconds():.2f} 秒')
            blocked = blocked_reason(resp)
            if blocked:
                raise BlockedError(blocked)
            if status == 200:
                return resp
            reason = f'HTTP {status}'
            if status == 404:
                raise FetchError(f'页面不存在（HTTP 404）：{url}')
            if status != 429 and status < 500:
                raise FetchError(f'请求失败（HTTP {status}）：{url}')
        if status == 429:
            raise BlockedError('HTTP 429：请求太频繁，被网站限流。请稍后再试，或在 config.ini 的 [download] 里调大 delay。')
        raise FetchError(f'{reason}，已重试 {self.retries} 次：{url}。{NETWORK_HINT}')

    def get_html(self, path_or_url):
        resp = self.get(path_or_url)
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
