'''
av-wiki：搜索结果里的女优链接、女优页红框里的名字，以及在后台一轮一轮地给"喜欢、没有演员"的影片补演员名。
一轮的测试用编造的页面和 FakeFetcher，不联网
'''
import datetime
import logging

import pytest

from bustag import util
from bustag.spider import actresses, avwiki, db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, Item, ItemRate, has_star
from bustag.spider.fetcher import NETWORK_HINT, BlockedError, FetchError, NotFoundError
from bustag.spider.parser import avwiki_search_url, parse_avwiki_cards, parse_avwiki_names, parse_avwiki_search

NAMES = ['なわび溷鹾', 'なわびれくな', 'uzccz czwyprt', '閧汢ぼねれ', 'なわびぼねれ']
ACTRESS = 'https://av-wiki.net/av-actress/p1/'


def test_search_card_keeps_only_the_matching_actress_link(page):
    assert parse_avwiki_search(page('avwiki-search-card.html'), 'ABC-001') == [ACTRESS]


def test_profile_keeps_the_name_line_and_the_alias_line(page):
    names = parse_avwiki_names(page('avwiki-actress-names.html'))
    assert names == NAMES
    assert '2002懑09桝03日' not in names
    assert not any(name.startswith('T159') for name in names)


def test_alias_label_without_the_long_form():
    html = '''<div class="actress-data"><dl>
    <dt>AV女優名</dt><dd>なわび溷鹾（なわびれくな）</dd>
    <dt>別名</dt><dd>閧汢ぼねれ</dd>
    </dl></div>'''
    assert parse_avwiki_names(html) == ['なわび溷鹾', 'なわびれくな', '閧汢ぼねれ']


def test_profile_without_the_name_field_is_rejected():
    html = '<div class="actress-data"><dl><dt>杢懑桝日</dt><dd>2002懑09桝03日</dd></dl></div>'
    with pytest.raises(Exception, match='AV女優名'):
        parse_avwiki_names(html)


NOW = datetime.datetime(2026, 11, 6, 12, 0, 0)
MINUTE = datetime.timedelta(minutes=1)
DAY = datetime.timedelta(days=1)


def actress_url(slug):
    return f'https://av-wiki.net/av-actress/{slug}/'


def search_page(code, *slugs):
    '''编造的搜索结果页：一张番号是 code 的卡片，列出这几位女优的女优页'''
    links = ''.join(f'<li class="actress-name"><a href="{actress_url(slug)}">{slug}</a></li>' for slug in slugs)
    return f'<article class="archive-list"><ul><li><i class="fa fa-circle-o"></i> {code}</li>{links}</ul></article>'


def actress_page(name, alias=None):
    '''编造的女优页：红框里的名字和别名'''
    alias_row = f'<dt>別名義</dt><dd>{alias}</dd>' if alias else ''
    return f'<div class="actress-data"><dl><dt>AV女優名</dt><dd>{name}</dd>{alias_row}</dl></div>'


NO_RESULT = '<main><p>没有找到</p></main>'


def card_page(code, *actresses):
    '''编造的搜索结果页：一张番号是 code 的卡片，actresses 是 (slug, 卡片上她的名字)'''
    links = ''.join(f'<li class="actress-name"><a href="{actress_url(slug)}">{name}</a></li>' for slug, name in actresses)
    return (f'<article class="archive-list"><ul><li><i class="fa fa-circle-o"></i> {code}</li>{links}</ul></article>'
            f'<article class="archive-list"><ul><li><i class="fa fa-circle-o"></i> XYZ-999</li>'
            f'<li class="actress-name"><a href="{actress_url("other")}">别的影片的女优</a></li></ul></article>')


class TestCards:
    def test_each_actress_on_the_card_with_her_page_and_name(self):
        html = card_page('ABC-001', ('jia', '演员甲'), ('yi', '演员乙'))
        assert parse_avwiki_cards(html, 'abc-001') == [(actress_url('jia'), '演员甲'), (actress_url('yi'), '演员乙')]

    def test_the_name_is_the_text_of_the_link_with_blanks_tidied(self):
        html = card_page('ABC-001', ('jia', '<span> 演员\n甲 </span>'), ('yi', ''), ('bing', '   '))
        assert parse_avwiki_cards(html, 'ABC-001') == [(actress_url('jia'), '演员 甲'), (actress_url('yi'), ''),
                                                        (actress_url('bing'), '')]

    def test_a_page_listed_twice_on_the_card_is_kept_once(self):
        html = card_page('ABC-001', ('jia', '演员甲'), ('jia', '演员甲'))
        assert parse_avwiki_cards(html, 'ABC-001') == [(actress_url('jia'), '演员甲')]

    def test_no_card_for_the_code(self):
        assert parse_avwiki_cards(card_page('ABC-002', ('jia', '演员甲')), 'ABC-001') == []
        assert parse_avwiki_cards(NO_RESULT, 'ABC-001') == []

    def test_the_links_alone_are_the_same_as_before(self):
        html = card_page('ABC-001', ('jia', '演员甲'), ('yi', '演员乙'))
        assert parse_avwiki_search(html, 'ABC-001') == [actress_url('jia'), actress_url('yi')]


def site(*movies):
    '''
    假网站的全部页面。movies 是 (番号, [女优页的 slug, …])：每部影片一个搜索结果页，
    每位女优一个女优页，名字是"演员" + slug
    '''
    routes = {}
    for fanhao, slugs in movies:
        routes[avwiki_search_url(fanhao)] = search_page(fanhao, *slugs)
        for slug in slugs:
            routes[actress_url(slug)] = actress_page(f'演员{slug}')
    return routes


def like(make_item, fanhao, minutes_ago=0, stars=()):
    '''入库一部用户喜欢的影片。minutes_ago 是打标的时间离现在多久，补演员名时刚打标的排在前面'''
    make_item(fanhao, tags=[('genre', '标签一')] + [('star', name) for name in stars])
    ItemRate.create(item=fanhao, rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.LIKE,
                    rete_time=NOW - minutes_ago * MINUTE)


def stars_of(fanhao):
    '''影片挂着的演员名字，按挂上的先后'''
    query = (db.ItemTag.select(db.Tag.value).join(db.Tag)
             .where((db.ItemTag.item == fanhao) & (db.Tag.type_ == 'star')).order_by(db.ItemTag.id))
    return [value for (value,) in query.tuples()]


def lookup(fanhao):
    '''这部影片补演员名的记录：(结果, 连续几次没有, 多久以后再试, 原因)；没有记录时是 None'''
    row = db.ActressLookup.get_or_none(db.ActressLookup.item == fanhao)
    return None if row is None else (row.status, row.misses, row.next_try - row.checked_at, row.detail)


def counts(result):
    '''一轮的结果：(到期的, 补到的, 网站上没有的, 失败的, 留到下一轮的)'''
    return result.due, result.filled, result.missing, result.failed, result.left


@pytest.fixture
def clock(monkeypatch):
    '''补演员名看到的当前时间，测试里直接改 clock[0]'''
    now = [NOW]
    monkeypatch.setattr(avwiki, 'get_now_time', lambda: now[0])
    monkeypatch.setattr(actresses, 'get_now_time', lambda: now[0])
    return now


@pytest.fixture(autouse=True)
def no_round_yet(monkeypatch):
    monkeypatch.setattr(avwiki, 'last_round', None)


def liked_movies(make_item, *codes):
    '''几部用户喜欢、没有演员的影片，先写的先补（入库的先后反过来：同一档里新入库的在前）'''
    for fanhao in reversed(codes):
        like(make_item, fanhao)


def rated(make_item, fanhao, rate_type=None, value=None):
    '''入库一部没有演员的影片：rate_type 为 None 时没有评分'''
    make_item(fanhao, tags=[('genre', '标签一')])
    if rate_type is not None:
        ItemRate.create(item=fanhao, rate_type=rate_type, rate_value=value, rete_time=NOW)


def actress_of(slug):
    '''这位女优：(正式名字, [别名…], 资料读到了哪一步, JavBus 演员页)'''
    actress = db.Actress.get(db.Actress.avwiki == f'/av-actress/{slug}/')
    aliases = [row.name for row in db.ActressAlias.select().where(db.ActressAlias.actress == actress.id)
               .order_by(db.ActressAlias.id)]
    return db.Tag.get_by_id(actress.tag_id).value, aliases, actress.state, actress.javbus


