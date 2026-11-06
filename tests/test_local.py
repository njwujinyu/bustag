'''
本地番号与打标数据导入：add_local_fanhao、load_tags_db，以及 /local_fanhao、/load_db 页面。
测试里定时任务没有启动，加入下载队列只会写一条警告，不会联网
'''
import sqlite3

import pytest

from bustag.app.local import UPLOADED_DB, add_local_fanhao, load_tags_db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, DBError, ItemRate, LocalItem

USER, SYSTEM = RATE_TYPE.USER_RATE, RATE_TYPE.SYSTEM_RATE
LIKE, DISLIKE = RATE_VALUE.LIKE, RATE_VALUE.DISLIKE
# bustag 数据库里 item 表的建表语句（与 2019 年的库相同）；item_rate 只列出导入用到的列
ITEM_DDL = ('CREATE TABLE "item" ("id" INTEGER NOT NULL PRIMARY KEY, "title" VARCHAR(255) NOT NULL, '
            '"fanhao" VARCHAR(255) NOT NULL, "url" VARCHAR(255) NOT NULL, "release_date" DATE NOT NULL, '
            '"add_date" DATETIME NOT NULL, "meta_info" TEXT NOT NULL)')
RATE_DDL = ('CREATE TABLE "item_rate" ("id" INTEGER NOT NULL PRIMARY KEY, "rate_type" INTEGER NOT NULL, '
            '"rate_value" INTEGER NOT NULL, "item_id" {item_id} NOT NULL, "rete_time" DATETIME NOT NULL)')
# 两条用户打标、一条系统推荐、一部没有评分
RATES = [('ABC-001', USER, LIKE), ('ABC-002', USER, DISLIKE), ('ABC-003', SYSTEM, LIKE), ('ABC-004', None, None)]
UPLOAD_WARNING = '定时任务没有启动，{} 个番号没有加入下载队列'


def run_sql(path, *statements):
    con = sqlite3.connect(path)
    try:
        for statement in statements:
            con.execute(statement)
        con.commit()
    finally:
        con.close()


