'''
封面：地址换算、图片校验、按需下载与本地缓存（下载走 FakeFetcher）
'''
import threading
import time

import pytest

from bustag import util
from bustag.spider import covers, db
from bustag.spider.covers import (
    MAX_SIZE, MIN_SIZE, cover_referer, cover_source_url, get_cover_file, normalize_cover_url,
    validate_image,
)
from bustag.spider.fetcher import FetchError

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
        assert cover_fetcher.created == [{'delay': 0, 'retries': 1}]
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

    @pytest.mark.parametrize('fanhao', ['', '../ABC-001', 'ABC_001', 'ABC 001', None])
    def test_invalid_fanhao(self, make_item, cover_fetcher, fanhao):
        make_item('ABC_001')
        assert get_cover_file(fanhao) is None
        assert cover_fetcher.requests == []

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
        deadline = time.monotonic() + 10
        while len(peak) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        assert max(peak) == 2
        release.set()
        for thread in threads:
            thread.join(timeout=10)
        assert all(results[f] and results[f][0].endswith(f'{f}.jpg') for f in fanhaos)
        assert max(peak) == 2
