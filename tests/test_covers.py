'''
封面：地址换算、图片校验、按需下载与本地缓存、失败记录与排队（下载走 FakeFetcher）
'''
import errno
import logging
import threading
import time
import types

import pytest

from bustag import util
from bustag.spider import covers, db
from bustag.spider.covers import (
    MAX_SIZE, MIN_SIZE, cover_referer, cover_source_url, get_cover_file, normalize_cover_url,
    validate_image,
)
from bustag.spider.fetcher import FetchError, NetworkError, NotFoundError

JPEG = b'\xff\xd8\xff\xe0' + b'0' * 4096
DMM = 'https://pics.dmm.co.jp/digital/video/483zbbb00004/483zbbb00004pl.jpg'


@pytest.mark.parametrize('url, expected', [
    (None, None),
    ('', ''),
    ('/pics/cover/abc_b.jpg', '/pics/cover/abc_b.jpg'),
    ('https://pics.javcdn.pw/cover/7by5_b.jpg', '/pics/cover/7by5_b.jpg'),
    ('https://pics.javcdn.pw/pics/cover/7by5_b.jpg', '/pics/cover/7by5_b.jpg'),
    ('HTTPS://PICS.JAVCDN.PW/cover/7by5_b.jpg', '/pics/cover/7by5_b.jpg'),
    ('https://www.busdmm.work/pics/cover/x_b.jpg', '/pics/cover/x_b.jpg'),
    ('https://www.cdnbus.bid/pics/cover/x_b.jpg', '/pics/cover/x_b.jpg'),
    ('https://www.javbus.com/pics/cover/x_b.jpg', 'https://www.javbus.com/pics/cover/x_b.jpg'),
    (DMM, DMM),
])
def test_normalize_cover_url(url, expected):
    assert normalize_cover_url(url) == expected


class TestSourceUrl:
    def test_relative_cover_uses_current_root_path(self, monkeypatch):
        assert cover_referer() == 'https://www.javbus.com/'
        assert cover_source_url('/pics/cover/abc_b.jpg') == 'https://www.javbus.com/pics/cover/abc_b.jpg'
        monkeypatch.setitem(util.APP_CONFIG, 'download.root_path', 'https://www.busjav.casa')
        assert cover_referer() == 'https://www.busjav.casa/'
        assert cover_source_url('https://pics.javcdn.pw/cover/7by5_b.jpg') == (
            'https://www.busjav.casa/pics/cover/7by5_b.jpg')

    def test_absolute_cover_and_missing_cover(self):
        assert cover_source_url(DMM) == DMM
        assert cover_source_url('') is None
        assert cover_source_url(None) is None


@pytest.mark.parametrize('size, content_type, expected', [
    (2048, 'text/html', '不是图片（content-type: text/html）'),
    (2048, '', '不是图片（content-type: 无）'),
    (2048, 'image/bmp', '不支持的图片格式（image/bmp）'),
    (MIN_SIZE, 'image/jpeg', f'文件太小（{MIN_SIZE} 字节）'),
    (MIN_SIZE + 1, 'image/jpeg', None),
    (MAX_SIZE, 'image/webp', None),
    (MAX_SIZE + 1, 'image/png', f'文件太大（{MAX_SIZE + 1} 字节）'),
])
def test_validate_image(size, content_type, expected):
    assert validate_image(b'0' * size, content_type) == expected


def covers_dir(data_dir):
    return data_dir / 'covers'


