'''
指定推荐的匹配：什么叫符合设定、候选范围、排序分页、别名、库内数量、"从你喜欢的影片里选"和输入联想
'''
import datetime

import pytest

from bustag.app import pick
from bustag.app.pick import Settings
from bustag.spider import actresses, db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate

AVWIKI = 'https://av-wiki.net/av-actress/'
NOW = datetime.datetime(2026, 11, 11, 12, 0, 0)


def chosen(stars=(), tags=(), mode='all', model_only=False):
    return Settings(stars=tuple(stars), tags=tuple(tags), mode=mode, model_only=model_only)


def picked(settings, page=1):
    '''这一页列出的番号'''
    items, _ = pick.get_items(settings, pick.match_names(settings), page)
    return [item.fanhao for item in items]


def total(settings):
    return pick.count_items(settings, pick.match_names(settings))


def rate(fanhao, rate_type, rate_value):
    assert ItemRate.saveit(rate_type, rate_value, fanhao) is not None


def like(make_item, fanhao, tags=()):
    make_item(fanhao, tags=tags)
    rate(fanhao, RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)


@pytest.fixture(params=['as-is', 'always-scan', 'never-scan', 'always-probe', 'never-probe'])
def query_shape(request, monkeypatch):
    '''
    同一个条件按"带这些标签的影片有多少、一组里有几个标签"挑不同的 SQL 写法，哪一种都要得到同样的结果
    '''
    forced = {'always-scan': ('SCAN_RATIO', 10 ** 9), 'never-scan': ('SCAN_RATIO', 0),
              'always-probe': ('PROBE_LIMIT', 10 ** 9), 'never-probe': ('PROBE_LIMIT', 0)}
    if request.param in forced:
        monkeypatch.setattr(pick, *forced[request.param])


@pytest.fixture
def library(make_item):
    '''五部没有评分的影片，后入库的排在前面'''
    make_item('M-001', tags=[('star', '演员甲'), ('genre', '标签一'), ('genre', '标签二')])
    make_item('M-002', tags=[('star', '演员乙'), ('genre', '标签一')])
    make_item('M-003', tags=[('star', '演员甲'), ('star', '演员乙'), ('genre', '标签二'), ('系列:', '系列甲')])
    make_item('M-004', tags=[('genre', '标签一'), ('genre', '标签二'), ('genre', '4K')])
    make_item('M-005', tags=[('star', '演员丙'), ('genre', '标签三')])


@pytest.mark.usefixtures('query_shape')
class TestWhatMatches:
    def test_no_star_and_no_tag_matches_nothing(self, library):
        assert picked(chosen()) == []
        assert total(chosen()) == 0
        assert picked(chosen(mode='any')) == []

    def test_one_star(self, library):
        assert picked(chosen(stars=['演员甲'])) == ['M-003', 'M-001']

    def test_all_needs_at_least_one_of_the_stars(self, library):
        assert picked(chosen(stars=['演员甲', '演员乙'])) == ['M-003', 'M-002', 'M-001']

    def test_all_needs_every_tag(self, library):
        assert picked(chosen(tags=['标签一', '标签二'])) == ['M-004', 'M-001']

    def test_all_with_stars_and_tags_needs_both(self, library):
        assert picked(chosen(stars=['演员甲', '演员乙'], tags=['标签一'])) == ['M-002', 'M-001']
        assert picked(chosen(stars=['演员甲', '演员乙'], tags=['标签一', '标签二'])) == ['M-001']

    def test_any_takes_any_star_or_any_tag(self, library):
        assert picked(chosen(stars=['演员丙'], tags=['标签二'], mode='any')) == ['M-005', 'M-004', 'M-003', 'M-001']
        assert picked(chosen(tags=['标签三', '4K'], mode='any')) == ['M-005', 'M-004']

    @pytest.mark.parametrize('type_', ['genre', '系列:', '製作商:', '發行商:', '導演:', 'star'])
    def test_a_tag_may_be_of_any_type(self, make_item, type_):
        make_item('M-001', tags=[(type_, '标签一')])
        make_item('M-002', tags=[('genre', '标签二')])
        assert picked(chosen(tags=['标签一'])) == ['M-001']

    def test_a_star_must_be_an_actress_tag(self, library):
        assert picked(chosen(stars=['标签一'])) == []
        assert picked(chosen(stars=['系列甲'])) == []
        assert picked(chosen(stars=['标签一'], mode='any')) == []

    def test_letters_ignore_case_and_blanks_around_are_dropped(self, library, make_item):
        make_item('M-006', tags=[('star', 'Abc Def')])
        assert picked(chosen(tags=['4k'])) == ['M-004']
        assert picked(chosen(tags=[' 标签三 '])) == ['M-005']
        assert picked(chosen(stars=['aBC dEF'])) == ['M-006']

    def test_simplified_and_traditional_spellings_are_the_same_name(self, make_item):
        make_item('M-001', tags=[('genre', '女大學生'), ('star', '演員甲')])
        make_item('M-002', tags=[('genre', '女大学生')])
        make_item('M-003', tags=[('genre', '學生')])
        for name in ('女大学生', '女大學生', '女大学生 '):
            assert picked(chosen(tags=[name])) == ['M-002', 'M-001'], name
        for name in ('演员甲', '演員甲'):
            assert picked(chosen(stars=[name])) == ['M-001'], name
        # 仍然要整个名字相同，只写一部分不算
        assert picked(chosen(tags=['学生'])) == ['M-003']
        assert picked(chosen(tags=['大学生'])) == []
        assert picked(chosen(tags=['女大学生', '学生'], mode='any')) == ['M-003', 'M-002', 'M-001']

    def test_japanese_spellings_are_not_converted(self, make_item):
        make_item('M-001', tags=[('genre', '気持ち')])
        make_item('M-002', tags=[('genre', '氣質')])
        assert picked(chosen(tags=['气质'])) == ['M-002']
        assert picked(chosen(tags=['气持ち'])) == []
        assert picked(chosen(tags=['気持ち'])) == ['M-001']

    @pytest.mark.parametrize('blank', [' ', '\u3000', '\t', '\xa0', '\n', '\u3000 \t'],
                             ids=['space', 'full-width', 'tab', 'no-break', 'newline', 'mixed'])
    def test_blanks_of_any_kind_around_a_stored_value_are_dropped(self, make_item, blank):
        make_item('M-001', tags=[('genre', f'{blank}标签一{blank}'), ('star', f'{blank}演员甲{blank}')])
        make_item('M-002', tags=[('genre', '标签一甲')])
        settings = chosen(stars=['演员甲'], tags=['标签一'])
        assert picked(settings) == ['M-001']
        assert pick.library_counts(pick.match_names(settings)) == ({'演员甲': 1}, {'标签一': 1})

    def test_part_of_a_value_does_not_match(self, library):
        assert picked(chosen(tags=['标签'])) == []
        assert picked(chosen(stars=['演员'])) == []
        assert picked(chosen(tags=['标签一标签二'])) == []
        assert picked(chosen(tags=['标签', '演员'], stars=['演员'], mode='any')) == []

    def test_all_with_a_tag_nobody_has_matches_nothing(self, library):
        assert picked(chosen(tags=['标签一', '还没有的标签'])) == []
        assert picked(chosen(tags=['标签一', '还没有的标签'], mode='any')) == ['M-004', 'M-002', 'M-001']

    def test_all_with_stars_nobody_has_matches_nothing(self, library):
        assert picked(chosen(stars=['还没有的演员'], tags=['标签一'])) == []
        assert picked(chosen(stars=['还没有的演员'], tags=['标签一'], mode='any')) == ['M-004', 'M-002', 'M-001']
        assert picked(chosen(stars=['还没有的演员'], mode='any')) == []

    @pytest.mark.parametrize('name', ["<script>alert(1)</script>", '"双引号" & \'单引号\'', "'; DROP TABLE item; --",
                                      '100%_纯\\'])
    def test_odd_characters_are_matched_literally(self, make_item, name):
        make_item('M-001', tags=[('genre', name), ('star', name)])
        make_item('M-002', tags=[('genre', '标签一')])
        assert picked(chosen(tags=[name])) == ['M-001']
        assert picked(chosen(stars=[name])) == ['M-001']
        assert db.Item.select().count() == 2

    def test_a_hundred_stars_and_a_hundred_tags(self, library):
        stars = ['演员甲'] + [f'演员{n}' for n in range(99)]
        tags = ['标签一'] + [f'标签{n}' for n in range(99)]
        assert picked(chosen(stars=stars, tags=tags, mode='any')) == ['M-004', 'M-003', 'M-002', 'M-001']
        assert picked(chosen(stars=stars, tags=tags)) == []
        assert picked(chosen(stars=stars, tags=['标签一'])) == ['M-001']


