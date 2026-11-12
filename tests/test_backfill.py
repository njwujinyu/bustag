'''
抓取队列和按演员补全作品：上传番号、导入打标库时库里还没有的番号，以及从演员页上找到、库里还没有的影片，
记在库里（db.FetchTask），由后台任务一部一部地抓（backfill.run）。网站用 FakeFetcher 代替
'''
import contextlib
import datetime
import logging
import threading
import time

import pytest
from peewee import OperationalError

from bustag import util
from bustag.spider import actresses, backfill, bus_spider, db
from bustag.spider.bus_spider import search_path
from bustag.spider.fetcher import BlockedError, FetchError, NetworkError, NotFoundError
from bustag.spider.parser import Tag, list_page_path, searchstar_path

BLOCKED = '被年龄验证页拦截（跳转到了 /doc/driver-verify）。'
STAR_A = '/star/a1'
STAR_B = '/star/b2'
DAY = datetime.datetime(2026, 11, 6, 12, 0, 0)


def tasks():
    '''队列里每一行：(番号, 来处, 状态, 原因, 试过几次)，按排进来的先后'''
    return [(task.fanhao, task.origin, task.status, task.detail, task.tries)
            for task in db.FetchTask.select().order_by(db.FetchTask.id)]


def statuses():
    return {task.fanhao: task.status for task in db.FetchTask.select()}


def saved():
    return [item.fanhao for item in db.Item.select().order_by(db.Item.id)]


@pytest.fixture
def site(fake_fetcher, detail_page):
    '''
    没有给出的详情页按路径里的番号生成（同番号的第二部，页面上的識別碼不带日期后缀，和真网站一样）；
    按番号搜索其他条目的页面默认什么都没有
    '''
    def build(routes=None):
        def default(path):
            if path.startswith('/search/'):
                return '<html></html>'
            return detail_page(bus_spider.DATED_SUFFIX.sub('', path.strip('/')))
        return fake_fetcher(routes or {}, default=default)
    return build


def upload(*fanhaos):
    return backfill.enqueue_codes(list(fanhaos), db.ORIGIN_UPLOAD)


class TestEnqueueCodes:
    def test_codes_are_queued_once_in_the_order_given(self):
        assert upload('ABC-002', 'ABC-001', 'ABC-002') == 2
        assert tasks() == [('ABC-002', 'upload', 'queued', '', 0), ('ABC-001', 'upload', 'queued', '', 0)]
        assert [task.path for task in db.FetchTask.select().order_by(db.FetchTask.id)] == ['/ABC-002', '/ABC-001']

    def test_codes_that_cannot_be_put_in_an_address_are_not_queued(self):
        assert upload('/evil.example/x', '../x', '含 空格', '', 'ABC-001') == 1
        assert [fanhao for fanhao, *_ in tasks()] == ['ABC-001']

    def test_a_code_already_queued_is_not_queued_twice(self):
        assert upload('ABC-001') == 1
        assert backfill.enqueue_codes(['ABC-001'], db.ORIGIN_IMPORT) == 0
        assert tasks() == [('ABC-001', 'upload', 'queued', '', 0)]

    def test_imported_codes_are_marked_as_imported(self):
        assert backfill.enqueue_codes(['ABC-001'], db.ORIGIN_IMPORT) == 1
        assert tasks() == [('ABC-001', 'import', 'queued', '', 0)]

    @pytest.mark.parametrize('status', ['failed', 'missing'])
    def test_a_code_that_was_not_fetched_is_queued_again(self, status):
        upload('ABC-001')
        db.FetchTask.update(status=status, detail='原因', tries=3).execute()
        assert upload('ABC-001') == 1
        assert tasks() == [('ABC-001', 'upload', 'queued', '', 0)]


class TestRun:
    def test_each_code_is_fetched_and_its_other_entries_are_looked_up(self, site):
        upload('A-001', 'A-002')
        fetcher = site()
        result = backfill.run(fetcher=fetcher)
        assert fetcher.requests == ['/A-001', search_path('A-001'), '/A-002', search_path('A-002')]
        assert (result.fetched, result.added, result.left) == (2, 2, 0)
        assert saved() == ['A-001', 'A-002']
        assert statuses() == {'A-001': 'done', 'A-002': 'done'}
        assert all(task.tried_at is not None for task in db.FetchTask.select())

    def test_nothing_queued_sends_nothing(self, site):
        fetcher = site()
        result = backfill.run(fetcher=fetcher)
        assert fetcher.requests == []
        assert (result.fetched, result.added, result.left, result.stopped) == (0, 0, 0, '')

    def test_a_movie_that_got_into_the_library_meanwhile_is_not_requested_again(self, site, make_item):
        upload('A-001', 'A-002')
        make_item('A-001')
        fetcher = site()
        result = backfill.run(fetcher=fetcher)
        # 和以前一样，库里已有的也查一次同番号的其他条目
        assert fetcher.requests == [search_path('A-001'), '/A-002', search_path('A-002')]
        assert (result.fetched, result.added, result.skipped) == (1, 1, 1)
        assert statuses() == {'A-001': 'done', 'A-002': 'done'}
        assert db.FetchTask.get(db.FetchTask.fanhao == 'A-001').tried_at is None

    def test_dated_code(self, site):
        upload('A-001_2026-10-31')
        fetcher = site()
        assert backfill.run(fetcher=fetcher).added == 1
        assert [(item.fanhao, item.url) for item in db.Item.select()] == [('A-001_2026-10-31', '/A-001_2026-10-31')]
        assert fetcher.requests == ['/A-001_2026-10-31', search_path('A-001')]

    def test_dated_movie_saved_under_the_plain_code_is_not_requested(self, site):
        db.save({'fanhao': 'A-001', 'title': '作品 A-001', 'url': '/A-001_2026-10-31', 'release_date': '2026-10-31',
                 'cover_img_url': '', 'length': '120'}, [])
        upload('A-001_2026-10-31')
        fetcher = site()
        assert backfill.run(fetcher=fetcher).skipped == 1
        assert fetcher.requests == [search_path('A-001_2026-10-31')]

    def test_other_entries_found_on_a_saved_search_page_are_recorded(self, site, page):
        fetcher = site({search_path('ABC-001'): page('same-code-entries.html')})
        upload('ABC-001')
        assert backfill.run(fetcher=fetcher).added == 1
        assert db.Item.get_by_fanhao('ABC-001').url == '/ABC-001'
        assert [row.path for row in db.other_entries('ABC-001')] == ['/ABC-001_2026-01-02']

    def test_other_entries_of_the_code_are_recorded(self, site, detail_page):
        box = ('<a class="movie-box" href="https://www.javbus.com/{path}"><date>ABC-001</date>'
               '<date>2026-01-02</date></a>')
        fetcher = site({search_path('ABC-001'): box.format(path='ABC-001') + box.format(path='ABC-001_2026-01-02')})
        upload('ABC-001')
        backfill.run(fetcher=fetcher)
        assert [row.path for row in db.other_entries('ABC-001')] == ['/ABC-001_2026-01-02']

    def test_a_search_for_other_entries_that_fails_does_not_stop_the_queue(self, site):
        fetcher = site({search_path('A-001'): FetchError('搜索失败'), search_path('A-002'): BlockedError(BLOCKED)})
        upload('A-001', 'A-002', 'A-003')
        result = backfill.run(fetcher=fetcher)
        assert result.added == 3
        assert result.stopped == ''
        assert saved() == ['A-001', 'A-002', 'A-003']

    def test_a_page_the_site_does_not_have_is_marked_missing(self, site):
        fetcher = site({'/A-001': NotFoundError('页面不存在（HTTP 404）：https://www.javbus.com/A-001')})
        upload('A-001', 'A-002')
        result = backfill.run(fetcher=fetcher)
        assert (result.fetched, result.added, result.missing) == (2, 1, 1)
        assert tasks()[0] == ('A-001', 'upload', 'missing', '页面不存在（HTTP 404）：https://www.javbus.com/A-001', 0)

    def test_pages_that_cannot_be_used_are_marked_failed(self, site, detail_page):
        fetcher = site({'/A-001': '<html><body>不是详情页</body></html>', '/A-002': detail_page('A-002', release='')})
        upload('A-001', 'A-002', 'A-003')
        result = backfill.run(fetcher=fetcher)
        assert (result.added, result.failed) == (1, 2)
        assert tasks()[0][2:] == ('failed', '不是影片详情页：找不到标题或影片信息（可能是验证页，或页面结构变了）', 0)
        assert tasks()[1][2:] == ('failed', '缺少发行日期', 0)
        assert saved() == ['A-003']

    def test_a_movie_that_cannot_be_reached_stays_queued_and_fails_after_three_tries(self, site):
        fetcher = site({'/A-001': NetworkError('网络连接失败（ConnectTimeout）')})
        upload('A-001')
        for tries in (1, 2):
            result = backfill.run(fetcher=fetcher)
            assert (result.retry_later, result.failed, result.left) == (1, 0, 1)
            assert tasks() == [('A-001', 'upload', 'queued', '网络连接失败（ConnectTimeout）', tries)]
        result = backfill.run(fetcher=fetcher)
        assert (result.retry_later, result.failed, result.left) == (0, 1, 0)
        assert tasks() == [('A-001', 'upload', 'failed', '网络连接失败（ConnectTimeout）', 3)]

    def test_server_errors_are_tried_again_like_network_problems(self, site):
        fetcher = site({'/A-001': FetchError('请求失败（HTTP 503）：https://www.javbus.com/A-001')})
        upload('A-001')
        backfill.run(fetcher=fetcher)
        assert tasks() == [('A-001', 'upload', 'queued', '请求失败（HTTP 503）：https://www.javbus.com/A-001', 1)]

    def test_three_failures_to_reach_the_site_in_a_row_stop_the_run(self, site):
        unreachable = NetworkError('网络连接失败（ConnectionError）')
        fetcher = site({f'/A-00{n}': unreachable for n in range(1, 5)})
        upload('A-001', 'A-002', 'A-003', 'A-004')
        result = backfill.run(fetcher=fetcher)
        assert fetcher.requests == ['/A-001', '/A-002', '/A-003']
        assert result.stopped == '连续 3 次连不上网站'
        assert (result.retry_later, result.left) == (3, 4)
        assert statuses() == dict.fromkeys(['A-001', 'A-002', 'A-003', 'A-004'], 'queued')

    def test_an_answer_in_between_resets_the_count_of_failures(self, site):
        unreachable = NetworkError('网络连接失败（ConnectionError）')
        fetcher = site({'/A-001': unreachable, '/A-002': unreachable, '/A-004': unreachable})
        upload('A-001', 'A-002', 'A-003', 'A-004', 'A-005')
        result = backfill.run(fetcher=fetcher)
        assert result.stopped == ''
        assert saved() == ['A-003', 'A-005']

    def test_blocked_stops_at_once_and_the_movie_stays_queued(self, site):
        fetcher = site({'/A-002': BlockedError(BLOCKED)})
        upload('A-001', 'A-002', 'A-003')
        result = backfill.run(fetcher=fetcher)
        assert fetcher.requests == ['/A-001', search_path('A-001'), '/A-002']
        assert result.blocked == BLOCKED
        assert result.stopped == '被网站拦截'
        assert (result.added, result.left) == (1, 2)
        assert tasks()[1:] == [('A-002', 'upload', 'queued', '', 0), ('A-003', 'upload', 'queued', '', 0)]

    def test_a_task_whose_code_cannot_be_put_in_an_address_fails_without_a_request(self, site):
        db.FetchTask.create(path='/../x', fanhao='../x', origin=db.ORIGIN_UPLOAD)
        fetcher = site()
        result = backfill.run(fetcher=fetcher)
        assert fetcher.requests == []
        assert result.failed == 1
        assert tasks() == [('../x', 'upload', 'failed', '番号不合法', 0)]

    def test_a_liked_movie_without_actresses_only_costs_requests_to_javbus(self, site, make_item, detail_page):
        # 补演员名是抓取之后单独的一轮，抓排队的影片本身不去 av-wiki
        db.ItemRate.saveit(db.RATE_TYPE.USER_RATE, db.RATE_VALUE.LIKE, 'A-001')
        upload('A-001')
        fetcher = site()
        backfill.run(fetcher=fetcher)
        assert not db.has_star('A-001')
        assert fetcher.requests == ['/A-001', search_path('A-001')]

    def test_own_fetcher_is_made_without_retries_and_closed(self, site, monkeypatch):
        fetcher = site()
        made = []

        def create_fetcher(**overrides):
            made.append(overrides)
            return fetcher

        monkeypatch.setattr(backfill, 'create_fetcher', create_fetcher)
        upload('A-001')
        backfill.run()
        assert made == [{'retries': 0}]
        assert fetcher.closed

    def test_injected_fetcher_is_left_open(self, site):
        fetcher = site()
        upload('A-001')
        backfill.run(fetcher=fetcher)
        assert not fetcher.closed

    def test_waits_for_the_running_crawl(self, site):
        fetcher = site()
        upload('A-001')
        results = []

        def run():
            try:
                results.append(backfill.run(fetcher=fetcher))
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

    def test_the_last_run_is_remembered(self, site, monkeypatch):
        monkeypatch.setattr(backfill, 'last_run', None)
        upload('A-001')
        result = backfill.run(fetcher=site())
        assert backfill.last_run is result
        assert result.finished_at is not None

    def test_log_names_only_codes_and_numbers(self, site, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        fetcher = site({'/A-002': NotFoundError('页面不存在（HTTP 404）：https://www.javbus.com/A-002')})
        upload('A-001', 'A-002')
        backfill.run(fetcher=fetcher)
        assert '开始抓排队的影片：上传番号和导入打标库的 2 部' in caplog.messages
        assert '新增 A-001' in caplog.messages
        assert 'A-002 网站上没有：页面不存在（HTTP 404）：https://www.javbus.com/A-002' in caplog.messages
        assert '抓排队的影片结束：抓了 2 部，新增 1 部，网站上没有 1 部，失败 0 部' in caplog.messages
        assert '示例影片' not in caplog.text


class TestCodeQueue:
    '''上传番号页上的"下载队列"：还在排队的个数，没抓到的逐个列出，失败的可以再试'''

    def problems(self):
        return [(row.fanhao, row.status, row.detail) for row in backfill.code_queue().problems]

    def test_counts_what_waits_and_lists_what_was_not_fetched(self):
        upload('A-001', 'A-002', 'A-003', 'A-004')
        backfill.enqueue_codes(['B-001'], db.ORIGIN_IMPORT)
        db.FetchTask.update(status='failed', detail='缺少发行日期').where(db.FetchTask.fanhao == 'A-002').execute()
        db.FetchTask.update(status='missing', detail='页面不存在').where(db.FetchTask.fanhao == 'A-003').execute()
        db.FetchTask.update(status='done').where(db.FetchTask.fanhao == 'A-004').execute()
        queue = backfill.code_queue()
        assert queue.waiting == 2
        assert self.problems() == [('A-003', 'missing', '页面不存在'), ('A-002', 'failed', '缺少发行日期')]

    def test_movies_queued_by_the_actress_pages_are_not_listed(self):
        source = db.ListSource.create(path='/star/a1')
        db.FetchTask.create(path='/A-001', fanhao='A-001', origin=db.ORIGIN_LIST, source=source, status='failed')
        db.FetchTask.create(path='/A-002', fanhao='A-002', origin=db.ORIGIN_LIST, source=source)
        queue = backfill.code_queue()
        assert (queue.waiting, queue.problems) == (0, [])

    def test_failed_codes_can_be_tried_again(self):
        upload('A-001', 'A-002', 'A-003')
        db.FetchTask.update(status='failed', detail='缺少发行日期', tries=3).where(
            db.FetchTask.fanhao.in_(['A-001', 'A-002'])).execute()
        db.FetchTask.update(status='missing', detail='页面不存在').where(db.FetchTask.fanhao == 'A-003').execute()
        assert backfill.retry_codes(['A-001']) == 1
        assert backfill.retry_codes(['A-003', 'NOT-001']) == 0
        assert statuses() == {'A-001': 'queued', 'A-002': 'failed', 'A-003': 'missing'}
        assert tasks()[0] == ('A-001', 'upload', 'queued', '', 0)
        assert backfill.retry_codes() == 1
        assert statuses() == {'A-001': 'queued', 'A-002': 'queued', 'A-003': 'missing'}

    def test_the_list_of_codes_not_fetched_can_be_cleared(self):
        upload('A-001', 'A-002', 'A-003')
        db.FetchTask.update(status='failed').where(db.FetchTask.fanhao == 'A-001').execute()
        db.FetchTask.update(status='missing').where(db.FetchTask.fanhao == 'A-002').execute()
        assert backfill.forget_problems() == 2
        assert statuses() == {'A-003': 'queued'}
        assert backfill.code_queue().problems == []


def codes(prefix, newest, oldest=1):
    '''演员页上的番号，从新到旧：prefix-newest … prefix-oldest'''
    return [f'{prefix}-{n:03d}' for n in range(newest, oldest - 1, -1)]


def released(fanhao):
    '''编号越大发行得越晚'''
    return (datetime.date(2020, 1, 1) + datetime.timedelta(days=int(fanhao.rsplit('-', 1)[1]))).isoformat()


def list_html(fanhaos, counts=None, has_next=False):
    '''演员页的一页：上方写着两个数，第一个框是演员本人，后面是影片框'''
    boxes = ''.join(f'<div class="item"><a class="movie-box" href="https://www.javbus.com/{fanhao}">'
                    f'<span>示例影片<br><date>{fanhao}</date> / <date>{released(fanhao)}</date></span></a></div>'
                    for fanhao in fanhaos)
    numbers = f'<div class="alert">已有磁力 {counts[0]} / 全部影片 {counts[1]}</div>' if counts else ''
    nav = '<ul class="pagination"><li><a id="next" href="#">下一頁</a></li></ul>' if has_next else ''
    return (f'<html><body>{numbers}<div id="waterfall"><div class="item"><div class="avatar-box">演员</div></div>'
            f'{boxes}</div>{nav}</body></html>')


def actress_pages(path, fanhaos, total=None, per_page=30):
    '''演员页 path 的各页（fanhaos 从新到旧，每页 per_page 部），{路径: html}'''
    chunks = [fanhaos[n:n + per_page] for n in range(0, len(fanhaos), per_page)] or [[]]
    counts = (len(fanhaos), total or len(fanhaos))
    return {list_page_path(path, n): list_html(chunk, counts, has_next=n < len(chunks))
            for n, chunk in enumerate(chunks, 1)}


def source(path=STAR_A):
    return db.ListSource.get(db.ListSource.path == path)


def queued_of(path=STAR_A):
    '''这位演员排在队里、还没抓的番号，按排进去的先后'''
    return [task.fanhao for task in db.FetchTask.select().join(db.ListSource)
            .where((db.ListSource.path == path) & (db.FetchTask.status == 'queued')).order_by(db.FetchTask.id)]


def list_requests(fetcher):
    return [path for path in fetcher.requests if path.startswith('/star/')]


def detail_requests(fetcher):
    return [path for path in fetcher.requests if not path.startswith(('/star/', '/search'))]


@pytest.fixture
def today(monkeypatch):
    '''把"现在"钉在 DAY（可以改：today.now = …）'''
    class Clock:
        now = DAY

    clock = Clock()
    monkeypatch.setattr(backfill, 'get_now_time', lambda: clock.now)
    return clock


@pytest.fixture
def library(make_item):
    '''库里已经有的影片'''
    def add(*fanhaos):
        for fanhao in fanhaos:
            make_item(fanhao, release_date=released(fanhao))
    return add


class TestCheck:
    '''「查看」：只读演员页的第 1 页，记下"已有磁力""全部影片"'''

    def test_only_the_first_page_is_read(self, site, today):
        fetcher = site(actress_pages(STAR_A, codes('A', 95), total=120))
        backfill.request_check(STAR_A)
        assert source().checking is True
        result = backfill.run(fetcher=fetcher)
        assert fetcher.requests == [STAR_A]
        found = source()
        assert (found.magnets, found.total, found.checked_at, found.checking) == (95, 120, DAY, False)
        assert (result.pages, result.queued, result.fetched) == (1, 0, 0)
        assert db.FetchTask.select().count() == 0

    def test_an_actress_without_movies_that_have_magnets(self, site, today):
        fetcher = site({STAR_A: list_html([], counts=(0, 3))})
        backfill.request_check(STAR_A)
        backfill.run(fetcher=fetcher)
        assert (source().magnets, source().total, source().error) == (0, 3, '')

    def test_a_page_that_cannot_be_read_is_reported(self, site, today):
        fetcher = site({STAR_A: FetchError('请求失败（HTTP 500）：https://www.javbus.com/star/a1')})
        backfill.request_check(STAR_A)
        backfill.run(fetcher=fetcher)
        found = source()
        assert (found.checking, found.magnets) == (False, None)
        assert found.error == '第 1 页读不到：请求失败（HTTP 500）：https://www.javbus.com/star/a1'
        assert found.error_at == DAY

    def test_a_page_with_movies_but_without_the_numbers_is_checked_without_them(self, site, today):
        backfill.request_check(STAR_A)
        backfill.run(fetcher=site({STAR_A: list_html(codes('A', 3))}))
        found = source()
        assert (found.magnets, found.total, found.checked_at, found.checking, found.error) == (None, None, DAY, False, '')

    def test_a_page_with_neither_movies_nor_numbers_is_reported(self, site, today):
        fetcher = site({STAR_A: '<html><body>别的页面</body></html>'})
        backfill.request_check(STAR_A)
        backfill.run(fetcher=fetcher)
        assert source().error == '第 1 页没有找到影片，也没有"已有磁力"的数字（页面结构可能变了，或被拦截）'

    def test_reading_it_again_clears_the_earlier_error(self, site, today):
        backfill.request_check(STAR_A)
        db.ListSource.update(error='第 1 页读不到：x', error_at=DAY).execute()
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 3))))
        assert (source().error, source().error_at, source().magnets) == ('', None, 3)

    @pytest.mark.parametrize('path', ['/star/演员甲', '/genre/a1', '/star/a1/2', 'https://evil.example/star/a1'])
    def test_only_actress_pages_can_be_checked(self, path):
        with pytest.raises(ValueError):
            backfill.request_check(path)
        assert db.ListSource.select().count() == 0


