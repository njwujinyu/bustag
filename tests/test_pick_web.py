'''
指定推荐的页面和表单：首页的第三个标签页、推荐设定区（不靠脚本，全部是表单提交）、列表、打标后回到原处
'''
import errno
import json
import logging
import re
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from bs4 import BeautifulSoup

from bustag import util
from bustag.app import pick
from bustag.app.pick import Settings
from bustag.spider import db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate

TOP_NAV_ACTIVE = re.compile(r'<li class="nav-item active">\s*<a class="nav-link" href="([^"]+)"')
CROSS_SITE = {'Sec-Fetch-Site': 'cross-site', 'Origin': 'https://evil.example'}
ODD_NAMES = ['<script>alert(1)</script>', '"双引号" & \'单引号\'', 'a&b<c>d']
AVWIKI = 'https://av-wiki.net/av-actress/'


def covers_on(text):
    '''页面上列出的影片（按封面地址里的番号，保持顺序）'''
    return re.findall(r'src="/cover/([^"]+)"', text)


def redirect_target(response):
    assert response.status == 303
    parts = urlsplit(response.headers['location'])
    assert parts.netloc == '127.0.0.1'
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}, parts.fragment


def follow(wsgi, response):
    '''跟着表单提交后的重定向打开页面'''
    parts = urlsplit(response.headers['location'])
    assert response.status == 303 and parts.netloc == '127.0.0.1'
    return wsgi.get(parts.path + ('?' + parts.query if parts.query else ''))


def back_on_the_pick_page(response):
    '''推荐设定的表单提交后跳回指定推荐页：网址里只有一次性提示的编号，返回这个编号'''
    path, query, fragment = redirect_target(response)
    assert (path, set(query), fragment) == ('/pick', {'note'}, '')
    assert re.fullmatch(r'[A-Za-z0-9_-]{16,}', query['note'])
    return query['note']


def soup_of(text):
    return BeautifulSoup(text, 'html.parser')


def chosen(text, kind):
    '''设定区里已选的名字：[(名字, 后面显示的部数, 是不是标成库里没有)]'''
    return [(chip.select_one('button[name="name"]')['value'], chip.select_one('.pick-count').get_text(strip=True),
             'badge-danger' in chip['class'])
            for chip in soup_of(text).select(f'#pick-chosen-{kind} .pick-chip')]


def offered(text, kind):
    '''"从你喜欢的影片里选"里列出的名字：[(名字, 次数)]'''
    return [(button['value'], int(button.select_one('.badge').get_text(strip=True)))
            for button in soup_of(text).select(f'#pick-liked-{kind} button[name="name"]')]


def tabs(text):
    '''推荐页的标签页：[(文字, 地址, 是不是当前页)]，文字里的空白收成一个空格'''
    return [(' '.join(link.get_text(' ', strip=True).split()), link['href'], 'active' in link['class'])
            for link in soup_of(text).select('ul.nav-tabs a.nav-link')]


def alerts(text):
    soup = soup_of(text)
    return [' '.join(box.get_text(' ', strip=True).split()) for box in soup.select('div.alert')]


def cards(text):
    '''页面上每部影片的卡片：[(番号, 相似度, 因为, 拉低分数的)]，没有的那一项是 None'''
    found = []
    for row in soup_of(text).select('div.row'):
        cover = row.select_one('img.coverimg')
        if cover is not None:
            parts = [row.select_one(selector) for selector in ('.item-score .badge', '.item-because', '.item-against')]
            found.append((cover['data-cover'].rsplit('/', 1)[1],
                          *(' '.join(part.get_text(' ', strip=True).split()) if part else None for part in parts)))
    return found


def rate(fanhao, rate_type, rate_value):
    assert ItemRate.saveit(rate_type, rate_value, fanhao) is not None


def stored_rate(fanhao):
    item_rate = ItemRate.get_by_fanhao(fanhao)
    return (item_rate.rate_type, item_rate.rate_value) if item_rate else None


def choose(stars=(), tags=(), mode='all', model_only=False):
    if stars:
        pick.add_names('star', stars)
    if tags:
        pick.add_names('tag', tags)
    if mode != 'all' or model_only:
        pick.save_options(mode, model_only)


@pytest.fixture
def library(make_item):
    '''五部没有评分的影片，后入库的排在前面'''
    make_item('M-001', tags=[('star', '演员甲'), ('genre', '标签一'), ('genre', '标签二')])
    make_item('M-002', tags=[('star', '演员乙'), ('genre', '标签一')])
    make_item('M-003', tags=[('star', '演员甲'), ('star', '演员乙'), ('genre', '标签二'), ('系列:', '系列甲')])
    make_item('M-004', tags=[('genre', '标签一'), ('genre', '标签二'), ('genre', '4K')])
    make_item('M-005', tags=[('star', '演员丙'), ('genre', '标签三')])


