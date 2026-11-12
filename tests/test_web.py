'''
网页：直接调用 WSGI 应用（不开端口）。封面下载走 FakeFetcher，启动相关的函数用假的 socket / 服务器
'''
import datetime
import errno
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import bottle
import pytest
from bs4 import BeautifulSoup
from peewee import OperationalError

import bustag
import bustag.main
from bustag import util
from bustag.app import index, schedule
from bustag.app.local import add_local_fanhao
from bustag.model import classifier, persist
from bustag.model.persist import LEGACY_MODEL, ModelLoadError, read_model
from bustag.spider import avwiki, covers, db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate, LocalItem
from bustag.spider.fetcher import FetchError

ROOT = Path(__file__).resolve().parent.parent
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 4096
TOP_NAV_ACTIVE = re.compile(r'<li class="nav-item active">\s*<a class="nav-link" href="([^"]+)"')
NOW = datetime.datetime(2026, 11, 6, 12, 0, 0)


def covers_on(text):
    '''页面上列出的影片（按封面地址里的番号，保持顺序）'''
    return re.findall(r'src="/cover/([^"]+)"', text)


def redirect_target(response):
    assert response.status == 303
    parts = urlsplit(response.headers['location'])
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}, parts.fragment


class TestImport:
    def test_import_does_not_start_scheduler_or_threads(self, tmp_path):
        code = ('import json, threading\n'
                'import bustag.app.index\n'
                'from bustag.app import schedule\n'
                'print(json.dumps({"scheduler": repr(schedule.scheduler),'
                ' "threads": [t.name for t in threading.enumerate()]}))\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith('BUSTAG_')}
        env.update(BUSTAG_DATA_DIR=str(tmp_path / 'data'), PYTHONPATH=str(ROOT), PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, '-c', code], cwd=str(tmp_path), env=env,
                                capture_output=True, encoding='utf-8', errors='replace', timeout=180)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip().splitlines()[-1]) == {'scheduler': 'None', 'threads': ['MainThread']}

    def test_requests_close_the_database_connection(self, wsgi, make_item):
        make_item('ABC-001')
        wsgi.get('/tagit')
        assert db.db.is_closed()
        assert db.Item.select().count() == 1


class TestPages:
    @pytest.mark.parametrize('path, title', [
        ('/', '推荐'),
        ('/tagit', '打标'),
        ('/local', '本地文件'),
        ('/model', '模型'),
        ('/load_db', '数据'),
        ('/about', '关于'),
    ])
    def test_top_level_pages(self, wsgi, path, title):
        response = wsgi.get(path)
        assert response.status == 200
        assert response.headers['content-type'] == 'text/html; charset=UTF-8'
        assert f'<title>{title}</title>' in response.text
        assert TOP_NAV_ACTIVE.findall(response.text) == [path]
        assert f'bustag v{bustag.__version__} · 维护版' in response.text

    def test_upload_fanhao_page_is_a_tab_of_local_page(self, wsgi):
        response = wsgi.get('/local_fanhao')
        assert response.status == 200
        assert '<a class="nav-link active" href="/local_fanhao">上传番号</a>' in response.text
        assert 'name="fanhao"' in response.text

    def test_about_page(self, wsgi):
        text = wsgi.get('/about').text
        assert '免责声明' in text
        assert 'https://github.com/njwujinyu/bustag' in text

    @pytest.mark.parametrize('path', ['/', '/tagit', '/local'])
    def test_empty_lists(self, wsgi, path):
        text = wsgi.get(path).text
        assert covers_on(text) == []
        assert '暂无数据' in text

    @pytest.mark.parametrize('path', ['/', '/about', '/nope'])
    def test_pages_declare_chinese(self, wsgi, path):
        assert '<html lang="zh-CN">' in wsgi.get(path).text

    def test_static_files(self, wsgi):
        response = wsgi.get('/static/css/bustag.css')
        assert response.status == 200
        assert response.headers['content-type'].startswith('text/css')
        assert wsgi.get('/static/images/nocover.svg').status == 200
        assert wsgi.get('/static/images/missing.png').status == 404

    @pytest.mark.parametrize('path', ['/static/../views/base.tpl', '/static/..\\views\\base.tpl',
                                      '/static/../../util.py'])
    def test_static_files_stay_inside_static_folder(self, wsgi, path):
        response = wsgi.get(path)
        assert response.status in (403, 404)
        assert b'rebase' not in response.body
        assert b'import' not in response.body


def plain(node):
    '''节点里的文字，空白收成一个空格'''
    return ' '.join(node.get_text(' ', strip=True).split())


def about_tasks(text):
    '''关于页"后台任务"里的每一行：{名称: 文字}'''
    block = BeautifulSoup(text, 'html.parser').select_one('#background-tasks')
    return {plain(name): plain(name.find_next_sibling('dd')) for name in block.select('dt')}


class TestBackgroundTasksOnTheAboutPage:
    @pytest.fixture(autouse=True)
    def nothing_ran(self, monkeypatch):
        monkeypatch.setattr(schedule, 'scheduler', None)
        monkeypatch.setattr(schedule, 'last_download', None)
        monkeypatch.setattr(schedule, 'download_state', lambda: (None, None))
        monkeypatch.setattr(schedule, 'fill_state', lambda: (None, None))
        monkeypatch.setattr(avwiki, 'last_round', None)
        monkeypatch.setattr(avwiki, 'get_now_time', lambda: NOW)

    @pytest.fixture
    def started(self, monkeypatch):
        '''定时任务已经启动，下一次抓取在 2026-11-06 07:00（UTC）'''
        next_run = datetime.datetime(2026, 11, 6, 7, 0, 0, tzinfo=datetime.timezone.utc)
        job = types.SimpleNamespace(next_run_time=next_run)
        monkeypatch.setattr(schedule, 'scheduler', types.SimpleNamespace(get_job={'download': job}.get))
        return next_run.astimezone().strftime('%Y-%m-%d %H:%M:%S')

    def test_before_the_scheduler_started(self, wsgi):
        tasks = about_tasks(wsgi.get('/about').text)
        assert tasks['定时抓取'] == '定时任务没有启动。'
        assert tasks['补演员名'] == f'现在没有缺演员的影片。{self.TODAY}程序启动后还没有补过。'

    @pytest.mark.parametrize('interval, every', [('10800', '每 3 小时'), ('5400', '每 90 分钟'), ('90', '每 90 秒')])
    def test_download_row_tells_the_interval_and_the_next_run(self, wsgi, started, monkeypatch, interval, every):
        monkeypatch.setitem(util.APP_CONFIG, 'download.interval', interval)
        tasks = about_tasks(wsgi.get('/about').text)
        assert tasks['定时抓取'] == f'{every}自动抓取一次。程序启动后还没有抓取过。下一次：{started}。'

    @pytest.mark.parametrize('ending', ['', '。'], ids=['plain', 'ends-with-a-full-stop'])
    def test_download_row_tells_how_the_last_download_went(self, wsgi, started, monkeypatch, ending):
        summary = '抓取结束：新增 0 部，跳过 0 部，失败 0 部，用时 22 秒（列表页读取失败）。出错：列表第 1 页读取失败'
        monkeypatch.setattr(schedule, 'last_download', (NOW, summary + ending))
        tasks = about_tasks(wsgi.get('/about').text)
        assert tasks['定时抓取'] == f'每 3 小时自动抓取一次。上一次（2026-11-06 12:00:00）{summary}。下一次：{started}。'

    def test_download_row_while_the_first_download_is_running(self, wsgi, started, monkeypatch):
        # 启动后几秒就开始第一次抓取，要抓一阵子：这时不能还说"还没有抓取过"
        monkeypatch.setattr(schedule, 'download_state', lambda: ('running', NOW))
        tasks = about_tasks(wsgi.get('/about').text)
        assert tasks['定时抓取'] == f'每 3 小时自动抓取一次。现在正在抓取（2026-11-06 12:00:00 开始）。下一次：{started}。'

    def test_download_row_while_a_later_download_is_running(self, wsgi, started, monkeypatch):
        monkeypatch.setattr(schedule, 'download_state', lambda: ('running', NOW + datetime.timedelta(hours=3)))
        monkeypatch.setattr(schedule, 'last_download', (NOW, '抓取结束：新增 3 部，跳过 0 部，失败 0 部，用时 40 秒'))
        tasks = about_tasks(wsgi.get('/about').text)
        assert tasks['定时抓取'] == ('每 3 小时自动抓取一次。现在正在抓取（2026-11-06 15:00:00 开始）。'
                                 '上一次（2026-11-06 12:00:00）抓取结束：新增 3 部，跳过 0 部，失败 0 部，用时 40 秒。'
                                 f'下一次：{started}。')

    def test_download_row_while_the_download_waits_for_another_job(self, wsgi, started, monkeypatch):
        # 所有后台任务排成一队：到了抓取的时间，前面的任务（训练、补演员名、按番号抓取）还没做完
        monkeypatch.setattr(schedule, 'download_state', lambda: ('queued', None))
        tasks = about_tasks(wsgi.get('/about').text)
        assert tasks['定时抓取'] == f'每 3 小时自动抓取一次。已经到了抓取的时间，正在等前面的任务做完。下一次：{started}。'

    def test_log_row_tells_where_the_log_file_is(self, wsgi, monkeypatch, data_dir):
        path = str(data_dir / 'logs' / 'bustag.log')
        monkeypatch.setattr(index, 'active_log_file', lambda: path)
        assert about_tasks(wsgi.get('/about').text)['运行日志'] == (
            f'{path}（写满 1 MB 换一个新文件，另外保留最近 5 个旧文件）。')

    def test_log_row_when_no_log_file_is_written(self, wsgi):
        assert about_tasks(wsgi.get('/about').text)['运行日志'] == '这次运行没有写日志文件，日志只显示在控制台。'

    def like_without_star(self, make_item, fanhao):
        make_item(fanhao)
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, fanhao)

    TODAY = '今天向 av-wiki 请求了 0 次，还可以请求 200 次（每天最多 200 次）。'

    @pytest.fixture(autouse=True)
    def requests_today(self, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.avwiki_daily', '200')
        monkeypatch.setattr(avwiki, 'get_now_time', lambda: NOW)

    def test_actress_row_counts_the_movies_that_wait_and_tells_the_last_round(self, wsgi, make_item, monkeypatch):
        day = datetime.timedelta(days=1)
        for n in range(1, 5):
            self.like_without_star(make_item, f'ABC-{n:03d}')
        make_item('ABC-010')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-010')
        make_item('ABC-011')
        db.save_lookup('ABC-003', db.LOOKUP_MISSING, 1, NOW, NOW + day, 'av-wiki 的搜索结果里没有这个番号')
        db.save_lookup('ABC-004', db.LOOKUP_FAILED, 0, NOW, NOW, '网络连接失败（ConnectTimeout）')
        for n in (5, 6, 7):
            make_item(f'ABC-{n:03d}', tags=[('star', '演员甲')])
            db.save_lookup(f'ABC-{n:03d}', db.LOOKUP_FILLED, 0, NOW, NOW)
        db.JobState.create(name=avwiki.REQUESTS, text='38', at=NOW - datetime.timedelta(hours=1))
        monkeypatch.setattr(avwiki, 'last_round', avwiki.FillRound(
            finished_at=NOW, due=5, filled=3, names=3, missing=1, failed=1, pages=2, requests=6,
            error='ABC-004：网络连接失败（ConnectTimeout）'))
        assert about_tasks(wsgi.get('/about').text)['补演员名'] == (
            '还有 6 部影片没有演员（其中喜欢的 4 部，在「推荐」里的 1 部）：5 部下一轮去 av-wiki 补，'
            '1 部上次没查到、过些天再试。到现在一共补到 3 部。'
            '今天向 av-wiki 请求了 38 次，还可以请求 162 次（每天最多 200 次）。'
            '上一轮（2026-11-06 12:00:00）补到 3 部，av-wiki 上没有 1 部，失败 1 部，读了 2 个女优页。'
            '上次失败的原因：网络连接失败（ConnectTimeout）（ABC-004，2026-11-06 12:00:00）。')

    def test_actress_row_when_every_movie_waits_for_the_next_round(self, wsgi, make_item, monkeypatch):
        self.like_without_star(make_item, 'ABC-001')
        monkeypatch.setattr(avwiki, 'last_round', avwiki.FillRound(finished_at=NOW))
        assert about_tasks(wsgi.get('/about').text)['补演员名'] == (
            '还有 1 部影片没有演员（其中喜欢的 1 部，在「推荐」里的 0 部），下一轮去 av-wiki 补。'
            f'{self.TODAY}上一轮（2026-11-06 12:00:00）没有要补的影片。')

    def test_actress_row_when_the_site_has_none_of_them(self, wsgi, make_item):
        self.like_without_star(make_item, 'ABC-001')
        db.save_lookup('ABC-001', db.LOOKUP_MISSING, 1, NOW, NOW + datetime.timedelta(days=1), 'av-wiki 上没有')
        assert about_tasks(wsgi.get('/about').text)['补演员名'] == (
            '还有 1 部影片没有演员（其中喜欢的 1 部，在「推荐」里的 0 部），av-wiki 上次没查到，过些天再试。'
            f'{self.TODAY}程序启动后还没有补过。')

    def test_actress_row_when_every_movie_has_actresses(self, wsgi, make_item):
        make_item('ABC-001', tags=[('star', '演员甲')])
        assert about_tasks(wsgi.get('/about').text)['补演员名'] == (
            f'现在没有缺演员的影片。{self.TODAY}程序启动后还没有补过。')

    def test_actress_row_while_the_first_round_is_running(self, wsgi, make_item, monkeypatch):
        # 启动后几秒就开始第一轮，要补一阵子：这时不能还说"还没有补过"
        self.like_without_star(make_item, 'ABC-001')
        monkeypatch.setattr(schedule, 'fill_state', lambda: ('running', NOW))
        assert about_tasks(wsgi.get('/about').text)['补演员名'] == (
            '还有 1 部影片没有演员（其中喜欢的 1 部，在「推荐」里的 0 部），下一轮去 av-wiki 补。'
            f'{self.TODAY}现在正在补一轮（2026-11-06 12:00:00 开始）。')

    def test_actress_row_while_the_first_round_waits_for_its_turn(self, wsgi, make_item, monkeypatch):
        self.like_without_star(make_item, 'ABC-001')
        monkeypatch.setattr(schedule, 'fill_state', lambda: ('queued', None))
        assert about_tasks(wsgi.get('/about').text)['补演员名'].endswith(f'{self.TODAY}已经排了一轮，轮到就开始。')

    def test_actress_row_while_a_later_round_is_running(self, wsgi, make_item, monkeypatch):
        self.like_without_star(make_item, 'ABC-001')
        monkeypatch.setattr(schedule, 'fill_state', lambda: ('running', NOW + datetime.timedelta(hours=3)))
        monkeypatch.setattr(avwiki, 'last_round', avwiki.FillRound(finished_at=NOW, due=2, filled=2, names=2))
        assert about_tasks(wsgi.get('/about').text)['补演员名'].endswith(
            f'{self.TODAY}现在正在补一轮（2026-11-06 15:00:00 开始）。'
            '上一轮（2026-11-06 12:00:00）补到 2 部，av-wiki 上没有 0 部，失败 0 部。')

    def test_actress_row_tells_why_the_last_round_stopped_early(self, wsgi, make_item, monkeypatch):
        for n in range(1, 6):
            self.like_without_star(make_item, f'ABC-{n:03d}')
        monkeypatch.setattr(avwiki, 'last_round', avwiki.FillRound(
            finished_at=NOW, due=5, failed=3, left=2, stopped='连续 3 次请求失败', error='ABC-003：网络连接失败'))
        text = about_tasks(wsgi.get('/about').text)['补演员名']
        assert '上一轮（2026-11-06 12:00:00）补到 0 部，av-wiki 上没有 0 部，失败 3 部，连续 3 次请求失败就停下了，还有 2 部留到下一轮。' in text

    def test_actress_row_tells_the_daily_limit_was_reached(self, wsgi, make_item, monkeypatch):
        self.like_without_star(make_item, 'ABC-001')
        db.JobState.create(name=avwiki.REQUESTS, text='200', at=NOW)
        monkeypatch.setattr(avwiki, 'last_round', avwiki.FillRound(
            finished_at=NOW, due=3, filled=2, left=1, requests=5, stopped='今天向 av-wiki 的请求到了每天 200 次的上限'))
        text = about_tasks(wsgi.get('/about').text)['补演员名']
        assert '今天向 av-wiki 请求了 200 次，还可以请求 0 次（每天最多 200 次），剩下的明天接着补。' in text
        assert '补到 2 部，av-wiki 上没有 0 部，失败 0 部，今天向 av-wiki 的请求到了每天 200 次的上限就停下了，还有 1 部留到下一轮。' in text

    def test_actress_row_tells_how_far_the_correction_after_the_upgrade_is(self, wsgi, make_item, cast,
                                                                           monkeypatch):
        cast.movie('OLD-001')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'OLD-001')
        cast.legacy('OLD-001', 'jia', '演员甲', 'えんいんこう')
        db.save_lookup('OLD-001', db.LOOKUP_FILLED, 0, NOW, NOW)
        from bustag.spider import actresses
        monkeypatch.setattr(actresses, 'get_now_time', lambda: NOW)
        actresses.migrate()
        monkeypatch.setattr(avwiki, 'last_round', avwiki.FillRound(finished_at=NOW, due=1, corrected=0, failed=1))
        text = about_tasks(wsgi.get('/about').text)['补演员名']
        assert '数据库升级以后按新存法重新查：还有 1 部影片、1 位女优的女优页没查完。' in text
        db.ActressLookup.update(status=db.LOOKUP_FILLED).execute()
        db.Actress.update(names_at=NOW).execute()
        actresses.finish_correction(NOW)
        text = about_tasks(wsgi.get('/about').text)['补演员名']
        assert '数据库升级以后的校正已经做完（2026-11-06 12:00:00）。' in text

    def test_actress_row_tells_that_some_movies_are_looked_up_again_in_a_few_days(self, wsgi, make_item, cast,
                                                                                  monkeypatch):
        from bustag.spider import actresses
        for fanhao in ('OLD-001', 'OLD-002'):
            cast.movie(fanhao)
            ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, fanhao)
            cast.legacy(fanhao, 'jia', '演员甲', 'えんいんこう')
            db.save_lookup(fanhao, db.LOOKUP_FILLED, 0, NOW, NOW)
        monkeypatch.setattr(actresses, 'get_now_time', lambda: NOW)
        actresses.migrate()
        db.save_lookup('OLD-002', db.LOOKUP_STALE, 1, NOW, NOW + datetime.timedelta(days=1))
        text = about_tasks(wsgi.get('/about').text)['补演员名']
        assert ('数据库升级以后按新存法重新查：还有 2 部影片、1 位女优的女优页没查完'
                '（其中 1 部这次在 av-wiki 上没查到，过几天再查一次）。') in text

    def test_actress_row_tells_why_the_upgrade_did_not_happen(self, wsgi, make_item, monkeypatch):
        from bustag.spider import actresses
        monkeypatch.setattr(actresses, 'last_migration', actresses.Migration(
            'failed', NOW, reason='升级前的快照没有做成（磁盘空间不足）'))
        text = about_tasks(wsgi.get('/about').text)['补演员名']
        assert text.startswith('演员数据还没有升级（2026-11-06 12:00:00 试过）：升级前的快照没有做成（磁盘空间不足）。'
                               '程序开着的话明天会自动再试一次（重启程序时也会再试），在那之前不去 av-wiki 补演员名。')

    def test_movies_waiting_for_names_wait_for_the_upgrade_when_it_did_not_happen(self, wsgi, make_item,
                                                                                    monkeypatch):
        from bustag.spider import actresses
        self.like_without_star(make_item, 'ABC-001')
        monkeypatch.setattr(actresses, 'last_migration', actresses.Migration('failed', NOW, reason='数据库正被别的程序占用'))
        text = about_tasks(wsgi.get('/about').text)['补演员名']
        assert '还有 1 部影片没有演员（其中喜欢的 1 部，在「推荐」里的 0 部），升级做完以后再去 av-wiki 补。' in text
        assert '下一轮' not in text

    def test_actress_row_tells_why_the_names_left_by_an_older_version_are_not_tidied_yet(self, wsgi, make_item,
                                                                                        monkeypatch):
        # 迁移过的库里旧版本又写了旧存法的名字、这次没整理成：别的影片照常补名
        from bustag.spider import actresses
        self.like_without_star(make_item, 'ABC-001')
        monkeypatch.setattr(actresses, 'last_migration', actresses.Migration(
            'failed', NOW, reason='数据库正被别的程序占用', again=True))
        text = about_tasks(wsgi.get('/about').text)['补演员名']
        assert text.startswith('旧版本按旧存法写下的演员名还没有整理（2026-11-06 12:00:00 试过）：数据库正被别的程序占用。'
                               '程序开着的话明天会自动再试一次（重启程序时也会再试）。还有 1 部影片没有演员'
                               '（其中喜欢的 1 部，在「推荐」里的 0 部），下一轮去 av-wiki 补。')

    def test_a_long_reason_is_cut_short(self, wsgi, make_item):
        self.like_without_star(make_item, 'ABC-001')
        db.save_lookup('ABC-001', db.LOOKUP_FAILED, 0, NOW, NOW, '网络连接失败（' + '很长的原因' * 60 + '）')
        text = about_tasks(wsgi.get('/about').text)['补演员名']
        reason = ('网络连接失败（' + '很长的原因' * 60)[:160]
        assert text.endswith(f'上次失败的原因：{reason}…（ABC-001，2026-11-06 12:00:00）。')

    def test_reasons_are_html_escaped(self, wsgi, make_item):
        self.like_without_star(make_item, 'ABC-001')
        db.save_lookup('ABC-001', db.LOOKUP_FAILED, 0, NOW, NOW, '<script>alert(1)</script>')
        text = wsgi.get('/about').text
        assert '<script>alert(1)</script>' not in text
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in text

    def test_a_reason_saved_earlier_is_shown_without_the_name_in_the_address(self, wsgi, make_item):
        # 女优页的地址里有她名字的罗马字；以前的版本把整个地址记进了失败的原因
        self.like_without_star(make_item, 'ABC-001')
        db.save_lookup('ABC-001', db.LOOKUP_FAILED, 0, NOW, NOW, 'HTTP 503：https://av-wiki.net/av-actress/mingzi-jia/')
        text = about_tasks(wsgi.get('/about').text)['补演员名']
        assert text.endswith('上次失败的原因：HTTP 503：https://av-wiki.net/av-actress/…/（ABC-001，2026-11-06 12:00:00）。')
        assert 'mingzi' not in wsgi.get('/about').text


