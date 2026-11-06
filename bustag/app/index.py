import os
import re
import signal
import socket
import sys
from multiprocessing import freeze_support
from urllib.parse import urlparse

if __name__ == '__main__' and not getattr(sys, 'frozen', False):
    # python bustag/app/index.py：sys.path 里只有脚本所在目录，要把仓库根目录加进去才能导入 bustag
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bottle
from bottle import route, template, static_file, request, redirect, hook, abort
from requests.utils import requote_uri
from waitress import create_server

import bustag.model.classifier as clf
from bustag import __version__
from bustag.app.local import add_local_fanhao, load_tags_db, remove_stale_uploads, save_upload
from bustag.app.schedule import start_scheduler, add_download_job
from bustag.model.persist import ModelLoadError
from bustag.spider import db
from bustag.spider.covers import get_cover_file
from bustag.spider.db import (get_items, get_local_items, count_local_items_without_item, RATE_TYPE,
                              RATE_VALUE, ItemRate, Item, LocalItem, DBError, db as dbconn)
from bustag.util import logger, get_resource_dir, wait_for_enter_if_console

DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 8000
# 封面请求在缓存未命中时要等下载，多留一些线程，避免页面请求排在封面后面
SERVER_THREADS = 8
COVER_CACHE_CONTROL = 'public, max-age=2592000'
# page 参数的上限，再大的值传给 SQLite 会溢出
MAX_PAGE = 1000000
RESOURCE_DIR = get_resource_dir()
STATIC_DIR = os.path.join(RESOURCE_DIR, 'static')
bottle.TEMPLATE_PATH.insert(0, os.path.join(RESOURCE_DIR, 'views'))
# 所有模板都能直接用 {{version}}（页脚显示版本号）
bottle.BaseTemplate.defaults['version'] = __version__


@hook('before_request')
def _connect_db():
    dbconn.connect(reuse_if_open=True)


@hook('after_request')
def _close_db():
    if not dbconn.is_closed():
        dbconn.close()


@route('/static/<filepath:path>')
def send_static(filepath):
    return static_file(filepath, root=STATIC_DIR)


@route('/cover/<fanhao>')
def cover(fanhao):
    fanhao = fanhao.upper()
    if Item.get_by_fanhao(fanhao) is None:
        abort(404, '没有这个番号')
    cover_file = get_cover_file(fanhao)
    if cover_file is None:
        # 下载失败时返回占位图，不让浏览器缓存，下次刷新页面再重试
        return static_file('images/nocover.svg', root=STATIC_DIR, mimetype='image/svg+xml',
                           headers={'Cache-Control': 'no-store'})
    path, mimetype = cover_file
    return static_file(os.path.basename(path), root=os.path.dirname(path), mimetype=mimetype,
                       headers={'Cache-Control': COVER_CACHE_CONTROL})


def _remove_extra_tags(item):
    limit = 10
    tags_dict = item.tags_dict
    tags = ['genre', 'star']
    for t in tags:
        tags_dict[t] = tags_dict[t][:limit]


def _page_param():
    '''page 参数：不是整数或小于 1 时按第 1 页处理'''
    try:
        page = int(request.query.get('page', 1))
    except ValueError:
        return 1
    return min(max(page, 1), MAX_PAGE)


def _like_param(default):
    '''like 参数只认 0 和 1，其他取值（没有、空、None 等）用 default'''
    value = request.query.get('like')
    return int(value) if value in ('0', '1') else default


def _submit_value():
    '''表单按钮的值只接受 0 和 1'''
    value = request.POST.submit
    if value not in ('0', '1'):
        abort(400, '提交的值只能是 0 或 1')
    return int(value)


def _form_anchor():
    '''重定向后回到原来的表单位置，formid 只接受 form-数字'''
    formid = request.POST.formid
    return f'#{formid}' if re.fullmatch(r'form-\d{1,4}', formid) else ''


@route('/')
def index():
    rate_type = RATE_TYPE.SYSTEM_RATE.value
    rate_value = _like_param(RATE_VALUE.LIKE.value)
    page = _page_param()
    items, page_info = get_items(
        rate_type=rate_type, rate_value=rate_value, page=page)
    for item in items:
        _remove_extra_tags(item)
    today_update_count = db.get_today_update_count()
    today_recommend_count = db.get_today_recommend_count()
    msg = f'今日更新 {today_update_count} , 今日推荐 {today_recommend_count}'
    return template('index', items=items, page_info=page_info, like=rate_value, path=request.path, msg=msg)


