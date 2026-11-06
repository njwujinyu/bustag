'''
抓取循环：用 FakeFetcher 模拟网站，验证停止条件、跳过已有影片、失败记录和抓取锁
'''
import json
import threading
import time

import pytest

from bustag import util
from bustag.spider import bus_spider, db
from bustag.spider.bus_spider import DownloadResult, download, download_fanhaos
from bustag.spider.fetcher import BlockedError, FetchError
from bustag.spider.parser import DATED_SUFFIX, fanhao_from_path

BLOCKED = '被年龄验证页拦截（跳转到了 /doc/driver-verify）。'


def list_page(fanhaos, has_next=True):
    '''只含解析用到的结构：影片框 a.movie-box 和分页里的 a#next'''
    boxes = ''.join(f'<a class="movie-box" href="https://www.javbus.com/{f}"></a>' for f in fanhaos)
    pagination = '<ul class="pagination"><li><a id="next" href="#">下一頁</a></li></ul>' if has_next else ''
    return f'<div id="waterfall">{boxes}</div>{pagination}'


@pytest.fixture
def detail(page):
    '''按番号生成详情页（ABP-001 样本换番号），可以再替换其中的文字'''
    template = page('detail-ABP-001-series-no-director.html')

    def make(fanhao, **replace):
        html = template.replace('ABP-001', fanhao)
        for old, new in replace.items():
            html = html.replace(old, new)
        return html
    return make


@pytest.fixture
def site(fake_fetcher, detail):
    '''
    list_pages 依次是第 1、2… 页列表；其他详情页按路径里的番号生成（和真网站一样，
    带日期后缀的那部页面上的識別碼不带后缀），没有给出的列表页返回 404
    '''
    def build(list_pages, routes=None):
        pages = {('/' if n == 1 else f'/page/{n}'): html for n, html in enumerate(list_pages, 1)}

        def default(path):
            if path == '/' or path.startswith('/page/'):
                return None
            return detail(DATED_SUFFIX.sub('', fanhao_from_path(path)))
        return fake_fetcher({**pages, **(routes or {})}, default=default)
    return build


@pytest.fixture
def known(make_item):
    def add(*fanhaos):
        for fanhao in fanhaos:
            make_item(fanhao)
    return add


def saved_fanhaos():
    return [item.fanhao for item in db.Item.select().order_by(db.Item.id)]


class TestStopConditions:
    def test_stops_after_count(self, site):
        fetcher = site([list_page(['A-001', 'A-002', 'A-003', 'A-004', 'A-005'])])
        result = download(count=3, max_page=5, fetcher=fetcher)
        assert result.ok
        assert (result.added, result.pages, result.stop_reason) == (3, 1, '已新增 3 部')
        assert fetcher.requests == ['/', '/A-001', '/A-002', '/A-003']
        assert saved_fanhaos() == ['A-001', 'A-002', 'A-003']

    def test_stops_after_three_pages_without_new_movies(self, site, known):
        known('A-001', 'A-002', 'A-003', 'A-004', 'A-005')
        fetcher = site([list_page(['A-001', 'A-002']), list_page(['A-003']), list_page(['A-004']),
                        list_page(['A-005'])])
        result = download(count=10, max_page=10, fetcher=fetcher)
        assert result.stop_reason == '已追上：连续 3 页没有新影片'
        assert (result.pages, result.added, result.skipped) == (3, 0, 4)
        assert fetcher.requests == ['/', '/page/2', '/page/3']

    def test_a_new_movie_resets_the_empty_page_count(self, site, known):
        known('A-001', 'A-002', 'A-003', 'A-004', 'A-005', 'A-006')
        fetcher = site([list_page(['A-001']), list_page(['A-002']), list_page(['NEW-001']),
                        list_page(['A-003']), list_page(['A-004']), list_page(['A-005']),
                        list_page(['A-006'])])
        result = download(count=10, max_page=10, fetcher=fetcher)
        assert result.stop_reason == '已追上：连续 3 页没有新影片'
        assert (result.pages, result.added) == (6, 1)

    def test_stops_at_max_page(self, site):
        fetcher = site([list_page(['A-001']), list_page(['A-002']), list_page(['A-003'])])
        result = download(count=10, max_page=2, fetcher=fetcher)
        assert result.stop_reason == '已到最大页数 2'
        assert (result.pages, result.added) == (2, 2)
        assert '/page/3' not in fetcher.requests

    def test_stops_on_the_last_page(self, site):
        fetcher = site([list_page(['A-001']), list_page(['A-002'], has_next=False)])
        result = download(count=10, max_page=10, fetcher=fetcher)
        assert result.ok
        assert result.stop_reason == '没有下一页了'
        assert (result.pages, result.added) == (2, 2)