def cards_on(text):
    '''页面上每部影片的卡片：[(番号, 相似度, 因为, 拉低分数的)]，没有的那一项是 None'''
    cards = []
    for row in BeautifulSoup(text, 'html.parser').select('div.row'):
        cover = row.select_one('img.coverimg')
        if cover is not None:
            parts = [row.select_one(selector) for selector in ('.item-score .badge', '.item-because', '.item-against')]
            cards.append((cover['data-cover'].rsplit('/', 1)[1], *(plain(part) if part else None for part in parts)))
    return cards


def recommend_tabs(text):
    '''推荐页的标签页：[(文字, 地址, 是不是当前页)]'''
    return [(plain(link), link['href'], 'active' in link['class'])
            for link in BeautifulSoup(text, 'html.parser').select('ul.nav-tabs a.nav-link')]


def top(count):
    return lambda total: min(count, total)


class TestRecommendPage:
    @pytest.fixture
    def scored(self, make_item, monkeypatch):
        '''
        六部影片：ABC-002 到 ABC-005 有分数（分数最高的两部在"推荐"里），ABC-001 只有以前的模型留下的系统评分、
        没有分数，ABC-006 用户打过标
        '''
        monkeypatch.setattr(db, 'get_now_time', lambda: NOW)
        for n in range(1, 7):
            make_item(f'ABC-00{n}')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ABC-006')
        db.save_scores([db.Score('ABC-005', 0.874, ['标签一', '演员甲'], ['标签九']),
                        db.Score('ABC-004', 0.126, ['标签四'], ['标签二', '标签三']),
                        db.Score('ABC-003', 0.957, ['标签一'], []),
                        db.Score('ABC-002', 0.5, [], [])], top(2))
        ItemRate.create(item='ABC-001', rate_type=RATE_TYPE.SYSTEM_RATE, rate_value=RATE_VALUE.LIKE, rete_time=NOW)
        db.Item.update(add_date=NOW).execute()

    def test_the_two_tabs_are_recommended_and_not_so_alike(self, wsgi, scored):
        assert recommend_tabs(wsgi.get('/').text) == [('推荐', '/?like=1', True), ('不太像', '/?like=0', False),
                                                     ('指定推荐', '/pick', False)]
        assert recommend_tabs(wsgi.get('/?like=0').text)[:2] == [('推荐', '/?like=1', False), ('不太像', '/?like=0', True)]

    def test_both_tabs_list_the_highest_score_first(self, wsgi, scored):
        recommended = wsgi.get('/').text
        # 没有分数的排在有分数的后面
        assert covers_on(recommended) == ['ABC-003', 'ABC-005', 'ABC-001']
        assert covers_on(wsgi.get('/?like=0').text) == ['ABC-002', 'ABC-004']
        assert '今日更新 6 , 今日推荐 3' in recommended

    def test_cards_show_the_similarity_and_the_tags_behind_it(self, wsgi, scored):
        assert cards_on(wsgi.get('/').text) == [
            ('ABC-003', '相似度 96%', '因为：标签一', None),
            ('ABC-005', '相似度 87%', '因为：标签一、演员甲', None),
            ('ABC-001', None, None, None)]
        assert cards_on(wsgi.get('/?like=0').text) == [
            ('ABC-002', '相似度 50%', None, None),
            ('ABC-004', '相似度 13%', '因为：标签四', '拉低分数的：标签二、标签三')]

    def test_recommended_movies_are_marked_green_and_the_others_grey(self, wsgi, scored):
        def badge_classes(path):
            soup = BeautifulSoup(wsgi.get(path).text, 'html.parser')
            return [set(badge['class']) - {'badge'} for badge in soup.select('.item-score .badge')]

        assert badge_classes('/') == [{'badge-success'}, {'badge-success'}]
        assert badge_classes('/?like=0') == [{'badge-secondary'}, {'badge-secondary'}]

    def test_tag_values_in_the_reasons_are_escaped(self, wsgi, make_item):
        odd = ['<script>alert(1)</script>', '"双引号" & \'单引号\'']
        make_item('ABC-001')
        make_item('ABC-002')
        db.save_scores([db.Score('ABC-002', 0.9, odd, []), db.Score('ABC-001', 0.1, [], odd)], top(1))
        for path, selector, prefix in (('/', '.item-because', '因为：'), ('/?like=0', '.item-against', '拉低分数的：')):
            text = wsgi.get(path).text
            assert '<script>alert(1)' not in text and '"双引号" &' not in text
            assert '&lt;script&gt;alert(1)&lt;/script&gt;' in text
            assert plain(BeautifulSoup(text, 'html.parser').select_one(selector)) == prefix + '、'.join(odd)

    def test_buttons_tag_the_movie_as_like_or_dislike(self, wsgi, scored):
        soup = BeautifulSoup(wsgi.get('/?like=0').text, 'html.parser')
        forms = soup.select('form[action^="/tag/"]')
        assert [(form['id'], form['action'], form['method']) for form in forms] == [
            ('form-1', '/tag/ABC-002?page=1&like=0', 'post'), ('form-2', '/tag/ABC-004?page=1&like=0', 'post')]
        for form in forms:
            assert form.select_one('input[name="back"]')['value'] == 'recommend'
            assert form.select_one('input[name="formid"]')['value'] == form['id']
            assert [(button['value'], plain(button)) for button in form.select('button[name="submit"]')] == [
                ('1', '喜欢'), ('0', '不喜欢')]
        assert '/correct/' not in str(soup)
        assert '正确' not in plain(soup) and '错误' not in plain(soup)

    def test_how_the_two_tabs_are_split_is_explained(self, wsgi, scored):
        for path in ('/', '/?like=0'):
            rule = plain(BeautifulSoup(wsgi.get(path).text, 'html.parser').select_one('#recommend-rule'))
            assert '「推荐」是相似度最高的 30%（至少 20 部）' in rule
            assert '其余在「不太像」里' in rule and '从高到低' in rule

    def test_pages_follow_the_score(self, wsgi, make_item):
        with db.db.atomic():
            for n in range(1, 26):
                make_item(f'ABC-{n:03d}')
        # 分数和入库先后无关：7 的倍数最高
        scores = [db.Score(f'ABC-{n:03d}', (n * 7 % 25 + 1) / 30, [], []) for n in range(25, 0, -1)]
        db.save_scores(scores, top(25))
        by_score = [row.fanhao for row in sorted(scores, key=lambda row: -row.score)]
        first = wsgi.get('/').text
        assert covers_on(first) == by_score[:10]
        assert '共  3页,25条' in first
        assert covers_on(wsgi.get('/?page=3&like=1').text) == by_score[20:]

    def test_without_a_model_the_page_says_how_to_get_one(self, wsgi):
        for path in ('/', '/?like=0'):
            assert alert_texts(wsgi.get(path).text, 'warning') == [
                '还没有训练模型。打标满 200 条（其中"喜欢""不喜欢"里少的那一类至少 10 条）后到"模型"页训练，'
                '这里就会按相似度列出推荐']

    def test_a_model_of_an_older_version_is_reported(self, wsgi, data_dir):
        (data_dir / 'model' / 'model.pkl').write_bytes(b'left by an older version')
        assert alert_texts(wsgi.get('/').text, 'warning') == [f'模型现在不能用，下面的内容不会更新：{LEGACY_MODEL}']

    def test_while_the_old_model_is_being_replaced_the_page_says_so_instead_of_asking_to_train(
            self, wsgi, data_dir, make_item, monkeypatch):
        (data_dir / 'model' / 'model.pkl').write_bytes(b'left by an older version')
        add_rated(make_item, 195, like=True)
        add_rated(make_item, 12, like=False)
        monkeypatch.setattr(schedule, '_upgrade_pending', True)
        for path in ('/', '/?like=0'):
            assert alert_texts(wsgi.get(path).text, 'warning') == [
                '现有的模型文件是旧版本程序训练的，新版本换了模型：程序正在用现有的打标自动重新训练，'
                '稍等片刻再刷新这个页面。下面暂时还是原来的内容']
        # 自动训练做完（或者没做成）以后，回到平常的说法
        monkeypatch.setattr(schedule, '_upgrade_pending', False)
        assert alert_texts(wsgi.get('/').text, 'warning') == [f'模型现在不能用，下面的内容不会更新：{LEGACY_MODEL}']

    def test_an_old_model_that_cannot_be_replaced_for_lack_of_tags_is_reported_as_before(self, wsgi, data_dir,
                                                                                        make_item, monkeypatch):
        (data_dir / 'model' / 'model.pkl').write_bytes(b'left by an older version')
        add_rated(make_item, 195, like=True)
        add_rated(make_item, 3, like=False)
        monkeypatch.setattr(schedule, '_upgrade_pending', True)
        assert alert_texts(wsgi.get('/').text, 'warning') == [f'模型现在不能用，下面的内容不会更新：{LEGACY_MODEL}']

    def test_a_broken_model_file_is_reported(self, wsgi, data_dir):
        (data_dir / classifier.MODEL_FILE).write_bytes(b'garbage')
        notice, = alert_texts(wsgi.get('/').text, 'warning')
        assert notice.startswith('模型现在不能用，下面的内容不会更新：模型文件 model.json 无法读取')

    def test_few_tags_of_one_kind_make_the_ranking_for_reference_only(self, wsgi, write_model, make_item):
        write_model(likes=185, dislikes=19, like_recall=0.99, dislike_recall=0.75)
        add_rated(make_item, 185, like=True)
        add_rated(make_item, 19, like=False)
        assert alert_texts(wsgi.get('/').text, 'warning') == [
            '训练模型时"不喜欢"的打标只有 19 条，下面的排序仅供参考；多标一些"不喜欢"的影片，再到"模型"页重新训练会更准']

    def test_tags_made_since_training_are_not_passed_off_as_what_the_model_was_trained_on(self, wsgi, write_model,
                                                                                         make_item):
        write_model(likes=185, dislikes=19, like_recall=0.99, dislike_recall=0.75)
        add_rated(make_item, 185, like=True)
        add_rated(make_item, 25, like=False)
        # 训练之后又标了 6 条不喜欢：提示里的 19 条是训练时的数，并且说明现在的要重新训练才用得上
        assert alert_texts(wsgi.get('/?like=0').text, 'warning') == [
            '训练模型时"不喜欢"的打标只有 19 条，下面的排序仅供参考；现在已经有 25 条，到"模型"页重新训练才会用上']

    def test_the_smaller_class_may_be_the_likes(self, wsgi, write_model, make_item):
        write_model(likes=12, dislikes=300, like_recall=0.6, dislike_recall=0.99)
        add_rated(make_item, 12, like=True)
        assert alert_texts(wsgi.get('/').text, 'warning') == [
            '训练模型时"喜欢"的打标只有 12 条，下面的排序仅供参考；多标一些"喜欢"的影片，再到"模型"页重新训练会更准']

    def test_a_model_that_only_guesses_is_reported(self, wsgi, write_model):
        write_model(likes=185, dislikes=19, like_recall=1.0, dislike_recall=0.0)
        assert alert_texts(wsgi.get('/?like=0').text, 'warning') == [
            '现在的模型把所有影片都判成喜欢，等于没有筛选，下面的排序没有参考价值。原因和办法见"模型"页']

    NOT_SCORED = ('模型已经训练好了，但还没有给影片打分，打完分才有按相似度排好的推荐。'
                  '下一次抓取后会自动打分，也可以到"模型"页点一次"开始训练"')
    SCORING = '模型已经训练好了，程序正在给影片打分，打完分才有按相似度排好的推荐，稍等片刻再刷新这个页面'

    def test_a_model_without_a_single_score_says_the_movies_are_not_scored_yet(self, wsgi, write_model, make_item):
        write_model()
        make_item('NEW-001')
        make_item('NEW-002')
        for path in ('/', '/?like=0'):
            text = wsgi.get(path).text
            assert alert_texts(text, 'warning') == [self.NOT_SCORED]
            assert '暂无数据' in text and covers_on(text) == []

    def test_while_the_first_scoring_is_under_way_the_page_says_it_is_being_done(self, wsgi, write_model, make_item,
                                                                                monkeypatch):
        # 升级后第一次启动：训练完、模型存好了，打分还在等数据库的写锁
        write_model()
        make_item('NEW-001')
        monkeypatch.setattr(schedule, '_upgrade_pending', True)
        for path in ('/', '/?like=0'):
            assert alert_texts(wsgi.get(path).text, 'warning') == [self.SCORING]
        monkeypatch.setattr(schedule, '_upgrade_pending', False)
        assert alert_texts(wsgi.get('/').text, 'warning') == [self.NOT_SCORED]

    def test_the_older_recommendations_are_still_listed_under_the_note(self, wsgi, write_model, make_item):
        write_model()
        make_item('OLD-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'OLD-001')
        text = wsgi.get('/').text
        assert alert_texts(text, 'warning') == [self.NOT_SCORED]
        assert covers_on(text) == ['OLD-001']

    def test_scores_end_the_note(self, wsgi, write_model, scored):
        write_model()
        for path in ('/', '/?like=0'):
            assert alert_texts(wsgi.get(path).text, 'warning') == []

    def test_a_library_with_nothing_left_to_score_gets_no_note(self, wsgi, write_model, make_item):
        write_model()
        assert alert_texts(wsgi.get('/').text, 'warning') == []
        add_rated(make_item, 3, like=True)
        text = wsgi.get('/').text
        assert alert_texts(text, 'warning') == [] and '暂无数据' in text

    def test_a_sound_model_needs_no_notice(self, wsgi, write_model, scored):
        write_model(likes=185, dislikes=40, like_recall=0.99, dislike_recall=0.75)
        for path in ('/', '/?like=0'):
            text = wsgi.get(path).text
            assert alert_texts(text, 'warning') == [] and alert_texts(text, 'danger') == []
            assert alert_texts(text, 'success') == ['今日更新 6 , 今日推荐 3']

    def test_the_pick_page_carries_no_model_notice(self, wsgi):
        assert alert_texts(wsgi.get('/pick').text, 'warning') == []

    @pytest.mark.parametrize('like', ['abc', '', '2', 'None'])
    def test_invalid_like_shows_the_recommended(self, wsgi, make_item, like):
        make_item('ABC-001')
        make_item('ABC-002')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE, 'ABC-002')
        assert covers_on(wsgi.get(f'/?like={like}').text) == ['ABC-001']


class TestTagPage:
    def test_tabs(self, wsgi, make_item):
        for fanhao in ('ABC-001', 'ABC-002', 'ABC-003', 'ABC-004'):
            make_item(fanhao)
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ABC-001')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, 'ABC-002')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-003')

        unrated = wsgi.get('/tagit').text
        assert covers_on(unrated) == ['ABC-004']
        assert 'value="1">喜欢</button>' in unrated
        assert 'value="0">不喜欢</button>' in unrated
        liked = wsgi.get('/tagit?like=1').text
        assert covers_on(liked) == ['ABC-001']
        assert 'value="1">喜欢</button>' not in liked
        disliked = wsgi.get('/tagit?like=0').text
        assert covers_on(disliked) == ['ABC-002']
        assert 'value="0">不喜欢</button>' not in disliked

    def test_pagination_newest_first(self, wsgi, make_item):
        with db.db.atomic():
            for i in range(1, 26):
                make_item(f'ABC-{i:03d}')
        first = wsgi.get('/tagit').text
        assert covers_on(first) == [f'ABC-{i:03d}' for i in range(25, 15, -1)]
        assert '共  3页,25条' in first
        assert covers_on(wsgi.get('/tagit?page=3').text) == [f'ABC-{i:03d}' for i in range(5, 0, -1)]

    @pytest.mark.parametrize('query, page', [
        ('page=2', 2),
        ('page=abc', 1),
        ('page=0', 1),
        ('page=-5', 1),
        ('page=', 1),
        ('page=4', 3),
        ('page=99999999999999999999', 3),
    ])
    def test_page_parameter(self, wsgi, make_item, query, page):
        with db.db.atomic():
            for i in range(1, 26):
                make_item(f'ABC-{i:03d}')
        text = wsgi.get('/tagit?' + query).text
        assert f'第{page}页' in text
        assert covers_on(text)

    def test_at_most_ten_genres_and_stars(self, wsgi, make_item):
        make_item('ABC-001', tags=[('genre', f'类别{i:02d}') for i in range(12)]
                  + [('star', f'演员{i:02d}') for i in range(12)])
        text = wsgi.get('/tagit').text
        assert text.count('badge badge-primary') == 10
        assert text.count('badge badge-warning') == 10

    def test_titles_are_html_escaped(self, wsgi, make_item):
        make_item('ABC-001', title='<script>alert(1)</script>')
        text = wsgi.get('/tagit').text
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in text
        assert '<script>alert(1)' not in text


def tag_query(like, word, page=None):
    '''打标页搜索地址。like 为空时留在未打标页'''
    parts = []
    if page is not None:
        parts.append(f'page={page}')
    if like is not None:
        parts.append(f'like={like}')
    if word is not None:
        parts.append('q=' + quote(word))
    return '/tagit' + ('?' + '&'.join(parts) if parts else '')