@pytest.fixture(autouse=True)
def plenty_of_requests(monkeypatch):
    '''每天的请求上限：默认足够多，要撞上限的测试自己改'''
    monkeypatch.setitem(util.APP_CONFIG, 'download.avwiki_daily', '200')


class TestFillRound:
    def test_a_liked_movie_gets_her_name_from_her_page_and_the_others_as_aliases(self, make_item, fake_fetcher,
                                                                                 clock):
        like(make_item, 'ABC-001')
        fetcher = fake_fetcher({
            avwiki_search_url('ABC-001'): card_page('ABC-001', ('jia', '演员甲'), ('yi', '演员乙')),
            actress_url('jia'): actress_page('演员甲（えんいんこう） – Actor A', '演员甲别名'),
            actress_url('yi'): actress_page('演员乙'),
        })
        result = avwiki.fill_round(fetcher=fetcher)
        assert stars_of('ABC-001') == ['演员甲', '演员乙']
        item = Item.get_by_fanhao('ABC-001')
        Item.get_tags_dict(item)
        assert item.tags_dict['genre'] == ['标签一']
        assert actress_of('jia') == ('演员甲', ['えんいんこう', 'Actor A', '演员甲别名'], db.ACTRESS_PAGE, '')
        assert actress_of('yi') == ('演员乙', [], db.ACTRESS_PAGE, '')
        # 别名不是标签
        assert {tag.value: tag.url for tag in db.Tag.select().where(db.Tag.type_ == 'star')} == {
            '演员甲': '/av-actress/jia/', '演员乙': '/av-actress/yi/'}
        assert fetcher.requests == [avwiki_search_url('ABC-001'), actress_url('jia'), actress_url('yi')]
        assert counts(result) == (1, 1, 0, 0, 0)
        assert (result.names, result.pages, result.requests, result.unlabeled) == (2, 2, 3, 0)
        assert (result.stopped, result.error, result.finished_at) == ('', '', NOW)
        assert lookup('ABC-001') == ('filled', 0, 0 * DAY, '')
        assert not fetcher.closed

    def test_a_movie_nobody_likes_takes_the_names_on_the_card_with_one_request(self, make_item, fake_fetcher, clock):
        rated(make_item, 'ABC-001', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        rated(make_item, 'ABC-002')
        fetcher = fake_fetcher({
            avwiki_search_url('ABC-001'): card_page('ABC-001', ('jia', '演员甲'), ('yi', '演员乙')),
            avwiki_search_url('ABC-002'): card_page('ABC-002', ('jia', '演员甲')),
        })
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url('ABC-001'), avwiki_search_url('ABC-002')]
        assert stars_of('ABC-001') == ['演员甲', '演员乙'] and stars_of('ABC-002') == ['演员甲']
        assert actress_of('jia') == ('演员甲', [], db.ACTRESS_CARD, '')
        assert counts(result) == (2, 2, 0, 0, 0)
        assert (result.names, result.pages, result.requests, result.unlabeled) == (3, 0, 2, 2)

    def test_liked_first_then_recommended_then_the_rest_newest_first(self, make_item, fake_fetcher, clock):
        rated(make_item, 'U-001')
        rated(make_item, 'D-001', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        rated(make_item, 'R-001', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        rated(make_item, 'N-001', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)
        like(make_item, 'L-001')
        rated(make_item, 'U-002')
        like(make_item, 'L-002')
        rated(make_item, 'R-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        like(make_item, 'S-001', stars=['已有的演员'])
        fetcher = fake_fetcher(default=NO_RESULT)
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url(code) for code in (
            'L-002', 'L-001', 'R-002', 'R-001', 'U-002', 'N-001', 'D-001', 'U-001')]
        assert counts(result) == (8, 0, 8, 0, 0)
        assert stars_of('S-001') == ['已有的演员']

    def test_her_page_is_read_once_and_not_again_in_later_rounds(self, make_item, fake_fetcher, clock):
        like(make_item, 'ABC-001')
        fetcher = fake_fetcher(site(('ABC-001', ['jia']), ('ABC-002', ['jia']), ('ABC-003', ['jia'])))
        avwiki.fill_round(fetcher=fetcher)
        like(make_item, 'ABC-002')
        rated(make_item, 'ABC-003')
        clock[0] += MINUTE
        avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url('ABC-001'), actress_url('jia'), avwiki_search_url('ABC-002'),
                                    avwiki_search_url('ABC-003')]
        assert [stars_of(code) for code in ('ABC-001', 'ABC-002', 'ABC-003')] == [['演员jia']] * 3

    def test_a_chosen_actress_gets_her_page_read(self, make_item, fake_fetcher, clock):
        rated(make_item, 'ABC-001')
        fetcher = fake_fetcher(site(('ABC-001', ['jia'])))
        avwiki.fill_round(fetcher=fetcher)
        assert actress_of('jia')[:3] == ('jia', [], db.ACTRESS_CARD)
        clock[0] += MINUTE
        result = avwiki.fill_round(fetcher=fetcher, chosen=['JIA'])
        assert fetcher.requests == [avwiki_search_url('ABC-001'), actress_url('jia')]
        assert actress_of('jia')[:3] == ('演员jia', ['jia'], db.ACTRESS_PAGE)
        # 名字换了：她的影片跟着换
        assert stars_of('ABC-001') == ['演员jia']
        assert (result.pages, result.requests, result.unlabeled) == (1, 1, 1)
        assert not db.Tag.select().where(db.Tag.value == 'jia').exists()

    def test_a_card_without_her_name_falls_back_to_her_page(self, make_item, fake_fetcher, clock):
        rated(make_item, 'ABC-001')
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): card_page('ABC-001', ('jia', '')),
                                actress_url('jia'): actress_page('演员甲', 'えんいんこう')})
        assert counts(avwiki.fill_round(fetcher=fetcher)) == (1, 1, 0, 0, 0)
        assert fetcher.requests == [avwiki_search_url('ABC-001'), actress_url('jia')]
        assert actress_of('jia')[:3] == ('演员甲', ['えんいんこう'], db.ACTRESS_PAGE)

    def test_a_name_that_is_a_javbus_star_makes_her_that_star(self, make_item, fake_fetcher, clock, cast):
        cast.movie('C-001', ('演员甲', '/star/a1'))
        like(make_item, 'ABC-001')
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): card_page('ABC-001', ('jia', 'えんいんこう')),
                                actress_url('jia'): actress_page('えんいんこう', '演员甲')})
        avwiki.fill_round(fetcher=fetcher)
        assert actress_of('jia') == ('演员甲', ['えんいんこう', '演员甲'], db.ACTRESS_PAGE, '/star/a1')
        assert stars_of('ABC-001') == ['演员甲'] and cast.stars_of('C-001') == ['演员甲']
        assert db.Tag.get(db.Tag.value == '演员甲').url == '/star/a1'
        assert not db.Tag.select().where(db.Tag.value == 'えんいんこう').exists()

    def test_two_actresses_with_the_same_first_name_are_kept_apart(self, make_item, fake_fetcher, clock):
        rated(make_item, 'ABC-001')
        rated(make_item, 'ABC-002')
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): card_page('ABC-001', ('jia', '同名')),
                                avwiki_search_url('ABC-002'): card_page('ABC-002', ('yi', '同名'))})
        avwiki.fill_round(fetcher=fetcher)
        assert stars_of('ABC-002') == ['同名'] and stars_of('ABC-001') == ['同名（2）']

    def test_the_second_one_keeps_her_number_when_her_page_is_read(self, make_item, fake_fetcher, clock):
        rated(make_item, 'ABC-001')
        rated(make_item, 'ABC-002')
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): card_page('ABC-001', ('jia', '同名')),
                                avwiki_search_url('ABC-002'): card_page('ABC-002', ('yi', '同名')),
                                actress_url('jia'): actress_page('同名（どうめい）')})
        avwiki.fill_round(fetcher=fetcher)
        assert actress_of('jia')[:2] == ('同名（2）', [])
        clock[0] += MINUTE
        avwiki.fill_round(fetcher=fetcher, chosen=['同名（2）'])
        assert fetcher.requests[-1] == actress_url('jia')
        # 读了女优页还是"同名（2）"；为了和另一位分开编出来的名字不是她的别名
        assert actress_of('jia')[:3] == ('同名（2）', ['同名', 'どうめい'], db.ACTRESS_PAGE)
        assert stars_of('ABC-001') == ['同名（2）'] and stars_of('ABC-002') == ['同名']
        actresses.read_page('/av-actress/jia/', ['同名', 'どうめい'], clock[0])
        assert actress_of('jia')[:2] == ('同名（2）', ['同名', 'どうめい'])
        assert not db.Tag.select().where(db.Tag.value == '同名（3）').exists()

    def test_a_number_given_to_her_card_name_is_not_kept_when_her_page_starts_with_another_name(
            self, make_item, fake_fetcher, clock):
        rated(make_item, 'ABC-001')
        rated(make_item, 'ABC-002')
        avwiki.fill_round(fetcher=fake_fetcher({
            avwiki_search_url('ABC-001'): card_page('ABC-001', ('jia', '同名')),
            avwiki_search_url('ABC-002'): card_page('ABC-002', ('yi', '同名'))}))
        actresses.read_page('/av-actress/jia/', ['真名', '同名'], clock[0])
        assert actress_of('jia')[:2] == ('真名', ['同名'])
        assert stars_of('ABC-001') == ['真名']
        assert not db.Tag.select().where(db.Tag.value == '同名（2）').exists()

    def test_a_number_kept_as_an_alias_by_an_earlier_version_stays_when_her_page_is_read(self, cast, clock):
        # 以前的版本把她从"同名（2）"换成"同名（3）"时把旧的记成了别名，设定里可能存着它：读女优页以后照样认得她
        cast.actress('同名', 'jia')
        yi = cast.actress('同名（3）', 'yi', '同名', '同名（2）', state=db.ACTRESS_CARD)
        actresses.read_page('/av-actress/yi/', ['同名', 'えんいんおつ'], clock[0])
        assert actress_of('yi')[:2] == ('同名（3）', ['同名', 'えんいんおつ', '同名（2）'])
        person, = actresses.find_people(['同名（2）'])['同名（2）']
        assert (person.actress, person.name, person.search_name) == (yi.id, '同名（3）', '同名')

    def test_two_movies_with_the_same_code_share_one_search(self, make_item, fake_fetcher, clock):
        # 同一个識別碼的两条（一条带发行日期）：搜索地址一样，一轮里只请求一次，结果两条都用
        like(make_item, 'ABC-001')
        like(make_item, 'ABC-001_2026-01-01')
        fetcher = fake_fetcher(site(('ABC-001', ['jia'])))
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url('ABC-001'), actress_url('jia')]
        assert stars_of('ABC-001') == stars_of('ABC-001_2026-01-01') == ['演员jia']
        assert (result.filled, result.requests) == (2, 2)

    def test_a_movie_of_the_same_code_left_out_of_the_round_uses_its_search(self, make_item, fake_fetcher, clock,
                                                                           monkeypatch):
        # 一轮只排 2 部（新入库的在前：ABC-002、ABC-001），带日期的那条排不上：同一个识别码，这次的搜索结果它也用上，
        # 不占这一轮的名额，以后也不用再为它搜一次
        monkeypatch.setattr(avwiki, 'ROUND_LIMIT', 2)
        rated(make_item, 'ABC-001_2026-01-01')
        rated(make_item, 'ABC-001')
        rated(make_item, 'ABC-002')
        fetcher = fake_fetcher(site(('ABC-001', ['jia']), ('ABC-002', ['yi'])))
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url('ABC-002'), avwiki_search_url('ABC-001')]
        assert stars_of('ABC-001') == stars_of('ABC-001_2026-01-01') == ['jia']
        assert counts(result) == (3, 3, 0, 0, 0) and result.requests == 2
        assert lookup('ABC-001_2026-01-01') == ('filled', 0, 0 * DAY, '')
        clock[0] += MINUTE
        assert avwiki.fill_round(fetcher=fetcher).due == 0
        assert len(fetcher.requests) == 2

    def test_a_movie_of_the_same_code_left_out_of_the_round_shares_a_search_without_results(
            self, make_item, fake_fetcher, clock, monkeypatch):
        monkeypatch.setattr(avwiki, 'ROUND_LIMIT', 1)
        rated(make_item, 'ABC-001_2026-01-01')
        rated(make_item, 'ABC-001')
        fetcher = fake_fetcher(default=NO_RESULT)
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url('ABC-001')]
        assert (result.missing, result.left, result.requests) == (2, 0, 1)
        assert lookup('ABC-001')[:2] == lookup('ABC-001_2026-01-01')[:2] == ('missing', 1)

    def test_a_search_that_failed_is_not_shared_with_movies_left_out_of_the_round(self, make_item, fake_fetcher, clock,
                                                                                    monkeypatch):
        monkeypatch.setattr(avwiki, 'ROUND_LIMIT', 1)
        rated(make_item, 'ABC-001_2026-01-01')
        rated(make_item, 'ABC-001')
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): FetchError('网络连接失败')})
        result = avwiki.fill_round(fetcher=fetcher)
        assert (result.failed, result.left) == (1, 1)
        assert lookup('ABC-001_2026-01-01') is None

    def test_two_movies_with_the_same_code_share_a_search_without_results(self, make_item, fake_fetcher, clock):
        rated(make_item, 'ABC-001')
        rated(make_item, 'ABC-001_2026-01-01')
        fetcher = fake_fetcher(default=NO_RESULT)
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url('ABC-001')]
        assert (result.missing, result.requests) == (2, 1)
        assert lookup('ABC-001')[:2] == lookup('ABC-001_2026-01-01')[:2] == ('missing', 1)

    def test_a_movie_that_got_stars_meanwhile_gets_no_names(self, make_item, fake_fetcher, clock, cast):
        like(make_item, 'ABC-001')

        def search(url):
            cast.movie('C-001', ('演员丙', '/star/c1'))
            db.ItemTag.saveit(Item.get_by_fanhao('ABC-001'), db.Tag.get(db.Tag.value == '演员丙'))
            return card_page('ABC-001', ('jia', '演员甲'))

        result = avwiki.fill_round(fetcher=fake_fetcher({avwiki_search_url('ABC-001'): search}))
        assert stars_of('ABC-001') == ['演员丙']
        assert counts(result) == (1, 0, 0, 0, 0)
        assert lookup('ABC-001') is None

    def test_nothing_is_filled_before_the_actress_data_is_upgraded(self, make_item, fake_fetcher, clock, cast):
        like(make_item, 'ABC-001')
        cast.movie('OLD-001')
        cast.legacy('OLD-001', 'jia', '演员甲', 'えんいんこう')
        fetcher = fake_fetcher(site(('ABC-001', ['jia'])))
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [] and not has_star('ABC-001')
        assert result.stopped == '演员数据还没有升级（见启动时的日志），升级做完以后才补演员名'

    def test_a_movie_the_site_does_not_have_is_tried_again_after_a_day_a_week_then_every_month(
            self, make_item, fake_fetcher, clock):
        like(make_item, 'ABC-001')
        # 搜索结果里只有别的番号的卡片
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): search_page('XYZ-999', 'jia')})
        for misses, days in enumerate([1, 7, 30, 30], start=1):
            result = avwiki.fill_round(fetcher=fetcher)
            assert counts(result) == (1, 0, 1, 0, 0)
            assert lookup('ABC-001') == ('missing', misses, days * DAY, 'av-wiki 的搜索结果里没有这个番号')
            # 没到再试的时间：不算到期，不发请求
            clock[0] += days * DAY - MINUTE
            assert counts(avwiki.fill_round(fetcher=fetcher)) == (0, 0, 0, 0, 0)
            assert len(fetcher.requests) == misses
            clock[0] += MINUTE
        assert not has_star('ABC-001')

    def test_a_search_page_that_does_not_exist_means_the_site_does_not_have_the_movie(self, make_item, fake_fetcher,
                                                                                   clock):
        like(make_item, 'ABC-001')
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): NotFoundError('页面不存在（HTTP 404）')})
        assert counts(avwiki.fill_round(fetcher=fetcher)) == (1, 0, 1, 0, 0)
        assert lookup('ABC-001') == ('missing', 1, DAY, 'av-wiki 上没有这个番号的页面')

    def test_a_movie_found_later_starts_with_short_waits_again(self, make_item, fake_fetcher, clock):
        like(make_item, 'ABC-001')
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): NO_RESULT})
        avwiki.fill_round(fetcher=fetcher)
        clock[0] += DAY
        avwiki.fill_round(fetcher=fetcher)
        assert lookup('ABC-001')[:2] == ('missing', 2)
        clock[0] += 7 * DAY
        fetcher.routes.update(site(('ABC-001', ['jia'])))
        assert counts(avwiki.fill_round(fetcher=fetcher)) == (1, 1, 0, 0, 0)
        assert lookup('ABC-001') == ('filled', 0, 0 * DAY, '')
        assert stars_of('ABC-001') == ['演员jia']

    def test_a_network_failure_is_tried_again_in_the_next_round(self, make_item, fake_fetcher, clock, caplog):
        like(make_item, 'ABC-001')
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): FetchError('网络连接失败（ConnectTimeout）')})
        result = avwiki.fill_round(fetcher=fetcher)
        assert counts(result) == (1, 0, 0, 1, 0)
        assert result.error == 'ABC-001：网络连接失败（ConnectTimeout）'
        assert lookup('ABC-001') == ('failed', 0, 0 * DAY, '网络连接失败（ConnectTimeout）')
        assert 'ABC-001 补演员名失败：网络连接失败（ConnectTimeout）' in caplog.messages
        assert ItemRate.get_by_fanhao('ABC-001').rate_value == RATE_VALUE.LIKE
        # 下一轮（哪怕紧接着）就再试
        fetcher.routes.update(site(('ABC-001', ['jia'])))
        assert counts(avwiki.fill_round(fetcher=fetcher)) == (1, 1, 0, 0, 0)
        assert stars_of('ABC-001') == ['演员jia']

    def test_the_reason_leaves_out_the_advice_about_the_javbus_settings(self, make_item, fake_fetcher, clock):
        like(make_item, 'ABC-001')
        url = avwiki_search_url('ABC-001')
        # Fetcher 的报错末尾带着检查 root_path 的建议，那是给 JavBus 的；这里留下原因和地址就够了
        fetcher = fake_fetcher({url: FetchError(f'网络连接失败（ConnectTimeout: 超时）：{url}。{NETWORK_HINT}')})
        result = avwiki.fill_round(fetcher=fetcher)
        assert lookup('ABC-001')[3] == f'网络连接失败（ConnectTimeout: 超时）：{url}'
        assert result.error == f'ABC-001：网络连接失败（ConnectTimeout: 超时）：{url}'

    def test_a_failure_keeps_the_count_of_earlier_misses(self, make_item, fake_fetcher, clock):
        like(make_item, 'ABC-001')
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): NO_RESULT})
        avwiki.fill_round(fetcher=fetcher)
        clock[0] += DAY
        fetcher.routes[avwiki_search_url('ABC-001')] = FetchError('网络连接失败')
        avwiki.fill_round(fetcher=fetcher)
        assert lookup('ABC-001') == ('failed', 1, 0 * DAY, '网络连接失败')
        fetcher.routes[avwiki_search_url('ABC-001')] = NO_RESULT
        avwiki.fill_round(fetcher=fetcher)
        assert lookup('ABC-001')[:3] == ('missing', 2, 7 * DAY)

    def test_the_round_stops_after_three_failed_requests_in_a_row(self, make_item, fake_fetcher, clock, caplog):
        liked_movies(make_item, *[f'ABC-{n:03d}' for n in range(1, 6)])
        fetcher = fake_fetcher(default=FetchError('网络连接失败'))
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url(f'ABC-{n:03d}') for n in (1, 2, 3)]
        assert counts(result) == (5, 0, 0, 3, 2)
        assert result.stopped == '连续 3 次请求失败'
        assert [lookup(f'ABC-{n:03d}') is not None for n in range(1, 6)] == [True, True, True, False, False]
        assert '补演员名：连续 3 次请求失败，这一轮先停下，剩下的下一轮再试' in caplog.messages

    def test_a_request_that_gets_through_starts_the_count_again(self, make_item, fake_fetcher, clock):
        liked_movies(make_item, *[f'ABC-{n:03d}' for n in range(1, 7)])
        routes = {avwiki_search_url(f'ABC-{n:03d}'): FetchError('网络连接失败') for n in (1, 2, 4, 5)}
        routes.update({avwiki_search_url(f'ABC-{n:03d}'): NO_RESULT for n in (3, 6)})
        fetcher = fake_fetcher(routes)
        result = avwiki.fill_round(fetcher=fetcher)
        assert len(fetcher.requests) == 6
        assert counts(result) == (6, 0, 2, 4, 0)
        assert result.stopped == ''

    def test_a_round_handles_a_limited_number_and_leaves_the_rest_for_later_rounds(self, make_item, fake_fetcher,
                                                                                clock, monkeypatch):
        monkeypatch.setattr(avwiki, 'ROUND_LIMIT', 2)
        liked_movies(make_item, *[f'ABC-{n:03d}' for n in range(1, 6)])
        fetcher = fake_fetcher(default=NO_RESULT)
        assert counts(avwiki.fill_round(fetcher=fetcher)) == (5, 0, 2, 0, 3)
        assert counts(avwiki.fill_round(fetcher=fetcher)) == (3, 0, 2, 0, 1)
        assert counts(avwiki.fill_round(fetcher=fetcher)) == (1, 0, 1, 0, 0)
        assert counts(avwiki.fill_round(fetcher=fetcher)) == (0, 0, 0, 0, 0)
        assert fetcher.requests == [avwiki_search_url(f'ABC-{n:03d}') for n in range(1, 6)]

    def test_movies_that_keep_failing_do_not_hold_up_the_others(self, make_item, fake_fetcher, clock, monkeypatch):
        monkeypatch.setattr(avwiki, 'ROUND_LIMIT', 2)
        liked_movies(make_item, *[f'ABC-{n:03d}' for n in range(1, 5)])
        routes = site(('ABC-003', ['jia']), ('ABC-004', ['yi']))
        routes.update({avwiki_search_url(f'ABC-{n:03d}'): FetchError('HTTP 500') for n in (1, 2)})
        fetcher = fake_fetcher(routes)
        assert counts(avwiki.fill_round(fetcher=fetcher)) == (4, 0, 0, 2, 2)
        clock[0] += MINUTE
        # 上一轮失败的排到队尾，这一轮先做还没试过的
        assert counts(avwiki.fill_round(fetcher=fetcher)) == (4, 2, 0, 0, 2)
        assert stars_of('ABC-003') == ['演员jia'] and stars_of('ABC-004') == ['演员yi']

    def test_an_actress_page_is_read_once_in_a_round(self, make_item, fake_fetcher, clock):
        liked_movies(make_item, 'ABC-001', 'ABC-002', 'ABC-003')
        fetcher = fake_fetcher(site(('ABC-001', ['jia']), ('ABC-002', ['jia', 'yi']), ('ABC-003', ['yi'])))
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url('ABC-001'), actress_url('jia'), avwiki_search_url('ABC-002'),
                                    actress_url('yi'), avwiki_search_url('ABC-003')]
        assert counts(result) == (3, 3, 0, 0, 0)
        assert (result.names, result.pages) == (4, 2)
        assert stars_of('ABC-002') == ['演员jia', '演员yi']

    def test_a_page_that_cannot_be_read_does_not_hold_up_the_movie(self, make_item, fake_fetcher, clock):
        liked_movies(make_item, 'ABC-001', 'ABC-002')
        routes = site(('ABC-001', ['jia', 'yi']), ('ABC-002', ['yi']))
        routes[actress_url('yi')] = FetchError('网络连接失败')
        fetcher = fake_fetcher(routes)
        result = avwiki.fill_round(fetcher=fetcher)
        # 卡片上的名字已经够了：影片照样补上，她的女优页下一轮再读
        assert stars_of('ABC-001') == ['演员jia', 'yi'] and stars_of('ABC-002') == ['yi']
        assert counts(result) == (2, 2, 0, 0, 0)
        assert fetcher.requests == [avwiki_search_url('ABC-001'), actress_url('jia'), actress_url('yi'),
                                    avwiki_search_url('ABC-002')]
        assert actress_of('yi')[2] == db.ACTRESS_CARD
        fetcher.routes[actress_url('yi')] = actress_page('演员yi')
        clock[0] += MINUTE
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests[-1] == actress_url('yi') and result.pages == 1
        assert stars_of('ABC-001') == ['演员jia', '演员yi'] and stars_of('ABC-002') == ['演员yi']

    def test_every_request_to_an_actress_page_counts_as_a_page_read(self, make_item, fake_fetcher, clock):
        like(make_item, 'ABC-001')
        fetcher = fake_fetcher({
            avwiki_search_url('ABC-001'): card_page('ABC-001', ('jia', ''), ('yi', '演员乙'), ('bing', '演员丙'),
                                                    ('ding', '演员丁')),
            actress_url('jia'): actress_page('演员甲'),
            actress_url('yi'): NotFoundError('页面不存在（HTTP 404）'),
            actress_url('bing'): '<div class="actress-data"><dl><dt>杢懑桝日</dt><dd>2000年01月01日</dd></dl></div>',
            actress_url('ding'): FetchError('网络连接失败')})
        result = avwiki.fill_round(fetcher=fetcher)
        # 卡片上没有名字时读的、404 的、没有名字栏的、连不上的，都是发出去的女优页请求
        assert fetcher.requests == [avwiki_search_url('ABC-001')] + [actress_url(slug) for slug in
                                                                     ('jia', 'yi', 'bing', 'ding')]
        assert (result.pages, result.requests) == (4, 5)

    @pytest.mark.parametrize('reply', [
        '<div class="actress-data"><dl><dt>杢懑桝日</dt><dd>2000年01月01日</dd></dl></div>',
        NotFoundError('页面不存在（HTTP 404）'),
    ], ids=['no-name-box', 'page-gone'])
    def test_a_page_without_names_is_not_read_again(self, make_item, fake_fetcher, clock, reply):
        like(make_item, 'ABC-001')
        routes = site(('ABC-001', ['jia']))
        routes[actress_url('jia')] = reply
        fetcher = fake_fetcher(routes)
        assert counts(avwiki.fill_round(fetcher=fetcher)) == (1, 1, 0, 0, 0)
        assert stars_of('ABC-001') == ['jia']
        clock[0] += MINUTE
        avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url('ABC-001'), actress_url('jia')]

    @pytest.mark.parametrize('reply, reason', [
        ('<div class="actress-data"><dl><dt>杢懑桝日</dt><dd>2000年01月01日</dd></dl></div>', '女优页没有 AV女優名'),
        (NotFoundError('页面不存在（HTTP 404）'), '页面不存在（HTTP 404）'),
    ], ids=['no-name-box', 'page-gone'])
    def test_no_name_on_the_card_and_none_on_her_page_means_the_site_does_not_have_them(
            self, make_item, fake_fetcher, clock, reply, reason):
        like(make_item, 'ABC-001')
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): card_page('ABC-001', ('jia', '演员甲'), ('yi', '')),
                                actress_url('jia'): actress_page('演员甲'), actress_url('yi'): reply})
        result = avwiki.fill_round(fetcher=fetcher)
        assert counts(result) == (1, 0, 1, 0, 0)
        assert lookup('ABC-001') == ('missing', 1, DAY, f'女优页没有读到名字：{reason}')
        assert not has_star('ABC-001')

    def test_the_reason_leaves_out_the_name_in_the_address_of_an_actress_page(self, make_item, fake_fetcher, clock,
                                                                              caplog):
        # 女优页的地址是 /av-actress/<名字的罗马字>/：原因要写进日志、库里和关于页，只留到 /av-actress/
        caplog.set_level(logging.DEBUG, logger='bustag')
        liked_movies(make_item, 'ABC-001', 'ABC-002')
        unreachable, gone = actress_url('mingzi-jia'), actress_url('mingzi-yi')
        routes = {avwiki_search_url('ABC-001'): card_page('ABC-001', ('mingzi-jia', '')),
                  avwiki_search_url('ABC-002'): card_page('ABC-002', ('mingzi-yi', '')),
                  unreachable: FetchError('网络连接失败（ConnectionError: Max retries exceeded with url: '
                                          f'/av-actress/mingzi-jia/ (Caused by …)）：{unreachable}。{NETWORK_HINT}'),
                  gone: NotFoundError(f'页面不存在（HTTP 404）：{gone}')}
        result = avwiki.fill_round(fetcher=fake_fetcher(routes))
        shown = 'https://av-wiki.net/av-actress/…/'
        assert counts(result) == (2, 0, 1, 1, 0)
        assert lookup('ABC-001')[3] == ('网络连接失败（ConnectionError: Max retries exceeded with url: '
                                        f'/av-actress/…/ (Caused by …)）：{shown}')
        assert lookup('ABC-002')[3] == f'女优页没有读到名字：页面不存在（HTTP 404）：{shown}'
        assert result.error == 'ABC-001：' + lookup('ABC-001')[3]
        assert f'ABC-001 补演员名失败：{lookup("ABC-001")[3]}' in caplog.messages
        assert 'mingzi' not in caplog.text

    def test_being_blocked_stops_the_round_at_once(self, make_item, fake_fetcher, clock, caplog):
        liked_movies(make_item, 'ABC-001', 'ABC-002', 'ABC-003')
        fetcher = fake_fetcher(default=BlockedError('HTTP 429：请求太频繁，被网站限流'))
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url('ABC-001')]
        assert counts(result) == (3, 0, 0, 1, 2)
        assert result.stopped == '被 av-wiki 拦截'
        assert lookup('ABC-001') == ('failed', 0, 0 * DAY, 'HTTP 429：请求太频繁，被网站限流')
        assert '补演员名：被 av-wiki 拦截，这一轮先停下，剩下的下一轮再试' in caplog.messages

    def test_an_unexpected_error_only_fails_that_movie(self, make_item, fake_fetcher, clock, caplog):
        liked_movies(make_item, 'ABC-001', 'ABC-002')

        def broken(url):
            raise RuntimeError('意料之外的错')

        routes = site(('ABC-002', ['jia']))
        routes[avwiki_search_url('ABC-001')] = broken
        result = avwiki.fill_round(fetcher=fake_fetcher(routes))
        assert counts(result) == (2, 1, 0, 1, 0)
        assert lookup('ABC-001') == ('failed', 0, 0 * DAY, "程序出错：RuntimeError('意料之外的错')")
        assert stars_of('ABC-002') == ['演员jia']
        record = next(r for r in caplog.records if r.getMessage() == 'ABC-001 补演员名时程序出错')
        assert record.exc_info is not None

    def test_an_unexpected_error_is_recorded_without_credentials(self, make_item, fake_fetcher, clock):
        like(make_item, 'ABC-001')

        def broken(url):
            raise RuntimeError('Failed to parse: http://envuser:EnvSecret9@127.0.0.1:99999')

        avwiki.fill_round(fetcher=fake_fetcher({avwiki_search_url('ABC-001'): broken}))
        assert lookup('ABC-001')[3] == "程序出错：RuntimeError('Failed to parse: http://envuser:***@127.0.0.1:99999')"

    def test_its_own_fetcher_tries_each_request_once_and_is_closed(self, make_item, fake_fetcher, clock, monkeypatch):
        like(make_item, 'ABC-001')
        fetcher = fake_fetcher(site(('ABC-001', ['jia'])))
        created = []

        def create_fetcher(**overrides):
            created.append(overrides)
            return fetcher

        monkeypatch.setattr(avwiki, 'create_fetcher', create_fetcher)
        assert counts(avwiki.fill_round()) == (1, 1, 0, 0, 0)
        assert created == [{'retries': 0, 'timeout': (10, 20)}]
        assert fetcher.closed
        # 没有到期的影片时连 Fetcher 都不建
        avwiki.fill_round()
        assert len(created) == 1

    def test_the_round_is_written_to_the_log_in_chinese_without_names(self, make_item, fake_fetcher, clock, caplog):
        caplog.set_level(logging.DEBUG, logger='bustag')

        def written():
            return [record.getMessage() for record in caplog.records if record.module in ('avwiki', 'actresses')]

        avwiki.fill_round(fetcher=fake_fetcher())
        assert written() == ['补演员名：没有到期要补的影片']
        caplog.clear()
        liked_movies(make_item, 'ABC-001', 'ABC-002')
        routes = site(('ABC-001', ['jia']))
        routes[avwiki_search_url('ABC-002')] = NO_RESULT
        avwiki.fill_round(fetcher=fake_fetcher(routes))
        number = db.Actress.get().id
        assert written() == [
            '开始补演员名：要补的有 2 部，这一轮处理 2 部',
            f'新的女优 #{number}（只有搜索卡片上的名字）',
            f'女优 #{number} 的女优页读到了 1 个名字，正式名字换了，改了 1 部影片的演员',
            'ABC-001 从 av-wiki 补了 1 位女优',
            'ABC-002 在 av-wiki 上没有找到演员（av-wiki 的搜索结果里没有这个番号），1 天后再试',
            '补演员名结束：补到 1 部，av-wiki 上没有 1 部，失败 0 部；读了 1 个女优页，这一轮请求 3 次（今天一共 3 次）',
        ]
        assert 'jia' not in caplog.text