class TestFill:
    '''「补全」：从第 1 页往后翻，把库里没有的影片排进队列，一次最多 backfill_count 部（默认 60），排进来的接着抓'''

    def test_the_newest_movies_not_in_the_library_are_queued_and_fetched(self, site, library, today):
        fanhaos = codes('A', 95)
        library('A-094', 'A-080', 'A-070', 'A-050', 'A-040')
        fetcher = site(actress_pages(STAR_A, fanhaos))
        assert backfill.request_fill(STAR_A) is None
        assert (source().wanted, source().requested_at) == (60, DAY)
        result = backfill.run(fetcher=fetcher)
        expected = [fanhao for fanhao in fanhaos if fanhao not in ('A-094', 'A-080', 'A-070', 'A-050', 'A-040')][:60]
        assert list_requests(fetcher) == [STAR_A, '/star/a1/2', '/star/a1/3']
        assert detail_requests(fetcher) == ['/' + fanhao for fanhao in expected]
        assert (result.pages, result.queued, result.fetched, result.added, result.left) == (3, 60, 60, 60, 0)
        found = source()
        # 停在第 3 页中间：下次从第 3 页接着读，已经排过的会跳过
        assert (found.wanted, found.next_page, found.reached_end) == (0, 3, False)
        assert found.oldest == datetime.date.fromisoformat(released(expected[-1]))
        assert (found.magnets, found.total) == (95, 95)
        tasks_ = list(db.FetchTask.select().order_by(db.FetchTask.id))
        assert [task.fanhao for task in tasks_] == expected
        assert {(task.origin, task.status) for task in tasks_} == {('list', 'done')}
        assert [task.page for task in tasks_][:3] == [1, 1, 1] and tasks_[-1].page == 3

    def test_the_batch_size_comes_from_the_config(self, site, today, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_count', '5')
        fetcher = site(actress_pages(STAR_A, codes('A', 95)))
        backfill.request_fill(STAR_A)
        result = backfill.run(fetcher=fetcher)
        assert list_requests(fetcher) == [STAR_A]
        assert (result.queued, source().next_page) == (5, 1)

    def test_continuing_looks_at_the_first_page_for_new_movies_then_goes_back_where_it_stopped(self, site, library,
                                                                                                 today):
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 95))))
        # 这段时间又出了两部新片，旧片都往后挪了两个位置
        fetcher = site(actress_pages(STAR_A, codes('A', 97)))
        backfill.request_fill(STAR_A)
        result = backfill.run(fetcher=fetcher)
        assert list_requests(fetcher) == [STAR_A, '/star/a1/3', '/star/a1/4']
        assert detail_requests(fetcher) == ['/A-097', '/A-096'] + ['/' + f for f in codes('A', 35)]
        assert result.queued == 37
        found = source()
        assert (found.reached_end, found.next_page, found.oldest) == (True, 5, datetime.date.fromisoformat(released('A-001')))
        assert db.Item.select().count() == 97

    def test_when_the_whole_first_page_is_new_the_next_page_is_read_too(self, site, today):
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 95))))
        fetcher = site(actress_pages(STAR_A, codes('A', 130)))
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=fetcher)
        # 新片 35 部：第 1 页全是新的，第 2 页前 5 部是新的；然后回到上次停下的第 3 页（现在是第 4 页的位置）往回补
        assert list_requests(fetcher)[:3] == [STAR_A, '/star/a1/2', '/star/a1/3']
        assert detail_requests(fetcher)[:35] == ['/' + f for f in codes('A', 130, 96)]
        assert len(detail_requests(fetcher)) == 60

    def test_after_the_last_page_only_new_movies_are_looked_for(self, site, today):
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 20))))
        assert (source().reached_end, source().oldest) == (True, datetime.date.fromisoformat(released('A-001')))
        fetcher = site(actress_pages(STAR_A, codes('A', 22)))
        backfill.request_fill(STAR_A)
        result = backfill.run(fetcher=fetcher)
        assert list_requests(fetcher) == [STAR_A]
        assert (result.queued, detail_requests(fetcher)) == (2, ['/A-022', '/A-021'])
        fetcher = site(actress_pages(STAR_A, codes('A', 22)))
        backfill.request_fill(STAR_A)
        assert backfill.run(fetcher=fetcher).queued == 0
        assert list_requests(fetcher) == [STAR_A]

    def test_a_movie_on_two_actresses_pages_is_queued_once(self, site, today):
        shared = 'C-050'
        pages = {**actress_pages(STAR_A, codes('A', 10)[:5] + [shared] + codes('A', 5)),
                 **actress_pages(STAR_B, [shared] + codes('B', 9))}
        backfill.request_fill(STAR_A)
        backfill.request_fill(STAR_B)
        fetcher = site(pages)
        result = backfill.run(fetcher=fetcher)
        assert result.queued == 20
        assert detail_requests(fetcher).count('/' + shared) == 1
        assert db.FetchTask.get(db.FetchTask.fanhao == shared).source.path == STAR_A
        assert db.Item.select().count() == 20

    def test_both_movies_of_one_code_are_queued(self, site, today):
        pages = {STAR_A: list_html(['A-002', 'A-001_2026-01-02', 'A-001'], counts=(3, 3))}
        backfill.request_fill(STAR_A)
        fetcher = site(pages)
        backfill.run(fetcher=fetcher)
        assert sorted(item.fanhao for item in db.Item.select()) == ['A-001', 'A-001_2026-01-02', 'A-002']

    def test_movies_already_queued_by_an_upload_are_not_queued_again(self, site, today):
        backfill.enqueue_codes(['A-003'], db.ORIGIN_UPLOAD)
        backfill.request_fill(STAR_A)
        fetcher = site(actress_pages(STAR_A, codes('A', 5)))
        result = backfill.run(fetcher=fetcher)
        assert result.queued == 4
        assert detail_requests(fetcher).count('/A-003') == 1

    def test_a_list_page_that_cannot_be_read_stops_that_actress_and_says_why(self, site, today):
        pages = actress_pages(STAR_A, codes('A', 95))
        pages['/star/a1/2'] = FetchError('请求失败（HTTP 500）：https://www.javbus.com/star/a1/2')
        fetcher = site(pages)
        backfill.request_fill(STAR_A)
        result = backfill.run(fetcher=fetcher)
        found = source()
        assert found.error == '第 2 页读不到：请求失败（HTTP 500）：https://www.javbus.com/star/a1/2'
        assert (found.wanted, found.next_page) == (0, 2)
        # 第 1 页排进来的照常抓
        assert (result.queued, result.added) == (30, 30)

    @pytest.mark.parametrize('counts', [None, (0, 3)], ids=['no-numbers', 'zero-magnets'])
    def test_a_list_page_without_movies_that_does_not_say_she_has_some_is_the_end(self, site, today, counts):
        pages = actress_pages(STAR_A, codes('A', 95))
        pages['/star/a1/2'] = list_html([], counts=counts)
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(pages))
        assert (source().reached_end, source().error) == (True, '')

    def test_a_list_page_that_is_not_there_is_the_end(self, site, today):
        pages = actress_pages(STAR_A, codes('A', 95))
        pages['/star/a1/2'] = NotFoundError('页面不存在（HTTP 404）')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(pages))
        assert (source().reached_end, source().error) == (True, '')

    def test_a_list_that_never_ends_is_stopped(self, site, today, monkeypatch):
        monkeypatch.setattr(backfill, 'MAX_PAGES', 3)
        page = list_html(['A-001'], counts=(1, 1), has_next=True)
        fetcher = site({STAR_A: page, '/star/a1/2': page, '/star/a1/3': page, '/star/a1/4': page})
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=fetcher)
        assert list_requests(fetcher) == [STAR_A, '/star/a1/2', '/star/a1/3']
        assert source().error == '翻了 3 页还没有到最后一页，先停下（页面结构可能变了）'

    def test_a_second_request_is_refused_while_her_movies_are_queued(self, site, today, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '1')
        backfill.request_fill(STAR_A)
        assert backfill.request_fill(STAR_A) == '还在找她的作品'
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 10))))
        assert len(queued_of()) == 9
        assert backfill.request_fill(STAR_A) == '她还有 9 部在排队'
        assert source().wanted == 0

    def test_a_checked_actress_is_not_read_twice_when_she_is_also_filled(self, site, today):
        fetcher = site(actress_pages(STAR_A, codes('A', 10)))
        backfill.request_check(STAR_A)
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=fetcher)
        assert list_requests(fetcher) == [STAR_A]
        assert source().checking is False

    @pytest.mark.parametrize('path', ['/star/演员甲', '/genre/a1'])
    def test_only_actress_pages_can_be_filled(self, path):
        with pytest.raises(ValueError):
            backfill.request_fill(path)