class TestTagSearch:
    def test_search_box_names_code_title_actor_and_tag(self, wsgi):
        for path in ('/tagit', '/tagit?like=1', '/tagit?like=0'):
            text = wsgi.get(path).text
            assert 'placeholder="可以搜索番号、标题、演员、标签"' in text
            assert '>搜索</button>' in text
            assert '只能搜番号' not in text

    def test_any_name_of_an_actress_finds_her_movies(self, wsgi, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', 'Actor A')
        for code in ('A-001', 'A-002'):
            cast.movie(code)
            cast.link(code, jia)
        cast.movie('A-003', ('演员乙', '/star/b1'))
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'A-002')
        assert covers_on(wsgi.get(tag_query(None, 'えんいん')).text) == ['A-001']
        assert covers_on(wsgi.get(tag_query(1, 'actor')).text) == ['A-002']
        # 卡片上只有她的正式名字
        assert '演员甲' in wsgi.get(tag_query(None, 'えんいん')).text

    def test_fanhao_case_dash_and_dated_suffix_stay_on_the_tab(self, wsgi, make_item):
        for fanhao in ('AEGE-032', 'AEGE-032_2024-06-20', 'AEGE032', 'AEGE-999', 'OTHER-032'):
            make_item(fanhao, title='plain')
        for fanhao in ('AEGE-032', 'AEGE-032_2024-06-20', 'AEGE032', 'AEGE-999'):
            ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, fanhao)
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, 'OTHER-032')
        expected = {'AEGE-032', 'AEGE-032_2024-06-20', 'AEGE032'}
        for word in ('aege032', 'AEGE-032', 'AeGe-032'):
            text = wsgi.get(tag_query(1, word)).text
            assert set(covers_on(text)) == expected
            assert 'q=aege032' in text.lower() or 'q=AEGE-032' in text or 'q=AeGe-032' in text
        missed = wsgi.get(tag_query(0, 'aege032')).text
        assert covers_on(missed) == []
        assert '没有匹配的结果' in missed

    def test_digits_alone_do_not_match_other_codes(self, wsgi, make_item):
        make_item('AEGE-032', title='plain')
        make_item('NUM-032', title='plain')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'AEGE-032')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'NUM-032')
        text = wsgi.get(tag_query(1, '032')).text
        assert covers_on(text) == []
        assert '没有匹配的结果' in text
        assert set(covers_on(wsgi.get(tag_query(1, 'AEGE')).text)) == {'AEGE-032'}

    def test_tag_title_and_marker_stay_inside_like_or_dislike(self, wsgi, make_item):
        make_item('AEGE-032', title='plain', tags=[
            ('genre', '学生'), ('genre', '教室'), ('star', '演员甲'),
            ('導演:', '导演甲'), ('製作商:', '商甲'), ('發行商:', '商乙'), ('系列:', '系列甲'),
        ])
        make_item('FOUR-001', title='4K')
        make_item('TAG4-001', title='plain', tags=[('genre', '4K')])
        make_item('OTHER-111', title='4K', tags=[('genre', '学生')])
        make_item('FREE-001', title='plain', tags=[('genre', '学生')])
        for fanhao in ('AEGE-032', 'FOUR-001', 'TAG4-001'):
            ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, fanhao)
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, 'OTHER-111')

        for word in ('学生', '教室', '演员甲', '导演甲', '商甲', '商乙', '系列甲'):
            assert covers_on(wsgi.get(tag_query(1, word)).text) == ['AEGE-032']
        assert covers_on(wsgi.get(tag_query(0, '学生')).text) == ['OTHER-111']
        assert covers_on(wsgi.get(tag_query(1, '4k')).text) == ['TAG4-001', 'FOUR-001']
        assert covers_on(wsgi.get(tag_query(0, '4K')).text) == ['OTHER-111']
        assert 'AEGE-032' not in covers_on(wsgi.get(tag_query(0, '商甲')).text)
        assert covers_on(wsgi.get(tag_query(None, '学生')).text) == ['FREE-001']

    def test_simplified_and_traditional_spellings_find_each_other(self, wsgi, make_item):
        make_item('TRA-001', title='plain', tags=[('genre', '女大學生'), ('製作商:', '藝術館')])
        make_item('TRA-002', title='學園的故事', tags=[('genre', '標籤一')])
        make_item('SIM-001', title='plain', tags=[('genre', '女大学生')])
        make_item('JPN-001', title='元気な学園', tags=[('genre', '標籤二')])
        make_item('OTH-001', title='plain', tags=[('genre', '标签九')])
        for word in ('学生', '學生', '女大学生', '女大學生', ' 大學 '):
            assert covers_on(wsgi.get(tag_query(None, word)).text) == ['SIM-001', 'TRA-001'], word
        for word in ('艺术', '藝術館', '艺術'):
            assert covers_on(wsgi.get(tag_query(None, word)).text) == ['TRA-001'], word
        # 标题也是简体繁体互通
        for word in ('学园', '學園', '学園'):
            assert covers_on(wsgi.get(tag_query(None, word)).text) == ['JPN-001', 'TRA-002'], word
        assert covers_on(wsgi.get(tag_query(None, '标签')).text) == ['OTH-001', 'JPN-001', 'TRA-002']

    def test_japanese_spellings_are_not_converted(self, wsgi, make_item):
        make_item('JPN-001', title='元気な人', tags=[('genre', '気持ち')])
        make_item('TRA-001', title='元氣的人', tags=[('genre', '氣質')])
        # 气 是 氣 的简体；気 是日文的写法，不算同一个字
        assert covers_on(wsgi.get(tag_query(None, '元气')).text) == ['TRA-001']
        assert covers_on(wsgi.get(tag_query(None, '气')).text) == ['TRA-001']
        assert covers_on(wsgi.get(tag_query(None, '気')).text) == ['JPN-001']

    def test_a_search_in_either_spelling_keeps_tabs_pages_and_the_word(self, wsgi, make_item):
        with db.db.atomic():
            for n in range(1, 13):
                make_item(f'STU-{n:03d}', title='plain', tags=[('genre', '學生')])
                ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE if n % 4 else RATE_VALUE.DISLIKE, f'STU-{n:03d}')
        first = wsgi.get(tag_query(1, '学生')).text
        assert '共  1页,9条' in first and 'value="学生"' in first
        assert covers_on(first) == [f'STU-{n:03d}' for n in range(11, 0, -1) if n % 4]
        assert covers_on(wsgi.get(tag_query(0, '学生')).text) == ['STU-012', 'STU-008', 'STU-004']
        assert covers_on(wsgi.get(tag_query(None, '学生')).text) == []

    def test_no_match_is_stated(self, wsgi, make_item):
        make_item('AEGE-032', title='plain')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'AEGE-032')
        text = wsgi.get(tag_query(1, '没有这个词')).text
        assert covers_on(text) == []
        assert '没有匹配的结果' in text
        assert '暂无数据' not in text
        kept = wsgi.get('/tagit?like=1').text
        assert covers_on(kept) == ['AEGE-032']
        assert '没有匹配的结果' not in kept

    def test_paging_keeps_the_word_and_the_tab(self, wsgi, make_item):
        for n in range(1, 12):
            fanhao = f'STU-{n:03d}'
            make_item(fanhao, title='plain', tags=[('genre', '学生')])
            ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, fanhao)
        make_item('STU-999', title='plain', tags=[('genre', '学生')])
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, 'STU-999')
        encoded = quote('学生')
        first = wsgi.get(tag_query(1, '学生')).text
        assert '共  2页,11条' in first
        assert f'q={encoded}' in first
        assert 'STU-999' not in covers_on(first)
        second = wsgi.get(tag_query(1, '学生', page=2)).text
        assert covers_on(second) == ['STU-001']
        assert f'q={encoded}' in second
        assert 'value="学生"' in second
        assert '没有匹配的结果' not in second

    def test_unrated_list_without_a_word_stays_the_same(self, wsgi, make_item):
        make_item('FREE-001', title='plain', tags=[('genre', '学生')])
        make_item('AEGE-032', title='plain', tags=[('genre', '学生')])
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'AEGE-032')
        text = wsgi.get('/tagit').text
        assert covers_on(text) == ['FREE-001']
        assert '没有匹配的结果' not in text
        assert 'placeholder="可以搜索番号、标题、演员、标签"' in text
        assert covers_on(wsgi.get('/tagit?q=%20%20').text) == ['FREE-001']