@route('/tagit')
def tagit():
    rate_value = _like_param(None)
    rate_type = None if rate_value is None else RATE_TYPE.USER_RATE
    page = _page_param()
    items, page_info = get_items(
        rate_type=rate_type, rate_value=rate_value, page=page)
    for item in items:
        _remove_extra_tags(item)
    return template('tagit', items=items, page_info=page_info, like=rate_value, path=request.path)


@route('/tag/<fanhao>', method='POST')
def tag(fanhao):
    rate_value = _submit_value()
    if Item.get_by_fanhao(fanhao) is None:
        abort(404, '没有这个番号')
    # 打标页"喜欢 / 不喜欢"标签页上的按钮就是用来改自己的打标的，所以覆盖已有的用户打标；
    # 页面打开期间定时推荐可能已经给它打过系统评分，也一起覆盖
    outcome = ItemRate.save_user_rate(fanhao, rate_value, replace_user_rate=True)
    logger.debug(f'打标 {fanhao}：{outcome}')
    redirect(f'/tagit?page={_page_param()}&like={_like_param(None)}{_form_anchor()}')


@route('/correct/<fanhao>', method='POST')
def correct(fanhao):
    is_correct = _submit_value()
    item_rate = ItemRate.get_by_fanhao(fanhao)
    if not item_rate:
        abort(404, '没有这个番号的推荐记录')
    item_rate.rate_type = RATE_TYPE.USER_RATE
    if not is_correct:
        rate_value = item_rate.rate_value
        rate_value = 1 if rate_value == 0 else 0
        item_rate.rate_value = rate_value
    item_rate.save()
    logger.debug(
        f'updated item fanhao: {fanhao}, {"and correct the rate_value" if not is_correct else ""}')
    redirect(f'/?page={_page_param()}&like={_like_param(RATE_VALUE.LIKE.value)}{_form_anchor()}')


@route('/model')
def other_settings():
    error_msg = None
    model_scores = None
    try:
        _, model_scores = clf.load()
    except FileNotFoundError:
        pass
    except ModelLoadError as ex:
        logger.warning(ex)
        error_msg = str(ex)
    return template('model', path=request.path, model_scores=model_scores, error_msg=error_msg)


@route('/do-training')
def do_training():
    error_msg = None
    model_scores = None
    try:
        _, model_scores = clf.train()
    except ValueError as ex:
        error_msg = ' '.join(ex.args)
        logger.warning(f'训练失败：{error_msg}')
    return template('model', path=request.path, model_scores=model_scores, error_msg=error_msg)


@route('/local_fanhao', method=['GET', 'POST'])
def update_local_fanhao():
    msg = ''
    errmsg = ''
    if request.POST.submit:
        fanhao_list = request.POST.fanhao
        tag_like = request.POST.tag_like == '1'
        missed_fanhao, local_file_count, tag_file_count = add_local_fanhao(
            fanhao_list, tag_like)
        add_download_job(missed_fanhao)
        parts = []
        if local_file_count:
            parts.append(f'{local_file_count} 个本地文件')
        if tag_like and tag_file_count:
            parts.append(f'{tag_file_count} 个番号打标为喜欢')
        if missed_fanhao:
            parts.append(f'{len(missed_fanhao)} 个番号加入下载队列')
        if parts:
            msg = '已提交：' + '，'.join(parts)
        else:
            errmsg = '没有新增内容：没有识别到番号，或者这些番号都已经提交过'
    return template('local_fanhao', path=request.path, msg=msg, errmsg=errmsg)


@route('/local')
def local():
    page = _page_param()
    items, page_info = get_local_items(page=page)
    for local_item in items:
        LocalItem.loadit(local_item)
        _remove_extra_tags(local_item.item)
    return template('local', items=items, page_info=page_info, pending=count_local_items_without_item(),
                    path=request.path)


@route('/local_play/<id:int>')
def local_play(id):
    local_item = LocalItem.get_or_none(LocalItem.id == id)
    if local_item is None:
        abort(404, '没有这个本地文件')
    file_path = local_item.path or ''
    parsed = urlparse(file_path)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or not file_path.isprintable():
        abort(400, '只能播放 http / https 地址的文件')
    LocalItem.update_play(id)
    logger.debug(file_path)
    # 地址里有中文或空格时，Location 头必须先做百分号编码
    redirect(requote_uri(file_path))


def _import_message(result):
    '''
    导入打标数据的结果提示，返回 (msg, errmsg)
    '''
    parts = []
    if result.imported:
        text = f'已导入 {result.imported} 条打标'
        if result.updated:
            text += f'（其中 {result.updated} 条原来是系统推荐）'
        parts.append(text)
    elif result.kept:
        parts.append('没有新的打标（都已导入过）')
    if result.imported and result.kept:
        parts.append(f'{result.kept} 条已有用户打标，没有改动')
    if result.missed_fanhaos:
        parts.append(f'{len(result.missed_fanhaos)} 个番号加入下载队列')
    if result.invalid:
        parts.append(f'跳过 {result.invalid} 条番号或评分不合法的打标')
    if result.imported or result.kept:
        return '，'.join(parts), ''
    return '', '，'.join(['上传的数据库里没有可导入的用户打标'] + parts)