class TestBlockedAndErrors:
    def test_blocked_list_page(self, site):
        fetcher = site([], routes={'/': BlockedError(BLOCKED)})
        result = download(count=10, max_page=10, fetcher=fetcher)
        assert not result.ok
        assert result.blocked == BLOCKED
        assert result.stop_reason == '被拦截，停止抓取'
        assert result.pages == 0
        assert f'。被拦截：{BLOCKED}' in str(result)

    def test_blocked_detail_page_stops_immediately(self, site):
        fetcher = site([list_page(['A-001', 'A-002', 'A-003'])], routes={'/A-002': BlockedError(BLOCKED)})
        result = download(count=10, max_page=10, fetcher=fetcher)
        assert result.blocked == BLOCKED
        assert result.stop_reason == '被拦截，停止抓取'
        assert result.added == 1
        assert fetcher.requests == ['/', '/A-001', '/A-002']

    def test_list_page_fetch_error_keeps_earlier_results(self, site):
        fetcher = site([list_page(['A-001'])], routes={'/page/2': FetchError('HTTP 500，已重试 3 次')})
        result = download(count=10, max_page=10, fetcher=fetcher)
        assert not result.ok
        assert result.error == '列表第 2 页读取失败：HTTP 500，已重试 3 次'
        assert result.stop_reason == '列表页读取失败'
        assert (result.pages, result.added) == (1, 1)

    def test_list_page_without_movies(self, site, page):
        # 没有跳转、直接返回验证页内容的情况
        fetcher = site([page('driver-verify.html')])
        result = download(count=10, max_page=10, fetcher=fetcher)
        assert result.error == '列表第 1 页没有找到影片（页面结构可能变了，或被拦截）'
        assert result.stop_reason == '列表页解析失败'
        assert result.pages == 1

    def test_failed_movies_are_recorded_and_the_crawl_goes_on(self, site, detail, page):
        fetcher = site([list_page(['A-001', 'A-002', 'A-003', 'A-004'], has_next=False)], routes={
            '/A-001': FetchError('页面不存在（HTTP 404）：https://www.javbus.com/A-001'),
            '/A-002': page('list-home.html'),
            '/A-003': detail('A-003', **{'2013-06-28': ''}),
        })
        result = download(count=10, max_page=10, fetcher=fetcher)
        assert result.ok
        assert result.added == 1
        assert [fanhao for fanhao, _ in result.failed] == ['A-001', 'A-002', 'A-003']
        reasons = [reason for _, reason in result.failed]
        assert reasons[0] == '页面不存在（HTTP 404）：https://www.javbus.com/A-001'
        assert reasons[1].startswith('不是影片详情页')
        assert reasons[2] == '缺少发行日期'
        assert saved_fanhaos() == ['A-004']


def saved_movies():
    return [(item.fanhao, item.url) for item in db.Item.select().order_by(db.Item.id)]