def make_tags_db(path, rates, old_format=False, wal=False, filler=0):
    '''
    生成一个 bustag 数据库文件。rates 是 [(番号, rate_type 或 None, rate_value)]；
    old_format 时 item_rate.item_id 存 item.id（早期版本），否则存番号；filler 是附加的无关数据字节数
    '''
    con = sqlite3.connect(path)
    try:
        if wal:
            con.execute('PRAGMA journal_mode=wal')
        con.execute(ITEM_DDL)
        con.execute(RATE_DDL.format(item_id='INTEGER' if old_format else 'VARCHAR(255)'))
        for item_id, (fanhao, rate_type, rate_value) in enumerate(rates, start=1):
            con.execute('INSERT INTO item VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (item_id, f'作品 {fanhao}', fanhao, '/' + fanhao, '2019-01-01', '2019-06-01 00:00:00', '{}'))
            if rate_type is not None:
                con.execute('INSERT INTO item_rate (rate_type, rate_value, item_id, rete_time) VALUES (?, ?, ?, ?)',
                            (rate_type, rate_value, item_id if old_format else fanhao, '2019-06-01 00:00:00'))
        if filler:
            con.execute('CREATE TABLE filler (data BLOB)')
            con.execute('INSERT INTO filler VALUES (?)', (b'\0' * filler,))
        con.commit()
    finally:
        con.close()
    return path


def rate_of(fanhao):
    rate = ItemRate.get_by_fanhao(fanhao)
    return None if rate is None else (rate.rate_type, rate.rate_value)


def local_path(fanhao):
    local = LocalItem.get_or_none(LocalItem.item_id == fanhao)
    return None if local is None else local.path


def leftovers(data_dir):
    return sorted(p.name for p in data_dir.glob(UPLOADED_DB + '*'))


class TestAddLocalFanhao:
    @pytest.mark.parametrize('line, fanhao, path', [
        ('ABC-123', 'ABC-123', None),
        ('abc-123', 'ABC-123', None),
        ('ABC123', 'ABC-123', None),
        ('  ABC-123  ', 'ABC-123', None),
        ('[中字] ABC-123 高清版', 'ABC-123', None),
        ('ABC-123,', 'ABC-123', None),
        ('ABC-123,http://media.local/ABC-123.mp4', 'ABC-123', 'http://media.local/ABC-123.mp4'),
        ('ABC-123 ,  http://media.local/ABC-123.mp4  ', 'ABC-123', 'http://media.local/ABC-123.mp4'),
        ('ABC-123,http://media.local/a,b.mp4', 'ABC-123', 'http://media.local/a,b.mp4'),
    ])
    def test_line_formats(self, line, fanhao, path):
        missed, local_added, tag_added = add_local_fanhao(line, tag_like=False)
        assert missed == [fanhao]
        assert (local_added, tag_added) == (1 if path else 0, 0)
        assert local_path(fanhao) == path
        assert rate_of(fanhao) is None

    def test_lines_without_fanhao_are_ignored(self):
        text = '\n   \n没有番号的一行\n123-456\n,http://media.local/x.mp4\n'
        assert add_local_fanhao(text, tag_like=True) == ([], 0, 0)
        assert LocalItem.select().count() == 0
        assert ItemRate.select().count() == 0

    def test_movies_in_library_are_not_queued(self, make_item):
        make_item('ABC-001')
        missed, _, _ = add_local_fanhao('ABC-001\r\nABC-002\r\n', tag_like=False)
        assert missed == ['ABC-002']

    def test_tag_like(self, make_item):
        make_item('ABC-001')
        result = add_local_fanhao('ABC-001\nABC-002,http://media.local/ABC-002.mp4', tag_like=True)
        assert result == (['ABC-002'], 1, 2)
        assert rate_of('ABC-001') == rate_of('ABC-002') == (USER, LIKE)

    def test_submitted_fanhao_keeps_its_first_path(self):
        add_local_fanhao('ABC-001,http://media.local/old.mp4', tag_like=True)
        assert add_local_fanhao('ABC-001,http://media.local/new.mp4', tag_like=True) == (['ABC-001'], 0, 0)
        assert local_path('ABC-001') == 'http://media.local/old.mp4'
        assert LocalItem.select().count() == 1

    def test_existing_user_rate_is_kept(self, make_item):
        make_item('ABC-001')
        ItemRate.saveit(USER, DISLIKE, 'ABC-001')
        assert add_local_fanhao('ABC-001', tag_like=True) == ([], 0, 0)
        assert rate_of('ABC-001') == (USER, DISLIKE)

    @pytest.mark.xfail(strict=True, reason='产品缺陷：带数字前缀的番号被截掉前缀（123ABC-456 记成 ABC-456）')
    def test_digit_prefixed_fanhao_is_kept(self):
        missed, local_added, _ = add_local_fanhao('123ABC-456,http://media.local/x.mp4', tag_like=False)
        assert local_added == 1
        assert missed == ['123ABC-456']

    @pytest.mark.xfail(strict=True, reason='产品缺陷：番号已有系统推荐评分时，「全部打标为喜欢」被静默丢弃')
    def test_tag_like_replaces_system_rate(self, make_item):
        make_item('ABC-001')
        ItemRate.saveit(SYSTEM, DISLIKE, 'ABC-001')
        _, _, tag_added = add_local_fanhao('ABC-001', tag_like=True)
        assert rate_of('ABC-001') == (USER, LIKE)
        assert tag_added == 1


def write_garbage(path):
    path.write_bytes(b'not a database ' * 100)


def write_empty(path):
    path.write_bytes(b'')


def write_other_tables(path):
    run_sql(path, 'CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)')


def write_missing_column(path):
    run_sql(path, ITEM_DDL, 'CREATE TABLE item_rate (id INTEGER PRIMARY KEY, rate_value INTEGER, item_id VARCHAR(255))')


class TestLoadTagsDb:
    @pytest.mark.parametrize('old_format', [False, True], ids=['item_id-is-fanhao', 'item_id-is-item-id'])
    def test_imports_user_rates_only(self, data_dir, make_item, old_format):
        make_item('ABC-001')
        make_tags_db(data_dir / UPLOADED_DB, RATES, old_format=old_format)
        assert load_tags_db() == (2, ['ABC-002'])
        assert rate_of('ABC-001') == (USER, LIKE)
        assert rate_of('ABC-002') == (USER, DISLIKE)
        assert rate_of('ABC-003') is None
        assert rate_of('ABC-004') is None
        assert leftovers(data_dir) == []

    def test_wal_mode_database(self, data_dir):
        make_tags_db(data_dir / UPLOADED_DB, RATES, wal=True)
        assert load_tags_db() == (2, ['ABC-001', 'ABC-002'])
        assert leftovers(data_dir) == []

    def test_existing_user_rates_are_kept(self, data_dir, make_item):
        make_item('ABC-001')
        ItemRate.saveit(USER, DISLIKE, 'ABC-001')
        make_tags_db(data_dir / UPLOADED_DB, [('ABC-001', USER, LIKE)])
        assert load_tags_db() == (0, [])
        assert rate_of('ABC-001') == (USER, DISLIKE)

    @pytest.mark.xfail(strict=True, reason='产品缺陷：导入的用户打标遇到本地已有的系统推荐评分时被静默丢弃')
    def test_import_replaces_system_rate(self, data_dir, make_item):
        make_item('ABC-001')
        ItemRate.saveit(SYSTEM, DISLIKE, 'ABC-001')
        make_tags_db(data_dir / UPLOADED_DB, [('ABC-001', USER, LIKE)])
        load_tags_db()
        assert rate_of('ABC-001') == (USER, LIKE)

    @pytest.mark.parametrize('write, message', [
        (write_garbage, '不是有效的 SQLite 数据库文件'),
        (write_empty, '不是 bustag 的数据库：找不到 item、item_rate 表'),
        (write_other_tables, '不是 bustag 的数据库：找不到 item、item_rate 表'),
        (write_missing_column, '数据库结构和 bustag 不一致：'),
    ], ids=['garbage', 'empty', 'other-tables', 'missing-column'])
    def test_not_a_bustag_database(self, data_dir, write, message):
        write(data_dir / UPLOADED_DB)
        with pytest.raises(DBError, match=message):
            load_tags_db()
        assert leftovers(data_dir) == []
        assert ItemRate.select().count() == 0


class TestLocalFanhaoPage:
    def test_submit(self, wsgi, make_item, caplog):
        make_item('ABC-001')
        response = wsgi.post('/local_fanhao', {
            'fanhao': 'ABC-001,http://media.local/ABC-001.mp4\r\nABC-002\r\n', 'tag_like': '1', 'submit': '1'})
        assert '已提交：1 个本地文件，2 个番号打标为喜欢，1 个番号加入下载队列' in response.text
        assert UPLOAD_WARNING.format(1) in caplog.messages

    def test_submit_without_tag_like(self, wsgi):
        text = wsgi.post('/local_fanhao', {'fanhao': 'ABC-001', 'submit': '1'}).text
        assert '已提交：1 个番号加入下载队列' in text
        assert rate_of('ABC-001') is None

    def test_nothing_new(self, wsgi):
        text = wsgi.post('/local_fanhao', {'fanhao': '没有番号', 'submit': '1'}).text
        assert 'alert-danger' in text
        assert '没有新增内容：没有识别到番号，或者这些番号都已经提交过' in text

    def test_page_without_submit_shows_no_message(self, wsgi):
        for response in (wsgi.get('/local_fanhao'), wsgi.post('/local_fanhao', {'fanhao': 'ABC-001'})):
            assert 'alert-success' not in response.text
            assert 'alert-danger' not in response.text
        assert LocalItem.select().count() == 0

    @pytest.mark.xfail(strict=True, reason='产品缺陷：番号列表超过 100 KB（一千多行带地址的番号）时返回 413，提交失败')
    def test_large_submission(self, wsgi):
        lines = '\r\n'.join(f'ABC-{i:04d},http://media.local/library/parts/{i}/file.mp4' for i in range(1, 2001))
        response = wsgi.post('/local_fanhao', {'fanhao': lines, 'submit': '1'})
        assert response.status == 200
        assert '已提交：2000 个本地文件' in response.text


class TestLoadDbPage:
    def test_upload(self, wsgi, tmp_path, data_dir, make_item, caplog):
        make_item('ABC-001')
        content = make_tags_db(tmp_path / 'bus.db', RATES).read_bytes()
        text = wsgi.post_multipart('/load_db', [('dbfile', ('bus.db', content)), ('submit', '1')]).text
        assert '上传 2 条用户打标数据, 1 个番号' in text
        assert rate_of('ABC-002') == (USER, DISLIKE)
        assert leftovers(data_dir) == []
        assert UPLOAD_WARNING.format(1) in caplog.messages

    def test_upload_large_database(self, wsgi, tmp_path):
        content = make_tags_db(tmp_path / 'bus.db', RATES, filler=3 * 1024 * 1024).read_bytes()
        assert len(content) > 3 * 1024 * 1024
        text = wsgi.post_multipart('/load_db', [('dbfile', ('bus.db', content)), ('submit', '1')]).text
        assert '上传 2 条用户打标数据, 2 个番号' in text

    def test_not_a_database(self, wsgi, data_dir):
        text = wsgi.post_multipart('/load_db', [('dbfile', ('notes.txt', b'hello ' * 500)), ('submit', '1')]).text
        assert '数据库文件错误：不是有效的 SQLite 数据库文件。请选择 bustag 的 bus.db 文件' in text
        assert leftovers(data_dir) == []

    def test_no_file_chosen(self, wsgi):
        text = wsgi.post_multipart('/load_db', [('dbfile', ('', b'')), ('submit', '1')]).text
        assert '请上传数据库文件' in text