# 一个女优页地址的两种写法：网站链接里按 UTF-8 转义的和解码的（编造的「えんいん」）
PLAIN_SLUG = 'えんいん'
ESCAPED_SLUG = '%E3%81%88%E3%82%93%E3%81%84%E3%82%93'


class TestOnePageWrittenTwoWays:
    '''同一个女优页的地址，转义了的和没转义的（十六进制大写、小写）都是这一页：认成同一个人，一轮里只读一次'''

    @pytest.mark.parametrize('stored, on_card', [(ESCAPED_SLUG, PLAIN_SLUG), (PLAIN_SLUG, ESCAPED_SLUG),
                                                 (ESCAPED_SLUG.lower(), ESCAPED_SLUG)])
    def test_the_card_finds_her_whichever_way_her_address_is_written(self, make_item, fake_fetcher, clock, cast,
                                                                     stored, on_card):
        jia = cast.actress('演员甲', stored, 'えんいんこう')
        db.Actress.update(names_at=NOW - DAY).where(db.Actress.id == jia.id).execute()
        like(make_item, 'ABC-001')
        fetcher = fake_fetcher({avwiki_search_url('ABC-001'): card_page('ABC-001', (on_card, '演员甲'))})
        result = avwiki.fill_round(fetcher=fetcher)
        # 认出是她：不另建一位，她的女优页读过了也不再读
        assert fetcher.requests == [avwiki_search_url('ABC-001')]
        assert stars_of('ABC-001') == ['演员甲']
        assert [actress.avwiki for actress in db.Actress.select()] == [f'/av-actress/{stored}/']
        assert (result.filled, result.pages) == (1, 0)

    def test_a_new_actress_is_kept_with_her_address_escaped(self, make_item, fake_fetcher, clock):
        rated(make_item, 'ABC-001')
        avwiki.fill_round(fetcher=fake_fetcher({
            avwiki_search_url('ABC-001'): card_page('ABC-001', (PLAIN_SLUG, '演员甲'))}))
        actress = db.Actress.get()
        assert actress.avwiki == f'/av-actress/{ESCAPED_SLUG}/'
        assert db.Tag.get_by_id(actress.tag_id).url == f'/av-actress/{ESCAPED_SLUG}/'

    def test_a_page_given_two_ways_in_one_round_is_one_actress_read_once(self, make_item, fake_fetcher, clock):
        liked_movies(make_item, 'ABC-001', 'ABC-002')
        fetcher = fake_fetcher({
            avwiki_search_url('ABC-001'): card_page('ABC-001', (ESCAPED_SLUG, '演员甲')),
            avwiki_search_url('ABC-002'): card_page('ABC-002', (PLAIN_SLUG, '演员甲')),
            actress_url(ESCAPED_SLUG): actress_page('演员甲', 'えんいんこう'),
            actress_url(PLAIN_SLUG): actress_page('演员甲', 'えんいんこう')})
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url('ABC-001'), actress_url(ESCAPED_SLUG), avwiki_search_url('ABC-002')]
        assert db.Actress.select().count() == 1
        assert stars_of('ABC-001') == stars_of('ABC-002') == ['演员甲']
        assert (result.filled, result.pages) == (2, 1)

    def test_two_rows_left_for_one_page_are_both_read_with_one_request(self, make_item, fake_fetcher, clock, cast):
        # 以前的版本按两种写法各建了一位，两行都留着：这一页一轮里只请求一次，两行都记下读过了
        first = cast.actress('演员甲', ESCAPED_SLUG, state=db.ACTRESS_LEGACY)
        second = cast.actress('演员甲（2）', PLAIN_SLUG, state=db.ACTRESS_LEGACY)
        fetcher = fake_fetcher({actress_url(ESCAPED_SLUG): actress_page('演员甲', 'えんいんこう'),
                                actress_url(PLAIN_SLUG): actress_page('演员甲', 'えんいんこう')})
        result = avwiki.fill_round(fetcher=fetcher)
        assert len(fetcher.requests) == 1 and result.pages == 1
        assert [db.Actress.get_by_id(actress.id).names_at for actress in (first, second)] == [NOW, NOW]
        assert actresses.pages_to_read() == []