NO_BOXES = '没有找到影片，页面上却写着有磁力的影片 {} 部（页面结构可能变了，或被拦截）'


class TestListPagesWithoutMovieBoxes:
    '''没有影片框的列表页：页面上的数字说她没有有磁力的影片时才是翻到底了；说她有、却一个框也找不到，是读不懂这一页'''

    def test_a_first_page_without_boxes_that_says_she_has_movies_is_an_error_not_the_end(self, site, today):
        fetcher = site({STAR_A: list_html([], counts=(95, 120), has_next=True)})
        backfill.request_fill(STAR_A)
        result = backfill.run(fetcher=fetcher)
        found = source()
        assert found.error == '第 1 页' + NO_BOXES.format(95)
        assert (found.wanted, found.next_page, found.reached_end, found.oldest) == (0, 0, False, None)
        assert (found.magnets, found.total) == (95, 120)
        assert fetcher.requests == [STAR_A]
        assert (result.queued, result.fetched) == (0, 0)

    def test_so_is_a_checked_one(self, site, today):
        backfill.request_check(STAR_A)
        backfill.run(fetcher=site({STAR_A: list_html([], counts=(95, 120))}))
        found = source()
        assert found.error == '第 1 页' + NO_BOXES.format(95)
        assert (found.magnets, found.total, found.checking, found.reached_end) == (95, 120, False, False)

    def test_a_later_page_that_says_she_has_movies_is_an_error_too(self, site, today):
        pages = actress_pages(STAR_A, codes('A', 95))
        pages['/star/a1/2'] = list_html([], counts=(95, 95), has_next=True)
        fetcher = site(pages)
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=fetcher)
        found = source()
        assert found.error == '第 2 页' + NO_BOXES.format(95)
        # 第 1 页排进来的照常抓，往回补停在第 2 页，没有记成翻到底
        assert (found.wanted, found.next_page, found.reached_end) == (0, 2, False)
        assert len(detail_requests(fetcher)) == 30

    def test_an_actress_without_magnet_movies_is_the_end_without_an_error(self, site, today):
        fetcher = site({STAR_A: list_html([], counts=(0, 5))})
        backfill.request_fill(STAR_A)
        result = backfill.run(fetcher=fetcher)
        found = source()
        assert (found.reached_end, found.error, found.magnets, found.total) == (True, '', 0, 5)
        assert (result.queued, fetcher.requests) == (0, [STAR_A])

    def test_the_error_goes_once_the_page_can_be_read(self, site, today):
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site({STAR_A: list_html([], counts=(95, 120))}))
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 95))))
        found = source()
        assert (found.error, found.error_at) == ('', None)
        assert db.Item.select().count() == 60


class TestFalseEnds:
    '''没有影片框的第 1 页曾经被记成"已经翻到最后一页"：以后再也往回补不了。这样的记录自己恢复'''

    def false_end(self, **values):
        fields = {'path': STAR_A, 'magnets': 95, 'total': 120, 'checked_at': DAY, 'next_page': 2, 'reached_end': True}
        return db.ListSource.create(**{**fields, **values})

    def test_a_mark_made_without_ever_seeing_a_movie_is_taken_back(self, site, today):
        self.false_end()
        backfill.run(fetcher=site({}))
        found = source()
        assert (found.reached_end, found.next_page) == (False, 0)

    def test_she_is_then_filled_from_the_start_again(self, site, today):
        self.false_end()
        backfill.run(fetcher=site({}))
        backfill.request_fill(STAR_A)
        fetcher = site(actress_pages(STAR_A, codes('A', 95)))
        result = backfill.run(fetcher=fetcher)
        assert list_requests(fetcher) == [STAR_A, '/star/a1/2']
        assert detail_requests(fetcher) == ['/' + fanhao for fanhao in codes('A', 95, 36)]
        assert (result.queued, source().next_page, source().reached_end) == (60, 3, False)

    def test_what_is_really_the_end_is_left_alone(self, site, today):
        # 没有磁力的演员（数字是 0）、记着经过的最早一部的、排过影片的（数字和库里、队里已知的一样多）、本来就没到底的
        self.false_end(path='/star/b2', magnets=0, total=5)
        counted = self.false_end(path='/star/c3', magnets=1, total=1, oldest=datetime.date(2020, 1, 1))
        db.FetchTask.create(path='/C-001', fanhao='C-001', origin=db.ORIGIN_LIST, source=counted, page=1)
        walked = self.false_end(path='/star/d4', magnets=1, total=1)
        db.FetchTask.create(path='/D-001', fanhao='D-001', origin=db.ORIGIN_LIST, source=walked, page=1)
        self.false_end(path='/star/e5', reached_end=False)
        backfill.run(fetcher=site({}))
        marks = {found.path: (found.reached_end, found.next_page) for found in db.ListSource.select()}
        assert marks == {'/star/b2': (True, 2), '/star/c3': (True, 2), '/star/d4': (True, 2), '/star/e5': (False, 2)}

    def test_the_log_tells_how_many(self, site, today, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        self.false_end()
        backfill.run(fetcher=site({}))
        assert any('1 位' in record.getMessage() and '最后一页' in record.getMessage() for record in caplog.records)

    def known_by_the_library(self, fanhao, path=STAR_A):
        '''库里有这部影片，带着她的演员页的标签'''
        meta = {'fanhao': fanhao, 'title': f'示例影片 {fanhao}', 'url': '/' + fanhao,
                'release_date': released(fanhao), 'cover_img_url': '', 'length': '120'}
        assert db.save(meta, [Tag('star', '演员甲', path)])

    def queued_before(self, row, fanhaos, status='done'):
        for fanhao in fanhaos:
            db.FetchTask.create(path=f'/{fanhao}', fanhao=fanhao, origin=db.ORIGIN_LIST, source=row, page=1,
                                status=status)

    def ended_behind_a_page_without_boxes(self):
        '''她有 33 部：第 1 页的 30 部里库里有 1 部、排了 29 部，第 2 页有数字没有框，被记成了"已经翻到最后一页"'''
        row = self.false_end(magnets=33, total=33, next_page=2, oldest=datetime.date(2020, 1, 4))
        self.known_by_the_library('A-033')
        self.queued_before(row, codes('A', 32, 4))
        return row

    def test_a_mark_made_behind_a_page_without_boxes_is_taken_back_too(self, site, today):
        self.ended_behind_a_page_without_boxes()
        backfill.run(fetcher=site({}))
        found = source()
        # 往回补的进度留着：下次先看第 1 页的新片，再从停下的那一页接着补
        assert (found.reached_end, found.next_page) == (False, 2)

    def test_she_is_then_continued_from_the_page_that_could_not_be_read(self, site, today):
        self.ended_behind_a_page_without_boxes()
        backfill.run(fetcher=site({}))
        backfill.request_fill(STAR_A)
        fetcher = site(actress_pages(STAR_A, codes('A', 33)))
        result = backfill.run(fetcher=fetcher)
        assert list_requests(fetcher) == [STAR_A, '/star/a1/2']
        assert detail_requests(fetcher) == ['/A-003', '/A-002', '/A-001']
        assert (result.queued, source().reached_end) == (3, True)

    def test_a_movie_in_the_library_and_in_the_queue_counts_once(self, site, today):
        row = self.false_end(magnets=2, total=2)
        self.known_by_the_library('A-001')
        self.queued_before(row, ['A-001'])
        backfill.run(fetcher=site({}))
        assert source().reached_end is False

    def test_as_many_known_as_the_page_says_is_the_end(self, site, today):
        row = self.false_end(magnets=2, total=2)
        self.known_by_the_library('A-002')
        self.queued_before(row, ['A-001'])
        backfill.run(fetcher=site({}))
        assert source().reached_end is True

    def test_only_the_movies_of_her_own_page_count(self, site, today):
        self.false_end(magnets=2, total=2, oldest=datetime.date(2020, 1, 4))
        self.known_by_the_library('B-001', path=STAR_B)
        self.known_by_the_library('B-002', path=STAR_B)
        backfill.run(fetcher=site({}))
        assert source().reached_end is False

    def test_the_numbers_are_only_checked_once(self, site, today):
        # 以后"查看"发现了新片的演员，页面上的数字也会比已知的多，她不能因此被改回没到底
        backfill.run(fetcher=site({}))
        assert db.JobState.get(db.JobState.name == backfill.ENDS)
        self.false_end(path=STAR_B, magnets=33, total=33, oldest=datetime.date(2020, 1, 4))
        backfill.run(fetcher=site({}))
        assert source(STAR_B).reached_end is True

    def test_a_walk_to_the_real_end_is_not_undone_by_later_runs(self, site, today):
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 5))))
        assert source().reached_end is True
        for _ in range(2):
            backfill.run(fetcher=site({}))
        assert source().reached_end is True
        backfill.request_fill(STAR_A)
        fetcher = site(actress_pages(STAR_A, codes('A', 5)))
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [STAR_A]

    def test_what_the_numbers_got_wrong_is_not_taken_back_again_after_she_is_walked(self, site, today):
        # 库里有她的一部影片没带她的演员页标签：数字看起来比已知的多一部。第一次整理时当错的收回，再补一次翻到底以后不再收回
        row = self.false_end(magnets=3, total=3)
        self.queued_before(row, ['A-003', 'A-002'])
        db.save({'fanhao': 'A-001', 'title': '示例影片 A-001', 'url': '/A-001', 'release_date': released('A-001'),
                 'cover_img_url': '', 'length': '120'}, [Tag('star', '另一个写法', '/star/other')])
        backfill.run(fetcher=site({}))
        assert source().reached_end is False
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 3))))
        assert source().reached_end is True
        backfill.run(fetcher=site({}))
        assert source().reached_end is True

    def test_the_log_tells_how_many_were_taken_back_by_the_numbers(self, site, today, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        self.ended_behind_a_page_without_boxes()
        backfill.run(fetcher=site({}))
        assert any('1 位' in record.getMessage() and '没有影片框' in record.getMessage() and '已知' in record.getMessage()
                   for record in caplog.records)
        assert '/star/' not in caplog.text


class TestCancel:
    def test_movies_still_queued_are_removed_and_she_can_be_continued_from_there(self, site, today, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '10')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 95))))
        assert len(queued_of()) == 50
        assert source().next_page == 3
        assert backfill.cancel(STAR_A) == 50
        assert queued_of() == []
        # 取消的最早是第 1 页上的：下次从第 1 页接着往回补
        assert (source().next_page, source().reached_end, source().wanted) == (1, False, 0)
        assert db.FetchTask.select().count() == 10

    def test_a_request_that_has_not_started_is_called_off(self, site, today):
        backfill.request_fill(STAR_A)
        assert backfill.cancel(STAR_A) == 0
        fetcher = site(actress_pages(STAR_A, codes('A', 5)))
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == []

    def test_an_actress_that_was_never_filled(self):
        assert backfill.cancel(STAR_A) == 0

    def test_the_row_no_longer_shows_how_far_back_the_cancelled_batch_went(self, site, today, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '10')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 95))))
        assert source().oldest is not None
        assert backfill.cancel(STAR_A) == 50
        assert source().oldest is None

    @pytest.fixture
    def second_batch_queued(self, site, today, monkeypatch):
        '''第一批（她最新的 60 部）抓完；第二批（剩下的 35 部）排上了队，今天的额度已经用完，一部也没抓。返回第一批的日期'''
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 95))))
        first = datetime.date.fromisoformat(released('A-036'))
        assert source().oldest == first
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '60')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 95))))
        assert (len(queued_of()), source().oldest) == (35, datetime.date.fromisoformat(released('A-001')))
        return first

    def test_cancelling_a_later_batch_keeps_how_far_back_the_earlier_ones_went(self, second_batch_queued):
        assert backfill.cancel(STAR_A) == 35
        assert source().oldest == second_batch_queued

    def test_going_on_after_cancelling_a_later_batch_records_how_far_back_she_got_again(
            self, site, second_batch_queued, monkeypatch):
        backfill.cancel(STAR_A)
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '300')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 95))))
        assert (source().oldest, source().reached_end) == (datetime.date.fromisoformat(released('A-001')), True)

    def test_a_batch_without_a_record_of_where_it_began_is_cleared(self, second_batch_queued):
        # 旧版本排的一批：库里没有它开始之前的日期
        db.JobState.delete().where(db.JobState.name.startswith('backfill-oldest')).execute()
        assert backfill.cancel(STAR_A) == 35
        assert source().oldest is None

    def test_each_actress_keeps_her_own_date(self, site, today, monkeypatch):
        pages = {**actress_pages(STAR_A, codes('A', 95)), **actress_pages(STAR_B, codes('B', 95))}
        backfill.request_fill(STAR_A)
        backfill.request_fill(STAR_B)
        backfill.run(fetcher=site(pages))
        first = {path: source(path).oldest for path in (STAR_A, STAR_B)}
        assert first[STAR_A] == datetime.date.fromisoformat(released('A-036'))
        assert first[STAR_B] == datetime.date.fromisoformat(released('B-036'))
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '120')
        backfill.request_fill(STAR_B)
        backfill.run(fetcher=site(pages))
        assert source(STAR_B).oldest == datetime.date.fromisoformat(released('B-001'))
        assert backfill.cancel(STAR_B) == 35
        assert (source(STAR_A).oldest, source(STAR_B).oldest) == (first[STAR_A], first[STAR_B])

    def test_a_cancel_that_removes_nothing_leaves_the_date_alone(self, site, today):
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 95))))
        oldest = source().oldest
        assert oldest is not None
        assert backfill.cancel(STAR_A) == 0
        assert source().oldest == oldest

    def test_going_on_after_a_cancel_records_how_far_back_she_got_again(self, site, today, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '10')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 95))))
        backfill.cancel(STAR_A)
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '300')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 95))))
        # 她的前 10 部已经抓到、认识；再排的是第 1 页的 20 部、第 2 页的 30 部和第 3 页的 10 部，经过的最早一部是 A-026
        assert (source().oldest, source().next_page) == (datetime.date.fromisoformat(released('A-026')), 3)

    def test_the_log_does_not_say_page_zero_when_nothing_was_read(self, site, today, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(pages_read_during(STAR_A, lambda: backfill.cancel(STAR_A), codes('A', 10))))
        assert not any('第 0 页' in message for message in caplog.messages)
        assert any('这一次排进 0 部，下次从第 1 页接着往回补' in message for message in caplog.messages)

    def test_the_log_does_not_say_page_zero_after_a_page_that_could_not_be_read_either(self, site, today, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site({STAR_A: NetworkError('网络连接失败（ConnectTimeout）')}))
        assert not any('第 0 页' in message for message in caplog.messages)
        assert any('这一次排进 0 部，下次从第 1 页接着往回补' in message for message in caplog.messages)


