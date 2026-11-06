'''
解析器：用保存的网页样本（tests/fixtures/）验证详情页、列表页的解析结果
'''
import copy
import re

import pytest
from bs4 import BeautifulSoup

from bustag.spider.parser import (
    ParseError, Tag, fanhao_from_path, has_next_page, parse_item, parse_list,
)

ZBBB_004_TAGS = [
    Tag('導演:', '実録出版', '/director/ec'),
    Tag('製作商:', '実録出版', '/studio/bt'),
    Tag('發行商:', '実録出版', '/label/17e'),
    Tag('系列:', 'BBBビッグ・ブーブス・バット', '/series/2ah'),
    Tag('genre', '其他戀物癖', '/genre/1y'),
    Tag('genre', '屁股', '/genre/2k'),
    Tag('genre', '高畫質', '/genre/4o'),
    Tag('genre', '單體作品', '/genre/f'),
    Tag('star', '神ユキ', '/star/7ry'),
]

ABP_001_META = {
    'fanhao': 'ABP-001',
    'title': '水咲ローラがご奉仕しちゃう超最新やみつきエステ',
    'cover_img_url': '/pics/cover/2qh5_b.jpg',
    'release_date': '2013-06-28',
    'length': '125',
}

ABP_001_TAGS = [
    Tag('製作商:', 'プレステージ', '/studio/75'),
    Tag('發行商:', 'ABSOLUTELYPERFECT', '/label/xo'),
    Tag('系列:', '最新やみつきエステ', '/series/829'),
    Tag('genre', '美容院', '/genre/44'),
    Tag('genre', '按摩', '/genre/45'),
    Tag('genre', '單體作品', '/genre/f'),
    Tag('star', '水咲ローラ（滝澤ローラ）', '/star/82w'),
]

ABP_001 = 'detail-ABP-001-series-no-director.html'
ZBBB_004 = 'detail-ZBBB-004-director.html'


def soup_of(html):
    return BeautifulSoup(html, 'html.parser')


def header_row(soup, label):
    header = soup.find('span', class_='header', string=lambda s: s and s.strip() == label)
    return header.find_parent('p')


class TestDetailPage:
    def test_page_with_director(self, page):
        meta, tags = parse_item(page(ZBBB_004))
        assert meta == {
            'fanhao': 'ZBBB-004',
            'title': 'BBB BigBoobsButt 神ユキ',
            'cover_img_url': '/pics/cover/73sj_b.jpg',
            'release_date': '2019-04-23',
            'length': '180',
        }
        # 導演、製作商、發行商同名也各算一个标签；信息区下方的演员头像和导航链接都不算
        assert tags == ZBBB_004_TAGS

    def test_page_with_series_and_without_director(self, page):
        meta, tags = parse_item(page(ABP_001))
        assert meta == ABP_001_META
        assert tags == ABP_001_TAGS

    def test_tags_have_the_same_shape_as_2019_database_rows(self, page):
        # 2019 年 bus.db 的 tag 表里同一批标签的 (type, value, url)
        rows_2019 = {
            ('製作商:', '実録出版', '/studio/bt'),
            ('發行商:', '実録出版', '/label/17e'),
            ('genre', '高畫質', '/genre/4o'),
        }
        _, tags = parse_item(page(ZBBB_004))
        assert rows_2019 <= {tuple(tag) for tag in tags}

    def test_page_without_label(self, page):
        meta, tags = parse_item(page('detail-no-label-synthetic.html'))
        assert meta == ABP_001_META
        assert tags == [tag for tag in ABP_001_TAGS if tag.type != '發行商:']

    def test_page_without_stars(self, page):
        meta, tags = parse_item(page('detail-no-stars-synthetic.html'))
        assert meta == ABP_001_META
        assert tags == [tag for tag in ABP_001_TAGS if tag.type != 'star']

    def test_page_without_length(self, page):
        meta, tags = parse_item(page('detail-no-length-synthetic.html'))
        assert meta == dict(ABP_001_META, length=None)
        assert tags == ABP_001_TAGS

    def test_absolute_dmm_cover_is_kept_as_is(self, page):
        meta, tags = parse_item(page('detail-dmm-cover-synthetic.html'))
        assert meta['cover_img_url'] == (
            'https://pics.dmm.co.jp/digital/video/483zbbb00004/483zbbb00004pl.jpg')
        assert tags == ZBBB_004_TAGS

    @pytest.mark.parametrize('value', ['0000-00-00', '2013-02-30', '未知', ''])
    def test_invalid_or_missing_release_date_is_none(self, page, value):
        html = page(ABP_001).replace('2013-06-28', value)
        meta, _ = parse_item(html)
        assert meta['release_date'] is None
        assert meta['fanhao'] == 'ABP-001'

    def test_links_are_paths_whatever_the_domain(self, page):
        html = page(ZBBB_004)
        mirrored = html.replace('https://www.javbus.com/', 'https://www.busjav.casa/')
        assert mirrored != html
        assert parse_item(mirrored) == parse_item(html)
        assert all(tag.link.startswith('/') for tag in parse_item(html)[1])

    def test_duplicate_tags_are_dropped(self, page):
        soup = soup_of(page(ABP_001))
        for selector in ('div.info p span.genre a[href*="/star/"]',
                         'div.info p span.genre a[href*="/genre/44"]'):
            span = soup.select_one(selector).find_parent('span', class_='genre')
            span.parent.append(copy.copy(span))
        _, tags = parse_item(str(soup))
        assert tags == ABP_001_TAGS

    def test_same_value_under_different_types_is_kept(self, page):
        soup = soup_of(page(ABP_001))
        soup.select_one('div.info a[href$="/series/829"]').string = 'プレステージ'
        _, tags = parse_item(str(soup))
        assert Tag('製作商:', 'プレステージ', '/studio/75') in tags
        assert Tag('系列:', 'プレステージ', '/series/829') in tags

    def test_fanhao_with_digit_prefix(self, page):
        meta, _ = parse_item(page(ABP_001).replace('ABP-001', '259LUXU-1908'))
        assert meta['fanhao'] == '259LUXU-1908'
        assert meta['title'] == ABP_001_META['title']

    def test_fanhao_falls_back_to_heading(self, page):
        soup = soup_of(page(ABP_001))
        header_row(soup, '識別碼:').decompose()
        meta, _ = parse_item(str(soup))
        assert meta['fanhao'] == 'ABP-001'
        assert meta['title'] == ABP_001_META['title']

    def test_cover_falls_back_to_image_and_then_empty(self, page):
        soup = soup_of(page(ABP_001))
        big_image = soup.select_one('a.bigImage')
        del big_image['href']
        assert parse_item(str(soup))[0]['cover_img_url'] == '/pics/cover/2qh5_b.jpg'
        big_image.img.decompose()
        assert parse_item(str(soup))[0]['cover_img_url'] == ''

    @pytest.mark.parametrize('name', ['driver-verify.html', 'list-home.html'])
    def test_pages_other_than_detail_raise(self, page, name):
        with pytest.raises(ParseError):
            parse_item(page(name))

    def test_heading_without_info_raises(self, page):
        soup = soup_of(page(ABP_001))
        soup.select_one('div.info').decompose()
        with pytest.raises(ParseError):
            parse_item(str(soup))

    def test_empty_html_raises(self):
        with pytest.raises(ParseError):
            parse_item('')


