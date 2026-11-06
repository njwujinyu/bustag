'''
网页：直接调用 WSGI 应用（不开端口）。封面下载走 FakeFetcher，启动相关的函数用假的 socket / 服务器
'''
import datetime
import errno
import json
import logging
import os
import pickle
import re
import subprocess
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import bottle
import pytest
import sklearn
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import MultiLabelBinarizer

import bustag
from bustag.app import index, schedule
from bustag.app.local import add_local_fanhao
from bustag.model import classifier, persist
from bustag.model.persist import BINARIZER_PATH, PREDICT_ERROR, ModelLoadError, read_model_files
from bustag.spider import covers, db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate, LocalItem
from bustag.spider.fetcher import FetchError

ROOT = Path(__file__).resolve().parent.parent
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 4096
SCORES = {'precision': 0.81, 'recall': 0.72, 'f1': 0.76}
TOP_NAV_ACTIVE = re.compile(r'<li class="nav-item active">\s*<a class="nav-link" href="([^"]+)"')
NOW = datetime.datetime(2026, 11, 6, 12, 0, 0)


def covers_on(text):
    '''页面上列出的影片（按封面地址里的番号，保持顺序）'''
    return re.findall(r'src="/cover/([^"]+)"', text)


def redirect_target(response):
    assert response.status == 303
    parts = urlsplit(response.headers['location'])
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}, parts.fragment


def save_model(data_dir, scores=SCORES, binarizer=None):
    '''写一份能用的 model.pkl 和 label_binarizer.pkl（模型用 g1、s1 两个标签训练过）；binarizer 换掉标签编码器'''
    model = KNeighborsClassifier(n_neighbors=1).fit([[1, 0], [0, 1]], [0, 1])
    (data_dir / classifier.MODEL_FILE).write_bytes(pickle.dumps(
        {'model': model, 'scores': scores, 'sklearn_version': sklearn.__version__}))
    (data_dir / BINARIZER_PATH).write_bytes(pickle.dumps(binarizer or MultiLabelBinarizer().fit([['g1', 's1']])))


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


class TestRecommendPage:
    def test_lists_system_recommendations(self, wsgi, make_item, monkeypatch):
        monkeypatch.setattr(db, 'get_now_time', lambda: NOW)
        for fanhao in ('ABC-001', 'ABC-002', 'ABC-003', 'ABC-004'):
            make_item(fanhao)
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE, 'ABC-002')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ABC-003')
        db.Item.update(add_date=NOW).execute()
        ItemRate.update(rete_time=NOW).execute()

        liked = wsgi.get('/')
        assert covers_on(liked.text) == ['ABC-001']
        assert '今日更新 4 , 今日推荐 1' in liked.text
        assert 'action="/correct/ABC-001?page=1&like=1"' in liked.text
        assert covers_on(wsgi.get('/?like=0').text) == ['ABC-002']

    @pytest.mark.parametrize('like', ['abc', '', '2', 'None'])
    def test_invalid_like_shows_liked(self, wsgi, make_item, like):
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
        ('page=99999999999999999999', index.MAX_PAGE),
    ])
    def test_page_parameter(self, wsgi, make_item, query, page):
        make_item('ABC-001')
        assert f'第{page}页' in wsgi.get('/tagit?' + query).text

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


class TestTagForm:
    def test_tag_unrated_movie(self, wsgi, make_item):
        make_item('ABC-001')
        response = wsgi.post('/tag/ABC-001?page=2&like=None', {'submit': '1', 'formid': 'form-3'})
        path, query, fragment = redirect_target(response)
        assert (path, query['page'], fragment) == ('/tagit', '2', 'form-3')
        rate = ItemRate.get_by_fanhao('ABC-001')
        assert (rate.rate_type, rate.rate_value) == (RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)

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
        assert 'value="0">不喜欢</button>' in wsgi.get('/tagit?like=1').text
        wsgi.post('/tag/ABC-001?like=1', {'submit': '0'})
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


