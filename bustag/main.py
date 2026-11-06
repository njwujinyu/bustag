'''
命令行入口：python -m bustag.main download | recommend | check
'''
import os
import sys
import time
import warnings

import click
import requests

from bustag.model.persist import load_model
from bustag.spider import bus_spider
from bustag.spider.covers import cover_referer, cover_source_url, validate_image
from bustag.spider.db import DB_FILE, Item, ItemRate, RATE_TYPE
from bustag.spider.fetcher import BlockedError, FetchError, create_fetcher
from bustag.spider.parser import ParseError, parse_item, parse_list
from bustag.util import (APP_CONFIG, DEFAULT_ROOT_PATH, MODEL_PATH, dead_domain_warning,
                         get_config_path, get_data_dir, get_data_path)

OK = '✅'
FAIL = '❌'
WARN = '⚠️'


@click.command()
def recommend():
    '''
    根据现有模型预测推荐数据
    '''
    # 模型模块要导入 scikit-learn，较慢，只在用到时导入
    import bustag.model.classifier as clf

    try:
        clf.recommend()
    except FileNotFoundError:
        click.echo('还没有训练好的模型, 无法推荐')


@click.command()
@click.option('--count', type=click.IntRange(min=1), help='最多新增多少部影片，默认取 config.ini 的 count')
def download(count):
    '''
    抓取新影片
    '''
    result = bus_spider.download(count=count)
    if not result.ok:
        sys.exit(1)


def _report(results, status, name, text, advice=None):
    results.append(status)
    click.echo(f'{status} {name}：{text}')
    if advice:
        click.echo(f'   建议：{advice}')


def _check_config(results):
    _report(results, OK, '数据目录', get_data_dir())
    _report(results, OK, '配置文件', get_config_path())
    root_path = APP_CONFIG['download.root_path']
    if dead_domain_warning(root_path):
        _report(results, FAIL, 'root_path', f'{root_path} 是已经失效的旧域名',
                f'打开 {get_config_path()}，把 root_path 改成 {DEFAULT_ROOT_PATH}')
    else:
        _report(results, OK, 'root_path', root_path)
    proxy = APP_CONFIG.get('options.proxy', '').strip()
    if proxy:
        _report(results, OK, '代理', f'{proxy}（config.ini 里设置）')
    else:
        env_proxies = requests.utils.get_environ_proxies(root_path)
        system_proxy = env_proxies.get(root_path.split(':', 1)[0]) or env_proxies.get('all')
        if system_proxy:
            _report(results, OK, '代理', f'没有设置，使用系统 / 环境变量里的代理 {system_proxy}')
        else:
            _report(results, OK, '代理', '没有设置，也没有系统 / 环境变量代理（直连）')


def _check_site(results):
    fetcher = create_fetcher(retries=1)
    try:
        start = time.monotonic()
        try:
            html = fetcher.get_html('/')
        except FetchError as ex:
            kind = '被拦截' if isinstance(ex, BlockedError) else '失败'
            _report(results, FAIL, '连通', f'GET {fetcher.url_for("/")} {kind}：{ex}')
            click.echo('   后面的联网检查已跳过')
            return
        _report(results, OK, '连通',
                f'GET {fetcher.url_for("/")} → 200，用时 {time.monotonic() - start:.2f} 秒，没有被拦截')

        paths = parse_list(html)
        if not paths:
            _report(results, FAIL, '列表解析', '首页没有找到影片链接',
                    '页面结构可能变了，请到项目 Issues 反馈')
            return
        _report(results, OK, '列表解析', f'首页找到 {len(paths)} 个影片链接（正常约 30 个）')

        try:
            meta, tags = parse_item(fetcher.get_html(paths[0]))
        except (FetchError, ParseError) as ex:
            _report(results, FAIL, '详情解析', f'{paths[0]}：{ex}')
            return
        genres = sum(tag.type == 'genre' for tag in tags)
        stars = sum(tag.type == 'star' for tag in tags)
        _report(results, OK, '详情解析',
                f'{meta["fanhao"]} {meta["title"][:20]}，类别 {genres} 个，演员 {stars} 个，'
                f'封面 {meta["cover_img_url"] or "无"}')

        source = cover_source_url(meta['cover_img_url'])
        if not source:
            _report(results, FAIL, '封面', f'{meta["fanhao"]} 的详情页里没有封面地址')
            return
        try:
            data, content_type = fetcher.get_bytes(source, referer=cover_referer())
        except FetchError as ex:
            _report(results, FAIL, '封面', f'{source}：{ex}')
            return
        error = validate_image(data, content_type)
        if error:
            _report(results, FAIL, '封面', f'{source}：{error}')
        else:
            _report(results, OK, '封面', f'{source} → {content_type}，{len(data) / 1024:.1f} KB')
    finally:
        fetcher.close()


def _check_model(results):
    model_file = os.path.normpath(get_data_path(MODEL_PATH + 'model.pkl'))
    if not os.path.exists(model_file):
        _report(results, WARN, '模型', '还没有训练模型', '打标 200 条以上后，在网页的"模型"页训练')
        return
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        try:
            load_model(model_file)
        except Exception as ex:
            _report(results, WARN, '模型', f'{model_file} 读取失败（{type(ex).__name__}），'
                    '多半是旧版本 scikit-learn 训练的，版本不兼容', '在网页的"模型"页重新训练')
            return
    import sklearn
    from sklearn.exceptions import InconsistentVersionWarning

    old = [w.message for w in caught if issubclass(w.category, InconsistentVersionWarning)]
    if old:
        _report(results, WARN, '模型',
                f'可以读取，但是用 scikit-learn {old[0].original_sklearn_version} 训练的'
                f'（当前 {sklearn.__version__}）', '在网页的"模型"页重新训练')
    else:
        _report(results, OK, '模型', f'可用（{model_file}）')


@click.command()
def check():
    '''
    自检：配置、网站连通、列表和详情解析、封面、数据库和模型
    '''
    results = []
    click.echo('== 配置')
    _check_config(results)
    click.echo('== 网站')
    _check_site(results)
    click.echo('== 数据')
    items = Item.select().count()
    rated = ItemRate.select().where(ItemRate.rate_type == RATE_TYPE.USER_RATE).count()
    _report(results, OK, '数据库', f'{get_data_path(DB_FILE)}：影片 {items} 部，打标 {rated} 条')
    _check_model(results)
    if FAIL in results:
        click.echo('有检查项失败，请按上面的建议处理')
        sys.exit(1)
    click.echo('检查完成，关键项全部正常')


@click.group()
def main():
    '''
    bustag 命令行
    '''


main.add_command(download)
main.add_command(recommend)
main.add_command(check)

if __name__ == "__main__":
    main()