class TestSkipAndSave:
    def test_known_movies_are_skipped(self, site, known):
        known('A-001')
        fetcher = site([list_page(['A-001', 'A-002'], has_next=False)])
        result = download(count=10, max_page=10, fetcher=fetcher)
        assert (result.added, result.skipped) == (1, 1)
        assert fetcher.requests == ['/', '/A-002']

    def test_both_movies_of_one_fanhao_are_saved(self, site):
        lists = [list_page(['A-001_2026-10-31', 'A-001', 'A-002'], has_next=False)]
        result = download(count=10, max_page=10, fetcher=site(lists))
        assert (result.added, result.skipped) == (3, 0)
        assert saved_movies() == [('A-001_2026-10-31', '/A-001_2026-10-31'), ('A-001', '/A-001'), ('A-002', '/A-002')]
        fetcher = site(lists)
        result = download(count=10, max_page=10, fetcher=fetcher)
        assert (result.added, result.skipped) == (0, 3)
        assert fetcher.requests == ['/']

    def test_dated_movie_saved_under_the_plain_fanhao_is_skipped(self, site):
        # 以前的版本把带日期后缀的那部存在不带后缀的番号下，url 是带后缀的路径
        db.save({'fanhao': 'A-001', 'title': '作品 A-001', 'url': '/A-001_2026-10-31', 'release_date': '2026-10-31',
                 'cover_img_url': '', 'length': '120'}, [])
        fetcher = site([list_page(['A-001_2026-10-31', 'A-001'], has_next=False)])
        result = download(count=10, max_page=10, fetcher=fetcher)
        assert (result.added, result.skipped) == (0, 2)
        assert fetcher.requests == ['/']
        assert saved_movies() == [('A-001', '/A-001_2026-10-31')]

    def test_saved_movie_keeps_paths(self, site):
        fetcher = site([list_page(['B-001_2026-10-31'], has_next=False)])
        download(count=10, max_page=10, fetcher=fetcher)
        item = db.Item.get_by_fanhao('B-001_2026-10-31')
        assert item.url == '/B-001_2026-10-31'
        assert item.release_date.isoformat() == '2013-06-28'
        assert json.loads(item.meta_info) == {'cover_img_url': '/pics/cover/2qh5_b.jpg', 'length': '125'}
        tag_urls = [item_tag.tag.url for item_tag in item.tags_list]
        assert tag_urls and all(url.startswith('/') and '//' not in url for url in tag_urls)

    def test_crawl_over_saved_list_pages(self, site, page):
        fetcher = site([page('list-home.html'), page('list-page-2.html')])
        result = download(count=30, max_page=5, fetcher=fetcher)
        # 首页 28 个路径（其中 5 个带日期后缀）各是一部影片，全部入库；第 2 页再新增 2 部
        assert (result.pages, result.added, result.skipped) == (2, 30, 0)
        assert result.stop_reason == '已新增 30 部'
        assert db.Item.select().count() == 30
        assert db.Item.get_by_fanhao('SORA-655').url == '/SORA-655'
        assert db.Item.get_by_fanhao('SORA-655_2026-10-31').url == '/SORA-655_2026-10-31'

    def test_defaults_come_from_config(self, site, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.count', '2')
        monkeypatch.setitem(util.APP_CONFIG, 'download.max_page', '1')
        fetcher = site([list_page(['A-001', 'A-002', 'A-003'])])
        result = download(fetcher=fetcher)
        assert result.added == 2
        assert result.stop_reason == '已新增 2 部'


class TestFetcherOwnership:
    def test_own_fetcher_is_created_and_closed(self, site, monkeypatch):
        fetcher = site([list_page([], has_next=False)])
        monkeypatch.setattr(bus_spider, 'create_fetcher', lambda: fetcher)
        download(count=1, max_page=1)
        assert fetcher.closed

    def test_injected_fetcher_is_left_open(self, site):
        fetcher = site([list_page(['A-001'], has_next=False)])
        download(count=1, max_page=1, fetcher=fetcher)
        download_fanhaos(['A-002'], fetcher=fetcher)
        assert not fetcher.closed


class TestLock:
    def test_download_skips_while_another_crawl_runs(self, site):
        fetcher = site([list_page(['A-001'])])
        bus_spider._lock.acquire()
        try:
            result = download(count=1, max_page=1, fetcher=fetcher)
        finally:
            bus_spider._lock.release()
        assert result.stop_reason == '已有抓取任务在运行，本次跳过'
        assert fetcher.requests == []

    def test_download_fanhaos_waits_for_the_running_crawl(self, site):
        fetcher = site([])
        results = []

        def run():
            try:
                results.append(download_fanhaos(['A-001'], fetcher=fetcher))
            finally:
                db.db.close()

        bus_spider._lock.acquire()
        worker = threading.Thread(target=run)
        try:
            worker.start()
            time.sleep(0.2)
            assert fetcher.requests == []
            assert worker.is_alive()
        finally:
            bus_spider._lock.release()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert results[0].added == 1
        assert fetcher.requests == ['/A-001']


class TestDownloadFanhaos:
    def test_dedupes_and_skips_known(self, site, known):
        known('A-001')
        fetcher = site([])
        result = download_fanhaos([' A-002 ', 'A-001', 'A-002', '', 'A-003'], fetcher=fetcher)
        assert fetcher.requests == ['/A-002', '/A-003']
        assert (result.added, result.skipped) == (2, 1)
        assert result.ok

    def test_stops_when_blocked(self, site):
        fetcher = site([], routes={'/A-002': BlockedError(BLOCKED)})
        result = download_fanhaos(['A-001', 'A-002', 'A-003'], fetcher=fetcher)
        assert result.blocked == BLOCKED
        assert result.stop_reason == '被拦截，停止抓取'
        assert fetcher.requests == ['/A-001', '/A-002']

    def test_dated_fanhao(self, site):
        fetcher = site([])
        assert download_fanhaos(['A-001_2026-10-31'], fetcher=fetcher).added == 1
        assert saved_movies() == [('A-001_2026-10-31', '/A-001_2026-10-31')]
        assert download_fanhaos(['A-001_2026-10-31'], fetcher=fetcher).skipped == 1
        assert fetcher.requests == ['/A-001_2026-10-31']

    def test_dated_movie_saved_under_the_plain_fanhao_is_not_requested(self, site):
        db.save({'fanhao': 'A-001', 'title': '作品 A-001', 'url': '/A-001_2026-10-31', 'release_date': '2026-10-31',
                 'cover_img_url': '', 'length': '120'}, [])
        fetcher = site([])
        assert download_fanhaos(['A-001_2026-10-31'], fetcher=fetcher).skipped == 1
        assert fetcher.requests == []

    def test_invalid_fanhaos_are_not_requested(self, site):
        fetcher = site([])
        result = download_fanhaos(['/evil.example/x', '../x', '含 空格', 'A-001'], fetcher=fetcher)
        assert fetcher.requests == ['/A-001']
        assert result.failed == [('/evil.example/x', '番号不合法'), ('../x', '番号不合法'), ('含 空格', '番号不合法')]
        assert result.added == 1


class TestDownloadResult:
    def test_summary(self):
        result = DownloadResult(pages=2, added=3, skipped=4, failed=[('A-001', 'x')],
                                stop_reason='没有下一页了', elapsed=12.4)
        assert result.ok
        assert str(result) == '抓取结束：列表页 2 页，新增 3 部，跳过 4 部，失败 1 部，用时 12 秒（没有下一页了）'

    def test_summary_with_problems(self):
        result = DownloadResult(blocked='HTTP 429', error='列表第 1 页读取失败：x')
        assert not result.ok
        assert str(result) == ('抓取结束：新增 0 部，跳过 0 部，失败 0 部，用时 0 秒'
                               '。被拦截：HTTP 429。出错：列表第 1 页读取失败：x')
