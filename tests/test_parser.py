'''
解析器：用保存的网页样本（tests/fixtures/）验证详情页、列表页的解析结果
'''
import copy
import re

import pytest
from bs4 import BeautifulSoup

from bustag.spider.parser import (
    ParseError, Tag, fanhao_from_path, has_next_page, is_star_page, is_valid_fanhao, list_page_path, parse_item,
    parse_list, parse_list_counts, parse_list_entries, parse_star_search, searchstar_path,
)

ZBBB_004_TAGS = [
    Tag('導演:', '惶曰布艀', '/director/ec'),
    Tag('製作商:', '惶曰布艀', '/studio/bt'),
    Tag('發行商:', '惶曰布艀', '/label/17e'),
    Tag('系列:', 'LLLゾゥギ・ダーダハ・カゥメ', '/series/2ah'),
    Tag('genre', '猗鯵順免菁', '/genre/1y'),
    Tag('genre', '眼渤', '/genre/2k'),
    Tag('genre', '斡瀠鰉', '/genre/4o'),
    Tag('genre', '膿燈作瓰', '/genre/f'),
    Tag('star', '汹フミ', '/star/7ry'),
]

ABP_001_META = {
    'fanhao': 'ABP-001',
    'title': '窄軅ヤーデぷち孟毓しぴぉで妓伺恫けびわぶノハポ',
    'cover_img_url': '/pics/cover/2qh5_b.jpg',
    'release_date': '2013-06-28',
    'length': '125',
}

ABP_001_TAGS = [
    Tag('製作商:', 'ヒラハポーソ', '/studio/75'),
    Tag('發行商:', 'ZLYMBPWKBQJKHSKNW', '/label/xo'),
    Tag('系列:', '伺恫けびわぶノハポ', '/series/829'),
    Tag('genre', '桔窒宦', '/genre/44'),
    Tag('genre', '膈何', '/genre/45'),
    Tag('genre', '膿燈作瓰', '/genre/f'),
    Tag('star', '窄軅ヤーデ（髢誤ヤーデ）', '/star/82w'),
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
            'title': 'LLL LteLmmlyLpww 汹フミ',
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
            ('製作商:', '惶曰布艀', '/studio/bt'),
            ('發行商:', '惶曰布艀', '/label/17e'),
            ('genre', '斡瀠鰉', '/genre/4o'),
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
        soup.select_one('div.info a[href$="/series/829"]').string = 'ヒラハポーソ'
        _, tags = parse_item(str(soup))
        assert Tag('製作商:', 'ヒラハポーソ', '/studio/75') in tags
        assert Tag('系列:', 'ヒラハポーソ', '/series/829') in tags

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
    ('/SORA-655_2026-10-31', 'SORA-655_2026-10-31'),
    ('/259LUXU-1908', '259LUXU-1908'),
    ('/ABC-123_2026', 'ABC-123_2026'),
])
def test_fanhao_from_path(path, fanhao):
    assert fanhao_from_path(path) == fanhao


@pytest.mark.parametrize('text, valid', [
    ('SSIS-001', True),
    ('300MIUM-1506', True),
    ('FC2-PPV-123', True),
    ('HEYZO-1234', True),
    ('abp001', True),
    ('GEBB-062_2026-10-31', True),
    ('A' * 64, True),
    ('A' * 65, False),
    ('', False),
    ('/evil.example/x', False),
    ('../x', False),
    ('含 空格', False),
    ('ABC 001', False),
    ('-ABC-001', False),
    ('_ABC-001', False),
    ('ABC-001\n', False),
    ('ＡＢＣ-001', False),
    ('ABC-001?x=1', False),
    (None, False),
])
def test_is_valid_fanhao(text, valid):
    assert is_valid_fanhao(text) is valid


def movie_box(code, date=None, path=None):
    '''编造的列表页影片框：链接到详情页，里面两个 <date> 是識別碼和发行日期'''
    dates = f'<date>{code}</date> / <date>{date}</date>' if date else f'<date>{code}</date>'
    return (f'<div class="item"><a class="movie-box" href="https://www.javbus.com/{path or code}">'
            f'<div class="photo-info"><span>示例影片 {code}<br>{dates}</span></div></a></div>')