class TestDailyLimit:
    def test_the_round_stops_at_the_daily_limit_and_goes_on_the_next_day(self, make_item, fake_fetcher, clock,
                                                                         monkeypatch, caplog):
        monkeypatch.setitem(util.APP_CONFIG, 'download.avwiki_daily', '3')
        liked_movies(make_item, 'ABC-001', 'ABC-002', 'ABC-003')
        fetcher = fake_fetcher(site(('ABC-001', ['jia']), ('ABC-002', ['yi']), ('ABC-003', ['bing'])))
        result = avwiki.fill_round(fetcher=fetcher)
        # 搜索、女优页都算请求：ABC-001 两次，ABC-002 的搜索一次，读她的女优页时到了上限
        assert fetcher.requests == [avwiki_search_url('ABC-001'), actress_url('jia'), avwiki_search_url('ABC-002')]
        assert counts(result) == (3, 2, 0, 0, 1)
        assert result.stopped == '今天向 av-wiki 的请求到了每天 3 次的上限'
        assert '补演员名：今天向 av-wiki 的请求到了每天 3 次的上限，剩下的明天接着补' in caplog.messages
        assert avwiki.daily_used() == 3
        assert stars_of('ABC-002') == ['yi']
        # 当天再来一轮：一个请求都不发
        clock[0] += MINUTE
        assert counts(avwiki.fill_round(fetcher=fetcher))[1:] == (0, 0, 0, 1)
        assert len(fetcher.requests) == 3
        # 第二天接着做
        clock[0] += DAY
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests[3:] == [avwiki_search_url('ABC-003'), actress_url('bing'), actress_url('yi')]
        assert stars_of('ABC-002') == ['演员yi'] and stars_of('ABC-003') == ['演员bing']
        assert avwiki.daily_used() == 3

    def test_the_count_is_kept_in_the_library(self, make_item, fake_fetcher, clock):
        liked_movies(make_item, 'ABC-001')
        avwiki.fill_round(fetcher=fake_fetcher(site(('ABC-001', ['jia']))))
        row = db.JobState.get(db.JobState.name == avwiki.REQUESTS)
        assert (row.text, row.at) == ('2', NOW)
        assert avwiki.daily_used(NOW + DAY) == 0
        # 重启以后（内存里什么都没有）也算数
        assert avwiki.daily_used(NOW + MINUTE) == 2


