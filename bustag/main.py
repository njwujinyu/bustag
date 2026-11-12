'''
命令行入口：python -m bustag.main download | recommend | check
'''
import os
import sys
import time

import click
import requests
from peewee import OperationalError

import bustag.model.classifier as clf
from bustag.model.persist import MODEL_FILE, ModelLoadError, read_model
from bustag.spider import bus_spider, db
from bustag.spider.covers import cover_referer, cover_source_url, validate_image
from bustag.spider.db import DB_FILE, Item, ItemRate, RATE_TYPE, busy_reason, is_busy
from bustag.spider.fetcher import BlockedError, FetchError, create_fetcher
from bustag.spider.parser import ParseError, parse_item, parse_list
from bustag.util import (APP_CONFIG, DEFAULT_ROOT_PATH, dead_domain_warning, get_config_path,
                         get_data_dir, get_data_path, get_log_path, redact_url)

OK = '✅'
FAIL = '❌'
WARN = '⚠️'


@click.command()
def recommend():
    '''
    用现有模型给没打标的影片打分并划分推荐，没能打分（没有模型、模型不能用）时退出码为 1
    '''
    try:
        result = clf.recommend()
    except FileNotFoundError:
        click.echo('还没有训练好的模型, 无法推荐')
        sys.exit(1)
    except ModelLoadError as ex:
        click.echo(f'无法推荐：{ex}')
        sys.exit(1)
    if result.scored:
        click.echo(f'已给 {result.scored} 部没有打标的影片打分：推荐 {result.recommended} 部，'
                   f'不太像 {result.scored - result.recommended} 部')
    else:
        click.echo('没有需要打分的影片')


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
    _report(results, OK, '运行日志', f'{get_log_path()}（网页服务运行时写在这里，命令行的输出只显示在控制台）')
    root_path = APP_CONFIG['download.root_path']
    if dead_domain_warning(root_path):
        _report(results, FAIL, 'root_path', f'{root_path} 是已经失效的旧域名',
                f'打开 {get_config_path()}，把 root_path 改成 {DEFAULT_ROOT_PATH}')
    else:
        _report(results, OK, 'root_path', root_path)
    proxy = APP_CONFIG.get('options.proxy', '').strip()
    if proxy:
        _report(results, OK, '代理', f'{redact_url(proxy)}（config.ini 里设置）')
    else:
        env_proxies = requests.utils.get_environ_proxies(root_path)
        system_proxy = env_proxies.get(root_path.split(':', 1)[0]) or env_proxies.get('all')
        if system_proxy:
            _report(results, OK, '代理', f'没有设置，使用系统 / 环境变量里的代理 {redact_url(system_proxy)}')
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
        # 只写番号和数量，不写标题和名字：这段输出常被原样贴出来求助
        _report(results, OK, '详情解析',
                f'{meta["fanhao"]}，标题 {len(meta["title"])} 个字，类别 {genres} 个，演员 {stars} 个，'
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
    '''和网页"模型"页、打分一样用 read_model 检查模型文件：读得出、内容对'''
    model_file = os.path.normpath(get_data_path(MODEL_FILE))
    try:
        read_model(model_file)
    except FileNotFoundError:
        _report(results, WARN, '模型', '还没有训练模型', '打标 200 条以上后，在网页的"模型"页训练')
    except ModelLoadError as ex:
        _report(results, WARN, '模型', str(ex))
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


class _Commands(click.Group):
    '''
    命令组。命令可以和网页服务同时运行，两边写的是同一个数据库：写锁被占着、等到超时还没轮到时，
    输出一行说明、以退出码 1 结束，不打印异常栈
    '''

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except OperationalError as ex:
            if not is_busy(ex):
                raise
            click.echo(f'{busy_reason(ex)}，这次没有做成，请稍后再试')
            ctx.exit(1)


@click.group(cls=_Commands)
def main():
    '''
    bustag 命令行
    '''
    # 打开库时别的程序占着写锁、新加的表没建成：命令开始前再建一次，这次等满 BUSY_TIMEOUT
    if db.tables_missing and not db.create_tables():
        click.echo(f'{db.TABLES_MISSING}，这次没有做成，请关掉正开着 bus.db 的程序后再试')
        sys.exit(1)


main.add_command(download)
main.add_command(recommend)
main.add_command(check)

if __name__ == "__main__":
    main()