class TestCoverCache:
    def test_cache_hit_needs_neither_database_nor_network(self, data_dir, cover_fetcher):
        folder = covers_dir(data_dir)
        folder.mkdir()
        (folder / 'ABC-001.jpg').write_bytes(JPEG)
        (folder / 'ABC-002.webp').write_bytes(JPEG)
        assert get_cover_file('ABC-001') == (str(folder / 'ABC-001.jpg'), 'image/jpeg')
        assert get_cover_file('abc-001') == (str(folder / 'ABC-001.jpg'), 'image/jpeg')
        assert get_cover_file('ABC-002') == (str(folder / 'ABC-002.webp'), 'image/webp')
        assert cover_fetcher.created == []
        assert cover_fetcher.requests == []

    def test_miss_downloads_validates_and_caches(self, data_dir, make_item, cover_fetcher):
        make_item('ABC-001', cover='/pics/cover/abc_b.jpg')
        source = 'https://www.javbus.com/pics/cover/abc_b.jpg'
        cover_fetcher.routes[source] = (JPEG, 'image/jpeg')
        path = covers_dir(data_dir) / 'ABC-001.jpg'
        assert get_cover_file('ABC-001') == (str(path), 'image/jpeg')
        assert path.read_bytes() == JPEG
        assert cover_fetcher.requests == [source]
        assert cover_fetcher.referers == ['https://www.javbus.com/']
        # 封面之间隔 0.3 秒，封面自己一组（不和抓取、补全的请求互相等）
        assert cover_fetcher.created == [{'delay': 0.3, 'retries': 0, 'timeout': (5, 15), 'lane': 'covers'}]
        assert sorted(p.name for p in covers_dir(data_dir).iterdir()) == ['ABC-001.jpg']
        assert get_cover_file('ABC-001') == (str(path), 'image/jpeg')
        assert cover_fetcher.requests == [source]

    @pytest.mark.parametrize('cover, source', [
        (DMM, DMM),
        ('https://pics.javcdn.pw/cover/7by5_b.jpg', 'https://www.javbus.com/pics/cover/7by5_b.jpg'),
    ])
    def test_old_cover_addresses(self, make_item, cover_fetcher, cover, source):
        make_item('ABC-001', cover=cover)
        cover_fetcher.routes[source] = (JPEG, 'image/png')
        path, content_type = get_cover_file('ABC-001')
        assert path.endswith('ABC-001.png')
        assert content_type == 'image/png'
        assert cover_fetcher.requests == [source]

    @pytest.mark.parametrize('reply', [
        FetchError('页面不存在（HTTP 404）'),
        (b'<html>not found</html>', 'text/html'),
        (b'tiny', 'image/jpeg'),
    ])
    def test_failed_download_returns_none_and_caches_nothing(self, data_dir, make_item, cover_fetcher, reply):
        make_item('ABC-001', cover='/pics/cover/abc_b.jpg')
        cover_fetcher.routes['https://www.javbus.com/pics/cover/abc_b.jpg'] = reply
        assert get_cover_file('ABC-001') is None
        assert list(covers_dir(data_dir).iterdir()) == []

    @pytest.mark.parametrize('error, reason', [
        (PermissionError(errno.EACCES, 'Permission denied'), '拒绝访问：没有权限，或者正被别的程序占用'),
        (OSError(errno.ENOSPC, 'No space left on device'), '磁盘空间不足'),
    ], ids=['denied', 'disk-full'])
    def test_the_reason_a_cover_cannot_be_cached_is_given_in_chinese(self, data_dir, make_item, cover_fetcher,
                                                                      monkeypatch, caplog, error, reason):
        make_item('ABC-001', cover='/pics/cover/abc_b.jpg')
        cover_fetcher.routes['https://www.javbus.com/pics/cover/abc_b.jpg'] = (JPEG, 'image/jpeg')

        def refuse(source, target):
            raise error

        monkeypatch.setattr(covers.os, 'replace', refuse)
        assert get_cover_file('ABC-001') is None
        warning, = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning == f'封面缓存写入失败 {covers_dir(data_dir) / "ABC-001.jpg"}：{reason}'
        # 写了一半的临时文件不留着
        assert list(covers_dir(data_dir).iterdir()) == []

    @pytest.mark.parametrize('fanhao', ['', '../ABC-001', 'ABC.001', 'ABC 001', '_ABC-001', None])
    def test_invalid_fanhao(self, make_item, cover_fetcher, fanhao):
        make_item('ABC.001')
        assert get_cover_file(fanhao) is None
        assert cover_fetcher.requests == []

    def test_dated_fanhao(self, data_dir, make_item, cover_fetcher):
        make_item('GEBB-062_2026-10-31', cover='/pics/cover/gebb_b.jpg')
        cover_fetcher.routes['https://www.javbus.com/pics/cover/gebb_b.jpg'] = (JPEG, 'image/jpeg')
        path = covers_dir(data_dir) / 'GEBB-062_2026-10-31.jpg'
        assert get_cover_file('GEBB-062_2026-10-31') == (str(path), 'image/jpeg')
        assert path.read_bytes() == JPEG

    def test_unknown_fanhao_or_missing_cover(self, make_item, cover_fetcher):
        make_item('ABC-002', cover='')
        assert get_cover_file('ABC-001') is None
        assert get_cover_file('ABC-002') is None
        assert cover_fetcher.requests == []

    def test_at_most_two_downloads_at_once(self, make_item, cover_fetcher):
        fanhaos = ['ABC-001', 'ABC-002', 'ABC-003', 'ABC-004']
        for fanhao in fanhaos:
            make_item(fanhao, cover=f'/pics/cover/{fanhao}.jpg')
        lock = threading.Lock()
        release = threading.Event()
        active = []
        peak = []

        def slow_download(url):
            with lock:
                active.append(url)
                peak.append(len(active))
            release.wait(timeout=10)
            with lock:
                active.remove(url)
            return JPEG, 'image/jpeg'

        cover_fetcher.default = slow_download
        results = {}

        def worker(fanhao):
            try:
                results[fanhao] = get_cover_file(fanhao)
            finally:
                db.db.close()

        threads = [threading.Thread(target=worker, args=(f,)) for f in fanhaos]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 30
        while len(peak) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        assert max(peak) == 2
        release.set()
        for thread in threads:
            thread.join(timeout=10)
        assert all(results[f] and results[f][0].endswith(f'{f}.jpg') for f in fanhaos)
        assert max(peak) == 2