class TestTagForm:
    # "未打标的"不是哪个标签页：地址里没有 like。后两种写法是以前的版本打开的页面上的表单地址
    @pytest.mark.parametrize('like', ['', '&like=', '&like=None'], ids=['none', 'empty', 'the-word-none'])
    def test_tag_unrated_movie(self, wsgi, make_item, like):
        make_item('ABC-001')
        response = wsgi.post(f'/tag/ABC-001?page=2{like}', {'submit': '1', 'formid': 'form-3'})
        path, query, fragment = redirect_target(response)
        assert (path, query, fragment) == ('/tagit', {'page': '2'}, 'form-3')
        rate = ItemRate.get_by_fanhao('ABC-001')
        assert (rate.rate_type, rate.rate_value) == (RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)

    def test_the_untagged_list_has_no_like_in_its_addresses(self, wsgi, make_item):
        make_item('ABC-001', title='示例影片')
        for address, action in [('/tagit', '/tag/ABC-001?page=1'),
                                ('/tagit?q=%E7%A4%BA%E4%BE%8B', '/tag/ABC-001?page=1&amp;q=%E7%A4%BA%E4%BE%8B')]:
            text = wsgi.get(address).text
            assert f'<form action="{action}" method="post">' in text
            assert 'like=None' not in text and 'like=&' not in text and 'like="' not in text
        response = wsgi.post('/tag/ABC-001?page=1&q=%E7%A4%BA%E4%BE%8B', {'submit': '0'})
        assert redirect_target(response)[1] == {'page': '1', 'q': '示例'}

    def test_user_tag_replaces_system_rate(self, wsgi, make_item):
        make_item('ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-001')
        wsgi.post('/tag/ABC-001', {'submit': '0'})
        rate = ItemRate.get_by_fanhao('ABC-001')
        assert (rate.rate_type, rate.rate_value) == (RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        assert ItemRate.select().count() == 1

    def test_user_changes_own_tag(self, wsgi, make_item):
        make_item('ABC-001')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ABC-001')
        text = wsgi.get('/tagit?like=1').text
        assert 'value="0">不喜欢</button>' in text
        assert '<form action="/tag/ABC-001?page=1&amp;like=1" method="post">' in text
        response = wsgi.post('/tag/ABC-001?page=1&like=1', {'submit': '0'})
        assert redirect_target(response)[:2] == ('/tagit', {'page': '1', 'like': '1'})
        rate = ItemRate.get_by_fanhao('ABC-001')
        assert (rate.rate_type, rate.rate_value) == (RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        assert ItemRate.select().count() == 1

    @pytest.mark.parametrize('data', [{}, {'submit': ''}, {'submit': '2'}, {'submit': 'like'}])
    def test_rejects_other_values(self, wsgi, make_item, data):
        make_item('ABC-001')
        response = wsgi.post('/tag/ABC-001', data)
        assert response.status == 400
        assert '提交的值只能是 0 或 1' in response.text
        assert ItemRate.get_by_fanhao('ABC-001') is None

    def test_unknown_fanhao(self, wsgi):
        response = wsgi.post('/tag/ABC-001', {'submit': '1'})
        assert response.status == 404
        assert '没有这个番号' in response.text
        assert ItemRate.select().count() == 0

    def test_tagging_the_only_movie_on_the_last_page_goes_back_to_the_new_last_page(self, wsgi, make_item):
        with db.db.atomic():
            for i in range(1, 12):
                make_item(f'ABC-{i:03d}')
        assert covers_on(wsgi.get('/tagit?page=2').text) == ['ABC-001']
        path, query, _ = redirect_target(wsgi.post('/tag/ABC-001?page=2', {'submit': '1'}))
        assert (path, query) == ('/tagit', {'page': '2'})
        text = wsgi.get('/tagit?page=2').text
        assert covers_on(text) == [f'ABC-{i:03d}' for i in range(11, 1, -1)]
        assert '第1页' in text
        assert 'action="/tag/ABC-011?page=1"' in text

    @pytest.mark.parametrize('formid, fragment', [
        ('form-1', 'form-1'),
        ('form-1000', 'form-1000'),
        ('form-10000', ''),
        ('form-1"><script>', ''),
        ('', ''),
    ])
    def test_redirect_anchor_only_accepts_form_numbers(self, wsgi, make_item, formid, fragment):
        make_item('ABC-001')
        response = wsgi.post('/tag/ABC-001', {'submit': '1', 'formid': formid})
        assert redirect_target(response)[2] == fragment


class TestTagFromTheRecommendPage:
    '''推荐页上的"喜欢 / 不喜欢"就是打标：系统评分改成用户打标，然后回到原来的标签页、页码和位置'''

    @pytest.mark.parametrize('system, submit, expected', [
        (RATE_VALUE.LIKE, '1', RATE_VALUE.LIKE),
        (RATE_VALUE.LIKE, '0', RATE_VALUE.DISLIKE),
        (RATE_VALUE.DISLIKE, '1', RATE_VALUE.LIKE),
        (RATE_VALUE.DISLIKE, '0', RATE_VALUE.DISLIKE),
    ])
    def test_the_button_says_what_the_user_thinks(self, wsgi, make_item, system, submit, expected):
        make_item('ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, system, 'ABC-001')
        response = wsgi.post('/tag/ABC-001?page=2&like=0', {'submit': submit, 'formid': 'form-4', 'back': 'recommend'})
        assert redirect_target(response) == ('/', {'page': '2', 'like': '0'}, 'form-4')
        rate = ItemRate.get_by_fanhao('ABC-001')
        assert (rate.rate_type, rate.rate_value) == (RATE_TYPE.USER_RATE, expected)
        assert ItemRate.select().count() == 1

    def test_tagged_movie_leaves_the_tab_and_shows_on_the_tag_page(self, wsgi, make_item, fill_requests):
        for fanhao in ('ABC-001', 'ABC-002', 'ABC-003'):
            make_item(fanhao)
        db.save_scores([db.Score('ABC-003', 0.9, [], []), db.Score('ABC-002', 0.8, [], []),
                        db.Score('ABC-001', 0.7, [], [])], top(3))
        assert covers_on(wsgi.get('/').text) == ['ABC-003', 'ABC-002', 'ABC-001']
        wsgi.post('/tag/ABC-002?page=1&like=1', {'submit': '0', 'formid': 'form-2', 'back': 'recommend'})
        wsgi.post('/tag/ABC-003?page=1&like=1', {'submit': '1', 'formid': 'form-1', 'back': 'recommend'})
        assert covers_on(wsgi.get('/?page=1&like=1').text) == ['ABC-001']
        assert covers_on(wsgi.get('/tagit?like=0').text) == ['ABC-002']
        assert covers_on(wsgi.get('/tagit?like=1').text) == ['ABC-003']
        # 打成喜欢、详情里又没有演员的，登记一次补演员名；不喜欢的不登记
        assert fill_requests == ['fill']

    @pytest.mark.parametrize('like, landed', [('1', '1'), ('0', '0'), ('abc', '1'), ('', '1')])
    def test_goes_back_to_the_tab_it_came_from(self, wsgi, make_item, like, landed):
        make_item('ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-001')
        response = wsgi.post(f'/tag/ABC-001?page=3&like={like}', {'submit': '1', 'formid': 'form-1', 'back': 'recommend'})
        assert redirect_target(response) == ('/', {'page': '3', 'like': landed}, 'form-1')

    def test_tagging_the_only_movie_on_the_last_page_goes_back_to_the_new_last_page(self, wsgi, make_item):
        with db.db.atomic():
            for i in range(1, 12):
                make_item(f'ABC-{i:03d}')
                ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, f'ABC-{i:03d}')
        response = wsgi.post('/tag/ABC-001?page=2&like=1', {'submit': '1', 'back': 'recommend'})
        path, query, _ = redirect_target(response)
        assert (path, query) == ('/', {'page': '2', 'like': '1'})
        text = wsgi.get('/?page=2&like=1').text
        assert covers_on(text) == [f'ABC-{i:03d}' for i in range(11, 1, -1)]
        assert '第1页' in text
        assert 'action="/tag/ABC-011?page=1&like=1"' in text

    def test_a_movie_without_any_rate_can_be_tagged_too(self, wsgi, make_item):
        make_item('ABC-001')
        response = wsgi.post('/tag/ABC-001?page=1&like=1', {'submit': '0', 'back': 'recommend'})
        assert response.status == 303
        rate = ItemRate.get_by_fanhao('ABC-001')
        assert (rate.rate_type, rate.rate_value) == (RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)

    def test_the_address_of_the_old_right_and_wrong_buttons_is_gone(self, wsgi, make_item):
        make_item('ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-001')
        assert wsgi.post('/correct/ABC-001?page=1&like=1', {'submit': '0', 'formid': 'form-1'}).status == 404
        rate = ItemRate.get_by_fanhao('ABC-001')
        assert (rate.rate_type, rate.rate_value) == (RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)


CROSS_SITE = {'Sec-Fetch-Site': 'cross-site', 'Origin': 'https://evil.example'}


class TestCrossSiteRequests:
    '''别的网页里自动提交的表单（跨站 POST）返回 403、数据不变；bustag 自己的页面和非浏览器的请求照常'''

    @pytest.mark.parametrize('headers, reason', [
        (CROSS_SITE, index.CROSS_SITE_REASON),
        ({'Sec-Fetch-Site': 'same-site', 'Origin': 'http://127.0.0.1:8080'}, index.CROSS_SITE_REASON),
        ({'Origin': 'https://evil.example'}, index.ORIGIN_MISMATCH_REASON),
        ({'Origin': 'http://127.0.0.1:8080'}, index.ORIGIN_MISMATCH_REASON),
        ({'Origin': 'null'}, index.ORIGIN_MISMATCH_REASON),
        ({'Origin': 'http://[::1'}, index.ORIGIN_MISMATCH_REASON),
    ], ids=['cross-site', 'same-site', 'other-origin', 'other-port', 'null-origin', 'bad-origin'])
    def test_tag_from_another_site_is_rejected(self, wsgi, make_item, caplog, headers, reason):
        make_item('ABC-001')
        response = wsgi.post('/tag/ABC-001', {'submit': '1'}, headers=headers)
        assert response.status == 403
        assert '<title>禁止访问</title>' in response.text
        assert f'<p>{reason}</p>' in response.text
        assert ItemRate.get_by_fanhao('ABC-001') is None
        assert any(m.startswith('拒绝跨站请求 POST /tag/ABC-001（') for m in caplog.messages)

    @pytest.mark.parametrize('headers', [
        {'Sec-Fetch-Site': 'same-origin', 'Origin': 'http://127.0.0.1'},
        {'Sec-Fetch-Site': 'none'},
        {'Origin': 'http://127.0.0.1'},
        {'Origin': 'HTTP://127.0.0.1'},
        {'Host': '192.168.1.5:8000', 'Origin': 'http://192.168.1.5:8000'},
        {},
    ], ids=['same-origin', 'user-initiated', 'old-browser-same-origin', 'origin-case', 'host-with-port', 'no-headers'])
    def test_tag_from_bustag_itself_or_a_script_works(self, wsgi, make_item, headers):
        make_item('ABC-001')
        response = wsgi.post('/tag/ABC-001', {'submit': '1'}, headers=headers)
        assert response.status == 303
        rate = ItemRate.get_by_fanhao('ABC-001')
        assert (rate.rate_type, rate.rate_value) == (RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)

    def test_every_form_is_protected(self, wsgi, make_item, monkeypatch):
        make_item('ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-001')
        trainings, jobs = [], []
        monkeypatch.setattr(classifier, 'train_and_recommend', lambda: trainings.append('train'))
        monkeypatch.setattr(index, 'request_queue_run', lambda: jobs.append('run'))
        db.FetchTask.create(path='/OLD-001', fanhao='OLD-001', origin=db.ORIGIN_UPLOAD, status=db.TASK_FAILED)
        for path, data in [('/tag/ABC-001?page=1&like=1', {'submit': '0', 'formid': 'form-1', 'back': 'recommend'}),
                           ('/do-training', {}),
                           ('/local_fanhao', {'submit': '1', 'fanhao': 'ABC-001,http://nas/a.mp4\nNEW-001', 'tag_like': '1'}),
                           ('/local_fanhao/retry', {'all': '1'}),
                           ('/local_fanhao/clear', {})]:
            assert wsgi.post(path, data, headers=CROSS_SITE).status == 403, path
        upload = wsgi.post_multipart('/load_db', [('dbfile', ('bus.db', b'SQLite format 3\x00')), ('submit', '1')],
                                     headers=CROSS_SITE)
        assert upload.status == 403
        rate = ItemRate.get_by_fanhao('ABC-001')
        assert (rate.rate_type, rate.rate_value) == (RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        assert (trainings, jobs) == ([], [])
        assert LocalItem.select().count() == 0
        assert [(task.fanhao, task.status) for task in db.FetchTask.select()] == [('OLD-001', db.TASK_FAILED)]

    @pytest.mark.parametrize('path', ['/', '/tagit', '/model', '/local_fanhao', '/load_db', '/static/css/bustag.css'])
    def test_pages_opened_from_another_site_still_work(self, wsgi, path):
        assert wsgi.get(path, headers=CROSS_SITE).status == 200


class TestCover:
    def test_cached_cover_needs_no_network(self, wsgi, data_dir, make_item, cover_fetcher):
        make_item('ABC-001')
        (data_dir / 'covers').mkdir()
        (data_dir / 'covers' / 'ABC-001.jpg').write_bytes(JPEG)
        for path in ('/cover/ABC-001', '/cover/abc-001'):
            response = wsgi.get(path)
            assert response.status == 200
            assert response.headers['content-type'] == 'image/jpeg'
            assert response.headers['cache-control'] == index.COVER_CACHE_CONTROL
            assert response.body == JPEG
        assert cover_fetcher.created == []

    def test_unknown_fanhao(self, wsgi, cover_fetcher):
        response = wsgi.get('/cover/ABC-001')
        assert response.status == 404
        assert '没有这个番号' in response.text
        assert cover_fetcher.requests == []

    def test_download_and_cache(self, wsgi, data_dir, make_item, cover_fetcher):
        make_item('ABC-001', cover='/pics/cover/abc_b.jpg')
        cover_fetcher.routes['https://www.javbus.com/pics/cover/abc_b.jpg'] = (JPEG, 'image/jpeg')
        response = wsgi.get('/cover/ABC-001')
        assert (response.status, response.body) == (200, JPEG)
        assert response.headers['cache-control'] == index.COVER_CACHE_CONTROL
        assert (data_dir / 'covers' / 'ABC-001.jpg').read_bytes() == JPEG

    def test_failed_download_shows_placeholder_and_retries_later(self, wsgi, make_item, cover_fetcher, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(covers, 'time', types.SimpleNamespace(monotonic=lambda: now[0]))
        make_item('ABC-001', cover='/pics/cover/abc_b.jpg')
        source = 'https://www.javbus.com/pics/cover/abc_b.jpg'
        cover_fetcher.routes[source] = FetchError('网络错误')
        placeholder = wsgi.get('/cover/ABC-001')
        assert placeholder.status == 200
        assert placeholder.headers['content-type'] == 'image/svg+xml'
        assert placeholder.headers['cache-control'] == 'no-store'
        assert b'<svg' in placeholder.body

        # 失败后 10 分钟内不再下载，直接回占位图
        cover_fetcher.routes[source] = (JPEG, 'image/jpeg')
        assert wsgi.get('/cover/ABC-001').headers['content-type'] == 'image/svg+xml'
        assert cover_fetcher.requests == [source]
        now[0] += covers.FAILURE_TTL
        assert wsgi.get('/cover/ABC-001').body == JPEG
        assert cover_fetcher.requests == [source, source]

    def test_dated_fanhao(self, wsgi, data_dir, make_item, cover_fetcher):
        make_item('GEBB-062_2026-10-31', cover='/pics/cover/gebb_b.jpg')
        cover_fetcher.routes['https://www.javbus.com/pics/cover/gebb_b.jpg'] = (JPEG, 'image/jpeg')
        assert 'src="/cover/GEBB-062_2026-10-31"' in wsgi.get('/tagit').text
        response = wsgi.get('/cover/gebb-062_2026-10-31')
        assert (response.status, response.body) == (200, JPEG)
        assert (data_dir / 'covers' / 'GEBB-062_2026-10-31.jpg').read_bytes() == JPEG

    def test_the_download_is_told_how_to_ask_whether_the_browser_is_still_there(self, wsgi, make_item, monkeypatch):
        make_item('ABC-001')
        seen = []

        def get_cover_file(fanhao, abandoned=None):
            seen.append((fanhao, abandoned))

        def gone():
            return True

        monkeypatch.setattr(index, 'get_cover_file', get_cover_file)
        # waitress 把"客户端断开了没有"放在 WSGI 环境里；别的服务器（gunicorn）没有这一项
        assert wsgi.get('/cover/ABC-001', environ={'waitress.client_disconnected': gone}).status == 200
        assert wsgi.get('/cover/ABC-001').status == 200
        assert seen == [('ABC-001', gone), ('ABC-001', None)]


BLANK_IMAGE = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=='


class TestCoverImages:
    '''
    封面由页面脚本排队加载（一次最多同时取 2 张）：卡片上的 <img> 只带着封面的地址，
    紧跟着的 <noscript> 里是关掉脚本时用的那一张
    '''

    @pytest.fixture
    def library(self, make_item):
        make_item('ABC-001', tags=[('genre', '标签一')])
        make_item('ABC-002', tags=[('genre', '标签一')])
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-001')
        LocalItem.saveit('ABC-001', 'http://media.local/ABC-001.mp4')
        index.pick.add_names('tag', ['标签一'])

    @pytest.mark.parametrize('path, fanhaos', [
        ('/', ['ABC-001']),
        ('/tagit', ['ABC-002']),
        ('/local', ['ABC-001']),
        ('/pick', ['ABC-001', 'ABC-002']),
    ])
    def test_every_card_waits_for_the_script_and_has_a_fallback(self, wsgi, library, path, fanhaos):
        soup = BeautifulSoup(wsgi.get(path).text, 'html.parser')
        images = soup.select('img.coverimg')
        assert [image['data-cover'] for image in images] == [f'/cover/{fanhao}' for fanhao in fanhaos]
        for image, fanhao in zip(images, fanhaos):
            # 页面一打开浏览器不会自己去取封面：src 是一张 1×1 的透明图
            assert image['src'] == BLANK_IMAGE
            assert 'cover-waiting' in image['class'] and 'img-thumbnail' in image['class']
            assert image['alt'] == f'{fanhao} 封面'
            fallback = image.find_next_sibling()
            assert fallback.name == 'noscript'
            plain_image = fallback.find('img')
            assert plain_image['src'] == f'/cover/{fanhao}'
            assert 'coverimg' not in plain_image['class'] and 'img-thumbnail' in plain_image['class']
            assert plain_image['alt'] == f'{fanhao} 封面'

    def test_without_scripts_only_the_fallback_is_shown(self, wsgi):
        head = BeautifulSoup(wsgi.get('/about').text, 'html.parser').head
        assert 'img[data-cover] { display: none; }' in head.find('noscript').find('style').get_text()

    def test_the_script_and_the_style_sheet_know_the_waiting_covers(self, wsgi):
        script = wsgi.get('/static/js/bustag.js').text
        assert "getAttribute('data-cover')" in script and 'cover-waiting' in script
        assert '.coverimg.cover-waiting' in wsgi.get('/static/css/bustag.css').text


class TestLocalFiles:
    def test_local_page_lists_files_with_a_path(self, wsgi, make_item):
        make_item('ABC-001')
        make_item('ABC-002')
        local = LocalItem.saveit('ABC-001', 'http://media.local/ABC-001.mp4')
        LocalItem.saveit('ABC-002', None)
        text = wsgi.get('/local').text
        assert covers_on(text) == ['ABC-001']
        assert f'href="/local_play/{local.id}"' in text
        assert '影片信息没有下载到' not in text

    def test_total_matches_listed_files(self, wsgi, make_item):
        make_item('ABC-001')
        LocalItem.saveit('ABC-001', 'http://media.local/ABC-001.mp4')
        # 上传了本地地址、影片信息还没抓下来的番号（上传之后、抓取完成之前就是这样）
        LocalItem.saveit('ABC-002', 'http://media.local/ABC-002.mp4')
        text = wsgi.get('/local').text
        listed = re.findall(r'href="/local_play/\d+"', text)
        total = re.search(r'共\s+\d+页,(\d+)条', text)
        assert listed and total
        assert len(listed) == int(total.group(1))

    def test_files_without_movie_info_are_explained(self, wsgi, make_item):
        make_item('HAVE-001')
        add_local_fanhao('HAVE-001,http://nas/HAVE-001.mp4\n300MIUM-1506,http://nas/a.mp4\n'
                         'NOPE-001,http://nas/b.mp4', tag_like=False)
        text = wsgi.get('/local').text
        assert covers_on(text) == ['HAVE-001']
        assert '共  1页,1条' in text
        assert '还有 2 个本地文件的影片信息没有下载到（正在下载，或者番号不存在、下载失败），暂不显示' in text

    def test_page_after_the_last_shows_the_last_page(self, wsgi, make_item):
        make_item('ABC-001')
        LocalItem.saveit('ABC-001', 'http://media.local/ABC-001.mp4')
        text = wsgi.get('/local?page=5').text
        assert covers_on(text) == ['ABC-001']
        assert '第1页' in text

    def test_local_page_view_count_label(self, wsgi, make_item):
        make_item('ABC-001')
        LocalItem.saveit('ABC-001', 'http://media.local/ABC-001.mp4')
        text = wsgi.get('/local').text
        assert covers_on(text) == ['ABC-001']
        assert '观看次数: 0' in text
        assert '观看次数观看' not in text

    def test_play_redirects_and_counts(self, wsgi, make_item, monkeypatch):
        monkeypatch.setattr(db, 'get_now_time', lambda: NOW)
        make_item('ABC-001')
        local = LocalItem.saveit('ABC-001', 'http://media.local/library/ABC-001.mp4')
        response = wsgi.get(f'/local_play/{local.id}')
        assert response.status == 303
        assert response.headers['location'] == 'http://media.local/library/ABC-001.mp4'
        played = LocalItem.get_by_id(local.id)
        assert (played.view_times, played.last_view_date) == (1, NOW)
        assert '2026-11-06 12:00:00' in wsgi.get('/local').text

    def test_play_percent_encodes_location(self, wsgi, make_item):
        make_item('ABC-001')
        local = LocalItem.saveit('ABC-001', 'https://media.local/影片 1.mp4')
        response = wsgi.get(f'/local_play/{local.id}')
        assert response.headers['location'] == 'https://media.local/%E5%BD%B1%E7%89%87%201.mp4'

    @pytest.mark.parametrize('path', [
        'file:///C:/movies/ABC-001.mp4',
        'C:\\movies\\ABC-001.mp4',
        '/movies/ABC-001.mp4',
        'javascript:alert(1)',
        'http://',
        'http://media.local/ABC-001\r\nSet-Cookie: x=1',
    ])
    def test_play_rejects_non_http_paths(self, wsgi, make_item, path):
        make_item('ABC-001')
        local = LocalItem.saveit('ABC-001', path)
        response = wsgi.get(f'/local_play/{local.id}')
        assert response.status == 400
        assert '只能播放 http / https 地址的文件' in response.text
        assert LocalItem.get_by_id(local.id).view_times == 0

    def test_play_unknown_id(self, wsgi):
        response = wsgi.get('/local_play/1')
        assert response.status == 404
        assert '没有这个本地文件' in response.text


def add_rated(make_item, count, like):
    prefix, marker = ('LIK', [('genre', 'g0'), ('star', 's0')]) if like else ('DIS', [])
    value = RATE_VALUE.LIKE if like else RATE_VALUE.DISLIKE
    with db.db.atomic():
        for i in range(count):
            fanhao = f'{prefix}-{i:03d}'
            make_item(fanhao, tags=marker + [('genre', f'g{1 + i % 9}'), ('star', f's{1 + i % 13}')])
            ItemRate.saveit(RATE_TYPE.USER_RATE, value, fanhao)


# 模型文件的几种坏法：读不出来、是 JSON 但内容不对
BROKEN_MODELS = [b'garbage', b'', json.dumps({'version': 1}).encode(), json.dumps([1, 2, 3]).encode()]


def report_lines(text):
    '''模型页"当前模型数据"里的各句话'''
    return [plain(line) for line in BeautifulSoup(text, 'html.parser').select('#model-report p')]


def alert_texts(text, kind):
    '''页面上某一种提示框（success / warning / danger）里的文字'''
    return [plain(box) for box in BeautifulSoup(text, 'html.parser').select(f'div.alert-{kind}')]


def alike_line(text):
    '''模型页上"没打标的影片几乎都在一边"那一句，没有时是 None'''
    line = BeautifulSoup(text, 'html.parser').select_one('#model-alike')
    return None if line is None else plain(line)


def score_movies(make_item, count, above_half):
    '''给 count 部影片（还没入库的先入库）打分，其中 above_half 部的分数过半'''
    with db.db.atomic():
        for n in range(count):
            if db.Item.get_by_fanhao(f'SCO-{n:03d}') is None:
                make_item(f'SCO-{n:03d}')
    db.save_scores([db.Score(f'SCO-{n:03d}', 0.8 if n < above_half else 0.2, [], []) for n in range(count)],
                   classifier.recommended_count)


class TestModelPage:
    def test_no_model_yet(self, wsgi):
        text = wsgi.get('/model').text
        assert '还没有训练过模型.' in text
        assert report_lines(text) == []
        assert '认对' not in text

    def test_the_card_keeps_its_bottom_border(self, wsgi):
        # Bootstrap 给 .accordion 里的第一张卡片去掉下边框，等着和下一张连在一起；这一页只有一张，所以不用这个类。
        # 点标题收起 / 展开照旧
        soup = BeautifulSoup(wsgi.get('/model').text, 'html.parser')
        assert soup.select('.accordion') == []
        card, = soup.select('div.card')
        assert card.select_one('#collapseOne.collapse.show') is not None
        assert card.select_one('button[data-toggle="collapse"]')['data-target'] == '#collapseOne'

    def test_says_in_plain_words_how_good_the_model_is(self, wsgi, write_model):
        write_model(likes=185, dislikes=19, like_recall=0.986, dislike_recall=0.753, auc=0.967)
        text = wsgi.get('/model').text
        assert report_lines(text) == [
            '用了 204 条打标（喜欢 185、不喜欢 19）训练，训练时间 2026-11-06 12:00:00。',
            '交叉验证（把打标分成 5 份，轮流拿 1 份出来检验，重复 10 次）：喜欢认对 99%，不喜欢认对 75%（全部猜喜欢时是 0%）。',
            '按相似度排序时，喜欢的影片排在不喜欢的影片前面的概率是 97%（乱排是 50%）。',
            '不喜欢只有 19 条，"不喜欢认对 75%"这个数字上下可能差 22 个百分点；不喜欢攒到 50 条以上后重新训练会更准（还差 31 条）。']
        assert alert_texts(text, 'warning') == ['"不喜欢"的打标不到 30 条，这个模型的结果仅供参考。']
        assert alert_texts(text, 'danger') == []
        assert '还没有训练过模型' not in text

    @pytest.mark.parametrize('dislikes, last_line, tentative', [
        (10, '不喜欢只有 10 条，"不喜欢认对 80%"这个数字上下可能差 31 个百分点；不喜欢攒到 50 条以上后重新训练会更准（还差 40 条）。', True),
        (30, '不喜欢只有 30 条，"不喜欢认对 80%"这个数字上下可能差 18 个百分点；不喜欢攒到 50 条以上后重新训练会更准（还差 20 条）。', False),
        (50, '不喜欢有 50 条，"不喜欢认对 80%"这个数字上下可能差 14 个百分点。', False),
        (100, '不喜欢有 100 条，"不喜欢认对 80%"这个数字上下可能差 10 个百分点。', False),
    ], ids=['10', '30', '50', '100'])
    def test_the_margin_comes_from_how_many_tags_the_smaller_class_has(self, wsgi, write_model, dislikes, last_line,
                                                                      tentative):
        write_model(likes=300, dislikes=dislikes)
        text = wsgi.get('/model').text
        assert report_lines(text)[-1] == last_line
        assert bool(alert_texts(text, 'warning')) is tentative

    def test_likes_can_be_the_smaller_class(self, wsgi, write_model):
        write_model(likes=25, dislikes=400, like_recall=0.6, dislike_recall=0.97, auc=0.91)
        text = wsgi.get('/model').text
        assert report_lines(text)[1:] == [
            '交叉验证（把打标分成 5 份，轮流拿 1 份出来检验，重复 10 次）：不喜欢认对 97%，喜欢认对 60%（全部猜不喜欢时是 0%）。',
            '按相似度排序时，喜欢的影片排在不喜欢的影片前面的概率是 91%（乱排是 50%）。',
            '喜欢只有 25 条，"喜欢认对 60%"这个数字上下可能差 20 个百分点；喜欢攒到 50 条以上后重新训练会更准（还差 25 条）。']
        assert alert_texts(text, 'warning') == ['"喜欢"的打标不到 30 条，这个模型的结果仅供参考。']

    @pytest.mark.parametrize('dislike_recall, guessing', [(0.0, True), (0.09, True), (0.1, False), (0.75, False)])
    def test_a_model_that_only_guesses_the_bigger_class_is_called_out(self, wsgi, write_model, dislike_recall,
                                                                     guessing):
        write_model(likes=185, dislikes=40, like_recall=1.0, dislike_recall=dislike_recall)
        text = wsgi.get('/model').text
        expected = ['现在的模型把所有影片都判成喜欢，等于没有筛选。原因通常是"不喜欢"的打标太少：多标一些"不喜欢"的影片，再重新训练。']
        assert alert_texts(text, 'danger') == (expected if guessing else [])

    @pytest.mark.parametrize('scored, above_half, guessing', [(60, 60, True), (60, 59, True), (60, 58, False),
                                                              (49, 49, False), (0, 0, False)])
    def test_a_weak_model_that_puts_nearly_every_movie_on_one_side_is_called_out(self, wsgi, write_model, make_item,
                                                                                scored, above_half, guessing):
        # 检验里不喜欢只认对三成，没打标的影片又几乎都判成了喜欢
        write_model(likes=185, dislikes=40, like_recall=0.99, dislike_recall=0.3)
        score_movies(make_item, scored, above_half)
        assert bool(alert_texts(wsgi.get('/model').text, 'danger')) is guessing
        assert alike_line(wsgi.get('/model').text) is None

    def test_the_bigger_class_decides_which_side_counts(self, wsgi, write_model, make_item):
        # 打标里不喜欢多：分数几乎都不过半才算"全判成不喜欢"
        write_model(likes=40, dislikes=185, like_recall=0.3, dislike_recall=0.99)
        score_movies(make_item, 60, 60)
        assert alert_texts(wsgi.get('/model').text, 'danger') == []
        score_movies(make_item, 60, 1)
        assert alert_texts(wsgi.get('/model').text, 'danger') == [
            '现在的模型把所有影片都判成不喜欢，等于没有筛选。原因通常是"喜欢"的打标太少：多标一些"喜欢"的影片，再重新训练。']

    @pytest.mark.parametrize('dislike_recall', [0.5, 0.96])
    def test_a_model_that_tells_the_two_apart_is_not_blamed_for_movies_that_are_all_alike(self, wsgi, write_model,
                                                                                         make_item, dislike_recall):
        # 检验说明模型分得开；没打标的 90 部里 89 部相似度过半，是因为它们本来就像喜欢的那些
        write_model(likes=200, dislikes=40, like_recall=1.0, dislike_recall=dislike_recall, auc=0.99)
        score_movies(make_item, 90, 89)
        text = wsgi.get('/model').text
        assert alert_texts(text, 'danger') == [] and alert_texts(text, 'warning') == []
        assert '等于没有筛选' not in text
        assert alike_line(text) == ('还没打标的影片里，89 / 90 部的相似度都过半：它们大多和你喜欢的影片很像，不是模型分不开。'
                                    '「推荐」里仍是其中相似度最高的那一部分。')
        for page in ('/', '/?like=0'):
            home = wsgi.get(page).text
            assert alert_texts(home, 'warning') == [] and '没有参考价值' not in home

    def test_movies_that_are_all_unlike_are_described_the_other_way_round(self, wsgi, write_model, make_item):
        write_model(likes=40, dislikes=200, like_recall=0.9, dislike_recall=1.0)
        score_movies(make_item, 90, 1)
        text = wsgi.get('/model').text
        assert alert_texts(text, 'danger') == []
        assert alike_line(text) == ('还没打标的影片里，89 / 90 部的相似度都不到一半：它们大多和你不喜欢的影片很像，不是模型分不开。'
                                    '「推荐」里仍是其中相似度最高的那一部分。')

    @pytest.mark.parametrize('scored, above_half', [(90, 88), (49, 49), (0, 0)])
    def test_nothing_is_said_about_alike_movies_when_they_are_not(self, wsgi, write_model, make_item, scored,
                                                                 above_half):
        write_model(likes=200, dislikes=40, like_recall=1.0, dislike_recall=0.96)
        score_movies(make_item, scored, above_half)
        text = wsgi.get('/model').text
        assert alike_line(text) is None and alert_texts(text, 'danger') == []

    def test_tells_how_many_tags_there_are_now_and_whether_they_can_train(self, wsgi, make_item, write_model):
        def labels():
            return plain(BeautifulSoup(wsgi.get('/model').text, 'html.parser').select_one('#model-labels'))

        assert labels() == '现在有 0 条打标（喜欢 0、不喜欢 0），还不能训练：训练数据不足, 无法训练模型. 需要200, 当前0'
        add_rated(make_item, 205, like=True)
        add_rated(make_item, 4, like=False)
        assert labels() == ('现在有 209 条打标（喜欢 205、不喜欢 4），还不能训练：'
                            '"不喜欢"的打标只有 4 条，至少要 10 条才能训练，还差 6 条。请再标一些"不喜欢"的影片')
        for n in range(6):
            make_item(f'MORE-{n}')
            ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, f'MORE-{n}')
        assert labels() == '现在有 215 条打标（喜欢 205、不喜欢 10），可以训练'
        write_model(likes=205, dislikes=10)
        assert labels() == '现在有 215 条打标（喜欢 205、不喜欢 10），和训练时一样'
        write_model(likes=200, dislikes=10)
        assert labels() == '现在有 215 条打标（喜欢 205、不喜欢 10），训练之后有变化，重新训练才会用上'

    def test_unreadable_model_shows_reason(self, wsgi, data_dir):
        (data_dir / classifier.MODEL_FILE).write_bytes(b'garbage')
        text = wsgi.get('/model').text
        assert '无法读取' in text
        assert '重新训练' in text
        assert '还没有训练过模型' not in text

    @pytest.mark.parametrize('content', BROKEN_MODELS)
    def test_unusable_model_shows_the_same_reason_as_recommend_and_check(self, wsgi, data_dir, content):
        (data_dir / classifier.MODEL_FILE).write_bytes(content)
        with pytest.raises(ModelLoadError) as info:
            read_model(str(data_dir / classifier.MODEL_FILE))
        text = wsgi.get('/model').text
        assert f'<p class="card-text text-danger">{bottle.html_escape(str(info.value))} </p>' in text
        assert '重新训练' in text
        assert '认对' not in text
        assert '还没有训练过模型' not in text

    @pytest.mark.parametrize('change', [{'intercept': 10 ** 400}, {'tags': {'g1': [10 ** 400, 1.0]}},
                                        {'tags': {'g1': [1.0, -(10 ** 400)]}}],
                             ids=['intercept', 'idf', 'coefficient'])
    def test_a_number_far_too_big_makes_the_file_wrong_not_the_pages_fail(self, wsgi, write_model, make_item, change):
        make_item('ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-001')
        path = write_model()
        path.write_text(json.dumps({**json.loads(path.read_text(encoding='utf-8')), **change}), encoding='utf-8')
        wrong = '模型文件 model.json 格式不对，请到网页的"模型"页点击"开始训练"重新训练'
        for page in ('/', '/?like=0'):
            assert alert_texts(wsgi.get(page).text, 'warning') == [f'模型现在不能用，下面的内容不会更新：{wrong}']
        assert f'<p class="card-text text-danger">{bottle.html_escape(wrong)} </p>' in wsgi.get('/model').text
        assert wsgi.get('/pick').status == wsgi.get('/tagit').status == 200

    def test_model_files_of_an_older_version_need_retraining(self, wsgi, data_dir):
        for name in ('model.pkl', 'label_binarizer.pkl'):
            (data_dir / 'model' / name).write_bytes(b'left by an older version')
        text = wsgi.get('/model').text
        assert f'<p class="card-text text-danger">{bottle.html_escape(LEGACY_MODEL)} </p>' in text
        assert '认对' not in text
        assert '还没有训练过模型' not in text

    def test_while_the_old_model_is_being_replaced_the_page_says_so_instead_of_asking_to_train(
            self, wsgi, data_dir, make_item, monkeypatch, caplog):
        for name in ('model.pkl', 'label_binarizer.pkl'):
            (data_dir / 'model' / name).write_bytes(b'left by an older version')
        add_rated(make_item, 195, like=True)
        add_rated(make_item, 12, like=False)
        monkeypatch.setattr(schedule, '_upgrade_pending', True)
        text = wsgi.get('/model').text
        soup = BeautifulSoup(text, 'html.parser')
        assert plain(soup.select_one('#model-upgrading')) == (
            '现有的模型文件是旧版本程序训练的，新版本换了模型。程序正在用现有的打标自动重新训练，'
            '稍等片刻再刷新这个页面，不用点"开始训练"。')
        assert soup.select('p.text-danger') == [] and '不能再用' not in text and '还没有训练过模型' not in text
        assert plain(soup.select_one('#model-labels')) == '现在有 207 条打标（喜欢 195、不喜欢 12），可以训练'
        assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
        # 自动训练做完（或者没做成）以后，回到平常的说法
        monkeypatch.setattr(schedule, '_upgrade_pending', False)
        text = wsgi.get('/model').text
        assert f'<p class="card-text text-danger">{bottle.html_escape(LEGACY_MODEL)} </p>' in text
        assert BeautifulSoup(text, 'html.parser').select_one('#model-upgrading') is None

    def test_an_old_model_that_cannot_be_replaced_for_lack_of_tags_is_reported_as_before(self, wsgi, data_dir,
                                                                                        make_item, monkeypatch):
        (data_dir / 'model' / 'model.pkl').write_bytes(b'left by an older version')
        add_rated(make_item, 195, like=True)
        add_rated(make_item, 3, like=False)
        monkeypatch.setattr(schedule, '_upgrade_pending', True)
        text = wsgi.get('/model').text
        assert f'<p class="card-text text-danger">{bottle.html_escape(LEGACY_MODEL)} </p>' in text
        assert BeautifulSoup(text, 'html.parser').select_one('#model-upgrading') is None

    def test_a_current_model_is_shown_as_usual_while_the_startup_job_is_still_queued(self, wsgi, write_model,
                                                                                   monkeypatch):
        write_model(likes=185, dislikes=40)
        monkeypatch.setattr(schedule, '_upgrade_pending', True)
        text = wsgi.get('/model').text
        assert len(report_lines(text)) == 4
        assert BeautifulSoup(text, 'html.parser').select_one('#model-upgrading') is None
        assert alert_texts(wsgi.get('/').text, 'warning') == []

    def test_training(self, wsgi, data_dir, make_item):
        add_rated(make_item, 100, like=True)
        add_rated(make_item, 100, like=False)
        text = train(wsgi)
        assert '训练完成，下面是新模型的数据' in text
        lines = report_lines(text)
        assert lines[0].startswith('用了 200 条打标（喜欢 100、不喜欢 100）训练，训练时间 20')
        recalls = re.search(r'喜欢认对 (\d+)%，不喜欢认对 (\d+)%（全部猜喜欢时是 0%）', lines[1])
        assert recalls and int(recalls.group(1)) >= 90 and int(recalls.group(2)) >= 90
        assert alert_texts(text, 'warning') == alert_texts(text, 'danger') == []
        assert (data_dir / classifier.MODEL_FILE).is_file()

    def test_training_failure_shows_reason(self, wsgi, data_dir, make_item):
        add_rated(make_item, 3, like=True)
        text = train(wsgi)
        assert '训练失败：训练数据不足, 无法训练模型. 需要200, 当前3' in text
        assert '还没有训练过模型.' in text
        assert '训练完成' not in text
        assert not (data_dir / classifier.MODEL_FILE).exists()

    def test_training_failure_keeps_showing_current_model(self, wsgi, data_dir, make_item):
        add_rated(make_item, 100, like=True)
        add_rated(make_item, 100, like=False)
        shown = report_lines(train(wsgi))
        # 删掉一半打标后重新训练失败，原来的模型还在用
        ItemRate.delete().where(ItemRate.item_id.startswith('DIS')).execute()
        text = train(wsgi)
        assert '训练失败：训练数据不足, 无法训练模型. 需要200, 当前100' in text
        assert text.index('训练失败') < text.index('当前模型数据')
        assert len(shown) == 4 and report_lines(text) == shown

    def test_save_failure_keeps_showing_current_model(self, wsgi, data_dir, make_item, monkeypatch):
        add_rated(make_item, 100, like=True)
        add_rated(make_item, 100, like=False)
        shown = report_lines(train(wsgi))
        before = (data_dir / classifier.MODEL_FILE).read_bytes()

        def disk_full(path, write):
            raise OSError(errno.ENOSPC, '磁盘空间不足')

        monkeypatch.setattr(util, 'write_temp_file', disk_full)
        text = train(wsgi)
        assert re.search(r'训练失败：无法保存模型文件到 \S+（磁盘空间不足），原来的模型没有改动', text)
        assert len(shown) == 4 and report_lines(text) == shown
        assert '训练完成' not in text
        assert (data_dir / classifier.MODEL_FILE).read_bytes() == before

    def test_training_scores_the_movies_again_and_says_how_many(self, wsgi, make_item):
        add_rated(make_item, 100, like=True)
        add_rated(make_item, 100, like=False)
        with db.db.atomic():
            for i in range(30):
                fanhao = f'NEW-{i:03d}'
                make_item(fanhao, tags=[('genre', 'g0'), ('star', 's0')] if i < 5 else [('genre', f'g{1 + i % 9}')])
                # 上一个模型把它们全判成了喜欢
                ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, fanhao)
        text = train(wsgi)
        assert '训练完成，下面是新模型的数据。已重新打分 30 部，其中推荐 20 部' in text
        system = ItemRate.select().where(ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE)
        assert sum(rate.rate_value for rate in system) == 20
        assert all(ItemRate.get_by_fanhao(f'NEW-{i:03d}').rate_value == RATE_VALUE.LIKE for i in range(5))
        assert db.ItemScore.select().count() == 30

    def test_training_with_no_movie_left_to_score_says_so(self, wsgi, make_item):
        add_rated(make_item, 100, like=True)
        add_rated(make_item, 100, like=False)
        text = train(wsgi)
        assert '训练完成，下面是新模型的数据。现在没有需要打分的影片' in text
        assert '已重新打分' not in text

    def test_failed_training_scores_nothing(self, wsgi, make_item, write_model):
        write_model()
        add_rated(make_item, 3, like=True)
        make_item('NEW-001', tags=[('genre', 'g1')])
        text = train(wsgi)
        assert '训练失败' in text
        assert '已重新打分' not in text and '没有需要打分的影片' not in text
        assert ItemRate.get_by_fanhao('NEW-001') is None

    def test_get_does_not_train(self, wsgi, monkeypatch):
        calls = []
        monkeypatch.setattr(classifier, 'train_and_recommend', lambda: calls.append('train'))
        response = wsgi.get('/do-training')
        assert response.status == 303
        assert urlsplit(response.headers['location']).path == '/model'
        assert calls == []

    def test_refreshing_the_result_page_does_not_train_again(self, wsgi, monkeypatch):
        calls = []
        monkeypatch.setattr(classifier, 'train_and_recommend', lambda: calls.append('train') or (None, db.ScoreResult(7, 3, 0)))
        response = wsgi.post('/do-training')
        location = response.headers['location']
        for _ in range(2):
            assert '已重新打分 7 部，其中推荐 3 部' in wsgi.get(location[location.index('/model'):]).text
        assert calls == ['train']

    @pytest.mark.parametrize('query', ['training=999999', 'training=abc', 'training=', 'training=<b>x</b>'])
    def test_unknown_training_result_shows_nothing(self, wsgi, query):
        text = wsgi.get('/model?' + query).text
        assert '训练完成' not in text
        assert '训练失败' not in text
        assert '<b>x</b>' not in text

    def test_only_recent_training_results_are_kept(self, wsgi, monkeypatch):
        monkeypatch.setattr(classifier, 'train_and_recommend', lambda: (None, db.ScoreResult(0, 0, 0)))
        monkeypatch.setattr(index, '_training_results', {})
        ids = [redirect_target(wsgi.post('/do-training'))[1]['training'] for _ in range(index.MAX_TRAINING_RESULTS + 1)]
        assert '训练完成' not in wsgi.get(f'/model?training={ids[0]}').text
        assert '训练完成' in wsgi.get(f'/model?training={ids[-1]}').text
        assert len(index._training_results) == index.MAX_TRAINING_RESULTS


def train(wsgi):
    '''点模型页的"开始训练"：POST 之后跳回模型页，返回跳转后的页面'''
    path, query, _ = redirect_target(wsgi.post('/do-training'))
    assert path == '/model'
    return wsgi.get(f'/model?training={query["training"]}').text


class TestErrorPages:
    @pytest.mark.parametrize('method, path, status, reason', [
        ('GET', '/local_play/99999', 404, '没有这个本地文件'),
        ('GET', '/cover/NOPE-000', 404, '没有这个番号'),
        ('GET', '/no-such-page', 404, '没有这个页面，请检查网址'),
        ('POST', '/about', 405, '这个地址不能这样访问，请从页面上的链接或按钮进入'),
        ('POST', '/tag/ABC-001', 400, '提交的值只能是 0 或 1'),
        ('GET', '/static/images/missing.png', 404, '没有这个页面，请检查网址'),
    ])
    def test_chinese_error_page(self, wsgi, make_item, method, path, status, reason):
        make_item('ABC-001')
        response = wsgi.request(method, path)
        assert response.status == status
        assert response.headers['content-type'] == 'text/html; charset=UTF-8'
        text = response.text
        assert f'<title>{index.ERROR_TITLES[status]}</title>' in text
        assert f'<p>{reason}</p>' in text
        assert '<a href="/">返回首页</a>' in text
        assert 'href="/tagit">打标</a>' in text
        assert 'Sorry, the requested URL' not in text
        assert 'Error: ' not in text

    def test_405_keeps_allow_header(self, wsgi):
        response = wsgi.request('PUT', '/model')
        assert response.status == 405
        assert 'GET' in response.headers['allow']

    def test_server_error_hides_the_traceback(self, wsgi, monkeypatch, caplog):
        def broken(*args, **kwargs):
            raise RuntimeError('数据库文件坏了 secret-detail')

        monkeypatch.setattr(index, 'get_items', broken)
        response = wsgi.get('/', allow_server_error=True)
        assert response.status == 500
        assert '<p>程序出错了，出错的详细信息已写进日志</p>' in response.text
        assert 'secret-detail' not in response.text
        assert 'Traceback' not in response.text
        assert 'secret-detail' in response.errors
        assert "GET / 出错：RuntimeError('数据库文件坏了 secret-detail')" in caplog.messages

    def test_the_served_app_writes_the_traceback_to_the_log_instead_of_the_error_stream(self, served, monkeypatch,
                                                                                         caplog):
        def broken(*args, **kwargs):
            raise RuntimeError('数据库文件坏了 secret-detail')

        monkeypatch.setattr(index, 'get_items', broken)
        response = served.get('/', allow_server_error=True)
        assert response.status == 500
        assert '<p>程序出错了，出错的详细信息已写进日志</p>' in response.text
        assert 'secret-detail' not in response.text
        # 服务器给的 wsgi.errors（waitress 下是控制台）里没有东西：栈成了一条日志，控制台和日志文件各有一份
        assert response.errors == ''
        trace, = [message for message in caplog.messages if message.startswith('请求出错，异常栈：\n')]
        assert 'Traceback (most recent call last):' in trace
        assert trace.endswith('RuntimeError: 数据库文件坏了 secret-detail')
        assert "GET / 出错：RuntimeError('数据库文件坏了 secret-detail')" in caplog.messages

    def test_the_served_app_answers_like_the_plain_one(self, served, wsgi, make_item):
        make_item('ABC-001')
        for path in ('/about', '/tagit', '/static/css/bustag.css', '/nope'):
            answer = served.get(path)
            assert (answer.status, answer.body) == (wsgi.get(path).status, wsgi.get(path).body)
            assert answer.errors == ''


BUSY_REASON = ('数据库正被别的程序占用（等了好一会儿还没轮到），这次操作没有完成。请稍后再试；'
               '一直这样的话，看看是不是有别的程序（另一个 bustag、数据库工具）正开着 bus.db')
CROWDED_REASON = ('数据库正被别的程序占用，已经有好几个操作在等它了，不再多等一个，这次操作没有完成。请稍后再试；'
                  '一直这样的话，看看是不是有别的程序（另一个 bustag、数据库工具）正开着 bus.db')


class TestBusyDatabase:
    '''数据库的写锁被别的程序占着、等到超时还没轮到：给"数据库正忙"的页面，不是"程序出错"，日志里也没有异常栈'''

    def test_tagging_shows_a_busy_page_and_saves_nothing(self, served, make_item, locked_database, caplog):
        make_item('ABC-001')
        with locked_database():
            response = served.post('/tag/ABC-001?page=1', {'submit': '1', 'formid': 'form-1'},
                                   allow_server_error=True)
        assert response.status == 503
        assert response.headers['content-type'] == 'text/html; charset=UTF-8'
        assert '<title>数据库正忙</title>' in response.text
        assert f'<p>{BUSY_REASON}</p>' in response.text
        assert '<a href="/">返回首页</a>' in response.text and '程序出错' not in response.text
        assert ItemRate.get_by_fanhao('ABC-001') is None
        assert response.errors == ''
        assert [(record.levelname, record.getMessage()) for record in caplog.records
                if record.levelno >= logging.WARNING] == [
            ('WARNING', 'POST /tag/ABC-001 没有完成：数据库正被别的程序占用（等了好一会儿还没轮到）')]
        # 锁放开以后照常
        assert served.post('/tag/ABC-001', {'submit': '1'}).status == 303

    def test_pages_that_only_read_still_open(self, wsgi, make_item, locked_database):
        make_item('ABC-001')
        with locked_database():
            for path in ('/', '/tagit', '/pick', '/model', '/local', '/about'):
                assert wsgi.get(path).status == 200, path

    @pytest.fixture
    def old_journal(self, tmp_path):
        '''从没换成 WAL 的老库（日志方式还是 delete），表都已经建好'''
        path = tmp_path / 'old.db'
        original = db.db.database
        db.db.close()
        db.db.init(str(path), **db.DB_OPTIONS)
        db.create_tables()
        db.db.close()
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.close()
        try:
            yield path
        finally:
            db.db.close()
            db.db.init(original, **db.DB_OPTIONS)

    def test_an_old_database_held_exclusively_still_gets_quick_answers(self, served, old_journal, monkeypatch, caplog):
        # 别的程序以 EXCLUSIVE 锁占着从没换成 WAL 的老库（连读都不让）：静态文件照常，页面很快说"数据库正忙"，线程不干等
        attempts = []
        real = db._Database._set_pragmas

        def counting(self, conn):
            attempts.append(conn)
            return real(self, conn)

        monkeypatch.setattr(db._Database, '_set_pragmas', counting)
        other = sqlite3.connect(old_journal, isolation_level=None)
        other.execute('BEGIN EXCLUSIVE')
        try:
            static = served.get('/static/css/bustag.css')
            opened_for_static = len(attempts)
            began = time.monotonic()
            page = served.get('/', allow_server_error=True)
            page_took = time.monotonic() - began
        finally:
            other.execute('ROLLBACK')
            other.close()
        assert (static.status, page.status) == (200, 503)
        # 静态文件根本不碰数据库；页面不等满 30 秒（两次就是一分钟），界限放宽到慢的机器上也不会误报
        assert opened_for_static == 0 and page_took < db.BUSY_TIMEOUT / 2
        assert f'<p>{db.DATABASE_LOCKED_OUT}，{index.BUSY_ADVICE}</p>' in page.text
        # 首页自己也打不开：不给"返回首页"
        assert '返回首页' not in page.text
        assert page.errors == ''
        assert [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING] == [
            f'GET / 没有完成：{db.DATABASE_LOCKED_OUT}']
        assert served.get('/').status == 200

    def test_an_old_database_held_exclusively_is_tried_once_per_page(self, served, old_journal, monkeypatch, caplog):
        # 每试一次都要等一会儿（换 WAL、读一下各等 LOCKED_WAIT）：一个页面只试着打开一次
        attempts = []
        real = db._Database._set_pragmas

        def counting(self, conn):
            attempts.append(conn)
            return real(self, conn)

        monkeypatch.setattr(db._Database, '_set_pragmas', counting)
        other = sqlite3.connect(old_journal, isolation_level=None)
        other.execute('BEGIN EXCLUSIVE')
        try:
            page = served.get('/pick', allow_server_error=True)
        finally:
            other.execute('ROLLBACK')
            other.close()
        assert page.status == 503 and len(attempts) == 1
        assert f'<p>{db.DATABASE_LOCKED_OUT}，{index.BUSY_ADVICE}</p>' in page.text and '返回首页' not in page.text
        assert [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING] == [
            f'GET /pick 没有完成：{db.DATABASE_LOCKED_OUT}']
        assert served.get('/pick').status == 200

    def test_a_page_that_needs_a_table_not_added_yet_says_why(self, served, monkeypatch, caplog):
        # 新版本第一次打开旧库时库被别的程序占着，新加的表还没建：用到它们的页面说明原因，不是"程序出错"
        monkeypatch.setattr(db, 'tables_missing', True)

        def status():
            raise OperationalError('no such table: job_state')

        monkeypatch.setattr(avwiki, 'status', status)
        response = served.get('/about', allow_server_error=True)
        assert response.status == 503 and '<title>数据库正忙</title>' in response.text
        assert f'<p>{db.TABLES_MISSING}，{index.TABLES_ADVICE}</p>' in response.text
        # 首页要用的表都在，首页打得开：照旧给"返回首页"
        assert '<a href="/">返回首页</a>' in response.text
        assert response.errors == ''
        assert [(record.levelname, record.getMessage()) for record in caplog.records
                if record.levelno >= logging.WARNING] == [('WARNING', f'GET /about 没有完成：{db.TABLES_MISSING}')]

    @pytest.mark.parametrize('stars, missing, home', [
        ((), 'item_score', False),
        ((), 'actress_alias', True),
        (('演员甲',), 'actress_alias', False),
        (('演员甲',), 'job_state', True)])
    def test_the_way_back_home_is_offered_when_the_home_page_has_its_tables(self, served, monkeypatch, stars,
                                                                           missing, home):
        # 首页要用影片、标签、评分、分数这几张表；指定推荐里选了女优时还要按名字找人的几张
        if stars:
            index.pick.add_names('star', list(stars))
        present = [name for name in db.db.get_tables() if name != missing]
        monkeypatch.setattr(db.db, 'get_tables', lambda *args, **kwargs: present)
        monkeypatch.setattr(db, 'tables_missing', True)

        def status():
            raise OperationalError(f'no such table: {missing}')

        monkeypatch.setattr(avwiki, 'status', status)
        response = served.get('/about', allow_server_error=True)
        assert response.status == 503
        assert ('<a href="/">返回首页</a>' in response.text) is home

    def test_a_missing_table_is_a_program_error_once_the_tables_were_added(self, served, monkeypatch):
        def status():
            raise OperationalError('no such table: job_state')

        monkeypatch.setattr(avwiki, 'status', status)
        assert served.get('/about', allow_server_error=True).status == 500

    def test_when_several_requests_already_wait_for_the_lock_the_next_one_is_not_made_to_wait_too(
            self, served, make_item, locked_database, monkeypatch, caplog):
        # 服务一共 8 个线程：都在干等锁的话，连"关于"页也没有线程来处理。同时等的最多一半，其余的立刻得到"数据库正忙"
        for n in range(1, 6):
            make_item(f'ABC-00{n}')
        monkeypatch.setattr(db, 'MAX_WAITING_REQUESTS', 2)
        answers = {}

        def tag(n):
            try:
                answers[n] = served.post(f'/tag/ABC-00{n}?page=1', {'submit': '1'}, allow_server_error=True)
            finally:
                db.db.close()

        with locked_database():
            db.db.timeout = 20
            waiting = [threading.Thread(target=tag, args=(n,)) for n in (1, 2)]
            for thread in waiting:
                thread.start()
            deadline = time.monotonic() + 60
            while db.waiting_requests() < 2:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            # 下面"马上"的界限是不跟着等那 20 秒：放宽到一半，慢的机器上也不会误报
            started = time.monotonic()
            crowded = served.post('/tag/ABC-003?page=1', {'submit': '1'}, allow_server_error=True)
            assert time.monotonic() - started < db.db.timeout / 2
            assert crowded.status == 503 and '<title>数据库正忙</title>' in crowded.text
            assert f'<p>{CROWDED_REASON}</p>' in crowded.text and '程序出错' not in crowded.text
            assert crowded.errors == ''
            # 只读的页面照常、马上就开
            for path in ('/about', '/', '/tagit', '/pick', '/model'):
                started = time.monotonic()
                assert served.get(path).status == 200, path
                assert time.monotonic() - started < db.db.timeout / 2, path
            assert sorted(answers) == []
        for thread in waiting:
            thread.join(timeout=20)
        assert [answers[n].status for n in (1, 2)] == [303, 303]
        assert ItemRate.get_by_fanhao('ABC-003') is None
        assert [(record.levelname, record.getMessage()) for record in caplog.records
                if record.levelno >= logging.WARNING] == [
            ('WARNING', 'POST /tag/ABC-003 没有完成：数据库正被别的程序占用，已经有好几个操作在等它了，不再多等一个')]
        # 锁放开以后，这个请求重来一次就行
        assert served.post('/tag/ABC-003', {'submit': '1'}).status == 303

    def test_training_is_left_out_of_the_limit_because_it_says_how_long_it_waited(self, wsgi, make_item,
                                                                                  locked_database, monkeypatch):
        # 训练一次只有一个（模型锁），打分等不到锁时页面上写的是"等了好一会儿"，所以它自己照常等
        add_rated(make_item, 100, like=True)
        add_rated(make_item, 100, like=False)
        make_item('NEW-001', tags=[('genre', 'g0'), ('star', 's0')])
        monkeypatch.setattr(db, 'MAX_WAITING_REQUESTS', 0)
        turned_away = []

        class Spy(db.TooManyWaiting):
            def __init__(self, *args):
                turned_away.append(args)
                super().__init__(*args)

        monkeypatch.setattr(db, 'TooManyWaiting', Spy)
        with locked_database():
            text = train(wsgi)
        assert turned_away == []
        assert alert_texts(text, 'success') == ['训练完成，下面是新模型的数据。']
        assert alert_texts(text, 'warning') == [
            '模型已保存，但影片还没有重新打分：数据库正被别的程序占用（等了好一会儿还没轮到）。'
            '下一次抓取后会自动打分，也可以稍后再点一次"开始训练"']

    def test_other_database_errors_are_still_program_errors(self, wsgi, make_item, monkeypatch):
        make_item('ABC-001')

        def broken(*args, **kwargs):
            raise OperationalError('no such table: item_rate')

        monkeypatch.setattr(ItemRate, 'save_user_rate', staticmethod(broken))
        response = wsgi.post('/tag/ABC-001', {'submit': '1'}, allow_server_error=True)
        assert response.status == 500
        assert '<title>程序出错</title>' in response.text

    def test_training_keeps_the_new_model_and_says_the_scores_are_still_to_come(self, wsgi, make_item, data_dir,
                                                                                locked_database):
        add_rated(make_item, 100, like=True)
        add_rated(make_item, 100, like=False)
        make_item('NEW-001', tags=[('genre', 'g0'), ('star', 's0')])
        with locked_database():
            text = train(wsgi)
        assert alert_texts(text, 'success') == ['训练完成，下面是新模型的数据。']
        assert alert_texts(text, 'warning') == [
            '模型已保存，但影片还没有重新打分：数据库正被别的程序占用（等了好一会儿还没轮到）。'
            '下一次抓取后会自动打分，也可以稍后再点一次"开始训练"']
        assert '训练失败' not in text and alert_texts(text, 'danger') == []
        assert report_lines(text)[0].startswith('用了 200 条打标（喜欢 100、不喜欢 100）训练')
        assert (data_dir / classifier.MODEL_FILE).is_file()
        assert ItemRate.get_by_fanhao('NEW-001') is None and db.ItemScore.select().count() == 0
        # 稍后再点一次：打分补上了
        text = train(wsgi)
        assert alert_texts(text, 'success') == ['训练完成，下面是新模型的数据。已重新打分 1 部，其中推荐 1 部']
        assert alert_texts(text, 'warning') == []


class TestPagination:
    @staticmethod
    def render(max_page, curr_page):
        return bottle.template('pagination', page_info=(max_page * 10, max_page, curr_page, 10), like=1)

    def options(self, max_page, curr_page):
        return [int(n) for n in re.findall(r'<option[^>]*>(\d+)</option>', self.render(max_page, curr_page))]

    def test_few_pages_are_all_listed(self):
        assert self.options(5, 3) == [1, 2, 3, 4, 5]
        assert self.options(101, 51) == list(range(1, 102))

    @pytest.mark.parametrize('curr_page, expected', [
        (1500, [1] + list(range(1450, 1551)) + [3000]),
        (1, list(range(1, 52)) + [3000]),
        (30, list(range(1, 81)) + [3000]),
        (3000, [1] + list(range(2950, 3001))),
    ])
    def test_many_pages_list_first_last_and_nearby(self, curr_page, expected):
        assert self.options(3000, curr_page) == expected

    def test_current_page_is_selected(self):
        html = self.render(3000, 1500)
        assert re.findall(r'<option selected value="([^"]+)">', html) == ['?page=1500&amp;like=1']
        assert len(html) < 10000

    @pytest.mark.parametrize('like, q, links', [
        (1, '', ['?page=1&like=1', '?page=1&like=1', '?page=3&like=1', '?page=3&like=1']),
        (0, '学生', ['?page=1&like=0&q=%E5%AD%A6%E7%94%9F'] * 2 + ['?page=3&like=0&q=%E5%AD%A6%E7%94%9F'] * 2),
        # 没有标签页可分的列表（打标页的"未打标的"、指定推荐）链接里不带 like
        (None, '', ['?page=1', '?page=1', '?page=3', '?page=3']),
        ('', '', ['?page=1', '?page=1', '?page=3', '?page=3']),
        (None, '学生', ['?page=1&q=%E5%AD%A6%E7%94%9F'] * 2 + ['?page=3&q=%E5%AD%A6%E7%94%9F'] * 2),
    ], ids=['recommended', 'disliked-with-a-word', 'untagged', 'pick', 'untagged-with-a-word'])
    def test_links_carry_the_tab_only_where_there_is_one(self, like, q, links):
        html = bottle.template('pagination', page_info=(30, 3, 2, 10), like=like, q=q)
        soup = BeautifulSoup(html, 'html.parser')
        assert [link['href'] for link in soup.select('a')] == links
        assert [option['value'] for option in soup.select('option')] == [
            links[0].replace('page=1', f'page={n}') for n in (1, 2, 3)]


class TestCardAnchors:
    '''打标后回到 #form-N：这个位置是整张卡片（封面在最上面），不是按钮那一列'''

    @pytest.fixture
    def library(self, make_item):
        for n in (1, 2, 3):
            make_item(f'SYS-00{n}', tags=[('genre', '标签一')])
            ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, f'SYS-00{n}')
            make_item(f'NEW-00{n}', tags=[('genre', '标签一')])
        index.pick.add_names('tag', ['标签一'])

    @pytest.mark.parametrize('path, shown', [('/', 3), ('/tagit', 3), ('/pick', 6)])
    def test_the_place_a_tag_comes_back_to_holds_the_cover(self, wsgi, library, path, shown):
        soup = BeautifulSoup(wsgi.get(path).text, 'html.parser')
        forms = soup.select('form[action^="/tag/"]')
        assert len(forms) == shown
        for number, form in enumerate(forms, start=1):
            fanhao = form['action'].split('/')[2].split('?')[0]
            name = form.select_one('input[name="formid"]')['value']
            assert name == f'form-{number}'
            place, = soup.select(f'[id="{name}"]')
            assert place.select_one('img.coverimg')['data-cover'] == f'/cover/{fanhao}'


class TestCardBadges:
    '''卡片上的类别 / 演员徽标放在带 item-tags 的块里：样式表让这里面的徽标在卡片里折行，名字再长也不伸到按钮那一列下面'''

    LONG = '名字特别长的标签' * 6

    @pytest.fixture
    def library(self, make_item):
        make_item('SYS-001', tags=[('genre', self.LONG), ('star', '演员甲')])
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'SYS-001')
        make_item('NEW-001', tags=[('genre', self.LONG), ('star', '演员甲')])
        make_item('LOC-001', tags=[('genre', self.LONG), ('star', '演员甲')])
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'LOC-001')
        LocalItem.saveit('LOC-001', 'http://media.local/LOC-001.mp4')
        index.pick.add_names('star', ['演员甲'])

    @pytest.mark.parametrize('path', ['/', '/tagit', '/tagit?like=1', '/pick', '/local'])
    def test_genre_and_actress_badges_sit_in_a_block_that_lets_them_wrap(self, wsgi, library, path):
        soup = BeautifulSoup(wsgi.get(path).text, 'html.parser')
        badges = soup.select('span.badge-primary, span.badge-warning')
        assert {plain(badge) for badge in badges} == {self.LONG, '演员甲'}
        for badge in badges:
            assert 'item-tags' in badge.parent.get('class', [])

    def test_the_style_sheet_lets_them_wrap(self, wsgi):
        rule, = re.findall(r'\.item-tags \.badge \{([^}]*)\}', wsgi.get('/static/css/bustag.css').text)
        assert 'white-space: normal' in rule and 'word-break: break-all' in rule