class TestCorrectForm:
    @pytest.mark.parametrize('system, submit, expected', [
        (RATE_VALUE.LIKE, '1', RATE_VALUE.LIKE),
        (RATE_VALUE.LIKE, '0', RATE_VALUE.DISLIKE),
        (RATE_VALUE.DISLIKE, '1', RATE_VALUE.DISLIKE),
        (RATE_VALUE.DISLIKE, '0', RATE_VALUE.LIKE),
    ])
    def test_confirm_or_flip_recommendation(self, wsgi, make_item, system, submit, expected):
        make_item('ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, system, 'ABC-001')
        response = wsgi.post('/correct/ABC-001?page=2&like=1', {'submit': submit, 'formid': 'form-4'})
        path, query, fragment = redirect_target(response)
        assert (path, query, fragment) == ('/', {'page': '2', 'like': '1'}, 'form-4')
        rate = ItemRate.get_by_fanhao('ABC-001')
        assert (rate.rate_type, rate.rate_value) == (RATE_TYPE.USER_RATE, expected)

    def test_without_recommendation(self, wsgi, make_item):
        make_item('ABC-001')
        response = wsgi.post('/correct/ABC-001', {'submit': '1'})
        assert response.status == 404
        assert '没有这个番号的推荐记录' in response.text

    def test_rejects_other_values(self, wsgi, make_item):
        make_item('ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-001')
        response = wsgi.post('/correct/ABC-001', {'submit': '3'})
        assert response.status == 400
        assert ItemRate.get_by_fanhao('ABC-001').rate_type == RATE_TYPE.SYSTEM_RATE


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


class TestModelPage:
    def test_no_model_yet(self, wsgi):
        text = wsgi.get('/model').text
        assert '还没有训练过模型.' in text
        assert '准确率' not in text

    def test_shows_scores(self, wsgi, data_dir):
        save_model(data_dir)
        text = wsgi.get('/model').text
        assert '准确率: 0.81' in text
        assert '覆盖率: 0.72' in text
        assert '综合评分(越高越好): 0.76' in text

    def test_unreadable_model_shows_reason(self, wsgi, data_dir):
        (data_dir / classifier.MODEL_FILE).write_bytes(b'garbage')
        text = wsgi.get('/model').text
        assert '无法读取' in text
        assert '重新训练' in text
        assert '还没有训练过模型' not in text

    def test_old_format_holding_another_object_shows_reason(self, wsgi, data_dir):
        (data_dir / classifier.MODEL_FILE).write_bytes(pickle.dumps((MultiLabelBinarizer(), SCORES)))
        text = wsgi.get('/model').text
        assert '模型文件 model.pkl 格式不对' in text
        assert '准确率' not in text

    @pytest.mark.parametrize('content, reason', [
        (b'\x93\x1f\xa0\x07 not a pickle', '模型文件 label_binarizer.pkl 无法读取'),
        (pickle.dumps(KNeighborsClassifier()), '模型文件 label_binarizer.pkl 格式不对'),
    ], ids=['unreadable', 'not-a-binarizer'])
    def test_unusable_binarizer_shows_the_same_reason_as_recommend_and_check(self, wsgi, data_dir, content, reason):
        save_model(data_dir)
        (data_dir / BINARIZER_PATH).write_bytes(content)
        with pytest.raises(ModelLoadError, match=reason) as info:
            read_model_files(str(data_dir / classifier.MODEL_FILE), str(data_dir / BINARIZER_PATH))
        text = wsgi.get('/model').text
        assert f'<p class="card-text text-danger">{bottle.html_escape(str(info.value))} </p>' in text
        assert '重新训练' in text
        assert '准确率' not in text
        assert '还没有训练过模型' not in text

    def test_missing_binarizer_means_no_usable_model(self, wsgi, data_dir):
        save_model(data_dir)
        (data_dir / BINARIZER_PATH).unlink()
        text = wsgi.get('/model').text
        assert '还没有训练过模型.' in text
        assert '准确率' not in text

    def test_model_that_cannot_predict_shows_the_same_reason_as_recommend_and_check(self, wsgi, data_dir):
        save_model(data_dir, binarizer=MultiLabelBinarizer().fit([['x', 'y', 'z']]))
        text = wsgi.get('/model').text
        assert f'<p class="card-text text-danger">{bottle.html_escape(PREDICT_ERROR)} </p>' in text
        assert '准确率' not in text
        assert '还没有训练过模型' not in text

    def test_training(self, wsgi, data_dir, make_item):
        add_rated(make_item, 100, like=True)
        add_rated(make_item, 100, like=False)
        text = train(wsgi)
        assert '训练完成，下面是新模型的数据' in text
        f1 = re.search(r'综合评分\(越高越好\): ([\d.]+)', text)
        assert f1 and float(f1.group(1)) >= 0.9
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
        scores = re.search(r'准确率: ([\d.]+).*覆盖率: ([\d.]+).*综合评分\(越高越好\): ([\d.]+)', train(wsgi), re.S)
        # 删掉一半打标后重新训练失败，旧的 model.pkl 还在用
        ItemRate.delete().where(ItemRate.item_id.startswith('DIS')).execute()
        text = train(wsgi)
        assert '训练失败：训练数据不足, 无法训练模型. 需要200, 当前100' in text
        assert text.index('训练失败') < text.index('当前模型数据')
        assert f'准确率: {scores.group(1)}' in text
        assert f'覆盖率: {scores.group(2)}' in text
        assert f'综合评分(越高越好): {scores.group(3)}' in text

    def test_save_failure_keeps_showing_current_model(self, wsgi, data_dir, make_item, monkeypatch):
        add_rated(make_item, 100, like=True)
        add_rated(make_item, 100, like=False)
        scores = re.search(r'准确率: [\d.]+.*覆盖率: [\d.]+.*综合评分\(越高越好\): [\d.]+', train(wsgi), re.S).group(0)
        before = (data_dir / classifier.MODEL_FILE).read_bytes()
        real_write_temp = persist._write_temp

        def disk_full(path, write):
            if os.path.basename(path) == 'model.pkl':
                raise OSError(errno.ENOSPC, '磁盘空间不足')
            return real_write_temp(path, write)

        monkeypatch.setattr(persist, '_write_temp', disk_full)
        text = train(wsgi)
        assert re.search(r'训练失败：无法保存模型文件到 \S+（磁盘空间不足），原来的模型没有改动', text)
        assert scores in text
        assert '训练完成' not in text
        assert (data_dir / classifier.MODEL_FILE).read_bytes() == before

    def test_get_does_not_train(self, wsgi, monkeypatch):
        calls = []
        monkeypatch.setattr(classifier, 'train', lambda: calls.append('train'))
        response = wsgi.get('/do-training')
        assert response.status == 303
        assert urlsplit(response.headers['location']).path == '/model'
        assert calls == []

    def test_refreshing_the_result_page_does_not_train_again(self, wsgi, monkeypatch):
        calls = []
        monkeypatch.setattr(classifier, 'train', lambda: calls.append('train'))
        response = wsgi.post('/do-training')
        location = response.headers['location']
        for _ in range(2):
            assert '训练完成' in wsgi.get(location[location.index('/model'):]).text
        assert calls == ['train']

    @pytest.mark.parametrize('query', ['training=999999', 'training=abc', 'training=', 'training=<b>x</b>'])
    def test_unknown_training_result_shows_nothing(self, wsgi, query):
        text = wsgi.get('/model?' + query).text
        assert '训练完成' not in text
        assert '训练失败' not in text
        assert '<b>x</b>' not in text

    def test_only_recent_training_results_are_kept(self, wsgi, monkeypatch):
        monkeypatch.setattr(classifier, 'train', lambda: None)
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


class FakeProbe:
    '''代替 socket.socket：记录选项，bind 到 occupied 里的地址时抛 OSError'''
    occupied = set()
    created = []

    def __init__(self, family, socktype, proto):
        self.options = []
        self.bound = None
        self.closed = False
        FakeProbe.created.append(self)

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))

    def bind(self, address):
        if address in self.occupied:
            raise OSError(10048, '端口已被占用')
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

    FakeProbe.occupied = set()
    FakeProbe.created = []
    module = types.SimpleNamespace(SO_EXCLUSIVEADDRUSE=-5, SOL_SOCKET=0xffff, SOCK_STREAM=1, AI_PASSIVE=1,
                                   getaddrinfo=getaddrinfo, socket=FakeProbe, lookups=lookups)
    monkeypatch.setattr(index, 'socket', module)
    return module


