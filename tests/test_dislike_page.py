'''
整页标不喜欢：列表下面的「这一页剩下的都不喜欢」把这一页列出的番号一起提交，只标还不是用户亲手打标的影片
'''
import datetime
import logging
import re
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from bs4 import BeautifulSoup

from bustag.app import index, pick
from bustag.spider import db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate

CROSS_SITE = {'Sec-Fetch-Site': 'cross-site', 'Origin': 'https://evil.example'}
OLD = datetime.datetime(2026, 11, 1, 8, 0, 0)
UNDO_HINT = '标错了可以到"打标"页的「不喜欢」里改回来'


def soup_of(text):
    return BeautifulSoup(text, 'html.parser')


def plain(node):
    return ' '.join(node.get_text(' ', strip=True).split())


def alerts(text, kind):
    return [plain(box) for box in soup_of(text).select(f'div.alert-{kind}')]


def covers_on(text):
    '''页面上列出的影片（按封面地址里的番号，保持顺序）'''
    return re.findall(r'src="/cover/([^"]+)"', text)


def bulk_form(text):
    '''页面上「这一页剩下的都不喜欢」的表单，没有时是 None'''
    return soup_of(text).select_one('form#dislike-rest')


def fields_of(form):
    '''表单里要提交的字段：[(名字, 值)]，按页面上的先后'''
    return [(field['name'], field['value']) for field in form.select('input[type="hidden"]')]


def target_of(response):
    assert response.status == 303
    parts = urlsplit(response.headers['location'])
    assert parts.netloc == '127.0.0.1'
    return parts.path, {key: values[0] for key, values in parse_qs(parts.query).items()}, parts.fragment


def follow(wsgi, response):
    parts = urlsplit(response.headers['location'])
    assert response.status == 303
    return wsgi.get(parts.path + ('?' + parts.query if parts.query else '')).text


def rates():
    '''全部评分：{番号: (类型, 值)}'''
    return {row.item_id: (row.rate_type, row.rate_value) for row in ItemRate.select()}


def submit(wsgi, fanhaos, back='tagit', confirmed=True, **extra):
    data = [('back', back), *extra.items(), *(('fanhao', fanhao) for fanhao in fanhaos)]
    if confirmed:
        data.append(('confirmed', '1'))
    return wsgi.post('/dislike-page', data)


