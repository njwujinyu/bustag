'''
按演员补全抓进来的影片和补演员名放在一起：同一个番号的两部（一部带日期后缀）抓进来都没有演员时，
补演员名只为这个番号搜一次，两部用同一个搜索结果，补名不多发请求；两部被每轮的部数上限分开时，
界线外的那一部也用同一次搜索的结果，不另外搜
'''
import datetime

from bustag.spider import avwiki, backfill, db
from bustag.spider.bus_spider import DATED_SUFFIX
from bustag.spider.parser import avwiki_search_url, list_page_path

NOW = datetime.datetime(2026, 11, 6, 12, 0, 0)
STAR = '/star/a1'
CARDS = {'A-001': ('jia', '演员甲'), 'A-002': ('yi', '演员乙')}


def actress_url(slug):
    return f'https://av-wiki.net/av-actress/{slug}/'


def search_page(code, slug, name):
    return (f'<article class="archive-list"><ul><li><i class="fa fa-circle-o"></i> {code}</li>'
            f'<li class="actress-name"><a href="{actress_url(slug)}">{name}</a></li></ul></article>')


def actress_list(fanhaos):
    boxes = ''.join(f'<div class="item"><a class="movie-box" href="https://www.javbus.com/{fanhao}">'
                    f'<span>示例影片<br><date>{fanhao}</date> / <date>2026-01-01</date></span></a></div>'
                    for fanhao in fanhaos)
    return (f'<html><body><div class="alert">已有磁力 {len(fanhaos)} / 全部影片 {len(fanhaos)}</div>'
            f'<div id="waterfall"><div class="item"><div class="avatar-box">演员</div></div>{boxes}</div></body></html>')


def stars_of(fanhao):
    query = (db.ItemTag.select(db.Tag.value).join(db.Tag)
             .where((db.ItemTag.item == fanhao) & (db.Tag.type_ == 'star')).order_by(db.ItemTag.id))
    return [value for (value,) in query.tuples()]


def test_two_movies_of_one_code_fetched_without_actresses_share_one_name_search(fake_fetcher, detail_page, monkeypatch):
    monkeypatch.setattr(backfill, 'get_now_time', lambda: NOW)
    monkeypatch.setattr(avwiki, 'get_now_time', lambda: NOW)
    monkeypatch.setattr(avwiki, 'last_round', None)

    def javbus(path):
        return detail_page(DATED_SUFFIX.sub('', path.strip('/')))

    routes = {list_page_path(STAR, 1): actress_list(['A-002', 'A-001_2026-01-02', 'A-001'])}
    backfill.request_fill(STAR)
    backfill.run(fetcher=fake_fetcher(routes, default=javbus))
    assert sorted(item.fanhao for item in db.Item.select()) == ['A-001', 'A-001_2026-01-02', 'A-002']
    assert all(stars_of(fanhao) == [] for fanhao in ('A-001', 'A-001_2026-01-02', 'A-002'))

    avwiki_site = {}
    for code, (slug, name) in CARDS.items():
        avwiki_site[avwiki_search_url(code)] = search_page(code, slug, name)
        avwiki_site[actress_url(slug)] = '<div class="actress-data"><dl><dt>AV女優名</dt><dd>' + name + '</dd></dl></div>'
    fetcher = fake_fetcher(avwiki_site)
    result = avwiki.fill_round(fetcher=fetcher)

    # 每个番号搜一次（带日期的那部不另外搜），没有喜欢的影片，女优页一页也不读
    assert sorted(fetcher.requests) == sorted([avwiki_search_url('A-001'), avwiki_search_url('A-002')])
    assert (result.due, result.filled, result.requests) == (3, 3, 2)
    assert stars_of('A-001') == stars_of('A-001_2026-01-02') == ['演员甲']
    assert stars_of('A-002') == ['演员乙']


def test_the_movie_of_a_code_that_the_round_limit_left_out_takes_the_search_of_its_twin(
        fake_fetcher, detail_page, monkeypatch):
    # 补名先处理后入库的：列表上排在最前面的最先入库，正好被每轮的部数上限挡在界线外，
    # 它的同番号的另一部（紧跟在它后面）在界线里：界线外的这一部不另外搜，用同一次搜索的结果
    monkeypatch.setattr(backfill, 'get_now_time', lambda: NOW)
    monkeypatch.setattr(avwiki, 'get_now_time', lambda: NOW)
    monkeypatch.setattr(avwiki, 'last_round', None)
    others = [f'B-{n:03d}' for n in range(1, avwiki.ROUND_LIMIT)]
    codes = ['A-001'] + others

    def javbus(path):
        return detail_page(DATED_SUFFIX.sub('', path.strip('/')))

    routes = {list_page_path(STAR, 1): actress_list(['A-001_2026-01-02'] + codes)}
    backfill.request_fill(STAR)
    backfill.run(fetcher=fake_fetcher(routes, default=javbus))
    assert db.Item.select().count() == avwiki.ROUND_LIMIT + 1
    assert all(stars_of(fanhao) == [] for fanhao in ['A-001_2026-01-02'] + codes)

    avwiki_site = {avwiki_search_url(code): search_page(code, f'x{n}', f'演员{n}') for n, code in enumerate(codes)}
    fetcher = fake_fetcher(avwiki_site)
    result = avwiki.fill_round(fetcher=fetcher)

    # 一个番号搜一次：界线里的 30 个番号各一次，界线外带日期的那一部不再搜
    assert sorted(fetcher.requests) == sorted(avwiki_search_url(code) for code in codes)
    assert (result.due, result.filled, result.left, result.requests) == (
        avwiki.ROUND_LIMIT + 1, avwiki.ROUND_LIMIT + 1, 0, avwiki.ROUND_LIMIT)
    assert stars_of('A-001_2026-01-02') == stars_of('A-001') == ['演员0']
    assert stars_of('B-001') == ['演员1']
