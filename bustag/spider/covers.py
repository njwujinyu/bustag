'''
封面：地址换算，按需下载并缓存到 <数据目录>/covers/
'''
import json
import os
import re
import tempfile
import threading
from urllib.parse import urljoin, urlparse

from bustag.spider.fetcher import FetchError, create_fetcher
from bustag.util import APP_CONFIG, get_data_path, is_dead_domain, logger

COVERS_DIR = 'covers'
# 已失效的图片域名 -> 同一文件在 JavBus 上的路径前缀（pics.javcdn.pw/cover/x.jpg -> /pics/cover/x.jpg）
DEAD_IMAGE_HOSTS = {'pics.javcdn.pw': '/pics'}
IMAGE_TYPES = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif'}
MIN_SIZE = 1024
MAX_SIZE = 5 * 1024 * 1024
MAX_DOWNLOADS = 2

_download_slots = threading.BoundedSemaphore(MAX_DOWNLOADS)
_local = threading.local()


def normalize_cover_url(url):
    '''
    相对路径原样返回；已失效的图片域名和 JavBus 旧域名换成路径；其他绝对地址原样返回
    '''
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    host = (parsed.hostname or '').lower()
    if host in DEAD_IMAGE_HOSTS:
        prefix = DEAD_IMAGE_HOSTS[host]
        return parsed.path if parsed.path.startswith(prefix + '/') else prefix + parsed.path
    if is_dead_domain(url):
        return parsed.path
    return url


def cover_referer():
    return APP_CONFIG['download.root_path'] + '/'


def cover_source_url(cover_img_url):
    '''
    下载封面用的完整地址：相对路径拼到当前 root_path；没有封面时返回 None
    '''
    url = normalize_cover_url(cover_img_url)
    if not url:
        return None
    return urljoin(cover_referer(), url)


def validate_image(data, content_type):
    '''
    图片无效时返回原因，有效时返回 None
    '''
    if not content_type.startswith('image/'):
        return f'不是图片（content-type: {content_type or "无"}）'
    if content_type not in IMAGE_TYPES:
        return f'不支持的图片格式（{content_type}）'
    if len(data) <= MIN_SIZE:
        return f'文件太小（{len(data)} 字节）'
    if len(data) > MAX_SIZE:
        return f'文件太大（{len(data)} 字节）'
    return None


def _cache_name(fanhao):
    name = (fanhao or '').strip().upper()
    return name if re.fullmatch(r'[A-Z0-9-]+', name) else None


def _covers_dir():
    folder = get_data_path(COVERS_DIR)
    os.makedirs(folder, exist_ok=True)
    return folder


def _find_cached(name):
    folder = _covers_dir()
    for content_type, ext in IMAGE_TYPES.items():
        path = os.path.join(folder, name + ext)
        if os.path.isfile(path):
            return path, content_type
    return None


def _save(name, data, content_type):
    folder = _covers_dir()
    path = os.path.join(folder, name + IMAGE_TYPES[content_type])
    fd, tmp_path = tempfile.mkstemp(prefix=name + '.', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp_path, path)
    except OSError as ex:
        logger.warning(f'封面缓存写入失败 {path}：{ex}')
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return (path, content_type) if os.path.isfile(path) else None
    return path, content_type


def _fetcher():
    fetcher = getattr(_local, 'fetcher', None)
    if fetcher is None:
        fetcher = create_fetcher(delay=0, retries=1)
        _local.fetcher = fetcher
    return fetcher


def _cover_img_url(fanhao):
    from bustag.spider.db import Item

    item = Item.get_by_fanhao(fanhao)
    if item is None:
        return None
    meta = json.loads(item.meta_info or '{}')
    return meta.get('cover_img_url')


def get_cover_file(fanhao):
    '''
    返回 (缓存文件的绝对路径, mimetype)。番号不合法、库里没有、没有封面地址、
    下载失败或不是有效图片时返回 None（原因写进日志）。缓存命中时不查库、不联网。
    '''
    name = _cache_name(fanhao)
    if name is None:
        return None
    cached = _find_cached(name)
    if cached:
        return cached
    source = cover_source_url(_cover_img_url(fanhao))
    if not source:
        return None
    with _download_slots:
        cached = _find_cached(name)
        if cached:
            return cached
        try:
            data, content_type = _fetcher().get_bytes(source, referer=cover_referer())
        except FetchError as ex:
            logger.warning(f'封面下载失败 {fanhao}（{source}）：{ex}')
            return None
    error = validate_image(data, content_type)
    if error:
        logger.warning(f'封面无效 {fanhao}（{source}）：{error}')
        return None
    return _save(name, data, content_type)