SOURCE = 'https://www.javbus.com/pics/cover/abc_b.jpg'


@pytest.fixture
def clock(monkeypatch):
    '''covers 模块看到的 time.monotonic()，测试里直接改 clock[0]'''
    now = [1000.0]
    monkeypatch.setattr(covers, 'time', types.SimpleNamespace(monotonic=lambda: now[0]))
    return now


def run_threads(count, target):
    threads = [threading.Thread(target=target) for _ in range(count)]
    for thread in threads:
        thread.start()
    return threads


class FullSlots:
    '''下载名额一直是满的：不真的等，记下每次被要求等多久（秒），测试不靠钟表'''

    def __init__(self):
        self.asked = []

    def acquire(self, timeout=None):
        self.asked.append(timeout)
        return False

    def release(self):
        raise AssertionError('没有拿到名额，不该放回去')


class TestFailuresAndQueue:
    @pytest.mark.parametrize('reply', [
        FetchError('网络连接失败（ConnectTimeout）'),
        (b'<html>blocked</html>', 'text/html'),
    ], ids=['fetch-error', 'not-an-image'])
    def test_failure_is_remembered_for_ten_minutes(self, make_item, cover_fetcher, clock, reply):
        make_item('ABC-001', cover='/pics/cover/abc_b.jpg')
        make_item('ABC-002', cover='/pics/cover/abc2_b.jpg')
        cover_fetcher.routes[SOURCE] = reply
        cover_fetcher.routes['https://www.javbus.com/pics/cover/abc2_b.jpg'] = (JPEG, 'image/jpeg')
        assert get_cover_file('ABC-001') is None
        cover_fetcher.routes[SOURCE] = (JPEG, 'image/jpeg')
        clock[0] += covers.FAILURE_TTL - 1
        assert get_cover_file('ABC-001') is None
        assert get_cover_file('ABC-002') is not None
        assert cover_fetcher.requests == [SOURCE, 'https://www.javbus.com/pics/cover/abc2_b.jpg']
        clock[0] += 1
        assert get_cover_file('ABC-001')[0].endswith('ABC-001.jpg')
        assert cover_fetcher.requests[-1] == SOURCE
        assert covers._failures == {}

    def test_expired_failures_are_dropped_when_there_are_many(self, cover_fetcher, clock, monkeypatch):
        monkeypatch.setattr(covers, 'MAX_FAILURES', 3)
        for name in ('ABC-001', 'ABC-002', 'ABC-003'):
            covers._record_failure(name)
        clock[0] += covers.FAILURE_TTL
        covers._record_failure('ABC-004')
        assert list(covers._failures) == ['ABC-004']

    def test_full_queue_gives_up_without_remembering(self, make_item, cover_fetcher, monkeypatch):
        monkeypatch.setattr(covers, 'QUEUE_TIMEOUT', 0.2)
        make_item('ABC-001', cover='/pics/cover/abc_b.jpg')
        cover_fetcher.routes[SOURCE] = (JPEG, 'image/jpeg')
        full = FullSlots()
        with monkeypatch.context() as patch:
            patch.setattr(covers, '_download_slots', full)
            assert get_cover_file('ABC-001') is None
        # 一共只等了 QUEUE_TIMEOUT 秒就放弃
        assert full.asked == [covers.QUEUE_TIMEOUT]
        assert cover_fetcher.requests == []
        assert covers._failures == {}
        assert get_cover_file('ABC-001')[0].endswith('ABC-001.jpg')

    @pytest.mark.parametrize('reply, found', [((JPEG, 'image/jpeg'), True), (FetchError('网络错误'), False)],
                             ids=['downloaded', 'failed'])
    def test_same_fanhao_is_downloaded_once(self, make_item, cover_fetcher, reply, found):
        make_item('ABC-001', cover='/pics/cover/abc_b.jpg')
        started = threading.Event()
        release = threading.Event()

        def slow_download(url):
            started.set()
            release.wait(timeout=10)
            return reply

        cover_fetcher.routes[SOURCE] = slow_download
        results = []

        def worker():
            try:
                results.append(get_cover_file('ABC-001'))
            finally:
                db.db.close()

        threads = run_threads(1, worker)
        assert started.wait(timeout=30)
        threads += run_threads(2, worker)
        time.sleep(0.2)
        release.set()
        for thread in threads:
            thread.join(timeout=10)
        assert len(results) == 3
        assert len(set(results)) == 1
        assert (results[0] is not None) == found
        assert cover_fetcher.requests == [SOURCE]
        assert covers._inflight == {}


