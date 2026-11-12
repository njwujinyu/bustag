'''
指定推荐页上的"按演员补全作品"：每位已选女优一行（库里几部、JavBus 上有磁力几部、排队 / 已补 / 失败、按钮、进度），
单独取这一块的 /pick/backfill，几个表单，以及关于页的"补全作品"一行。测试里定时任务没有启动，登记抓取只记一笔
'''
import datetime
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from bustag import util
from bustag.app import index, pick, schedule
from bustag.spider import actresses, backfill, db
from bustag.spider.parser import Tag

CROSS_SITE = {'Sec-Fetch-Site': 'cross-site', 'Origin': 'https://evil.example'}
AVWIKI = 'https://av-wiki.net/av-actress/'
NOW = datetime.datetime(2026, 11, 6, 12, 0, 0)


def movie(fanhao, *stars, release='2026-01-01'):
    '''入库一部影片。stars 是 (名字, 链接)：JavBus 的演员页是 /star/<编号>，av-wiki 补的是女优页'''
    meta = {'fanhao': fanhao, 'title': f'示例影片 {fanhao}', 'url': '/' + fanhao, 'release_date': release,
            'cover_img_url': '', 'length': '120'}
    assert db.save(meta, [Tag('genre', '标签一', '/genre/1')] + [Tag('star', name, url) for name, url in stars])


def plain(node):
    return ' '.join(node.get_text(' ', strip=True).split())


def block(text):
    return BeautifulSoup(text, 'html.parser').select_one('#backfill')


def rows(text):
    '''这一块里的每一行：{第一个名字: 这一行的文字}'''
    found = block(text)
    return {} if found is None else {row['data-name']: plain(row) for row in found.select('li.backfill-row')}


def buttons(text, name):
    '''这一行里的按钮：[(表单地址, 按钮文字)]'''
    row = block(text).select_one(f'li.backfill-row[data-name="{name}"]')
    return [(button.find_parent('form')['action'], plain(button)) for button in row.select('button')]


def follow(wsgi, response):
    '''表单提交后跳回指定推荐页的这一块（#backfill），带着一次性的提示'''
    assert response.status == 303
    parts = urlsplit(response.headers['location'])
    assert (parts.path, set(parse_qs(parts.query)), parts.fragment) == ('/pick', {'note'}, 'backfill')
    return wsgi.get(f'{parts.path}?{parts.query}')


def alerts(text):
    return [plain(box) for box in BeautifulSoup(text, 'html.parser').select('div.alert')]


@pytest.fixture
def runs(monkeypatch):
    calls = []
    monkeypatch.setattr(index, 'request_queue_run', lambda: calls.append('run'))
    return calls


@pytest.fixture
def two(today):
    '''演员甲（/star/a1，库里 3 部）和演员乙（/star/b2，库里 1 部），都已经选进指定推荐'''
    movie('A-001', ('演员甲', '/star/a1'))
    movie('A-002', ('演员甲', '/star/a1'))
    movie('A-003', ('演员甲', '/star/a1'), ('演员乙', '/star/b2'))
    pick.add_names('star', ['演员甲', '演员乙'])


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(backfill, 'get_now_time', lambda: NOW)
    monkeypatch.setattr(backfill, 'activity', None)
    monkeypatch.setattr(schedule, 'queue_state', lambda: (None, None))


def queue(path, *statuses, page=1):
    '''给演员页 path 排几部：statuses 依次是每一部的状态'''
    source, _ = db.ListSource.get_or_create(path=path)
    for n, status in enumerate(statuses, 1):
        db.FetchTask.create(path=f'/Q{source.id}-{n:03d}', fanhao=f'Q{source.id}-{n:03d}', origin=db.ORIGIN_LIST,
                            source=source, page=page, status=status, detail='缺少发行日期' if status == 'failed' else '',
                            tried_at=NOW if status != 'queued' else None)
    return source