USER_DISLIKE = (RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
USER_LIKE = (RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)


@pytest.fixture
def library(make_item):
    '''十二部没有评分的影片 M-001 … M-012，都带"标签一"，后入库的排在前面'''
    with db.db.atomic():
        for n in range(1, 13):
            make_item(f'M-{n:03d}', tags=[('genre', '标签一'), ('star', '演员甲')])


def recommend_all(fanhaos, recommended):
    '''模型给这些影片打了分：前 recommended 部在"推荐"里，其余在"不太像"里'''
    scores = [db.Score(fanhao, 0.9 - n / 100, [], []) for n, fanhao in enumerate(fanhaos)]
    db.save_scores(scores, lambda total: recommended)


class TestButton:
    def test_the_recommend_tabs_carry_the_movies_of_the_page_in_the_form(self, wsgi, library):
        recommend_all([f'M-{n:03d}' for n in range(12, 0, -1)], 11)
        text = wsgi.get('/?page=1&like=1').text
        form = bulk_form(text)
        assert (form['action'], form['method']) == ('/dislike-page', 'post')
        listed = covers_on(text)
        assert len(listed) == 10
        assert fields_of(form) == [('back', 'recommend'), ('page', '1'), ('like', '1'),
                                   *(('fanhao', fanhao) for fanhao in listed)]
        assert plain(form.select_one('button[type="submit"]')) == '这一页剩下的都不喜欢'
        # 第二页只剩 1 部，"不太像"里也只有 1 部：确认的话里写的是这一页的部数
        assert form['data-confirm'] == '确定把这一页的 10 部影片都标为不喜欢吗？标错了可以到打标页的「不喜欢」里改回来'
        second = bulk_form(wsgi.get('/?page=2&like=1').text)
        assert fields_of(second) == [('back', 'recommend'), ('page', '2'), ('like', '1'), ('fanhao', 'M-002')]
        assert second['data-confirm'].startswith('确定把这一页的 1 部影片都标为不喜欢吗？')
        unlike = bulk_form(wsgi.get('/?like=0').text)
        assert fields_of(unlike) == [('back', 'recommend'), ('page', '1'), ('like', '0'), ('fanhao', 'M-001')]

    def test_the_untagged_tab_of_the_tag_page_has_it_with_or_without_a_search(self, wsgi, library, make_item):
        make_item('X-001', tags=[('genre', '标签二')])
        text = wsgi.get('/tagit?page=2').text
        assert fields_of(bulk_form(text)) == [('back', 'tagit'), ('page', '2'),
                                              *(('fanhao', fanhao) for fanhao in covers_on(text))]
        assert covers_on(text) == ['M-003', 'M-002', 'M-001']
        searched = wsgi.get('/tagit?q=' + quote('标签二')).text
        assert fields_of(bulk_form(searched)) == [('back', 'tagit'), ('page', '1'), ('q', '标签二'), ('fanhao', 'X-001')]

    def test_the_tabs_of_tagged_movies_do_not_have_it(self, wsgi, library):
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'M-001')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, 'M-002')
        for path in ('/tagit?like=1', '/tagit?like=0'):
            text = wsgi.get(path).text
            assert len(covers_on(text)) == 1 and bulk_form(text) is None

    def test_the_pick_page_has_it(self, wsgi, library):
        pick.add_names('tag', ['标签一'])
        text = wsgi.get('/pick?page=2').text
        assert covers_on(text) == ['M-002', 'M-001']
        assert fields_of(bulk_form(text)) == [('back', 'pick'), ('page', '2'), ('fanhao', 'M-002'), ('fanhao', 'M-001')]

    @pytest.mark.parametrize('path', ['/', '/?like=0', '/tagit', '/tagit?q=nothing', '/pick'])
    def test_an_empty_list_has_no_button(self, wsgi, path):
        pick.add_names('tag', ['标签一'])
        assert bulk_form(wsgi.get(path).text) is None

    def test_the_local_page_has_no_button(self, wsgi, library):
        db.LocalItem.saveit('M-001', 'http://media.local/M-001.mp4')
        text = wsgi.get('/local').text
        assert covers_on(text) == ['M-001'] and bulk_form(text) is None

    def test_the_page_script_asks_first_and_marks_the_form_as_confirmed(self, wsgi):
        script = wsgi.get('/static/js/bustag.js').text
        assert "form[data-confirm]" in script and 'window.confirm' in script
        assert 'name="confirmed"' in script


class TestConfirmPage:
    '''没有带着"已确认"提交（页面脚本还没加载好，或者关掉了脚本）：先回一个确认页，什么都不改'''

    def test_asks_before_tagging_and_changes_nothing(self, wsgi, library):
        response = submit(wsgi, ['M-012', 'M-011', 'M-010'], back='recommend', confirmed=False, page='3', like='0')
        assert response.status == 200
        soup = soup_of(response.text)
        assert '<title>确认</title>' in response.text
        assert plain(soup.select_one('#confirm-question')) == '把这一页的 3 部影片都标为不喜欢吗？'
        assert plain(soup.select_one('#confirm-detail')) == (
            f'M-012、M-011、M-010。已经打过标的会跳过，不会改。{UNDO_HINT}。')
        form = soup.select_one('form#confirm-form')
        assert (form['action'], form['method']) == ('/dislike-page', 'post')
        assert fields_of(form) == [('back', 'recommend'), ('page', '3'), ('like', '0'), ('fanhao', 'M-012'),
                                   ('fanhao', 'M-011'), ('fanhao', 'M-010'), ('confirmed', '1')]
        assert plain(form.select_one('button[type="submit"]')) == '确定，都标为不喜欢'
        assert form.select_one('a')['href'] == '/?page=3&like=0' and plain(form.select_one('a')) == '取消'
        assert rates() == {}

    @pytest.mark.parametrize('back, extra, cancel', [
        ('pick', {'page': '2'}, '/pick?page=2#pick-total'),
        ('tagit', {'page': '4', 'q': '标签一'}, '/tagit?page=4&q=' + quote('标签一')),
        ('tagit', {}, '/tagit?page=1'),
    ])
    def test_cancel_goes_back_to_the_list(self, wsgi, library, back, extra, cancel):
        response = submit(wsgi, ['M-001'], back=back, confirmed=False, **extra)
        assert soup_of(response.text).select_one('form#confirm-form a')['href'] == cancel

    def test_confirming_the_page_does_it(self, wsgi, library):
        asked = submit(wsgi, ['M-001', 'M-002'], confirmed=False)
        form = soup_of(asked.text).select_one('form#confirm-form')
        done = wsgi.post(form['action'], fields_of(form))
        assert done.status == 303
        assert rates() == {'M-001': USER_DISLIKE, 'M-002': USER_DISLIKE}

    def test_odd_search_words_are_escaped(self, wsgi, library):
        response = submit(wsgi, ['M-001'], confirmed=False, q='"><script>alert(1)</script>')
        assert '<script>alert(1)' not in response.text
        assert ('q', '"><script>alert(1)</script>') in fields_of(soup_of(response.text).select_one('form#confirm-form'))