@route('/load_db', method=['GET', 'POST'])
def load_db():
    msg = ''
    errmsg = ''
    if request.POST.submit:
        upload = request.files.get('dbfile')
        if upload:
            path = save_upload(upload)
            logger.debug(f'上传的 {upload.filename} 存为 {path}')
            try:
                result = load_tags_db(path)
            except DBError as ex:
                errmsg = f'数据库文件错误：{ex}。请选择 bustag 的 bus.db 文件'
            else:
                add_download_job(result.missed_fanhaos)
                msg, errmsg = _import_message(result)
        else:
            errmsg = '请上传数据库文件'
    return template('load_db', path=request.path, msg=msg, errmsg=errmsg)


@route('/about')
def about():
    return template('about', path=request.path)


app = bottle.default_app()


def create_app():
    '''
    清掉上次运行留下的上传临时文件，启动定时任务（只启动一次）并返回 WSGI 应用。
    用 gunicorn 时只能开 1 个 worker（定时任务在进程内）：
    gunicorn -w 1 "bustag.app.index:create_app()" --bind 0.0.0.0:8000
    '''
    remove_stale_uploads()
    start_scheduler()
    return app


class StartupError(Exception):
    '''启动参数有问题（端口无效、端口被占用等），消息直接给用户看'''


def _listen_address():
    host = os.environ.get('BUSTAG_HOST') or DEFAULT_HOST
    port_text = os.environ.get('BUSTAG_PORT') or str(DEFAULT_PORT)
    try:
        port = int(port_text)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise StartupError(f'环境变量 BUSTAG_PORT={port_text} 不是有效的端口号（1-65535）')
    return host, port


def _display_host(host):
    '''0.0.0.0 之类的监听地址不能直接在浏览器里打开，换成本机地址'''
    return '127.0.0.1' if host in ('0.0.0.0', '::', '[::]') else host


def _exit_on_sigterm():
    '''
    Docker 里 Python 是 PID 1，默认不处理 SIGTERM，docker stop 要等 10 秒才强行结束。
    收到 SIGTERM 时抛出 SystemExit，waitress 会接住它并关闭服务。
    '''
    sigterm = getattr(signal, 'SIGTERM', None)
    if sigterm is None:
        return

    def handler(signum, frame):
        raise SystemExit(0)

    try:
        signal.signal(sigterm, handler)
    except (ValueError, OSError):
        # 不在主线程里调用时不能注册信号处理函数
        pass


def _cannot_listen(host, port, ex):
    return StartupError(f'无法监听 {host}:{port}（{ex}）。端口可能已被占用，'
                        '可以设置环境变量 BUSTAG_PORT 换一个端口')


def _ensure_port_free(host, port):
    '''
    Windows 上 SO_REUSEADDR 允许两个进程绑定同一个端口，waitress 默认就设了它：
    第二个实例不会报错，请求会被两个实例随机分走。先用独占方式探测一下端口，被占用就报错。
    其他系统上 waitress 自己绑定失败就会报错，不需要探测。
    '''
    exclusive = getattr(socket, 'SO_EXCLUSIVEADDRUSE', None)
    if exclusive is None:
        return
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except OSError as ex:
        raise StartupError(f'BUSTAG_HOST={host} 不是有效的监听地址（{ex}）') from ex
    for family, socktype, proto, _, address in infos:
        probe = socket.socket(family, socktype, proto)
        try:
            probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            probe.bind(address)
        except OSError as ex:
            raise _cannot_listen(host, port, ex) from ex
        finally:
            probe.close()


def _serve():
    host, port = _listen_address()
    logger.info(f'bustag v{__version__} 启动')
    _ensure_port_free(host, port)
    try:
        server = create_server(app, host=host, port=port, threads=SERVER_THREADS)
    except OSError as ex:
        raise _cannot_listen(host, port, ex) from ex
    create_app()
    _exit_on_sigterm()
    logger.info(f'浏览器访问 http://{_display_host(host)}:{port}')
    server.run()
    logger.info('服务已停止')


def main():
    freeze_support()
    try:
        _serve()
    except KeyboardInterrupt:
        pass
    except StartupError as ex:
        logger.error(ex)
        wait_for_enter_if_console()
        sys.exit(1)
    except Exception:
        logger.exception('服务异常退出')
        wait_for_enter_if_console()
        sys.exit(1)


if __name__ == '__main__':
    main()
