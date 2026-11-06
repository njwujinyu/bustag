'''
抓取：从首页起逐页读列表，抓新影片的详情页并入库
'''
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from bustag.spider.db import Item, save
from bustag.spider.fetcher import BlockedError, FetchError, create_fetcher
from bustag.spider.parser import ParseError, fanhao_from_path, has_next_page, is_valid_fanhao, parse_item, parse_list
from bustag.util import APP_CONFIG, get_full_url, logger

EMPTY_PAGES_LIMIT = 3
_lock = threading.Lock()


@dataclass
class DownloadResult:
    pages: int = 0
    added: int = 0
    skipped: int = 0
    failed: list = field(default_factory=list)
    blocked: Optional[str] = None
    error: Optional[str] = None
    stop_reason: str = ''
    elapsed: float = 0.0

    @property
    def ok(self):
        return self.blocked is None and self.error is None

    def __str__(self):
        parts = []
        if self.pages:
            parts.append(f'列表页 {self.pages} 页')
        parts.append(f'新增 {self.added} 部')
        parts.append(f'跳过 {self.skipped} 部')
        parts.append(f'失败 {len(self.failed)} 部')
        parts.append(f'用时 {self.elapsed:.0f} 秒')
        text = '抓取结束：' + '，'.join(parts)
        if self.stop_reason:
            text += f'（{self.stop_reason}）'
        if self.blocked:
            text += f'。被拦截：{self.blocked}'
        if self.error:
            text += f'。出错：{self.error}'
        return text


def get_url_by_fanhao(fanhao):
    # return full url
    url = get_full_url(fanhao)
    return url


def _fail(result, fanhao, reason):
    result.failed.append((fanhao, reason))
    logger.warning(f'{fanhao} 抓取失败：{reason}')


def _download_item(fetcher, result, path, fanhao):
    '''
    抓一部影片并入库，被拦截时返回 False（应停止抓取）
    '''
    try:
        meta, tags = parse_item(fetcher.get_html(path))
    except BlockedError as ex:
        result.blocked = str(ex)
        return False
    except (FetchError, ParseError) as ex:
        _fail(result, fanhao, str(ex))
        return True
    fanhao = meta['fanhao']
    title = meta['title']
    if meta['release_date'] is None:
        _fail(result, fanhao, '缺少发行日期')
        return True
    meta['url'] = path
    if save(meta, tags) is None:
        result.skipped += 1
        logger.debug(f'{fanhao} 已在库里，跳过')
    else:
        result.added += 1
        logger.info(f'新增 {fanhao} {title[:30]}')
    return True


def _download_pages(fetcher, result, count, max_page):
    empty_pages = 0
    page = 1
    while True:
        if result.added >= count:
            result.stop_reason = f'已新增 {count} 部'
            return
        if page > max_page:
            result.stop_reason = f'已到最大页数 {max_page}'
            return
        try:
            html = fetcher.get_html('/' if page == 1 else f'/page/{page}')
        except BlockedError as ex:
            result.blocked = str(ex)
            result.stop_reason = '被拦截，停止抓取'
            return
        except FetchError as ex:
            result.error = f'列表第 {page} 页读取失败：{ex}'
            result.stop_reason = '列表页读取失败'
            return
        result.pages += 1
        paths = parse_list(html)
        if not paths:
            result.error = f'列表第 {page} 页没有找到影片（页面结构可能变了，或被拦截）'
            result.stop_reason = '列表页解析失败'
            return
        new_on_page = 0
        for path in paths:
            if result.added >= count:
                break
            fanhao = fanhao_from_path(path)
            if Item.get_by_fanhao(fanhao):
                result.skipped += 1
                continue
            new_on_page += 1
            if not _download_item(fetcher, result, path, fanhao):
                result.stop_reason = '被拦截，停止抓取'
                return
        logger.info(f'第 {page} 页完成：已新增 {result.added} 部')
        if result.added >= count:
            continue
        empty_pages = 0 if new_on_page else empty_pages + 1
        if empty_pages >= EMPTY_PAGES_LIMIT:
            result.stop_reason = f'已追上：连续 {EMPTY_PAGES_LIMIT} 页没有新影片'
            return
        if not has_next_page(html):
            result.stop_reason = '没有下一页了'
            return
        page += 1


def _finish(result, start):
    result.elapsed = time.monotonic() - start
    if result.ok:
        logger.info(str(result))
    else:
        logger.warning(str(result))


def download(count=None, max_page=None, fetcher=None):
    '''
    从首页起逐页抓取，跳过库里已有的番号。count 是最多新增的影片数，
    max_page 是最多读的列表页数，默认取 config.ini。已有抓取在运行时直接返回。
    '''
    count = int(APP_CONFIG['download.count']) if count is None else count
    max_page = int(APP_CONFIG['download.max_page']) if max_page is None else max_page
    result = DownloadResult()
    if not _lock.acquire(blocking=False):
        result.stop_reason = '已有抓取任务在运行，本次跳过'
        logger.warning(result.stop_reason)
        return result
    start = time.monotonic()
    own_fetcher = fetcher is None
    try:
        if own_fetcher:
            fetcher = create_fetcher()
        logger.info(f'开始抓取：最多新增 {count} 部，最多读 {max_page} 页')
        _download_pages(fetcher, result, count, max_page)
    finally:
        if own_fetcher and fetcher is not None:
            fetcher.close()
        _lock.release()
    _finish(result, start)
    return result


def download_fanhaos(fanhaos, fetcher=None):
    '''
    按番号抓取详情页（本地番号上传、导入打标库时用），库里已有的跳过。
    番号会拼进网址，不合法的记为失败、不发请求。已有抓取在运行时等它结束再开始。
    '''
    wanted = []
    for fanhao in fanhaos:
        fanhao = fanhao.strip()
        if fanhao and fanhao not in wanted:
            wanted.append(fanhao)
    result = DownloadResult()
    with _lock:
        start = time.monotonic()
        own_fetcher = fetcher is None
        try:
            if own_fetcher:
                fetcher = create_fetcher()
            logger.info(f'开始按番号抓取 {len(wanted)} 部')
            for fanhao in wanted:
                if not is_valid_fanhao(fanhao):
                    _fail(result, fanhao, '番号不合法')
                    continue
                if Item.get_by_fanhao(fanhao):
                    result.skipped += 1
                    continue
                if not _download_item(fetcher, result, '/' + fanhao, fanhao):
                    result.stop_reason = '被拦截，停止抓取'
                    break
        finally:
            if own_fetcher and fetcher is not None:
                fetcher.close()
    _finish(result, start)
    return result