@pytest.mark.usefixtures('query_shape')
class TestCandidates:
    def test_movies_the_user_tagged_are_left_out(self, library):
        rate('M-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        rate('M-002', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        rate('M-003', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        rate('M-004', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)
        settings = chosen(tags=['标签一', '标签二', '标签三'], mode='any')
        assert sorted(picked(settings)) == ['M-003', 'M-004', 'M-005']
        assert total(settings) == 3

    def test_model_rated_movies_come_first_then_unrated_then_model_dislikes(self, make_item):
        with db.db.atomic():
            for n in range(1, 10):
                make_item(f'M-{n:03d}', tags=[('genre', '标签一')])
        for fanhao in ('M-002', 'M-007'):
            rate(fanhao, RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        for fanhao in ('M-004', 'M-009'):
            rate(fanhao, RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)
        items, _ = pick.get_items(chosen(tags=['标签一']), pick.match_names(chosen(tags=['标签一'])), 1)
        assert [item.fanhao for item in items] == ['M-007', 'M-002', 'M-008', 'M-006', 'M-005', 'M-003', 'M-001',
                                                   'M-009', 'M-004']
        assert [item.rate_value for item in items] == [1, 1, None, None, None, None, None, 0, 0]

    def test_inside_each_group_the_higher_score_comes_first(self, make_item):
        with db.db.atomic():
            for n in range(1, 9):
                make_item(f'M-{n:03d}', tags=[('genre', '标签一')])
        scores = [('M-008', 0.2), ('M-007', 0.9), ('M-005', 0.6), ('M-004', 0.95), ('M-002', 0.4), ('M-001', 0.5)]
        db.save_scores([db.Score(fanhao, value, [], []) for fanhao, value in scores], lambda total: 3)
        settings = chosen(tags=['标签一'])
        items, _ = pick.get_items(settings, pick.match_names(settings), 1)
        # 推荐里的三部、模型还没打分的两部（新入库的在前）、不太像里的三部
        assert [item.fanhao for item in items] == ['M-004', 'M-007', 'M-005', 'M-006', 'M-003', 'M-001', 'M-002', 'M-008']
        assert [item.score for item in items] == [0.95, 0.9, 0.6, None, None, 0.5, 0.4, 0.2]
        only = chosen(tags=['标签一'], model_only=True)
        assert picked(only) == ['M-004', 'M-007', 'M-005']
        assert total(only) == 3

    def test_model_only_keeps_what_the_model_likes(self, library):
        rate('M-001', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        rate('M-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)
        rate('M-005', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        everything = chosen(tags=['标签一', '标签二', '标签三'], mode='any')
        assert picked(everything) == ['M-001', 'M-004', 'M-003', 'M-002']
        only = chosen(tags=['标签一', '标签二', '标签三'], mode='any', model_only=True)
        assert pick.has_model_results() is True
        assert picked(only) == ['M-001']
        assert total(only) == 1

    def test_model_only_does_nothing_before_the_model_has_rated_anything(self, library):
        rate('M-005', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        only = chosen(tags=['标签一'], model_only=True)
        assert pick.has_model_results() is False
        assert picked(only) == ['M-004', 'M-002', 'M-001']
        assert total(only) == 3

    def test_pages_have_ten_rows_and_stop_at_the_last_page(self, make_item):
        with db.db.atomic():
            for n in range(1, 26):
                make_item(f'M-{n:03d}', tags=[('star', '演员甲')])
            make_item('X-001', tags=[('star', '演员乙')])
        settings = chosen(stars=['演员甲'])
        matched = pick.match_names(settings)
        items, page_info = pick.get_items(settings, matched, 1)
        assert page_info == (25, 3, 1, 10)
        assert [item.fanhao for item in items] == [f'M-{n:03d}' for n in range(25, 15, -1)]
        items, page_info = pick.get_items(settings, matched, 3)
        assert [item.fanhao for item in items] == [f'M-{n:03d}' for n in range(5, 0, -1)]
        items, page_info = pick.get_items(settings, matched, 99)
        assert page_info == (25, 3, 3, 10)
        assert pick.count_items(settings, matched) == 25

    def test_nothing_matching_is_an_empty_first_page(self, library):
        settings = chosen(stars=['还没有的演员'])
        assert pick.get_items(settings, pick.match_names(settings), 5) == ([], (0, 0, 1, 10))
        assert pick.get_items(chosen(), pick.match_names(chosen()), 1) == ([], (0, 0, 1, 10))

    def test_rows_carry_what_the_card_shows(self, library):
        rate('M-003', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)
        settings = chosen(tags=['系列甲'])
        item, = pick.get_items(settings, pick.match_names(settings), 1)[0]
        assert (item.fanhao, item.rate_value, item.url) == ('M-003', 0, 'https://www.javbus.com/M-003')
        assert item.tags_dict['star'] == ['演员甲', '演员乙']
        assert item.tags_dict['genre'] == ['标签二']
        assert item.cover_img_url


def people(settings, name):
    '''设定里这个女优名字找到的人：[(正式名字, [别名…])]'''
    return [(person.name, list(person.aliases)) for person in pick.match_names(settings).people[name]]


@pytest.mark.usefixtures('query_shape')
class TestPeople:
    '''一位女优一条正式名字（影片只挂这一条），其他名字在别名表里：选她的任何一个名字都是选她'''

    def test_any_name_of_an_actress_selects_her(self, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', 'Actor A')
        cast.movie('C-001')
        cast.movie('C-002')
        cast.movie('C-003', ('演员乙', '/star/b1'))
        cast.link('C-001', jia)
        cast.link('C-002', jia)
        for name in ('演员甲', 'えんいんこう', 'actor a', ' ACTOR A ', '演員甲'):
            assert picked(chosen(stars=[name])) == ['C-002', 'C-001'], name
            assert people(chosen(stars=[name]), name) == [('演员甲', ['えんいんこう', 'Actor A'])]

    def test_a_name_two_actresses_share_selects_both(self, cast):
        jia = cast.actress('演员甲', 'jia', '共用读音')
        yi = cast.actress('演员乙', 'yi', '共用读音')
        cast.movie('C-001')
        cast.movie('C-002')
        cast.movie('C-003')
        cast.link('C-001', jia)
        cast.link('C-002', yi)
        assert picked(chosen(stars=['共用读音'])) == ['C-002', 'C-001']
        assert people(chosen(stars=['共用读音']), '共用读音') == [('演员甲', ['共用读音']), ('演员乙', ['共用读音'])]
        assert picked(chosen(stars=['演员乙'])) == ['C-002']

    def test_her_javbus_stars_count_as_her(self, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', javbus='/star/a1')
        cast.movie('C-001')
        cast.link('C-001', jia)
        cast.movie('C-002', ('別の名前', '/star/a1'))
        cast.movie('C-003', ('演员乙', '/star/b1'))
        assert picked(chosen(stars=['えんいんこう'])) == ['C-002', 'C-001']
        assert picked(chosen(stars=['別の名前'])) == ['C-002', 'C-001']

    def test_tags_do_not_follow_aliases(self, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう')
        cast.movie('C-001')
        cast.link('C-001', jia)
        assert picked(chosen(tags=['えんいんこう'])) == []
        assert picked(chosen(tags=['演员甲'])) == ['C-001']

    def test_names_are_compared_inside_sqlite(self, cast, sql_folds):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう')
        for n in range(20):
            cast.movie(f'C-{n:03d}', (f'演员{n}', f'/star/s{n}'), genres=[f'標籤{n % 3}'])
        cast.link('C-001', jia)
        settings = chosen(stars=['えんいんこう', '演员7'], tags=['标签1'], mode='any')
        assert len(picked(settings)) == 7
        pick.library_counts(pick.match_names(settings))
        assert sql_folds == []


@pytest.mark.usefixtures('query_shape')
class TestUnmigratedRows:
    '''迁移前的库：av-wiki 补名时每个名字各是一条 star 标签，地址是女优页；同一个女优页的名字算同一个人'''

    def fill(self, make_item, cast, fanhao, slug, names):
        like(make_item, fanhao)
        cast.legacy(fanhao, slug, *names)

    def test_names_from_one_actress_page_count_as_the_same_person(self, make_item, cast):
        self.fill(make_item, cast, 'L-001', 'jia', ['演员甲', '演员甲读音', 'jia yanyuan', '旧名甲'])
        make_item('C-001', tags=[('star', '旧名甲')])
        make_item('C-002', tags=[('star', '演员甲')])
        make_item('C-003', tags=[('star', '演员乙')])
        for name in ('演员甲', '演员甲读音', 'JIA Yanyuan', '旧名甲'):
            settings = chosen(stars=[name])
            assert picked(settings) == ['C-002', 'C-001'], name
        assert people(chosen(stars=['演员甲读音', '演员乙']), '演员甲读音') == [
            ('演员甲', ['演员甲读音', 'jia yanyuan', '旧名甲'])]

    def test_names_from_different_pages_stay_apart(self, make_item, cast):
        self.fill(make_item, cast, 'L-001', 'jia', ['演员甲', '共用读音'])
        self.fill(make_item, cast, 'L-002', 'yi', ['演员乙', '共用读音'])
        make_item('C-001', tags=[('star', '演员甲')])
        make_item('C-002', tags=[('star', '演员乙')])
        assert picked(chosen(stars=['演员乙'])) == ['C-002']
        assert people(chosen(stars=['演员乙']), '演员乙') == [('演员乙', [])]
        assert picked(chosen(stars=['演员甲'])) == ['C-001']
        assert people(chosen(stars=['演员甲']), '演员甲') == [('演员甲', ['共用读音'])]

    def test_a_name_that_came_from_the_site_first_is_not_grouped(self, make_item, cast):
        make_item('C-001', tags=[('star', '演员丙')])
        self.fill(make_item, cast, 'L-001', 'bing', ['演员丙', '演员丙读音'])
        assert people(chosen(stars=['演员丙']), '演员丙') == [('演员丙', [])]
        assert people(chosen(stars=['演员丙读音']), '演员丙读音') == [('演员丙读音', [])]
        assert picked(chosen(stars=['演员丙读音'])) == []
        assert picked(chosen(stars=['演员丙'])) == ['C-001']

    def test_an_alias_is_one_stored_name_not_every_spelling_of_it(self, make_item, cast):
        self.fill(make_item, cast, 'L-001', 'jia', ['演员甲', 'jia'])
        make_item('C-001', tags=[('star', 'JIA')])
        assert picked(chosen(stars=['演员甲'])) == []
        assert picked(chosen(stars=['jia'])) == ['C-001']

    def test_tags_do_not_follow_aliases(self, make_item, cast):
        self.fill(make_item, cast, 'L-001', 'jia', ['演员甲', '旧名甲'])
        make_item('C-001', tags=[('star', '旧名甲')])
        assert picked(chosen(tags=['演员甲'])) == []
        assert picked(chosen(tags=['旧名甲'])) == ['C-001']


def linked_second_actress(cast):
    '''
    两位第一个名字都是「演员甲」的女优，第二位编成「演员甲（2）」、补过两部影片；她查找到的 JavBus 演员页 a9 上写作
    「演員甲」（那里还有一部她的影片），正式名字换成了它
    '''
    actresses.read_page('/av-actress/jia/', ['演员甲', 'えんいんこう'], NOW)
    yi, _ = actresses.read_page('/av-actress/yi/', ['演员甲', 'えんいんおつ'], NOW)
    for fanhao in ('W-001', 'W-002'):
        cast.movie(fanhao)
        cast.link(fanhao, yi)
        db.save_lookup(fanhao, db.LOOKUP_FILLED, 0, NOW, NOW)
    cast.movie('JB-009', ('演員甲', '/star/a9'))
    with db.db.atomic('IMMEDIATE'):
        actresses.found_javbus('avwiki:/av-actress/yi/', '/star/a9', '演員甲')


def save_movie(fanhao, *stars):
    '''入库一部没有评分的影片，stars 是 (名字, 链接)'''
    from bustag.spider.parser import Tag
    meta = {'fanhao': fanhao, 'title': f'示例影片 {fanhao}', 'url': '/' + fanhao, 'release_date': '2026-01-01',
            'cover_img_url': '', 'length': '120'}
    assert db.save(meta, [Tag('star', name, url) for name, url in stars])


@pytest.mark.usefixtures('query_shape')
class TestJavbusPages:
    '''同一个 JavBus 演员页上的名字算同一个人；查找到的 JavBus 演员页上的名字也算她'''

    def test_names_on_one_javbus_page_count_as_the_same_person(self):
        save_movie('C-001', ('旧名甲', '/star/a1'))
        save_movie('C-002', ('演员甲', '/star/a1'))
        save_movie('C-003', ('演员乙', '/star/b2'))
        assert picked(chosen(stars=['演员甲'])) == ['C-002', 'C-001']
        assert people(chosen(stars=['演员甲']), '演员甲') == [('演员甲', ['旧名甲'])]

    def test_an_avwiki_name_takes_the_javbus_page_found_for_her(self, make_item, cast):
        like(make_item, 'L-001')
        cast.legacy('L-001', 'jia', '演员甲读音', 'jia')
        save_movie('C-001', ('演员甲', '/star/a1'))
        save_movie('C-002', ('别人', '/star/z9'))
        assert picked(chosen(stars=['jia'])) == []
        db.StarLink.create(key=f'avwiki:/av-actress/jia/', name='演员甲读音', state='found', path='/star/a1')
        assert picked(chosen(stars=['jia'])) == ['C-001']
        assert people(chosen(stars=['jia']), 'jia') == [('演员甲读音', ['jia', '演员甲'])]

    def test_an_actress_takes_the_javbus_page_found_for_her(self, cast):
        jia = cast.actress('演员甲读音', 'jia', 'jia')
        cast.movie('L-001')
        cast.link('L-001', jia)
        save_movie('C-001', ('演员甲', '/star/a1'))
        assert picked(chosen(stars=['jia'])) == ['L-001']
        db.StarLink.create(key=f'avwiki:/av-actress/jia/', name='演员甲读音', state='found', path='/star/a1')
        assert picked(chosen(stars=['jia'])) == ['C-001', 'L-001']

    def test_a_name_not_in_the_library_takes_the_page_found_for_it(self):
        save_movie('C-001', ('演员甲', '/star/a1'))
        db.StarLink.create(key='name:另一个写法', name='另一个写法', state='found', path='/star/a1')
        assert picked(chosen(stars=['另一个写法'])) == ['C-001']

    def test_a_search_still_waiting_for_a_choice_does_not_count(self):
        save_movie('C-001', ('演员甲', '/star/a1'))
        db.StarLink.create(key='name:另一个写法', name='另一个写法', state='choose', path='')
        assert picked(chosen(stars=['另一个写法'])) == []

    def test_made_up_links_that_are_not_actress_pages_are_not_grouped(self, make_item):
        make_item('C-001', tags=[('star', '演员甲')])
        make_item('C-002', tags=[('star', '演员乙')])
        assert people(chosen(stars=['演员甲']), '演员甲') == [('演员甲', [])]


class TestBackfillRows:
    def test_more_actresses_take_no_more_queries(self, cast, queries):
        # 按演员补全那一块每打开一次指定推荐页都要算：关联的演员页上的名字、她说过不是她的演员页，一次查完，不一行查一次
        def rows_for(count):
            names = []
            for n in range(count):
                actress = cast.actress(f'演员{count}-{n}', f'p{count}-{n}', javbus=f'/star/s{count}-{n}')
                save_movie(f'C-{count}-{n}', (f'别名{count}-{n}', f'/star/s{count}-{n}'))
                cast.movie(f'L-{count}-{n}')
                cast.link(f'L-{count}-{n}', actress)
                names.append(f'演员{count}-{n}')
            settings = chosen(stars=names)
            matched = pick.match_names(settings)
            star_counts, _ = pick.library_counts(matched)
            queries.clear()
            rows = pick.backfill_rows(settings, matched, star_counts)
            assert [row.page_name for row in rows] == [f'别名{count}-{n}' for n in range(count)]
            return len(queries)

        assert rows_for(2) == rows_for(12)

    def test_a_numbered_name_saved_before_still_finds_her_after_her_page_is_read(self, cast):
        # 以前的版本把第二位同名的女优编成"同名（3）"、别名里记着"同名（2）"，设定里存的是"同名（2）"
        cast.actress('同名', 'jia')
        yi = cast.actress('同名（3）', 'yi', '同名', 'どうめいに', '同名（2）')
        cast.movie('L-001')
        cast.link('L-001', yi)
        settings = chosen(stars=['同名（2）'])
        actresses.read_page('/av-actress/yi/', ['同名', 'どうめいに'], NOW)
        matched = pick.match_names(settings)
        star_counts, _ = pick.library_counts(matched)
        assert star_counts == {'同名（2）': 1}
        row, = pick.backfill_rows(settings, matched, star_counts)
        assert (row.row, row.name, row.search, row.movies) == (f'actress:{yi.id}', '同名（3）', '同名', 1)

    def test_a_numbered_name_saved_before_finds_her_while_she_goes_by_her_javbus_name(self, cast):
        # 设定里存的是「演员甲（2）」；她查找到 JavBus 演员页「演員甲」以后改用那个名字，这一行照样是她（可以取消关联）
        linked_second_actress(cast)
        yi = db.Actress.get(db.Actress.avwiki == '/av-actress/yi/')
        settings = chosen(stars=['演员甲（2）'])
        matched = pick.match_names(settings)
        star_counts, _ = pick.library_counts(matched)
        assert star_counts == {'演员甲（2）': 3}
        row, = pick.backfill_rows(settings, matched, star_counts)
        assert (row.row, row.name, row.movies, row.path, row.avwiki) == (
            f'actress:{yi.id}', '演員甲', 3, '/star/a9', '/av-actress/yi/')


class TestLibraryCounts:
    def test_counts_every_movie_carrying_the_name_whatever_its_rate(self, library):
        rate('M-001', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        rate('M-003', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        settings = chosen(stars=['演员甲', '还没有的演员', '标签一'], tags=['标签一', '4k', '演员乙', '还没有的标签'])
        stars, tags = pick.library_counts(pick.match_names(settings))
        assert stars == {'演员甲': 2, '还没有的演员': 0, '标签一': 0}
        assert tags == {'标签一': 3, '4k': 1, '演员乙': 2, '还没有的标签': 0}

    def test_a_movie_with_the_same_value_under_two_types_counts_once(self, make_item):
        make_item('M-001', tags=[('製作商:', '厂牌甲'), ('發行商:', '厂牌甲')])
        make_item('M-002', tags=[('發行商:', '厂牌甲')])
        make_item('M-003', tags=[('製作商:', '厂牌甲')])
        _, tags = pick.library_counts(pick.match_names(chosen(tags=['厂牌甲'])))
        assert tags == {'厂牌甲': 3}

    def test_a_star_is_counted_with_her_other_names(self, make_item, cast):
        like(make_item, 'L-001')
        cast.legacy('L-001', 'jia', '演员甲', '旧名甲')
        make_item('C-001', tags=[('star', '旧名甲')])
        make_item('C-002', tags=[('star', '演员甲')])
        stars, _ = pick.library_counts(pick.match_names(chosen(stars=['演员甲'])))
        assert stars == {'演员甲': 3}

    def test_an_actress_is_counted_once_per_movie_with_all_her_tags(self, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', javbus='/star/a1')
        cast.movie('C-001', ('別の名前', '/star/a1'))
        cast.link('C-001', jia)
        cast.movie('C-002', ('別の名前', '/star/a1'))
        cast.movie('C-003')
        cast.link('C-003', jia)
        stars, _ = pick.library_counts(pick.match_names(chosen(stars=['えんいんこう', '没有的人'])))
        assert stars == {'えんいんこう': 3, '没有的人': 0}

    def test_both_spellings_of_a_name_are_counted_together(self, make_item):
        make_item('M-001', tags=[('genre', '學生'), ('star', '演員甲')])
        make_item('M-002', tags=[('genre', '学生')])
        make_item('M-003', tags=[('genre', '学生'), ('系列:', '學生')])
        for star, tag in (('演员甲', '学生'), ('演員甲', '學生')):
            stars, tags = pick.library_counts(pick.match_names(chosen(stars=[star], tags=[tag])))
            assert (stars, tags) == ({star: 1}, {tag: 3})

    def test_nothing_chosen(self, library):
        assert pick.library_counts(pick.match_names(chosen())) == ({}, {})


class TestLikedChoices:
    def choices(self, settings=None):
        settings = settings or chosen()
        return pick.liked_choices(settings, pick.match_names(settings))

    def test_most_frequent_stars_and_tags_of_liked_movies(self, make_item):
        like(make_item, 'L-001', [('star', '演员甲'), ('genre', '标签一'), ('系列:', '系列甲')])
        like(make_item, 'L-002', [('star', '演员甲'), ('star', '演员乙'), ('genre', '标签一'), ('genre', '标签二')])
        like(make_item, 'L-003', [('star', '演员乙'), ('genre', '标签一')])
        like(make_item, 'L-004', [('star', '演员甲')])
        make_item('D-001', tags=[('star', '演员丙'), ('genre', '标签三')])
        rate('D-001', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        make_item('S-001', tags=[('star', '演员丁'), ('genre', '标签四')])
        rate('S-001', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        make_item('U-001', tags=[('star', '演员戊'), ('genre', '标签五')])
        stars, tags = self.choices()
        assert stars == [('演员甲', 3), ('演员乙', 2)]
        assert tags == [('标签一', 3), ('系列甲', 1), ('标签二', 1)]

    def test_what_is_already_chosen_is_not_offered_again(self, make_item):
        like(make_item, 'L-001', [('star', '演员甲'), ('star', 'Abc'), ('genre', '标签一'), ('genre', '4K')])
        stars, tags = self.choices(chosen(stars=['abc'], tags=['4k']))
        assert stars == [('演员甲', 1)]
        assert tags == [('标签一', 1)]
        stars, tags = self.choices(chosen(stars=['标签一'], tags=['演员甲']))
        assert stars == [('演员甲', 1), ('Abc', 1)]
        assert tags == [('标签一', 1), ('4K', 1)]

    def test_the_two_spellings_of_a_name_are_one_choice(self, make_item):
        like(make_item, 'L-001', [('genre', '學生'), ('star', '演員甲')])
        like(make_item, 'L-002', [('genre', '学生'), ('star', '演员甲')])
        like(make_item, 'L-003', [('genre', '学生')])
        stars, tags = self.choices()
        # 显示先入库的那个写法
        assert stars == [('演員甲', 2)] and tags == [('學生', 3)]
        # 用另一种写法选过的不再列出
        assert self.choices(chosen(stars=['演员甲'], tags=['学生'])) == ([], [])

    def fill(self, make_item, cast, fanhao, slug, names):
        '''一部喜欢的影片，演员名是按旧的存法从 av-wiki 的一个女优页补的（同一页拆出来的名字链接相同）'''
        like(make_item, fanhao)
        cast.legacy(fanhao, slug, *names)

    def test_the_names_of_one_person_take_one_place(self, make_item, cast):
        self.fill(make_item, cast, 'L-001', 'jia', ['演员甲', '演员甲读音', 'jia yanyuan', '旧名甲'])
        self.fill(make_item, cast, 'L-002', 'yi', ['演员乙', '演员乙读音'])
        self.fill(make_item, cast, 'L-003', 'yi', ['演员乙', '演员乙读音'])
        like(make_item, 'L-004', [('star', '演员丙')])
        stars, _ = self.choices()
        # 一个人一格，显示她的第一个名字；次数是带她任何一个名字的喜欢的影片数
        assert stars == [('演员乙', 2), ('演员甲', 1), ('演员丙', 1)]

    def test_a_name_that_came_from_the_site_first_keeps_its_own_place(self, make_item, cast):
        # 名字先从 JavBus 抓到的标签不知道它是哪个女优页的，只按名字本身算
        like(make_item, 'L-001', [('star', '旧名甲')])
        self.fill(make_item, cast, 'L-002', 'jia', ['演员甲', '旧名甲'])
        assert self.choices()[0] == [('旧名甲', 2), ('演员甲', 1)]

    def test_other_names_of_a_chosen_star_are_not_offered(self, make_item, cast):
        self.fill(make_item, cast, 'L-001', 'jia', ['演员甲', '演员甲读音', '旧名甲'])
        assert self.choices()[0] == [('演员甲', 1)]
        for name in ('演员甲', '演员甲读音', '旧名甲'):
            assert self.choices(chosen(stars=[name]))[0] == [], name

    def test_an_actress_takes_one_place_with_all_her_tags(self, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', javbus='/star/a1')
        cast.movie('L-001')
        cast.link('L-001', jia)
        cast.movie('L-002', ('別の名前', '/star/a1'), ('演员乙', '/star/b1'))
        cast.movie('L-003', ('演员乙', '/star/b1'))
        cast.movie('L-004')
        cast.link('L-004', jia)
        for fanhao in ('L-001', 'L-002', 'L-003', 'L-004'):
            rate(fanhao, RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        assert self.choices()[0] == [('演员甲', 3), ('演员乙', 2)]
        for name in ('演员甲', 'えんいんこう', '別の名前'):
            assert self.choices(chosen(stars=[name]))[0] == [('演员乙', 2)], name

    def test_counted_inside_sqlite(self, make_item, sql_folds):
        for n in range(20):
            like(make_item, f'L-{n:03d}', [('star', f'演員{n % 4}'), ('genre', f'標籤{n % 3}'), ('genre', '学生'),
                                          ('系列:', '學生')])
        stars, tags = self.choices()
        assert stars[0] == ('演員0', 5) and tags[0] == ('学生', 20)
        assert sql_folds == []

    def test_the_same_value_under_two_types_is_one_choice(self, make_item):
        like(make_item, 'L-001', [('製作商:', '厂牌甲'), ('發行商:', '厂牌甲'), ('genre', '标签一')])
        like(make_item, 'L-002', [('發行商:', '厂牌甲')])
        assert self.choices()[1] == [('厂牌甲', 2), ('标签一', 1)]

    def test_at_most_twenty_stars_and_thirty_tags(self, make_item):
        tags = [('star', f'演员{n:02d}') for n in range(25)] + [('genre', f'标签{n:02d}') for n in range(35)]
        like(make_item, 'L-001', tags)
        like(make_item, 'L-002', [('star', '演员24'), ('genre', '标签34')])
        stars, tags = self.choices(chosen(stars=['演员00'], tags=['标签00', '标签01']))
        assert len(stars) == 20 and len(tags) == 30
        assert stars[0] == ('演员24', 2) and tags[0] == ('标签34', 2)
        assert stars[1] == ('演员01', 1) and tags[1] == ('标签02', 1)

    def test_nothing_liked_yet(self, library):
        rate('M-001', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        rate('M-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        assert self.choices() == ([], [])


class TestSuggest:
    @pytest.fixture
    def tags(self, make_item):
        make_item('M-001', tags=[('star', '演员甲'), ('genre', '标签一'), ('製作商:', 'Studio One')])
        make_item('M-002', tags=[('star', '演员甲'), ('star', '演员乙'), ('genre', '标签一'), ('系列:', '演员系列')])
        make_item('M-003', tags=[('star', '演员乙'), ('genre', '标签一'), ('發行商:', 'Studio One'),
                                 ('導演:', '导演甲')])
        make_item('M-004', tags=[('star', '演员甲'), ('genre', '标签二')])
        rate('M-004', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)

    def test_stars_by_how_often_they_appear(self, tags):
        assert pick.suggest('star', '') == [{'value': '演员甲', 'type': '演员', 'count': 3},
                                            {'value': '演员乙', 'type': '演员', 'count': 2}]

    def test_star_suggestions_are_only_actresses(self, tags):
        assert pick.suggest('star', '演员') == [{'value': '演员甲', 'type': '演员', 'count': 3},
                                              {'value': '演员乙', 'type': '演员', 'count': 2}]
        assert pick.suggest('star', '标签') == []

    def test_tag_suggestions_cover_every_type(self, tags):
        assert pick.suggest('tag', '演员') == [{'value': '演员甲', 'type': '演员', 'count': 3},
                                             {'value': '演员乙', 'type': '演员', 'count': 2},
                                             {'value': '演员系列', 'type': '系列', 'count': 1}]
        assert pick.suggest('tag', 'studio') == [{'value': 'Studio One', 'type': '制作商', 'count': 1},
                                                {'value': 'Studio One', 'type': '发行商', 'count': 1}]
        assert pick.suggest('tag', '导演') == [{'value': '导演甲', 'type': '导演', 'count': 1}]
        assert pick.suggest('tag', '')[:3] == [{'value': '演员甲', 'type': '演员', 'count': 3},
                                              {'value': '标签一', 'type': '类别', 'count': 3},
                                              {'value': '演员乙', 'type': '演员', 'count': 2}]

    def test_text_in_either_spelling_finds_the_stored_value(self, make_item):
        make_item('M-001', tags=[('genre', '女大學生'), ('star', '演員甲'), ('genre', '気持ち')])
        make_item('M-002', tags=[('genre', '女大學生'), ('genre', '学生服')])
        for text in ('学生', '學生', ' 學生 ', '学'):
            assert pick.suggest('tag', text) == [{'value': '女大學生', 'type': '类别', 'count': 2},
                                                 {'value': '学生服', 'type': '类别', 'count': 1}], text
        assert [row['value'] for row in pick.suggest('tag', '大学')] == ['女大學生']
        assert pick.suggest('star', '演员') == [{'value': '演員甲', 'type': '演员', 'count': 1}]
        # 日文写法不转换
        assert pick.suggest('tag', '气') == []
        assert [row['value'] for row in pick.suggest('tag', '気')] == ['気持ち']

    def test_text_is_matched_anywhere_ignoring_case_and_blanks_around(self, tags):
        assert [row['value'] for row in pick.suggest('tag', ' DIO o ')] == ['Studio One', 'Studio One']
        assert [row['value'] for row in pick.suggest('star', '乙')] == ['演员乙']
        assert pick.suggest('star', '没有这个名字') == []

    def test_at_most_twenty(self, make_item):
        for n in range(25):
            make_item(f'M-{n:03d}', tags=[('star', f'演员{n:02d}')] + ([('star', '常见演员')] if n % 2 else []))
        rows = pick.suggest('star', '演员')
        assert len(rows) == 20
        assert rows[0] == {'value': '常见演员', 'type': '演员', 'count': 12}
        assert [row['value'] for row in rows[1:4]] == ['演员00', '演员01', '演员02']

    @pytest.mark.parametrize('text, expected', [('%', ['100%纯']), ('_', ['下划_线']), ('\\', ['反斜\\杠']),
                                                ("'", ["单'引号"]), ('<', ['<b>粗</b>'])])
    def test_wildcards_and_quotes_are_plain_text(self, make_item, text, expected):
        make_item('M-001', tags=[('genre', value) for value in ('100%纯', '下划_线', '反斜\\杠', "单'引号", '<b>粗</b>',
                                                                   '普通标签')])
        assert [row['value'] for row in pick.suggest('tag', text)] == expected

    def test_aliases_are_suggested_and_say_whose(self, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', 'Actor A')
        yi = cast.actress('演员乙', 'yi', 'えんいんこう')
        cast.actress('演员丙', 'bing', 'えんいんへい')
        for fanhao, actress in (('C-001', jia), ('C-002', jia), ('C-003', yi)):
            cast.movie(fanhao)
            cast.link(fanhao, actress)
        assert pick.suggest('star', 'えんいん') == [
            {'value': 'えんいんこう', 'type': '演员', 'count': 3, 'alias_of': ['演员甲', '演员乙']}]
        assert pick.suggest('star', ' ACTOR ') == [{'value': 'Actor A', 'type': '演员', 'count': 2, 'alias_of': ['演员甲']}]
        assert pick.suggest('star', '演员') == [{'value': '演员甲', 'type': '演员', 'count': 2},
                                              {'value': '演员乙', 'type': '演员', 'count': 1}]
        # 标签的联想照旧只有标签
        assert pick.suggest('tag', 'えんいん') == []

    def test_an_alias_that_is_also_a_name_is_suggested_once(self, cast):
        jia = cast.actress('演员甲', 'jia')
        yi = cast.actress('演员乙', 'yi', '演员甲')
        for fanhao, actress in (('C-001', jia), ('C-002', yi)):
            cast.movie(fanhao)
            cast.link(fanhao, actress)
        assert pick.suggest('star', '演员甲') == [
            {'value': '演员甲', 'type': '演员', 'count': 2, 'also_alias_of': ['演员乙']}]

    def test_her_own_spelling_kept_beside_a_javbus_name_is_not_suggested_as_her_alias(self, cast):
        # 正式名字是 JavBus 演员页上的「演員丙」，别名里留着她自己的写法「演员丙」：联想里她只出现一次，不算她自己的别名
        cast.movie('JB-001', ('演員丙', '/star/z1'))
        bing = cast.actress('演员丙', 'bing', 'へい')
        for fanhao in ('W-001', 'W-002'):
            cast.movie(fanhao)
            cast.link(fanhao, bing)
            db.save_lookup(fanhao, db.LOOKUP_FILLED, 0, NOW, NOW)
        with db.db.atomic('IMMEDIATE'):
            actresses.found_javbus('avwiki:/av-actress/bing/', '/star/z1', '演員丙')
        assert [row.name for row in db.ActressAlias.select().where(db.ActressAlias.actress == bing.id)
                .order_by(db.ActressAlias.id)] == ['演员丙', 'へい']
        for text in ('演', '演员丙', '演員丙'):
            assert pick.suggest('star', text) == [{'value': '演員丙', 'type': '演员', 'count': 3}], text

    def test_her_number_kept_while_she_goes_by_a_javbus_name_is_not_suggested(self, cast):
        # 关联期间她的「演员甲（2）」留着只为认人：联想里她只以正式名字出现一次，部数不翻倍
        linked_second_actress(cast)
        for text in ('演员甲', '演員甲'):
            assert pick.suggest('star', text) == [{'value': '演員甲', 'type': '演员', 'count': 3}], text
        assert pick.suggest('star', '（2）') == []

    def test_a_number_an_earlier_version_kept_as_an_alias_is_not_suggested(self, cast):
        jia = cast.actress('同名', 'jia')
        second = cast.actress('同名（3）', 'second', '同名', 'どうめいに', '同名（2）')
        for fanhao, actress in (('C-001', jia), ('C-002', second)):
            cast.movie(fanhao)
            cast.link(fanhao, actress)
        assert pick.suggest('star', '同名') == [
            {'value': '同名', 'type': '演员', 'count': 2, 'also_alias_of': ['同名（3）']},
            {'value': '同名（3）', 'type': '演员', 'count': 1}]

    def test_many_aliases_go_through_a_few_queries_with_a_few_parameters(self, cast, queries):
        # 一个字母能对上几千条别名：不把几千个值逐个当参数传，也不把它们全取回来再数
        for n in range(60):
            actress = cast.actress(f'演员{n:02d}', f'p{n}', f'Joyu {n:02d}')
            for m in range(1 + n % 3):
                cast.movie(f'C-{n:03d}-{m}')
                cast.link(f'C-{n:03d}-{m}', actress)
        queries.clear()
        rows = pick.suggest('star', 'j')
        assert len(rows) == 20
        assert all(row['count'] == 3 for row in rows) and rows[0] == {
            'value': 'Joyu 02', 'type': '演员', 'count': 3, 'alias_of': ['演员02']}
        assert len(queries) <= 3 and max(len(params) for _, params in queries) <= 3

    def test_an_alias_several_actresses_share_is_counted_whole_before_the_limit(self, cast, monkeypatch):
        # 三位女优共用读音「じょゆう」（各 1 部）；另外三位各有一个只属于她的读音（各 2 部）。
        # 取前几个别名之前先按别名合并：共用的那个一共 3 部，排在最前，三位主人都列出来
        monkeypatch.setattr(pick, 'MAX_ALIAS_SUGGESTIONS', 3)
        for n in range(3):
            actress = cast.actress(f'演员{n}', f'p{n}', 'じょゆう')
            cast.movie(f'S-{n}')
            cast.link(f'S-{n}', actress)
        for n in range(3):
            actress = cast.actress(f'女优{n}', f'q{n}', f'じょゆう{n}')
            for m in range(2):
                cast.movie(f'Q-{n}-{m}')
                cast.link(f'Q-{n}-{m}', actress)
        assert pick.suggest('star', 'じょ') == [
            {'value': 'じょゆう', 'type': '演员', 'count': 3, 'alias_of': ['演员0', '演员1', '演员2']},
            {'value': 'じょゆう0', 'type': '演员', 'count': 2, 'alias_of': ['女优0']},
            {'value': 'じょゆう1', 'type': '演员', 'count': 2, 'alias_of': ['女优1']}]

    def test_a_listed_name_still_shows_whose_alias_it_is_beyond_the_alias_limit(self, cast):
        for n in range(pick.MAX_ALIAS_SUGGESTIONS + 20):
            actress = cast.actress(f'演员{n:03d}', f'p{n}', f'Joyu {n:03d}')
            for m in range(2):
                cast.movie(f'C-{n:03d}-{m}')
                cast.link(f'C-{n:03d}-{m}', actress)
        few = cast.actress('演员乙', 'yi', 'jj')
        cast.movie('Y-001')
        cast.link('Y-001', few)
        for n in range(5):
            cast.movie(f'J-{n:03d}', ('jj', '/star/jj'))
        rows = pick.suggest('star', 'j')
        assert len(rows) == pick.MAX_SUGGESTIONS
        assert rows[0] == {'value': 'jj', 'type': '演员', 'count': 6, 'also_alias_of': ['演员乙']}

    def test_text_longer_than_a_name_can_be_finds_nothing(self, tags):
        assert pick.suggest('tag', '标' * 201) == []
        assert pick.suggest('tag', None) == pick.suggest('tag', '')

    def test_unknown_kind_is_refused(self, tags):
        with pytest.raises(ValueError):
            pick.suggest('genre', '标签')