class TestStartup:
    @pytest.mark.parametrize('env, expected', [
        ({}, ('0.0.0.0', 8000)),
        ({'BUSTAG_HOST': '127.0.0.1', 'BUSTAG_PORT': '8123'}, ('127.0.0.1', 8123)),
        ({'BUSTAG_HOST': '', 'BUSTAG_PORT': ''}, ('0.0.0.0', 8000)),
    ])
    def test_listen_address(self, monkeypatch, env, expected):
        monkeypatch.delenv('BUSTAG_HOST', raising=False)
        monkeypatch.delenv('BUSTAG_PORT', raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        assert index._listen_address() == expected

    @pytest.mark.parametrize('port', ['abc', '0', '65536', '-1', '80.5'])
    def test_invalid_port(self, monkeypatch, port):
        monkeypatch.setenv('BUSTAG_PORT', port)
        with pytest.raises(index.StartupError, match=re.escape(f'环境变量 BUSTAG_PORT={port} 不是有效的端口号（1-65535）')):
            index._listen_address()

    @pytest.mark.parametrize('host, shown', [
        ('0.0.0.0', '127.0.0.1'), ('::', '127.0.0.1'), ('[::]', '127.0.0.1'), ('192.168.1.5', '192.168.1.5'),
    ])
    def test_display_host(self, host, shown):
        assert index._display_host(host) == shown

    def test_free_port_is_probed_exclusively(self, fake_socket):
        index._ensure_port_free('0.0.0.0', 8000)
        probe, = FakeProbe.created
        assert probe.options == [(0xffff, -5, 1)]
        assert (probe.bound, probe.closed) == (('0.0.0.0', 8000), True)

    def test_occupied_port(self, fake_socket):
        FakeProbe.occupied = {('0.0.0.0', 8000)}
        with pytest.raises(index.StartupError, match='无法监听 0.0.0.0:8000.*BUSTAG_PORT'):
            index._ensure_port_free('0.0.0.0', 8000)
        assert FakeProbe.created[0].closed

    def test_invalid_host(self, fake_socket):
        with pytest.raises(index.StartupError, match='BUSTAG_HOST=bad host 不是有效的监听地址'):
            index._ensure_port_free('bad host', 8000)

    def test_no_probe_without_exclusive_bind(self, fake_socket):
        del fake_socket.SO_EXCLUSIVEADDRUSE
        index._ensure_port_free('0.0.0.0', 8000)
        assert fake_socket.lookups == []
        assert FakeProbe.created == []

    def test_sigterm_exits_cleanly(self, monkeypatch):
        handlers = {}
        fake_signal = types.SimpleNamespace(SIGTERM=15, signal=lambda sig, handler: handlers.update({sig: handler}))
        monkeypatch.setattr(index, 'signal', fake_signal)
        index._exit_on_sigterm()
        with pytest.raises(SystemExit) as info:
            handlers[15](15, None)
        assert info.value.code == 0

    @pytest.fixture
    def no_serve(self, monkeypatch):
        '''记录 create_server / create_app 的调用，不真的启动'''
        calls = []

        def create_server(app, host, port, threads):
            calls.append(('create_server', host, port, threads))
            return types.SimpleNamespace(run=lambda: calls.append(('run',)))

        monkeypatch.setattr(index, 'create_server', create_server)
        monkeypatch.setattr(index, 'create_app', lambda: calls.append(('create_app',)))
        monkeypatch.setattr(index, '_exit_on_sigterm', lambda: calls.append(('sigterm',)))
        monkeypatch.setattr(index, '_ensure_port_free', lambda host, port: calls.append(('probe', host, port)))
        monkeypatch.delenv('BUSTAG_HOST', raising=False)
        monkeypatch.delenv('BUSTAG_PORT', raising=False)
        return calls

    def test_main_serves_and_starts_scheduler_after_binding(self, no_serve, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        index.main()
        assert no_serve == [('probe', '0.0.0.0', 8000), ('create_server', '0.0.0.0', 8000, index.SERVER_THREADS),
                            ('create_app',), ('sigterm',), ('run',)]
        assert '浏览器访问 http://127.0.0.1:8000' in caplog.messages

    def test_main_exits_on_invalid_port(self, no_serve, monkeypatch, caplog):
        monkeypatch.setenv('BUSTAG_PORT', 'abc')
        with pytest.raises(SystemExit) as info:
            index.main()
        assert info.value.code == 1
        assert no_serve == []
        assert any('BUSTAG_PORT=abc' in m for m in caplog.messages)

    def test_main_exits_when_port_cannot_be_bound(self, no_serve, monkeypatch, caplog):
        def create_server(app, host, port, threads):
            raise OSError(98, 'Address already in use')

        monkeypatch.setattr(index, 'create_server', create_server)
        with pytest.raises(SystemExit) as info:
            index.main()
        assert info.value.code == 1
        assert ('create_app',) not in no_serve
        assert schedule.scheduler is None
        assert any('无法监听 0.0.0.0:8000' in m and 'BUSTAG_PORT' in m for m in caplog.messages)