def pages_read_during(page_path, during, fanhaos=None):
    '''演员页的各页，其中 page_path 这一页被读的时候先执行 during()，再把页面交回来：网页上的取消正好落在这一次读页期间'''
    pages = actress_pages(STAR_A, fanhaos or codes('A', 95))
    html = pages[page_path]

    def read(_path):
        during()
        return html

    pages[page_path] = read
    return pages


class TestCancelWhileAListPageIsBeingRead:
    '''后台读她的列表页要几秒，取消排队可能正好落在这期间：那一页回来以后什么都不排、不改"往回补到哪里"'''

    def test_the_page_that_comes_back_queues_nothing_and_nothing_is_fetched(self, site, today):
        backfill.request_fill(STAR_A)
        cancelled = []
        fetcher = site(pages_read_during('/star/a1/2', lambda: cancelled.append(backfill.cancel(STAR_A))))
        result = backfill.run(fetcher=fetcher)
        assert cancelled == [30]
        assert list_requests(fetcher) == [STAR_A, '/star/a1/2']
        assert detail_requests(fetcher) == []
        assert db.FetchTask.select().count() == 0
        found = source()
        # 取消时退回到第 1 页，第 2 页回来以后没有再动它
        assert (found.wanted, found.next_page, found.reached_end) == (0, 1, False)
        assert (result.fetched, result.left) == (0, 0)

    def test_the_end_of_her_list_is_not_recorded_after_a_cancel(self, site, today):
        backfill.request_fill(STAR_A)
        fetcher = site(pages_read_during(STAR_A, lambda: backfill.cancel(STAR_A), codes('A', 10)))
        backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == []
        assert db.FetchTask.select().count() == 0
        found = source()
        assert (found.wanted, found.next_page, found.reached_end, found.oldest) == (0, 0, False, None)

    def test_asking_again_meanwhile_keeps_the_new_request_for_the_next_run(self, site, today):
        def cancel_and_ask_again():
            backfill.cancel(STAR_A)
            today.now += datetime.timedelta(seconds=5)
            assert backfill.request_fill(STAR_A) is None

        backfill.request_fill(STAR_A)
        fetcher = site(pages_read_during('/star/a1/2', cancel_and_ask_again))
        backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == []
        assert db.FetchTask.select().count() == 0
        assert source().wanted == 60
        # 新的这一次从头来过，不会少排她最新的那些
        again = site(actress_pages(STAR_A, codes('A', 95)))
        result = backfill.run(fetcher=again)
        assert detail_requests(again) == ['/' + fanhao for fanhao in codes('A', 95, 36)]
        assert (result.queued, source().wanted) == (60, 0)

    def test_a_cancel_before_her_page_is_read_reads_nothing(self, site, today):
        backfill.request_fill(STAR_A)
        backfill.cancel(STAR_A)
        fetcher = site(actress_pages(STAR_A, codes('A', 5)))
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == []


class TestCancelWhileTheNextRequestWaitsForItsTurn:
    '''
    两次请求之间要等间隔，取消可以落在这期间：等完以后、真正发出去之前再看一眼她有没有被取消，取消了就不发。
    FakeFetcher.waiting 是"等间隔的时候别的线程做了什么"
    '''

    def cancel_when(self, fetcher, path, who=STAR_A):
        fetcher.waiting = lambda asked: backfill.cancel(who) if asked == path else None
        return fetcher

    def test_the_next_list_page_is_not_asked_for(self, site, today):
        backfill.request_fill(STAR_A)
        fetcher = self.cancel_when(site(actress_pages(STAR_A, codes('A', 95))), '/star/a1/2')
        result = backfill.run(fetcher=fetcher)
        assert list_requests(fetcher) == [STAR_A]
        assert detail_requests(fetcher) == []
        found = source()
        assert (found.wanted, found.next_page, found.reached_end) == (0, 1, False)
        assert (result.pages, result.fetched, result.left) == (1, 0, 0)

    def test_her_first_page_is_not_asked_for(self, site, today):
        backfill.request_fill(STAR_A)
        fetcher = self.cancel_when(site(actress_pages(STAR_A, codes('A', 5))), STAR_A)
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == []
        assert (source().wanted, source().error) == (0, '')

    def test_the_next_detail_page_is_not_asked_for(self, site, today):
        backfill.request_fill(STAR_A)
        fetcher = self.cancel_when(site(actress_pages(STAR_A, codes('A', 5))), '/A-004')
        result = backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == ['/A-005']
        assert statuses() == {'A-005': 'done'}
        assert saved() == ['A-005']
        assert (result.fetched, result.added) == (1, 1)

    def test_what_is_still_wanted_by_the_others_goes_on(self, site, today):
        backfill.request_fill(STAR_A)
        backfill.request_fill(STAR_B)
        fetcher = self.cancel_when(site({**actress_pages(STAR_A, codes('A', 4)), **actress_pages(STAR_B, codes('B', 3))}),
                                   '/A-003')
        backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == ['/A-004', '/B-003', '/B-002', '/B-001']
        assert statuses() == {'A-004': 'done', 'B-003': 'done', 'B-002': 'done', 'B-001': 'done'}

    def test_an_uploaded_code_has_nothing_to_cancel(self, site, today):
        upload('ABC-001')
        fetcher = site({})
        fetcher.waiting = lambda asked: backfill.cancel(STAR_A)
        backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == ['/ABC-001']
        assert statuses() == {'ABC-001': 'done'}

    def test_a_movie_whose_request_was_not_sent_is_not_counted_as_fetched(self, site, today):
        backfill.request_fill(STAR_A)
        fetcher = self.cancel_when(site(actress_pages(STAR_A, codes('A', 5))), '/A-005')
        result = backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == []
        assert (result.fetched, result.added, result.failed, result.retry_later) == (0, 0, 0, 0)

    def test_the_day_is_charged_for_the_requests_that_were_sent_and_for_no_others(self, site, today):
        backfill.request_fill(STAR_A)
        fetcher = self.cancel_when(site(actress_pages(STAR_A, codes('A', 5))), '/A-004')
        backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == ['/A-005']
        assert (backfill.daily_used(), backfill.daily_left()) == (1, 299)
        assert db.JobState.get(db.JobState.name == backfill.SPENT).text == '1'

    def test_a_cancel_before_the_first_detail_request_charges_nothing(self, site, today):
        backfill.request_fill(STAR_A)
        fetcher = self.cancel_when(site(actress_pages(STAR_A, codes('A', 5))), '/A-005')
        backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == []
        assert (backfill.daily_used(), backfill.daily_left()) == (0, 300)

    def test_a_request_that_was_already_on_its_way_stays_charged(self, site, today, detail_page):
        backfill.request_fill(STAR_A)
        pages = actress_pages(STAR_A, codes('A', 5))
        # 请求发出去了，网站答复的时候用户点了取消：这一部照常入库，也已经算了
        pages['/A-005'] = lambda path: (backfill.cancel(STAR_A), detail_page('A-005'))[1]
        fetcher = site(pages)
        backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == ['/A-005']
        assert (saved(), backfill.daily_used()) == (['A-005'], 1)

    def test_what_is_still_queued_is_charged_when_it_is_sent_later(self, site, today):
        backfill.request_fill(STAR_A)
        fetcher = self.cancel_when(site(actress_pages(STAR_A, codes('A', 5))), '/A-004')
        backfill.run(fetcher=fetcher)
        backfill.request_fill(STAR_A)
        later = site(actress_pages(STAR_A, codes('A', 5)))
        backfill.run(fetcher=later)
        assert detail_requests(later) == ['/A-004', '/A-003', '/A-002', '/A-001']
        assert (backfill.daily_used(), backfill.daily_left()) == (5, 295)


def web_write():
    '''网页线程上的一次写库（点"喜欢"、改设定……）：另一条数据库连接上的一个事务，提交了才返回'''
    def write():
        try:
            with db.db.atomic('IMMEDIATE'):
                db.JobState.create(name=f'web-{time.monotonic_ns()}', text='网页上的一次写')
        finally:
            db.db.close()

    thread = threading.Thread(target=write)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()


def after_a_web_write(page):
    '''读这一页的时候网页上正好有一次写库，再把页面（或者拦截）交回来：几秒的读页期间里常有这样的事'''
    def read(_path):
        web_write()
        return page

    return read


class TestAWriteFromTheWebWhileAPageIsBeingRead:
    '''
    后台读一页要几秒，网页上的写库（点"喜欢"、取消排队、改设定……）可以落在这期间：这一轮不能因此被数据库拒绝，
    后面还在等的演员、查看、查找照常做，被拦截也一定记得下
    '''

    def test_the_actress_waiting_behind_is_still_walked(self, site, today):
        pages = {**actress_pages(STAR_A, codes('A', 40)), **actress_pages(STAR_B, codes('B', 40))}
        pages[STAR_A] = after_a_web_write(pages[STAR_A])
        backfill.request_fill(STAR_A)
        backfill.request_fill(STAR_B)
        result = backfill.run(fetcher=site(pages))
        assert (result.queued, result.stopped) == (80, '')
        assert (source(STAR_A).wanted, source(STAR_B).wanted) == (0, 0)
        assert len(statuses()) == 80

    def test_the_view_waiting_behind_is_still_made(self, site, today):
        pages = {**actress_pages(STAR_A, codes('A', 3)), **actress_pages(STAR_B, codes('B', 4))}
        pages[STAR_A] = after_a_web_write(pages[STAR_A])
        backfill.request_check(STAR_A)
        backfill.request_check(STAR_B)
        backfill.run(fetcher=site(pages))
        assert (source(STAR_A).magnets, source(STAR_B).magnets) == (3, 4)
        assert (source(STAR_A).checking, source(STAR_B).checking) == (False, False)

    def test_the_search_waiting_behind_is_still_made(self, site, today):
        pages = {SEARCH_JIA: after_a_web_write(search_html(('/star/a1', '演员甲'))),
                 SEARCH_YI: search_html(('/star/b2', '演员乙'))}
        backfill.request_search('name:演员甲', '演员甲')
        backfill.request_search('name:演员乙', '演员乙')
        backfill.run(fetcher=site(pages))
        found = {link.key: (link.state, link.path) for link in db.StarLink.select()}
        assert found == {'name:演员甲': ('found', '/star/a1'), 'name:演员乙': ('found', '/star/b2')}

    def test_a_block_is_recorded_and_the_actress_waiting_behind_is_dropped(self, site, today):
        pages = actress_pages(STAR_B, codes('B', 40))
        pages[STAR_A] = after_a_web_write(BlockedError(BLOCKED))
        backfill.request_fill(STAR_A)
        backfill.request_fill(STAR_B)
        fetcher = site(pages)
        result = backfill.run(fetcher=fetcher)
        assert fetcher.requests == [STAR_A]
        assert (result.stopped, result.blocked) == ('被网站拦截', BLOCKED)
        assert backfill.paused() == (BLOCKED, DAY)
        assert (source(STAR_B).wanted, source(STAR_B).error) == (0, WAITING_DROPPED)


@pytest.fixture
def held_during(locked_database):
    '''
    held_during(回复)：把一个页面（或者异常）换成"读到它的时候，别的程序正好占住了数据库的写锁"：占住以后才把页面交回来
    （是异常就抛出来）。一直占到 held_during.release()，测试结束时也会放开
    '''
    held = contextlib.ExitStack()

    def around(reply):
        def read(_path):
            held.enter_context(locked_database())
            if isinstance(reply, BaseException):
                raise reply
            return reply

        return read

    around.release = held.close
    yield around
    held.close()


def freed_when_refused(monkeypatch, name, held_during):
    '''
    backfill.<name> 因为数据库被占着而失败的那一刻，别的程序放开了锁：失败以后收尾时再写一次的话，这一次写得进去
    （等锁的 30 秒里它放开了，就是这样）
    '''
    real = getattr(backfill, name)

    def refused(*args, **kwargs):
        try:
            return real(*args, **kwargs)
        except OperationalError:
            held_during.release()
            raise

    monkeypatch.setattr(backfill, name, refused)


class TestABlockTheDatabaseWasTooBusyToRecord:
    '''被拦截的那一刻数据库正被别的程序占着、暂停记不下来：记不下来的时候不能再对网站发请求'''

    @pytest.fixture
    def not_recorded(self, site, today, held_during):
        '''她的演员页被拦截，这时数据库被占着：这一次没有做成，暂停没有记进库里'''
        backfill.request_fill(STAR_A)
        with pytest.raises(OperationalError):
            backfill.run(fetcher=site({STAR_A: held_during(BlockedError(BLOCKED))}))
        held_during.release()
        assert backfill.paused() is None

    def test_the_next_run_records_it_before_asking_the_site_for_anything(self, site, today, not_recorded):
        fetcher = site(actress_pages(STAR_A, codes('A', 5)))
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == []
        assert backfill.paused() == (BLOCKED, DAY)
        assert (source().wanted, source().error) == (0, WAITING_DROPPED)

    def test_it_is_recorded_with_the_time_of_the_block(self, site, today, not_recorded):
        today.now = DAY + datetime.timedelta(hours=3)
        backfill.run(fetcher=site({}))
        assert backfill.paused() == (BLOCKED, DAY)

    def test_the_user_asking_for_more_lifts_it(self, site, today, not_recorded):
        assert backfill.request_fill(STAR_B) is None
        fetcher = site({**actress_pages(STAR_A, codes('A', 5)), **actress_pages(STAR_B, codes('B', 5))})
        backfill.run(fetcher=fetcher)
        assert backfill.paused() is None
        assert [path for path in fetcher.requests if path.startswith('/star/')] == [STAR_A, STAR_B]

    def test_the_next_day_is_not_held_back_by_it(self, site, today, not_recorded):
        today.now = DAY + datetime.timedelta(days=1)
        fetcher = site(actress_pages(STAR_A, codes('A', 5)))
        backfill.run(fetcher=fetcher)
        assert backfill.paused() is None
        assert STAR_A in fetcher.requests

    def test_an_old_database_held_exclusively_is_as_busy_as_one_whose_write_lock_is_taken(
            self, site, today, monkeypatch):
        real = backfill.pause

        def held(*args, **kwargs):
            raise db.LockedOut('database is locked (another program holds it exclusively)')

        backfill.request_fill(STAR_A)
        monkeypatch.setattr(backfill, 'pause', held)
        backfill.record_block(BLOCKED)
        assert backfill._unrecorded == (BLOCKED, DAY)
        # 数据库还被独占着：下一次一开始就写不进去，一个请求也不发
        fetcher = site(actress_pages(STAR_A, codes('A', 5)))
        with pytest.raises(db.LockedOut):
            backfill.run(fetcher=fetcher)
        assert fetcher.requests == []
        # 放开以后先记下，再看要不要做事
        monkeypatch.setattr(backfill, 'pause', real)
        backfill.run(fetcher=fetcher)
        assert backfill.paused() == (BLOCKED, DAY) and fetcher.requests == []