class Calls(list):
    '''启动过程中依次调用的函数；served 是交给 create_server 的应用，server_options 是传给它的其他参数'''
    served = None
    server_options = None


class FakeProbe:
    '''
    代替 socket.socket：记录选项；bind 到 occupied 里的地址时抛"端口已被占用"，到 refused 里的地址时抛对应的错；
    建 unsupported 里的地址族时抛"不支持"（这台电脑没有 IPv6）；设 broken 里的选项时抛错
    '''
    occupied = set()
    refused = {}
    unsupported = set()
    broken = set()
    created = []

    def __init__(self, family, socktype, proto=0):
        if family in self.unsupported:
            raise OSError(10047, '不支持这种地址族')
        self.family = family
        self.options = []
        self.bound = None
        self.closed = False
        FakeProbe.created.append(self)

    def setsockopt(self, level, option, value):
        if (level, option) in self.broken:
            raise OSError(10042, '选项不能用')
        self.options.append((level, option, value))

    def bind(self, address):
        if address in self.occupied:
            raise OSError(10048, '端口已被占用')
        if address in self.refused:
            raise self.refused[address]
        self.bound = address

    def close(self):
        self.closed = True


@pytest.fixture
def fake_socket(monkeypatch):
    lookups = []

    def getaddrinfo(host, port, type=0, flags=0):
        lookups.append((host, port))
        if host == 'bad host':
            raise OSError(11001, 'getaddrinfo failed')
        return [(2, 1, 6, '', (host, port))]

    def create_connection(address, timeout=None):
        connections.append((address, timeout))
        raise ConnectionRefusedError(10061, '连接被拒绝')

    connections = []
    FakeProbe.occupied = set()
    FakeProbe.refused = {}
    FakeProbe.unsupported = set()
    FakeProbe.broken = set()
    FakeProbe.created = []
    module = types.SimpleNamespace(SO_EXCLUSIVEADDRUSE=-5, SOL_SOCKET=0xffff, SOCK_STREAM=1, AI_PASSIVE=1,
                                   AF_INET=2, AF_INET6=23, IPPROTO_IPV6=41, IPV6_V6ONLY=27,
                                   getaddrinfo=getaddrinfo, socket=FakeProbe, lookups=lookups,
                                   create_connection=create_connection, connections=connections)
    monkeypatch.setattr(index, 'socket', module)
    return module