class TestTagging:
    def test_every_listed_movie_becomes_a_user_dislike_and_the_page_says_so_once(self, wsgi, library, caplog):
        recommend_all([f'M-{n:03d}' for n in range(12, 0, -1)], 11)
        listed = covers_on(wsgi.get('/?page=1&like=1').text)
        caplog.set_level(logging.INFO, logger='bustag')
        response = submit(wsgi, listed, back='recommend', page='1', like='1')
        path, query, fragment = target_of(response)
        assert (path, fragment) == ('/', '') and set(query) == {'page', 'like', 'note'}
        assert (query['page'], query['like']) == ('1', '1')
        saved = rates()
        assert all(saved[fanhao] == USER_DISLIKE for fanhao in listed)
        assert saved['M-002'] == (RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        assert saved['M-001'] == (RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)
        # 打了标的影片不再留着分数
        assert sorted(row.item_id for row in db.ItemScore.select()) == ['M-001', 'M-002']
        assert f'整页标不喜欢：标了 10 部（{"、".join(listed)}），跳过 0 部已经打过标的' in caplog.messages
        address = response.headers['location']
        text = follow(wsgi, response)
        assert alerts(text, 'success')[0] == f'已把 10 部标为不喜欢。{UNDO_HINT}'
        assert covers_on(text) == ['M-002']
        # 刷新、收藏这个地址：提示不再出现
        again = follow(wsgi, response)
        assert not any('标为不喜欢' in alert for alert in alerts(again, 'success')), address
        # 标错了的到打标页的"不喜欢"里改
        assert sorted(covers_on(wsgi.get('/tagit?like=0').text)) == sorted(listed)

    def test_only_what_the_page_listed_is_tagged_not_what_the_list_holds_now(self, wsgi, library, make_item):
        listed = covers_on(wsgi.get('/tagit').text)
        assert listed == [f'M-{n:03d}' for n in range(12, 2, -1)]
        # 页面开着的时候又抓到了新影片：现在的第一页已经不是用户看到的那一页
        for n in range(1, 6):
            make_item(f'NEW-{n:03d}')
        assert covers_on(wsgi.get('/tagit').text)[:5] == [f'NEW-{n:03d}' for n in range(5, 0, -1)]
        submit(wsgi, listed, page='1')
        saved = rates()
        assert sorted(saved) == sorted(listed) and set(saved.values()) == {USER_DISLIKE}

    def test_movies_the_user_tagged_meanwhile_are_skipped_not_overwritten(self, wsgi, library):
        ItemRate.create(item='M-012', rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.LIKE, rete_time=OLD)
        ItemRate.create(item='M-011', rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.DISLIKE, rete_time=OLD)
        ItemRate.create(item='M-010', rate_type=RATE_TYPE.SYSTEM_RATE, rate_value=RATE_VALUE.LIKE, rete_time=OLD)
        response = submit(wsgi, ['M-012', 'M-011', 'M-010', 'M-009'], page='1')
        saved = rates()
        assert saved == {'M-012': USER_LIKE, 'M-011': USER_DISLIKE, 'M-010': USER_DISLIKE, 'M-009': USER_DISLIKE}
        kept = {row.item_id: row.rete_time for row in ItemRate.select().where(ItemRate.item.in_(['M-012', 'M-011']))}
        assert kept == {'M-012': OLD, 'M-011': OLD}
        assert alerts(follow(wsgi, response), 'success') == [f'已把 2 部标为不喜欢，跳过 2 部已经打过标的。{UNDO_HINT}']

    def test_a_page_whose_movies_are_all_tagged_already_changes_nothing(self, wsgi, library):
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'M-012')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, 'M-011')
        text = follow(wsgi, submit(wsgi, ['M-012', 'M-011'], page='1'))
        assert alerts(text, 'danger') == ['这一页的 2 部都已经打过标了，没有改动'] and alerts(text, 'success') == []
        assert rates() == {'M-012': USER_LIKE, 'M-011': USER_DISLIKE}

    def test_codes_that_are_not_in_the_library_are_ignored(self, wsgi, library):
        response = submit(wsgi, ['M-012', 'GONE-001', 'M-012', '../etc', '<script>', ''], page='1')
        assert rates() == {'M-012': USER_DISLIKE}
        assert alerts(follow(wsgi, response), 'success') == [f'已把 1 部标为不喜欢。{UNDO_HINT}']
        assert db.Item.select().count() == 12

    def test_nothing_but_unknown_codes_changes_nothing(self, wsgi, library):
        text = follow(wsgi, submit(wsgi, ['GONE-001', 'GONE-002'], page='1'))
        assert alerts(text, 'danger') == ['提交的影片库里都没有，没有改动']
        assert rates() == {}

    @pytest.mark.parametrize('fanhaos', [[], ['', ' '], ['不是番号']])
    def test_an_empty_submission_changes_nothing(self, wsgi, library, fanhaos):
        response = submit(wsgi, fanhaos, back='recommend', page='2', like='0')
        path, query, _ = target_of(response)
        assert (path, query['page'], query['like']) == ('/', '2', '0')
        assert alerts(follow(wsgi, response), 'danger') == ['没有提交要标的影片，没有改动']
        assert rates() == {}

    def test_training_is_left_to_the_user(self, wsgi, library, write_model):
        path = write_model(likes=150, dislikes=60)
        before = path.read_bytes()
        submit(wsgi, ['M-012', 'M-011'], page='1')
        assert path.read_bytes() == before
        labels = plain(soup_of(wsgi.get('/model').text).select_one('#model-labels'))
        assert labels == '现在有 2 条打标（喜欢 0、不喜欢 2），还不能训练：训练数据不足, 无法训练模型. 需要200, 当前2'