class TestTabs:
    def test_home_page_has_a_third_tab_and_still_opens_on_likes(self, wsgi, library):
        rate('M-001', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        rate('M-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)
        home = wsgi.get('/').text
        assert tabs(home) == [('推荐', '/?like=1', True), ('不太像', '/?like=0', False), ('指定推荐', '/pick', False)]
        assert covers_on(home) == ['M-001']
        assert tabs(wsgi.get('/?like=0').text) == [('推荐', '/?like=1', False), ('不太像', '/?like=0', True),
                                                  ('指定推荐', '/pick', False)]

    def test_pick_page_is_a_tab_of_the_recommend_page(self, wsgi, library):
        response = wsgi.get('/pick')
        assert response.status == 200
        assert response.headers['content-type'] == 'text/html; charset=UTF-8'
        assert '<title>指定推荐</title>' in response.text
        assert TOP_NAV_ACTIVE.findall(response.text) == ['/']
        assert tabs(response.text) == [('推荐', '/?like=1', False), ('不太像', '/?like=0', False),
                                      ('指定推荐', '/pick', True)]
        assert db.db.is_closed()

    def test_tab_shows_how_many_untagged_movies_match(self, wsgi, library):
        choose(stars=['演员甲', '演员乙'])
        rate('M-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        rate('M-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        for path in ('/', '/?like=0', '/pick'):
            assert tabs(wsgi.get(path).text)[2][0] == '指定推荐 2', path

    def test_tab_shows_zero_when_nothing_matches_and_no_number_without_settings(self, wsgi, library):
        assert tabs(wsgi.get('/').text)[2][0] == '指定推荐'
        choose(tags=['还没有的标签'])
        assert tabs(wsgi.get('/').text)[2][0] == '指定推荐 0'
        assert tabs(wsgi.get('/pick').text)[2][0] == '指定推荐 0'

    def test_tab_number_follows_model_only(self, wsgi, library):
        choose(tags=['标签一'], model_only=True)
        assert tabs(wsgi.get('/').text)[2][0] == '指定推荐 3'
        rate('M-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        rate('M-004', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE)
        assert tabs(wsgi.get('/').text)[2][0] == '指定推荐 1'
        assert tabs(wsgi.get('/pick').text)[2][0] == '指定推荐 1'


class TestEmptyStates:
    def test_without_settings_the_page_points_at_the_settings(self, wsgi, library):
        text = wsgi.get('/pick').text
        soup = soup_of(text)
        assert soup.select_one('#pick-settings') is not None
        assert '还没有指定女优' in text and '还没有指定标签' in text
        assert covers_on(text) == []
        empty = soup.select_one('#pick-empty')
        assert empty.get_text(' ', strip=True).startswith('还没有设定')
        assert empty.select_one('a')['href'] == '#pick-settings'
        assert soup.select_one('#pick-total') is None
        assert '暂无数据' not in text and '暂时没有符合设定' not in text
        assert soup.select('#pick-liked-star, #pick-liked-tag') == []

    def test_settings_that_match_nothing_say_so(self, wsgi, library):
        choose(stars=['演员丙'], tags=['标签一'])
        text = wsgi.get('/pick').text
        assert covers_on(text) == []
        assert '暂时没有符合设定的未打标影片，抓到新影片后会自动出现在这里' in text
        assert soup_of(text).select_one('#pick-total').get_text(strip=True) == '符合设定的未打标影片共 0 部'
        assert soup_of(text).select_one('#pick-empty') is None


class TestSettingsForms:
    def test_adding_actresses_asks_for_a_round_that_reads_their_pages(self, wsgi, library, fill_requests):
        wsgi.post('/pick/add', {'kind': 'tag', 'names': '标签一'})
        wsgi.post('/pick/add', {'kind': 'star', 'names': '已经在列表里的,还没有的'})
        assert fill_requests == ['fill']
        wsgi.post('/pick/add', {'kind': 'star', 'names': '已经在列表里的'})
        assert fill_requests == ['fill']

    def test_add_a_star_then_a_tag(self, wsgi, library):
        response = wsgi.post('/pick/add', {'kind': 'star', 'names': '演员甲'})
        back_on_the_pick_page(response)
        text = follow(wsgi, response).text
        assert alerts(text) == ['已添加 1 个女优']
        assert chosen(text, 'star') == [('演员甲', '2 部', False)]
        assert covers_on(text) == ['M-003', 'M-001']
        text = follow(wsgi, wsgi.post('/pick/add', {'kind': 'tag', 'names': '标签一'})).text
        assert alerts(text) == ['已添加 1 个标签']
        assert chosen(text, 'tag') == [('标签一', '3 部', False)]
        assert covers_on(text) == ['M-001']
        assert pick.load_settings() == Settings(stars=('演员甲',), tags=('标签一',))

    def test_several_names_split_by_lines_and_both_kinds_of_comma(self, wsgi, library):
        response = wsgi.post('/pick/add', {'kind': 'star', 'names': ' 演员甲\r\n演员乙，演员丙 , 还没有的演员\n\n'})
        assert pick.load_settings().stars == ('演员甲', '演员乙', '演员丙', '还没有的演员')
        assert alerts(follow(wsgi, response).text) == ['已添加 4 个女优']

    def test_a_button_adds_exactly_its_own_name(self, wsgi, library):
        wsgi.post('/pick/add', {'kind': 'tag', 'name': '系列，带逗号, 的名字'})
        assert pick.load_settings().tags == ('系列，带逗号, 的名字',)

    def test_names_already_listed_are_reported(self, wsgi, library):
        choose(tags=['标签一', '4K'])
        response = wsgi.post('/pick/add', {'kind': 'tag', 'names': '4k, 标签一, 标签二'})
        assert alerts(follow(wsgi, response).text) == ['已添加 1 个标签，另有 2 个已经在列表里']
        response = wsgi.post('/pick/add', {'kind': 'tag', 'names': '标签二'})
        assert alerts(follow(wsgi, response).text) == ['1 个标签已经在列表里，没有重复添加']
        assert pick.load_settings().tags == ('标签一', '4K', '标签二')

    def test_too_long_names_are_reported(self, wsgi, library):
        response = wsgi.post('/pick/add', {'kind': 'star', 'names': '甲' * 200 + ',' + '乙' * 201})
        assert alerts(follow(wsgi, response).text) == ['已添加 1 个女优', '1 个名字超过 200 个字符或带有控制字符，没有添加']
        assert pick.load_settings().stars == ('甲' * 200,)

    def test_a_long_name_that_is_in_the_library_can_be_chosen(self, wsgi, make_item):
        # 库里有 68 个字符的系列名：联想和"从你喜欢的影片里选"都会列出它，也要加得进设定
        long_name = '系列' + '很长的名字' * 13 + '甲'
        assert len(long_name) == 68
        make_item('LONG-001', tags=[('系列:', long_name)])
        make_item('LONG-002', tags=[('系列:', long_name)])
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'LONG-002')
        assert offered(wsgi.get('/pick').text, 'tag') == [(long_name, 1)]
        suggested = json.loads(wsgi.get('/pick/suggest?kind=tag&q=' + quote('很长')).text)
        assert suggested == [{'value': long_name, 'type': '系列', 'count': 2}]
        text = follow(wsgi, wsgi.post('/pick/add', {'kind': 'tag', 'name': long_name})).text
        assert alerts(text) == ['已添加 1 个标签']
        assert chosen(text, 'tag') == [(long_name, '2 部', False)]
        assert covers_on(text) == ['LONG-001']

    def test_cells_copied_from_a_table_are_separate_names(self, wsgi, library):
        response = wsgi.post('/pick/add', {'kind': 'star', 'names': '演员甲\t演员乙\t演员丙'})
        assert alerts(follow(wsgi, response).text) == ['已添加 3 个女优']
        assert pick.load_settings().stars == ('演员甲', '演员乙', '演员丙')

    def test_more_than_a_hundred_names_are_reported(self, wsgi, library):
        pick.add_names('tag', [f'标签{n}' for n in range(99)])
        response = wsgi.post('/pick/add', {'kind': 'tag', 'names': '新标签一,新标签二,新标签三'})
        assert alerts(follow(wsgi, response).text) == ['已添加 1 个标签', '指定标签最多 100 个，有 2 个没有添加']
        assert len(pick.load_settings().tags) == 100
        text = wsgi.get('/pick').text
        assert '指定标签（100 / 100）' in text

    @pytest.mark.parametrize('data', [{'kind': 'star'}, {'kind': 'star', 'names': ''},
                                      {'kind': 'star', 'names': '  \n ,，\u3000'}, {'kind': 'star', 'name': ''}],
                             ids=['missing', 'empty', 'only-blanks-and-separators', 'empty-button'])
    def test_nothing_to_add_asks_for_a_name(self, wsgi, data_dir, data):
        response = wsgi.post('/pick/add', data)
        back_on_the_pick_page(response)
        assert alerts(follow(wsgi, response).text) == ['请先填写要添加的女优']
        assert not (data_dir / 'recommend.json').exists()

    def test_a_huge_paste_is_refused(self, wsgi, data_dir):
        # 上限是"每类 100 个名字、每个 200 个字符，加上分隔符"
        response = wsgi.post('/pick/add', {'kind': 'tag', 'names': ','.join(f'标签{n}' for n in range(4000))})
        back_on_the_pick_page(response)
        assert alerts(follow(wsgi, response).text) == ['一次提交的内容太多（超过 20100 个字符），请分几次添加']
        assert not (data_dir / 'recommend.json').exists()

    @pytest.mark.parametrize('path, data', [
        ('/pick/add', {'names': '演员甲'}),
        ('/pick/add', {'kind': 'genre', 'names': '演员甲'}),
        ('/pick/remove', {'kind': '', 'name': '演员甲'}),
        ('/pick/remove', {'kind': 'stars', 'name': '演员甲'}),
    ])
    def test_unknown_kind_is_a_bad_request(self, wsgi, data_dir, path, data):
        choose(stars=['演员甲'])
        response = wsgi.post(path, data)
        assert response.status == 400
        assert '<title>请求有误</title>' in response.text
        assert '<p>提交的类别只能是女优或标签</p>' in response.text
        assert pick.load_settings().stars == ('演员甲',)

    def test_remove_one_name(self, wsgi, library):
        choose(stars=['演员甲', '演员乙'], tags=['演员甲'])
        response = wsgi.post('/pick/remove', {'kind': 'star', 'name': '演员甲'})
        back_on_the_pick_page(response)
        text = follow(wsgi, response).text
        assert alerts(text) == ['已去掉 1 个女优']
        assert chosen(text, 'star') == [('演员乙', '2 部', False)]
        assert chosen(text, 'tag') == [('演员甲', '2 部', False)]

    @pytest.mark.parametrize('data', [{'kind': 'tag', 'name': '标签九'}, {'kind': 'tag', 'name': ''}, {'kind': 'tag'}])
    def test_removing_a_name_that_is_not_listed_changes_nothing(self, wsgi, library, data):
        choose(tags=['标签一'])
        response = wsgi.post('/pick/remove', data)
        back_on_the_pick_page(response)
        text = follow(wsgi, response).text
        assert alerts(text) == ['要去掉的标签不在列表里，没有改动']
        assert chosen(text, 'tag') == [('标签一', '3 部', False)]

    def test_options_are_saved_and_shown(self, wsgi, library):
        choose(stars=['演员丙'], tags=['标签二'])
        soup = soup_of(wsgi.get('/pick').text)
        assert [radio['value'] for radio in soup.select('#pick-options input[name="mode"][checked]')] == ['all']
        assert soup.select('#pick-options input[name="model_only"][checked]') == []
        assert covers_on(str(soup)) == []
        response = wsgi.post('/pick/options', {'mode': 'any', 'model_only': '1'})
        back_on_the_pick_page(response)
        text = follow(wsgi, response).text
        soup = soup_of(text)
        assert alerts(text) == ['推荐设定已保存']
        assert [radio['value'] for radio in soup.select('#pick-options input[name="mode"][checked]')] == ['any']
        assert len(soup.select('#pick-options input[name="model_only"][value="1"][checked]')) == 1
        assert covers_on(text) == ['M-005', 'M-004', 'M-003', 'M-001']
        assert pick.load_settings() == Settings(stars=('演员丙',), tags=('标签二',), mode='any', model_only=True)
        wsgi.post('/pick/options', {'mode': 'all'})
        assert pick.load_settings() == Settings(stars=('演员丙',), tags=('标签二',))

    @pytest.mark.parametrize('data', [{}, {'mode': ''}, {'mode': 'both'}, {'mode': 'ALL', 'model_only': '1'}])
    def test_unknown_mode_is_a_bad_request(self, wsgi, data):
        choose(stars=['演员甲'], mode='any')
        response = wsgi.post('/pick/options', data)
        assert response.status == 400
        assert '<p>条件关系只能是「同时满足」或「满足任一」</p>' in response.text
        assert pick.load_settings() == Settings(stars=('演员甲',), mode='any')

    def test_clear_asks_first_and_goes_back_to_the_defaults(self, wsgi, library):
        choose(stars=['演员甲'], tags=['标签一'], mode='any', model_only=True)
        form = soup_of(wsgi.get('/pick').text).select_one('#pick-clear')
        assert (form['action'], form['method']) == ('/pick/clear', 'post')
        assert form['data-confirm'].startswith('确定清空')
        # 没有经过页面脚本的确认框就提交过来（脚本还没加载好，或者关掉了脚本）：先回一个确认页，设定不动
        asked = wsgi.post('/pick/clear')
        assert asked.status == 200
        soup = soup_of(asked.text)
        assert soup.select_one('#confirm-question').get_text(strip=True) == '确定清空推荐设定吗？'
        assert soup.select_one('#confirm-detail').get_text(strip=True) == '已选的女优和标签都会去掉，条件关系等选项回到默认。'
        confirm = soup.select_one('form#confirm-form')
        assert (confirm['action'], confirm['method']) == ('/pick/clear', 'post')
        assert [(field['name'], field['value']) for field in confirm.select('input')] == [('confirmed', '1')]
        assert confirm.select_one('button[type="submit"]').get_text(strip=True) == '清空设定'
        assert confirm.select_one('a')['href'] == '/pick'
        assert TOP_NAV_ACTIVE.findall(asked.text) == ['/']
        assert pick.load_settings() == Settings(stars=('演员甲',), tags=('标签一',), mode='any', model_only=True)
        # 页面脚本问过之后带着 confirmed=1 提交，确认页上的「清空设定」也是
        response = wsgi.post('/pick/clear', {'confirmed': '1'})
        back_on_the_pick_page(response)
        text = follow(wsgi, response).text
        assert alerts(text) == ['已清空推荐设定']
        assert chosen(text, 'star') == [] and chosen(text, 'tag') == []
        assert pick.load_settings() == Settings()

    def test_every_settings_form_posts_and_none_needs_a_script(self, wsgi, library):
        choose(stars=['演员甲'], tags=['标签一'])
        rate('M-005', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        soup = soup_of(wsgi.get('/pick').text)
        forms = {form.get('id'): form for form in soup.select('#pick-settings form')}
        assert set(forms) == {'pick-chosen-star', 'pick-add-star', 'pick-chosen-tag', 'pick-add-tag',
                              'pick-liked-star', 'pick-liked-tag', 'pick-options', 'pick-clear'}
        assert {form['method'] for form in forms.values()} == {'post'}
        assert {name: form['action'] for name, form in forms.items()} == {
            'pick-chosen-star': '/pick/remove', 'pick-chosen-tag': '/pick/remove',
            'pick-add-star': '/pick/add', 'pick-add-tag': '/pick/add',
            'pick-liked-star': '/pick/add', 'pick-liked-tag': '/pick/add',
            'pick-options': '/pick/options', 'pick-clear': '/pick/clear'}
        for name, kind in (('pick-chosen-star', 'star'), ('pick-add-tag', 'tag'), ('pick-liked-star', 'star')):
            assert forms[name].select_one('input[type="hidden"][name="kind"]')['value'] == kind
        assert all(form.select('button[type="submit"]') for form in forms.values())
        assert soup.select('#pick-settings [onclick], #pick-settings [onsubmit], #pick-settings script') == []
        # 联想由页面脚本列在输入框后面的 <ul> 里：不用浏览器自带的 <datalist>，没有脚本时这个列表不显示
        for kind in ('star', 'tag'):
            names = forms[f'pick-add-{kind}'].select_one('input[name="names"]')
            assert names['data-suggest'] == kind and names['autocomplete'] == 'off' and not names.has_attr('list')
            suggestions = names.find_next_sibling('ul')
            assert suggestions['id'] == f'pick-{kind}-list' and 'pick-suggest-list' in suggestions['class']
            assert suggestions.has_attr('hidden') and suggestions.get_text(strip=True) == ''
        assert soup.select('datalist') == []

    def test_the_suggestion_lists_are_left_out_of_the_tab_order(self, wsgi):
        # 列表长到出现滚动条时，Chromium 把它当成可以用键盘聚焦的元素：输入框里按一次 Tab 焦点先落到列表上，
        # 列表随即因为输入框失去焦点而收起，焦点就没了，要再按一次才到"添加"。tabindex="-1" 让 Tab 直接去"添加"
        soup = soup_of(wsgi.get('/pick').text)
        for kind in ('star', 'tag'):
            assert soup.select_one(f'#pick-{kind}-list')['tabindex'] == '-1'

    def test_the_two_modes_are_explained_with_an_example(self, wsgi):
        text = ' '.join(soup_of(wsgi.get('/pick').text).select_one('#pick-options').get_text(' ', strip=True).split())
        assert '同时满足' in text and '满足任一' in text and '只看在「推荐」里的' in text
        assert '只看模型也判为喜欢的' not in text
        assert '选了女优，影片里要有其中至少一位；选了标签，每个标签都要有' in text
        assert '有任意一位所选女优，或者带任意一个所选标签' in text
        assert '例如' in text


class TestChosenNames:
    def test_each_name_shows_how_many_movies_carry_it_whatever_their_rate(self, wsgi, library):
        rate('M-001', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        rate('M-003', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        choose(stars=['演员甲', '演员丙'], tags=['标签一', '4k', '系列甲'], mode='any')
        text = wsgi.get('/pick').text
        assert chosen(text, 'star') == [('演员甲', '2 部', False), ('演员丙', '1 部', False)]
        assert chosen(text, 'tag') == [('标签一', '3 部', False), ('4k', '1 部', False), ('系列甲', '1 部', False)]
        assert '指定女优（2 / 100）' in text and '指定标签（3 / 100）' in text
        assert soup_of(text).select('.pick-missing') == []

    def test_a_name_nobody_carries_is_marked_with_a_hint(self, wsgi, library):
        choose(stars=['演员甲', '还没有的演员'], tags=['学生'])
        text = wsgi.get('/pick').text
        assert chosen(text, 'star') == [('演员甲', '2 部', False), ('还没有的演员', '0 部', True)]
        assert chosen(text, 'tag') == [('学生', '0 部', True)]
        hints = [' '.join(hint.get_text(' ', strip=True).split()) for hint in soup_of(text).select('.pick-missing')]
        assert hints == ['红色的是库里还没有带这个名字的影片，请核对写法（简体、繁体都可以；日文的写法要和网站上的一样）'] * 2

    def test_a_name_written_in_simplified_characters_finds_the_traditional_tag(self, wsgi, make_item):
        make_item('T-001', tags=[('genre', '女大學生'), ('star', '演員甲')])
        make_item('T-002', tags=[('genre', '女大學生')])
        make_item('T-003', tags=[('genre', '別的')])
        text = follow(wsgi, wsgi.post('/pick/add', {'kind': 'tag', 'names': '女大学生'})).text
        assert chosen(text, 'tag') == [('女大学生', '2 部', False)]
        assert covers_on(text) == ['T-002', 'T-001']
        assert soup_of(text).select('.pick-missing') == []
        text = follow(wsgi, wsgi.post('/pick/add', {'kind': 'star', 'names': '演员甲'})).text
        assert chosen(text, 'star') == [('演员甲', '1 部', False)]
        assert covers_on(text) == ['T-001']
        assert tabs(wsgi.get('/').text)[2][0] == '指定推荐 1'

    def test_other_names_of_a_star_are_listed(self, wsgi, make_item, cast):
        make_item('L-001')
        rate('L-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        cast.legacy('L-001', 'jia', '演员甲', '演员甲读音', '旧名甲')
        make_item('C-001', tags=[('star', '旧名甲')])
        make_item('C-002', tags=[('star', '演员乙')])
        choose(stars=['演员甲', '演员乙'])
        text = wsgi.get('/pick').text
        notes = [' '.join(note.get_text(' ', strip=True).split()) for note in soup_of(text).select('.pick-alias')]
        assert notes == ['演员甲 含别名：演员甲读音、旧名甲']
        assert chosen(text, 'star') == [('演员甲', '2 部', False), ('演员乙', '1 部', False)]
        assert covers_on(text) == ['C-002', 'C-001']

    @staticmethod
    def notes(text):
        return [' '.join(note.get_text(' ', strip=True).split()) for note in soup_of(text).select('.pick-alias')]

    def test_a_name_she_is_also_called_selects_her_and_shows_her_name(self, wsgi, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', 'Actor A')
        for code in ('C-001', 'C-002'):
            cast.movie(code)
            cast.link(code, jia)
        cast.movie('C-003', ('演员乙', '/star/b1'))
        choose(stars=['えんいんこう', '演员乙'])
        text = wsgi.get('/pick').text
        assert chosen(text, 'star') == [('えんいんこう', '2 部', False), ('演员乙', '1 部', False)]
        assert self.notes(text) == ['えんいんこう 就是 演员甲（含别名：えんいんこう、Actor A）']
        assert covers_on(text) == ['C-003', 'C-002', 'C-001']
        # 卡片上每位女优一个名字
        assert text.count('badge badge-warning') == 3

    def test_her_own_name_lists_her_aliases(self, wsgi, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', 'Actor A')
        cast.movie('C-001')
        cast.link('C-001', jia)
        choose(stars=['演員甲'])
        assert self.notes(wsgi.get('/pick').text) == ['演員甲 含别名：えんいんこう、Actor A']

    def test_a_name_two_actresses_share_selects_both_and_says_who(self, wsgi, cast):
        jia = cast.actress('演员甲', 'jia', '共用读音')
        yi = cast.actress('演员乙', 'yi', '共用读音', 'Actor B')
        for code, actress in (('C-001', jia), ('C-002', yi)):
            cast.movie(code)
            cast.link(code, actress)
        cast.movie('C-003')
        choose(stars=['共用读音'])
        text = wsgi.get('/pick').text
        assert chosen(text, 'star') == [('共用读音', '2 部', False)]
        assert self.notes(text) == ['共用读音 是 2 位女优共用的名字，都算：演员甲（含别名：共用读音）、'
                                    '演员乙（含别名：共用读音、Actor B）']
        assert covers_on(text) == ['C-002', 'C-001']

    def test_names_saved_before_the_upgrade_still_work(self, wsgi, cast, data_dir):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう')
        cast.movie('C-001')
        cast.link('C-001', jia)
        (data_dir / 'recommend.json').write_text(json.dumps(
            {'version': 1, 'stars': ['えんいんこう'], 'tags': [], 'mode': 'all', 'model_only': False}), encoding='utf-8')
        text = wsgi.get('/pick').text
        assert chosen(text, 'star') == [('えんいんこう', '1 部', False)] and covers_on(text) == ['C-001']

    @pytest.mark.parametrize('name', ODD_NAMES)
    def test_odd_characters_are_escaped_everywhere(self, wsgi, make_item, name):
        make_item('M-001', tags=[('star', name), ('genre', name)])
        make_item('L-001', tags=[('star', name), ('genre', name)])
        rate('L-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        text = wsgi.get('/pick').text
        assert offered(text, 'star') == [(name, 1)] and offered(text, 'tag') == [(name, 1)]
        for kind, word in (('star', '女优'), ('tag', '标签')):
            response = wsgi.post('/pick/add', {'kind': kind, 'name': name})
            assert alerts(follow(wsgi, response).text) == [f'已添加 1 个{word}']
        assert pick.load_settings() == Settings(stars=(name,), tags=(name,))
        text = wsgi.get('/pick').text
        assert chosen(text, 'star') == [(name, '2 部', False)] and chosen(text, 'tag') == [(name, '2 部', False)]
        assert covers_on(text) == ['M-001']
        for raw in ('<script>alert(1)', '<c>', '"双引号" &', "'单引号'"):
            assert raw not in text
        assert soup_of(text).select('#pick-settings script') == []
        assert wsgi.post('/pick/remove', {'kind': 'star', 'name': name}).status == 303
        assert pick.load_settings() == Settings(tags=(name,))


class TestLikedChoices:
    def test_stars_and_tags_of_liked_movies_can_be_added_with_one_click(self, wsgi, make_item, library):
        make_item('L-001', tags=[('star', '演员甲'), ('star', '演员丁'), ('genre', '标签一'), ('製作商:', '厂牌甲')])
        make_item('L-002', tags=[('star', '演员甲'), ('genre', '标签一'), ('genre', '标签九')])
        for fanhao in ('L-001', 'L-002'):
            rate(fanhao, RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        rate('M-005', RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        text = wsgi.get('/pick').text
        assert '从你喜欢的影片里选' in text
        assert offered(text, 'star') == [('演员甲', 2), ('演员丁', 1)]
        assert offered(text, 'tag') == [('标签一', 2), ('厂牌甲', 1), ('标签九', 1)]
        text = follow(wsgi, wsgi.post('/pick/add', {'kind': 'star', 'name': '演员甲'})).text
        assert chosen(text, 'star') == [('演员甲', '4 部', False)]
        assert offered(text, 'star') == [('演员丁', 1)]
        assert offered(text, 'tag') == [('标签一', 2), ('厂牌甲', 1), ('标签九', 1)]
        assert covers_on(text) == ['M-003', 'M-001']

    def test_one_person_with_several_names_is_offered_once(self, wsgi, make_item, cast):
        make_item('L-001')
        rate('L-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        cast.legacy('L-001', 'jia', '演员甲', '演员甲读音', 'jia', '旧名甲')
        make_item('L-002', tags=[('star', '演员乙')])
        rate('L-002', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        assert offered(wsgi.get('/pick').text, 'star') == [('演员甲', 1), ('演员乙', 1)]
        text = follow(wsgi, wsgi.post('/pick/add', {'kind': 'star', 'name': '演员甲'})).text
        assert offered(text, 'star') == [('演员乙', 1)]
        notes = [' '.join(note.get_text(' ', strip=True).split()) for note in soup_of(text).select('.pick-alias')]
        assert notes == ['演员甲 含别名：演员甲读音、jia、旧名甲']

    def test_block_is_hidden_when_there_is_nothing_left_to_offer(self, wsgi, make_item):
        make_item('L-001', tags=[('star', '演员甲')])
        rate('L-001', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        assert offered(wsgi.get('/pick').text, 'star') == [('演员甲', 1)]
        assert soup_of(wsgi.get('/pick').text).select_one('#pick-liked-tag') is None
        choose(stars=['演员甲'])
        text = wsgi.get('/pick').text
        assert '从你喜欢的影片里选' not in text
        assert soup_of(text).select('#pick-liked-star, #pick-liked-tag') == []


class TestList:
    def test_cards_show_the_similarity_and_tag_buttons(self, wsgi, library):
        rate('M-005', RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        choose(tags=['标签一', '标签三'], mode='any')
        # M-002 在"推荐"里，M-004 在"不太像"里，M-001 模型还没给它打分
        db.save_scores([db.Score('M-004', 0.31, ['标签一'], ['4K']), db.Score('M-002', 0.92, ['演员乙', '标签一'], [])],
                       lambda total: 1)
        text = wsgi.get('/pick').text
        assert covers_on(text) == ['M-002', 'M-001', 'M-004']
        soup = soup_of(text)
        assert soup.select_one('#pick-total').get_text(strip=True) == '符合设定的未打标影片共 3 部'
        rows = soup.select('form[action^="/tag/"]')
        assert [form['action'] for form in rows] == ['/tag/M-002?page=1', '/tag/M-001?page=1', '/tag/M-004?page=1']
        assert [form.select_one('input[name="formid"]')['value'] for form in rows] == ['form-1', 'form-2', 'form-3']
        for form in rows:
            assert form['method'] == 'post'
            assert form.select_one('input[name="back"]')['value'] == 'pick'
            assert [(button['value'], button.get_text(strip=True)) for button in form.select('button[name="submit"]')] \
                == [('1', '喜欢'), ('0', '不喜欢')]
        assert cards(text) == [('M-002', '相似度 92%', '因为：演员乙、标签一', None), ('M-001', None, None, None),
                               ('M-004', '相似度 31%', '因为：标签一', '拉低分数的：4K')]
        assert [set(badge['class']) - {'badge'} for badge in soup.select('.item-score .badge')] == [
            {'badge-success'}, {'badge-secondary'}]
        assert '模型：' not in text
        assert 'M-001' in text and '作品 M-001' in text and '发行日期: 2026-01-01' in text
        assert text.count('badge badge-primary') == 6 and text.count('badge badge-warning') == 2

    def test_movies_the_model_has_not_scored_show_no_similarity(self, wsgi, library):
        rate('M-001', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        choose(stars=['演员甲'])
        for path in ('/', '/pick'):
            text = wsgi.get(path).text
            assert ('M-001', None, None, None) in cards(text)
            assert soup_of(text).select('.item-score') == [] and '因为：' not in text
        text = wsgi.get('/').text
        assert '作品 M-001' in text and '发行日期: 2026-01-01' in text
        assert text.count('badge badge-primary') == 2 and text.count('badge badge-warning') == 1

    def test_pages(self, wsgi, make_item):
        with db.db.atomic():
            for n in range(1, 26):
                make_item(f'M-{n:03d}', tags=[('star', '演员甲')])
        choose(stars=['演员甲'])
        first = wsgi.get('/pick').text
        assert covers_on(first) == [f'M-{n:03d}' for n in range(25, 15, -1)]
        assert '共  3页,25条' in first and '第1页' in first
        third = wsgi.get('/pick?page=3').text
        assert covers_on(third) == [f'M-{n:03d}' for n in range(5, 0, -1)]
        assert 'action="/tag/M-005?page=3"' in third

    def test_page_links_land_on_the_list_not_on_the_settings(self, wsgi, make_item):
        with db.db.atomic():
            for n in range(1, 26):
                make_item(f'M-{n:03d}', tags=[('star', '演员甲')])
                rate(f'M-{n:03d}', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        choose(stars=['演员甲'])
        soup = soup_of(wsgi.get('/pick?page=2').text)
        assert soup.select_one('#pick-total') is not None
        links = [link['href'] for link in soup.select('a[href^="?page="]')]
        assert links == ['?page=1#pick-total', '?page=1#pick-total', '?page=3#pick-total', '?page=3#pick-total']
        assert [option['value'] for option in soup.select('#pagenav option')] == [
            f'?page={n}#pick-total' for n in (1, 2, 3)]
        home = soup_of(wsgi.get('/?page=2').text)
        assert [link['href'] for link in home.select('a[href^="?page="]')] == [
            '?page=1&like=1', '?page=1&like=1', '?page=3&like=1', '?page=3&like=1']
        assert [option['value'] for option in home.select('#pagenav option')] == [f'?page={n}&like=1' for n in (1, 2, 3)]

    @pytest.mark.parametrize('query, page', [('page=2', 2), ('page=abc', 1), ('page=0', 1), ('page=-5', 1), ('page=', 1),
                                             ('page=4', 3), ('page=99999999999999999999', 3)])
    def test_page_parameter(self, wsgi, make_item, query, page):
        with db.db.atomic():
            for n in range(1, 26):
                make_item(f'M-{n:03d}', tags=[('star', '演员甲')])
        choose(stars=['演员甲'])
        text = wsgi.get('/pick?' + query).text
        assert f'第{page}页' in text
        assert covers_on(text)

    def test_model_only_is_explained_while_the_model_has_rated_nothing(self, wsgi, library):
        choose(tags=['标签一'], model_only=True)
        text = wsgi.get('/pick').text
        note = soup_of(text).select_one('.pick-model-idle')
        assert '库里还没有模型的推荐结果' in note.get_text(strip=True) and '暂时不起作用' in note.get_text(strip=True)
        assert covers_on(text) == ['M-004', 'M-002', 'M-001']
        assert soup_of(text).select_one('#pick-total').get_text(strip=True) == '符合设定的未打标影片共 3 部'
        rate('M-002', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        text = wsgi.get('/pick').text
        assert soup_of(text).select_one('.pick-model-idle') is None
        assert covers_on(text) == ['M-002']
        assert soup_of(text).select_one('#pick-total').get_text(strip=True) == '符合设定、也在「推荐」里的未打标影片共 1 部'
        pick.save_options('all', False)
        assert soup_of(wsgi.get('/pick').text).select_one('.pick-model-idle') is None


class TestTagFromTheList:
    def test_like_moves_the_movie_to_the_liked_tab_and_comes_back(self, wsgi, library, fill_requests):
        choose(stars=['演员甲', '演员乙'])
        assert covers_on(wsgi.get('/pick').text) == ['M-003', 'M-002', 'M-001']
        response = wsgi.post('/tag/M-002?page=1', {'submit': '1', 'formid': 'form-2', 'back': 'pick'})
        assert redirect_target(response) == ('/pick', {'page': '1'}, 'form-2')
        assert stored_rate('M-002') == (RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        assert covers_on(wsgi.get('/pick?page=1').text) == ['M-003', 'M-001']
        assert covers_on(wsgi.get('/tagit?like=1').text) == ['M-002']
        assert tabs(wsgi.get('/pick').text)[2][0] == '指定推荐 2'
        # 这部影片本来就有演员，不用补
        assert fill_requests == []

    def test_liking_a_movie_without_actresses_registers_it_for_their_names(self, wsgi, library, fill_requests):
        choose(tags=['4K'])
        assert covers_on(wsgi.get('/pick').text) == ['M-004']
        response = wsgi.post('/tag/M-004?page=1', {'submit': '1', 'formid': 'form-1', 'back': 'pick'})
        assert redirect_target(response) == ('/pick', {'page': '1'}, 'form-1')
        assert stored_rate('M-004') == (RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)
        assert fill_requests == ['fill']

    def test_dislike_replaces_the_model_rate(self, wsgi, library, fill_requests):
        choose(tags=['标签三'])
        rate('M-005', RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE)
        response = wsgi.post('/tag/M-005?page=1', {'submit': '0', 'formid': 'form-1', 'back': 'pick'})
        assert redirect_target(response) == ('/pick', {'page': '1'}, 'form-1')
        assert stored_rate('M-005') == (RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)
        assert ItemRate.select().count() == 1
        assert covers_on(wsgi.get('/pick').text) == []
        assert covers_on(wsgi.get('/tagit?like=0').text) == ['M-005']
        assert fill_requests == []

    def test_tagging_the_only_movie_on_the_last_page_lands_on_the_new_last_page(self, wsgi, make_item):
        with db.db.atomic():
            for n in range(1, 12):
                make_item(f'M-{n:03d}', tags=[('genre', '标签一')])
        choose(tags=['标签一'])
        assert covers_on(wsgi.get('/pick?page=2').text) == ['M-001']
        response = wsgi.post('/tag/M-001?page=2', {'submit': '1', 'formid': 'form-1', 'back': 'pick'})
        assert redirect_target(response) == ('/pick', {'page': '2'}, 'form-1')
        text = follow(wsgi, response).text
        assert covers_on(text) == [f'M-{n:03d}' for n in range(11, 1, -1)]
        assert '第1页' in text

    @pytest.mark.parametrize('back', ['https://evil.example/', '//evil.example/pick', '/\\evil.example', 'pick/', 'PICK',
                                      '/pick', 'local', ''])
    def test_only_the_pick_page_can_be_asked_for(self, wsgi, library, back):
        response = wsgi.post('/tag/M-001?page=2', {'submit': '1', 'formid': 'form-1', 'back': back})
        path, query, fragment = redirect_target(response)
        assert (path, query['page'], fragment) == ('/tagit', '2', 'form-1')
        assert 'evil' not in response.headers['location']

    def test_tagging_from_the_tag_page_still_returns_there(self, wsgi, library):
        response = wsgi.post('/tag/M-001?page=3', {'submit': '1', 'formid': 'form-4'})
        assert redirect_target(response)[0] == '/tagit'

    @pytest.mark.parametrize('data', [{'back': 'pick'}, {'submit': '2', 'back': 'pick'}])
    def test_bad_values_are_still_refused(self, wsgi, library, data):
        response = wsgi.post('/tag/M-001?page=1', data)
        assert response.status == 400
        assert stored_rate('M-001') is None

    def test_unknown_movie(self, wsgi):
        assert wsgi.post('/tag/NOPE-001?page=1', {'submit': '1', 'back': 'pick'}).status == 404


class TestSafety:
    @pytest.mark.parametrize('path, data', [
        ('/pick/add', {'kind': 'star', 'names': '演员乙'}),
        ('/pick/add', {'kind': 'tag', 'name': '标签二'}),
        ('/pick/remove', {'kind': 'star', 'name': '演员甲'}),
        ('/pick/options', {'mode': 'any', 'model_only': '1'}),
        ('/pick/clear', {'confirmed': '1'}),
    ])
    def test_forms_submitted_by_another_site_are_rejected(self, wsgi, data_dir, path, data):
        choose(stars=['演员甲'], tags=['标签一'])
        before = (data_dir / 'recommend.json').read_bytes()
        for headers in (CROSS_SITE, {'Origin': 'https://evil.example'}):
            response = wsgi.post(path, data, headers=headers)
            assert response.status == 403
            assert '<title>禁止访问</title>' in response.text
        assert (data_dir / 'recommend.json').read_bytes() == before
        assert wsgi.post(path, data, headers={'Sec-Fetch-Site': 'same-origin', 'Origin': 'http://127.0.0.1'}).status == 303

    def test_page_opened_from_another_site_still_works(self, wsgi, library):
        choose(stars=['演员甲'])
        assert wsgi.get('/pick', headers=CROSS_SITE).status == 200

    @pytest.mark.parametrize('path', ['/pick/add', '/pick/remove', '/pick/options', '/pick/clear'])
    def test_changes_need_a_post(self, wsgi, path):
        choose(stars=['演员甲'])
        response = wsgi.get(path + '?kind=star&name=' + quote('演员甲') + '&names=x&mode=any')
        assert response.status == 405
        assert '这个地址不能这样访问，请从页面上的链接或按钮进入' in response.text
        assert pick.load_settings() == Settings(stars=('演员甲',))

    def test_broken_settings_file_is_explained_and_pages_still_open(self, wsgi, data_dir, library, caplog):
        path = data_dir / 'recommend.json'
        path.write_bytes(b'{"version": 1, "stars": [')
        text = wsgi.get('/pick').text
        assert alerts(text) == [f'推荐设定文件 {path} 读不了或格式不对（不是有效的 JSON），已按没有设定处理；'
                                '重新添加女优或标签并保存后会覆盖它']
        assert chosen(text, 'star') == [] and covers_on(text) == []
        assert tabs(wsgi.get('/').text)[2][0] == '指定推荐'
        warnings = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
        assert len(warnings) == 1 and str(path) in warnings[0]
        text = follow(wsgi, wsgi.post('/pick/add', {'kind': 'star', 'names': '演员甲'})).text
        assert alerts(text) == ['已添加 1 个女优']
        assert json.loads(path.read_text(encoding='utf-8'))['stars'] == ['演员甲']

    @pytest.mark.parametrize('path, data', [
        ('/pick/add', {'kind': 'star', 'names': '演员乙'}),
        ('/pick/remove', {'kind': 'star', 'name': '演员甲'}),
        ('/pick/options', {'mode': 'any'}),
        ('/pick/clear', {'confirmed': '1'}),
    ])
    def test_settings_that_cannot_be_written_are_explained(self, wsgi, data_dir, monkeypatch, caplog, path, data):
        choose(stars=['演员甲'])

        def read_only(source, target, swap=None):
            raise PermissionError(errno.EACCES, '拒绝访问')

        monkeypatch.setattr(util, 'replace_file', read_only)
        response = wsgi.post(path, data)
        back_on_the_pick_page(response)
        text = follow(wsgi, response).text
        assert alerts(text) == [f'推荐设定没有保存：无法写入 {data_dir / "recommend.json"}，'
                                '请检查磁盘空间和这个文件的权限（具体原因见运行日志）']
        assert chosen(text, 'star') == [('演员甲', '0 部', True)]
        assert any('拒绝访问' in record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING)

    @pytest.mark.parametrize('query', [
        'note=<script>alert(1)</script>', 'note=added', 'note=added&kind=star&n=3', 'note=added&kind=tag&n=3&dup=2&bad=1',
        'note=added&kind=<b>&n=3', 'note=removed&kind=star', 'note=empty&kind=tag', 'note=absent&kind=tag',
        'note=saved', 'note=cleared', 'note=failed', 'note=toomuch', 'note=', 'note=AAAAAAAAAAAAAAAA',
    ])
    def test_nothing_written_into_the_address_can_make_up_a_note(self, wsgi, query):
        response = wsgi.get('/pick?' + query)
        assert response.status == 200
        assert alerts(response.text) == []
        assert '<script>alert(1)' not in response.text and '<b>' not in response.text

    def test_a_note_is_shown_once(self, wsgi, library):
        response = wsgi.post('/pick/add', {'kind': 'tag', 'names': '标签一, 标签一, ' + '长' * 201})
        token = back_on_the_pick_page(response)
        expected = ['已添加 1 个标签', '1 个名字超过 200 个字符或带有控制字符，没有添加']
        assert alerts(wsgi.get(f'/pick?note={token}').text) == expected
        # 刷新这个地址、把它收藏起来以后再打开：提示不再出现，设定照旧
        text = wsgi.get(f'/pick?note={token}').text
        assert alerts(text) == [] and chosen(text, 'tag') == [('标签一', '3 部', False)]

    def test_the_note_of_one_form_does_not_show_for_another(self, wsgi, library):
        first = back_on_the_pick_page(wsgi.post('/pick/add', {'kind': 'star', 'names': '演员甲'}))
        second = back_on_the_pick_page(wsgi.post('/pick/options', {'mode': 'any'}))
        assert first != second
        assert alerts(wsgi.get(f'/pick?note={second}').text) == ['推荐设定已保存']
        assert alerts(wsgi.get(f'/pick?note={first}').text) == ['已添加 1 个女优']


class TestSuggest:
    '''输入框的联想：GET /pick/suggest?kind=star|tag&q=文字，返回 JSON 数组'''

    @pytest.fixture
    def tagged(self, make_item):
        make_item('M-001', tags=[('star', '演员甲'), ('genre', '标签一'), ('製作商:', 'Studio One')])
        make_item('M-002', tags=[('star', '演员甲'), ('star', '演员乙'), ('genre', '标签一'), ('系列:', '演员系列')])
        make_item('M-003', tags=[('star', '演员乙'), ('genre', '标签一'), ('發行商:', 'Studio One')])
        make_item('M-004', tags=[('star', '演员甲'), ('genre', '标签二')])

    def suggest(self, wsgi, query):
        response = wsgi.get('/pick/suggest?' + query)
        assert response.headers['content-type'] == 'application/json; charset=UTF-8'
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['x-content-type-options'] == 'nosniff'
        return response.status, json.loads(response.text)

    def test_stars_matching_the_text_most_common_first(self, wsgi, tagged):
        assert self.suggest(wsgi, 'kind=star&q=' + quote('演员')) == (200, [
            {'value': '演员甲', 'type': '演员', 'count': 3}, {'value': '演员乙', 'type': '演员', 'count': 2}])
        assert self.suggest(wsgi, 'kind=star&q=' + quote('乙')) == (200, [{'value': '演员乙', 'type': '演员', 'count': 2}])
        assert self.suggest(wsgi, 'kind=star&q=' + quote('标签')) == (200, [])
        assert db.db.is_closed()

    def test_tags_of_every_type(self, wsgi, tagged):
        assert self.suggest(wsgi, 'kind=tag&q=' + quote('演员')) == (200, [
            {'value': '演员甲', 'type': '演员', 'count': 3}, {'value': '演员乙', 'type': '演员', 'count': 2},
            {'value': '演员系列', 'type': '系列', 'count': 1}])
        assert self.suggest(wsgi, 'kind=tag&q=sTUDIO') == (200, [
            {'value': 'Studio One', 'type': '制作商', 'count': 1}, {'value': 'Studio One', 'type': '发行商', 'count': 1}])

    def test_text_typed_in_simplified_characters_finds_traditional_values(self, wsgi, make_item):
        make_item('T-001', tags=[('genre', '女大學生'), ('star', '演員甲')])
        for text in ('学生', '學生'):
            assert self.suggest(wsgi, 'kind=tag&q=' + quote(text)) == (200, [
                {'value': '女大學生', 'type': '类别', 'count': 1}])
        assert self.suggest(wsgi, 'kind=star&q=' + quote('演员')) == (200, [
            {'value': '演員甲', 'type': '演员', 'count': 1}])

    @pytest.mark.parametrize('query', ['kind=tag', 'kind=tag&q=', 'kind=tag&q=%20%20', 'kind=tag&q=%ff%fe'],
                             ids=['no-q', 'empty-q', 'blank-q', 'q-not-utf8'])
    def test_without_text_the_most_common_come_back(self, wsgi, tagged, query):
        status, rows = self.suggest(wsgi, query)
        assert status == 200
        assert rows[:3] == [{'value': '演员甲', 'type': '演员', 'count': 3}, {'value': '标签一', 'type': '类别', 'count': 3},
                            {'value': '演员乙', 'type': '演员', 'count': 2}]
        assert len(rows) == 7

    def test_at_most_twenty(self, wsgi, make_item):
        make_item('M-001', tags=[('genre', f'标签{n:02d}') for n in range(25)])
        status, rows = self.suggest(wsgi, 'kind=tag&q=' + quote('标签'))
        assert (status, len(rows)) == (200, 20)

    @pytest.mark.parametrize('query', ['', 'q=x', 'kind=', 'kind=genre', 'kind=STAR', 'kind=' + quote('乱写'),
                                       'kind=star%2Ctag&q=x', 'kind=%ff'])
    def test_unknown_kind_is_a_bad_request_in_json(self, wsgi, tagged, query):
        status, body = self.suggest(wsgi, query)
        assert (status, body) == (400, {'error': 'kind 只能是 star 或 tag'})

    def test_text_longer_than_a_name_finds_nothing(self, wsgi, tagged):
        assert self.suggest(wsgi, 'kind=tag&q=' + quote('标' * 201)) == (200, [])
        assert self.suggest(wsgi, 'kind=tag&q=' + 'x' * 5000) == (200, [])

    @pytest.mark.parametrize('name', ODD_NAMES + ['100%_纯\\', '\u2028行分隔符'])
    def test_odd_characters_arrive_intact_and_cannot_become_markup(self, wsgi, make_item, name):
        make_item('M-001', tags=[('genre', name), ('genre', '普通标签')])
        response = wsgi.get('/pick/suggest?kind=tag&q=' + quote(name))
        assert json.loads(response.text) == [{'value': name, 'type': '类别', 'count': 1}]
        for raw in ('<', '>', '&'):
            assert raw not in response.text

    def test_aliases_are_suggested_and_say_whose(self, wsgi, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', '<b>别名</b>')
        for code in ('C-001', 'C-002'):
            cast.movie(code)
            cast.link(code, jia)
        assert self.suggest(wsgi, 'kind=star&q=' + quote('エン')) == (200, [])
        assert self.suggest(wsgi, 'kind=star&q=' + quote('えんいん')) == (200, [
            {'value': 'えんいんこう', 'type': '演员', 'count': 2, 'alias_of': ['演员甲']}])
        response = wsgi.get('/pick/suggest?kind=star&q=' + quote('别名'))
        assert json.loads(response.text) == [{'value': '<b>别名</b>', 'type': '演员', 'count': 2, 'alias_of': ['演员甲']}]
        assert '<b>' not in response.text
        assert self.suggest(wsgi, 'kind=tag&q=' + quote('えんいん')) == (200, [])

    def test_the_script_shows_whose_alias_a_suggestion_is(self, wsgi):
        script = wsgi.get('/static/js/bustag.js').text
        assert 'alias_of' in script and 'also_alias_of' in script and '的别名' in script

    def test_only_get_is_allowed(self, wsgi):
        assert wsgi.post('/pick/suggest?kind=star', {}).status == 405

    def test_page_loads_the_script_that_asks_for_suggestions(self, wsgi):
        text = wsgi.get('/pick').text
        assert '<script type="text/javascript" src="/static/js/bustag.js"></script>' in text
        script = wsgi.get('/static/js/bustag.js').text
        assert '/pick/suggest' in script and 'data-suggest' in script and 'data-confirm' in script
        assert '.pick-suggest-list' in script and '.pick-suggest-list' in wsgi.get('/static/css/bustag.css').text
        # 粘贴进来的换行、制表符都当成名字之间的分隔
        assert r'/[\r\n\t]/' in script