class TestStartup:
    @pytest.mark.parametrize('env, config, expected', [
        ({}, None, ('127.0.0.1', 8000)),
        ({'BUSTAG_HOST': '192.168.1.5', 'BUSTAG_PORT': '8123'}, None, ('192.168.1.5', 8123)),
        ({'BUSTAG_HOST': '', 'BUSTAG_PORT': ''}, None, ('127.0.0.1', 8000)),
        ({}, '0.0.0.0', ('0.0.0.0', 8000)),
        ({}, ' 0.0.0.0 ', ('0.0.0.0', 8000)),
        ({}, '', ('127.0.0.1', 8000)),
        ({'BUSTAG_HOST': '127.0.0.1'}, '0.0.0.0', ('127.0.0.1', 8000)),
        ({'BUSTAG_HOST': ''}, '0.0.0.0', ('0.0.0.0', 8000)),
    ], ids=['default', 'environment', 'empty-environment', 'config', 'config-with-blanks', 'empty-config',
            'environment-wins', 'empty-environment-leaves-config'])
    def test_listen_address(self, fake_socket, monkeypatch, env, config, expected):
        '''监听地址：环境变量 BUSTAG_HOST > config.ini 的 [server] host > 127.0.0.1（只有本机能访问）'''
        monkeypatch.delenv('BUSTAG_HOST', raising=False)
        monkeypatch.delenv('BUSTAG_PORT', raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        if config is not None:
            monkeypatch.setitem(util.APP_CONFIG, 'server.host', config)
        assert index._listen_address() == expected
        assert fake_socket.lookups == [expected]

    def test_an_old_config_without_the_server_section_listens_on_this_computer_only(self, fake_socket, data_dir,
                                                                                   monkeypatch):
        monkeypatch.delenv('BUSTAG_HOST', raising=False)
        monkeypatch.delenv('BUSTAG_PORT', raising=False)
        (data_dir / 'config.ini').write_text('[download]\ncount = 7\n\n[options]\nproxy =\n', encoding='utf-8')
        util.load_config()
        assert index._listen_address() == ('127.0.0.1', 8000)

    @pytest.mark.parametrize('port', ['abc', '0', '65536', '-1', '80.5'])
    def test_invalid_port(self, monkeypatch, port):
        monkeypatch.setenv('BUSTAG_PORT', port)
        with pytest.raises(index.StartupError, match=re.escape(f'环境变量 BUSTAG_PORT={port} 不是有效的端口号（1-65535）')):
            index._listen_address()

    @pytest.mark.parametrize('env, config, source', [
        ('bad host', None, '环境变量 BUSTAG_HOST=bad host'),
        ('', 'bad host', 'config.ini 的 [server] host = bad host'),
    ], ids=['environment', 'config'])
    def test_an_address_that_cannot_be_listened_on_is_named_with_where_it_came_from(self, fake_socket, monkeypatch,
                                                                                    env, config, source):
        monkeypatch.setenv('BUSTAG_HOST', env)
        monkeypatch.delenv('BUSTAG_PORT', raising=False)
        if config is not None:
            monkeypatch.setitem(util.APP_CONFIG, 'server.host', config)
        with pytest.raises(index.StartupError) as info:
            index._listen_address()
        assert str(info.value) == (f'{source} 不是有效的监听地址（[Errno 11001] getaddrinfo failed）。'
                                   '只让这台电脑访问写 127.0.0.1，让同一局域网里的设备也能访问写 0.0.0.0')

    @pytest.mark.parametrize('host', ['::', '0:0:0:0:0:0:0:0', '[::]'])
    @pytest.mark.parametrize('from_env', [True, False], ids=['environment', 'config'])
    def test_the_ipv6_wildcard_is_refused_with_what_to_write_instead(self, fake_socket, monkeypatch, host, from_env):
        monkeypatch.delenv('BUSTAG_PORT', raising=False)
        monkeypatch.setenv('BUSTAG_HOST', host if from_env else '')
        monkeypatch.setitem(util.APP_CONFIG, 'server.host', '0.0.0.0' if from_env else host)
        source = f'环境变量 BUSTAG_HOST={host}' if from_env else f'config.ini 的 [server] host = {host}'
        with pytest.raises(index.StartupError) as info:
            index._listen_address()
        assert str(info.value) == (f'{source} 不支持：这样只监听 IPv6，用 127.0.0.1 和局域网里 192.168.x.x 这样的地址都打不开。'
                                   '让同一局域网里的设备也能访问请写 0.0.0.0')
        assert fake_socket.lookups == []

    @pytest.mark.parametrize('host, loopback', [
        ('127.0.0.1', True), ('127.0.0.2', True), ('localhost', True), ('LOCALHOST', True), ('::1', True),
        ('[::1]', True), ('0.0.0.0', False), ('::', False), ('192.168.1.5', False), ('nas.local', False),
    ])
    def test_which_addresses_only_this_computer_can_reach(self, host, loopback):
        assert index._is_loopback(host) is loopback

    @pytest.mark.parametrize('host, lan, container, notes', [
        ('127.0.0.1', None, False, [
            '浏览器访问 http://127.0.0.1:8000',
            '现在只有这台电脑能打开。要让同一局域网里的手机或电脑访问，在 config.ini 里写上 [server] host = 0.0.0.0'
            '（或者设置环境变量 BUSTAG_HOST=0.0.0.0），然后重启']),
        ('0.0.0.0', '192.168.1.5', False, [
            '浏览器访问 http://127.0.0.1:8000',
            '正在监听所有网络地址：同一局域网里的设备可以用 http://192.168.1.5:8000 访问。'
            '程序没有登录保护，不要在公共网络（公共 Wi-Fi 等）上这样用']),
        ('0.0.0.0', None, False, [
            '浏览器访问 http://127.0.0.1:8000',
            '正在监听所有网络地址：同一局域网里的设备可以用 http://<这台电脑的局域网 IP>:8000 访问。'
            '这台电脑的局域网 IP 程序拿不准，请自己查：在命令行里运行 ipconfig，看无线或有线网卡的"IPv4 地址"（一般是 192.168 开头）。'
            '程序没有登录保护，不要在公共网络（公共 Wi-Fi 等）上这样用']),
        ('0.0.0.0', '172.17.0.2', True, [
            '浏览器访问 http://127.0.0.1:8000',
            '正在监听所有网络地址（在容器里运行）：从容器外面用宿主机的地址和映射出来的端口访问。'
            '程序没有登录保护，不要把这个端口映射到公网']),
        ('192.168.1.5', None, False, [
            '浏览器访问 http://192.168.1.5:8000',
            '能连到这个地址的设备都可以打开。程序没有登录保护，不要在公共网络（公共 Wi-Fi 等）上这样用']),
        ('::1', None, False, [
            '浏览器访问 http://[::1]:8000',
            '现在只有这台电脑能打开。要让同一局域网里的手机或电脑访问，在 config.ini 里写上 [server] host = 0.0.0.0'
            '（或者设置环境变量 BUSTAG_HOST=0.0.0.0），然后重启']),
        ('fd00::5', None, False, [
            '浏览器访问 http://[fd00::5]:8000',
            '能连到这个地址的设备都可以打开。程序没有登录保护，不要在公共网络（公共 Wi-Fi 等）上这样用']),
    ], ids=['this-computer-only', 'all-addresses', 'all-addresses-lan-unknown', 'container', 'one-address',
            'ipv6-this-computer-only', 'ipv6-one-address'])
    def test_startup_says_who_can_open_the_pages(self, monkeypatch, host, lan, container, notes):
        monkeypatch.setattr(index, 'ON_WINDOWS', True)
        monkeypatch.setattr(index, '_lan_address', lambda: lan)
        monkeypatch.setattr(index, '_in_container', lambda: container)
        assert index._access_notes(host, 8000) == notes

    def test_how_to_look_up_the_lan_address_is_told_in_the_words_of_the_system(self, monkeypatch):
        monkeypatch.setattr(index, 'ON_WINDOWS', False)
        monkeypatch.setattr(index, '_lan_address', lambda: None)
        monkeypatch.setattr(index, '_in_container', lambda: False)
        assert index._access_notes('0.0.0.0', 8000)[1] == (
            '正在监听所有网络地址：同一局域网里的设备可以用 http://<这台电脑的局域网 IP>:8000 访问。'
            '这台电脑的局域网 IP 程序拿不准，请自己查：在终端里运行 ip addr（macOS 上是 ifconfig），'
            '看无线或有线网卡的 inet 地址（一般是 192.168 开头）。'
            '程序没有登录保护，不要在公共网络（公共 Wi-Fi 等）上这样用')

    @pytest.mark.parametrize('host, url', [
        ('0.0.0.0', 'http://127.0.0.1:8000'), ('192.168.1.5', 'http://192.168.1.5:8000'),
        ('localhost', 'http://localhost:8000'), ('::1', 'http://[::1]:8000'), ('fd00::5', 'http://[fd00::5]:8000'),
    ])
    def test_where_the_pages_are_opened_on_this_computer(self, host, url):
        assert index._page_url(host, 8000) == url

    # 对"所有地址"独占试绑的地址：IPv6 的 ::，同时收 IPv4
    EVERYWHERE = ('::', 8000, 0, 0)

    def test_a_free_port_is_probed_on_its_address_and_on_every_address_and_nobody_is_called(self, fake_socket):
        index._ensure_port_free('0.0.0.0', 8000, True)
        listen, everywhere = FakeProbe.created
        assert listen.options == [(0xffff, -5, 1)]
        assert (listen.bound, listen.closed) == (('0.0.0.0', 8000), True)
        # 所有地址：IPv6 的 :: 把 IPV6_V6ONLY 关掉，IPv4 和 IPv6 上占着的都看得到
        assert everywhere.options == [(41, 27, 0), (0xffff, -5, 1)]
        assert (everywhere.family, everywhere.bound, everywhere.closed) == (23, self.EVERYWHERE, True)
        # 哪里都没人占着，就不用再连一下确认：Windows 上连一个没人听的端口要等满超时才返回，每次启动都白等
        assert fake_socket.connections == []

    def test_a_port_held_on_some_other_address_is_settled_by_one_connection(self, fake_socket):
        # 占着的是 127.0.0.2 这样的别的具体地址：对所有地址试绑说有人占着，监听 127.0.0.1 却不受妨碍，连一下就分清了
        FakeProbe.occupied = {self.EVERYWHERE}
        index._ensure_port_free('127.0.0.1', 8000, True)
        assert fake_socket.connections == [(('127.0.0.1', 8000), index.CONNECT_TIMEOUT)]

    @pytest.mark.parametrize('held', [False, True], ids=['free', 'held'])
    def test_without_ipv6_every_ipv4_address_is_probed_instead(self, fake_socket, held):
        FakeProbe.unsupported = {23}
        FakeProbe.occupied = {('0.0.0.0', 8000)} if held else set()
        index._ensure_port_free('127.0.0.1', 8000, True)
        listen, everywhere = FakeProbe.created
        # 绑得上才记下绑的地址；"被占着"是靠 occupied 里写的 0.0.0.0 才抛出来的
        assert (everywhere.family, everywhere.options) == (2, [(0xffff, -5, 1)])
        assert everywhere.bound == (None if held else ('0.0.0.0', 8000))
        assert fake_socket.connections == ([(('127.0.0.1', 8000), index.CONNECT_TIMEOUT)] if held else [])

    def test_a_probe_that_cannot_be_set_up_does_not_say_the_port_is_free(self, fake_socket):
        FakeProbe.broken = {(41, 27)}
        index._ensure_port_free('127.0.0.1', 8000, True)
        assert fake_socket.connections == [(('127.0.0.1', 8000), index.CONNECT_TIMEOUT)]

    NOT_HERE = OSError(10049, '在其上下文中，该请求的地址无效。')
    CANNOT_LISTEN = ('无法监听 192.0.2.1:8000（[Errno 10049] 在其上下文中，该请求的地址无效。）。'
                     '请检查监听地址是不是这台电脑的地址、这个端口能不能用')

    @pytest.fixture
    def not_this_computer(self, fake_socket, monkeypatch):
        '''192.0.2.1（保留给文档用的地址）不在这台电脑上：往上面绑什么都是"地址无效"；连接和读页面都记下来，不应该有'''
        FakeProbe.refused = {('192.0.2.1', 8000): self.NOT_HERE, ('192.0.2.1', 0): self.NOT_HERE}
        pages = []
        monkeypatch.setattr(index.http.client, 'HTTPConnection', lambda *args, **kwargs: pages.append(args))
        return pages

    def test_an_address_that_is_not_this_computers_is_not_connected_to(self, fake_socket, not_this_computer):
        # 连它只会连到别的机器，或者被代理软件接住，读到的页面当成"别的程序占着"
        with pytest.raises(index.StartupError) as info:
            index._ensure_port_free('192.0.2.1', 8000, True)
        assert str(info.value) == self.CANNOT_LISTEN
        assert fake_socket.connections == [] and not_this_computer == []

    def test_where_there_is_no_exclusive_bind_such_an_address_is_not_connected_to_either(self, fake_socket,
                                                                                         not_this_computer):
        del fake_socket.SO_EXCLUSIVEADDRUSE
        index._ensure_port_free('192.0.2.1', 8000, True)
        assert fake_socket.connections == [] and not_this_computer == []
        error = index._cannot_listen('192.0.2.1', 8000, OSError(10049, '在其上下文中，该请求的地址无效。'), True)
        assert str(error) == self.CANNOT_LISTEN
        assert fake_socket.connections == [] and not_this_computer == []

    def test_an_address_of_this_computer_that_is_not_loopback_is_still_asked_about(self, fake_socket):
        # 假的 socket 让 192.168.1.5 绑得上：它是这台电脑的网卡地址，另一个监听所有地址的 bustag 也答它
        index._ensure_port_free('192.168.1.5', 8000, True)
        assert fake_socket.connections == []
        FakeProbe.occupied = {self.EVERYWHERE}
        index._ensure_port_free('192.168.1.5', 8000, True)
        assert fake_socket.connections == [(('192.168.1.5', 8000), index.CONNECT_TIMEOUT)]

    SAME_DIR_UNKNOWN = ('端口 8000 上已经有一个 bustag（v{version}）在运行，页面在 http://127.0.0.1:8000。'
                        '没法确认它用的是不是这个数据目录（{data}）：如果是，不要再启动第二个，两个一起用同一个数据目录会出问题；'
                        '如果不是，可以设置环境变量 BUSTAG_PORT 换一个端口再启动，例如 8001')
    OCCUPIED = [
        (('bustag', '0.4.0', True), True,
         '端口 8000 被另一个 bustag（v0.4.0）占着，它用的是别的数据目录，页面在 http://127.0.0.1:8000。'
         '要同时运行这一个（数据目录 {data}），请设置环境变量 BUSTAG_PORT 换一个端口再启动，例如 8001'),
        (('bustag', '0.3.1', False), True, SAME_DIR_UNKNOWN),
        # 版本号一样新、但没有说自己锁着数据目录的：不能当它用的是别的数据目录
        (('bustag', '0.4.0', False), True, SAME_DIR_UNKNOWN),
        (('bustag', '9.0.0', False), True, SAME_DIR_UNKNOWN),
        (('bustag', '0.4.0', True), False, SAME_DIR_UNKNOWN),
        (('other', None, False), True,
         '端口 8000 被别的程序占着（不是 bustag）。请设置环境变量 BUSTAG_PORT 换一个端口再启动，例如 8001'),
    ]
    OCCUPIED_IDS = ['another-bustag', 'older-bustag', 'same-version-without-the-mark', 'newer-version-without-the-mark',
                    'bustag-but-no-lock-here', 'another-program']

    @pytest.mark.parametrize('who, locked, message', OCCUPIED + [
        ((None, None, False), True,
         '无法监听 0.0.0.0:8000（[Errno 10048] 端口已被占用）。请检查监听地址是不是这台电脑的地址、这个端口能不能用'),
    ], ids=OCCUPIED_IDS + ['nobody-listening'])
    def test_occupied_port_is_explained_by_who_has_it(self, fake_socket, monkeypatch, who, locked, message):
        FakeProbe.occupied = {('0.0.0.0', 8000)}
        asked = []
        monkeypatch.setattr(index, '_who_listens', lambda host, port: asked.append((host, port)) or who)
        with pytest.raises(index.StartupError) as info:
            index._ensure_port_free('0.0.0.0', 8000, locked)
        assert str(info.value) == message.format(data=util.get_data_dir(), version=who[1])
        assert asked == [('0.0.0.0', 8000)]
        assert FakeProbe.created[0].closed

    @pytest.mark.parametrize('who, locked, message', OCCUPIED, ids=OCCUPIED_IDS)
    def test_a_port_that_answers_is_taken_even_when_the_exclusive_bind_succeeds(self, fake_socket, monkeypatch, who,
                                                                                locked, message):
        # 对方监听所有地址、这边要监听 127.0.0.1 时，Windows 上独占试绑是成功的；对所有地址试绑看得到它，再去问它是谁
        FakeProbe.occupied = {self.EVERYWHERE}
        asked = []
        monkeypatch.setattr(index, '_who_listens', lambda host, port: asked.append((host, port)) or who)
        with pytest.raises(index.StartupError) as info:
            index._ensure_port_free('127.0.0.1', 8000, locked)
        assert str(info.value) == message.format(data=util.get_data_dir(), version=who[1])
        assert asked == [('127.0.0.1', 8000)]
        assert FakeProbe.created[0].bound == ('127.0.0.1', 8000)

    def test_without_exclusive_bind_the_port_is_still_asked_whether_anybody_answers(self, fake_socket, monkeypatch):
        del fake_socket.SO_EXCLUSIVEADDRUSE
        index._ensure_port_free('0.0.0.0', 8000, True)
        assert fake_socket.lookups == []
        assert FakeProbe.created == []
        assert fake_socket.connections == [(('127.0.0.1', 8000), index.CONNECT_TIMEOUT)]
        monkeypatch.setattr(index, '_who_listens', lambda host, port: ('other', None, False))
        with pytest.raises(index.StartupError, match='端口 8000 被别的程序占着（不是 bustag）'):
            index._ensure_port_free('0.0.0.0', 8000, True)

    def test_sigterm_exits_cleanly(self, monkeypatch):
        handlers = {}
        fake_signal = types.SimpleNamespace(SIGTERM=15, signal=lambda sig, handler: handlers.update({sig: handler}))
        monkeypatch.setattr(index, 'signal', fake_signal)
        index._exit_on_sigterm()
        with pytest.raises(SystemExit) as info:
            handlers[15](15, None)
        assert info.value.code == 0

    @pytest.mark.parametrize('outgoing, own, expected', [
        ('192.168.1.5', {'192.168.1.5'}, '192.168.1.5'),
        ('10.0.0.8', {'10.0.0.8', '169.254.3.4', '127.0.0.1'}, '10.0.0.8'),
        ('198.18.0.1', {'192.168.1.5', '198.18.0.1'}, None),
        ('172.19.0.1', {'192.168.1.5', '172.19.0.1'}, None),
        ('10.8.0.2', {'192.168.1.5', '10.8.0.2'}, None),
        ('192.168.1.5', {'192.168.1.5', '192.168.56.1', '172.28.0.1'}, None),
        ('203.0.113.7', {'203.0.113.7'}, None),
        (None, {'192.168.1.5'}, None),
        ('192.168.1.5', set(), None),
        ('192.168.1.5', {'127.0.1.1'}, None),
    ], ids=['one-lan-address', 'link-local-and-loopback-do-not-count', 'proxy-adapter-is-the-way-out',
            'proxy-adapter-with-a-lan-like-address', 'vpn', 'virtual-machine-adapters', 'public-address', 'no-network',
            'own-addresses-unknown', 'host-name-points-at-loopback'])
    def test_the_lan_address_is_only_told_when_there_is_no_doubt(self, monkeypatch, outgoing, own, expected):
        monkeypatch.setattr(index, '_outgoing_address', lambda: outgoing)
        monkeypatch.setattr(index, '_own_addresses', lambda: own)
        assert index._lan_address() == expected

    @pytest.mark.parametrize('found, expected', [
        ([(2, 1, 6, '', ('192.168.1.5', 0)), (2, 1, 6, '', ('198.18.0.1', 0)), (2, 2, 17, '', ('192.168.1.5', 0))],
         {'192.168.1.5', '198.18.0.1'}),
        (OSError(11001, 'getaddrinfo failed'), set()),
    ], ids=['every-adapter', 'lookup-failed'])
    def test_own_addresses_are_those_of_the_host_name(self, monkeypatch, found, expected):
        asked = []

        def getaddrinfo(host, port, family=0):
            asked.append((host, port, family))
            if isinstance(found, OSError):
                raise found
            return found

        monkeypatch.setattr(index, 'ON_WINDOWS', True)
        monkeypatch.setattr(index, 'socket', types.SimpleNamespace(getaddrinfo=getaddrinfo, AF_INET=2,
                                                                   gethostname=lambda: 'this-pc'))
        assert index._own_addresses() == expected
        assert asked == [('this-pc', None, 2)]

    def test_own_addresses_are_not_looked_up_where_the_host_name_may_need_the_network(self, monkeypatch):
        # Windows 以外，主机名常常只对应 127.0.1.1，查它还可能要去问 DNS；conftest 的 socket 拦截会让真的查询判失败
        monkeypatch.setattr(index, 'ON_WINDOWS', False)
        assert index._own_addresses() == set()

    @pytest.mark.parametrize('address, expected', [
        ('192.168.1.5', '192.168.1.5'),
        ('127.0.0.1', None),
        ('0.0.0.0', None),
        (OSError(10051, '网络不可达'), None),
    ], ids=['lan', 'loopback', 'unspecified', 'no-network'])
    def test_outgoing_address_is_the_one_the_system_would_send_from(self, monkeypatch, address, expected):
        sockets = []

        class Udp:
            '''代替 UDP socket：connect 只是让系统选出口，这里记下连的是哪里'''

            def __init__(self, family, kind):
                self.created = (family, kind)
                self.closed = False
                sockets.append(self)

            def connect(self, target):
                self.target = target
                if isinstance(address, OSError):
                    raise address

            def getsockname(self):
                return address, 54321

            def close(self):
                self.closed = True

        monkeypatch.setattr(index, 'socket', types.SimpleNamespace(socket=Udp, AF_INET=2, SOCK_DGRAM=2))
        assert index._outgoing_address() == expected
        probe, = sockets
        # UDP、保留给文档用的地址：不会真的发出任何数据
        assert (probe.created, probe.target, probe.closed) == ((2, 2), ('192.0.2.1', 9), True)

    @pytest.mark.parametrize('marker, inside', [('/.dockerenv', True), ('/run/.containerenv', True), (None, False)])
    def test_a_container_is_known_by_its_marker_file(self, monkeypatch, marker, inside):
        monkeypatch.setattr(index.os.path, 'exists', lambda path: path == marker)
        assert index._in_container() is inside

    @pytest.fixture
    def no_serve(self, monkeypatch, fake_socket):
        '''记录 create_server / create_app 的调用，不真的启动'''
        calls = Calls()

        def create_server(app, host, port, threads, **options):
            calls.append(('create_server', host, port, threads))
            calls.served = app
            calls.server_options = options
            return types.SimpleNamespace(run=lambda: calls.append(('run',)))

        def lock_data_dir(url=None):
            calls.append(('lock', url))
            return True

        monkeypatch.setattr(index, 'create_server', create_server)
        monkeypatch.setattr(index, 'create_app', lambda: calls.append(('create_app',)))
        monkeypatch.setattr(index.instance, 'lock_data_dir', lock_data_dir)
        monkeypatch.setattr(index, 'start_file_log', lambda: calls.append(('log',)))
        monkeypatch.setattr(index, 'log_uncaught_exceptions', lambda: None)
        monkeypatch.setattr(index, '_exit_on_sigterm', lambda: calls.append(('sigterm',)))
        monkeypatch.setattr(index, '_ensure_port_free', lambda host, port, locked: calls.append(('probe', host, port)))
        monkeypatch.setattr(index, '_who_listens', lambda host, port: (None, None, False))
        monkeypatch.setattr(index, '_lan_address', lambda: '192.168.1.5')
        monkeypatch.setattr(index, '_in_container', lambda: False)
        monkeypatch.delenv('BUSTAG_HOST', raising=False)
        monkeypatch.delenv('BUSTAG_PORT', raising=False)
        return calls

    def test_main_serves_and_starts_scheduler_after_binding(self, no_serve, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        index.main()
        # 先给数据目录上锁（锁文件里写着访问地址），拿到了才开日志文件、占端口、启动定时任务
        assert no_serve == [('lock', 'http://127.0.0.1:8000'), ('log',), ('probe', '127.0.0.1', 8000),
                            ('create_server', '127.0.0.1', 8000, index.SERVER_THREADS), ('create_app',), ('sigterm',),
                            ('run',)]
        assert no_serve.served is index.application
        # 日志文件开了以后才写"启动"，这一行在文件里
        assert caplog.messages[0] == f'bustag v{bustag.__version__} 启动，数据目录 {util.get_data_dir()}'
        assert caplog.messages[1:3] == index._access_notes('127.0.0.1', 8000)
        assert caplog.messages[1] == '浏览器访问 http://127.0.0.1:8000'

    def test_main_listens_where_the_config_says(self, no_serve, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        monkeypatch.setitem(util.APP_CONFIG, 'server.host', '0.0.0.0')
        index.main()
        assert ('create_server', '0.0.0.0', 8000, index.SERVER_THREADS) in no_serve
        assert no_serve[0] == ('lock', 'http://127.0.0.1:8000')
        assert caplog.messages[2].startswith('正在监听所有网络地址：同一局域网里的设备可以用 http://192.168.1.5:8000 访问。')

    @pytest.mark.parametrize('pid, url, empty, message', [
        (4242, 'http://127.0.0.1:8123', False,
         'bustag 已经在运行（进程号 4242，数据目录 {data}），不用再启动一个：在浏览器里打开 http://127.0.0.1:8123 就行'),
        (4242, None, False, 'bustag 已经在运行（进程号 4242，数据目录 {data}），不用再启动一个'),
        (None, None, False, 'bustag 已经在运行（数据目录 {data}），不用再启动一个'),
        # 拿着锁的那个正在启动（还没写）或者刚没启动起来（已经清空）：不说"已经在运行"，那样却给不出进程号和地址
        (None, None, True, '另一个 bustag 正在启动或退出（数据目录 {data}），稍后再试'),
    ], ids=['address-known', 'address-unknown', 'nothing-known', 'lock-file-empty'])
    def test_a_second_service_on_the_same_data_dir_does_not_start(self, no_serve, monkeypatch, caplog, pid, url,
                                                                 empty, message):
        def lock_data_dir(address=None):
            raise index.instance.AlreadyRunning(pid, url, empty)

        waited = []
        clock = self.FakeClock()
        monkeypatch.setattr(index, 'time', clock)
        monkeypatch.setattr(index.instance, 'lock_data_dir', lock_data_dir)
        monkeypatch.setattr(index, 'wait_for_enter_if_console', lambda: waited.append('wait'))
        with pytest.raises(SystemExit) as info:
            index.main()
        assert info.value.code == 1
        # 双击了两次 exe 的人马上看到提示，不等
        assert clock.sleeps == []
        # 日志文件是正在运行的那一个在写，这一个不去碰；端口、定时任务也都没动
        assert no_serve == []
        assert schedule.scheduler is None
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [message.format(data=util.get_data_dir())]
        # 双击 exe 时窗口等回车再关（wait_for_enter_if_console 自己判断是不是这种情况）
        assert waited == ['wait']

    def test_create_app_locks_the_data_dir_first_then_starts_the_log_file_and_the_scheduler(self, monkeypatch):
        calls = []
        monkeypatch.setattr(index.instance, 'lock_data_dir', lambda url=None: calls.append(('lock', url)) or True)
        monkeypatch.setattr(index, 'start_file_log', lambda: calls.append('log'))
        monkeypatch.setattr(index, 'log_uncaught_exceptions', lambda: calls.append('hooks'))
        monkeypatch.setattr(index, 'remove_stale_uploads', lambda: calls.append('uploads'))
        monkeypatch.setattr(index, 'start_scheduler', lambda: calls.append('scheduler'))
        assert index.create_app() is index.application
        # gunicorn 从这里进来，监听地址是 gunicorn 管的，锁文件里只写进程号
        assert calls == [('lock', None), 'log', 'hooks', 'uploads', 'scheduler']

    def test_tables_not_added_at_startup_are_added_in_the_background_before_the_scheduler_starts(
            self, monkeypatch, caplog):
        # 启动时库被别的程序占着、新加的表没建成：网页服务照常开始，后台隔一会儿再建；建好了才启动定时任务
        # （迁移、补演员名、按演员补全都要用这些表）
        caplog.set_level(logging.INFO, logger='bustag')
        attempts, started = [], threading.Event()
        monkeypatch.setattr(db, 'tables_missing', True)
        monkeypatch.setattr(db, 'create_tables', lambda: attempts.append('try') or len(attempts) >= 3)
        monkeypatch.setattr(index, 'TABLES_RETRY', 0.01)
        monkeypatch.setattr(index.instance, 'lock_data_dir', lambda url=None: True)
        monkeypatch.setattr(index, 'start_file_log', lambda: None)
        monkeypatch.setattr(index, 'log_uncaught_exceptions', lambda: None)
        monkeypatch.setattr(index, 'remove_stale_uploads', lambda: None)
        monkeypatch.setattr(index, 'start_scheduler', started.set)
        assert index.create_app() is index.application
        assert started.wait(10)
        assert attempts == ['try'] * 3
        assert caplog.messages.count(
            f'{db.TABLES_MISSING}：程序照常启动，网页服务在后台接着加；加好以前，用到这几张表的页面会说明原因') == 1
        assert '数据库里新加的表已经建好，定时任务开始' in caplog.messages

    class FakeClock:
        '''代替 index 里的 time：sleep 不真的等，只把时钟往前拨'''

        def __init__(self):
            self.now = 1000.0
            self.sleeps = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    def test_create_app_refuses_a_data_dir_that_another_service_keeps_using(self, monkeypatch, caplog):
        def lock_data_dir(url=None):
            raise index.instance.AlreadyRunning(4242, 'http://127.0.0.1:8123')

        caplog.set_level(logging.INFO, logger='bustag')
        started = []
        clock = self.FakeClock()
        monkeypatch.setattr(index, 'time', clock)
        monkeypatch.setattr(index.instance, 'lock_data_dir', lock_data_dir)
        monkeypatch.setattr(index, 'start_file_log', lambda: started.append('log'))
        monkeypatch.setattr(index, 'start_scheduler', lambda: started.append('scheduler'))
        with pytest.raises(index.StartupError, match='bustag 已经在运行（进程号 4242，'):
            index.create_app()
        assert started == []
        # 等够了时间才放弃（gunicorn 重新加载时旧的 worker 要一会儿才退出），等的时候只说一次
        assert sum(clock.sleeps) == index.LOCK_WAIT
        assert set(clock.sleeps) == {index.LOCK_RETRY_STEP}
        assert caplog.messages == [f'数据目录 {util.get_data_dir()} 正被另一个 bustag 进程（进程号 4242）占用，'
                                   f'等它退出，最多等 {index.LOCK_WAIT} 秒']

    def test_create_app_does_not_call_a_starting_or_leaving_service_running(self, monkeypatch, caplog):
        def lock_data_dir(url=None):
            raise index.instance.AlreadyRunning(None, None, True)

        caplog.set_level(logging.INFO, logger='bustag')
        clock = self.FakeClock()
        monkeypatch.setattr(index, 'time', clock)
        monkeypatch.setattr(index.instance, 'lock_data_dir', lock_data_dir)
        with pytest.raises(index.StartupError) as info:
            index.create_app()
        assert str(info.value) == f'另一个 bustag 正在启动或退出（数据目录 {util.get_data_dir()}），稍后再试'
        assert sum(clock.sleeps) == index.LOCK_WAIT
        assert caplog.messages == [f'数据目录 {util.get_data_dir()} 正被另一个 bustag 进程占用，'
                                   f'等它退出，最多等 {index.LOCK_WAIT} 秒']

    def test_create_app_starts_once_the_other_service_has_left(self, monkeypatch):
        attempts = []

        def lock_data_dir(url=None):
            attempts.append(url)
            if len(attempts) < 3:
                raise index.instance.AlreadyRunning(4242, None)
            return True

        started = []
        clock = self.FakeClock()
        monkeypatch.setattr(index, 'time', clock)
        monkeypatch.setattr(index.instance, 'lock_data_dir', lock_data_dir)
        monkeypatch.setattr(index, 'start_file_log', lambda: started.append('log'))
        monkeypatch.setattr(index, 'log_uncaught_exceptions', lambda: None)
        monkeypatch.setattr(index, 'remove_stale_uploads', lambda: None)
        monkeypatch.setattr(index, 'start_scheduler', lambda: started.append('scheduler'))
        assert index.create_app() is index.application
        assert attempts == [None, None, None]
        assert clock.sleeps == [index.LOCK_RETRY_STEP] * 2
        assert started == ['log', 'scheduler']

    def test_the_server_keeps_reading_a_connection_so_it_notices_when_the_browser_leaves(self, no_serve):
        index.main()
        assert no_serve.server_options == {'channel_request_lookahead': 1}

    def test_main_exits_on_invalid_port(self, no_serve, monkeypatch, caplog):
        monkeypatch.setenv('BUSTAG_PORT', 'abc')
        with pytest.raises(SystemExit) as info:
            index.main()
        assert info.value.code == 1
        assert no_serve == []
        assert any('BUSTAG_PORT=abc' in m for m in caplog.messages)

    @pytest.fixture
    def exe(self, monkeypatch, no_serve, tmp_path):
        '''模拟打包后的 bustag.exe：返回带着命令行参数运行 index.main() 的函数'''
        executable = str(tmp_path / 'bustag' / 'bustag.exe')
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
        monkeypatch.setattr(sys, 'executable', executable)

        def run(*args):
            monkeypatch.setattr(sys, 'argv', [executable, *args])
            index.main()
        return run

    def test_exe_without_arguments_serves(self, exe, no_serve):
        exe()
        assert ('run',) in no_serve

    def test_exe_runs_check(self, exe, no_serve, fake_fetcher, monkeypatch, capsys):
        monkeypatch.setattr(bustag.main, 'create_fetcher', lambda **overrides: fake_fetcher({'/': FetchError('连接被拒绝')}))
        with pytest.raises(SystemExit) as info:
            exe('check')
        assert info.value.code == 1
        lines = capsys.readouterr().out.splitlines()
        assert [line for line in lines if line.startswith('==')] == ['== 配置', '== 网站', '== 数据']
        assert any(line.startswith('❌ 连通：') for line in lines)
        assert no_serve == []

    def test_exe_runs_recommend(self, exe, no_serve, capsys):
        with pytest.raises(SystemExit) as info:
            exe('recommend')
        assert info.value.code == 1
        assert '还没有训练好的模型, 无法推荐' in capsys.readouterr().out
        assert no_serve == []

    def test_exe_help_lists_the_commands(self, exe, no_serve, capsys):
        with pytest.raises(SystemExit) as info:
            exe('--help')
        assert info.value.code == 0
        out = capsys.readouterr().out
        assert out.startswith('Usage: bustag.exe ')
        for command in ('check', 'download', 'recommend'):
            assert re.search(rf'^\s+{command}\s', out, re.MULTILINE)
        assert no_serve == []

    def test_exe_with_unknown_command_shows_usage(self, exe, no_serve, capsys):
        with pytest.raises(SystemExit) as info:
            exe('chek')
        assert info.value.code == 2
        assert "No such command 'chek'" in capsys.readouterr().err
        assert no_serve == []

    def test_arguments_are_ignored_when_running_from_source(self, no_serve, monkeypatch):
        monkeypatch.setattr(sys, 'frozen', False, raising=False)
        monkeypatch.setattr(sys, 'argv', ['bustag/app/index.py', 'check'])
        index.main()
        assert ('run',) in no_serve

    @pytest.fixture
    def waiting(self, no_serve, monkeypatch):
        '''放掉数据目录的锁、窗口等回车这两步也记进 no_serve'''
        monkeypatch.setattr(index.instance, 'release', lambda: no_serve.append(('release',)))
        monkeypatch.setattr(index, 'wait_for_enter_if_console', lambda: no_serve.append(('wait',)))

    def test_main_exits_when_port_cannot_be_bound(self, no_serve, waiting, monkeypatch, caplog):
        def create_server(app, host, port, threads, **options):
            raise OSError(98, 'Address already in use')

        monkeypatch.setattr(index, 'create_server', create_server)
        monkeypatch.setattr(index, '_who_listens', lambda host, port: ('other', None, False))
        with pytest.raises(SystemExit) as info:
            index.main()
        assert info.value.code == 1
        assert ('create_app',) not in no_serve
        assert schedule.scheduler is None
        assert '端口 8000 被别的程序占着（不是 bustag）。请设置环境变量 BUSTAG_PORT 换一个端口再启动，例如 8001' in caplog.messages
        # 窗口等回车之前先把数据目录的锁放掉：照提示换个端口再启动的那一个不会被它挡住
        assert no_serve[-2:] == [('release',), ('wait',)]

    def test_main_lets_go_of_the_data_dir_when_the_port_is_answered_by_somebody_else(self, no_serve, waiting,
                                                                                     monkeypatch):
        def taken(host, port, locked):
            no_serve.append(('probe', host, port))
            raise index.StartupError('端口 8000 被别的程序占着')

        monkeypatch.setattr(index, '_ensure_port_free', taken)
        with pytest.raises(SystemExit):
            index.main()
        assert no_serve == [('lock', 'http://127.0.0.1:8000'), ('log',), ('probe', '127.0.0.1', 8000), ('release',),
                            ('wait',)]

    def test_main_keeps_the_data_dir_when_it_breaks_after_the_background_jobs_started(self, no_serve, waiting,
                                                                                      monkeypatch):
        def broken():
            raise RuntimeError('boom')

        # 定时任务已经在跑，它们还在用这个数据目录，窗口等回车的时候锁不能放
        monkeypatch.setattr(index, '_exit_on_sigterm', broken)
        with pytest.raises(SystemExit):
            index.main()
        assert ('create_app',) in no_serve
        assert ('release',) not in no_serve
        assert no_serve[-1] == ('wait',)


class FakeConnection:
    '''代替 http.client.HTTPConnection：记下问的是谁，回预置的内容和响应头（或抛出预置的异常）'''
    asked = []
    reply = b''
    headers = {}

    def __init__(self, host, port, timeout=None):
        self.address = (host, port, timeout)

    def request(self, method, path):
        FakeConnection.asked.append((self.address, method, path))
        if isinstance(self.reply, BaseException):
            raise self.reply

    def getresponse(self):
        return types.SimpleNamespace(read=lambda size: self.reply[:size], getheader=self.headers.get)

    def close(self):
        pass


class TestWhoListens:
    '''端口被占时看一眼占着它的是不是 bustag：连一下，再要一次"关于"页，看页脚'''

    @pytest.fixture
    def port(self, monkeypatch):
        '''端口上有程序在听；连接和要页面都换成假的，返回记录连接的列表'''
        connected = []

        def create_connection(address, timeout=None):
            connected.append((address, timeout))
            return types.SimpleNamespace(close=lambda: None)

        monkeypatch.setattr(index, 'socket', types.SimpleNamespace(create_connection=create_connection))
        monkeypatch.setattr(index, '_is_this_computer', lambda host: True)
        monkeypatch.setattr(index.http.client, 'HTTPConnection', FakeConnection)
        monkeypatch.setattr(FakeConnection, 'asked', [])
        monkeypatch.setattr(FakeConnection, 'headers', {})
        return connected

    def test_a_bustag_is_known_by_the_footer_of_its_about_page(self, port, wsgi, monkeypatch):
        monkeypatch.setattr(FakeConnection, 'reply', wsgi.get('/about').body)
        assert index._who_listens('0.0.0.0', 8000) == ('bustag', bustag.__version__, False)
        # 监听所有地址时问本机地址；不经过代理
        assert port == [(('127.0.0.1', 8000), index.CONNECT_TIMEOUT)]
        assert FakeConnection.asked == [(('127.0.0.1', 8000, index.PROBE_TIMEOUT), 'GET', '/about')]

    def test_an_older_bustag_is_known_too(self, port, monkeypatch):
        page = '<p class="text-center text-muted small">\n  bustag v0.3.1 · 维护版 <a href="#">x</a></p>'
        monkeypatch.setattr(FakeConnection, 'reply', page.encode('utf-8'))
        assert index._who_listens('192.168.1.5', 8000) == ('bustag', '0.3.1', False)
        assert port == [(('192.168.1.5', 8000), index.CONNECT_TIMEOUT)]

    @pytest.mark.parametrize('headers, locks', [
        ({'X-Bustag-Data-Lock': '1'}, True), ({'X-Bustag-Data-Lock': '0'}, False), ({'X-Bustag-Data-Lock': ''}, False),
        ({}, False),
    ], ids=['marked', 'zero', 'empty', 'no-header'])
    def test_a_bustag_that_holds_the_lock_of_its_data_dir_says_so_in_a_header(self, port, wsgi, monkeypatch, headers,
                                                                              locks):
        monkeypatch.setattr(FakeConnection, 'reply', wsgi.get('/about').body)
        monkeypatch.setattr(FakeConnection, 'headers', headers)
        assert index._who_listens('127.0.0.1', 8000) == ('bustag', bustag.__version__, locks)

    @pytest.mark.parametrize('path', ['/about', '/static/css/bustag.css', '/no-such-page'])
    def test_the_header_is_on_every_answer_while_the_lock_is_held(self, served, monkeypatch, path):
        assert 'x-bustag-data-lock' not in served.get(path).headers
        monkeypatch.setattr(index.instance, '_held', object())
        assert served.get(path).headers['x-bustag-data-lock'] == '1'

    @pytest.mark.parametrize('reply', [
        '<html><title>路由器管理</title></html>'.encode('utf-8'),
        b'',
        ConnectionResetError(10054, 'reset'),
        TimeoutError('timed out'),
        index.http.client.BadStatusLine('not http'),
    ], ids=['another-site', 'empty', 'reset', 'silent', 'not-http'])
    def test_anything_else_that_listens_is_another_program(self, port, monkeypatch, reply):
        monkeypatch.setattr(FakeConnection, 'reply', reply)
        monkeypatch.setattr(FakeConnection, 'headers', {'X-Bustag-Data-Lock': '1'})
        assert index._who_listens('127.0.0.1', 8000) == ('other', None, False)

    def test_nobody_listens(self, monkeypatch):
        def create_connection(address, timeout=None):
            raise ConnectionRefusedError(10061, '连接被拒绝')

        monkeypatch.setattr(index, 'socket', types.SimpleNamespace(create_connection=create_connection))
        assert index._who_listens('0.0.0.0', 8000) == (None, None, False)

    def test_an_address_that_is_not_this_computers_is_neither_connected_to_nor_asked_for_a_page(self, port, monkeypatch):
        monkeypatch.setattr(index, '_is_this_computer', lambda host: False)
        assert index._who_listens('192.0.2.1', 8000) == (None, None, False)
        assert port == [] and FakeConnection.asked == []


class TestThisComputer:
    '''监听地址是不是这台电脑自己的：只有这样的地址才去连它，看那个端口上是谁'''

    @pytest.mark.parametrize('host', ['127.0.0.1', '127.0.0.2', 'localhost', 'LOCALHOST', '::1', '[::1]'])
    def test_loopback_addresses_are_this_computers_without_asking_the_system(self, monkeypatch, host):
        monkeypatch.setattr(index, 'socket', types.SimpleNamespace())
        assert index._is_this_computer(host) is True

    def test_the_wildcard_address_can_be_bound_here(self):
        assert index._is_this_computer('0.0.0.0') is True

    def test_the_address_kept_for_documents_is_on_no_computer(self):
        # 192.0.2.1 保留给文档用，任何一台电脑上都没有它：绑不上
        assert index._is_this_computer('192.0.2.1') is False
        assert index._is_this_computer('2001:db8::1') is False

    def test_an_address_of_a_kind_this_computer_has_none_of_is_not_its_own(self, monkeypatch):
        # 系统里关掉了 IPv6 时，连 IPv6 的套接字都建不了：这样的地址不是这台电脑的，启动不能因此出错
        def refuse(*args):
            raise OSError(errno.EAFNOSUPPORT, 'Address family not supported by protocol')

        monkeypatch.setattr(index, 'socket', types.SimpleNamespace(AF_INET=2, AF_INET6=10, SOCK_STREAM=1, socket=refuse))
        assert index._is_this_computer('2001:db8::1') is False

    @pytest.mark.parametrize('found, expected', [
        ([(2, 1, 6, '', ('192.0.2.1', 0)), (2, 1, 6, '', ('127.0.0.1', 0))], True),
        ([(2, 1, 6, '', ('192.0.2.1', 0))], False),
        (OSError(11001, 'getaddrinfo failed'), False),
    ], ids=['one-of-its-addresses-is-here', 'none-is-here', 'unknown-name'])
    def test_a_name_is_this_computers_when_one_of_its_addresses_is(self, monkeypatch, found, expected):
        def getaddrinfo(host, port, type=0, flags=0):
            if isinstance(found, OSError):
                raise found
            return found

        monkeypatch.setattr(index.socket, 'getaddrinfo', getaddrinfo)
        assert index._is_this_computer('some-pc') is expected