class TestGoingBack:
    def test_the_pick_page_shows_the_result_next_to_the_list(self, wsgi, library):
        pick.add_names('tag', ['标签一'])
        response = submit(wsgi, ['M-002', 'M-001'], back='pick', page='2')
        path, query, fragment = target_of(response)
        assert (path, fragment) == ('/pick', 'pick-total') and set(query) == {'page', 'done'} and query['page'] == '2'
        text = follow(wsgi, response)
        soup = soup_of(text)
        assert plain(soup.select_one('#pick-done')) == f'已把 2 部标为不喜欢。{UNDO_HINT}'
        # 列表上方的那一条紧挨着"共 N 部"，页面最上面不再重复
        assert soup.select_one('#pick-total').find_next('div', class_='alert') is soup.select_one('#pick-done')
        assert alerts(text, 'success') == [f'已把 2 部标为不喜欢。{UNDO_HINT}']
        assert plain(soup.select_one('#pick-total')) == '符合设定的未打标影片共 10 部'
        assert soup_of(follow(wsgi, response)).select_one('#pick-done') is None

    def test_a_pick_page_with_nothing_changed_says_so_next_to_the_list(self, wsgi, library):
        pick.add_names('tag', ['标签一'])
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'M-001')
        text = follow(wsgi, submit(wsgi, ['M-001'], back='pick', page='1'))
        note = soup_of(text).select_one('#pick-done')
        assert plain(note) == '这一页的 1 部都已经打过标了，没有改动' and 'alert-danger' in note['class']

    def test_the_tag_page_keeps_the_page_and_the_search_word(self, wsgi, library):
        response = submit(wsgi, ['M-001'], back='tagit', page='2', q='标签一')
        path, query, fragment = target_of(response)
        assert (path, fragment) == ('/tagit', '')
        assert {key: value for key, value in query.items() if key != 'note'} == {'page': '2', 'q': '标签一'}
        text = follow(wsgi, response)
        assert alerts(text, 'success') == [f'已把 1 部标为不喜欢。{UNDO_HINT}']
        assert 'value="标签一"' in text and '第2页' in text

    @pytest.mark.parametrize('page, like, landed', [('3', '0', ('3', '0')), ('abc', '2', ('1', '1')), ('', '', ('1', '1')),
                                                    ('99999999', '1', ('1', '1')), ('-1', 'x', ('1', '1'))])
    def test_page_and_tab_come_back_as_plain_numbers(self, wsgi, library, page, like, landed):
        _, query, _ = target_of(submit(wsgi, ['M-001'], back='recommend', page=page, like=like))
        assert (query['page'], query['like']) == landed

    @pytest.mark.parametrize('back', ['', 'local', 'https://evil.example/', '//evil.example', 'RECOMMEND', '/pick'])
    def test_only_the_three_list_pages_can_be_asked_for(self, wsgi, library, back):
        response = submit(wsgi, ['M-001'], back=back)
        assert response.status == 400
        assert '<p>提交的页面只能是推荐、指定推荐或打标</p>' in response.text
        assert rates() == {}