class TestABusyDatabaseDoesNotVoidWhatWasClicked:
    '''
    后台读演员页期间别的程序占住了数据库、这一轮因为"库忙"中断：用户点的补全、查看留着，下一轮接着做，不能悄悄作废。
    只有走完、被取消、被拦截，或者出了错（那一行写明原因）才清掉。重做不会多排、不会多取详情页
    '''

    def test_a_fill_that_could_not_be_recorded_is_still_wanted(self, site, today, held_during, monkeypatch):
        pages = actress_pages(STAR_A, codes('A', 95))
        pages[STAR_A] = held_during(pages[STAR_A])
        # 第 1 页回来以后第一笔写是记下页面上的两个数
        freed_when_refused(monkeypatch, '_first_page', held_during)
        backfill.request_fill(STAR_A)
        with pytest.raises(OperationalError):
            backfill.run(fetcher=site(pages))
        found = source()
        assert (found.wanted, found.requested_at, found.error) == (60, DAY, '')
        assert db.FetchTask.select().count() == 0

    def test_the_next_run_does_it_with_one_more_read_of_the_first_page_and_no_more_movies(
            self, site, today, held_during, monkeypatch):
        pages = actress_pages(STAR_A, codes('A', 95))
        first = {**pages, STAR_A: held_during(pages[STAR_A])}
        freed_when_refused(monkeypatch, '_first_page', held_during)
        backfill.request_fill(STAR_A)
        with pytest.raises(OperationalError):
            backfill.run(fetcher=site(first))
        fetcher = site(pages)
        result = backfill.run(fetcher=fetcher)
        assert list_requests(fetcher) == [STAR_A, '/star/a1/2']
        assert detail_requests(fetcher) == ['/' + fanhao for fanhao in codes('A', 95, 36)]
        assert (result.queued, result.fetched, result.added, result.left) == (60, 60, 60, 0)
        assert (source().wanted, db.FetchTask.select().count(), db.Item.select().count()) == (0, 60, 60)

    def test_what_was_queued_before_the_interruption_is_not_queued_again(self, site, today, held_during, monkeypatch):
        pages = actress_pages(STAR_A, codes('A', 95))
        first = {**pages, '/star/a1/2': held_during(pages['/star/a1/2'])}
        freed_when_refused(monkeypatch, '_take', held_during)
        backfill.request_fill(STAR_A)
        with pytest.raises(OperationalError):
            backfill.run(fetcher=site(first))
        # 第 1 页的 30 部已经排进去了，还要排 30 部
        assert (len(queued_of()), source().wanted, source().next_page) == (30, 30, 2)
        fetcher = site(pages)
        result = backfill.run(fetcher=fetcher)
        # 先看第 1 页有没有新片（没有），再从停下的第 2 页接着
        assert list_requests(fetcher) == [STAR_A, '/star/a1/2']
        assert detail_requests(fetcher) == ['/' + fanhao for fanhao in codes('A', 95, 36)]
        assert result.queued == 30
        assert (source().wanted, db.FetchTask.select().count(), source().next_page) == (0, 60, 3)

    def test_a_view_that_could_not_be_recorded_is_still_wanted(self, site, today, held_during, monkeypatch):
        pages = actress_pages(STAR_A, codes('A', 95), total=120)
        pages[STAR_A] = held_during(pages[STAR_A])
        freed_when_refused(monkeypatch, '_first_page', held_during)
        backfill.request_check(STAR_A)
        with pytest.raises(OperationalError):
            backfill.run(fetcher=site(pages))
        found = source()
        assert (found.checking, found.magnets, found.checked_at, found.error) == (True, None, None, '')
        fetcher = site(actress_pages(STAR_A, codes('A', 95), total=120))
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [STAR_A]
        found = source()
        assert (found.checking, found.magnets, found.total, found.checked_at) == (False, 95, 120, DAY)

    def test_nothing_is_asked_of_the_site_while_the_database_is_held(self, site, today, locked_database):
        # 第一次开始时整理一次旧记录的那一笔写已经做过了：不然这一轮在走到演员页之前就因为数据库被占着停下了
        db.JobState.create(name=backfill.ENDS, text='', at=DAY)
        backfill.request_fill(STAR_A)
        backfill.request_check(STAR_B)
        fetcher = site({**actress_pages(STAR_A, codes('A', 95)), **actress_pages(STAR_B, codes('B', 5))})
        with locked_database():
            with pytest.raises(OperationalError):
                backfill.run(fetcher=fetcher)
        assert fetcher.requests == []
        assert (source(STAR_A).wanted, source(STAR_B).checking) == (60, True)
        # 放开以后一起做完
        result = backfill.run(fetcher=fetcher)
        assert (source(STAR_A).wanted, source(STAR_B).checking) == (0, False)
        assert (result.queued, source(STAR_B).magnets) == (60, 5)

    def test_asking_again_meanwhile_is_still_one_batch(self, site, today, held_during, monkeypatch):
        pages = actress_pages(STAR_A, codes('A', 95))
        pages[STAR_A] = held_during(pages[STAR_A])
        freed_when_refused(monkeypatch, '_first_page', held_during)
        backfill.request_fill(STAR_A)
        with pytest.raises(OperationalError):
            backfill.run(fetcher=site(pages))
        assert backfill.request_fill(STAR_A) == '还在找她的作品'
        assert source().wanted == 60

    def test_a_cancel_ends_what_the_interruption_left(self, site, today, held_during, monkeypatch):
        pages = actress_pages(STAR_A, codes('A', 95))
        pages[STAR_A] = held_during(pages[STAR_A])
        freed_when_refused(monkeypatch, '_first_page', held_during)
        backfill.request_fill(STAR_A)
        with pytest.raises(OperationalError):
            backfill.run(fetcher=site(pages))
        assert source().wanted == 60
        backfill.cancel(STAR_A)
        fetcher = site(actress_pages(STAR_A, codes('A', 95)))
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == []
        assert (source().wanted, source().requested_at) == (0, None)

    def test_a_block_ends_what_the_interruption_left(self, site, today, held_during, monkeypatch):
        pages = actress_pages(STAR_A, codes('A', 95))
        pages[STAR_A] = held_during(pages[STAR_A])
        freed_when_refused(monkeypatch, '_first_page', held_during)
        backfill.request_fill(STAR_A)
        with pytest.raises(OperationalError):
            backfill.run(fetcher=site(pages))
        fetcher = site({STAR_A: BlockedError(BLOCKED)})
        result = backfill.run(fetcher=fetcher)
        assert (fetcher.requests, result.stopped) == ([STAR_A], '被网站拦截')
        assert (source().wanted, backfill.paused()) == (0, (BLOCKED, DAY))
        later = site(actress_pages(STAR_A, codes('A', 95)))
        backfill.run(fetcher=later)
        assert later.requests == []

    def test_a_page_that_cannot_be_read_is_written_down_and_not_tried_again_by_the_next_run(self, site, today):
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site({STAR_A: FetchError('请求失败（HTTP 500）：https://www.javbus.com/star/a1')}))
        found = source()
        assert (found.wanted, found.error) == (0, '第 1 页读不到：请求失败（HTTP 500）：https://www.javbus.com/star/a1')
        fetcher = site(actress_pages(STAR_A, codes('A', 5)))
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == []


class TestRetrySource:
    def test_her_failed_movies_are_queued_again(self, site, today, detail_page):
        pages = actress_pages(STAR_A, codes('A', 3))
        pages['/A-002'] = detail_page('A-002', release='')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(pages))
        assert statuses() == {'A-003': 'done', 'A-002': 'failed', 'A-001': 'done'}
        assert backfill.retry_source(STAR_A) == 1
        assert queued_of() == ['A-002']


class TestDailyLimit:
    '''按演员补全抓的详情页，所有演员合计每天最多 backfill_daily 部（默认 300）。列表页、上传的番号不算'''

    def test_the_movies_beyond_the_limit_wait_for_the_next_day(self, site, today, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '5')
        fetcher = site({**actress_pages(STAR_A, codes('A', 4)), **actress_pages(STAR_B, codes('B', 4))})
        backfill.request_fill(STAR_A)
        backfill.request_fill(STAR_B)
        result = backfill.run(fetcher=fetcher)
        assert (result.queued, result.fetched, result.left, result.left_list, result.held) == (8, 5, 3, 3, 'limit')
        assert detail_requests(fetcher) == ['/A-004', '/A-003', '/A-002', '/A-001', '/B-004']
        assert backfill.daily_used() == 5
        # 同一天再跑一次：一部也不抓
        again = site({})
        assert backfill.run(fetcher=again).fetched == 0
        assert again.requests == []
        today.now = DAY + datetime.timedelta(days=1)
        later = site({})
        result = backfill.run(fetcher=later)
        assert detail_requests(later) == ['/B-003', '/B-002', '/B-001']
        assert (result.fetched, result.left, result.held) == (3, 0, '')
        assert backfill.daily_used() == 3

    def test_the_limit_is_counted_from_midnight(self, site, today, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '2')
        today.now = datetime.datetime(2026, 11, 6, 23, 59, 0)
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 4))))
        assert backfill.daily_used() == 2
        today.now = datetime.datetime(2026, 11, 7, 0, 0, 30)
        assert backfill.daily_used() == 0

    def test_uploaded_codes_are_not_limited(self, site, today, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '1')
        backfill.request_fill(STAR_A)
        backfill.enqueue_codes(['U-001', 'U-002'], db.ORIGIN_UPLOAD)
        fetcher = site(actress_pages(STAR_A, codes('A', 3)))
        result = backfill.run(fetcher=fetcher)
        # 上传的番号先抓，不受每天的上限
        assert detail_requests(fetcher) == ['/U-001', '/U-002', '/A-003']
        assert (result.left_list, result.held) == (2, 'limit')

    def test_a_movie_that_already_got_into_the_library_does_not_count(self, site, library, today, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '2')
        backfill.request_fill(STAR_A)
        fetcher = site(actress_pages(STAR_A, codes('A', 4)))
        db.ListSource.update(wanted=0).execute()
        source_ = source()
        for fanhao in codes('A', 4):
            db.FetchTask.create(path='/' + fanhao, fanhao=fanhao, origin=db.ORIGIN_LIST, source=source_, page=1)
        library('A-004', 'A-003')
        backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == ['/A-002', '/A-001']

    def test_the_status_tells_how_much_is_left_today(self, site, today, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '300')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 4))))
        assert (backfill.daily_used(), backfill.daily_left()) == (4, 296)


class TestTheCountOfTheDayIsKept:
    '''今天抓过多少部另记一笔：取消排队、改成上传番号会删掉或改掉队列里的行，不能让今天的数变小'''

    def two_tried_and_the_rest_queued(self, site, today, monkeypatch, limit='2'):
        '''额度 limit：A-004 抓到了，A-003 网站出错留在队里（今天试过了），额度用完，A-002、A-001 排着'''
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', limit)
        pages = {**actress_pages(STAR_A, codes('A', 4)), '/A-003': FetchError('请求失败（HTTP 500）：https://x')}
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(pages))

    def test_cancelling_does_not_give_back_a_movie_that_was_tried_today(self, site, today, monkeypatch):
        self.two_tried_and_the_rest_queued(site, today, monkeypatch)
        assert (backfill.daily_used(), queued_of()) == (2, ['A-003', 'A-002', 'A-001'])
        assert backfill.cancel(STAR_A) == 3
        assert (backfill.daily_used(), backfill.daily_left()) == (2, 0)
        backfill.request_fill(STAR_A)
        again = site(actress_pages(STAR_A, codes('A', 4)))
        backfill.run(fetcher=again)
        assert detail_requests(again) == []

    def test_uploading_a_movie_fetched_today_again_does_not_give_it_back_either(self, site, today, monkeypatch):
        self.two_tried_and_the_rest_queued(site, today, monkeypatch)
        # 今天抓到的影片后来从库里删掉了，又当成上传的番号排队：这一行的来处改成了上传
        assert backfill.enqueue_codes(['A-004'], db.ORIGIN_UPLOAD) == 1
        assert backfill.daily_used() == 2

    def test_a_movie_tried_again_the_same_day_is_counted_once(self, site, today, monkeypatch):
        self.two_tried_and_the_rest_queued(site, today, monkeypatch, limit='5')
        assert backfill.daily_used() == 4
        backfill.run(fetcher=site({}))
        assert statuses()['A-003'] == 'done'
        assert backfill.daily_used() == 4

    def test_the_next_day_starts_from_nothing(self, site, today, monkeypatch):
        self.two_tried_and_the_rest_queued(site, today, monkeypatch)
        today.now = DAY + datetime.timedelta(days=1)
        assert (backfill.daily_used(), backfill.daily_left()) == (0, 2)

    def test_what_was_fetched_before_the_count_was_kept_is_not_lost(self, site, today):
        source_ = db.ListSource.create(path=STAR_A)
        for fanhao in ('A-009', 'A-008'):
            db.FetchTask.create(path='/' + fanhao, fanhao=fanhao, origin=db.ORIGIN_LIST, source=source_, page=1,
                                status=db.TASK_DONE, tried_at=DAY)
        db.FetchTask.create(path='/A-007', fanhao='A-007', origin=db.ORIGIN_LIST, source=source_, page=1)
        assert backfill.daily_used() == 2
        backfill.run(fetcher=site({}))
        assert backfill.daily_used() == 3
        db.FetchTask.delete().where(db.FetchTask.fanhao == 'A-009').execute()
        assert backfill.daily_used() == 3

    def test_uploads_are_still_not_counted(self, site, today):
        backfill.enqueue_codes(['U-001'], db.ORIGIN_UPLOAD)
        backfill.run(fetcher=site({}))
        assert backfill.daily_used() == 0