class TestCorrection:
    '''迁移前按旧的存法补过的影片：迁移以后在补演员名的时候按新存法重新查一次'''

    def migrate(self, cast, make_item, fanhao, *blocks):
        like(make_item, fanhao)
        for slug, names in blocks:
            cast.legacy(fanhao, slug, *names)
        db.save_lookup(fanhao, db.LOOKUP_FILLED, 0, NOW - DAY, NOW - DAY)

    def test_a_movie_filled_the_old_way_is_looked_up_again(self, make_item, fake_fetcher, clock, cast):
        self.migrate(cast, make_item, 'OLD-001', ('jia', ['えんいんこう', '演员甲', 'Actor A']))
        assert actresses.migrate().state == 'done'
        # 迁移时只知道入库的先后，正式名字定成了 えんいんこう；女优页上排第一的是 演员甲
        assert stars_of('OLD-001') == ['えんいんこう']
        fetcher = fake_fetcher({avwiki_search_url('OLD-001'): card_page('OLD-001', ('jia', '演员甲')),
                                actress_url('jia'): actress_page('演员甲（えんいんこう） – Actor A')})
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == [avwiki_search_url('OLD-001'), actress_url('jia')]
        assert stars_of('OLD-001') == ['演员甲']
        assert actress_of('jia') == ('演员甲', ['えんいんこう', 'Actor A'], db.ACTRESS_PAGE, '')
        assert lookup('OLD-001') == ('filled', 0, 0 * DAY, '')
        assert (result.corrected, result.filled, result.due) == (1, 0, 1)
        assert db.JobState.get(db.JobState.name == actresses.FIXED_MARK).at == NOW
        assert actresses.correction_left() == (0, 0)

    def test_the_search_tells_whose_the_name_between_two_actresses_is(self, make_item, fake_fetcher, clock, cast):
        cast.movie('C-001', ('演员丙', '/star/c1'))
        self.migrate(cast, make_item, 'OLD-001', ('jia', ['演员甲', 'えんいんこう']), ('none', ['演员丙']),
                     ('yi', ['演员乙', 'えんいんおつ']))
        actresses.migrate()
        assert stars_of('OLD-001') == ['演员甲', '演员丙', '演员乙']
        fetcher = fake_fetcher({
            avwiki_search_url('OLD-001'): card_page('OLD-001', ('jia', '演员甲'), ('yi', '演员丙')),
            actress_url('jia'): actress_page('演员甲（えんいんこう）'),
            actress_url('yi'): actress_page('演员丙', '演员乙（えんいんおつ）')})
        avwiki.fill_round(fetcher=fetcher)
        assert stars_of('OLD-001') == ['演员甲', '演员丙']
        assert actress_of('yi') == ('演员丙', ['演员丙', '演员乙', 'えんいんおつ'], db.ACTRESS_PAGE, '/star/c1')
        assert cast.stars_of('C-001') == ['演员丙']

    def test_a_shared_reading_goes_with_the_actress_the_search_lists(self, make_item, fake_fetcher, clock, cast):
        self.migrate(cast, make_item, 'OLD-001', ('bing', ['演员丙', '共用读音', 'Actor C']))
        self.migrate(cast, make_item, 'OLD-002', ('jia', ['演员甲', 'えんいんこう']), ('none', ['共用读音']),
                     ('yi', ['演员乙', 'えんいんおつ']))
        actresses.migrate()
        # 共用读音夹在两位中间、演员丙的别的名字都不在 OLD-002 上：迁移时就不挂，说不准的是它属于左右哪一位
        assert stars_of('OLD-002') == ['演员甲', '演员乙']
        fetcher = fake_fetcher({
            avwiki_search_url('OLD-001'): card_page('OLD-001', ('bing', '演员丙')),
            avwiki_search_url('OLD-002'): card_page('OLD-002', ('jia', '演员甲'), ('yi', '演员乙')),
            actress_url('bing'): actress_page('演员丙（共用读音） – Actor C'),
            actress_url('jia'): actress_page('演员甲（えんいんこう）'),
            actress_url('yi'): actress_page('演员乙（えんいんおつ）', '共用读音')})
        avwiki.fill_round(fetcher=fetcher)
        assert stars_of('OLD-002') == ['演员甲', '演员乙']
        assert stars_of('OLD-001') == ['演员丙']
        assert '共用读音' in actress_of('yi')[1] and '共用读音' in actress_of('bing')[1]

    def test_the_second_of_two_actresses_with_the_same_first_name_keeps_her_number(self, make_item, fake_fetcher,
                                                                                  clock, cast):
        self.migrate(cast, make_item, 'OLD-001', ('jia', ['同名', 'えんいんこう']))
        self.migrate(cast, make_item, 'OLD-002', ('yi', ['同名', 'えんいんおつ']))
        actresses.migrate()
        assert stars_of('OLD-002') == ['同名（2）']
        fetcher = fake_fetcher({avwiki_search_url('OLD-001'): card_page('OLD-001', ('jia', '同名')),
                                avwiki_search_url('OLD-002'): card_page('OLD-002', ('yi', '同名')),
                                actress_url('jia'): actress_page('同名（えんいんこう）'),
                                actress_url('yi'): actress_page('同名（えんいんおつ）')})
        avwiki.fill_round(fetcher=fetcher)
        assert stars_of('OLD-001') == ['同名'] and stars_of('OLD-002') == ['同名（2）']
        assert actress_of('yi')[:2] == ('同名（2）', ['同名', 'えんいんおつ'])

    def test_the_result_of_the_upgrade_is_kept_when_av_wiki_no_longer_has_the_movie(self, make_item, fake_fetcher,
                                                                                     clock, cast):
        self.migrate(cast, make_item, 'OLD-001', ('jia', ['演员甲', 'えんいんこう']))
        actresses.migrate()
        fetcher = fake_fetcher({avwiki_search_url('OLD-001'): NO_RESULT,
                                actress_url('jia'): actress_page('演员甲（えんいんこう）')})
        avwiki.fill_round(fetcher=fetcher)
        # 搜不到的这一部过几天再查；不在任何要重新查的影片上的、迁移整理出来的女优，读一遍她的女优页
        assert actress_of('jia')[2] == db.ACTRESS_PAGE
        assert actresses.correction_left() == (1, 0)
        for days in (1, 7):
            clock[0] += days * DAY
            result = avwiki.fill_round(fetcher=fetcher)
        assert stars_of('OLD-001') == ['演员甲']
        assert lookup('OLD-001')[0] == 'filled'
        assert result.corrected == 1
        assert actresses.correction_left() == (0, 0)

    def test_a_movie_av_wiki_does_not_find_is_looked_up_again_after_a_day_and_after_a_week(
            self, make_item, fake_fetcher, clock, cast):
        self.migrate(cast, make_item, 'OLD-001', ('jia', ['演员甲', 'えんいんこう']))
        rated(make_item, 'NEW-001')
        actresses.migrate()
        fetcher = fake_fetcher({avwiki_search_url('OLD-001'): NO_RESULT,
                                avwiki_search_url('NEW-001'): card_page('NEW-001', ('yi', '演员乙')),
                                actress_url('jia'): actress_page('演员甲（えんいんこう）')})
        result = avwiki.fill_round(fetcher=fetcher)
        # 一时查不到不马上认定：1 天后再查；别的影片照常补
        assert fetcher.requests == [avwiki_search_url('OLD-001'), avwiki_search_url('NEW-001'), actress_url('jia')]
        assert (result.corrected, result.missing, result.filled) == (0, 1, 1)
        assert lookup('OLD-001') == ('stale', 1, DAY, '')
        assert stars_of('OLD-001') == ['演员甲'] and stars_of('NEW-001') == ['演员乙']
        assert actresses.correction_left() == (1, 0)
        status = avwiki.status()
        assert (status.stale, status.stale_later, status.last_failure) == (1, 1, None)
        assert not db.JobState.select().where(db.JobState.name == actresses.FIXED_MARK).exists()
        clock[0] += DAY - MINUTE
        avwiki.fill_round(fetcher=fetcher)
        assert len(fetcher.requests) == 3
        clock[0] += MINUTE
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests[3:] == [avwiki_search_url('OLD-001')]
        assert (result.corrected, result.missing) == (0, 1)
        assert lookup('OLD-001') == ('stale', 2, 7 * DAY, '')
        clock[0] += 7 * DAY
        result = avwiki.fill_round(fetcher=fetcher)
        # 第三次还没有：保留升级时整理的演员，不再查
        assert fetcher.requests[4:] == [avwiki_search_url('OLD-001')]
        assert (result.corrected, result.missing) == (1, 0)
        assert lookup('OLD-001')[:2] == ('filled', 0)
        assert stars_of('OLD-001') == ['演员甲']
        assert actresses.correction_left() == (0, 0)
        assert db.JobState.get(db.JobState.name == actresses.FIXED_MARK).at == clock[0]
        clock[0] += 30 * DAY
        avwiki.fill_round(fetcher=fetcher)
        assert len(fetcher.requests) == 5

    def test_a_movie_found_on_a_later_try_is_corrected(self, make_item, fake_fetcher, clock, cast):
        self.migrate(cast, make_item, 'OLD-001', ('jia', ['えんいんこう', '演员甲']))
        actresses.migrate()
        fetcher = fake_fetcher({avwiki_search_url('OLD-001'): NO_RESULT,
                                actress_url('jia'): actress_page('演员甲（えんいんこう）')})
        avwiki.fill_round(fetcher=fetcher)
        fetcher.routes[avwiki_search_url('OLD-001')] = card_page('OLD-001', ('jia', '演员甲'))
        clock[0] += DAY
        result = avwiki.fill_round(fetcher=fetcher)
        assert (result.corrected, result.missing) == (1, 0)
        assert lookup('OLD-001') == ('filled', 0, 0 * DAY, '')
        assert stars_of('OLD-001') == ['演员甲']

    def test_a_network_failure_leaves_it_for_the_next_round(self, make_item, fake_fetcher, clock, cast):
        self.migrate(cast, make_item, 'OLD-001', ('jia', ['演员甲', 'えんいんこう']))
        actresses.migrate()
        fetcher = fake_fetcher(default=FetchError('网络连接失败'))
        result = avwiki.fill_round(fetcher=fetcher)
        assert (result.failed, result.corrected) == (1, 0)
        assert lookup('OLD-001') == ('stale', 0, 0 * DAY, '网络连接失败')
        assert actresses.correction_left() == (1, 1)
        assert not db.JobState.select().where(db.JobState.name == actresses.FIXED_MARK).exists()
        assert avwiki.status().last_failure == ('OLD-001', '网络连接失败', NOW)