class TestLimits:
    @pytest.mark.parametrize('headers', [CROSS_SITE, {'Origin': 'https://evil.example'}])
    def test_a_form_submitted_by_another_site_is_rejected(self, wsgi, library, headers):
        data = [('back', 'tagit'), ('fanhao', 'M-001'), ('confirmed', '1')]
        response = wsgi.post('/dislike-page', data, headers=headers)
        assert response.status == 403 and '<title>禁止访问</title>' in response.text
        assert rates() == {}
        same_origin = {'Sec-Fetch-Site': 'same-origin', 'Origin': 'http://127.0.0.1'}
        assert wsgi.post('/dislike-page', data, headers=same_origin).status == 303

    def test_more_than_one_page_of_movies_is_refused(self, wsgi, library):
        assert db.PAGE_SIZE == 10
        response = submit(wsgi, [f'M-{n:03d}' for n in range(1, 12)])
        assert response.status == 400
        assert '<p>一次最多提交一页的影片（10 部）</p>' in response.text
        assert rates() == {}
        assert submit(wsgi, [f'M-{n:03d}' for n in range(1, 11)]).status == 303
        assert len(rates()) == 10

    def test_the_address_needs_a_post(self, wsgi, library):
        response = wsgi.get('/dislike-page?back=tagit&fanhao=M-001&confirmed=1')
        assert response.status == 405
        assert rates() == {}

    def test_a_database_held_by_another_program_gives_the_busy_page(self, wsgi, library, locked_database):
        with locked_database():
            response = wsgi.post('/dislike-page', [('back', 'tagit'), ('fanhao', 'M-001'), ('confirmed', '1')],
                                 allow_server_error=True)
        assert response.status == 503 and '<title>数据库正忙</title>' in response.text
        assert rates() == {}


class TestNotes:
    '''跳回页面时的提示只显示一次，网址里只有一个猜不到的编号'''

    def test_a_made_up_note_shows_nothing(self, wsgi, library):
        for query in ('note=added', 'note=abc', 'note=', 'note=<b>x</b>', 'done=1'):
            for path in ('/', '/tagit', '/pick'):
                text = wsgi.get(f'{path}?{query}').text
                assert alerts(text, 'danger') == [] and '<b>x</b>' not in text
                assert not any('标为不喜欢' in alert for alert in alerts(text, 'success'))

    def test_the_number_in_the_address_cannot_be_guessed(self, wsgi, library):
        tokens = {target_of(submit(wsgi, ['M-001']))[1]['note'] for _ in range(20)}
        assert len(tokens) == 20 and all(len(token) >= 16 for token in tokens)

    def test_a_note_belongs_to_the_page_it_was_left_for_only_once(self, wsgi, library):
        token = target_of(submit(wsgi, ['M-001']))[1]['note']
        assert alerts(wsgi.get(f'/tagit?note={token}').text, 'success') == [f'已把 1 部标为不喜欢。{UNDO_HINT}']
        assert alerts(wsgi.get(f'/tagit?note={token}').text, 'success') == []

    def test_only_the_latest_notes_are_kept(self, wsgi, library, monkeypatch):
        monkeypatch.setattr(index, '_notes', {})
        tokens = [target_of(submit(wsgi, ['M-001']))[1]['note'] for _ in range(index.MAX_NOTES + 1)]
        assert len(index._notes) == index.MAX_NOTES
        assert alerts(wsgi.get(f'/tagit?note={tokens[0]}').text, 'danger') == []
        assert alerts(wsgi.get(f'/tagit?note={tokens[-1]}').text, 'danger') == [
            '这一页的 1 部都已经打过标了，没有改动']