def cover_url(n):
    return f'https://www.javbus.com/pics/cover/abc{n}_b.jpg'


@pytest.fixture
def three_movies(make_item):
    for n in (1, 2, 3):
        make_item(f'ABC-00{n}', cover=f'/pics/cover/abc{n}_b.jpg')


@pytest.fixture
def busy_slots():
    '''两个下载名额都被占着；release() 把它们让出来'''
    for _ in range(covers.MAX_DOWNLOADS):
        covers._download_slots.acquire()
    held = [covers.MAX_DOWNLOADS]

    def release():
        while held[0]:
            covers._download_slots.release()
            held[0] -= 1

    yield release
    release()


class TestUnreachableImageHost:
    '''图片站连不上之后的一分钟里，这个图片站的其他封面直接回占位图，不用每张各等一遍'''

    def test_other_covers_of_the_host_are_not_tried_for_a_minute(self, three_movies, cover_fetcher, clock):
        cover_fetcher.default = NetworkError('网络连接失败（ConnectTimeout）')
        assert get_cover_file('ABC-001') is None
        assert get_cover_file('ABC-002') is None
        assert cover_fetcher.requests == [cover_url(1)]
        cover_fetcher.default = (JPEG, 'image/jpeg')
        clock[0] += covers.OUTAGE_TTL - 1
        assert get_cover_file('ABC-002') is None
        assert cover_fetcher.requests == [cover_url(1)]
        clock[0] += 1
        # 没轮到下载的那些不算失败过，一分钟后照常取
        assert get_cover_file('ABC-002')[0].endswith('ABC-002.jpg')
        assert get_cover_file('ABC-003')[0].endswith('ABC-003.jpg')
        assert cover_fetcher.requests == [cover_url(1), cover_url(2), cover_url(3)]
        # 真的下载失败的那一张仍然 10 分钟内不再下载
        assert get_cover_file('ABC-001') is None
        assert cover_fetcher.requests[-1] == cover_url(3)

    def test_covers_on_another_host_are_still_fetched(self, make_item, cover_fetcher, clock):
        make_item('ABC-001', cover='/pics/cover/abc_b.jpg')
        make_item('ABC-002', cover=DMM)
        cover_fetcher.routes[SOURCE] = NetworkError('网络连接失败（ConnectTimeout）')
        cover_fetcher.routes[DMM] = (JPEG, 'image/jpeg')
        assert get_cover_file('ABC-001') is None
        assert get_cover_file('ABC-002')[0].endswith('ABC-002.jpg')

    @pytest.mark.parametrize('reply', [
        NotFoundError('页面不存在（HTTP 404）'),
        FetchError('HTTP 503，已重试 0 次'),
        (b'<html>blocked</html>', 'text/html'),
    ], ids=['not-found', 'server-error', 'not-an-image'])
    def test_a_cover_that_is_missing_or_broken_does_not_stop_the_others(self, three_movies, cover_fetcher, clock, reply):
        cover_fetcher.routes[cover_url(1)] = reply
        cover_fetcher.default = (JPEG, 'image/jpeg')
        assert get_cover_file('ABC-001') is None
        assert get_cover_file('ABC-002')[0].endswith('ABC-002.jpg')

    def test_cached_covers_are_served_while_the_host_is_unreachable(self, data_dir, three_movies, cover_fetcher, clock):
        folder = covers_dir(data_dir)
        folder.mkdir()
        (folder / 'ABC-002.jpg').write_bytes(JPEG)
        cover_fetcher.default = NetworkError('网络连接失败（ConnectTimeout）')
        assert get_cover_file('ABC-001') is None
        assert get_cover_file('ABC-002') == (str(folder / 'ABC-002.jpg'), 'image/jpeg')

    def test_a_cover_that_was_queued_is_not_fetched_once_the_host_is_known_to_be_unreachable(
            self, three_movies, cover_fetcher, busy_slots):
        cover_fetcher.default = (JPEG, 'image/jpeg')
        results = []

        def worker():
            try:
                results.append(get_cover_file('ABC-002'))
            finally:
                db.db.close()

        thread, = run_threads(1, worker)
        time.sleep(0.3)
        # 排队期间，正在下载的那两张连不上图片站
        covers._note_unreachable(cover_url(1))
        busy_slots()
        thread.join(timeout=10)
        assert results == [None]
        assert cover_fetcher.requests == []
        assert covers._failures == {}