class TestPause:
    '''被拦截（403 / 429 / 验证页）就停下：当天不再自动抓按演员补全的，用户再点才继续；第二天自动接着'''

    def test_blocked_on_a_movie_stops_at_once_and_pauses_the_rest_of_the_day(self, site, today):
        pages = actress_pages(STAR_A, codes('A', 5))
        pages['/A-004'] = BlockedError(BLOCKED)
        fetcher = site(pages)
        backfill.request_fill(STAR_A)
        result = backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == ['/A-005', '/A-004']
        assert (result.stopped, result.blocked, result.left_list) == ('被网站拦截', BLOCKED, 4)
        assert backfill.paused() == (BLOCKED, DAY)
        # 同一天：上传的照常抓，按演员补全的不抓
        backfill.enqueue_codes(['U-001'], db.ORIGIN_UPLOAD)
        later = site({})
        result = backfill.run(fetcher=later)
        assert detail_requests(later) == ['/U-001']
        assert (result.left_list, result.held) == (4, 'paused')
        # 第二天自动接着
        today.now = DAY + datetime.timedelta(days=1)
        assert backfill.paused() is None
        next_day = site({})
        backfill.run(fetcher=next_day)
        assert detail_requests(next_day) == ['/A-004', '/A-003', '/A-002', '/A-001']

    def test_blocked_on_a_list_page_stops_at_once_and_pauses(self, site, today):
        pages = actress_pages(STAR_A, codes('A', 40))
        pages['/star/a1/2'] = BlockedError(BLOCKED)
        backfill.request_fill(STAR_A)
        fetcher = site(pages)
        result = backfill.run(fetcher=fetcher)
        assert detail_requests(fetcher) == []
        assert (result.queued, result.stopped) == (30, '被网站拦截')
        assert source().error == f'第 2 页被网站拦截：{BLOCKED}'
        assert source().wanted == 0
        assert backfill.paused() == (BLOCKED, DAY)

    def test_blocked_on_an_uploaded_code_pauses_the_backfill_too(self, site, today):
        backfill.enqueue_codes(['U-001'], db.ORIGIN_UPLOAD)
        backfill.request_fill(STAR_A)
        fetcher = site({**actress_pages(STAR_A, codes('A', 2)), '/U-001': BlockedError(BLOCKED)})
        backfill.run(fetcher=fetcher)
        assert backfill.paused() == (BLOCKED, DAY)
        assert detail_requests(fetcher) == ['/U-001']

    def test_while_paused_no_list_page_is_read_either(self, site, today):
        backfill.pause(BLOCKED)
        db.ListSource.create(path=STAR_A, wanted=60)
        fetcher = site(actress_pages(STAR_A, codes('A', 3)))
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == []

    def test_a_check_is_still_made_while_paused(self, site, today):
        backfill.pause(BLOCKED)
        backfill.request_check(STAR_A)
        fetcher = site(actress_pages(STAR_A, codes('A', 3)))
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [STAR_A]
        assert backfill.paused() == (BLOCKED, DAY)

    def test_asking_for_more_lifts_the_pause(self, site, today):
        backfill.pause(BLOCKED)
        backfill.request_fill(STAR_A)
        assert backfill.paused() is None

    def test_resume_lifts_the_pause(self, today):
        backfill.pause(BLOCKED)
        backfill.resume()
        assert backfill.paused() is None

    def test_the_reason_keeps_no_name(self, today):
        backfill.pause('HTTP 403：https://www.javbus.com/searchstar/%E6%BC%94%E5%91%98')
        assert backfill.paused()[0] == 'HTTP 403：https://www.javbus.com/searchstar/…'


WAITING_DROPPED = '被网站拦截，这次没有翻她的演员页，要继续请再点一次补全'
VIEW_DROPPED = '被网站拦截，这次没有读她的演员页，要继续请再点一次查看'
SEARCH_DROPPED = '被网站拦截，这次没有查找她的 JavBus 演员页，要继续请再点一次查找'
STAR_C = '/star/c3'