class TestStatus:
    def test_before_any_round(self, make_item, clock):
        like(make_item, 'ABC-001')
        rated(make_item, 'ABC-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        rated(make_item, 'ABC-003')
        status = avwiki.status()
        assert (status.waiting, status.liked, status.recommended, status.due, status.filled) == (3, 1, 1, 3, 0)
        assert (status.stale, status.pages, status.used, status.limit) == (0, 0, 0, 200)
        assert (status.last_round, status.last_failure) == (None, None)

    def test_counts_come_from_the_records_and_the_last_round_is_kept(self, make_item, fake_fetcher, clock):
        liked_movies(make_item, *[f'ABC-{n:03d}' for n in range(1, 5)])
        routes = site(('ABC-001', ['jia']), ('ABC-002', ['yi']))
        routes[avwiki_search_url('ABC-003')] = NO_RESULT
        routes[avwiki_search_url('ABC-004')] = FetchError('网络连接失败')
        result = avwiki.fill_round(fetcher=fake_fetcher(routes))
        clock[0] += MINUTE
        status = avwiki.status()
        # 还在等的两部里，没查到的那部明天才再试，失败的那部下一轮就试
        assert (status.waiting, status.due, status.filled, status.used) == (2, 1, 2, 6)
        assert status.last_round is result
        assert status.last_failure == ('ABC-004', '网络连接失败', NOW)
        assert avwiki.last_round is result

    def test_a_round_with_nothing_to_do_does_not_replace_the_last_one_that_did_something(self, make_item,
                                                                                         fake_fetcher, clock):
        # 抓取之后总会再补一轮：紧跟着的那一轮多半没有要补的，页面上要看的还是处理过影片的那一轮
        like(make_item, 'ABC-001')
        worked = avwiki.fill_round(fetcher=fake_fetcher(site(('ABC-001', ['jia']))))
        clock[0] += MINUTE
        idle = avwiki.fill_round(fetcher=fake_fetcher())
        assert counts(idle) == (0, 0, 0, 0, 0)
        assert avwiki.status().last_round is worked

    def test_until_a_round_did_something_the_last_idle_one_is_kept(self, make_item, fake_fetcher, clock):
        first = avwiki.fill_round(fetcher=fake_fetcher())
        assert avwiki.status().last_round is first
        clock[0] += MINUTE
        second = avwiki.fill_round(fetcher=fake_fetcher())
        assert avwiki.status().last_round is second
        like(make_item, 'ABC-001')
        worked = avwiki.fill_round(fetcher=fake_fetcher(site(('ABC-001', ['jia']))))
        assert avwiki.status().last_round is worked

    def test_a_reason_saved_earlier_is_reported_without_the_name_in_the_address(self, make_item, clock):
        like(make_item, 'ABC-001')
        db.save_lookup('ABC-001', db.LOOKUP_FAILED, 0, NOW, NOW, f'HTTP 503：{actress_url("mingzi-jia")}')
        assert avwiki.status().last_failure == ('ABC-001', 'HTTP 503：https://av-wiki.net/av-actress/…/', NOW)


class TestADatabaseAnotherProgramHolds:
    '''
    一轮补演员名里，数据库被别的程序占着、写不进去：这一轮停下，只写一条如实的警告（没有异常栈），不算哪一部影片失败，
    也不记失败的原因；记不进库里的请求不发，已经发出去的请求照常算
    '''

    @staticmethod
    def locked(*args, **kwargs):
        from peewee import OperationalError
        raise OperationalError('database is locked')

    @staticmethod
    def problems(caplog):
        return [record for record in caplog.records if record.levelno >= logging.WARNING]

    def test_a_request_that_cannot_be_counted_is_not_sent_and_the_round_stops_with_one_warning(
            self, make_item, fake_fetcher, clock, monkeypatch, caplog):
        liked_movies(make_item, 'ABC-001', 'ABC-002', 'ABC-003')
        fetcher = fake_fetcher(site(('ABC-001', ['jia']), ('ABC-002', ['yi']), ('ABC-003', ['bing'])))
        monkeypatch.setattr(avwiki, '_count_request', self.locked)
        result = avwiki.fill_round(fetcher=fetcher)
        assert fetcher.requests == []
        assert (result.failed, result.left, result.requests) == (0, 3, 0)
        assert [lookup(code) for code in ('ABC-001', 'ABC-002', 'ABC-003')] == [None, None, None]
        assert result.stopped and result.error == ''
        warned, = self.problems(caplog)
        assert warned.levelno == logging.WARNING and warned.exc_info is None
        assert '数据库正被别的程序占用' in warned.getMessage() and '剩下的下一轮再试' in warned.getMessage()

    def test_a_request_that_was_sent_stays_counted_when_its_result_cannot_be_written(
            self, make_item, fake_fetcher, clock, monkeypatch, caplog):
        liked_movies(make_item, 'ABC-001', 'ABC-002')
        fetcher = fake_fetcher(site(('ABC-001', ['jia']), ('ABC-002', ['yi'])))
        monkeypatch.setattr(actresses, 'set_movie_actresses', self.locked)
        result = avwiki.fill_round(fetcher=fetcher)
        # 第一部的请求已经发出去了、结果写不进去：这一轮停下，第二部不再去请求；发出去的请求算数，这部影片留着下一轮
        assert avwiki_search_url('ABC-002') not in fetcher.requests
        assert len(fetcher.requests) >= 1
        assert avwiki.daily_used(NOW) == result.requests == len(fetcher.requests)
        assert (result.failed, result.left) == (0, 2)
        assert lookup('ABC-001') is None and result.stopped
        warned, = self.problems(caplog)
        assert warned.exc_info is None
        assert avwiki.last_round is result

    def test_the_next_round_goes_on_as_usual_once_the_database_is_free(self, make_item, fake_fetcher, clock, monkeypatch):
        liked_movies(make_item, 'ABC-001')
        routes = site(('ABC-001', ['jia']))
        with monkeypatch.context() as held:
            held.setattr(avwiki, '_count_request', self.locked)
            avwiki.fill_round(fetcher=fake_fetcher(routes))
        result = avwiki.fill_round(fetcher=fake_fetcher(routes))
        assert counts(result) == (1, 1, 0, 0, 0)
        assert stars_of('ABC-001') == ['演员jia']