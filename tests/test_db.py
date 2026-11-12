'''
数据层：入库、查询分页、按评分过滤、本地影片、今日计数，以及与 2019 年数据库的兼容
'''
import datetime
import json
import logging
import math
import random
import sqlite3
import threading
import time
from string import ascii_lowercase, ascii_uppercase

import pytest
from peewee import Case, OperationalError, fn

from bustag import hanzi, util
from bustag.spider import db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate, LocalItem
from bustag.spider.parser import Tag

# 2019 年 bustag 生成的 bus.db 里 sqlite_master 的全部建表、建索引语句
SCHEMA_2019 = [
    'CREATE TABLE "item" ("id" INTEGER NOT NULL PRIMARY KEY, "title" VARCHAR(255) NOT NULL, '
    '"fanhao" VARCHAR(255) NOT NULL, "url" VARCHAR(255) NOT NULL, "release_date" DATE NOT NULL, '
    '"add_date" DATETIME NOT NULL, "meta_info" TEXT NOT NULL)',
    'CREATE UNIQUE INDEX "item_fanhao" ON "item" ("fanhao")',
    'CREATE TABLE "item_rate" ("id" INTEGER NOT NULL PRIMARY KEY, "rate_type" INTEGER NOT NULL, '
    '"rate_value" INTEGER NOT NULL, "item_id" VARCHAR(255) NOT NULL, "rete_time" DATETIME NOT NULL, '
    'FOREIGN KEY ("item_id") REFERENCES "item" ("fanhao"))',
    'CREATE UNIQUE INDEX "item_rate_item_id" ON "item_rate" ("item_id")',
    'CREATE TABLE "item_tag" ("id" INTEGER NOT NULL PRIMARY KEY, "item_id" VARCHAR(255) NOT NULL, '
    '"tag_id" INTEGER NOT NULL, FOREIGN KEY ("item_id") REFERENCES "item" ("fanhao"), '
    'FOREIGN KEY ("tag_id") REFERENCES "tag" ("id"))',
    'CREATE INDEX "item_tag_item_id" ON "item_tag" ("item_id")',
    'CREATE UNIQUE INDEX "item_tag_item_id_tag_id" ON "item_tag" ("item_id", "tag_id")',
    'CREATE INDEX "item_tag_tag_id" ON "item_tag" ("tag_id")',
    'CREATE UNIQUE INDEX "item_url" ON "item" ("url")',
    'CREATE TABLE "local_item" ("id" INTEGER NOT NULL PRIMARY KEY, "item_id" VARCHAR(255) NOT NULL, '
    '"path" VARCHAR(255), "size" INTEGER, "add_date" DATETIME NOT NULL, "last_view_date" DATETIME, '
    '"view_times" INTEGER NOT NULL, FOREIGN KEY ("item_id") REFERENCES "item" ("fanhao"))',
    'CREATE UNIQUE INDEX "local_item_item_id" ON "local_item" ("item_id")',
    'CREATE TABLE "tag" ("id" INTEGER NOT NULL PRIMARY KEY, "type" VARCHAR(255) NOT NULL, '
    '"value" VARCHAR(255) NOT NULL, "url" VARCHAR(255) NOT NULL)',
    'CREATE UNIQUE INDEX "tag_type_value" ON "tag" ("type", "value")',
]
# "没有等满"的界限：等满 30 秒（或者两次）那种的一半，慢的机器、忙的时候也到不了，到了就是又在等满
NOT_THE_FULL_WAIT = db.BUSY_TIMEOUT / 2
# 等后台线程走到某一步最多等这么久：只是防止一直卡住，慢的机器上也绰绰有余
PATIENCE = 60


def meta(fanhao, **extra):
    return {'fanhao': fanhao, 'title': f'作品 {fanhao}', 'url': '/' + fanhao,
            'release_date': '2026-01-01', 'cover_img_url': '/pics/cover/x_b.jpg', 'length': '120',
            **extra}


def rate(fanhao, rate_type, rate_value):
    assert ItemRate.saveit(rate_type, rate_value, fanhao) is not None


class TestSave:
    def test_save_stores_paths_and_meta(self):
        tags = [Tag('genre', '斡瀠鰉', 'https://www.javbus.com/genre/4o'), Tag('star', 's1', '/star/1')]
        item = db.save(meta('ABC-001', url='https://www.javbus.com/ABC-001'), tags)
        item = db.Item.get_by_fanhao('ABC-001')
        assert item.url == '/ABC-001'
        assert item.title == '作品 ABC-001'
        assert item.release_date == datetime.date(2026, 1, 1)
        assert json.loads(item.meta_info) == {'cover_img_url': '/pics/cover/x_b.jpg', 'length': '120'}
        assert sorted((t.tag.type_, t.tag.value, t.tag.url) for t in item.tags_list) == [
            ('genre', '斡瀠鰉', '/genre/4o'), ('star', 's1', '/star/1')]

    def test_tags_are_shared_between_movies(self, make_item):
        make_item('ABC-001', tags=[('genre', 'g1'), ('star', 's1')])
        make_item('ABC-002', tags=[('genre', 'g1')])
        assert db.Tag.select().count() == 2
        assert db.ItemTag.select().count() == 3

    def test_duplicate_fanhao_or_url_is_ignored(self, make_item):
        make_item('ABC-001', tags=[('genre', 'g1')])
        assert db.save(meta('ABC-001', title='again'), [Tag('genre', 'g2', '/genre/2')]) is None
        assert db.save(meta('ABC-999', url='/ABC-001'), []) is None
        assert db.Item.select().count() == 1
        assert db.Tag.select().count() == 1
        assert db.Item.get_by_fanhao('ABC-001').title == '作品 ABC-001'

    def test_rate_is_not_overwritten(self, make_item):
        make_item('ABC-001')
        rate('ABC-001', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        assert ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, 'ABC-001') is None
        stored = ItemRate.get_by_fanhao('ABC-001')
        assert (stored.rate_type, stored.rate_value) == (RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)


def stored_rate(fanhao):
    item_rate = ItemRate.get_by_fanhao(fanhao)
    return item_rate.rate_type, item_rate.rate_value