class TestBrowserGone:
    '''排队的封面请求每隔一会儿看一次浏览器还在不在：翻页、关掉页面之后不再占着服务线程等'''

    @pytest.fixture(autouse=True)
    def short_steps(self, monkeypatch):
        monkeypatch.setattr(covers, 'WAIT_STEP', 0.05)

    def test_a_request_queued_for_a_slot_gives_up_when_the_browser_has_left(self, three_movies, cover_fetcher,
                                                                            busy_slots):
        cover_fetcher.default = (JPEG, 'image/jpeg')
        asked = []
        assert get_cover_file('ABC-001', abandoned=lambda: asked.append(1) or True) is None
        # 等了一小步、看了一眼浏览器已经走了就放弃，没有等满 QUEUE_TIMEOUT
        assert asked == [1]
        assert cover_fetcher.requests == []
        assert covers._failures == {}
        busy_slots()
        assert get_cover_file('ABC-001')[0].endswith('ABC-001.jpg')

    def test_the_browser_is_asked_again_after_every_short_wait(self, three_movies, cover_fetcher, busy_slots):
        answers = iter([False, False, False, True])
        asked = []

        def abandoned():
            asked.append(1)
            return next(answers)

        assert get_cover_file('ABC-001', abandoned=abandoned) is None
        assert len(asked) == 4

    def test_a_request_waiting_for_the_same_cover_gives_up_when_the_browser_has_left(self, three_movies,
                                                                                     cover_fetcher):
        started = threading.Event()
        release = threading.Event()

        def slow_download(url):
            started.set()
            release.wait(timeout=10)
            return JPEG, 'image/jpeg'

        cover_fetcher.default = slow_download
        results = []

        def worker():
            try:
                results.append(get_cover_file('ABC-001'))
            finally:
                db.db.close()

        thread, = run_threads(1, worker)
        try:
            assert started.wait(timeout=30)
            asked = []
            assert get_cover_file('ABC-001', abandoned=lambda: asked.append(1) or True) is None
            # 等了一小步、看了一眼浏览器已经走了就放弃，没有等到那个下载结束
            assert asked == [1]
        finally:
            release.set()
        thread.join(timeout=10)
        assert results[0][0].endswith('ABC-001.jpg')
        assert cover_fetcher.requests == [cover_url(1)]

    def test_a_browser_that_stays_gets_its_cover(self, three_movies, cover_fetcher, busy_slots):
        cover_fetcher.default = (JPEG, 'image/jpeg')
        results = []

        def worker():
            try:
                results.append(get_cover_file('ABC-001', abandoned=lambda: False))
            finally:
                db.db.close()

        thread, = run_threads(1, worker)
        time.sleep(0.3)
        assert results == []
        busy_slots()
        thread.join(timeout=10)
        assert results[0][0].endswith('ABC-001.jpg')
