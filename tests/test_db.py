'''
数据层：入库、查询分页、按评分过滤、本地影片、今日计数，以及与 2019 年数据库的兼容
'''
import datetime
import json
import sqlite3

import pytest

from bustag import util
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


def meta(fanhao, **extra):
    return {'fanhao': fanhao, 'title': f'作品 {fanhao}', 'url': '/' + fanhao,
            'release_date': '2026-01-01', 'cover_img_url': '/pics/cover/x_b.jpg', 'length': '120',
            **extra}


def rate(fanhao, rate_type, rate_value):
    assert ItemRate.saveit(rate_type, rate_value, fanhao) is not None


class TestSave:
    def test_save_stores_paths_and_meta(self):
        tags = [Tag('genre', '高畫質', 'https://www.javbus.com/genre/4o'), Tag('star', 's1', '/star/1')]
        item = db.save(meta('ABC-001', url='https://www.javbus.com/ABC-001'), tags)
        item = db.Item.get_by_fanhao('ABC-001')
        assert item.url == '/ABC-001'
        assert item.title == '作品 ABC-001'
        assert item.release_date == datetime.date(2026, 1, 1)
        assert json.loads(item.meta_info) == {'cover_img_url': '/pics/cover/x_b.jpg', 'length': '120'}
        assert sorted((t.tag.type_, t.tag.value, t.tag.url) for t in item.tags_list) == [
            ('genre', '高畫質', '/genre/4o'), ('star', 's1', '/star/1')]

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


class TestQuery:
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
        items, page_info = db.get_items(page=4)
        assert items == []
        assert page_info == (25, 3, 4, 10)

    def test_empty_database(self):
        assert db.get_items(page=1) == ([], (0, 0, 1, 10))

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


class TestSchema2019:
    def test_new_database_has_the_2019_schema(self):
        rows = db.db.execute_sql("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
        assert sorted(sql for (sql,) in rows) == sorted(SCHEMA_2019)

    def test_reads_and_writes_a_2019_database(self, tmp_path, make_item):
        path = tmp_path / 'bus-2019.db'
        conn = sqlite3.connect(path)
        for statement in SCHEMA_2019:
            conn.execute(statement)
        conn.execute("INSERT INTO item VALUES (1, '作品 ZBBH-001', 'ZBBH-001', '/ZBBH-001', '2019-09-21', "
                     "'2019-09-27 09:48:32.840783', "
                     "'{\"cover_img_url\": \"https://pics.javcdn.pw/cover/7by5_b.jpg\", \"length\": \"136\"}')")
        conn.execute("INSERT INTO tag VALUES (1, '導演:', 'デカ拳', '/director/fq')")
        conn.execute("INSERT INTO tag VALUES (5, 'genre', '高畫質', '/genre/4o')")
        conn.execute("INSERT INTO item_tag VALUES (1, 'ZBBH-001', 1)")
        conn.execute("INSERT INTO item_tag VALUES (2, 'ZBBH-001', 5)")
        conn.commit()
        conn.close()

        original = db.db.database
        db.db.init(str(path), pragmas={'journal_mode': 'wal'})
        try:
            item = db.get_items(page=1)[0][0]
            assert item.fanhao == 'ZBBH-001'
            assert item.cover_img_url == '/pics/cover/7by5_b.jpg'
            assert dict(item.tags_dict) == {'導演:': ['デカ拳'], 'genre': ['高畫質']}
            make_item('ABC-001', tags=[('genre', '高畫質')])
            rate('ZBBH-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
            assert db.Tag.select().count() == 2
            assert [i.fanhao for i in db.get_items(RATE_TYPE.USER_RATE, None, page=1)[0]] == ['ZBBH-001']
        finally:
            db.db.close()
            db.db.init(original, pragmas={'journal_mode': 'wal'})
        assert db.Item.select().count() == 0
