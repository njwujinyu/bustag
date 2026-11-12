'''
本地番号与打标数据导入：add_local_fanhao、load_tags_db，以及 /local_fanhao、/load_db 页面。
测试里定时任务没有启动：库里还没有的番号记进抓取队列（db.FetchTask），不会联网
'''
import datetime
import io
import logging
import os
import sqlite3
import threading

from urllib.parse import urlsplit

import bottle
import pytest
from bs4 import BeautifulSoup

from bustag.app import index
from bustag.app.local import (UPLOAD_PREFIX, TagsImport, add_local_fanhao, load_tags_db, remove_stale_uploads,
                              save_upload)
from bustag.spider import backfill, db
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


def queued():
    '''抓取队列里的番号和来处，按排进去的先后'''
    return [(task.fanhao, task.origin) for task in db.FetchTask.select().order_by(db.FetchTask.id)]


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
    '''数据目录里剩下的上传临时文件'''
    return sorted(p.name for p in data_dir.glob(UPLOAD_PREFIX + '*'))


def upload_path(data_dir):
    '''load_tags_db 读的上传文件，和 save_upload 存的一样放在数据目录里'''
    return data_dir / (UPLOAD_PREFIX + 'test.db')


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
        # 横线前带数字的番号，以及其他常见写法
        ('300MIUM-1506', '300MIUM-1506', None),
        ('259luxu-1908', '259LUXU-1908', None),
        ('T28-123', 'T28-123', None),
        ('SSIS-001', 'SSIS-001', None),
        ('abp001', 'ABP-001', None),
        ('[FHD]SSIS-001.mp4', 'SSIS-001', None),
        ('SSIS-001,http://nas/a,b.mp4', 'SSIS-001', 'http://nas/a,b.mp4'),
        ('1080P SSIS-001', 'SSIS-001', None),
    ])
    def test_line_formats(self, line, fanhao, path):
        missed, local_added, tag_added = add_local_fanhao(line, tag_like=False)
        assert missed == [fanhao]
        assert (local_added, tag_added) == (1 if path else 0, 0)
        assert local_path(fanhao) == path
        assert rate_of(fanhao) is None

    def test_review_sample_lines(self):
        lines = ['300MIUM-1506', '259luxu-1908', 'T28-123', 'SSIS-001', 'abp001', '[FHD]SSIS-001.mp4',
                 'SSIS-002,http://nas/a,b.mp4']
        missed, local_added, _ = add_local_fanhao('\n'.join(lines), tag_like=False)
        assert missed == ['300MIUM-1506', '259LUXU-1908', 'T28-123', 'SSIS-001', 'ABP-001', 'SSIS-002']
        assert local_added == 1
        assert local_path('SSIS-002') == 'http://nas/a,b.mp4'

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

    def test_digit_prefixed_fanhao_is_kept(self):
        missed, local_added, _ = add_local_fanhao('123ABC-456,http://media.local/x.mp4', tag_like=False)
        assert local_added == 1
        assert missed == ['123ABC-456']

    def test_tag_like_replaces_system_rate(self, make_item):
        make_item('SYS-001')
        make_item('SYS-002')
        ItemRate.saveit(SYSTEM, DISLIKE, 'SYS-001')
        ItemRate.saveit(SYSTEM, DISLIKE, 'SYS-002')
        _, _, tag_added = add_local_fanhao('SYS-001\nSYS-002', tag_like=True)
        assert rate_of('SYS-001') == rate_of('SYS-002') == (USER, LIKE)
        assert tag_added == 2

    def test_repeated_fanhao_is_queued_once(self):
        missed, _, tag_added = add_local_fanhao('ABC-001\nabc001\n[FHD]ABC-001.mp4', tag_like=True)
        assert missed == ['ABC-001']
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
        path = make_tags_db(upload_path(data_dir), RATES, old_format=old_format)
        assert load_tags_db(str(path)) == TagsImport(added=2, missed_fanhaos=['ABC-002'])
        assert rate_of('ABC-001') == (USER, LIKE)
        assert rate_of('ABC-002') == (USER, DISLIKE)
        assert rate_of('ABC-003') is None
        assert rate_of('ABC-004') is None
        assert leftovers(data_dir) == []

    def test_wal_mode_database(self, data_dir):
        path = make_tags_db(upload_path(data_dir), RATES, wal=True)
        assert load_tags_db(str(path)) == TagsImport(added=2, missed_fanhaos=['ABC-001', 'ABC-002'])
        assert leftovers(data_dir) == []

    def test_existing_user_rates_are_kept(self, data_dir, make_item):
        make_item('ABC-001')
        ItemRate.saveit(USER, DISLIKE, 'ABC-001')
        path = make_tags_db(upload_path(data_dir), [('ABC-001', USER, LIKE)])
        assert load_tags_db(str(path)) == TagsImport(kept=1)
        assert rate_of('ABC-001') == (USER, DISLIKE)

    def test_import_replaces_system_rate(self, data_dir, make_item):
        make_item('SYS-002')
        ItemRate.saveit(SYSTEM, DISLIKE, 'SYS-002')
        path = make_tags_db(upload_path(data_dir), [('SYS-002', USER, LIKE)])
        assert load_tags_db(str(path)) == TagsImport(updated=1)
        assert rate_of('SYS-002') == (USER, LIKE)

    def test_system_rate_changed_to_user_rate_gets_new_time(self, data_dir, make_item, monkeypatch):
        now = datetime.datetime(2026, 11, 7, 9, 30)
        monkeypatch.setattr(db, 'get_now_time', lambda: now)
        make_item('SYS-002')
        ItemRate.create(item='SYS-002', rate_type=SYSTEM, rate_value=DISLIKE,
                        rete_time=datetime.datetime(2026, 1, 1))
        load_tags_db(str(make_tags_db(upload_path(data_dir), [('SYS-002', USER, LIKE)])))
        assert ItemRate.get_by_fanhao('SYS-002').rete_time == now

    def test_fanhaos_are_checked_and_uppercased(self, data_dir, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        path = make_tags_db(upload_path(data_dir), [
            ('/evil.example/x', USER, LIKE),
            ('../x', USER, LIKE),
            ('含 空格', USER, LIKE),
            ('ssis-001', USER, LIKE),
            ('SSIS-001', USER, LIKE),
            ('ABC-002', USER, 5),
        ])
        result = load_tags_db(str(path))
        assert result == TagsImport(added=1, kept=1, invalid=4, missed_fanhaos=['SSIS-001'])
        assert rate_of('SSIS-001') == (USER, LIKE)
        assert ItemRate.select().count() == 1
        assert ('导入打标数据：新增 1 条，由系统推荐改为用户打标 0 条，已有用户打标 1 条，'
                '跳过不合法的 4 条，1 个番号需要下载') in caplog.messages

    @pytest.mark.parametrize('write, message', [
        (write_garbage, '不是有效的 SQLite 数据库文件'),
        (write_empty, '不是 bustag 的数据库：找不到 item、item_rate 表'),
        (write_other_tables, '不是 bustag 的数据库：找不到 item、item_rate 表'),
        (write_missing_column, '数据库结构和 bustag 不一致：'),
    ], ids=['garbage', 'empty', 'other-tables', 'missing-column'])
    def test_not_a_bustag_database(self, data_dir, write, message):
        path = upload_path(data_dir)
        write(path)
        with pytest.raises(DBError, match=message):
            load_tags_db(str(path))
        assert leftovers(data_dir) == []
        assert ItemRate.select().count() == 0


def file_upload(content, filename='bus.db'):
    return bottle.FileUpload(io.BytesIO(content), 'dbfile', filename)


class TestUploadFiles:
    def test_each_upload_gets_its_own_file(self, data_dir, tmp_path):
        first = save_upload(file_upload(make_tags_db(tmp_path / 'a.db', [('ABC-001', USER, LIKE)]).read_bytes()))
        second = save_upload(file_upload(make_tags_db(tmp_path / 'b.db', [('ABC-002', USER, DISLIKE)]).read_bytes()))
        assert first != second
        assert os.path.dirname(first) == os.path.dirname(second) == str(data_dir)
        # 先存的后读：两次上传交错进行时，各自读到的还是自己上传的库
        assert load_tags_db(second) == TagsImport(added=1, missed_fanhaos=['ABC-002'])
        assert load_tags_db(first) == TagsImport(added=1, missed_fanhaos=['ABC-001'])
        assert leftovers(data_dir) == []

    def test_concurrent_imports_both_succeed(self, data_dir, monkeypatch):
        '''
        第一个导入在事务里读过库、还没写的时候，第二个导入开始：两个都要导入成功
        '''
        paths = {name: str(make_tags_db(data_dir / f'{UPLOAD_PREFIX}{name}.db', [(f'{name}-001', USER, LIKE)]))
                 for name in ('AAA', 'BBB')}
        real_save = ItemRate.save_user_rate
        first_read = threading.Event()
        second_read = threading.Event()

        def save_user_rate(fanhao, rate_value, replace_user_rate=False):
            ItemRate.get_by_fanhao(fanhao)
            if fanhao.startswith('AAA'):
                first_read.set()
                second_read.wait(1)
            else:
                second_read.set()
            return real_save(fanhao, rate_value, replace_user_rate)

        monkeypatch.setattr(ItemRate, 'save_user_rate', staticmethod(save_user_rate))
        results = {}
        errors = []

        def run(name):
            try:
                results[name] = load_tags_db(paths[name])
            except Exception as ex:
                errors.append(ex)
            finally:
                db.db.close()

        first = threading.Thread(target=run, args=('AAA',))
        first.start()
        assert first_read.wait(5)
        second = threading.Thread(target=run, args=('BBB',))
        second.start()
        first.join(15)
        second.join(15)
        assert errors == []
        assert results == {'AAA': TagsImport(added=1, missed_fanhaos=['AAA-001']),
                           'BBB': TagsImport(added=1, missed_fanhaos=['BBB-001'])}
        assert rate_of('AAA-001') == rate_of('BBB-001') == (USER, LIKE)
        assert leftovers(data_dir) == []

    def test_failed_save_leaves_nothing(self, data_dir):
        class BrokenUpload:
            def save(self, f):
                f.write(b'partial')
                raise OSError('磁盘已满')

        with pytest.raises(OSError, match='磁盘已满'):
            save_upload(BrokenUpload())
        assert leftovers(data_dir) == []

    def test_stale_uploads_are_removed(self, data_dir):
        names = ['upload-a1b2c3.db', 'upload-a1b2c3.db-wal', 'upload-a1b2c3.db-shm', 'upload-x.db-journal',
                 'bus.db', 'upload-notes.txt', 'uploaded.txt']
        for name in names:
            (data_dir / name).write_bytes(b'x')
        remove_stale_uploads()
        assert sorted(p.name for p in data_dir.iterdir() if p.is_file()) == [
            'bus.db', 'upload-notes.txt', 'uploaded.txt']

    def test_create_app_removes_stale_uploads(self, data_dir, monkeypatch):
        monkeypatch.setattr(index, 'start_scheduler', lambda: None)
        monkeypatch.setattr(index, 'start_file_log', lambda: None)
        monkeypatch.setattr(index, 'log_uncaught_exceptions', lambda: None)
        monkeypatch.setattr(index.instance, 'lock_data_dir', lambda url=None: True)
        (data_dir / 'upload-a1b2c3.db').write_bytes(b'x')
        index.create_app()
        assert leftovers(data_dir) == []


class TestLocalFanhaoPage:
    def test_submit(self, wsgi, make_item):
        make_item('ABC-001')
        response = wsgi.post('/local_fanhao', {
            'fanhao': 'ABC-001,http://media.local/ABC-001.mp4\r\nABC-002\r\n', 'tag_like': '1', 'submit': '1'})
        assert '已提交：1 个本地文件，2 个番号打标为喜欢，1 个番号加入下载队列' in response.text
        assert queued() == [('ABC-002', 'upload')]

    def test_submit_without_tag_like(self, wsgi):
        text = wsgi.post('/local_fanhao', {'fanhao': 'ABC-001', 'submit': '1'}).text
        assert '已提交：1 个番号加入下载队列' in text
        assert rate_of('ABC-001') is None

    def test_tag_like_counts_system_rates(self, wsgi, make_item):
        make_item('SYS-001')
        make_item('SYS-002')
        ItemRate.saveit(SYSTEM, DISLIKE, 'SYS-001')
        ItemRate.saveit(USER, DISLIKE, 'SYS-002')
        text = wsgi.post('/local_fanhao', {'fanhao': 'SYS-001\r\nSYS-002', 'tag_like': '1', 'submit': '1'}).text
        assert '已提交：1 个番号打标为喜欢' in text
        assert rate_of('SYS-001') == (USER, LIKE)
        assert rate_of('SYS-002') == (USER, DISLIKE)

    def test_new_likes_are_registered_for_actress_names(self, wsgi, make_item, fill_requests):
        make_item('ABC-001')
        wsgi.post('/local_fanhao', {'fanhao': 'ABC-001\r\nABC-002', 'tag_like': '1', 'submit': '1'})
        assert fill_requests == ['fill']

    def test_nothing_is_registered_without_new_likes(self, wsgi, make_item, fill_requests):
        make_item('ABC-001')
        ItemRate.saveit(USER, LIKE, 'ABC-001')
        wsgi.post('/local_fanhao', {'fanhao': 'ABC-001', 'tag_like': '1', 'submit': '1'})
        wsgi.post('/local_fanhao', {'fanhao': 'ABC-002,http://media.local/ABC-002.mp4', 'submit': '1'})
        assert fill_requests == []

    def test_nothing_new(self, wsgi):
        text = wsgi.post('/local_fanhao', {'fanhao': '没有番号', 'submit': '1'}).text
        assert 'alert-danger' in text
        assert '没有新增内容：没有识别到番号，或者这些番号都已经提交过' in text

    def test_page_without_submit_shows_no_message(self, wsgi):
        for response in (wsgi.get('/local_fanhao'), wsgi.post('/local_fanhao', {'fanhao': 'ABC-001'})):
            assert 'alert-success' not in response.text
            assert 'alert-danger' not in response.text
        assert LocalItem.select().count() == 0

    def test_large_submission(self, wsgi):
        lines = '\r\n'.join(f'ABC-{i:04d},http://media.local/library/parts/{i}/file.mp4' for i in range(1, 5001))
        assert len(lines) > 250 * 1024
        response = wsgi.post('/local_fanhao', {'fanhao': lines, 'submit': '1'})
        assert response.status == 200
        assert '已提交：5000 个本地文件，5000 个番号加入下载队列' in response.text

    def test_submission_over_10_mb(self, wsgi):
        response = wsgi.post('/local_fanhao', {'fanhao': 'ABC-001\r\n' * (11 * 1024 * 1024 // 9), 'submit': '1'})
        assert response.status == 413
        assert '<p>提交的内容太大（超过 10 MB），请分几次提交</p>' in response.text
        assert '<a href="/">返回首页</a>' in response.text
        assert LocalItem.select().count() == 0


def plain(node):
    return ' '.join(node.get_text(' ', strip=True).split())


def follow(wsgi, response):
    '''跟着表单提交后的重定向打开页面'''
    assert response.status == 303
    parts = urlsplit(response.headers['location'])
    return wsgi.get(parts.path + ('?' + parts.query if parts.query else ''))


@pytest.fixture
def runs(monkeypatch):
    '''登记"有排队的影片要抓"的次数（测试里定时任务没有启动）'''
    calls = []
    monkeypatch.setattr(index, 'request_queue_run', lambda: calls.append('run'))
    return calls


def mark(fanhao, status, detail='', tries=0):
    db.FetchTask.update(status=status, detail=detail, tries=tries).where(db.FetchTask.fanhao == fanhao).execute()


class TestDownloadQueueOnTheUploadPage:
    '''上传番号页上的"下载队列"：还在排队几个，没抓到的逐个列出（番号和原因），失败的可以点一下再试'''

    def test_submitted_codes_are_queued_and_a_run_is_requested(self, wsgi, runs):
        wsgi.post('/local_fanhao', {'fanhao': 'ABC-001\r\nABC-002', 'submit': '1'})
        assert queued() == [('ABC-001', 'upload'), ('ABC-002', 'upload')]
        assert runs == ['run']

    def test_codes_already_in_the_library_request_no_run(self, wsgi, make_item, runs):
        make_item('ABC-001')
        wsgi.post('/local_fanhao', {'fanhao': 'ABC-001,http://media.local/a.mp4', 'submit': '1'})
        assert (queued(), runs) == ([], [])

    def test_imported_codes_are_queued_and_a_run_is_requested(self, wsgi, tmp_path, make_item, runs):
        make_item('ABC-001')
        upload(wsgi, make_tags_db(tmp_path / 'bus.db', RATES).read_bytes())
        assert queued() == [('ABC-002', 'import')]
        assert runs == ['run']

    def test_the_page_tells_what_waits_and_lists_what_was_not_fetched(self, wsgi):
        backfill.enqueue_codes(['ABC-001', 'ABC-002', 'ABC-003', 'ABC-004'], db.ORIGIN_UPLOAD)
        mark('ABC-002', 'failed', '<b>缺少发行日期</b>')
        mark('ABC-003', 'missing', '页面不存在（HTTP 404）')
        mark('ABC-004', 'done')
        text = wsgi.get('/local_fanhao').text
        block = BeautifulSoup(text, 'html.parser').select_one('#code-queue')
        assert '还在排队 1 个番号' in plain(block)
        rows = [(plain(row.select_one('.code-fanhao')), plain(row.select_one('.code-result')),
                 plain(row.select_one('.code-reason')), row.select_one('button') is not None)
                for row in block.select('li.code-problem')]
        assert rows == [('ABC-003', '网站上没有', '页面不存在（HTTP 404）', False),
                        ('ABC-002', '失败', '<b>缺少发行日期</b>', True)]
        assert '<b>缺少发行日期</b>' not in text

    def test_an_empty_queue_shows_nothing(self, wsgi):
        assert BeautifulSoup(wsgi.get('/local_fanhao').text, 'html.parser').select_one('#code-queue') is None

    def test_a_failed_code_can_be_tried_again(self, wsgi, runs):
        backfill.enqueue_codes(['ABC-001', 'ABC-002'], db.ORIGIN_UPLOAD)
        mark('ABC-001', 'failed', '缺少发行日期', tries=3)
        mark('ABC-002', 'failed', '缺少发行日期')
        response = wsgi.post('/local_fanhao/retry', {'fanhao': 'ABC-001'})
        assert '已重新排进下载队列：1 个番号' in follow(wsgi, response).text
        assert {task.fanhao: (task.status, task.tries) for task in db.FetchTask.select()} == {
            'ABC-001': ('queued', 0), 'ABC-002': ('failed', 0)}
        assert runs == ['run']

    def test_every_failed_code_can_be_tried_again_at_once(self, wsgi, runs):
        backfill.enqueue_codes(['ABC-001', 'ABC-002', 'ABC-003'], db.ORIGIN_UPLOAD)
        mark('ABC-001', 'failed')
        mark('ABC-002', 'failed')
        mark('ABC-003', 'missing')
        response = wsgi.post('/local_fanhao/retry', {'all': '1'})
        assert '已重新排进下载队列：2 个番号' in follow(wsgi, response).text
        assert {task.fanhao: task.status for task in db.FetchTask.select()} == {
            'ABC-001': 'queued', 'ABC-002': 'queued', 'ABC-003': 'missing'}

    def test_trying_again_a_code_that_has_not_failed_changes_nothing(self, wsgi, runs):
        backfill.enqueue_codes(['ABC-001'], db.ORIGIN_UPLOAD)
        response = wsgi.post('/local_fanhao/retry', {'fanhao': 'ABC-001'})
        assert '没有失败的番号要再试，没有改动' in follow(wsgi, response).text
        assert runs == []

    @pytest.mark.parametrize('data', [{}, {'fanhao': '../x'}, {'fanhao': 'ABC-001', 'all': '1'}])
    def test_odd_retry_requests_are_refused(self, wsgi, data):
        assert wsgi.post('/local_fanhao/retry', data).status == 400

    def test_the_list_of_codes_not_fetched_can_be_cleared(self, wsgi):
        backfill.enqueue_codes(['ABC-001', 'ABC-002', 'ABC-003'], db.ORIGIN_UPLOAD)
        mark('ABC-001', 'failed')
        mark('ABC-002', 'missing')
        response = wsgi.post('/local_fanhao/clear', {})
        assert '已清除 2 条没有抓到的记录' in follow(wsgi, response).text
        assert queued() == [('ABC-003', 'upload')]

    @pytest.mark.parametrize('path', ['/local_fanhao/retry', '/local_fanhao/clear'])
    def test_changes_need_a_post(self, wsgi, path):
        assert wsgi.get(path).status == 405


def upload(wsgi, content):
    return wsgi.post_multipart('/load_db', [('dbfile', ('bus.db', content)), ('submit', '1')]).text


class TestLoadDbPage:
    def test_upload(self, wsgi, tmp_path, data_dir, make_item):
        make_item('ABC-001')
        text = upload(wsgi, make_tags_db(tmp_path / 'bus.db', RATES).read_bytes())
        assert 'alert-success' in text
        assert '已导入 2 条打标，1 个番号加入下载队列' in text
        assert '需要下载其他数据' not in text
        assert rate_of('ABC-002') == (USER, DISLIKE)
        assert leftovers(data_dir) == []
        assert queued() == [('ABC-002', 'import')]

    def test_imported_tags_are_registered_for_actress_names(self, wsgi, tmp_path, make_item, fill_requests):
        make_item('ABC-001')
        content = make_tags_db(tmp_path / 'bus.db', RATES).read_bytes()
        upload(wsgi, content)
        assert fill_requests == ['fill']
        # 再导入一遍没有新的打标，不用再登记
        upload(wsgi, content)
        assert fill_requests == ['fill']

    def test_upload_large_database(self, wsgi, tmp_path):
        content = make_tags_db(tmp_path / 'bus.db', RATES, filler=3 * 1024 * 1024).read_bytes()
        assert len(content) > 3 * 1024 * 1024
        assert '已导入 2 条打标，2 个番号加入下载队列' in upload(wsgi, content)

    def test_same_file_twice(self, wsgi, tmp_path, make_item):
        make_item('ABC-001')
        make_item('ABC-002')
        content = make_tags_db(tmp_path / 'bus.db', RATES).read_bytes()
        assert '已导入 2 条打标' in upload(wsgi, content)
        text = upload(wsgi, content)
        assert 'alert-success' in text
        assert '没有新的打标（都已导入过）' in text
        assert '已导入 ' not in text
        assert '加入下载队列' not in text
        assert '需要下载其他数据' not in text

    def test_system_rates_changed_to_user_rates_are_counted(self, wsgi, tmp_path, make_item):
        make_item('ABC-001')
        make_item('ABC-002')
        ItemRate.saveit(USER, DISLIKE, 'ABC-001')
        ItemRate.saveit(SYSTEM, LIKE, 'ABC-002')
        text = upload(wsgi, make_tags_db(tmp_path / 'bus.db', RATES).read_bytes())
        assert '已导入 1 条打标（其中 1 条原来是系统推荐），1 条已有用户打标，没有改动' in text
        assert rate_of('ABC-001') == (USER, DISLIKE)
        assert rate_of('ABC-002') == (USER, DISLIKE)

    def test_invalid_rows_are_reported(self, wsgi, tmp_path):
        rows = [('ABC-001', USER, LIKE), ('/evil.example/x', USER, LIKE), ('含 空格', USER, DISLIKE)]
        text = upload(wsgi, make_tags_db(tmp_path / 'bus.db', rows).read_bytes())
        assert '已导入 1 条打标，1 个番号加入下载队列，跳过 2 条番号或评分不合法的打标' in text
        assert [rate.item_id for rate in ItemRate.select()] == ['ABC-001']

    @pytest.mark.parametrize('rows, message', [
        ([('ABC-003', SYSTEM, LIKE), ('ABC-004', None, None)], '上传的数据库里没有可导入的用户打标'),
        ([('../x', USER, LIKE)], '上传的数据库里没有可导入的用户打标，跳过 1 条番号或评分不合法的打标'),
    ], ids=['no-user-rates', 'only-invalid'])
    def test_nothing_to_import(self, wsgi, tmp_path, rows, message):
        text = upload(wsgi, make_tags_db(tmp_path / 'bus.db', rows).read_bytes())
        assert 'alert-danger' in text
        assert 'alert-success' not in text
        assert message in text
        assert ItemRate.select().count() == 0

    def test_not_a_database(self, wsgi, data_dir):
        text = wsgi.post_multipart('/load_db', [('dbfile', ('notes.txt', b'hello ' * 500)), ('submit', '1')]).text
        assert '数据库文件错误：不是有效的 SQLite 数据库文件。请选择 bustag 的 bus.db 文件' in text
        assert leftovers(data_dir) == []

    def test_no_file_chosen(self, wsgi):
        text = wsgi.post_multipart('/load_db', [('dbfile', ('', b'')), ('submit', '1')]).text
        assert '请上传数据库文件' in text