class TestListPage:
    def test_home_page(self, page):
        html = page('list-home.html')
        paths = parse_list(html)
        # 30 个影片框里有两部各出现两次
        assert len(soup_of(html).select('a.movie-box')) == 30
        assert len(paths) == 28
        assert len(set(paths)) == 28
        assert paths[:3] == ['/IENF-465', '/SKTH-009', '/SDJS-386']
        # 同番号的另一部影片路径带发行日期后缀，两条都保留
        assert '/SORA-655_2026-10-31' in paths
        assert '/SORA-655' in paths
        assert all(re.fullmatch(r'/[^/]+', path) for path in paths)

    def test_second_page(self, page):
        paths = parse_list(page('list-page-2.html'))
        assert paths[0] == '/URVRSP-618'
        assert len(set(paths)) == len(paths)
        assert not set(paths) & set(parse_list(page('list-home.html')))

    def test_last_page_has_the_same_movies(self, page):
        assert parse_list(page('list-last-page-synthetic.html')) == parse_list(page('list-page-2.html'))

    def test_only_single_segment_paths_are_kept(self):
        html = ''.join([
            '<a class="movie-box" href="https://www.javbus.com/ABC-123">1</a>',
            '<a class="movie-box" href="/ABC-123?from=list">2</a>',
            '<a class="movie-box" href="/forum/forum.php?mod=viewthread&amp;tid=1">3</a>',
            '<a class="movie-box" href="https://www.busjav.casa/XYZ-001_2026-01-02">4</a>',
            '<a class="movie-box">5</a>',
            '<a href="/DEF-456">6</a>',
        ])
        assert parse_list(html) == ['/ABC-123', '/XYZ-001_2026-01-02']

    def test_verify_page_has_no_movies(self, page):
        assert parse_list(page('driver-verify.html')) == []

    @pytest.mark.parametrize('name, expected', [
        ('list-home.html', True),
        ('list-page-2.html', True),
        ('list-last-page-synthetic.html', False),
        (ZBBB_004, False),
        ('driver-verify.html', False),
    ])
    def test_has_next_page(self, page, name, expected):
        assert has_next_page(page(name)) is expected


@pytest.mark.parametrize('path, fanhao', [
    ('/IENF-465', 'IENF-465'),
    ('IENF-465', 'IENF-465'),
    ('/SORA-655_2026-10-31', 'SORA-655'),
    ('/259LUXU-1908', '259LUXU-1908'),
    ('/ABC-123_2026', 'ABC-123_2026'),
])
def test_fanhao_from_path(path, fanhao):
    assert fanhao_from_path(path) == fanhao
