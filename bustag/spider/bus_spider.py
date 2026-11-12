'''
抓取：从首页起逐页读列表，抓新影片的详情页并入库
'''
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from bustag.spider.db import (DBError, Item, find_item, replace_with_detail, save, save_candidates)
from bustag.spider.fetcher import BlockedError, FetchError, create_fetcher
from bustag.spider.parser import (DATED_SUFFIX, ParseError, detail_path_for, fanhao_from_path, has_next_page,
                                  parse_code_entries, parse_item, parse_list, same_code)
from bustag.util import APP_CONFIG, logger

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


def _fail(result, fanhao, reason):
    result.failed.append((fanhao, reason))
    logger.warning(f'{fanhao} 抓取失败：{reason}')


def _in_library(fanhao, path):
    '''
    番号已经入库，或者这个详情页已经入库（以前的版本把带日期后缀的那部存在不带后缀的番号下）
    '''
    return Item.select().where((Item.fanhao == fanhao) | (Item.url == path)).exists()


def fetch_detail(fetcher, path, fanhao, proceed=None):
    '''
    读一部影片的详情页，返回入库用的 (meta, tags)。读不到抛 FetchError（被拦截是 BlockedError），
    不是详情页或者没有发行日期抛 ParseError。proceed 是等完间隔、发请求之前再问一次的函数（见 Fetcher.get），
    说不发时抛 RequestCancelled
    '''
    html = fetcher.get_html(path) if proceed is None else fetcher.get_html(path, proceed=proceed)
    meta, tags = parse_item(html)
    if DATED_SUFFIX.search(fanhao):
        # 同番号的第二部影片，页面上的識別碼和第一部相同，用路径里带日期的编号入库
        meta['fanhao'] = fanhao
    if meta['release_date'] is None:
        raise ParseError('缺少发行日期')
    meta['url'] = path
    return meta, tags


def _download_item(fetcher, result, path, fanhao):
    '''
    抓一部影片并入库，被拦截时返回 False（应停止抓取）
    '''
    try:
        meta, tags = fetch_detail(fetcher, path, fanhao)
    except BlockedError as ex:
        result.blocked = str(ex)
        return False
    except (FetchError, ParseError) as ex:
        _fail(result, fanhao, str(ex))
        return True
    fanhao = meta['fanhao']
    if save(meta, tags) is None:
        result.skipped += 1
        logger.debug(f'{fanhao} 已在库里，跳过')
    else:
        result.added += 1
        logger.info(f'新增 {fanhao}')
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
            if _in_library(fanhao, path):
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


def search_path(fanhao):
    '''JavBus 按識別碼搜索的路径。parent=ce 是有码搜索，和站点搜索框一致'''
    return f'/search/{same_code(fanhao)}&type=&parent=ce'


def record_alternates(fanhao, fetcher=None):
    '''
    把这个番号在搜索结果里的其他详情路径记到影片上。
    失败只记日志，返回 None；没有其他条目返回 0；有则返回条数。番号还不在库里时返回 0、不发请求
    '''
    item = find_item(fanhao)
    if item is None:
        return 0
    own = fetcher is None
    try:
        if own:
            fetcher = create_fetcher()
        try:
            html = fetcher.get_html(search_path(item.fanhao))
        except (FetchError, ParseError) as ex:
            logger.warning(f'{item.fanhao} 查找其他条目失败：{ex}')
            return None
        entries = parse_code_entries(html, item.fanhao)
    finally:
        if own and fetcher is not None:
            fetcher.close()
    save_candidates(item.fanhao, entries)
    current = urlparse(item.url).path
    others = [entry for entry in entries if entry['path'] != current]
    if others:
        logger.info(f'{item.fanhao} 还有 {len(others)} 条其他详情')
    return len(others)


def _after_fanhao(fetcher, fanhao):
    '''记下其他详情。失败不影响抓取'''
    try:
        record_alternates(fanhao, fetcher=fetcher)
    except Exception:
        logger.exception(f'{fanhao} 查找其他条目失败')


def refetch_entry(fanhao, raw_path, fetcher=None):
    '''
    用用户选的详情路径替换这一条。返回 replaced / same / badpath / fetch。抓取失败不改库
    '''
    item = Item.get_by_fanhao(fanhao)
    if item is None:
        return 'fetch'
    path = detail_path_for(raw_path, fanhao)
    if path is None:
        return 'badpath'
    if item.url == path:
        return 'same'
    own = fetcher is None
    try:
        if own:
            fetcher = create_fetcher()
        try:
            meta, tags = parse_item(fetcher.get_html(path))
        except (FetchError, ParseError) as ex:
            logger.warning(f'{fanhao} 改选详情失败：{ex}')
            return 'fetch'
    finally:
        if own and fetcher is not None:
            fetcher.close()
    if same_code(meta.get('fanhao', '')) != same_code(fanhao):
        logger.warning(f'{fanhao} 的详情識別碼和所选路径不是同一个番号')
        return 'badpath'
    path_fanhao = path.strip('/')
    meta['fanhao'] = path_fanhao if DATED_SUFFIX.search(path_fanhao) else same_code(meta['fanhao'])
    meta['url'] = path
    if not meta.get('release_date'):
        logger.warning(f'{fanhao} 改选的详情没有发行日期')
        return 'fetch'
    try:
        replace_with_detail(fanhao, meta, tags)
    except DBError as ex:
        logger.warning(f'{fanhao} 改选详情没有写入：{ex}')
        return 'fetch'
    return 'replaced'