class TestBlock:
    def test_there_is_no_block_without_chosen_actresses(self, wsgi, today):
        movie('A-001', ('演员甲', '/star/a1'))
        pick.add_names('tag', ['标签一'])
        assert block(wsgi.get('/pick').text) is None

    def test_one_row_per_actress_with_what_the_library_has(self, wsgi, two):
        text = wsgi.get('/pick').text
        found = rows(text)
        assert list(found) == ['演员甲', '演员乙']
        assert found['演员甲'].startswith('演员甲 库里 3 部')
        assert '还没有查看她在 JavBus 上有多少部' in found['演员甲']
        assert buttons(text, '演员甲') == [('/pick/backfill/check', '查看'), ('/pick/backfill/start', '补全最新 60 部')]
        assert found['演员乙'].startswith('演员乙 库里 1 部')

    def test_the_block_says_how_it_works_and_how_much_is_left_today(self, wsgi, two, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '300')
        queue('/star/a1', 'done', 'done', 'queued')
        text = plain(block(wsgi.get('/pick').text))
        assert '点了才抓：每次每位最多 60 部，所有演员合计每天最多 300 部' in text
        assert '只补有磁力的' in text
        assert '今天已抓 2 部，还可以抓 298 部；现在排队 1 部。' in text

    def test_names_from_one_actress_page_share_a_row(self, wsgi, today):
        movie('L-001', ('演员丙', f'{AVWIKI}bing/'), ('演员丙读音', f'{AVWIKI}bing/'))
        movie('L-002', ('演员丙读音', f'{AVWIKI}bing/'))
        pick.add_names('star', ['演员丙', '演员丙读音'])
        found = rows(wsgi.get('/pick').text)
        assert list(found) == ['演员丙']
        assert found['演员丙'].startswith('演员丙、演员丙读音 库里 2 部')

    def test_names_on_the_same_javbus_page_share_a_row(self, wsgi, today):
        # 网站给演员改过名字：两个名字的链接是同一个演员页
        movie('A-001', ('旧名甲', '/star/a1'))
        movie('A-002', ('演员甲', '/star/a1'))
        pick.add_names('star', ['演员甲', '旧名甲'])
        found = rows(wsgi.get('/pick').text)
        assert list(found) == ['演员甲']
        assert found['演员甲'].startswith('演员甲、旧名甲 库里 2 部')

    def test_any_of_her_names_gives_her_row_with_her_name(self, wsgi, today, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', 'Actor A', javbus='/star/a1')
        cast.movie('C-001')
        cast.link('C-001', jia)
        movie('C-002', ('別の名前', '/star/a1'))
        pick.add_names('star', ['えんいんこう', 'Actor A'])
        text = wsgi.get('/pick').text
        found = rows(text)
        assert list(found) == ['えんいんこう']
        assert found['えんいんこう'].startswith('えんいんこう、Actor A（演员甲） 库里 2 部')
        assert '关联的 JavBus 演员页：別の名前（/star/a1）' in found['えんいんこう']
        assert buttons(text, 'えんいんこう') == [('/pick/backfill/check', '查看'),
                                                ('/pick/backfill/start', '补全最新 60 部'),
                                                ('/pick/backfill/relink', '换一个'), ('/pick/backfill/unlink', '取消关联')]

    def test_a_name_two_actresses_share_gives_each_her_own_row(self, wsgi, today, cast, runs):
        jia = cast.actress('演员甲', 'jia', '共用读音', javbus='/star/a1')
        yi = cast.actress('演员乙', 'yi', '共用读音', javbus='/star/b2')
        for code, actress in (('C-001', jia), ('C-002', yi), ('C-003', yi)):
            cast.movie(code)
            cast.link(code, actress)
        pick.add_names('star', ['共用读音'])
        found = block(wsgi.get('/pick').text).select('li.backfill-row')
        assert [(row['data-name'], row['data-row']) for row in found] == [
            ('共用读音', f'actress:{jia.id}'), ('共用读音', f'actress:{yi.id}')]
        assert [plain(row).split(' 库里 ')[0] for row in found] == ['共用读音（演员甲）', '共用读音（演员乙）']
        for row in found:
            assert {field['value'] for field in row.select('input[name="row"]')} == {row['data-row']}
        # 第二行的「查看」查看的是演员乙的演员页
        response = wsgi.post('/pick/backfill/check', {'name': '共用读音', 'row': f'actress:{yi.id}'})
        assert alerts(follow(wsgi, response).text) == ['已登记查看「共用读音」的演员页：只读她的第 1 页，稍等几秒这里会显示结果']
        assert [source.path for source in db.ListSource.select()] == ['/star/b2']
        # 不在这几行里的键：当作这位女优已经不在设定里了
        response = wsgi.post('/pick/backfill/check', {'name': '共用读音', 'row': 'actress:999'})
        assert alerts(follow(wsgi, response).text) == ['「共用读音」不在指定推荐的女优里了，没有改动']

    def test_an_actress_without_a_javbus_page(self, wsgi, today):
        movie('L-001', ('演员丙', f'{AVWIKI}bing/'))
        pick.add_names('star', ['演员丙', '还没有的演员'])
        text = wsgi.get('/pick').text
        found = rows(text)
        assert found['演员丙'].startswith('演员丙 库里 1 部')
        assert '没有她的 JavBus 演员页地址' in found['演员丙']
        assert found['还没有的演员'].startswith('还没有的演员 库里 0 部')
        assert ('/pick/backfill/start', '补全最新 60 部') not in buttons(text, '演员丙')

    def test_a_checked_actress_shows_the_numbers_on_her_page(self, wsgi, two):
        db.ListSource.create(path='/star/a1', magnets=95, total=120, checked_at=NOW)
        text = wsgi.get('/pick').text
        found = rows(text)['演员甲']
        assert 'JavBus 上有磁力 95 部（11-06 12:00 查看），另有 25 部没有磁力，未包含' in found
        assert ('/pick/backfill/check', '重新查看') in buttons(text, '演员甲')

    def test_a_checked_actress_whose_page_gave_no_numbers_is_not_called_unchecked(self, wsgi, two):
        db.ListSource.create(path='/star/a1', checked_at=NOW)
        text = wsgi.get('/pick').text
        found = rows(text)['演员甲']
        assert '查看过（11-06 12:00），页面上没有读到有磁力的部数' in found
        assert '还没有查看' not in found
        assert ('/pick/backfill/check', '重新查看') in buttons(text, '演员甲')

    def test_her_queue_and_what_can_be_done_with_it(self, wsgi, two):
        queue('/star/a1', 'queued', 'queued', 'queued', 'done', 'done', 'failed', 'missing')
        text = wsgi.get('/pick').text
        assert '已排队 3 部，已补 2 部，失败 2 部' in rows(text)['演员甲']
        assert buttons(text, '演员甲') == [('/pick/backfill/check', '查看'), ('/pick/backfill/cancel', '取消排队'),
                                           ('/pick/backfill/retry', '重试失败的 1 部')]

    def test_after_a_first_fill_she_can_be_continued(self, wsgi, two):
        db.ListSource.create(path='/star/a1', next_page=3, oldest=datetime.date(2021, 3, 5))
        text = wsgi.get('/pick').text
        assert '往回补到 2021-03-05 发行的' in rows(text)['演员甲']
        assert ('/pick/backfill/start', '继续补全') in buttons(text, '演员甲')

    def test_at_the_end_of_her_list_only_new_movies_are_looked_for(self, wsgi, two):
        db.ListSource.create(path='/star/a1', next_page=5, reached_end=True, oldest=datetime.date(2013, 1, 2))
        text = wsgi.get('/pick').text
        assert '已经补到她最早的一部，再点只找新片' in rows(text)['演员甲']
        assert ('/pick/backfill/start', '继续补全') in buttons(text, '演员甲')

    @pytest.mark.parametrize('state, activity, words', [
        ({'checking': True}, None, '等着读她的第 1 页'),
        ({'wanted': 60}, None, '等着翻她的演员页'),
        ({'wanted': 60}, ('page', 'id', 2), '正在读她的第 2 页'),
        ({}, ('movie', 'id', 'Q1-002'), '正在抓 Q1-002'),
    ])
    def test_what_is_going_on_now(self, wsgi, two, monkeypatch, state, activity, words):
        source = db.ListSource.create(path='/star/a1', **state)
        if activity:
            monkeypatch.setattr(backfill, 'activity', tuple(source.id if part == 'id' else part for part in activity))
        text = wsgi.get('/pick').text
        assert words in rows(text)['演员甲']
        assert block(text)['data-refresh'] == '1'
        assert all(path != '/pick/backfill/start' for path, _ in buttons(text, '演员甲'))

    def test_nothing_going_on_needs_no_refresh(self, wsgi, two):
        queue('/star/a1', 'done')
        assert block(wsgi.get('/pick').text)['data-refresh'] == '0'

    def test_movies_waiting_to_be_fetched_keep_it_refreshing(self, wsgi, two):
        queue('/star/a1', 'queued')
        assert block(wsgi.get('/pick').text)['data-refresh'] == '1'

    def test_movies_waiting_for_tomorrow_do_not(self, wsgi, two, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.backfill_daily', '1')
        queue('/star/a1', 'done', 'queued')
        text = plain(block(wsgi.get('/pick').text))
        assert '今天的额度已经用完，排队的明天 00:01 起接着抓' in text
        assert block(wsgi.get('/pick').text)['data-refresh'] == '0'

    def test_an_error_is_shown_escaped(self, wsgi, two):
        db.ListSource.create(path='/star/a1', error='第 2 页读不到：<b>HTTP 500</b>', error_at=NOW)
        text = wsgi.get('/pick').text
        assert '上次出错（11-06 12:00）：第 2 页读不到：<b>HTTP 500</b>' in rows(text)['演员甲']
        assert '<b>HTTP 500</b>' not in text

    def test_a_pause_is_explained_with_a_way_to_go_on(self, wsgi, two):
        backfill.pause('HTTP 403：访问被拒绝，可能被 Cloudflare 拦截。', now=NOW)
        text = wsgi.get('/pick').text
        assert alerts(text) == ['今天被网站拦截过（11-06 12:00）：HTTP 403：访问被拒绝，可能被 Cloudflare 拦截。'
                                '补全先停下了，今天不再自动接着抓；已经排队的影片明天会自动继续，'
                                '还没做的补全、查看、查找不会再自己去做，要继续请再点一次。 现在继续']
        assert block(text).select_one('#backfill-paused form')['action'] == '/pick/backfill/resume'

    def test_an_actress_whose_page_was_not_read_because_of_the_block_says_so(self, wsgi, two):
        backfill.request_fill('/star/b2')
        backfill.pause('HTTP 403：访问被拒绝', now=NOW)
        text = wsgi.get('/pick').text
        assert ('上次出错（11-06 12:00）：被网站拦截，这次没有翻她的演员页，要继续请再点一次补全'
                in rows(text)['演员乙'])
        assert ('/pick/backfill/start', '补全最新 60 部') in buttons(text, '演员乙')
        assert block(text)['data-refresh'] == '0'

    def test_a_view_that_was_waiting_when_the_block_came_says_so_and_can_be_asked_for_again(self, wsgi, two):
        backfill.request_check('/star/b2')
        backfill.pause('HTTP 403：访问被拒绝', now=NOW)
        text = wsgi.get('/pick').text
        assert ('上次出错（11-06 12:00）：被网站拦截，这次没有读她的演员页，要继续请再点一次查看'
                in rows(text)['演员乙'])
        assert '等着读她的第 1 页' not in rows(text)['演员乙']
        assert ('/pick/backfill/check', '查看') in buttons(text, '演员乙')
        assert block(text)['data-refresh'] == '0'

    def test_a_request_left_waiting_during_a_pause_is_not_going_on(self, wsgi, two):
        backfill.pause('HTTP 403：访问被拒绝', now=NOW)
        db.ListSource.create(path='/star/a1', wanted=60)
        text = wsgi.get('/pick').text
        assert block(text)['data-refresh'] == '0'
        assert '等着翻她的演员页' not in rows(text)['演员甲']

    def test_the_block_can_be_fetched_alone(self, wsgi, two):
        page = wsgi.get('/pick').text
        response = wsgi.get('/pick/backfill')
        assert response.status == 200
        assert response.headers['content-type'] == 'text/html; charset=UTF-8'
        assert response.headers['cache-control'] == 'no-store'
        assert '<html' not in response.text
        assert str(block(response.text)) == str(block(page))

    def test_the_block_alone_without_chosen_actresses_is_empty(self, wsgi, today):
        response = wsgi.get('/pick/backfill')
        assert response.status == 200
        assert block(response.text) is None

    def test_a_hundred_actresses_fit_in_one_scrolling_list(self, wsgi, today):
        names = [f'演员{n:03d}' for n in range(100)]
        for n, name in enumerate(names):
            movie(f'H-{n:03d}', (name, f'/star/h{n}'))
        pick.add_names('star', names)
        text = wsgi.get('/pick').text
        assert len(rows(text)) == 100
        assert 'backfill-rows' in block(text).select_one('ul')['class']

    def test_odd_names_are_escaped(self, wsgi, today):
        name = '<script>alert(1)</script>'
        movie('A-001', (name, '/star/a1'))
        pick.add_names('star', [name])
        text = wsgi.get('/pick').text
        assert '<script>alert(1)</script>' not in text
        assert list(rows(text)) == [name]


class TestForms:
    def test_check(self, wsgi, two, runs):
        response = wsgi.post('/pick/backfill/check', {'name': '演员甲'})
        assert alerts(follow(wsgi, response).text) == ['已登记查看「演员甲」的演员页：只读她的第 1 页，稍等几秒这里会显示结果']
        assert db.ListSource.get(db.ListSource.path == '/star/a1').checking is True
        assert runs == ['run']

    def test_start(self, wsgi, two, runs):
        response = wsgi.post('/pick/backfill/start', {'name': '演员甲'})
        assert alerts(follow(wsgi, response).text) == [
            '已开始补全「演员甲」：最多排进 60 部库里还没有的作品，后台一部一部地抓，这里会显示进度']
        source = db.ListSource.get(db.ListSource.path == '/star/a1')
        assert (source.wanted, source.requested_at) == (60, NOW)
        assert runs == ['run']

    def test_start_is_refused_while_her_movies_are_queued(self, wsgi, two, runs):
        queue('/star/a1', 'queued', 'queued')
        response = wsgi.post('/pick/backfill/start', {'name': '演员甲'})
        assert alerts(follow(wsgi, response).text) == ['「演员甲」没有开始补全：她还有 2 部在排队，抓完再继续补全']
        assert runs == []

    def test_cancel(self, wsgi, two, runs):
        queue('/star/a1', 'queued', 'queued', 'done')
        response = wsgi.post('/pick/backfill/cancel', {'name': '演员甲'})
        assert alerts(follow(wsgi, response).text) == ['已取消「演员甲」排队的 2 部，已经抓到的留在库里']
        assert db.FetchTask.select().count() == 1

    def test_retry(self, wsgi, two, runs):
        queue('/star/a1', 'failed', 'failed', 'missing')
        response = wsgi.post('/pick/backfill/retry', {'name': '演员甲'})
        assert alerts(follow(wsgi, response).text) == ['「演员甲」失败的 2 部重新排进了队列']
        assert runs == ['run']

    def test_resume(self, wsgi, two, runs):
        backfill.pause('HTTP 403', now=NOW)
        response = wsgi.post('/pick/backfill/resume', {})
        assert alerts(follow(wsgi, response).text) == ['补全接着抓了']
        assert backfill.paused() is None
        assert runs == ['run']


    @pytest.mark.parametrize('path, data', [
        ('/pick/backfill/check', {}), ('/pick/backfill/start', {}), ('/pick/backfill/cancel', {}),
        ('/pick/backfill/retry', {}), ('/pick/backfill/search', {}), ('/pick/backfill/choose', {'path': '/star/c1'}),
    ])
    def test_a_form_without_a_row_does_not_guess_between_two_actresses_with_the_same_name(
            self, wsgi, today, cast, runs, path, data):
        for slug, javbus, code in (('jia', '/star/a1', 'C-001'), ('yi', '/star/b2', 'C-002')):
            actress = cast.actress(f'演员{slug}', slug, '共用读音', javbus=javbus)
            cast.movie(code)
            cast.link(code, actress)
        pick.add_names('star', ['共用读音'])
        # 页面是升级以前打开的，表单里没有 row；这个名字现在对着两行
        response = wsgi.post(path, {'name': '共用读音', **data})
        assert alerts(follow(wsgi, response).text) == [
            '页面已经过期：「共用读音」现在对应好几位女优，没有改动，请刷新页面后再点']
        assert (runs, db.ListSource.select().count(), db.StarLink.select().count()) == ([], 0, 0)

    @pytest.mark.parametrize('path', ['/pick/backfill/check', '/pick/backfill/start', '/pick/backfill/cancel',
                                      '/pick/backfill/retry'])
    def test_a_name_that_is_not_chosen_changes_nothing(self, wsgi, two, runs, path):
        response = wsgi.post(path, {'name': '没有选的演员'})
        assert alerts(follow(wsgi, response).text) == ['「没有选的演员」不在指定推荐的女优里了，没有改动']
        assert (runs, db.ListSource.select().count()) == ([], 0)

    @pytest.mark.parametrize('path', ['/pick/backfill/check', '/pick/backfill/start'])
    def test_an_actress_without_a_javbus_page_cannot_be_checked_or_filled(self, wsgi, today, runs, path):
        movie('L-001', ('演员丙', f'{AVWIKI}bing/'))
        pick.add_names('star', ['演员丙'])
        response = wsgi.post(path, {'name': '演员丙'})
        assert alerts(follow(wsgi, response).text) == ['还不知道「演员丙」的 JavBus 演员页，先点「查找她的 JavBus 页面」']
        assert (runs, db.ListSource.select().count()) == ([], 0)

    @pytest.mark.parametrize('path', ['/pick/backfill/check', '/pick/backfill/start', '/pick/backfill/cancel',
                                      '/pick/backfill/retry'])
    def test_a_missing_name_is_a_bad_request(self, wsgi, two, path):
        assert wsgi.post(path, {}).status == 400
        assert wsgi.post(path, {'name': '长' * 300}).status == 400

    @pytest.mark.parametrize('path', ['/pick/backfill/check', '/pick/backfill/start', '/pick/backfill/cancel',
                                      '/pick/backfill/retry', '/pick/backfill/resume'])
    def test_changes_need_a_post_from_bustag_itself(self, wsgi, two, runs, path):
        assert wsgi.get(path + '?name=' + '演员甲').status == 405
        assert wsgi.post(path, {'name': '演员甲'}, headers=CROSS_SITE).status == 403
        assert (runs, db.ListSource.select().count()) == ([], 0)


class TestTakingBackHerPage:
    '''关联了 JavBus 演员页的 av-wiki 女优：这一行写明关联的是哪个演员页，可以换一个、取消关联'''

    @pytest.fixture
    def linked(self, today, cast):
        '''えんいんこう（别名 演员甲）点选了 /star/a1：那里的名字演员甲是她的一个名字，正式名字换成了 JavBus 的那条'''
        jia = cast.actress('えんいんこう', 'jia', '演员甲')
        movie('L-001')
        cast.link('L-001', jia)
        db.save_lookup('L-001', db.LOOKUP_FILLED, 0, NOW, NOW)
        movie('C-001', ('演员甲', '/star/a1'))
        db.StarLink.create(key='avwiki:/av-actress/jia/', name='えんいんこう', state='choose', checked_at=NOW,
                           candidates='[["/star/a1", "演员甲"], ["/star/a2", "演员乙"]]')
        assert backfill.choose_page('avwiki:/av-actress/jia/', '/star/a1')
        pick.add_names('star', ['えんいんこう'])
        return {'name': 'えんいんこう', 'row': f'actress:{jia.id}'}

    def test_the_row_says_which_page_she_is_linked_to(self, wsgi, linked):
        text = wsgi.get('/pick').text
        assert '关联的 JavBus 演员页：演员甲（/star/a1）' in rows(text)['えんいんこう']
        assert buttons(text, 'えんいんこう')[-2:] == [('/pick/backfill/relink', '换一个'),
                                                  ('/pick/backfill/unlink', '取消关联')]

    def test_the_confirmation_of_taking_back_says_when_the_queue_is_kept(self, wsgi, linked):
        row = block(wsgi.get('/pick').text).select_one('li.backfill-row[data-name="えんいんこう"]')
        confirm = row.select_one('form[action="/pick/backfill/unlink"]')['data-confirm']
        assert '这个演员页还没抓的排队也不抓了' in confirm
        assert '还有别的女优也关联着这个演员页时，它排着的影片留着' in confirm

    def test_a_javbus_star_has_nothing_to_take_back(self, wsgi, two):
        text = wsgi.get('/pick').text
        assert '关联的 JavBus 演员页' not in rows(text)['演员甲']
        assert not [action for action, _ in buttons(text, '演员甲') if action.endswith('link')]

    def test_unlink(self, wsgi, linked, runs, fill_requests, cast):
        text = follow(wsgi, wsgi.post('/pick/backfill/unlink', linked)).text
        assert alerts(text) == ['已取消「えんいんこう」和 JavBus 演员页 /star/a1 的关联']
        assert buttons(text, 'えんいんこう') == [('/pick/backfill/search', '查找她的 JavBus 页面（用「えんいんこう」）')]
        assert cast.stars_of('L-001') == ['えんいんこう'] and cast.stars_of('C-001') == ['演员甲']
        # 正式名字变了：登记一轮补名，读一次她的女优页
        assert (fill_requests, runs) == (['fill'], [])

    def test_unlink_cancels_the_queue_of_that_page(self, wsgi, linked):
        queue('/star/a1', 'done', 'queued', 'queued')
        text = follow(wsgi, wsgi.post('/pick/backfill/unlink', linked)).text
        assert alerts(text) == ['已取消「えんいんこう」和 JavBus 演员页 /star/a1 的关联，这个演员页还没抓的 2 部也不抓了']

    def test_another_one(self, wsgi, linked, runs, fill_requests):
        text = follow(wsgi, wsgi.post('/pick/backfill/relink', linked)).text
        assert alerts(text) == ['已取消「えんいんこう」和 JavBus 演员页 /star/a1 的关联，从下面的查找结果里点一位是她']
        assert buttons(text, 'えんいんこう') == [('/pick/backfill/choose', '演员甲（/star/a1，取消过关联）'),
                                             ('/pick/backfill/choose', '演员乙（/star/a2）'),
                                             ('/pick/backfill/search', '再查一次')]
        assert (fill_requests, runs) == (['fill'], [])

    def test_another_one_without_earlier_results_searches_again(self, wsgi, linked, runs):
        db.StarLink.update(candidates='[]').execute()
        text = follow(wsgi, wsgi.post('/pick/backfill/relink', linked)).text
        assert alerts(text) == ['已取消「えんいんこう」和 JavBus 演员页 /star/a1 的关联，正在用「えんいんこう」重新查找，'
                                '稍等几秒这里会显示结果']
        assert runs == ['run']

    @pytest.mark.parametrize('path', ['/pick/backfill/relink', '/pick/backfill/unlink'])
    def test_changes_need_a_post_from_bustag_itself(self, wsgi, linked, path):
        assert wsgi.get(path + '?name=えんいんこう').status == 405
        assert wsgi.post(path, linked, headers=CROSS_SITE).status == 403
        assert db.Tag.get_by_id(db.Actress.get().tag_id).value == '演员甲'

    @pytest.mark.parametrize('path', ['/pick/backfill/relink', '/pick/backfill/unlink'])
    def test_bad_requests(self, wsgi, linked, path):
        assert wsgi.post(path, {}).status == 400
        assert wsgi.post(path, {'name': '长' * 300}).status == 400
        assert wsgi.post(path, {'name': 'えんいんこう', 'row': 'r' * 400}).status == 400
        assert alerts(follow(wsgi, wsgi.post(path, {'name': '没有选的演员'})).text) == [
            '「没有选的演员」不在指定推荐的女优里了，没有改动']
        assert db.Tag.get_by_id(db.Actress.get().tag_id).value == '演员甲'

    @pytest.mark.parametrize('path', ['/pick/backfill/relink', '/pick/backfill/unlink'])
    def test_a_form_without_a_row_does_not_guess_which_of_two_actresses_to_take_the_page_from(
            self, wsgi, linked, cast, fill_requests, path):
        cast.actress('演员乙', 'yi', '演员甲', javbus='/star/b2')
        pick.add_names('star', ['演员甲'])
        before = (db.Actress.select().count(), [(a.id, a.tag_id) for a in db.Actress.select().order_by(db.Actress.id)])
        response = wsgi.post(path, {'name': '演员甲'})
        assert response.status == 303
        assert alerts(follow(wsgi, response).text) == [
            '页面已经过期：「演员甲」现在对应好几位女优，没有改动，请刷新页面后再点']
        assert actresses.rejected_pages('/av-actress/jia/') == set()
        assert [(a.id, a.tag_id) for a in db.Actress.select().order_by(db.Actress.id)] == before[1]
        assert db.StarLink.get(db.StarLink.key == 'avwiki:/av-actress/jia/').state == db.LINK_FOUND
        assert fill_requests == []

    @pytest.mark.parametrize('path', ['/pick/backfill/relink', '/pick/backfill/unlink'])
    def test_a_form_without_a_row_for_a_name_that_is_still_one_actress_still_works(
            self, wsgi, linked, cast, path):
        response = wsgi.post(path, {'name': linked['name']})
        assert response.status == 303
        assert alerts(follow(wsgi, response).text)[0].startswith('已取消「えんいんこう」和 JavBus 演员页 /star/a1 的关联')
        assert actresses.rejected_pages('/av-actress/jia/') == {'/star/a1'}

    @pytest.mark.parametrize('path', ['/pick/backfill/relink', '/pick/backfill/unlink'])
    def test_a_row_that_is_gone_changes_nothing(self, wsgi, linked, fill_requests, path):
        response = wsgi.post(path, {'name': linked['name'], 'row': 'actress:999'})
        assert alerts(follow(wsgi, response).text) == ['「えんいんこう」不在指定推荐的女优里了，没有改动']
        assert actresses.rejected_pages('/av-actress/jia/') == set() and fill_requests == []

    @pytest.mark.parametrize('path', ['/pick/backfill/relink', '/pick/backfill/unlink'])
    def test_a_row_without_a_page_to_take_back(self, wsgi, two, path):
        text = follow(wsgi, wsgi.post(path, {'name': '演员甲'})).text
        assert alerts(text) == ['「演员甲」没有可以取消的 JavBus 演员页关联，没有改动']


class TestFindingHerPage:
    '''只有 av-wiki 名字（或者库里还没有）的女优：「查找她的 JavBus 页面」，名字相同的一位自动采用，否则点一位'''

    @pytest.fixture
    def avwiki_only(self, today):
        movie('L-001', ('演员丙', f'{AVWIKI}bing/'), ('演员丙读音', f'{AVWIKI}bing/'))
        pick.add_names('star', ['演员丙读音'])

    def test_her_row_offers_a_search_with_her_main_name(self, wsgi, avwiki_only):
        text = wsgi.get('/pick').text
        assert '没有她的 JavBus 演员页地址' in rows(text)['演员丙读音']
        assert buttons(text, '演员丙读音') == [('/pick/backfill/search', '查找她的 JavBus 页面（用「演员丙」）')]

    def test_the_number_that_tells_two_actresses_apart_is_left_out_of_the_search(self, wsgi, today, runs, cast):
        cast.actress('同名', 'jia')
        yi = cast.actress('同名（2）', 'yi', '同名', 'えんいんおつ')
        movie('L-001')
        cast.link('L-001', yi)
        pick.add_names('star', ['同名（2）'])
        text = wsgi.get('/pick').text
        # 设定里的名字就是她的正式名字：后面不另写；查找时用不带编号的名字
        assert rows(text)['同名（2）'].startswith('同名（2） 库里 1 部')
        assert buttons(text, '同名（2）') == [('/pick/backfill/search', '查找她的 JavBus 页面（用「同名」）')]
        response = wsgi.post('/pick/backfill/search', {'name': '同名（2）', 'row': f'actress:{yi.id}'})
        assert alerts(follow(wsgi, response).text) == ['已登记用「同名」查找她的 JavBus 演员页，稍等几秒这里会显示结果']
        found = db.StarLink.get()
        assert (found.key, found.name) == ('avwiki:/av-actress/yi/', '同名')

    def test_a_name_not_in_the_library_is_searched_by_itself(self, wsgi, today):
        pick.add_names('star', ['还没有的演员'])
        assert buttons(wsgi.get('/pick').text, '还没有的演员') == [
            ('/pick/backfill/search', '查找她的 JavBus 页面（用「还没有的演员」）')]

    def test_search(self, wsgi, avwiki_only, runs):
        response = wsgi.post('/pick/backfill/search', {'name': '演员丙读音'})
        assert alerts(follow(wsgi, response).text) == ['已登记用「演员丙」查找她的 JavBus 演员页，稍等几秒这里会显示结果']
        found = db.StarLink.get()
        assert (found.key, found.name, found.state) == ('avwiki:/av-actress/bing/', '演员丙', 'pending')
        assert runs == ['run']
        text = wsgi.get('/pick').text
        assert '正在查找她的 JavBus 演员页' in rows(text)['演员丙读音']
        assert block(text)['data-refresh'] == '1'

    def test_nobody_found(self, wsgi, avwiki_only):
        db.StarLink.create(key='avwiki:/av-actress/bing/', name='演员丙', state='none', checked_at=NOW)
        text = wsgi.get('/pick').text
        assert 'JavBus 上没有找到「演员丙」（11-06 12:00）' in rows(text)['演员丙读音']
        assert buttons(text, '演员丙读音') == [('/pick/backfill/search', '再查一次')]

    def test_a_search_that_was_waiting_when_the_block_came_says_so_and_can_be_asked_for_again(self, wsgi, avwiki_only):
        backfill.request_search('avwiki:/av-actress/bing/', '演员丙')
        backfill.pause('HTTP 403：访问被拒绝', now=NOW)
        text = wsgi.get('/pick').text
        assert ('查找出错（11-06 12:00）：被网站拦截，这次没有查找她的 JavBus 演员页，要继续请再点一次查找'
                in rows(text)['演员丙读音'])
        assert '正在查找她的 JavBus 演员页' not in rows(text)['演员丙读音']
        assert buttons(text, '演员丙读音') == [('/pick/backfill/search', '再查一次')]
        assert block(text)['data-refresh'] == '0'

    def test_a_search_that_went_wrong(self, wsgi, avwiki_only):
        db.StarLink.create(key='avwiki:/av-actress/bing/', name='演员丙', state='error', checked_at=NOW,
                           error='网络连接失败（<b>ConnectTimeout</b>）')
        text = wsgi.get('/pick').text
        assert '查找出错（11-06 12:00）：网络连接失败（<b>ConnectTimeout</b>）' in rows(text)['演员丙读音']
        assert '<b>ConnectTimeout</b>' not in text

    def test_candidates_to_pick_from(self, wsgi, avwiki_only):
        db.StarLink.create(key='avwiki:/av-actress/bing/', name='演员丙', state='choose', checked_at=NOW,
                           candidates='[["/star/c1", "演员丙丁"], ["/star/c2", "<i>演员</i>"]]')
        text = wsgi.get('/pick').text
        assert 'JavBus 上找到 2 位，点一位是她：' in rows(text)['演员丙读音']
        assert buttons(text, '演员丙读音') == [('/pick/backfill/choose', '演员丙丁（/star/c1）'),
                                               ('/pick/backfill/choose', '<i>演员</i>（/star/c2）'),
                                               ('/pick/backfill/search', '再查一次')]
        assert '<i>演员</i>' not in text

    def test_picking_a_candidate_gives_her_a_javbus_row(self, wsgi, avwiki_only, runs):
        db.StarLink.create(key='avwiki:/av-actress/bing/', name='演员丙', state='choose', checked_at=NOW,
                           candidates='[["/star/c1", "演员丙丁"], ["/star/c2", "演员丙"]]')
        response = wsgi.post('/pick/backfill/choose', {'name': '演员丙读音', 'path': '/star/c2'})
        text = follow(wsgi, response).text
        assert alerts(text) == ['已记下「演员丙读音」的 JavBus 演员页，可以查看和补全了']
        assert '关联的 JavBus 演员页：演员丙（/star/c2）' in rows(text)['演员丙读音']
        assert buttons(text, '演员丙读音') == [('/pick/backfill/check', '查看'),
                                               ('/pick/backfill/start', '补全最新 60 部'),
                                               ('/pick/backfill/relink', '换一个'), ('/pick/backfill/unlink', '取消关联')]

    def test_only_a_listed_candidate_can_be_picked(self, wsgi, avwiki_only):
        db.StarLink.create(key='avwiki:/av-actress/bing/', name='演员丙', state='choose', checked_at=NOW,
                           candidates='[["/star/c1", "演员丙丁"]]')
        response = wsgi.post('/pick/backfill/choose', {'name': '演员丙读音', 'path': '/star/c9'})
        assert alerts(follow(wsgi, response).text) == ['这个演员页不在查找的结果里，没有改动']
        assert db.StarLink.get().state == 'choose'

    def test_a_found_page_joins_the_row_of_the_same_page(self, wsgi, today):
        movie('L-001', ('演员丙读音', f'{AVWIKI}bing/'))
        movie('C-001', ('演员丙', '/star/c2'))
        pick.add_names('star', ['演员丙', '演员丙读音'])
        assert list(rows(wsgi.get('/pick').text)) == ['演员丙', '演员丙读音']
        db.StarLink.create(key='avwiki:/av-actress/bing/', name='演员丙读音', state='found', path='/star/c2')
        found = rows(wsgi.get('/pick').text)
        assert list(found) == ['演员丙']
        assert found['演员丙'].startswith('演员丙、演员丙读音 库里 2 部')

    @pytest.mark.parametrize('path, data', [
        ('/pick/backfill/search', {}),
        ('/pick/backfill/choose', {'name': '演员丙读音'}),
        ('/pick/backfill/choose', {'name': '演员丙读音', 'path': '长' * 300}),
    ])
    def test_odd_requests_are_refused(self, wsgi, avwiki_only, path, data):
        assert wsgi.post(path, data).status == 400

    def test_an_actress_that_already_has_a_page_needs_no_search(self, wsgi, two, runs):
        response = wsgi.post('/pick/backfill/search', {'name': '演员甲'})
        assert alerts(follow(wsgi, response).text) == ['「演员甲」已经有 JavBus 演员页了，不用查找']
        assert (runs, db.StarLink.select().count()) == ([], 0)

    @pytest.mark.parametrize('path', ['/pick/backfill/search', '/pick/backfill/choose'])
    def test_changes_need_a_post_from_bustag_itself(self, wsgi, avwiki_only, runs, path):
        assert wsgi.get(path + '?name=x').status == 405
        assert wsgi.post(path, {'name': '演员丙读音', 'path': '/star/c1'}, headers=CROSS_SITE).status == 403
        assert db.StarLink.select().count() == 0


def about_row(wsgi):
    found = BeautifulSoup(wsgi.get('/about').text, 'html.parser').select_one('#background-tasks')
    return {plain(name): plain(name.find_next_sibling('dd')) for name in found.select('dt')}['补全作品']


class TestAboutPage:
    @pytest.fixture(autouse=True)
    def quiet(self, monkeypatch, today):
        monkeypatch.setattr(backfill, 'last_run', None)

    def test_nothing_queued(self, wsgi):
        assert about_row(wsgi) == ('现在没有在排队的影片。今天按演员补全已抓 0 部，还可以抓 300 部（每天最多 300 部）。'
                                   '程序启动后还没有抓过排队的影片。')

    def test_what_waits_what_was_fetched_today_and_the_last_run(self, wsgi, monkeypatch):
        queue('/star/a1', 'queued', 'queued', 'done', 'done', 'done')
        backfill.enqueue_codes(['U-001'], db.ORIGIN_UPLOAD)
        monkeypatch.setattr(backfill, 'last_run', backfill.QueueRun(
            finished_at=NOW, fetched=3, added=2, missing=1, left=3, left_list=2))
        assert about_row(wsgi) == ('在排队 3 部（按演员补全 2 部，上传番号和导入打标库 1 部）。'
                                   '今天按演员补全已抓 3 部，还可以抓 297 部（每天最多 300 部）。'
                                   '上一次（2026-11-06 12:00:00）抓了 3 部，新增 2 部，网站上没有 1 部，失败 0 部。')

    def test_while_a_run_is_going(self, wsgi, monkeypatch):
        monkeypatch.setattr(schedule, 'queue_state', lambda: ('running', NOW))
        assert '现在正在抓（2026-11-06 12:00:00 开始）。' in about_row(wsgi)

    def test_while_a_run_waits_for_its_turn(self, wsgi, monkeypatch):
        monkeypatch.setattr(schedule, 'queue_state', lambda: ('queued', None))
        assert '已经排了一次，轮到就开始。' in about_row(wsgi)

    def test_why_the_last_run_stopped(self, wsgi, monkeypatch):
        monkeypatch.setattr(backfill, 'last_run', backfill.QueueRun(
            finished_at=NOW, fetched=5, added=5, left=4, left_list=4, held='limit'))
        assert about_row(wsgi).endswith('上一次（2026-11-06 12:00:00）抓了 5 部，新增 5 部，网站上没有 0 部，失败 0 部，'
                                        '今天按演员补全的额度用完了，明天接着抓。')

    def test_a_pause_is_explained(self, wsgi):
        backfill.pause('HTTP 429：请求太频繁', now=NOW)
        assert ('今天被网站拦截过（2026-11-06 12:00:00）：HTTP 429：请求太频繁。按演员补全今天不再自动接着抓；'
                '已经排队的影片明天自动继续，也可以在指定推荐页上点"现在继续"；还没做的补全、查看、查找不会再自己去做，'
                '要继续请在指定推荐页上再点一次。') in about_row(wsgi)


class TestWhileTheDatabaseIsHeldByAnotherProgram:
    '''
    抓排队的影片因为数据库被别的程序占着没做成、用户登记了的事留着等再试（schedule.busy_wait）：
    页面上那一行、这一块、关于页写明在等什么、什么时候再试；再点同一位的"补全"，提示如实，这次点击也让后台再看一轮
    '''
    WAITING = '数据库被别的程序占着，这一轮没做成，约 12:05 自动再试'

    @pytest.fixture
    def busy(self, monkeypatch):
        '''下一次再试在 12:05（next_at 是 None 时是正在试）'''
        def hold(next_at=datetime.datetime(2026, 11, 6, 12, 5)):
            monkeypatch.setattr(schedule, 'busy_wait', lambda: schedule.BusyWait(NOW, next_at))

        return hold

    @pytest.mark.parametrize('state, words', [
        ({'checking': True}, '等着读她的第 1 页…'),
        ({'wanted': 60}, '等着翻她的演员页…'),
    ], ids=['view', 'fill'])
    def test_a_click_kept_for_the_database_says_what_it_waits_for(self, wsgi, two, busy, state, words):
        db.ListSource.create(path='/star/a1', **state)
        busy()
        text = wsgi.get('/pick').text
        assert f'{words}（{self.WAITING}）' in rows(text)['演员甲']
        assert block(text)['data-refresh'] == '1'

    def test_a_search_kept_for_the_database_says_what_it_waits_for(self, wsgi, today, busy):
        movie('L-001', ('演员丙', f'{AVWIKI}bing/'), ('演员丙读音', f'{AVWIKI}bing/'))
        pick.add_names('star', ['演员丙读音'])
        backfill.request_search('avwiki:/av-actress/bing/', '演员丙')
        busy()
        assert f'等着查找她的 JavBus 演员页…（{self.WAITING}）' in rows(wsgi.get('/pick').text)['演员丙读音']

    def test_while_the_next_try_is_going_on_it_says_so(self, wsgi, two, busy):
        db.ListSource.create(path='/star/a1', wanted=60)
        busy(None)
        assert '等着翻她的演员页…（数据库被别的程序占着，这一轮没做成，正在再试）' in rows(wsgi.get('/pick').text)['演员甲']

    def test_the_block_says_it_even_when_only_queued_movies_wait(self, wsgi, two, busy):
        queue('/star/a1', 'queued', 'queued')
        busy()
        assert f'现在排队 2 部。 {self.WAITING}。' in plain(block(wsgi.get('/pick').text))

    def test_nothing_is_said_while_the_database_is_not_held(self, wsgi, two):
        db.ListSource.create(path='/star/a1', wanted=60)
        text = wsgi.get('/pick').text
        assert '数据库被别的程序占着' not in text
        assert '等着翻她的演员页…' in rows(text)['演员甲'] and '等着翻她的演员页…（' not in rows(text)['演员甲']

    def test_asking_for_the_fill_that_is_kept_again_says_so_and_asks_for_a_run(self, wsgi, two, busy, runs):
        assert backfill.request_fill('/star/a1') is None
        busy()
        response = wsgi.post('/pick/backfill/start', {'name': '演员甲'})
        assert alerts(follow(wsgi, response).text) == [
            '「演员甲」上一次点的补全还留着没做完：数据库刚才被别的程序占着，没来得及翻她的演员页。'
            '已经让后台马上再试一次，不用再点；做完以后再点才会补下一批']
        assert db.ListSource.get(db.ListSource.path == '/star/a1').wanted == 60
        assert runs == ['run']

    def test_asking_again_while_she_is_still_being_looked_up_also_asks_for_a_run(self, wsgi, two, runs):
        assert backfill.request_fill('/star/a1') is None
        response = wsgi.post('/pick/backfill/start', {'name': '演员甲'})
        assert alerts(follow(wsgi, response).text) == ['「演员甲」没有开始补全：还在找她的作品，抓完再继续补全']
        assert runs == ['run']

    def test_the_about_page_says_it_too(self, wsgi, busy, monkeypatch):
        monkeypatch.setattr(backfill, 'last_run', None)
        busy()
        assert f'{self.WAITING}。' in about_row(wsgi)
