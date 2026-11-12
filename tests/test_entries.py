'''同一番号多条详情：列出候选，改选后打标留在选定的那条。'''
import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from bustag.spider import avwiki, bus_spider, db
from bustag.spider.bus_spider import search_path
from bustag.spider.db import RATE_TYPE, RATE_VALUE, Item, ItemCandidate, ItemRate, LocalItem, get_items
from bustag.spider.fetcher import FetchError
from bustag.spider.parser import Tag, parse_code_entries


def notes_after(wsgi, response):
    '''跟着改选之后的跳转打开页面，返回页面上的提示：(成功的, 出错的)'''
    assert response.status == 303
    parts = urlsplit(response.headers['location'])
    soup = BeautifulSoup(wsgi.get(f'{parts.path}?{parts.query}').text, 'html.parser')
    return tuple([' '.join(box.get_text(' ', strip=True).split()) for box in soup.select(f'div.alert-{kind}')]
                 for kind in ('success', 'danger'))


def test_lists_only_the_same_code(page):
    entries = parse_code_entries(page('same-code-entries.html'), 'abc-001')
    assert entries == [
        {'path': '/ABC-001_2026-01-02', 'release_date': '2026-01-02'},
        {'path': '/ABC-001', 'release_date': '2026-01-01'},
    ]