class TestSaveUserRate:
    NOW = datetime.datetime(2026, 11, 7, 9, 30, 0)

    def test_new_rate(self, make_item):
        make_item('ABC-001')
        assert ItemRate.save_user_rate('ABC-001', RATE_VALUE.LIKE) == 'added'
        assert stored_rate('ABC-001') == (RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)

    @pytest.mark.parametrize('replace_user_rate', [False, True])
    def test_system_rate_becomes_user_rate(self, make_item, monkeypatch, replace_user_rate):
        monkeypatch.setattr(db, 'get_now_time', lambda: self.NOW)
        make_item('ABC-001')
        ItemRate.create(item='ABC-001', rate_type=RATE_TYPE.SYSTEM_RATE, rate_value=RATE_VALUE.LIKE,
                        rete_time=datetime.datetime(2026, 1, 1))
        outcome = ItemRate.save_user_rate('ABC-001', RATE_VALUE.DISLIKE, replace_user_rate=replace_user_rate)
        assert outcome == 'updated'
        assert stored_rate('ABC-001') == (RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        assert ItemRate.get_by_fanhao('ABC-001').rete_time == self.NOW
        assert ItemRate.select().count() == 1

    def test_user_rate_is_kept_unless_replacing(self, make_item):
        make_item('ABC-001')
        rate('ABC-001', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        assert ItemRate.save_user_rate('ABC-001', RATE_VALUE.LIKE) == 'kept'
        assert stored_rate('ABC-001') == (RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        # 改的是用户自己打的标，和"系统评分改成用户打标"（updated）分开说
        assert ItemRate.save_user_rate('ABC-001', RATE_VALUE.LIKE, replace_user_rate=True) == 'changed'
        assert stored_rate('ABC-001') == (RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        assert ItemRate.select().count() == 1

    @pytest.mark.parametrize('rate_value', [RATE_VALUE.LIKE, RATE_VALUE.DISLIKE])
    def test_a_movie_the_user_tagged_no_longer_has_a_score(self, make_item, rate_value):
        for fanhao in ('ABC-001', 'ABC-002', 'ABC-003'):
            make_item(fanhao)
        db.save_scores([db.Score('ABC-003', 0.9, ['标签一'], []), db.Score('ABC-002', 0.8, [], []),
                        db.Score('ABC-001', 0.2, [], ['标签二'])], lambda total: 2)
        assert db.count_scores(0.5) == (3, 2)
        assert ItemRate.save_user_rate('ABC-003', rate_value) == 'updated'
        assert sorted(row.item_id for row in db.ItemScore.select()) == ['ABC-001', 'ABC-002']
        # 统计"打过分的影片"时不再把它算进去
        assert db.count_scores(0.5) == (2, 1)

    def test_a_score_left_on_a_movie_the_user_tagged_earlier_goes_when_it_is_tagged_again(self, make_item):
        make_item('ABC-001')
        rate('ABC-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        db.ItemScore.create(item='ABC-001', score=0.7, because='[]', against='[]')
        assert ItemRate.save_user_rate('ABC-001', RATE_VALUE.DISLIKE) == 'kept'
        assert db.ItemScore.select().count() == 0

    def test_the_rate_and_the_dropped_score_are_written_together_or_not_at_all(self, make_item, monkeypatch):
        make_item('ABC-001')
        db.save_scores([db.Score('ABC-001', 0.9, [], [])], lambda total: 0)

        def fails(*args, **kwargs):
            raise RuntimeError('写到一半出错')

        monkeypatch.setattr(ItemRate, 'save', fails)
        with pytest.raises(RuntimeError):
            ItemRate.save_user_rate('ABC-001', RATE_VALUE.LIKE)
        assert stored_rate('ABC-001') == (RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)
        assert db.ItemScore.select().count() == 1


class TestDislikeUntagged:
    '''整页标不喜欢：只标还不是用户亲手打标的影片'''
    OLD = datetime.datetime(2026, 11, 1, 8, 0, 0)

    def test_untagged_and_model_rated_movies_become_user_dislikes(self, make_item):
        for fanhao in ('A-001', 'A-002', 'A-003', 'A-004'):
            make_item(fanhao)
        rate('A-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        rate('A-003', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)
        db.ItemScore.create(item='A-002', score=0.9, because='[]', against='[]')
        result = db.dislike_untagged(['A-001', 'A-002', 'A-003'])
        assert result == (['A-001', 'A-002', 'A-003'], 0, 0)
        assert (result.tagged, result.kept, result.unknown) == (['A-001', 'A-002', 'A-003'], 0, 0)
        assert [stored_rate(fanhao) for fanhao in ('A-001', 'A-002', 'A-003')] == [
            (RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)] * 3
        assert ItemRate.get_by_fanhao('A-004') is None
        assert db.ItemScore.select().count() == 0

    def test_movies_the_user_tagged_are_kept_as_they_are(self, make_item):
        for fanhao in ('A-001', 'A-002', 'A-003'):
            make_item(fanhao)
        ItemRate.create(item='A-001', rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.LIKE, rete_time=self.OLD)
        ItemRate.create(item='A-002', rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.DISLIKE, rete_time=self.OLD)
        assert db.dislike_untagged(['A-001', 'A-002', 'A-003']) == (['A-003'], 2, 0)
        kept = [(row.item_id, row.rate_value, row.rete_time) for row in ItemRate.select().order_by(ItemRate.item)][:2]
        assert kept == [('A-001', RATE_VALUE.LIKE, self.OLD), ('A-002', RATE_VALUE.DISLIKE, self.OLD)]

    def test_codes_that_are_not_in_the_library_get_no_rate(self, make_item):
        make_item('A-001')
        assert db.dislike_untagged(['GONE-001', 'A-001', 'GONE-002']) == (['A-001'], 0, 2)
        assert [row.item_id for row in ItemRate.select()] == ['A-001']

    def test_nothing_to_tag(self):
        assert db.dislike_untagged([]) == ([], 0, 0)

    def test_all_or_nothing(self, make_item, monkeypatch):
        make_item('A-001')
        make_item('A-002')
        real_save = ItemRate.save_user_rate

        def second_one_fails(fanhao, rate_value, replace_user_rate=False):
            if fanhao == 'A-002':
                raise RuntimeError('写到一半出错')
            return real_save(fanhao, rate_value, replace_user_rate)

        monkeypatch.setattr(ItemRate, 'save_user_rate', staticmethod(second_one_fails))
        with pytest.raises(RuntimeError):
            db.dislike_untagged(['A-001', 'A-002'])
        assert ItemRate.select().count() == 0


class TestQuery:
    def test_a_page_holds_ten_movies(self):
        assert db.PAGE_SIZE == 10

    def test_pagination_newest_first(self, make_item):
        with db.db.atomic():
            for n in range(1, 26):
                make_item(f'ABC-{n:03d}')
        items, page_info = db.get_items(page=1)
        assert page_info == (25, 3, 1, 10)
        assert [item.fanhao for item in items] == [f'ABC-{n:03d}' for n in range(25, 15, -1)]
        items, page_info = db.get_items(page=3)
        assert page_info == (25, 3, 3, 10)
        assert [item.fanhao for item in items] == [f'ABC-{n:03d}' for n in range(5, 0, -1)]

    @pytest.mark.parametrize('page', [4, 10 ** 12])
    def test_page_after_the_last_returns_the_last_page(self, make_item, page):
        with db.db.atomic():
            for n in range(1, 26):
                make_item(f'ABC-{n:03d}')
        items, page_info = db.get_items(page=page)
        assert page_info == (25, 3, 3, 10)
        assert [item.fanhao for item in items] == [f'ABC-{n:03d}' for n in range(5, 0, -1)]

    @pytest.mark.parametrize('page', [1, 5])
    def test_empty_database(self, page):
        assert db.get_items(page=page) == ([], (0, 0, 1, 10))

    def test_filter_by_rate(self, make_item):
        for fanhao in ('A-001', 'A-002', 'A-003', 'A-004'):
            make_item(fanhao)
        rate('A-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        rate('A-002', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        rate('A-003', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)

        def fanhaos(*args):
            return [item.fanhao for item in db.get_items(*args, page=1)[0]]

        assert fanhaos() == ['A-004']
        assert fanhaos(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE) == ['A-001']
        assert fanhaos(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE) == ['A-002']
        assert fanhaos(RATE_TYPE.USER_RATE, None) == ['A-002', 'A-001']
        assert fanhaos(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE) == ['A-003']
        assert db.get_items(page=1)[0][0].rate_value is None
        assert db.get_items(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, page=1)[0][0].rate_value == 1

    def test_loaded_items_have_display_fields(self, make_item, monkeypatch):
        make_item('ABC-001', cover='https://pics.javcdn.pw/cover/7by5_b.jpg',
                  tags=[('genre', 'g1'), ('genre', 'g2'), ('star', 's1')])
        monkeypatch.setitem(util.APP_CONFIG, 'download.root_path', 'https://www.busjav.casa')
        item = db.get_items(page=1)[0][0]
        assert item.url == 'https://www.busjav.casa/ABC-001'
        assert item.cover_img_url == '/pics/cover/7by5_b.jpg'
        assert len(item.add_date) == len('2026-01-01 00:00:00')
        assert dict(item.tags_dict) == {'genre': ['g1', 'g2'], 'star': ['s1']}

    def test_loadit_tolerates_empty_meta(self, make_item):
        item = make_item('ABC-001')
        item.meta_info = ''
        db.Item.loadit(item)
        assert item.cover_img_url is None


class TestKeyword:
    '''带搜索词的列表：番号、标题、标签值，简体繁体互通'''

    def fanhaos(self, keyword, **options):
        items, page_info = db.get_items(keyword=keyword, **options)
        return [item.fanhao for item in items], page_info

    def test_title_and_tag_values_match_in_either_spelling(self, make_item):
        make_item('A-001', title='學園', tags=[('genre', '膿燈作瓰')])
        make_item('A-002', title='学园', tags=[('genre', '脓灯作瓰')])
        make_item('A-003', title='别的', tags=[('star', '演员甲')])
        for keyword in ('学园', '學園', '脓灯', '膿燈', ' 膿灯作瓰 '):
            assert self.fanhaos(keyword) == (['A-002', 'A-001'], (2, 1, 1, 10)), keyword
        assert self.fanhaos('演員') == (['A-003'], (1, 1, 1, 10))

    def test_any_name_of_an_actress_finds_her_movies(self, cast, make_item):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', 'Actor A', javbus='/star/a1')
        cast.movie('A-001')
        cast.link('A-001', jia)
        cast.movie('A-002', ('別の名前', '/star/a1'))
        cast.movie('A-003', ('演员乙', '/star/b1'))
        make_item('A-004', title='えんいんこう 特集')
        for keyword in ('えんいん', 'actor', ' ACTOR A ', '演員甲', '別の'):
            assert sorted(self.fanhaos(keyword)[0]) == ['A-001', 'A-002'] + (['A-004'] if 'えん' in keyword else []), \
                keyword
        assert self.fanhaos('演员乙')[0] == ['A-003']

    def test_an_alias_shared_by_two_finds_both(self, cast):
        jia = cast.actress('演员甲', 'jia', '共用读音')
        yi = cast.actress('演员乙', 'yi', '共用读音')
        for fanhao, actress in (('A-001', jia), ('A-002', yi)):
            cast.movie(fanhao)
            cast.link(fanhao, actress)
        cast.movie('A-003')
        assert self.fanhaos('共用')[0] == ['A-002', 'A-001']

    def test_a_search_by_alias_hands_nothing_but_the_word_to_python(self, cast, sql_folds, monkeypatch):
        jia = cast.actress('演員甲', 'jia', 'えんいんこう')
        for n in range(20):
            cast.movie(f'A-{n:03d}', (f'演員{n}', f'/star/s{n}'))
        cast.link('A-001', jia)
        folded = []
        fold = db.fold

        def counting(text):
            folded.append(text)
            return fold(text)

        monkeypatch.setattr(db, 'fold', counting)
        assert self.fanhaos('えんいん')[0] == ['A-001']
        assert folded == ['えんいん'] and sql_folds == []

    def test_the_actresses_a_word_hits_are_found_before_the_list_is_searched(self, cast, queries):
        # 碰到的女优先单独查出来，列表的查询里不嵌着找女优的几层子查询：嵌着的话 SQLite 会把它们算好几遍
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', javbus='/star/a1')
        cast.movie('A-001')
        cast.link('A-001', jia)
        cast.movie('A-002', ('別の名前', '/star/a1'))
        cast.movie('A-003', ('演员乙', '/star/b1'))
        queries.clear()
        assert sorted(self.fanhaos('えんいん')[0]) == ['A-001', 'A-002']
        listing = [sql for sql, _ in queries if 'FROM "item" ' in sql]
        assert listing and not any('actress' in sql for sql in listing)

    def test_wildcards_in_the_word_are_plain_text(self, make_item):
        make_item('A-001', title='100%', tags=[('genre', '下划_线')])
        make_item('A-002', title='1000', tags=[('genre', '下划线')])
        assert self.fanhaos('0%')[0] == ['A-001']
        assert self.fanhaos('划_')[0] == ['A-001']

    CODES = ['ABP-001', 'ABP-002', 'ABP-010', 'ABP-010_2026-01-01', 'XYZ-100', '300MIUM-1506', 'T28-123', 'QWVS-123']

    @pytest.fixture
    def codes(self, make_item):
        for fanhao in self.CODES:
            make_item(fanhao, title='別の作品')

    @pytest.mark.parametrize('keyword, found', [
        # 小写、不带横线、带横线、日期后缀（写全或者写了一半）都认
        ('abp001', ['ABP-001']), ('ABP-001', ['ABP-001']), ('abp-001', ['ABP-001']), (' abp-001 ', ['ABP-001']),
        ('abp-010', ['ABP-010', 'ABP-010_2026-01-01']), ('abp010', ['ABP-010', 'ABP-010_2026-01-01']),
        ('abp-010_2026-01-01', ['ABP-010', 'ABP-010_2026-01-01']), ('abp-010_2026', ['ABP-010', 'ABP-010_2026-01-01']),
        ('300mium-1506', ['300MIUM-1506']), ('300mium1506', ['300MIUM-1506']), ('mium-1506', ['300MIUM-1506']),
        ('mium1506', ['300MIUM-1506']), ('t28-123', ['T28-123']),
        # 只写开头几位也行
        ('abp', ['ABP-001', 'ABP-002', 'ABP-010', 'ABP-010_2026-01-01']),
        ('ab', ['ABP-001', 'ABP-002', 'ABP-010', 'ABP-010_2026-01-01']),
        ('abp-0', ['ABP-001', 'ABP-002', 'ABP-010', 'ABP-010_2026-01-01']),
        ('abp0', ['ABP-001', 'ABP-002', 'ABP-010', 'ABP-010_2026-01-01']),
        ('abp-01', ['ABP-010', 'ABP-010_2026-01-01']),
        ('300mi', ['300MIUM-1506']),
    ])
    def test_a_code_in_any_of_its_spellings_finds_the_movie(self, codes, keyword, found):
        assert sorted(self.fanhaos(keyword)[0]) == sorted(found), keyword

    @pytest.mark.parametrize('keyword', ['s1あいう', 'S1あいう', 's1 あいう', 'abp-001学生', 'abp001 学生', 'qwvs-123の',
                                         's1abc', 'x-1あ', '「s1」'])
    def test_only_a_whole_word_that_looks_like_a_code_is_searched_as_one(self, make_item, codes, keyword):
        # QWVS-123 去掉横线是 QWVS123，里面有 "S1"：词里只是夹着一段像番号的字母数字，不能因此把它带出来
        make_item('TXT-001', title=f'特集 {keyword} 版')
        assert self.fanhaos(keyword)[0] == ['TXT-001'], keyword

    def test_pages_and_newest_first_stay_the_same(self, make_item):
        with db.db.atomic():
            for n in range(1, 26):
                make_item(f'A-{n:03d}', tags=[('genre', '學生')] if n % 2 else [('genre', '别的')])
        assert self.fanhaos('学生') == ([f'A-{n:03d}' for n in range(25, 5, -2)], (13, 2, 1, 10))
        assert self.fanhaos('学生', page=2) == (['A-005', 'A-003', 'A-001'], (13, 2, 2, 10))
        assert self.fanhaos('学生', page=9) == (['A-005', 'A-003', 'A-001'], (13, 2, 2, 10))
        assert self.fanhaos('没有这个词', page=3) == ([], (0, 0, 1, 10))

    def test_a_search_hands_nothing_but_the_word_to_python(self, make_item, monkeypatch):
        with db.db.atomic():
            for n in range(1, 31):
                make_item(f'A-{n:03d}', tags=[('genre', f'標籤{n % 5}'), ('star', f'演員{n % 3}'), ('genre', '共用')])
        folded = []
        fold = db.fold

        def counting(text):
            folded.append(text)
            return fold(text)

        monkeypatch.setattr(db, 'fold', counting)
        assert len(self.fanhaos('标签3')[0]) == 6
        # 标题和标签值是在 SQLite 里直接比的（contains_pattern），不是一行一行折了再比：
        # 几个线程同时搜索时，每一行都从 SQLite 调回 Python 会让它们互相等解释器锁，比一个一个搜还慢
        assert folded == ['标签3']


class TestManyValuesAndRows:
    '''几千个值的 IN、几千行的结果：一个参数、一次取回（几个请求同时进行时不互相等解释器锁）'''

    def test_in_values_takes_more_values_than_old_sqlite_allowed_parameters(self, make_item):
        make_item('ABC-001', tags=[('genre', '标签一')])
        wanted = ['标签一'] + [f'没有的标签{n}' for n in range(2000)]
        condition = db.in_values(db.Tag.value, wanted)
        sql, params = db.Tag.select().where(condition).sql()
        assert len(params) == 1
        assert [tag.value for tag in db.Tag.select().where(condition)] == ['标签一']
        assert db.in_values(db.Tag.value, []) is None

    def test_fetch_rows_gives_the_same_values_as_reading_row_by_row(self, make_item):
        texts = ['', '"引号"', '\\反斜杠', '\\"', '制表\t换行\n回车\r', '\x01\x1f\x7f', '空\x00字符', '表情😀🎉',
                 '{"像": [1]}', 'null', '1e5', ' 两头空白 ', '\u2028\u2029']
        for n, text in enumerate(texts):
            db.JobState.create(name=f'k{n}', text=text)
        for n, value in enumerate([2 ** 63 - 1, -2 ** 63, 2 ** 53 + 1, 0, None]):
            db.ListSource.create(path=f'/star/x{n}', magnets=value)
        for n, value in enumerate([0.1 + 0.2, 1e-300, 1.7976931348623157e308, 123456789.123456789, 5e-324, 1.0]):
            make_item(f'ABC-{n:03d}')
            db.ItemScore.create(item=f'ABC-{n:03d}', score=value, because='[]', against='[]')
        for query, columns in ((db.JobState.select(db.JobState.id, db.JobState.text), ('id', 'text')),
                               (db.ListSource.select(db.ListSource.id, db.ListSource.magnets), ('id', 'magnets')),
                               (db.ItemScore.select(db.ItemScore.id, db.ItemScore.score), ('id', 'score'))):
            fetched, direct = sorted(db.fetch_rows(query, *columns)), sorted(query.tuples())
            assert fetched == direct, columns
            assert [[type(value) for value in row] for row in fetched] == [[type(value) for value in row]
                                                                           for row in direct], columns

    def test_fetch_rows_gives_back_negative_zero_as_zero(self, make_item):
        make_item('ABC-001')
        db.ItemScore.create(item='ABC-001', score=-0.0, because='[]', against='[]')
        (score,), = db.fetch_rows(db.ItemScore.select(db.ItemScore.score), 'score')
        assert score == 0.0 and math.copysign(1, score) == 1.0

    def test_fetch_rows_does_not_take_blob_values(self):
        db.JobState.create(name='k', text='')
        with pytest.raises(OperationalError, match='BLOB'):
            db.fetch_rows(db.JobState.select(fn.zeroblob(2).alias('data')), 'data')

    def test_fetch_rows_gives_back_everyday_reals_exactly_and_huge_and_tiny_ones_to_the_last_digit(self):
        from peewee import FloatField, chunked

        class Real(db.BaseModel):
            value = FloatField()

        rng = random.Random(20261112)
        everyday = [rng.uniform(1, 10) * 10.0 ** exponent
                    for exponent in (-60, -20, -7, -4, -1, 0, 3, 6, 9, 12, 15, 16, 17, 40, 100) for _ in range(200)]
        huge_and_tiny = [rng.uniform(1, 10) * 10.0 ** exponent for exponent in (150, 300, 307, -150, -300, -307)
                         for _ in range(200)]
        # 数据库文件整个会话只建一次，这张表用完要删掉
        db.db.create_tables([Real])
        try:
            for batch in chunked(everyday + huge_and_tiny, 500):
                Real.insert_many([{'value': value} for value in batch]).execute()
            query = Real.select(Real.id, Real.value)
            fetched, direct = dict(db.fetch_rows(query, 'id', 'value')), dict(query.tuples())
        finally:
            db.db.drop_tables([Real])
        rows = range(1, len(everyday) + 1)
        assert [fetched[n] for n in rows] == everyday == [direct[n] for n in rows]
        assert all(abs(fetched[n] - direct[n]) <= abs(direct[n]) * 1e-15 for n in direct)

    def test_fetch_rows_gives_every_row_as_a_tuple(self, make_item):
        make_item('ABC-001', tags=[('genre', '标签一'), ('star', '演员甲')])
        query = db.Tag.select(db.Tag.id, db.Tag.value, (db.Tag.type_ == 'star').alias('star')).order_by(db.Tag.id)
        assert sorted(db.fetch_rows(query, 'id', 'value', 'star')) == sorted(query.tuples())
        assert db.fetch_rows(query.where(db.Tag.id == 0), 'id', 'value', 'star') == []


class TestJsonFunctions:
    '''几条大查询要用 SQLite 的 JSON 函数（3.38 起自带）：启动时先看一眼，没有就说清楚、退出，不等某个页面出错'''

    @pytest.fixture
    def without_json(self, monkeypatch):
        '''Python 带的是没有 JSON 函数的 SQLite：内存库里调 JSON 函数就出错，别的连接照旧'''
        real = sqlite3.connect

        class Old:
            def __init__(self):
                self.connection = real(':memory:')

            def execute(self, sql, *args):
                if 'json' in sql:
                    raise sqlite3.OperationalError('no such function: json_array')
                return self.connection.execute(sql, *args)

            def close(self):
                self.connection.close()

        monkeypatch.setattr(db.sqlite3, 'connect',
                            lambda database, *args, **kwargs: Old() if database == ':memory:' else real(
                                database, *args, **kwargs))
        monkeypatch.setattr(db.sqlite3, 'sqlite_version', '3.7.17')

    def test_the_sqlite_python_brings_has_them(self):
        assert db.json_problem() == ''

    def test_without_them_the_problem_says_what_is_needed_and_what_to_do(self, without_json):
        problem = db.json_problem()
        assert problem.startswith('bustag 要用 SQLite 的 JSON 函数，这台电脑上 Python 带的 SQLite（3.7.17）没有')
        assert '3.38' in problem and 'python.org' in problem

    def test_starting_without_them_stops_with_that_line(self, without_json, caplog, monkeypatch):
        tables = []
        monkeypatch.setattr(db, 'create_tables', lambda wait=db.BUSY_TIMEOUT: tables.append(wait) or True)
        with pytest.raises(SystemExit) as stopped:
            db.init()
        assert stopped.value.code == 1
        assert [(record.levelname, record.getMessage()) for record in caplog.records] == [('ERROR', db.json_problem())]
        assert tables == []


class TestContainsPattern:
    '''搜索和联想用的 GLOB 模式：结果和"折过以后包含"一样，只是在 SQLite 里直接比'''

    @staticmethod
    def matches(value, text):
        return db.db.execute_sql('SELECT ? GLOB ?', (value, db.contains_pattern(text))).fetchone()[0] == 1

    def test_it_finds_what_folding_both_sides_finds_in_random_text(self):
        rng = random.Random(20261111)
        pairs = list(zip(hanzi.TRADITIONAL, hanzi.SIMPLIFIED))[:60]
        alphabet = (list('aAbBzZ09 \u3000\t%_\\*?[]^-.\'"') + [c for pair in pairs for c in pair] + list('気学ひカÉéÀ'))
        found = 0
        for _ in range(20000):
            value = ''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 9)))
            if value and rng.random() < 0.6:
                # 取值里的一段，每个字换成它的另一种写法
                start = rng.randrange(len(value))
                piece = value[start:rng.randint(start + 1, len(value))]
                text = ''.join(rng.choice(db._SPELLINGS.get(db.fold(char), char)) for char in piece)
            else:
                text = ''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 3)))
            if not text.strip():
                continue
            expected = db.fold(text) in db.fold(value)
            found += expected
            assert self.matches(value, text) is expected, (value, text)
        # 随机出来的组合里有足够多的命中，上面的比对才有分量
        assert found > 3000

    def test_every_spelling_of_a_character_matches_every_other(self):
        for traditional, simplified in zip(hanzi.TRADITIONAL, hanzi.SIMPLIFIED):
            assert self.matches(f'前{traditional}后', simplified), (traditional, simplified)
            assert self.matches(f'前{simplified}后', traditional), (traditional, simplified)
        for lower, upper in zip(ascii_lowercase, ascii_uppercase):
            assert self.matches(f'-{upper}-', lower) and self.matches(f'-{lower}-', upper)

    @pytest.mark.parametrize('char', list('*?[]^-%_\\.\'"{}()|+$&'))
    def test_a_character_that_means_something_to_glob_is_plain_text(self, char):
        assert self.matches(f'a{char}b', f'a{char}b')
        assert self.matches(f'{char}', char)
        assert not self.matches('axb', f'a{char}b')
        assert not self.matches('x', char)

    def test_what_is_not_a_spelling_of_the_same_character_does_not_match(self):
        assert not self.matches('気', '气') and not self.matches('气', '気')
        assert not self.matches('学生', '学园') and not self.matches('abc', 'abd')
        # 只管 ASCII 字母的大小写
        assert not self.matches('É', 'é') and not self.matches('é', 'É')

    def test_white_space_around_the_value_and_the_word_does_not_matter(self):
        assert self.matches('\u3000\t學生 \xa0', ' 学生\u3000') and self.matches('學生', '学生')
        assert self.matches('女大學生', '大学') and not self.matches('女大學生', '大 学')


class TestListItems:
    def test_condition_can_use_the_rate_and_rows_have_display_fields(self, make_item):
        make_item('A-001')
        make_item('A-002', tags=[('genre', 'g1'), ('star', 's1')])
        make_item('A-003')
        rate('A-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        rate('A-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        not_tagged_by_user = ItemRate.rate_type.is_null() | (ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE)
        items, page_info = db.list_items(not_tagged_by_user)
        assert [item.fanhao for item in items] == ['A-003', 'A-002']
        assert page_info == (2, 1, 1, 10)
        assert [item.rate_value for item in items] == [None, 1]
        assert dict(items[1].tags_dict) == {'genre': ['g1'], 'star': ['s1']}
        assert items[1].url == 'https://www.javbus.com/A-002'
        assert db.count_items(not_tagged_by_user) == 2

    def test_extra_ordering_goes_before_newest_first(self, make_item):
        for fanhao in ('A-001', 'A-002', 'A-003', 'A-004'):
            make_item(fanhao)
        second_first = Case(None, [(db.Item.fanhao == 'A-002', 0)], 1)
        items, _ = db.list_items(db.Item.id.is_null(False), order_first=[second_first])
        assert [item.fanhao for item in items] == ['A-002', 'A-004', 'A-003', 'A-001']

    def test_pages_follow_the_same_rules_as_get_items(self, make_item):
        with db.db.atomic():
            for n in range(1, 26):
                make_item(f'ABC-{n:03d}')
        everything = db.Item.id.is_null(False)
        items, page_info = db.list_items(everything, page=4)
        assert page_info == (25, 3, 3, 10)
        assert [item.fanhao for item in items] == [f'ABC-{n:03d}' for n in range(5, 0, -1)]
        items, page_info = db.list_items(everything, page=None)
        assert page_info == (25, 3, None, 10)
        assert len(items) == 25
        assert db.list_items(db.Item.fanhao == 'NOPE-001', page=7) == ([], (0, 0, 1, 10))
        assert db.count_items(everything) == 25

    @pytest.fixture
    def rated_library(self, make_item):
        with db.db.atomic():
            for n in range(1, 26):
                make_item(f'ABC-{n:03d}', title='挑出来的' if n % 5 else '别的', tags=[('genre', f'g{n % 3}')])
                if n % 4 == 0:
                    rate(f'ABC-{n:03d}', RATE_TYPE.SYSTEM_RATE, n % 8 == 0)

    def test_sort_once_hands_python_the_ids_of_one_page_only(self, rated_library):
        # 整个列表的番号一个个交给 Python 再切出一页，几个线程同时这样做，每取一行都要抢解释器锁，互相拖慢；
        # 在 SQLite 里数总数、切出这一页，只有这一页的番号回到 Python
        liked_first = Case(None, [(ItemRate.rate_value == RATE_VALUE.LIKE, 0)], 1)
        statements = []
        connection = db.db.connection()
        connection.set_trace_callback(statements.append)
        try:
            items, page_info = db.list_items(db.Item.title.contains('挑出来的'), order_first=[liked_first], page=2,
                                             sort_once=True)
        finally:
            connection.set_trace_callback(None)
        assert page_info == (20, 2, 2, 10) and len(items) == 10
        listing = [statement for statement in statements if ' OVER ' in statement]
        assert len(listing) == 1 and listing[0].endswith('LIMIT 10 OFFSET 10')

    @pytest.mark.parametrize('page', [1, 2, 3, 99])
    def test_sort_once_lists_the_same_rows(self, rated_library, page):
        where = db.Item.title.contains('挑出来的')
        liked_first = Case(None, [(ItemRate.rate_value == RATE_VALUE.LIKE, 0)], 1)

        def rows(**options):
            items, page_info = db.list_items(where, order_first=[liked_first], page=page, **options)
            return [(item.fanhao, item.rate_value, dict(item.tags_dict), item.url) for item in items], page_info

        assert rows(sort_once=True) == rows()
        assert rows()[1] == (20, 2, min(page, 2), 10)

    def test_sort_once_without_rows_or_without_pages(self, rated_library):
        assert db.list_items(db.Item.fanhao == 'NOPE-001', page=3, sort_once=True) == ([], (0, 0, 1, 10))
        items, page_info = db.list_items(db.Item.title.contains('挑出来的'), page=None, sort_once=True)
        assert (len(items), page_info) == (20, (20, 2, None, 10))

    def test_sort_once_evaluates_the_condition_in_one_statement(self, rated_library, monkeypatch):
        statements = []
        execute_sql = db.db.execute_sql

        def recording(sql, params=None, *args, **kwargs):
            statements.append((sql, tuple(params or ())))
            return execute_sql(sql, params, *args, **kwargs)

        monkeypatch.setattr(db.db, 'execute_sql', recording)
        where = db.Item.title.contains('挑出来的')
        db.list_items(where, page=2, sort_once=True)
        assert sum('%挑出来的%' in params for _, params in statements) == 1
        statements.clear()
        db.list_items(where, page=2)
        assert sum('%挑出来的%' in params for _, params in statements) > 1


def system_rates():
    '''系统评分：{番号: 值}'''
    rows = ItemRate.select().where(ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE)
    return {row.item_id: row.rate_value for row in rows}


def all_rates():
    '''全部评分，按番号排：[(番号, 类型, 值, 时间)]'''
    return [(row.item_id, row.rate_type, row.rate_value, row.rete_time)
            for row in ItemRate.select().order_by(ItemRate.item)]


def stored_scores():
    '''分数表的内容：{番号: (分数, 拉高分数的标签, 拉低分数的标签)}'''
    return {row.item_id: (row.score, json.loads(row.because), json.loads(row.against))
            for row in db.ItemScore.select()}


def score(fanhao, value, because=(), against=()):
    return db.Score(fanhao, value, list(because), list(against))


def top(count):
    '''划分规则：分数最高的 count 部算推荐'''
    return lambda total: min(count, total)


class TestScores:
    OLD = datetime.datetime(2026, 11, 1, 8, 0, 0)
    NOW = datetime.datetime(2026, 11, 6, 12, 0, 0)

    def test_scores_are_stored_and_the_highest_are_recommended(self, make_item):
        for fanhao in ('A-001', 'A-002', 'A-003'):
            make_item(fanhao)
        result = db.save_scores([score('A-003', 0.5), score('A-002', 0.2, against=['标签二']),
                                 score('A-001', 0.9, because=['标签一', '标签三'])], top(1))
        assert result == (3, 1, 0)
        assert (result.scored, result.recommended, result.skipped) == (3, 1, 0)
        assert stored_scores() == {'A-001': (0.9, ['标签一', '标签三'], []), 'A-002': (0.2, [], ['标签二']),
                                   'A-003': (0.5, [], [])}
        assert system_rates() == {'A-001': RATE_VALUE.LIKE, 'A-002': RATE_VALUE.DISLIKE, 'A-003': RATE_VALUE.DISLIKE}

    def test_equal_scores_keep_the_order_they_came_in(self, make_item):
        for fanhao in ('A-001', 'A-002', 'A-003', 'A-004'):
            make_item(fanhao)
        scores = [score(fanhao, 0.5) for fanhao in ('A-004', 'A-003', 'A-002', 'A-001')]
        assert db.save_scores(scores, top(2)) == (4, 2, 0)
        assert system_rates() == {'A-004': 1, 'A-003': 1, 'A-002': 0, 'A-001': 0}

    def test_the_rule_is_asked_with_the_number_of_movies_actually_scored(self, make_item):
        for fanhao in ('A-001', 'A-002', 'A-003'):
            make_item(fanhao)
        rate('A-003', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        asked = []

        def rule(total):
            asked.append(total)
            return 1

        db.save_scores([score('A-003', 0.9), score('A-002', 0.8), score('A-001', 0.7)], rule)
        assert asked == [2]
        assert system_rates() == {'A-002': 1, 'A-001': 0}

    def test_user_tags_made_meanwhile_are_kept(self, make_item, monkeypatch):
        monkeypatch.setattr(db, 'get_now_time', lambda: self.NOW)
        for fanhao in ('A-001', 'A-002', 'A-003'):
            make_item(fanhao)
        ItemRate.create(item='A-001', rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.DISLIKE, rete_time=self.OLD)
        ItemRate.create(item='A-002', rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.LIKE, rete_time=self.OLD)
        # 打分时这两部还没有用户打标，写入前用户打了标：分数高的那部也不会被改回系统评分
        scores = [score('A-003', 0.1), score('A-002', 0.2), score('A-001', 0.9)]
        assert db.save_scores(scores, top(1)) == (1, 1, 2)
        assert all_rates() == [('A-001', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, self.OLD),
                               ('A-002', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, self.OLD),
                               ('A-003', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, self.NOW)]
        assert list(stored_scores()) == ['A-003']

    def test_system_rates_follow_the_new_split_and_only_changed_ones_get_a_new_time(self, make_item, monkeypatch):
        monkeypatch.setattr(db, 'get_now_time', lambda: self.NOW)
        for fanhao in ('A-001', 'A-002', 'A-003', 'A-004', 'A-005'):
            make_item(fanhao)
        for fanhao, value in (('A-001', 1), ('A-002', 0), ('A-003', 1), ('A-004', 0)):
            ItemRate.create(item=fanhao, rate_type=RATE_TYPE.SYSTEM_RATE, rate_value=value, rete_time=self.OLD)
        scores = [score('A-005', 0.3), score('A-004', 0.1), score('A-003', 0.2), score('A-002', 0.8),
                  score('A-001', 0.9)]
        assert db.save_scores(scores, top(2)) == (5, 2, 0)
        system = RATE_TYPE.SYSTEM_RATE
        assert all_rates() == [('A-001', system, 1, self.OLD), ('A-002', system, 1, self.NOW),
                               ('A-003', system, 0, self.NOW), ('A-004', system, 0, self.OLD),
                               ('A-005', system, 0, self.NOW)]

    def test_today_recommend_count_counts_movies_that_entered_the_recommended_today(self, make_item, monkeypatch):
        monkeypatch.setattr(db, 'get_now_time', lambda: self.NOW)
        for fanhao in ('A-001', 'A-002', 'A-003', 'A-004'):
            make_item(fanhao)
        ItemRate.create(item='A-001', rate_type=RATE_TYPE.SYSTEM_RATE, rate_value=1, rete_time=self.OLD)
        ItemRate.create(item='A-002', rate_type=RATE_TYPE.SYSTEM_RATE, rate_value=0, rete_time=self.OLD)
        scores = [score('A-004', 0.1), score('A-003', 0.7), score('A-002', 0.8), score('A-001', 0.9)]
        for _ in range(2):
            # A-001 以前就在推荐里，不算今天的；A-002 今天从不太像变成推荐，A-003 今天第一次打分就是推荐。
            # 再打一次分、划分没变，数字也不变
            db.save_scores(scores, top(3))
            assert db.get_today_recommend_count() == 2

    def test_each_pass_replaces_the_previous_scores(self, make_item):
        for fanhao in ('A-001', 'A-002', 'A-003'):
            make_item(fanhao)
        db.save_scores([score('A-003', 0.1), score('A-002', 0.2), score('A-001', 0.9, because=['标签一'])], top(1))
        assert db.save_scores([score('A-003', 0.6), score('A-002', 0.4)], top(1)) == (2, 1, 0)
        assert stored_scores() == {'A-002': (0.4, [], []), 'A-003': (0.6, [], [])}
        assert system_rates() == {'A-001': 1, 'A-002': 0, 'A-003': 1}

    def test_movies_no_longer_in_the_library_are_skipped(self, make_item):
        make_item('A-001')
        assert db.save_scores([score('GONE-001', 0.9), score('A-001', 0.1)], top(1)) == (1, 1, 1)
        assert list(stored_scores()) == ['A-001']
        assert system_rates() == {'A-001': 1}

    def test_nothing_to_score_clears_the_old_scores(self, make_item):
        make_item('A-001')
        db.save_scores([score('A-001', 0.5)], top(1))
        ItemRate.save_user_rate('A-001', RATE_VALUE.DISLIKE)
        assert db.save_scores([], top(1)) == (0, 0, 0)
        assert stored_scores() == {}
        assert all_rates()[0][:3] == ('A-001', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)

    def test_many_movies_are_written_in_batches(self, make_item, monkeypatch):
        monkeypatch.setattr(db, 'WRITE_BATCH', 7)
        fanhaos = [f'A-{n:03d}' for n in range(1, 41)]
        with db.db.atomic():
            for n, fanhao in enumerate(fanhaos):
                make_item(fanhao)
                if n % 2:
                    rate(fanhao, RATE_TYPE.SYSTEM_RATE, n % 4 == 1)
        scores = [score(fanhao, n / 100) for n, fanhao in enumerate(fanhaos)]
        assert db.save_scores(scores, top(12)) == (40, 12, 0)
        assert {fanhao: value for fanhao, (value, _, _) in stored_scores().items()} == {
            fanhao: n / 100 for n, fanhao in enumerate(fanhaos)}
        assert system_rates() == {fanhao: int(n >= 28) for n, fanhao in enumerate(fanhaos)}

    def test_a_tag_made_while_scores_are_being_written_waits_and_wins(self, make_item):
        for fanhao in ('A-001', 'A-002'):
            make_item(fanhao)
            rate(fanhao, RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        writing, release = threading.Event(), threading.Event()
        outcomes = []

        def nothing_recommended(total):
            # 这时打分的事务已经拿着写锁、读过现有的评分
            writing.set()
            release.wait(timeout=30)
            return 0

        def save():
            try:
                outcomes.append(db.save_scores([score('A-002', 0.6), score('A-001', 0.4)], nothing_recommended))
            finally:
                db.db.close()

        def tag():
            try:
                outcomes.append(ItemRate.save_user_rate('A-001', RATE_VALUE.LIKE, replace_user_rate=True))
            finally:
                db.db.close()

        scorer, tagger = threading.Thread(target=save), threading.Thread(target=tag)
        scorer.start()
        try:
            assert writing.wait(timeout=30)
            tagger.start()
            tagger.join(timeout=0.5)
            # 用户的打标在等写锁，没有报错也没有丢
            assert tagger.is_alive() and outcomes == []
        finally:
            release.set()
            scorer.join(timeout=30)
            if tagger.ident is not None:
                tagger.join(timeout=30)
        assert outcomes == [(2, 0, 0), 'updated']
        assert [row[:3] for row in all_rates()] == [('A-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE),
                                                    ('A-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)]

    def test_listed_movies_carry_their_score(self, make_item):
        make_item('A-001')
        make_item('A-002')
        db.save_scores([score('A-001', 0.875, because=['标签一'], against=['标签二', '标签三'])], top(1))
        items, _ = db.list_items(db.Item.id.is_null(False))
        assert [(item.fanhao, item.score, item.because, item.against, item.rate_value) for item in items] == [
            ('A-002', None, [], [], None), ('A-001', 0.875, ['标签一'], ['标签二', '标签三'], 1)]

    @pytest.mark.parametrize('sort_once', [False, True])
    def test_by_score_lists_the_highest_first_and_the_unscored_last(self, make_item, sort_once):
        for fanhao in ('A-001', 'A-002', 'A-003', 'A-004', 'A-005'):
            make_item(fanhao)
        db.save_scores([score('A-003', 0.9), score('A-002', 0.5), score('A-001', 0.9)], top(1))
        items, page_info = db.list_items(db.Item.id.is_null(False), order_first=db.by_score(), sort_once=sort_once)
        # 同分的、没有分数的，都是新入库的在前
        assert [item.fanhao for item in items] == ['A-003', 'A-001', 'A-002', 'A-005', 'A-004']
        assert page_info == (5, 1, 1, 10)

    def test_get_items_can_list_by_score(self, make_item):
        with db.db.atomic():
            for n in range(1, 16):
                make_item(f'A-{n:03d}')
        scores = [score(f'A-{n:03d}', n / 20) for n in range(1, 16)]
        db.save_scores(scores, top(12))
        items, page_info = db.get_items(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, page=2, order_first=db.by_score())
        assert [item.fanhao for item in items] == ['A-005', 'A-004']
        assert page_info == (12, 2, 2, 10)
        items, _ = db.get_items(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE, page=1, order_first=db.by_score())
        assert [(item.fanhao, item.score) for item in items] == [('A-003', 0.15), ('A-002', 0.1), ('A-001', 0.05)]


HOUR = datetime.timedelta(hours=1)
DAY = datetime.timedelta(days=1)


def like(make_item, fanhao, when, stars=()):
    '''入库一部用户在 when 打成喜欢的影片'''
    make_item(fanhao, tags=[('genre', '标签一')] + [('star', name) for name in stars])
    ItemRate.create(item=fanhao, rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.LIKE, rete_time=when)


def lookup(fanhao):
    '''这部影片补演员名的记录：(结果, 连续几次没有, 试的时间, 下次再试的时间, 原因)'''
    row = db.ActressLookup.get(db.ActressLookup.item == fanhao)
    return row.status, row.misses, row.checked_at, row.next_try, row.detail


class TestActressLookup:
    '''补演员名的记录，以及"没有演员、到了该试的时候"的影片'''
    NOW = datetime.datetime(2026, 11, 6, 12, 0, 0)

    def test_every_movie_without_a_star_is_waiting_liked_first_then_recommended_then_the_rest(self, make_item):
        make_item('A-001')
        rate('A-001', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        like(make_item, 'A-002', self.NOW)
        make_item('A-003')
        rate('A-003', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        make_item('A-004')
        rate('A-004', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)
        make_item('A-005')
        like(make_item, 'A-006', self.NOW, stars=['演员甲'])
        like(make_item, 'A-007', self.NOW)
        make_item('A-008')
        rate('A-008', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        # 同一档里新入库的在前；有演员的不在里面
        assert db.starless_due(self.NOW) == [('A-007', True), ('A-002', True), ('A-008', False), ('A-003', False),
                                             ('A-005', False), ('A-004', False), ('A-001', False)]
        assert db.starless_due(self.NOW, limit=3) == [('A-007', True), ('A-002', True), ('A-008', False)]
        assert db.count_starless(self.NOW) == (7, 7, 2, 2)

    def test_never_tried_movies_come_first_then_the_ones_tried_longest_ago(self, make_item):
        for n in range(1, 6):
            like(make_item, f'A-{n:03d}', self.NOW)
        db.save_lookup('A-004', db.LOOKUP_FAILED, 0, self.NOW - HOUR, self.NOW - HOUR, '连不上')
        db.save_lookup('A-005', db.LOOKUP_FAILED, 0, self.NOW - 2 * HOUR, self.NOW - 2 * HOUR, '连不上')
        # 没试过的：新入库的在前；试过的：上次试得早的在前
        assert [fanhao for fanhao, _ in db.starless_due(self.NOW)] == ['A-003', 'A-002', 'A-001', 'A-005', 'A-004']

    def test_a_movie_waits_until_its_next_try(self, make_item):
        like(make_item, 'A-001', self.NOW)
        make_item('A-002')
        db.save_lookup('A-001', db.LOOKUP_MISSING, 1, self.NOW, self.NOW + DAY, 'av-wiki 上没有')
        just_before = self.NOW + DAY - datetime.timedelta(seconds=1)
        assert db.starless_due(just_before) == [('A-002', False)]
        assert db.count_starless(just_before) == (2, 1, 1, 0)
        assert db.starless_due(self.NOW + DAY) == [('A-001', True), ('A-002', False)]
        assert db.count_starless(self.NOW + DAY) == (2, 2, 1, 0)

    def test_nothing_is_waiting_in_an_empty_library(self):
        assert db.starless_due(self.NOW) == []
        assert db.count_starless(self.NOW) == (0, 0, 0, 0)

    def test_movies_to_look_up_again_after_the_upgrade(self, make_item):
        for n in range(1, 4):
            like(make_item, f'A-{n:03d}', self.NOW, stars=[f'演员{n}'])
        db.save_lookup('A-001', db.LOOKUP_STALE, 0, self.NOW, self.NOW)
        db.save_lookup('A-002', db.LOOKUP_STALE, 0, self.NOW - HOUR, self.NOW - HOUR)
        db.save_lookup('A-003', db.LOOKUP_STALE, 0, self.NOW, self.NOW + HOUR, '连不上')
        assert db.stale_due(self.NOW) == ['A-002', 'A-001']
        assert db.count_stale_due(self.NOW) == 2
        assert db.starless_due(self.NOW) == []

    def test_each_movie_keeps_one_record_of_its_latest_attempt(self, make_item):
        like(make_item, 'A-001', self.NOW)
        assert db.save_lookup('A-001', db.LOOKUP_FAILED, 0, self.NOW, self.NOW, '连不上') is True
        assert lookup('A-001') == ('failed', 0, self.NOW, self.NOW, '连不上')
        assert db.save_lookup('A-001', db.LOOKUP_MISSING, 2, self.NOW + HOUR, self.NOW + 7 * DAY, '没有') is True
        assert lookup('A-001') == ('missing', 2, self.NOW + HOUR, self.NOW + 7 * DAY, '没有')
        assert db.save_lookup('A-001', db.LOOKUP_FILLED, 0, self.NOW + DAY, self.NOW + DAY) is True
        assert lookup('A-001') == ('filled', 0, self.NOW + DAY, self.NOW + DAY, '')
        assert db.ActressLookup.select().count() == 1

    def test_a_movie_that_is_gone_gets_no_record(self):
        assert db.save_lookup('GONE-001', db.LOOKUP_FAILED, 0, self.NOW, self.NOW, '连不上') is False
        assert db.ActressLookup.select().count() == 0

    def test_results_are_counted(self, make_item):
        for n, status in enumerate(['filled', 'filled', 'missing', 'failed'], start=1):
            like(make_item, f'A-{n:03d}', self.NOW)
            db.save_lookup(f'A-{n:03d}', status, 0, self.NOW, self.NOW)
        assert [db.count_lookups(status) for status in (db.LOOKUP_FILLED, db.LOOKUP_MISSING, db.LOOKUP_FAILED)] == [2, 1, 1]

    def test_the_latest_failure_is_of_a_movie_that_is_still_waiting(self, make_item):
        assert db.latest_failed_lookup() is None
        like(make_item, 'A-001', self.NOW)
        like(make_item, 'A-002', self.NOW)
        like(make_item, 'A-003', self.NOW, stars=['演员甲'])
        like(make_item, 'A-004', self.NOW)
        db.save_lookup('A-001', db.LOOKUP_FAILED, 0, self.NOW - 2 * HOUR, self.NOW - 2 * HOUR, '早先连不上')
        db.save_lookup('A-002', db.LOOKUP_FAILED, 0, self.NOW - HOUR, self.NOW - HOUR, '后来连不上')
        # 失败之后有了演员的（例如改选了详情）不再算；没找到的不是失败
        db.save_lookup('A-003', db.LOOKUP_FAILED, 0, self.NOW, self.NOW, '已经不用补了')
        db.save_lookup('A-004', db.LOOKUP_MISSING, 1, self.NOW, self.NOW + DAY, 'av-wiki 上没有')
        latest = db.latest_failed_lookup()
        assert (latest.item_id, latest.detail, latest.checked_at) == ('A-002', '后来连不上', self.NOW - HOUR)


class TestBusyDatabase:
    '''数据库的写锁被别的连接占着时：等 30 秒，等不到的那个错误认得出来'''

    def test_a_writer_waits_thirty_seconds_for_the_lock(self):
        assert db.BUSY_TIMEOUT == 30
        assert db.db.timeout == 30
        db.db.connect(reuse_if_open=True)
        assert db.db.execute_sql('PRAGMA busy_timeout').fetchone()[0] == 30000

    def test_a_lock_held_by_another_connection_is_recognised(self, make_item, locked_database):
        make_item('ABC-001')
        with locked_database():
            with pytest.raises(OperationalError) as info:
                ItemRate.create(item='ABC-001', rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.LIKE)
        assert db.is_busy(info.value)
        # SQLite 等锁按名义时长累计，实际常是 30 秒的两倍多：不说等了几秒
        assert db.DATABASE_BUSY == '数据库正被别的程序占用（等了好一会儿还没轮到）'
        ItemRate.create(item='ABC-001', rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.LIKE)

    def test_reading_goes_on_while_another_connection_holds_the_lock(self, make_item, locked_database):
        make_item('ABC-001')
        with locked_database():
            items, page_info = db.get_items(page=1)
        assert [item.fanhao for item in items] == ['ABC-001'] and page_info == (1, 1, 1, 10)

    def test_other_errors_are_not_taken_for_a_busy_database(self):
        with pytest.raises(OperationalError) as info:
            db.db.execute_sql('SELECT * FROM no_such_table')
        assert not db.is_busy(info.value)
        assert not db.is_busy(ValueError('database is locked'))


class TestWaitingRequests:
    '''
    别的程序占着写锁时，等锁的网页请求最多同时这么多个，多出来的立刻得到"数据库正忙"：服务一共只有 8 个线程，
    都在干等 30 秒的话，连只读的页面也没有线程来处理
    '''

    @pytest.fixture
    def three_places(self, make_item, monkeypatch):
        for n in range(1, 8):
            make_item(f'A-{n:03d}')
        monkeypatch.setattr(db, 'MAX_WAITING_REQUESTS', 3)

    @staticmethod
    def tag(n, results):
        '''像一个网页请求那样给 A-00n 打标，结果（或出的错）记进 results'''
        try:
            # 网页请求的第一件事就是连上数据库（before_request），等锁时用的是那时候的等待时间
            db.db.connect(reuse_if_open=True)
            with db.limited_waiting():
                results[n] = ItemRate.save_user_rate(f'A-{n:03d}', RATE_VALUE.LIKE)
        except OperationalError as ex:
            results[n] = ex
        finally:
            db.db.close()

    @staticmethod
    def wait_until(condition):
        deadline = time.monotonic() + PATIENCE
        while not condition():
            assert time.monotonic() < deadline, f'等了 {PATIENCE} 秒还是不对'
            time.sleep(0.01)

    def test_requests_beyond_the_limit_are_turned_away_at_once_and_the_others_get_their_turn(self, three_places,
                                                                                             locked_database):
        results = {}
        with locked_database():
            db.db.timeout = 20
            waiting = [threading.Thread(target=self.tag, args=(n, results)) for n in (1, 2, 3)]
            for thread in waiting:
                thread.start()
            self.wait_until(lambda: db.waiting_requests() == 3)
            started = time.monotonic()
            self.tag(4, results)
            # 不跟着等那 20 秒
            assert time.monotonic() - started < db.db.timeout / 2
            assert isinstance(results[4], db.TooManyWaiting) and db.is_busy(results[4])
            assert db.waiting_requests() == 3 and sorted(results) == [4]
            # 只读的照常
            assert db.get_items(page=1)[1][0] == 7
        for thread in waiting:
            thread.join(timeout=20)
        assert [results[n] for n in (1, 2, 3)] == ['added'] * 3
        assert db.waiting_requests() == 0
        # 锁放开以后新的请求照常进来
        self.tag(5, results)
        assert results[5] == 'added'

    def test_a_place_is_given_back_when_the_wait_ends_in_a_time_out(self, three_places, locked_database):
        results = {}
        with locked_database():
            for n in (1, 2, 3, 4):
                self.tag(n, results)
        # 锁一直被占着：每个都等满超时（0.2 秒）而不是被挡回去，等完空位都还回来了
        assert all(isinstance(results[n], OperationalError) and not isinstance(results[n], db.TooManyWaiting)
                   for n in (1, 2, 3, 4))
        assert all(db.is_busy(results[n]) for n in (1, 2, 3, 4)) and db.waiting_requests() == 0

    def test_work_that_is_not_a_web_request_is_never_turned_away(self, three_places, locked_database):
        results = {}
        with locked_database():
            db.db.timeout = 20
            waiting = [threading.Thread(target=self.tag, args=(n, results)) for n in (1, 2, 3)]
            for thread in waiting:
                thread.start()
            self.wait_until(lambda: db.waiting_requests() == 3)
            db.db.timeout = 0.2
            # 后台任务、命令行不在 limited_waiting() 里：照常等，等到超时才是"正忙"
            with pytest.raises(OperationalError) as info:
                ItemRate.save_user_rate('A-006', RATE_VALUE.LIKE)
            assert db.is_busy(info.value) and not isinstance(info.value, db.TooManyWaiting)
            # 训练那样的网页请求自己说明了不受限制，也照常等
            with db.limited_waiting(), db.unlimited_waiting():
                with pytest.raises(OperationalError) as info:
                    ItemRate.save_user_rate('A-007', RATE_VALUE.LIKE)
            assert not isinstance(info.value, db.TooManyWaiting)
        for thread in waiting:
            thread.join(timeout=20)
        assert [results[n] for n in (1, 2, 3)] == ['added'] * 3

    def test_a_write_that_gets_the_lock_at_once_does_not_use_a_place_up(self, three_places):
        for n in (1, 2, 3, 4, 5):
            with db.limited_waiting():
                assert ItemRate.save_user_rate(f'A-{n:03d}', RATE_VALUE.LIKE) == 'added'
        assert db.waiting_requests() == 0

    def test_only_a_transaction_that_takes_the_write_lock_at_once_has_to_wait_its_turn(self, monkeypatch):
        # 没有空位时：BEGIN IMMEDIATE 被挡回去；普通的事务到第一次写的时候才拿锁，开始时不等，不受这里管
        monkeypatch.setattr(db, 'MAX_WAITING_REQUESTS', 0)
        with db.limited_waiting():
            with db.db.atomic():
                db.db.execute_sql('SELECT 1')
            with pytest.raises(db.TooManyWaiting):
                with db.db.atomic('IMMEDIATE'):
                    pass
        assert db.waiting_requests() == 0

    def test_playing_a_local_file_takes_part_too(self, make_item, monkeypatch):
        make_item('A-009')
        local = db.LocalItem.saveit('A-009', 'http://media.local/a.mp4')
        monkeypatch.setattr(db, 'MAX_WAITING_REQUESTS', 0)
        with db.limited_waiting():
            with pytest.raises(db.TooManyWaiting):
                db.LocalItem.update_play(local.id)
        assert db.LocalItem.get_by_id(local.id).view_times == 0
        assert db.LocalItem.update_play(local.id).view_times == 1

    def test_the_two_reasons_a_page_is_busy_are_told_apart(self):
        assert db.busy_reason(OperationalError('database is locked')) == db.DATABASE_BUSY
        assert db.busy_reason(db.TooManyWaiting('x')) == db.DATABASE_CROWDED
        assert db.DATABASE_CROWDED == '数据库正被别的程序占用，已经有好几个操作在等它了，不再多等一个'


class TestLocalItems:
    def test_save_list_and_play(self, make_item):
        make_item('ABC-001', tags=[('genre', 'g1')])
        make_item('ABC-002')
        first = LocalItem.saveit('ABC-001', 'http://nas/ABC-001.mp4')
        LocalItem.saveit('ABC-002', None)
        assert LocalItem.saveit('ABC-001', 'http://nas/other.mp4') is None

        items, page_info = db.get_local_items(page=1)
        assert page_info == (1, 1, 1, 10)
        assert [local.item.fanhao for local in items] == ['ABC-001']
        assert items[0].item.url == 'https://www.javbus.com/ABC-001'
        assert dict(items[0].item.tags_dict) == {'genre': ['g1']}

        played = LocalItem.update_play(first.id)
        assert played.view_times == 1
        assert played.last_view_date is not None
        LocalItem.loadit(played)
        assert len(played.last_view_date) == len('2026-01-01 00:00:00')
        assert LocalItem.update_play(first.id).view_times == 2

    def test_never_played_has_empty_view_date(self, make_item):
        make_item('ABC-001')
        local = LocalItem.saveit('ABC-001', 'http://nas/a.mp4')
        LocalItem.loadit(local)
        assert local.last_view_date == ''

    def test_files_without_movie_info_are_counted_separately(self, make_item):
        make_item('ABC-001')
        LocalItem.saveit('ABC-001', 'http://nas/ABC-001.mp4')
        LocalItem.saveit('ABC-002', 'http://nas/ABC-002.mp4')
        LocalItem.saveit('ABC-003', 'http://nas/ABC-003.mp4')
        LocalItem.saveit('ABC-004', None)
        items, page_info = db.get_local_items(page=1)
        assert [local.item.fanhao for local in items] == ['ABC-001']
        assert page_info == (1, 1, 1, 10)
        assert db.count_local_items_without_item() == 2

    def test_pages_count_only_listed_files(self, make_item):
        with db.db.atomic():
            for n in range(1, 13):
                make_item(f'ABC-{n:03d}')
                LocalItem.saveit(f'ABC-{n:03d}', f'http://nas/ABC-{n:03d}.mp4')
                LocalItem.saveit(f'NOPE-{n:03d}', f'http://nas/NOPE-{n:03d}.mp4')
        items, page_info = db.get_local_items(page=2)
        assert page_info == (12, 2, 2, 10)
        assert [local.item.fanhao for local in items] == ['ABC-002', 'ABC-001']
        items, page_info = db.get_local_items(page=3)
        assert page_info == (12, 2, 2, 10)
        assert [local.item.fanhao for local in items] == ['ABC-002', 'ABC-001']

    def test_broken_movie_info_is_not_hidden(self, make_item):
        make_item('ABC-001')
        db.Item.update(meta_info='{broken').where(db.Item.fanhao == 'ABC-001').execute()
        LocalItem.saveit('ABC-001', 'http://nas/ABC-001.mp4')
        with pytest.raises(json.JSONDecodeError):
            db.get_local_items(page=1)


class TestTodayCounts:
    NOW = datetime.datetime(2026, 11, 6, 12, 0, 0)

    def test_today_update_count(self, make_item, monkeypatch):
        monkeypatch.setattr(db, 'get_now_time', lambda: self.NOW)
        for fanhao, added in (('A-001', self.NOW), ('A-002', self.NOW.replace(hour=0, minute=0)),
                              ('A-003', self.NOW - datetime.timedelta(days=1))):
            make_item(fanhao)
            db.Item.update(add_date=added).where(db.Item.fanhao == fanhao).execute()
        assert db.get_today_update_count() == 2

    def test_today_recommend_count(self, make_item, monkeypatch):
        monkeypatch.setattr(db, 'get_now_time', lambda: self.NOW)
        rows = [('A-001', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, self.NOW),
                ('A-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE, self.NOW),
                ('A-003', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, self.NOW),
                ('A-004', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, self.NOW - datetime.timedelta(days=1))]
        for fanhao, rate_type, rate_value, when in rows:
            make_item(fanhao)
            ItemRate.create(item=fanhao, rate_type=rate_type, rate_value=rate_value, rete_time=when)
        assert db.get_today_recommend_count() == 1

    # 今天 = 当天零点（含）到第二天零点（不含）；库里的时间有的带小数秒，有的不带
    EDGES = [(datetime.datetime(2026, 11, 5, 23, 59, 59, 999999), False),
             (datetime.datetime(2026, 11, 6, 0, 0, 0), True),
             (datetime.datetime(2026, 11, 6, 0, 0, 0, 1), True),
             (datetime.datetime(2026, 11, 6, 9, 30, 0), True),
             (datetime.datetime(2026, 11, 6, 23, 59, 59), True),
             (datetime.datetime(2026, 11, 6, 23, 59, 59, 999999), True),
             (datetime.datetime(2026, 11, 7, 0, 0, 0), False),
             (datetime.datetime(2025, 11, 6, 12, 0, 0), False),
             (datetime.datetime(2026, 10, 6, 12, 0, 0), False)]

    @pytest.mark.parametrize('now', [NOW, datetime.datetime(2026, 11, 6, 0, 0, 0),
                                     datetime.datetime(2026, 11, 6, 23, 59, 59, 999999)])
    def test_today_runs_from_midnight_to_midnight(self, make_item, monkeypatch, now):
        monkeypatch.setattr(db, 'get_now_time', lambda: now)
        for n, (when, _) in enumerate(self.EDGES):
            make_item(f'A-{n:03d}')
            db.Item.update(add_date=when).where(db.Item.fanhao == f'A-{n:03d}').execute()
            ItemRate.create(item=f'A-{n:03d}', rate_type=RATE_TYPE.SYSTEM_RATE, rate_value=RATE_VALUE.LIKE, rete_time=when)
        today = sum(inside for _, inside in self.EDGES)
        assert db.get_today_update_count() == today
        assert db.get_today_recommend_count() == today

    def test_the_last_day_of_a_month_and_of_a_year(self, make_item, monkeypatch):
        monkeypatch.setattr(db, 'get_now_time', lambda: datetime.datetime(2026, 12, 31, 18, 0, 0))
        for n, when in enumerate([datetime.datetime(2026, 12, 31, 0, 0, 0), datetime.datetime(2026, 12, 31, 23, 59, 59),
                                  datetime.datetime(2027, 1, 1, 0, 0, 0), datetime.datetime(2026, 12, 30, 23, 59, 59)]):
            make_item(f'A-{n:03d}')
            db.Item.update(add_date=when).where(db.Item.fanhao == f'A-{n:03d}').execute()
        assert db.get_today_update_count() == 2

    def test_the_date_is_compared_as_stored_without_a_function_call_per_row(self, monkeypatch):
        # peewee 的 add_date.year 在 SQLite 上是一个 Python 函数，每一行都要调用；库大了以后首页因此变慢
        monkeypatch.setattr(db, 'get_now_time', lambda: self.NOW)
        asked = []
        db.db.connection().set_trace_callback(asked.append)
        try:
            db.get_today_update_count()
            db.get_today_recommend_count()
        finally:
            db.db.connection().set_trace_callback(None)
        counting = [sql for sql in asked if 'COUNT' in sql.upper()]
        assert len(counting) == 2
        for sql in counting:
            assert 'date_part' not in sql
            assert "'2026-11-06 00:00:00'" in sql and "'2026-11-07 00:00:00'" in sql


def schema_statements():
    '''当前数据库里全部的建表、建索引语句'''
    return [sql for (sql,) in db.db.execute_sql("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()]


class TestSchema2019:
    def test_new_database_keeps_the_2019_tables(self):
        sqls = schema_statements()
        assert set(SCHEMA_2019) <= set(sqls)
        extra = [sql for sql in sqls if sql not in SCHEMA_2019]
        later_tables = ('item_candidate', 'item_score', 'actress_lookup', 'list_source', 'fetch_task', 'job_state',
                        'star_link', '"actress"', 'actress_alias')
        for table in later_tables:
            assert any(table in sql for sql in extra), table
        assert all(any(table in sql for table in later_tables) for sql in extra)

    def test_opens_reads_and_writes_a_2019_database(self, tmp_path, make_item):
        path = tmp_path / 'bus-2019.db'
        conn = sqlite3.connect(path)
        for statement in SCHEMA_2019:
            conn.execute(statement)
        conn.execute("INSERT INTO item VALUES (1, '作品 ZBBH-001', 'ZBBH-001', '/ZBBH-001', '2019-09-21', "
                     "'2019-09-27 09:48:32.840783', "
                     "'{\"cover_img_url\": \"https://pics.javcdn.pw/cover/7by5_b.jpg\", \"length\": \"136\"}')")
        conn.execute("INSERT INTO tag VALUES (1, '導演:', 'デカ拳', '/director/fq')")
        conn.execute("INSERT INTO tag VALUES (5, 'genre', '斡瀠鰉', '/genre/4o')")
        conn.execute("INSERT INTO item_tag VALUES (1, 'ZBBH-001', 1)")
        conn.execute("INSERT INTO item_tag VALUES (2, 'ZBBH-001', 5)")
        conn.commit()
        conn.close()

        original = db.db.database
        db.db.init(str(path), **db.DB_OPTIONS)
        try:
            # 程序启动时就是这样打开数据库的：原有的表原样留着，后来才有的表补上
            db.init()
            sqls = schema_statements()
            assert set(SCHEMA_2019) <= set(sqls)
            for table in ('"item_score"', '"item_candidate"', '"actress_lookup"', '"list_source"', '"fetch_task"',
                          '"job_state"', '"star_link"', '"actress"', '"actress_alias"'):
                assert any(table in sql for sql in sqls), table
            assert db.FetchTask.create(path='/ABC-002', fanhao='ABC-002', origin=db.ORIGIN_UPLOAD).status == 'queued'
            item = db.get_items(page=1)[0][0]
            assert item.fanhao == 'ZBBH-001'
            assert item.cover_img_url == '/pics/cover/7by5_b.jpg'
            assert dict(item.tags_dict) == {'導演:': ['デカ拳'], 'genre': ['斡瀠鰉']}
            make_item('ABC-001', tags=[('genre', '斡瀠鰉')])
            rate('ZBBH-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
            assert db.Tag.select().count() == 2
            assert [i.fanhao for i in db.get_items(RATE_TYPE.USER_RATE, None, page=1)[0]] == ['ZBBH-001']
            assert db.save_scores([score('ABC-001', 0.5)], top(1)) == (1, 1, 0)
            assert db.get_items(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, page=1)[0][0].score == 0.5
        finally:
            db.db.close()
            db.db.init(original, **db.DB_OPTIONS)
        assert db.Item.select().count() == 0


class TestTablesWhileLocked:
    '''
    新版本第一次打开旧库要加几张表，这要写锁。别的程序一直占着写锁时：启动不等满 30 秒，缺的表记下来以后再建，
    已有的表照常读；放开以后再建就成了
    '''

    @pytest.fixture
    def old_database(self, tmp_path):
        path = tmp_path / 'bus-2019.db'
        conn = sqlite3.connect(path)
        for statement in SCHEMA_2019:
            conn.execute(statement)
        conn.execute("INSERT INTO item VALUES (1, '作品 ZBBH-001', 'ZBBH-001', '/ZBBH-001', '2019-09-21', "
                     "'2019-09-27 09:48:32.840783', '{}')")
        conn.commit()
        conn.close()
        original = db.db.database
        db.db.init(str(path), **db.DB_OPTIONS)
        try:
            yield path
        finally:
            db.db.close()
            db.db.init(original, **db.DB_OPTIONS)
            db.tables_missing = False

    @staticmethod
    def hold(path):
        db.db.connect(reuse_if_open=True)
        other = sqlite3.connect(path, isolation_level=None)
        other.execute('BEGIN IMMEDIATE')
        return other

    def test_missing_tables_wait_while_another_program_holds_the_lock(self, old_database):
        other = self.hold(old_database)
        try:
            began = time.monotonic()
            assert db.create_tables(wait=0.2) is False
            assert time.monotonic() - began < NOT_THE_FULL_WAIT
            assert db.tables_missing
            assert db.Item.get_by_fanhao('ZBBH-001').title == '作品 ZBBH-001'
            assert not db.JobState.table_exists()
        finally:
            other.execute('ROLLBACK')
            other.close()
        assert db.create_tables(wait=0.2) is True
        assert not db.tables_missing
        assert db.JobState.table_exists() and db.ActressAlias.table_exists()

    def test_starting_up_does_not_wait_the_full_timeout(self, old_database, monkeypatch, caplog):
        monkeypatch.setattr(db, 'CREATE_WAIT', 0.2)
        other = self.hold(old_database)
        try:
            began = time.monotonic()
            db.init()
            assert time.monotonic() - began < NOT_THE_FULL_WAIT
        finally:
            other.execute('ROLLBACK')
            other.close()
        assert db.tables_missing
        # 怎么说由启动的那一方定：网页服务说它在后台接着加，命令行说这次没做成（打开库时什么都不说）
        assert [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING] == []

    def test_waiting_goes_back_to_thirty_seconds(self, old_database):
        other = self.hold(old_database)
        try:
            db.create_tables(wait=0.2)
        finally:
            other.execute('ROLLBACK')
            other.close()
        assert db.db.execute_sql('PRAGMA busy_timeout').fetchone()[0] == 30000

    def test_an_old_database_never_switched_to_wal_opens_while_locked(self, old_database, monkeypatch):
        # 2019 年的库还是 journal_mode=delete：换成 WAL 要独占它，被别的程序占着时先照原来的方式打开
        monkeypatch.setattr(db, 'CREATE_WAIT', 0.2)
        db.db.close()
        other = sqlite3.connect(old_database, isolation_level=None)
        other.execute('BEGIN IMMEDIATE')
        try:
            db.init()
            assert db.tables_missing
            assert db.Item.get_by_fanhao('ZBBH-001').title == '作品 ZBBH-001'
        finally:
            other.execute('ROLLBACK')
            other.close()
        db.db.close()
        assert db.create_tables(wait=0.2) is True
        assert db.db.execute_sql('PRAGMA journal_mode').fetchone()[0] == 'wal'

    @staticmethod
    def held_exclusively(path):
        '''别的程序以 EXCLUSIVE 锁占着从没换成 WAL 的老库：连读都不让'''
        other = sqlite3.connect(path, isolation_level=None)
        other.execute('BEGIN EXCLUSIVE')
        return other

    def test_an_old_database_held_exclusively_is_reported_busy_at_once(self, old_database):
        db.db.close()
        other = self.held_exclusively(old_database)
        try:
            began = time.monotonic()
            with pytest.raises(OperationalError) as caught:
                db.db.connect()
            took = time.monotonic() - began
        finally:
            other.execute('ROLLBACK')
            other.close()
        # 换 WAL 只试一下，读一下也读不了：这次连接用不了，马上说"数据库正忙"，不等满两次 30 秒
        assert took < NOT_THE_FULL_WAIT
        assert db.is_busy(caught.value)
        assert db.busy_reason(caught.value) == db.DATABASE_LOCKED_OUT
        assert db.db.is_closed()
        db.db.connect()
        assert db.db.execute_sql('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.db.execute_sql('PRAGMA busy_timeout').fetchone()[0] == 30000

    def test_starting_up_while_an_old_database_is_held_exclusively_does_not_wait(self, old_database, monkeypatch):
        monkeypatch.setattr(db, 'CREATE_WAIT', 0.2)
        db.db.close()
        other = self.held_exclusively(old_database)
        try:
            began = time.monotonic()
            db.init()
            took = time.monotonic() - began
        finally:
            other.execute('ROLLBACK')
            other.close()
        assert took < NOT_THE_FULL_WAIT
        assert db.tables_missing
        assert db.create_tables(wait=0.2) is True
        assert db.db.execute_sql('PRAGMA journal_mode').fetchone()[0] == 'wal'

    def test_a_database_that_has_every_table_needs_no_lock(self, locked_database):
        with locked_database():
            assert db.create_tables(wait=0.2) is True
        assert not db.tables_missing