class TestBlockedWhileOthersAreWaiting:
    '''被网站拦截时，点过"补全"、还没翻演员页的演员也都不翻了，第二天也不自动翻；排好队的详情页照旧第二天接着抓'''

    def test_the_actresses_still_waiting_are_not_walked_the_next_day_either(self, site, today):
        pages = {**actress_pages(STAR_A, codes('A', 40)), **actress_pages(STAR_B, codes('B', 40))}
        pages[STAR_A] = BlockedError(BLOCKED)
        backfill.request_fill(STAR_A)
        backfill.request_fill(STAR_B)
        fetcher = site(pages)
        backfill.run(fetcher=fetcher)
        assert list_requests(fetcher) == [STAR_A]
        waiting = source(STAR_B)
        assert (waiting.wanted, waiting.error, waiting.error_at) == (0, WAITING_DROPPED, DAY)
        today.now = DAY + datetime.timedelta(days=1)
        next_day = site(pages)
        backfill.run(fetcher=next_day)
        assert next_day.requests == []
        assert source(STAR_B).wanted == 0

    def test_pausing_drops_every_request_that_has_not_been_walked(self, today):
        backfill.request_fill(STAR_A)
        backfill.request_fill(STAR_B)
        backfill.pause(BLOCKED)
        assert [(found.wanted, found.error) for found in db.ListSource.select().order_by(db.ListSource.id)] == [
            (0, WAITING_DROPPED), (0, WAITING_DROPPED)]

    def test_the_reason_the_other_actress_was_stopped_for_is_not_overwritten(self, site, today):
        pages = actress_pages(STAR_A, codes('A', 40))
        pages[STAR_A] = BlockedError(BLOCKED)
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(pages))
        assert source().error == f'第 1 页被网站拦截：{BLOCKED}'

    def test_what_is_already_queued_waits_for_tomorrow_while_the_ones_waiting_are_dropped(self, site, today,
                                                                                         monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '1')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 4))))
        assert queued_of() == ['A-003', 'A-002', 'A-001']
        backfill.request_fill(STAR_B)
        backfill.pause(BLOCKED)
        assert (queued_of(), source(STAR_B).wanted) == (['A-003', 'A-002', 'A-001'], 0)
        today.now = DAY + datetime.timedelta(days=1)
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '300')
        next_day = site(actress_pages(STAR_B, codes('B', 4)))
        backfill.run(fetcher=next_day)
        assert detail_requests(next_day) == ['/A-003', '/A-002', '/A-001']
        assert list_requests(next_day) == []

    def test_a_request_left_waiting_by_an_earlier_version_is_dropped_the_next_time_it_runs(self, site, today):
        backfill.pause(BLOCKED)
        db.ListSource.create(path=STAR_B, wanted=60, requested_at=DAY)
        backfill.run(fetcher=site({}))
        assert (source(STAR_B).wanted, source(STAR_B).error) == (0, WAITING_DROPPED)
        today.now = DAY + datetime.timedelta(days=1)
        next_day = site(actress_pages(STAR_B, codes('B', 3)))
        backfill.run(fetcher=next_day)
        assert next_day.requests == []

    def test_the_log_says_how_many_were_dropped_and_names_nobody(self, today, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        backfill.request_fill(STAR_A)
        backfill.request_fill(STAR_B)
        backfill.pause(BLOCKED)
        dropped = [record.getMessage() for record in caplog.records if '再点' in record.getMessage()]
        assert len(dropped) == 1 and '2 位' in dropped[0]


class TestBlockedWhileViewsAndSearchesAreWaiting:
    '''
    被网站拦截时，点过"查看"、"查找"还没做的和点过"补全"还没翻的一样：不做了，第二天也不自己去做，那一行写明原因，
    要继续请再点一次。拦截以后用户才点的照旧做一次
    '''

    def test_the_views_still_waiting_are_not_made_later_either(self, site, today):
        pages = {STAR_A: BlockedError(BLOCKED), **actress_pages(STAR_B, codes('B', 3)),
                 **actress_pages(STAR_C, codes('C', 3))}
        for path in (STAR_A, STAR_B, STAR_C):
            backfill.request_check(path)
        fetcher = site(pages)
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [STAR_A]
        assert [(source(path).checking, source(path).error, source(path).error_at) for path in (STAR_B, STAR_C)] == [
            (False, VIEW_DROPPED, DAY), (False, VIEW_DROPPED, DAY)]
        for later in (DAY + datetime.timedelta(hours=5), DAY + datetime.timedelta(days=1)):
            today.now = later
            fetcher = site(pages)
            backfill.run(fetcher=fetcher)
            assert fetcher.requests == []
        # 再点一次就做
        backfill.request_check(STAR_B)
        again = site(pages)
        backfill.run(fetcher=again)
        assert again.requests == [STAR_B]
        assert source(STAR_B).magnets == 3

    def test_the_searches_still_waiting_are_not_made_later_either(self, site, today):
        pages = {SEARCH_JIA: BlockedError(BLOCKED), SEARCH_YI: search_html(('/star/b2', '演员乙'))}
        backfill.request_search('name:演员甲', '演员甲')
        backfill.request_search('name:演员乙', '演员乙')
        fetcher = site(pages)
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [SEARCH_JIA]
        found = {link.key: (link.state, link.error, link.checked_at) for link in db.StarLink.select()}
        assert found == {'name:演员甲': ('error', f'被网站拦截：{BLOCKED}', DAY),
                         'name:演员乙': ('error', SEARCH_DROPPED, DAY)}
        for later in (DAY + datetime.timedelta(hours=5), DAY + datetime.timedelta(days=1)):
            today.now = later
            fetcher = site(pages)
            backfill.run(fetcher=fetcher)
            assert fetcher.requests == []
        backfill.request_search('name:演员乙', '演员乙')
        again = site(pages)
        backfill.run(fetcher=again)
        assert again.requests == [SEARCH_YI]
        assert db.StarLink.get(db.StarLink.key == 'name:演员乙').state == 'found'

    def test_pausing_drops_every_kind_of_request_that_has_not_been_served(self, today):
        backfill.request_fill(STAR_A)
        backfill.request_check(STAR_B)
        backfill.request_search('name:演员甲', '演员甲')
        backfill.pause(BLOCKED)
        assert [(found.wanted, found.checking, found.error) for found in db.ListSource.select().order_by(
            db.ListSource.id)] == [(0, False, WAITING_DROPPED), (0, False, VIEW_DROPPED)]
        link = db.StarLink.get()
        assert (link.state, link.error, link.checked_at) == ('error', SEARCH_DROPPED, DAY)

    def test_one_who_asked_for_both_a_view_and_a_fill_is_dropped_once(self, today):
        backfill.request_check(STAR_A)
        backfill.request_fill(STAR_A)
        backfill.pause(BLOCKED)
        found = source()
        assert (found.wanted, found.checking, found.error) == (0, False, WAITING_DROPPED)

    def test_what_was_already_done_is_left_alone(self, today):
        db.ListSource.create(path=STAR_A, checked_at=DAY, magnets=3, next_page=2, reached_end=True)
        db.StarLink.create(key='name:演员甲', name='演员甲', state='found', path='/star/a1')
        backfill.pause(BLOCKED)
        assert (source().error, source().magnets, db.StarLink.get().state) == ('', 3, 'found')

    def test_a_view_asked_for_after_the_block_is_still_made(self, site, today):
        pages = {STAR_A: BlockedError(BLOCKED), **actress_pages(STAR_B, codes('B', 3))}
        backfill.request_check(STAR_A)
        backfill.request_check(STAR_B)
        backfill.run(fetcher=site(pages))
        assert backfill.paused() == (BLOCKED, DAY)
        # 拦截以后用户新点的不是以前留下的
        backfill.request_check(STAR_B)
        fetcher = site(pages)
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [STAR_B]
        assert (source(STAR_B).magnets, backfill.paused()) == (3, (BLOCKED, DAY))

    def test_a_search_asked_for_after_the_block_is_still_made(self, site, today):
        backfill.pause(BLOCKED)
        backfill.request_search('name:演员甲', '演员甲')
        fetcher = site({SEARCH_JIA: search_html(('/star/a1', '演员甲'))})
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [SEARCH_JIA]
        assert backfill.paused() == (BLOCKED, DAY)

    def test_views_and_searches_left_waiting_by_an_earlier_version_are_dropped_the_next_time_it_runs(self, site, today):
        # 旧版本被拦截时只丢掉等着翻的：库里只有暂停这一行，没有"丢过了"的记号
        db.JobState.create(name=backfill.PAUSE, text=BLOCKED, at=DAY)
        db.ListSource.create(path=STAR_B, checking=True)
        db.StarLink.create(key='name:演员甲', name='演员甲')
        fetcher = site({**actress_pages(STAR_B, codes('B', 3)), SEARCH_JIA: search_html(('/star/a1', '演员甲'))})
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == []
        assert (source(STAR_B).checking, source(STAR_B).error) == (False, VIEW_DROPPED)
        assert (db.StarLink.get().state, db.StarLink.get().error) == ('error', SEARCH_DROPPED)
        today.now = DAY + datetime.timedelta(days=1)
        next_day = site({**actress_pages(STAR_B, codes('B', 3)), SEARCH_JIA: search_html(('/star/a1', '演员甲'))})
        backfill.run(fetcher=next_day)
        assert next_day.requests == []
        # 整理过以后用户再点的照常做
        today.now = DAY + datetime.timedelta(hours=1)
        backfill.request_check(STAR_B)
        again = site(actress_pages(STAR_B, codes('B', 3)))
        backfill.run(fetcher=again)
        assert again.requests == [STAR_B]

    def test_an_earlier_pause_with_a_mark_from_another_day_is_still_tidied(self, site, today):
        db.JobState.create(name=backfill.TIDIED, text='', at=DAY - datetime.timedelta(days=3))
        db.JobState.create(name=backfill.PAUSE, text=BLOCKED, at=DAY)
        db.ListSource.create(path=STAR_B, checking=True)
        fetcher = site(actress_pages(STAR_B, codes('B', 3)))
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == []
        assert source(STAR_B).error == VIEW_DROPPED

    def test_the_log_says_how_many_views_and_searches_were_dropped_and_names_nobody(self, today, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        for path in (STAR_A, STAR_B):
            backfill.request_check(path)
        backfill.request_search('name:演员甲', '演员甲')
        backfill.pause(BLOCKED)
        dropped = [record.getMessage() for record in caplog.records if '再点' in record.getMessage()]
        assert len(dropped) == 1 and '2 位' in dropped[0] and '1 位' in dropped[0] and '查看' in dropped[0]
        assert '演员甲' not in caplog.text and '/star/' not in dropped[0]


class TestTheUsersOwnClickOnTheDayAnEarlierVersionLeftAPause:
    '''
    旧版本被拦截时只丢掉等着翻的，没有记"丢过了"的记号。升级当天它记的暂停还在：登记了、没做的查看和查找里，旧版本留下的
    要丢掉，升级以后用户自己点的不能丢——第一轮抓排队的影片开始之前点的也一样，不能和遗留一起被丢掉
    '''

    @pytest.fixture
    def left_behind(self, today):
        '''旧版本留下的：今天的暂停（没有"丢过了"的记号）、一位等着读第 1 页的、一次等着的查找'''
        db.JobState.create(name=backfill.PAUSE, text=BLOCKED, at=DAY)
        db.ListSource.create(path=STAR_B, checking=True)
        db.StarLink.create(key='name:演员乙', name='演员乙')

    def pages(self):
        return {**actress_pages(STAR_A, codes('A', 3)), **actress_pages(STAR_B, codes('B', 3)),
                SEARCH_JIA: search_html(('/star/a1', '演员甲')), SEARCH_YI: search_html(('/star/b2', '演员乙'))}

    def test_a_view_asked_for_before_the_first_run_is_made_and_what_was_left_behind_is_dropped(self, site, left_behind):
        backfill.request_check(STAR_A)
        fetcher = site(self.pages())
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [STAR_A]
        assert (source(STAR_A).magnets, source(STAR_A).checking) == (3, False)
        assert (source(STAR_B).checking, source(STAR_B).error) == (False, VIEW_DROPPED)
        assert (link('name:演员乙')[0], link('name:演员乙')[3]) == ('error', SEARCH_DROPPED)
        assert backfill.paused() == (BLOCKED, DAY)

    def test_a_search_asked_for_before_the_first_run_is_made_and_what_was_left_behind_is_dropped(self, site,
                                                                                                 left_behind):
        backfill.request_search('name:演员甲', '演员甲')
        fetcher = site(self.pages())
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [SEARCH_JIA]
        assert link('name:演员甲')[:2] == ('found', '/star/a1')
        assert (source(STAR_B).checking, source(STAR_B).error) == (False, VIEW_DROPPED)
        assert (link('name:演员乙')[0], link('name:演员乙')[3]) == ('error', SEARCH_DROPPED)

    def test_a_fill_asked_for_before_the_first_run_is_made_and_what_was_left_behind_is_dropped(self, site, left_behind):
        assert backfill.request_fill(STAR_A) is None
        fetcher = site(self.pages())
        backfill.run(fetcher=fetcher)
        # 她的补全照做（她的第 1 页、她的三部影片）；旧版本留下的查看和查找一个请求也不发
        assert list_requests(fetcher) == [STAR_A]
        assert detail_requests(fetcher) == ['/A-003', '/A-002', '/A-001']
        assert SEARCH_YI not in fetcher.requests and STAR_B not in fetcher.requests
        assert (source(STAR_B).checking, source(STAR_B).error) == (False, VIEW_DROPPED)
        assert (link('name:演员乙')[0], link('name:演员乙')[3]) == ('error', SEARCH_DROPPED)
        assert backfill.paused() is None

    def test_continuing_before_the_first_run_drops_what_was_left_behind(self, site, left_behind):
        backfill.resume()
        assert backfill.paused() is None
        fetcher = site(self.pages())
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == []
        assert (source(STAR_B).checking, source(STAR_B).error) == (False, VIEW_DROPPED)
        assert (link('name:演员乙')[0], link('name:演员乙')[3]) == ('error', SEARCH_DROPPED)

    def test_a_fill_asked_for_by_one_whose_old_view_was_still_waiting_is_made_and_the_old_view_is_dropped(
            self, site, left_behind):
        backfill.request_fill(STAR_B)
        fetcher = site(self.pages())
        backfill.run(fetcher=fetcher)
        assert list_requests(fetcher) == [STAR_B]
        assert SEARCH_YI not in fetcher.requests
        assert (source(STAR_B).wanted, source(STAR_B).checking, source(STAR_B).error) == (0, False, '')
        assert (link('name:演员乙')[0], link('name:演员乙')[3]) == ('error', SEARCH_DROPPED)

    def test_asking_again_for_one_whose_old_view_was_still_waiting_is_her_own_view(self, site, left_behind):
        backfill.request_check(STAR_B)
        fetcher = site(self.pages())
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [STAR_B]
        assert (source(STAR_B).checking, source(STAR_B).magnets) == (False, 3)

    def test_a_search_asked_for_again_is_her_own_search(self, site, left_behind):
        backfill.request_search('name:演员乙', '演员乙')
        fetcher = site(self.pages())
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [SEARCH_YI]
        assert link('name:演员乙')[:2] == ('found', '/star/b2')

    def test_a_click_after_another_click_does_not_drop_it(self, site, left_behind):
        backfill.request_check(STAR_A)
        backfill.request_search('name:演员甲', '演员甲')
        fetcher = site(self.pages())
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [SEARCH_JIA, STAR_A]
        assert (source(STAR_A).magnets, link('name:演员甲')[0]) == (3, 'found')

    def test_asking_leaves_the_others_alone_when_there_is_no_pause(self, today):
        backfill.request_check(STAR_B)
        backfill.request_search('name:演员乙', '演员乙')
        backfill.request_check(STAR_A)
        assert (source(STAR_B).checking, source(STAR_A).checking, link('name:演员乙')[0]) == (True, True, 'pending')
        assert db.JobState.get_or_none(db.JobState.name == backfill.TIDIED) is None

    def test_asking_after_a_block_that_dropped_what_was_waiting_leaves_the_earlier_asking_alone(self, site, today):
        backfill.pause(BLOCKED)
        backfill.request_check(STAR_B)
        backfill.request_check(STAR_A)
        fetcher = site(self.pages())
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [STAR_B, STAR_A]
        assert (source(STAR_A).magnets, source(STAR_B).magnets) == (3, 3)

    def test_a_pause_from_another_day_leaves_what_is_waiting_to_be_made(self, site, today):
        db.JobState.create(name=backfill.PAUSE, text=BLOCKED, at=DAY - datetime.timedelta(days=1))
        db.ListSource.create(path=STAR_B, checking=True)
        backfill.request_check(STAR_A)
        fetcher = site(self.pages())
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [STAR_B, STAR_A]


class TestRunOrder:
    def test_checks_then_walks_then_uploaded_codes_then_actress_movies(self, site, today):
        backfill.enqueue_codes(['U-001'], db.ORIGIN_UPLOAD)
        backfill.request_fill(STAR_A)
        backfill.request_check(STAR_B)
        fetcher = site({**actress_pages(STAR_A, codes('A', 2)), **actress_pages(STAR_B, codes('B', 2))})
        backfill.run(fetcher=fetcher)
        assert [path for path in fetcher.requests if not path.startswith('/search')] == [
            STAR_B, STAR_A, '/U-001', '/A-002', '/A-001']

    def test_searches_come_first(self, site, today):
        backfill.request_check(STAR_B)
        backfill.request_search('name:演员甲', '演员甲')
        fetcher = site({**actress_pages(STAR_B, codes('B', 2)), SEARCH_JIA: search_html(('/star/a1', '演员甲'))})
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [SEARCH_JIA, STAR_B]

    def test_the_log_names_actress_pages_by_number_only(self, site, today, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        backfill.request_fill(STAR_A)
        backfill.run(fetcher=site(actress_pages(STAR_A, codes('A', 2))))
        number = source().id
        assert f'按演员补全：演员页 #{number} 有磁力 2 部，全部 2 部' in caplog.messages
        assert f'按演员补全：演员页 #{number} 这一次排进 2 部，已经翻到最后一页' in caplog.messages
        assert '开始抓排队的影片：按演员补全的 2 部（今天还可以抓 300 部）' in caplog.messages
        assert '/star/a1' not in caplog.text


SEARCH_JIA = '/searchstar/%E6%BC%94%E5%91%98%E7%94%B2'
SEARCH_YI = searchstar_path('演员乙')


def search_html(*actresses, badge=''):
    '''
    按名字找演员的结果页：actresses 是 (演员页路径, 名字)。badge 不空时，每个名字后面、同一个 span 里
    跟着一个写着 badge 的按钮（网站上的角标）
    '''
    button = f'<button class="btn btn-xs">{badge}</button>' if badge else ''
    boxes = ''.join(f'<div class="item"><a class="avatar-box text-center" href="https://www.javbus.com{path}">'
                    f'<div class="photo-info"><span>{name}{button}</span></div></a></div>' for path, name in actresses)
    return f'<html><body><div id="waterfall">{boxes}</div></body></html>'


def link(key='name:演员甲'):
    found = db.StarLink.get(db.StarLink.key == key)
    return found.state, found.path, [tuple(item) for item in found.candidate_list], found.error


class TestSearch:
    '''「查找她的 JavBus 页面」：按名字找（1 次请求），名字相同的正好一位就自动采用，否则列出候选让用户选'''

    def test_a_page_found_for_an_actress_becomes_her_javbus_page(self, site, today, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう')
        cast.movie('L-001')
        cast.link('L-001', jia)
        db.save_lookup('L-001', db.LOOKUP_FILLED, 0, DAY, DAY)
        backfill.request_search('avwiki:/av-actress/jia/', '演员甲')
        backfill.run(fetcher=site({SEARCH_JIA: search_html(('/star/a1', '演员甲'))}))
        assert db.Actress.get_by_id(jia.id).javbus == '/star/a1'
        # 库里还没有这个演员页的标签：名字不变
        assert cast.stars_of('L-001') == ['演员甲']

    def test_a_chosen_page_whose_star_has_another_name_only_becomes_her_page(self, site, today, cast):
        jia = cast.actress('えんいんこう', 'jia', 'Actor A')
        cast.movie('L-001')
        cast.link('L-001', jia)
        db.save_lookup('L-001', db.LOOKUP_FILLED, 0, DAY, DAY)
        cast.movie('C-001', ('演员甲', '/star/a1'))
        db.StarLink.create(key='avwiki:/av-actress/jia/', name='えんいんこう', state=db.LINK_CHOOSE,
                           candidates='[["/star/a1", "演员甲"], ["/star/a2", "演员乙"]]')
        assert backfill.choose_page('avwiki:/av-actress/jia/', '/star/a1') is True
        actress = db.Actress.get_by_id(jia.id)
        # 演员页上的名字不是她的任何一个名字：只记下演员页，正式名字、影片上的挂接都不动
        assert (db.Tag.get_by_id(actress.tag_id).value, actress.javbus) == ('えんいんこう', '/star/a1')
        assert cast.stars_of('L-001') == ['えんいんこう'] and cast.stars_of('C-001') == ['演员甲']
        # 那个演员页的影片按人算是她的
        person, = actresses.find_people(['Actor A'])['Actor A']
        assert sorted(db.Tag.get_by_id(tag).value for tag in person.tags) == ['えんいんこう', '演员甲']
        # 每一轮补名开头的核对也不改她的名字
        assert actresses.reconcile() == set()
        assert db.Tag.get_by_id(db.Actress.get_by_id(jia.id).tag_id).value == 'えんいんこう'

    def test_a_chosen_page_whose_star_has_one_of_her_names_gives_her_that_name(self, site, today, cast):
        jia = cast.actress('えんいんこう', 'jia', 'Actor A', '演員甲')
        cast.movie('L-001')
        cast.link('L-001', jia)
        db.save_lookup('L-001', db.LOOKUP_FILLED, 0, DAY, DAY)
        cast.movie('C-001', ('演员甲', '/star/a1'))
        db.StarLink.create(key='avwiki:/av-actress/jia/', name='えんいんこう', state=db.LINK_CHOOSE,
                           candidates='[["/star/a1", "演员甲"], ["/star/a2", "演员乙"]]')
        assert backfill.choose_page('avwiki:/av-actress/jia/', '/star/a1') is True
        actress = db.Actress.get_by_id(jia.id)
        assert (db.Tag.get_by_id(actress.tag_id).value, actress.javbus) == ('演员甲', '/star/a1')
        assert cast.stars_of('L-001') == ['演员甲'] and cast.stars_of('C-001') == ['演员甲']
        aliases = [row.name for row in db.ActressAlias.select().where(db.ActressAlias.actress == jia.id)
                   .order_by(db.ActressAlias.id)]
        # 换下来的正式名字排在别名的最前面；她自己的写法「演員甲」留在原来的位置上
        assert aliases == ['えんいんこう', 'Actor A', '演員甲']

    def test_the_one_with_the_same_name_is_taken(self, site, today):
        fetcher = site({SEARCH_JIA: search_html(('/star/a2', '演员甲乙'), ('/star/a1', '演員甲'))})
        backfill.request_search('name:演员甲', '演员甲')
        result = backfill.run(fetcher=fetcher)
        assert fetcher.requests == [SEARCH_JIA]
        assert link() == ('found', '/star/a1', [('/star/a2', '演员甲乙'), ('/star/a1', '演員甲')], '')
        assert db.StarLink.get().checked_at == DAY
        assert result.pages == 0

    def test_several_with_the_same_name_are_left_to_the_user(self, site, today):
        fetcher = site({SEARCH_JIA: search_html(('/star/a1', '演员甲'), ('/star/a3', '演员甲'))})
        backfill.request_search('name:演员甲', '演员甲')
        backfill.run(fetcher=fetcher)
        assert link() == ('choose', '', [('/star/a1', '演员甲'), ('/star/a3', '演员甲')], '')

    def test_only_other_names_are_left_to_the_user(self, site, today):
        fetcher = site({SEARCH_JIA: search_html(('/star/a2', '演员甲乙'))})
        backfill.request_search('name:演员甲', '演员甲')
        backfill.run(fetcher=fetcher)
        assert link() == ('choose', '', [('/star/a2', '演员甲乙')], '')

    @pytest.mark.parametrize('page', [search_html(), NotFoundError('页面不存在（HTTP 404）')])
    def test_nobody_found(self, site, today, page):
        backfill.request_search('name:演员甲', '演员甲')
        backfill.run(fetcher=site({SEARCH_JIA: page}))
        assert link() == ('none', '', [], '')

    def test_too_many_candidates_are_cut(self, site, today, monkeypatch):
        monkeypatch.setattr(backfill, 'MAX_CANDIDATES', 2)
        fetcher = site({SEARCH_JIA: search_html(('/star/a1', '甲一'), ('/star/a2', '甲二'), ('/star/a3', '甲三'))})
        backfill.request_search('name:演员甲', '演员甲')
        backfill.run(fetcher=fetcher)
        assert link()[2] == [('/star/a1', '甲一'), ('/star/a2', '甲二')]

    def test_a_search_that_cannot_be_made_says_why(self, site, today):
        fetcher = site({SEARCH_JIA: NetworkError('网络连接失败（ConnectTimeout）：https://www.javbus.com/searchstar/…')})
        backfill.request_search('name:演员甲', '演员甲')
        backfill.run(fetcher=fetcher)
        assert link() == ('error', '', [], '网络连接失败（ConnectTimeout）：https://www.javbus.com/searchstar/…')

    def test_blocked_stops_and_pauses(self, site, today):
        backfill.request_search('name:演员甲', '演员甲')
        backfill.request_check(STAR_A)
        fetcher = site({SEARCH_JIA: BlockedError(BLOCKED)})
        result = backfill.run(fetcher=fetcher)
        assert fetcher.requests == [SEARCH_JIA]
        assert link() == ('error', '', [], f'被网站拦截：{BLOCKED}')
        assert (result.stopped, backfill.paused()) == ('被网站拦截', (BLOCKED, DAY))

    def test_searching_again_starts_over(self, site, today):
        backfill.request_search('name:演员甲', '演员甲')
        backfill.run(fetcher=site({SEARCH_JIA: search_html(('/star/a2', '演员甲乙'))}))
        backfill.request_search('name:演员甲', '演员甲')
        assert link() == ('pending', '', [], '')

    def test_the_user_picks_one_of_the_candidates(self, site, today):
        backfill.request_search('name:演员甲', '演员甲')
        backfill.run(fetcher=site({SEARCH_JIA: search_html(('/star/a2', '演员甲乙'), ('/star/a3', '甲'))}))
        assert backfill.choose_page('name:演员甲', '/star/a3') is True
        assert link()[:2] == ('found', '/star/a3')

    @pytest.mark.parametrize('path', ['/star/a9', '/genre/1', ''])
    def test_only_a_candidate_can_be_picked(self, site, today, path):
        backfill.request_search('name:演员甲', '演员甲')
        backfill.run(fetcher=site({SEARCH_JIA: search_html(('/star/a2', '演员甲乙'))}))
        assert backfill.choose_page('name:演员甲', path) is False
        assert link()[:2] == ('choose', '')

    def test_the_log_has_no_name(self, site, today, caplog):
        caplog.set_level(logging.DEBUG, logger='bustag')
        backfill.request_search('name:演员甲', '演员甲')
        backfill.run(fetcher=site({SEARCH_JIA: search_html(('/star/a1', '演员甲'))}))
        number = db.StarLink.get().id
        assert f'查找演员页 #{number}：找到 1 位，名字相同的正好一位，已采用' in caplog.messages
        assert '演员甲' not in caplog.text and '%E6%BC%94' not in caplog.text

    def test_found_pages(self, today):
        db.StarLink.create(key='avwiki:/av-actress/jia/', name='演员甲', state='found', path='/star/a1')
        db.StarLink.create(key='name:演员乙', name='演员乙', state='choose', path='')
        assert db.found_pages(['avwiki:/av-actress/jia/', 'name:演员乙', 'name:没有']) == {
            'avwiki:/av-actress/jia/': '/star/a1'}

    def test_a_badge_next_to_the_name_does_not_keep_the_one_with_the_same_name_from_being_taken(self, site, today):
        fetcher = site({SEARCH_JIA: search_html(('/star/a2', '演员甲乙'), ('/star/a1', '演員甲'), badge='角标')})
        backfill.request_search('name:演员甲', '演员甲')
        backfill.run(fetcher=fetcher)
        assert link() == ('found', '/star/a1', [('/star/a2', '演员甲乙'), ('/star/a1', '演員甲')], '')

    def test_the_candidates_left_to_the_user_are_written_without_the_badge(self, site, today):
        fetcher = site({SEARCH_JIA: search_html(('/star/a1', '演员甲'), ('/star/a3', '演员甲'), badge='角标')})
        backfill.request_search('name:演员甲', '演员甲')
        backfill.run(fetcher=fetcher)
        assert link() == ('choose', '', [('/star/a1', '演员甲'), ('/star/a3', '演员甲')], '')


JIA_KEY = 'avwiki:/av-actress/jia/'


def actress_now(actress):
    '''(正式名字, [别名…], JavBus 演员页, 女优页最近读的时间)'''
    fresh = db.Actress.get_by_id(actress.id)
    aliases = [row.name for row in db.ActressAlias.select().where(db.ActressAlias.actress == fresh.id)
               .order_by(db.ActressAlias.id)]
    return db.Tag.get_by_id(fresh.tag_id).value, aliases, fresh.javbus, fresh.names_at


class TestTakingBackHerJavbusPage:
    '''给 av-wiki 女优关联的 JavBus 演员页可以取消、换一个：正式名字按规则重新定，补名写的影片跟着改回来'''

    @pytest.fixture
    def linked(self, today, cast):
        '''
        えんいんこう（女优页 jia，别名 演员甲）点选了 /star/a1：那里的名字演员甲是她的一个名字，正式名字换成了 JavBus 的那条
        '''
        jia = cast.actress('えんいんこう', 'jia', '演员甲')
        db.Actress.update(names_at=DAY).where(db.Actress.id == jia.id).execute()
        cast.movie('L-001')
        cast.link('L-001', jia)
        db.save_lookup('L-001', db.LOOKUP_FILLED, 0, DAY, DAY)
        cast.movie('C-001', ('演员甲', STAR_A))
        db.StarLink.create(key=JIA_KEY, name='えんいんこう', state=db.LINK_CHOOSE,
                           candidates=f'[["{STAR_A}", "演员甲"], ["{STAR_B}", "演员乙"]]')
        assert backfill.choose_page(JIA_KEY, STAR_A) is True
        # 她自己那一侧的「演员甲」照样留在别名里原来的位置上
        assert actress_now(jia)[:3] == ('演员甲', ['えんいんこう', '演员甲'], STAR_A)
        assert cast.stars_of('L-001') == ['演员甲']
        return jia

    def test_unlinking_gives_her_own_name_back(self, linked, cast):
        removed = backfill.unlink(JIA_KEY, STAR_A)
        # 补名写的影片改回她自己的名字，JavBus 的影片还是 JavBus 的那条
        assert actress_now(linked) == ('えんいんこう', ['演员甲'], '', None)
        assert cast.stars_of('L-001') == ['えんいんこう'] and cast.stars_of('C-001') == ['演员甲']
        assert db.Tag.get(db.Tag.value == '演员甲').url == STAR_A
        assert db.Tag.get(db.Tag.value == 'えんいんこう').url == '/av-actress/jia/'
        # 正式名字变了，names_at 清掉：下一轮再读一次她的女优页，按页面上的先后再定一遍
        assert db.StarLink.get_or_none(db.StarLink.key == JIA_KEY) is None
        assert actresses.rejected_pages('/av-actress/jia/') == {STAR_A}
        assert removed == 0
        # 按人算时那个演员页的影片不再算她
        person, = actresses.find_people(['えんいんこう'])['えんいんこう']
        assert person.tags == {db.Tag.get(db.Tag.value == 'えんいんこう').id}

    def test_an_unlinked_page_is_not_linked_again_by_her_names(self, linked, cast, monkeypatch):
        backfill.unlink(JIA_KEY, STAR_A)
        monkeypatch.setattr(actresses, 'get_now_time', lambda: DAY)
        actresses.read_page('/av-actress/jia/', ['演员甲', 'えんいんこう'], DAY)
        name, aliases, javbus, _ = actress_now(linked)
        # 第一个名字和那位 JavBus 演员相同：不是同一个人，她用"演员甲（2）"
        assert (name, aliases, javbus) == ('演员甲（2）', ['演员甲', 'えんいんこう'], '')
        assert cast.stars_of('L-001') == ['演员甲（2）'] and cast.stars_of('C-001') == ['演员甲']
        assert actresses.reconcile() == set()

    def test_unlinking_a_page_that_never_changed_her_name_changes_nothing_else(self, today, cast):
        jia = cast.actress('えんいんこう', 'jia', 'Actor A')
        db.Actress.update(names_at=DAY).where(db.Actress.id == jia.id).execute()
        cast.movie('L-001')
        cast.link('L-001', jia)
        cast.movie('C-001', ('演员甲', STAR_A))
        db.StarLink.create(key=JIA_KEY, name='えんいんこう', state=db.LINK_CHOOSE, candidates=f'[["{STAR_A}", "演员甲"]]')
        backfill.choose_page(JIA_KEY, STAR_A)
        backfill.unlink(JIA_KEY, STAR_A)
        assert actress_now(jia) == ('えんいんこう', ['Actor A'], '', DAY)
        assert cast.stars_of('L-001') == ['えんいんこう'] and cast.stars_of('C-001') == ['演员甲']

    def test_unlinking_cancels_what_is_still_queued_from_that_page(self, linked):
        db.ListSource.create(path=STAR_A, kind='star', wanted=5)
        queued = [db.FetchTask.create(path=f'/Q-{n}', fanhao=f'Q-{n}', origin=db.ORIGIN_LIST,
                                      source=source(STAR_A), page=1) for n in range(3)]
        db.FetchTask.update(status=db.TASK_DONE).where(db.FetchTask.id == queued[0].id).execute()
        assert backfill.unlink(JIA_KEY, STAR_A) == 2
        assert statuses() == {'Q-0': 'done'}
        assert source(STAR_A).wanted == 0

    def test_a_page_another_actress_is_linked_to_keeps_its_queue(self, linked, cast):
        cast.actress('演员丙', 'bing', javbus=STAR_A)
        db.ListSource.create(path=STAR_A, kind='star')
        db.FetchTask.create(path='/Q-1', fanhao='Q-1', origin=db.ORIGIN_LIST, source=source(STAR_A), page=1)
        assert backfill.unlink(JIA_KEY, STAR_A) == 0
        assert statuses() == {'Q-1': 'queued'}

    def test_another_one_offers_the_candidates_again(self, linked, cast):
        assert backfill.change(JIA_KEY, STAR_A, '演员甲') == (None, 0)
        assert link(JIA_KEY)[:3] == ('choose', '', [(STAR_A, '演员甲'), (STAR_B, '演员乙')])
        assert actress_now(linked)[:3] == ('えんいんこう', ['演员甲'], '')
        cast.movie('C-002', ('演员乙', STAR_B))
        assert backfill.choose_page(JIA_KEY, STAR_B) is True
        # 演员乙不是她的名字：只记下演员页
        assert actress_now(linked)[:3] == ('えんいんこう', ['演员甲'], STAR_B)
        assert actresses.rejected_pages('/av-actress/jia/') == {STAR_A}

    def test_another_one_without_candidates_searches_again_with_her_name(self, today, cast):
        # 她的名字和那位 JavBus 演员一字不差、直接用了那条标签（没有查找记录）：换下来是"演员甲（2）"，查找用不带编号的名字
        cast.movie('C-001', ('演员甲', STAR_A))
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', javbus=STAR_A)
        assert db.Tag.get_by_id(jia.tag_id).url == STAR_A
        assert backfill.change(JIA_KEY, STAR_A, '演员甲') == ('演员甲', 0)
        assert link(JIA_KEY) == ('pending', '', [], '')
        assert actress_now(jia)[:3] == ('演员甲（2）', ['えんいんこう', '演员甲'], '')
        assert db.StarLink.get(db.StarLink.key == JIA_KEY).name == '演员甲'

    def test_a_search_does_not_take_a_page_she_was_unlinked_from(self, linked, site):
        backfill.unlink(JIA_KEY, STAR_A)
        backfill.request_search(JIA_KEY, '演员甲')
        backfill.run(fetcher=site({SEARCH_JIA: search_html((STAR_A, '演员甲'))}))
        assert link(JIA_KEY)[:3] == ('choose', '', [(STAR_A, '演员甲')])
        assert actress_now(linked)[2] == ''

    def test_picking_a_page_she_was_unlinked_from_links_it_again(self, linked):
        backfill.unlink(JIA_KEY, STAR_A)
        db.StarLink.create(key=JIA_KEY, name='演员甲', state=db.LINK_CHOOSE, candidates=f'[["{STAR_A}", "演员甲"]]')
        assert backfill.choose_page(JIA_KEY, STAR_A) is True
        assert actress_now(linked)[2] == STAR_A
        assert actresses.rejected_pages('/av-actress/jia/') == set()

    def test_unlinking_an_actress_of_a_library_not_yet_upgraded(self, today, cast):
        cast.movie('L-001')
        cast.legacy('L-001', 'jia', 'えんいんこう', 'Actor A')
        cast.movie('C-001', ('演员甲', STAR_A))
        db.StarLink.create(key=JIA_KEY, name='えんいんこう', state=db.LINK_FOUND, path=STAR_A)
        backfill.unlink(JIA_KEY, STAR_A)
        person, = actresses.find_people(['Actor A'])['Actor A']
        assert (person.javbus, len(person.tags)) == ('', 2)
        assert actresses.rejected_pages('/av-actress/jia/') == {STAR_A}


class TestPagesSheIsNotAtAreNotSearchesWaitingForTheirTurn:
    '''
    star_link 里记着"这些演员页不是她"的那一行（state = rejected，不是一次查找）：被拦截时丢掉等着的查找、旧版本遗留的整理、
    抓排队的影片时找活干，都只管等着的查找（state = pending），不能把它当成等着的查找去发请求、丢掉或改写
    '''

    @pytest.fixture
    def not_her(self, today, cast):
        jia = cast.actress('えんいんこう', 'jia', '演员甲')
        db.StarLink.create(key=JIA_KEY, name='えんいんこう', state=db.LINK_FOUND, path=STAR_A)
        backfill.unlink(JIA_KEY, STAR_A)
        return db.StarLink.get(db.StarLink.key == actresses.NOT_HER + '/av-actress/jia/')

    def row(self):
        found = db.StarLink.get(db.StarLink.key == actresses.NOT_HER + '/av-actress/jia/')
        return found.state, found.error, found.checked_at, found.candidate_list

    def test_it_is_not_work_for_the_run(self, not_her, site):
        assert not_her.state == db.LINK_REJECTED
        fetcher = site({})
        assert backfill.run(fetcher=fetcher) is not None
        assert fetcher.requests == []
        assert backfill.has_work() is False

    def test_a_block_does_not_drop_it_but_drops_the_search_waiting_next_to_it(self, not_her):
        before = self.row()
        backfill.request_search('name:演员乙', '演员乙')
        backfill.pause(BLOCKED)
        assert self.row() == before
        assert (db.StarLink.get(db.StarLink.key == 'name:演员乙').state, db.StarLink.get(
            db.StarLink.key == 'name:演员乙').error) == ('error', SEARCH_DROPPED)
        assert actresses.rejected_pages('/av-actress/jia/') == {STAR_A}

    def test_tidying_what_an_older_version_left_behind_does_not_touch_it(self, not_her, site):
        before = self.row()
        backfill.request_search('name:演员乙', '演员乙')
        # 旧版本记的暂停：没有"整理过"的记号
        db.JobState.create(name=backfill.PAUSE, text=BLOCKED, at=DAY)
        fetcher = site({})
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == []
        assert self.row() == before
        assert db.StarLink.get(db.StarLink.key == 'name:演员乙').error == SEARCH_DROPPED

    def test_a_run_makes_the_search_that_is_waiting_and_only_that_one(self, not_her, site):
        # 库里同时有"不是她"的行和一次真要做的等着的查找：请求里只有这一次查找，"不是她"的行一个字不改
        before = self.row()
        backfill.request_search('name:演员乙', '演员乙')
        fetcher = site({SEARCH_YI: search_html(('/star/b2', '演员乙'))})
        backfill.run(fetcher=fetcher)
        assert fetcher.requests == [SEARCH_YI]
        assert self.row() == before
        assert link('name:演员乙')[:2] == ('found', '/star/b2')

    def test_the_search_for_another_one_is_a_search_like_any_other_when_a_block_comes(self, not_her):
        # 「换一个」没有上次的结果时登记的重新查找：拦截时和别的等着的查找一样写明原因，不改"不是她"的记录
        before = self.row()
        backfill.request_search(JIA_KEY, 'えんいんこう')
        backfill.pause(BLOCKED)
        assert (link(JIA_KEY)[0], link(JIA_KEY)[3]) == ('error', SEARCH_DROPPED)
        assert self.row() == before


class TestWritesFromTheWebWaitInTurn:
    """
    网页请求里的写操作都要先拿写锁（BEGIN IMMEDIATE），同时在等写锁的网页请求才不会超过 db.MAX_WAITING_REQUESTS：
    没有位置了就马上得到 TooManyWaiting，不白等 30 秒
    """

    @pytest.mark.parametrize('call', [
        lambda: backfill.request_check(STAR_A),
        lambda: backfill.request_fill(STAR_A),
        lambda: backfill.cancel(STAR_A),
        lambda: backfill.retry_source(STAR_A),
        lambda: backfill.request_search('name:演员甲', '演员甲'),
        lambda: backfill.choose_page('name:演员甲', '/star/a1'),
        lambda: backfill.resume(),
        lambda: backfill.retry_codes(),
        lambda: backfill.forget_problems(),
        lambda: upload('ABC-001'),
    ], ids=['check', 'fill', 'cancel', 'retry-source', 'search', 'choose', 'resume', 'retry-codes', 'forget',
            'upload'])
    def test_a_write_is_turned_away_when_no_place_is_left(self, monkeypatch, call):
        db.ListSource.create(path=STAR_A, kind='star')
        db.StarLink.create(key='name:演员甲', name='演员甲', state='choose', candidates='[["/star/a1", "演员甲"]]')
        monkeypatch.setattr(db, 'MAX_WAITING_REQUESTS', 0)
        with db.limited_waiting():
            with pytest.raises(db.TooManyWaiting):
                call()
        assert db.waiting_requests() == 0