def test_replace_moves_the_like_off_the_rejected_row(make_item):
    make_item('ABC-001', tags=[('star', '旧演员'), ('genre', '旧类别')])
    ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ABC-001')
    LocalItem.saveit('ABC-001', 'F:/kept.mp4')
    ItemCandidate.create(item='ABC-001', path='/ABC-001', release_date='2026-01-01')
    ItemCandidate.create(item='ABC-001', path='/ABC-001_2026-01-02', release_date='2026-01-02')
    db.replace_with_detail('ABC-001', {
        'fanhao': 'ABC-001_2026-01-02',
        'title': '作品 ABC-001_2026-01-02',
        'url': '/ABC-001_2026-01-02',
        'release_date': '2026-01-02',
        'cover_img_url': '/pics/cover/abc.jpg',
        'length': '90',
    }, [Tag('star', '新演员', '/star/new'), Tag('genre', '新类别', '/genre/new')])

    assert Item.get_by_fanhao('ABC-001') is None
    assert ItemRate.get_by_fanhao('ABC-001') is None
    item = Item.get_by_fanhao('ABC-001_2026-01-02')
    assert (item.url, item.release_date.isoformat()) == ('/ABC-001_2026-01-02', '2026-01-02')
    rate = ItemRate.get_by_fanhao('ABC-001_2026-01-02')
    assert (rate.rate_type, rate.rate_value) == (RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
    liked, _ = get_items(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, page=None)
    assert [row.fanhao for row in liked] == ['ABC-001_2026-01-02']
    Item.get_tags_dict(item)
    assert item.tags_dict['star'] == ['新演员']
    assert item.tags_dict['genre'] == ['新类别']
    assert LocalItem.get(LocalItem.item == item.fanhao).path == 'F:/kept.mp4'
    assert [row.path for row in db.other_entries(item.fanhao)] == ['/ABC-001']


def detail(fanhao, **changes):
    return {'fanhao': fanhao, 'title': f'作品 {fanhao}', 'url': '/' + fanhao, 'release_date': '2026-01-02',
            'cover_img_url': '/pics/cover/abc.jpg', 'length': '90', **changes}


def scored():
    return sorted(row.item_id for row in db.ItemScore.select())


def test_replace_forgets_the_score_computed_from_the_old_tags(make_item):
    for fanhao in ('ABC-001', 'ABC-001_2026-01-02', 'KEEP-001', 'SAME-001'):
        make_item(fanhao, tags=[('genre', '旧类别')])
    db.save_scores([db.Score(fanhao, 0.5, ['旧类别'], [])
                    for fanhao in ('SAME-001', 'KEEP-001', 'ABC-001_2026-01-02', 'ABC-001')], lambda total: 2)
    new_tags = [Tag('genre', '新类别', '/genre/new')]
    # 换成另一个番号的详情：两条的标签都变了；还是这个番号、换了详情：它的标签变了
    db.replace_with_detail('ABC-001', detail('ABC-001_2026-01-02'), new_tags)
    assert scored() == ['KEEP-001', 'SAME-001']
    db.replace_with_detail('SAME-001', detail('SAME-001', url='/SAME-001_2026-01-02'), new_tags)
    assert scored() == ['KEEP-001']


def test_replace_forgets_the_actress_lookups_of_the_old_tags(make_item):
    now = datetime.datetime(2026, 11, 6, 12, 0, 0)
    for fanhao in ('ABC-001', 'ABC-001_2026-01-02', 'KEEP-001', 'SAME-001'):
        make_item(fanhao)
        db.save_lookup(fanhao, db.LOOKUP_MISSING, 1, now, now + datetime.timedelta(days=1), 'av-wiki 上没有')
    # 换了详情，演员要按新的详情重新看：新旧两个番号的记录都不作数了
    db.replace_with_detail('ABC-001', detail('ABC-001_2026-01-02'), [])
    db.replace_with_detail('SAME-001', detail('SAME-001', url='/SAME-001_2026-01-02'), [])
    assert sorted(row.item_id for row in db.ActressLookup.select()) == ['KEEP-001']


def test_tag_page_lists_the_other_path(wsgi, make_item):
    make_item('ABC-001')
    ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ABC-001')
    ItemCandidate.create(item='ABC-001', path='/ABC-001_2026-01-02', release_date='2026-01-02')
    text = wsgi.get('/tagit?like=1').text
    assert '这个番号还有其他条目' in text
    assert '/ABC-001_2026-01-02' in text
    assert '改用这个详情' in text


def test_choosing_a_path_keeps_the_like_on_the_new_row(wsgi, make_item, page, monkeypatch, fake_fetcher,
                                                       fill_requests):
    make_item('ABC-001', tags=[('star', '旧演员')])
    ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ABC-001')
    html = page('detail-ABP-001-series-no-director.html').replace('ABP-001', 'ABC-001')
    fetcher = fake_fetcher({'/ABC-001_2026-01-02': html})
    monkeypatch.setattr(bus_spider, 'create_fetcher', lambda **kwargs: fetcher)
    response = wsgi.post('/entry/ABC-001', {
        'path': '/ABC-001_2026-01-02', 'use': '1', 'back': 'tagit', 'page': '1', 'like': '1',
    })
    parts = urlsplit(response.headers['location'])
    assert parts.path == '/tagit' and {'page': ['1'], 'like': ['1']}.items() <= parse_qs(parts.query).items()
    assert notes_after(wsgi, response) == (
        ['已改到所选详情。原来的喜欢或不喜欢留在这一条上，另一条不再占着这个打标。'], [])
    # 提示只出现一次：再打开同一个地址就没有了
    assert notes_after(wsgi, response) == ([], [])
    assert Item.get_by_fanhao('ABC-001') is None
    item = Item.get_by_fanhao('ABC-001_2026-01-02')
    assert item.url == '/ABC-001_2026-01-02'
    assert ItemRate.get_by_fanhao('ABC-001_2026-01-02').rate_value == RATE_VALUE.LIKE
    assert ItemRate.get_by_fanhao('ABC-001') is None
    Item.get_tags_dict(item)
    assert '旧演员' not in item.tags_dict['star']
    assert item.tags_dict['star']
    # 换了详情，演员要不要补由后台的那一轮按新的详情来看：这里只登记，请求只发了抓详情的那一次
    assert fill_requests == ['fill']
    assert fetcher.requests == ['/ABC-001_2026-01-02']


def test_a_failed_fetch_leaves_the_like_where_it_was(wsgi, make_item, monkeypatch, fake_fetcher, fill_requests):
    make_item('ABC-001')
    ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ABC-001')
    fetcher = fake_fetcher({'/ABC-001_2026-01-02': FetchError('没有这个页面')})
    monkeypatch.setattr(bus_spider, 'create_fetcher', lambda **kwargs: fetcher)
    response = wsgi.post('/entry/ABC-001', {'path': '/ABC-001_2026-01-02', 'use': '1', 'back': 'tagit'})
    assert notes_after(wsgi, response) == ([], ['没有改成：这一条的详情没有抓到'])
    assert Item.get_by_fanhao('ABC-001') is not None
    assert Item.get_by_fanhao('ABC-001_2026-01-02') is None
    assert ItemRate.get_by_fanhao('ABC-001').rate_value == RATE_VALUE.LIKE
    assert fill_requests == []


def test_a_different_code_is_rejected_without_a_request(wsgi, make_item, monkeypatch):
    make_item('ABC-001')
    ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ABC-001')
    called = []
    monkeypatch.setattr(bus_spider, 'create_fetcher', lambda **kwargs: called.append('fetch'))
    response = wsgi.post('/entry/ABC-001', {'path': '/OTHER-001', 'use': '1'})
    assert notes_after(wsgi, response) == ([], ['路径不是这个番号的详情。请贴 /番号 或 /番号_日期'])
    assert called == []
    assert ItemRate.get_by_fanhao('ABC-001').rate_value == RATE_VALUE.LIKE


def test_find_stores_the_other_path(wsgi, make_item, page, monkeypatch, fake_fetcher):
    make_item('ABC-001')
    fetcher = fake_fetcher({search_path('ABC-001'): page('same-code-entries.html')})
    monkeypatch.setattr(bus_spider, 'create_fetcher', lambda **kwargs: fetcher)
    response = wsgi.post('/entry/ABC-001', {'find': '1', 'back': 'tagit', 'page': '1'})
    assert notes_after(wsgi, response) == (['已记下这个番号的其他条目，可以改选。'], [])
    assert [row.path for row in db.other_entries('ABC-001')] == ['/ABC-001_2026-01-02']


def test_finding_no_other_path_says_so(wsgi, make_item, monkeypatch, fake_fetcher):
    make_item('ABC-001')
    fetcher = fake_fetcher({search_path('ABC-001'): '<div id="waterfall"></div>'})
    monkeypatch.setattr(bus_spider, 'create_fetcher', lambda **kwargs: fetcher)
    response = wsgi.post('/entry/ABC-001', {'find': '1', 'back': 'tagit', 'page': '1'})
    assert notes_after(wsgi, response) == ([], ['没有找到这个番号的其他条目'])


def test_the_local_page_shows_the_note_too(wsgi, make_item, monkeypatch):
    make_item('ABC-001')
    LocalItem.saveit('ABC-001', 'http://media.local/ABC-001.mp4')
    monkeypatch.setattr(bus_spider, 'create_fetcher', lambda **kwargs: pytest.fail('不该发请求'))
    response = wsgi.post('/entry/ABC-001', {'path': '/ABC-001', 'use': '1', 'back': 'local', 'page': '1'})
    parts = urlsplit(response.headers['location'])
    assert parts.path == '/local' and parse_qs(parts.query)['page'] == ['1']
    assert notes_after(wsgi, response) == ([], ['这已经是当前详情，没有改动'])
    assert notes_after(wsgi, response) == ([], [])


@pytest.mark.parametrize('path', ['/tagit', '/local'])
@pytest.mark.parametrize('note', ['replaced', 'found', 'same', 'badpath', 'fetch', 'empty', '<b>x</b>', ''])
def test_nothing_written_into_the_address_can_make_up_a_note(wsgi, make_item, path, note):
    make_item('ABC-001')
    LocalItem.saveit('ABC-001', 'http://media.local/ABC-001.mp4')
    text = wsgi.get(f'{path}?note={note}').text
    assert 'class="alert' not in text and '<b>x</b>' not in text


def test_liking_a_movie_without_stars_registers_it_for_actress_names(wsgi, make_item, fill_requests):
    make_item('ABC-001')
    wsgi.post('/tag/ABC-001', {'submit': '1'})
    assert fill_requests == ['fill']


def test_liking_a_movie_that_has_stars_registers_nothing(wsgi, make_item, fill_requests):
    make_item('ABC-001', tags=[('star', '演员甲')])
    wsgi.post('/tag/ABC-001', {'submit': '1'})
    assert fill_requests == []


def test_disliking_registers_nothing(wsgi, make_item, fill_requests):
    make_item('ABC-001')
    wsgi.post('/tag/ABC-001', {'submit': '0'})
    assert fill_requests == []


def test_tagging_never_looks_up_names_inside_the_request(wsgi, make_item, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('网页请求里不该去 av-wiki 补演员名')

    monkeypatch.setattr(avwiki, 'fill_round', forbidden)
    monkeypatch.setattr(avwiki, 'create_fetcher', forbidden)
    make_item('ABC-001')
    assert wsgi.post('/tag/ABC-001', {'submit': '1'}).status == 303
    assert ItemRate.get_by_fanhao('ABC-001').rate_value == RATE_VALUE.LIKE


@pytest.mark.parametrize('raw', ['ABC-001', ' https://www.javbus.com/ABC-001/ '])
def test_pasting_the_current_path_does_not_fetch(wsgi, make_item, monkeypatch, raw):
    make_item('ABC-001')
    called = []
    monkeypatch.setattr(bus_spider, 'create_fetcher', lambda **kwargs: called.append('fetch'))
    response = wsgi.post('/entry/ABC-001', {'path': raw, 'use': '1'})
    assert notes_after(wsgi, response) == ([], ['这已经是当前详情，没有改动'])
    assert called == []