class TestListSourcePages:
    '''演员页（和首页同一种列表）：上方写着"已有磁力 N""全部影片 M"，影片框里有識別碼和发行日期'''

    @pytest.mark.parametrize('html, counts', [
        ('<div><a id="resultshowmag">已有磁力 95</a> / <a id="resultshowall">全部影片 120</a></div>', (95, 120)),
        ('<span>全部影片</span> <span class="badge">1,234</span><span>已有磁力</span><span>(382)</span>', (382, 1234)),
        ('<p>已有磁力：7 全部影片：9</p>', (7, 9)),
        ('<p>已有磁力 0 / 全部影片 3</p>', (0, 3)),
        ('<ul><li><a>全部影片</a></li><li><a>已有磁力</a></li></ul><p>示例 12</p>', (None, None)),
        ('<div id="waterfall"></div>', (None, None)),
    ])
    def test_counts_on_the_page(self, html, counts):
        assert parse_list_counts(html) == counts

    def test_movies_with_their_release_dates_newest_first_as_on_the_page(self):
        html = ('<div id="waterfall"><div class="item"><div class="avatar-box">演员甲</div></div>'
                + movie_box('ABC-003', '2026-03-01') + movie_box('ABC-002', '2026-02-01')
                + movie_box('ABC-002', '2026-02-01', path='ABC-002_2026-02-01') + movie_box('ABC-001')
                + movie_box('ABC-003', '2026-03-01') + '<a class="movie-box" href="/forum/x">号汇</a></div>')
        assert parse_list_entries(html) == [('/ABC-003', '2026-03-01'), ('/ABC-002', '2026-02-01'),
                                            ('/ABC-002_2026-02-01', '2026-02-01'), ('/ABC-001', None)]

    @pytest.mark.parametrize('path, star', [
        ('/star/a1', True),
        ('/uncensored/star/x9z', True),
        ('/star/a1/2', False),
        ('/star/演员甲', False),
        ('/star/', False),
        ('/genre/a1', False),
        ('/av-actress/jia/', False),
        ('/star/a1?x=1', False),
        (None, False),
    ])
    def test_which_paths_are_actress_pages(self, path, star):
        assert is_star_page(path) is star

    @pytest.mark.parametrize('page, path', [(1, '/star/a1'), (2, '/star/a1/2'), (40, '/star/a1/40')])
    def test_pages_of_a_list(self, page, path):
        assert list_page_path('/star/a1', page) == path


def actress_box(path, name, title=None, span=True):
    '''编造的"按名字找演员"结果里的一个演员框'''
    info = f'<div class="photo-info"><span>{name}</span></div>' if span else ''
    return (f'<div class="item"><a class="avatar-box text-center" href="https://www.javbus.com{path}">'
            f'<div class="photo-frame"><img src="/pics/actress/x.jpg" title="{title or name}"></div>{info}</a></div>')


class TestActressSearch:
    '''按名字找演员（/searchstar/<名字>）：结果是一个个演员框，链接是她的演员页'''

    def test_each_actress_with_her_page(self):
        html = ('<div id="waterfall">' + actress_box('/star/a1', '演员甲') + actress_box('/star/a2', ' 演员甲乙 ')
                + actress_box('/uncensored/star/u3', '演员甲', span=False) + actress_box('/star/a1', '演员甲')
                + '<a class="avatar-box" href="/genre/1">类别</a></div>')
        assert parse_star_search(html) == [('/star/a1', '演员甲'), ('/star/a2', '演员甲乙'), ('/uncensored/star/u3', '演员甲')]

    def test_no_result(self):
        assert parse_star_search('<div id="waterfall"></div><h4>沒有您要的結果！</h4>') == []

    def test_boxes_without_the_usual_class_inside_the_list(self):
        html = '<div id="waterfall"><div class="item"><a href="https://www.javbus.com/star/a1"><span>演员甲</span></a></div></div>'
        assert parse_star_search(html) == [('/star/a1', '演员甲')]

    @pytest.mark.parametrize('name, path', [
        ('演员甲', '/searchstar/%E6%BC%94%E5%91%98%E7%94%B2'),
        ('a b/c?d', '/searchstar/a%20b%2Fc%3Fd'),
    ])
    def test_the_address_of_a_search(self, name, path):
        assert searchstar_path(name) == path

    @pytest.mark.parametrize('inside', [
        '演员甲<button class="btn btn-xs btn-danger">角标</button>',
        '演员甲 <button class="btn btn-xs btn-danger">角标</button>',
        '\n  演员甲\n  <button class="btn btn-xs btn-danger">角标</button>\n',
        '演员甲&nbsp;<button class="btn btn-xs btn-danger">角标</button>',
        '<button class="btn btn-xs btn-danger">角标</button>演员甲',
        '演员甲<!-- 备注 --><button class="btn btn-xs btn-danger"><i>角</i>标</button>',
    ], ids=['glued', 'space', 'lines', 'nbsp', 'badge-first', 'comment-and-nested'])
    def test_the_badge_inside_the_name_span_is_not_part_of_the_name(self, inside):
        # 头像的 title 写成别的字：名字必须取自 span 自己的文字
        html = '<div id="waterfall">' + actress_box('/star/a1', inside, title='头像标题') + '</div>'
        assert parse_star_search(html) == [('/star/a1', '演员甲')]

    @pytest.mark.parametrize('inside', [
        '',
        '  \n ',
        '<button class="btn btn-xs btn-danger">角标</button>',
        ' <button class="btn btn-xs btn-danger">角标</button> ',
    ], ids=['empty', 'blank', 'badge-only', 'badge-and-blanks'])
    def test_the_avatar_title_is_the_name_when_the_span_has_no_text_of_its_own(self, inside):
        html = '<div id="waterfall">' + actress_box('/star/a1', inside, title=' 演员甲 ') + '</div>'
        assert parse_star_search(html) == [('/star/a1', '演员甲')]

    def test_a_box_whose_only_text_is_the_badge_has_no_name(self):
        box = ('<div class="item"><a class="avatar-box" href="https://www.javbus.com/star/a1">'
               '<div class="photo-info"><span><button>角标</button></span></div></a></div>')
        assert parse_star_search('<div id="waterfall">' + box + '</div>') == []

    def test_the_badge_in_boxes_without_the_usual_class(self):
        html = ('<div id="waterfall"><div class="item"><a href="https://www.javbus.com/star/a1">'
                '<span>演员甲<button>角标</button></span></a></div></div>')
        assert parse_star_search(html) == [('/star/a1', '演员甲')]
