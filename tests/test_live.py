'''
真网站测试：用项目的 Fetcher 请求首页和首页第一部影片的详情页，检查解析结果，网站改版时这里会先失败。
默认跳过，设置环境变量 BUSTAG_LIVE_TEST=1 才运行
'''
import os
import re

import pytest

from bustag.spider import parser
from bustag.spider.fetcher import create_fetcher

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get('BUSTAG_LIVE_TEST') != '1', reason='设置环境变量 BUSTAG_LIVE_TEST=1 才请求真网站'),
]


@pytest.fixture(scope='module')
def pages():
    '''
    整个文件只发 2 个请求：首页、首页第一部影片的详情页。
    用默认配置（root_path、两次请求至少间隔 1.5 秒），代理走系统 / 环境变量；
    不重试，网络出错时请求数也不会超过 2 个
    '''
    fetcher = create_fetcher(retries=0)
    try:
        home = fetcher.get_html('/')
        paths = parser.parse_list(home)
        detail = fetcher.get_html(paths[0]) if paths else None
    finally:
        fetcher.close()
    return home, paths, detail


def test_home_page_lists_movies(pages):
    home, paths, _ = pages
    assert len(paths) >= 20, f'首页只找到 {len(paths)} 个影片链接'
    assert parser.has_next_page(home)


def test_first_movie_detail_parses(pages):
    _, paths, detail = pages
    assert detail is not None, '首页没有影片链接'
    meta, tags = parser.parse_item(detail)
    # 带日期后缀的那部，页面上的識別碼不带后缀
    assert meta['fanhao'].upper() == parser.DATED_SUFFIX.sub('', parser.fanhao_from_path(paths[0])).upper()
    assert meta['title']
    assert meta['cover_img_url']
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', meta['release_date'] or ''), meta['release_date']
    assert any(tag.type == 'genre' for tag in tags), tags
    assert all(tag.link.startswith('/') and '://' not in tag.link for tag in tags), tags
