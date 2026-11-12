import functools
import http.client
import ipaddress
import itertools
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import types
from multiprocessing import freeze_support
from urllib.parse import quote, urlparse

if __name__ == '__main__' and not getattr(sys, 'frozen', False):
    # python bustag/app/index.py：sys.path 里只有脚本所在目录，要把仓库根目录加进去才能导入 bustag
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bottle
from bottle import route, template, static_file, request, response, redirect, hook
from peewee import OperationalError
from requests.utils import requote_uri
from waitress import create_server

import bustag.model.classifier as clf
from bustag import __version__
from bustag.app import instance, pick, schedule
from bustag.app.local import add_local_fanhao, load_tags_db, remove_stale_uploads, save_upload
from bustag.app.schedule import start_scheduler, request_actress_fill, request_queue_run
from bustag.main import main as command_line
from bustag.model import prepare
from bustag.model.persist import LegacyModelError, ModelLoadError, ModelSaveError
from bustag.spider import actresses, avwiki, backfill, bus_spider, db
from bustag.spider.covers import get_cover_file
from bustag.spider.db import (get_items, get_local_items, count_local_items_without_item, RATE_TYPE,
                              RATE_VALUE, ItemRate, Item, LocalItem, DBError, db as dbconn)
from bustag.spider.parser import is_star_page, is_valid_fanhao
from bustag.util import (APP_CONFIG, LOG_BACKUPS, LOG_MAX_BYTES, active_log_file, format_datetime, get_data_dir,
                         get_resource_dir, log_uncaught_exceptions, logger, start_file_log,
                         wait_for_enter_if_console)

# 默认只监听本机：程序没有登录，监听所有地址时同一个网络里的设备都能打开并修改
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8000
# 表示"所有网络地址"的监听地址。IPv6 的 :: 不支持：waitress 监听它时只收 IPv6，127.0.0.1 和局域网里常用的
# IPv4 地址都连不上
ALL_ADDRESSES = '0.0.0.0'
# 局域网用的网段。判断一个地址是不是局域网地址只认这三个：代理软件的虚拟网卡常用的 198.18.0.0/15 不算
LAN_NETWORKS = tuple(ipaddress.ip_network(network) for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
ON_WINDOWS = os.name == 'nt'
# 封面请求在缓存未命中时要等下载，多留一些线程，避免页面请求排在封面后面
SERVER_THREADS = 8
# 处理一个请求期间，waitress 默认不再读这条连接，也就发现不了浏览器已经断开；允许它多读 1 个请求，
# 排队等封面的请求才能知道浏览器走了（environ 里的 waitress.client_disconnected）。
# 代价：发完请求就把发送方向关掉（半关闭）再等答复的客户端，waitress 读到"对方不再发了"就当它已经走了，
# 这个请求收不到答复。浏览器不这样做，只有个别脚本和命令行工具会；而发现不了浏览器已经走了的话，
# 翻页以后上一页排着队的封面还会一张张下载下去，占着线程。两头只能顾一头，顾的是浏览器
REQUEST_LOOKAHEAD = 1
COVER_CACHE_CONTROL = 'public, max-age=2592000'
# page 参数的上限，再大的值传给 SQLite 会溢出
MAX_PAGE = 1000000
# 普通表单一次最多提交 10 MB（本地番号页一行"番号,地址"约 100 字节，约十万行），超过返回 413；
# 上传文件时这也是留在内存里的上限，更大的文件写到临时文件
MAX_FORM_SIZE = 10 * 1024 * 1024
bottle.BaseRequest.MEMFILE_MAX = MAX_FORM_SIZE
# "数据库正忙"页上跟在原因后面的话
BUSY_ADVICE = ('这次操作没有完成。请稍后再试；'
               '一直这样的话，看看是不是有别的程序（另一个 bustag、数据库工具）正开着 bus.db')
# 新版本要加的表还没建成（启动时库被别的程序占着）时，用到它们的页面上跟在原因后面的话
TABLES_ADVICE = ('这一页暂时打不开。关掉正开着 bus.db 的程序（另一个 bustag、数据库工具）以后，'
                 '程序会自己加上，过一会儿再刷新这一页')
# 启动时没建成的表，每隔这么多秒在后台再建一次（每次等写锁最多 db.BUSY_TIMEOUT 秒）
TABLES_RETRY = 10
# 错误页的标题，以及 bottle 自己产生的错误（英文原因）换成的中文原因
ERROR_TITLES = {400: '请求有误', 403: '禁止访问', 404: '找不到页面', 405: '不支持的请求方式',
                413: '提交的内容太大', 500: '程序出错', 503: '数据库正忙'}
ERROR_REASONS = {
    400: '提交的内容不对，请返回上一页重试',
    403: '不能访问这个地址',
    404: '没有这个页面，请检查网址',
    405: '这个地址不能这样访问，请从页面上的链接或按钮进入',
    413: f'提交的内容太大（超过 {MAX_FORM_SIZE // 1024 // 1024} MB），请分几次提交',
    500: '程序出错了，出错的详细信息已写进日志',
    503: f'{db.DATABASE_BUSY}，{BUSY_ADVICE}',
}
# 模型页只认最近这么多次训练的结果
MAX_TRAINING_RESULTS = 10
# 表单处理完跳回页面时要显示一次的提示最多留这么多条，再多就去掉最早的
MAX_NOTES = 100
OLD_MODEL = '现有的模型文件是旧版本程序训练的，新版本换了模型'
# 关于页上"上次失败的原因"最多显示这么多个字符（网络库的报错常常很长），完整的在运行日志里
MAX_REASON_LENGTH = 160
# 演员数据的升级没做成时什么时候再试（见 schedule._retry_upgrade）
RETRY_NOTE = '程序开着的话明天会自动再试一次（重启程序时也会再试）'
# 启动时看端口上是不是已经有程序在听：连本机，有人在听时马上就连上；没人在听时 Windows 要等到超时才返回，
# 所以只等这么多秒（每次启动都要等这一下）
CONNECT_TIMEOUT = 0.5
# 端口上有程序在听时去看它是不是 bustag：读"关于"页最多等这么多秒，页面最多读这么多字节
PROBE_TIMEOUT = 2
MAX_PROBE_SIZE = 200 * 1024
# 每个页面的页脚里都有这一句，带着版本号
BUSTAG_FOOTER = re.compile(r'bustag v([0-9][0-9A-Za-z.+-]*) · 维护版')
# 网页服务锁着自己的数据目录时，每个答复都带这个响应头（值是 1）。另一个 bustag 启动时发现端口被它占着，
# 看到这个头才能断定它用的是别的数据目录；版本号说明不了它上不上锁
LOCK_HEADER = 'X-Bustag-Data-Lock'
# gunicorn 的 worker 拿不到数据目录的锁时，最多等这么多秒（每隔 LOCK_RETRY_STEP 秒再试一次）
LOCK_WAIT = 10
LOCK_RETRY_STEP = 0.5
# 打标页上打标的结果（ItemRate.save_user_rate 的返回值）在日志里的说法
TAG_OUTCOMES = {'added': '新增', 'updated': '新增，原来是模型给的评分', 'changed': '改动，原来已经打过标'}
# 推荐设定里两类名字在页面上的叫法
PICK_KIND_NAMES = {'star': '女优', 'tag': '标签'}
# 整页标不喜欢可以从这几个列表页提交（表单里的 back），标完回到那一页；值是那一页在导航栏里算哪一项
BULK_PAGES = {'recommend': '/', 'pick': '/', 'tagit': '/tagit'}
# 指定推荐页一次添加最多提交这么多个字符：一类名字写满（100 个、每个 200 个字符）再加上分隔符
PICK_INPUT_LIMIT = pick.MAX_NAMES * (pick.MAX_NAME_LENGTH + 1)
# 按演员补全那一块里一行的键（actress:<编号>、javbus:<演员页>、avwiki:<女优页>、name:<名字>）最长这么多个字符
MAX_ROW_KEY = pick.MAX_NAME_LENGTH + 100
# 不会改数据的请求方式，跨站检查不管它们
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')
CROSS_SITE_REASON = '这个请求来自别的网站，已拒绝。会修改数据的操作只能在 bustag 自己的页面上进行'
ORIGIN_MISMATCH_REASON = ('请求的来源（Origin）和访问地址（Host）不一致，已拒绝。会修改数据的操作只能在 bustag 自己的页面上进行；'
                          '通过反向代理访问时，请让代理保留原来的 Host 头')
RESOURCE_DIR = get_resource_dir()
STATIC_DIR = os.path.join(RESOURCE_DIR, 'static')
bottle.TEMPLATE_PATH.insert(0, os.path.join(RESOURCE_DIR, 'views'))
# 所有模板都能直接用 {{version}}（页脚显示版本号）
bottle.BaseTemplate.defaults['version'] = __version__
# 封面由页面脚本排队加载，轮到之前 <img> 的 src 是这张 1×1 的透明 GIF，浏览器不会为它发请求（见 cover.tpl）
bottle.BaseTemplate.defaults['blank_image'] = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=='

# 训练编号 -> (训练失败的原因, 成功时绿色的提示, 成功时黄色的提示)，成功时第一个为 None、失败时后两个为空；
# 训练后跳回模型页时按网址里的编号取出
_training_results = {}
_training_ids = itertools.count(1)
_training_lock = threading.Lock()
# 表单处理完跳回页面时要显示一次的提示：编号 -> (成功的话, 出错的话)。文字留在这里，跳转的网址里只带编号；
# 页面一显示就从这里去掉，所以刷新、收藏那个网址都不会再出现；编号是随机的，网址里编不出提示来
_notes = {}
_notes_lock = threading.Lock()


class PageError(bottle.HTTPError):
    '''
    带中文原因的错误，原因原样显示在错误页上。home 为假时错误页上不给"返回首页"：首页自己也打不开的时候
    '''

    def __init__(self, status, reason, home=True):
        super().__init__(status, reason)
        self.home = home


def _abort(status, reason, home=True):
    raise PageError(status, reason, home)


def _origin_host(origin):
    '''Origin 头里的"主机:端口"（小写）；格式不对或者是 null 时返回 None'''
    try:
        return urlparse(origin).netloc.lower() or None
    except ValueError:
        return None


@hook('before_request')
def _reject_cross_origin():
    '''
    程序没有登录，用户浏览别的网页时，那个网页可以用自动提交的表单让浏览器向这里发 POST（改打标、训练、导入数据）。
    浏览器标明是跨站发出的这类请求一律 403：有 Sec-Fetch-Site 时只放行 same-origin 和 none（用户自己发起的）；
    较老的浏览器不发它，就要求 Origin 里的主机:端口等于 Host 头；两个头都没有的不是浏览器发的（命令行工具等），放行。
    规则和 Go 1.25 net/http 的 CrossOriginProtection 相同
    '''
    if request.method in SAFE_METHODS:
        return
    site = request.get_header('Sec-Fetch-Site')
    origin = request.get_header('Origin')
    if site:
        if site in ('same-origin', 'none'):
            return
        reason = CROSS_SITE_REASON
    elif not origin:
        return
    elif _origin_host(origin) == (request.get_header('Host') or '').lower():
        return
    else:
        reason = ORIGIN_MISMATCH_REASON
    logger.warning(f'拒绝跨站请求 {request.method} {request.path}（Sec-Fetch-Site: {site!r}，Origin: {origin!r}）')
    _abort(403, reason)


@hook('before_request')
def _connect_db():
    # 静态文件用不着数据库。连不上（老库被别的程序独占着）时这里就给"数据库正忙"：每试着打开一次都要等一会儿，
    # 不让页面查库时再等一遍
    if request.path.startswith('/static/'):
        return
    try:
        dbconn.connect(reuse_if_open=True)
    except OperationalError as ex:
        if db.locked_out(ex):
            logger.warning(f'{request.method} {request.path} 没有完成：{db.DATABASE_LOCKED_OUT}')
            _abort(503, f'{db.DATABASE_LOCKED_OUT}，{BUSY_ADVICE}', home=False)
        if not db.is_busy(ex):
            raise


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
        _abort(404, '没有这个番号')
    # 封面要排队下载时，浏览器可能已经翻页或关掉了页面：waitress 提供了查询的办法，别的服务器没有这一项
    cover_file = get_cover_file(fanhao, abandoned=request.environ.get('waitress.client_disconnected'))
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


def _attach_entries(item):
    item.other_entries = db.other_entries(item.fanhao)


# 改选详情、查找其他条目的结果 -> 跳回页面后显示的提示 (成功的话, 出错的话)
_ENTRY_NOTES = {
    'replaced': ('已改到所选详情。原来的喜欢或不喜欢留在这一条上，另一条不再占着这个打标。', ''),
    'found': ('已记下这个番号的其他条目，可以改选。', ''),
    'same': ('', '这已经是当前详情，没有改动'),
    'badpath': ('', '路径不是这个番号的详情。请贴 /番号 或 /番号_日期'),
    'fetch': ('', '没有改成：这一条的详情没有抓到'),
    'empty': ('', '没有找到这个番号的其他条目'),
}


def _leave_note(msg='', errmsg=''):
    '''记下跳回页面后要显示一次的提示 (成功的话, 出错的话)，返回放进网址的编号'''
    token = secrets.token_urlsafe(12)
    with _notes_lock:
        _notes[token] = (msg, errmsg)
        while len(_notes) > MAX_NOTES:
            del _notes[next(iter(_notes))]
    return token


def _take_note(name='note'):
    '''
    取出网址里的编号（参数名是 name）对应的提示 (成功的话, 出错的话)，取出就作废。
    没有编号、编号不对或者已经显示过时，两个都是空的
    '''
    token = request.query.get(name) or ''
    with _notes_lock:
        return _notes.pop(token, ('', ''))


def _search_text(value):
    '''
    搜索词：去掉两端空白，过长的只留前 100 个字符。空白表示不搜索。
    从请求里读时用 getunicode：bottle 先按 latin1 解开查询串和表单，中文要再转回 UTF-8
    '''
    return (value or '').strip()[:100]


def _search_suffix(value):
    text = _search_text(value)
    if not text:
        return ''
    return '&q=' + quote(text, safe='')


def _posted_page():
    '''表单里带回来的页码：1 到 7 位的数字，其他写法按第 1 页'''
    page = request.POST.get('page') or '1'
    return page if re.fullmatch(r'\d{1,7}', page) else '1'


def _posted_like():
    '''表单里带回来的推荐页的标签页：1（推荐）或 0（不太像），其他写法按 1'''
    like = request.POST.get('like')
    return like if like in ('0', '1') else str(RATE_VALUE.LIKE.value)


def _entry_redirect(outcome):
    '''改选详情、查找其他条目之后跳回原来的页（本地页，或者打标页原来的标签页、页码和搜索词），带着一次性的提示'''
    page = _posted_page()
    like = request.POST.get('like')
    if like not in ('0', '1'):
        like = ''
    note = _leave_note(*_ENTRY_NOTES[outcome])
    back = request.POST.get('back')
    if back == 'local':
        return f'/local?page={page}&note={note}'
    like_q = f'&like={like}' if like else ''
    return f'/tagit?page={page}{like_q}{_search_suffix(request.POST.getunicode("q"))}&note={note}'


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
        _abort(400, '提交的值只能是 0 或 1')
    return int(value)


def _form_anchor():
    '''重定向后回到原来的表单位置，formid 只接受 form-数字'''
    formid = request.POST.formid
    return f'#{formid}' if re.fullmatch(r'form-\d{1,4}', formid) else ''


@route('/')
def index():
    '''
    推荐页的"推荐"（like=1，系统评分为喜欢）和"不太像"（like=0）两个标签页，都按模型给的分数从高到低排
    '''
    rate_type = RATE_TYPE.SYSTEM_RATE.value
    rate_value = _like_param(RATE_VALUE.LIKE.value)
    page = _page_param()
    items, page_info = get_items(
        rate_type=rate_type, rate_value=rate_value, page=page, order_first=db.by_score())
    for item in items:
        _remove_extra_tags(item)
    today_update_count = db.get_today_update_count()
    today_recommend_count = db.get_today_recommend_count()
    msg = f'今日更新 {today_update_count} , 今日推荐 {today_recommend_count}'
    settings = pick.load_settings()
    # "指定推荐"标签页上的数量；没有设定时不显示
    pick_count = None if settings.is_empty else pick.count_items(settings, pick.match_names(settings))
    done, errmsg = _take_note()
    return template('index', items=items, page_info=page_info, like=rate_value, path=request.path, msg=msg,
                    done=done, errmsg=errmsg, notice=_model_notice(), pick_count=pick_count,
                    recommended_percent=clf.RECOMMENDED_PERCENT, min_recommended=clf.MIN_RECOMMENDED)


@route('/tagit')
def tagit():
    rate_value = _like_param(None)
    rate_type = None if rate_value is None else RATE_TYPE.USER_RATE
    page = _page_param()
    keyword = _search_text(request.query.getunicode('q'))
    items, page_info = get_items(
        rate_type=rate_type, rate_value=rate_value, page=page, keyword=keyword)
    for item in items:
        _remove_extra_tags(item)
        _attach_entries(item)
    msg, errmsg = _take_note()
    return template('tagit', items=items, page_info=page_info, like=rate_value, path=request.path,
                    msg=msg, errmsg=errmsg, q=keyword)


@route('/tag/<fanhao>', method='POST')
def tag(fanhao):
    rate_value = _submit_value()
    if Item.get_by_fanhao(fanhao) is None:
        _abort(404, '没有这个番号')
    # 打标页"喜欢 / 不喜欢"标签页上的按钮就是用来改自己的打标的，所以覆盖已有的用户打标；
    # 页面打开期间定时推荐可能已经给它打过系统评分，也一起覆盖
    outcome = ItemRate.save_user_rate(fanhao, rate_value, replace_user_rate=True)
    # 打过什么标要能在日志文件里查到：番号、喜欢还是不喜欢、是新增还是改动
    logger.info(f'打标 {fanhao}：{"喜欢" if rate_value else "不喜欢"}（{TAG_OUTCOMES[outcome]}）')
    # 喜欢的影片没有演员时，演员名由后台任务去 av-wiki 补：这里只登记，不联网，页面马上跳回
    if rate_value == RATE_VALUE.LIKE and not db.has_star(fanhao):
        request_actress_fill()
    # 推荐页、指定推荐页的列表也用这个地址打标，打完回到那一页。只认这两个词，不接受任何地址
    back = request.POST.get('back')
    if back == 'pick':
        redirect(f'/pick?page={_page_param()}{_form_anchor()}')
    if back == 'recommend':
        redirect(f'/?page={_page_param()}&like={_like_param(RATE_VALUE.LIKE.value)}{_form_anchor()}')
    like = _like_param(None)
    like_q = '' if like is None else f'&like={like}'
    redirect(f'/tagit?page={_page_param()}{like_q}{_search_suffix(request.query.getunicode("q"))}{_form_anchor()}')


def _bulk_target(back, note=''):
    '''
    整页标不喜欢的表单从哪个列表页提交，就回到那一页原来的页码（推荐页还有标签页，打标页还有搜索词）。
    note 是一次性提示的编号；不给时就是不带提示的地址，确认页上的"取消"用它
    '''
    page = _posted_page()
    if back == 'pick':
        return f'/pick?page={page}' + (f'&done={note}' if note else '') + '#pick-total'
    extra = f'&note={note}' if note else ''
    if back == 'recommend':
        return f'/?page={page}&like={_posted_like()}{extra}'
    return f'/tagit?page={page}{_search_suffix(request.POST.getunicode("q"))}{extra}'


def _bulk_fields(back, fanhaos):
    '''确认页上再提交一次时带的字段，和列表页上的表单一样'''
    fields = [('back', back), ('page', _posted_page())]
    if back == 'recommend':
        fields.append(('like', _posted_like()))
    keyword = _search_text(request.POST.getunicode('q'))
    if back == 'tagit' and keyword:
        fields.append(('q', keyword))
    return fields + [('fanhao', fanhao) for fanhao in fanhaos]


@route('/dislike-page', method='POST')
def dislike_page():
    '''
    整页标不喜欢。表单里是页面打开时列出的那些番号（fanhao，一页的数量以内）：标的是用户看到的这一页，
    不是提交时服务器算出来的那一页。其中还不是用户亲手打标的标成不喜欢，已经打过标的不动，库里没有的忽略。
    没有带 confirmed=1 的先回一个确认页：页面脚本在浏览器里问过之后会带上它，脚本没加载好或者关掉了就由这一页来问
    '''
    back = request.POST.get('back')
    if back not in BULK_PAGES:
        _abort(400, '提交的页面只能是推荐、指定推荐或打标')
    submitted = request.POST.getall('fanhao')
    if len(submitted) > db.PAGE_SIZE:
        _abort(400, f'一次最多提交一页的影片（{db.PAGE_SIZE} 部）')
    fanhaos = [fanhao for fanhao in dict.fromkeys(submitted) if is_valid_fanhao(fanhao)]
    if not fanhaos:
        redirect(_bulk_target(back, _leave_note(errmsg='没有提交要标的影片，没有改动')))
    undo = '标错了可以到"打标"页的「不喜欢」里改回来'
    if request.POST.get('confirmed') != '1':
        return template('confirm', path=BULK_PAGES[back], action=request.path, fields=_bulk_fields(back, fanhaos),
                        question=f'把这一页的 {len(fanhaos)} 部影片都标为不喜欢吗？',
                        detail=f'{"、".join(fanhaos)}。已经打过标的会跳过，不会改。{undo}。',
                        yes='确定，都标为不喜欢', cancel=_bulk_target(back))
    result = db.dislike_untagged(fanhaos)
    tagged = len(result.tagged)
    listed = f'（{"、".join(result.tagged)}）' if tagged else ''
    ignored = f'，{result.unknown} 个番号库里没有' if result.unknown else ''
    logger.info(f'整页标不喜欢：标了 {tagged} 部{listed}，跳过 {result.kept} 部已经打过标的{ignored}')
    if tagged:
        skipped = f'，跳过 {result.kept} 部已经打过标的' if result.kept else ''
        note = _leave_note(f'已把 {tagged} 部标为不喜欢{skipped}。{undo}')
    elif result.kept:
        note = _leave_note(errmsg=f'这一页的 {result.kept} 部都已经打过标了，没有改动')
    else:
        note = _leave_note(errmsg='提交的影片库里都没有，没有改动')
    redirect(_bulk_target(back, note))


@route('/entry/<fanhao>', method='POST')
def choose_entry(fanhao):
    '''改选同一番号的另一条详情，或先查找有哪些条目。失败只影响这一条'''
    fanhao = fanhao.upper()
    if Item.get_by_fanhao(fanhao) is None:
        _abort(404, '没有这个番号')
    try:
        if request.POST.get('find'):
            count = bus_spider.record_alternates(fanhao)
            note = 'fetch' if count is None else ('found' if count else 'empty')
        else:
            note = bus_spider.refetch_entry(fanhao, request.POST.get('path') or '')
    except Exception:
        logger.exception(f'{fanhao} 改选详情失败')
        note = 'fetch'
    if note == 'replaced':
        # 新的详情里可能没有演员
        request_actress_fill()
    redirect(_entry_redirect(note))


def _added_note(word, result):
    '''往推荐设定里加名字的结果（pick.AddResult）换成提示 (成功的话, 出错的话)，word 是"女优"或"标签"'''
    msg = ''
    problems = []
    if result.added:
        msg = f'已添加 {result.added} 个{word}' + (f'，另有 {result.duplicate} 个已经在列表里' if result.duplicate else '')
    elif result.duplicate:
        problems.append(f'{result.duplicate} 个{word}已经在列表里，没有重复添加')
    if result.rejected:
        problems.append(f'{result.rejected} 个名字超过 {pick.MAX_NAME_LENGTH} 个字符或带有控制字符，没有添加')
    if result.overflow:
        problems.append(f'指定{word}最多 {pick.MAX_NAMES} 个，有 {result.overflow} 个没有添加')
    return msg, '；'.join(problems)


def _short_time(moment):
    return moment.strftime('%m-%d %H:%M')


def _busy_text(busy):
    '''
    登记了的事因为数据库被别的程序占着没做成、留着等再试时，那一行后面写的说明。busy 是 schedule.busy_wait() 的结果，
    没有这回事时是 None，说明也是空
    '''
    if busy is None:
        return ''
    again = f'约 {busy.next_at.strftime("%H:%M")} 自动再试' if busy.next_at else '正在再试'
    return f'数据库被别的程序占着，这一轮没做成，{again}'


def _doing(row, activity, paused, busy=''):
    '''
    这一行的女优现在在做什么（给进度看）；没有在做什么时是空。暂停期间等着翻的不会翻，不算在做什么。
    busy 是 _busy_text 的结果：不是空时，等着的那几种后面写上在等什么
    '''
    link = row.link
    waiting = f'（{busy}）' if busy else ''
    if row.path is None and link is not None and link.state == db.LINK_PENDING:
        return f'等着查找她的 JavBus 演员页…{waiting}' if busy else '正在查找她的 JavBus 演员页…'
    source = row.source
    if source is None:
        return ''
    if activity is not None and activity[0] in ('page', 'movie') and activity[1] == source.id:
        kind, _, what = activity
        return f'正在读她的第 {what} 页…' if kind == 'page' else f'正在抓 {what}…'
    if source.checking:
        return f'等着读她的第 1 页…{waiting}'
    if source.wanted > 0 and not paused:
        return f'等着翻她的演员页…{waiting}'
    return ''


def _search_view(view, row):
    '''还不知道 JavBus 演员页的那一行：查找的结果，查找、选一位的按钮'''
    link = row.link
    view.facts = '没有她的 JavBus 演员页地址'
    view.search = f'查找她的 JavBus 页面（用「{row.search}」）'
    if link is None or link.state == db.LINK_PENDING:
        if link is not None:
            view.search = ''
        return view
    when = f'（{_short_time(link.checked_at)}）' if link.checked_at else ''
    view.search = '再查一次'
    if link.state == db.LINK_NONE:
        view.facts = f'JavBus 上没有找到「{link.name}」{when}'
    elif link.state == db.LINK_ERROR:
        view.error = f'查找出错{when}：{link.error}'
    elif link.state == db.LINK_CHOOSE:
        view.candidates = [(path, f'{name}（{path}，取消过关联）' if path in row.rejected else f'{name}（{path}）')
                           for path, name in link.candidate_list]
        view.facts = f'JavBus 上找到 {len(view.candidates)} 位，点一位是她：'
    return view


def _row_view(row, activity, paused, busy=''):
    '''按演员补全作品里一行要显示的文字和按钮；busy 见 _doing'''
    source = row.source
    # 设定里的名字不是她的正式名字（选的是别名）时，在后面写出正式名字
    who = '' if any(db.fold(name) == db.fold(row.name) for name in row.names) else row.name
    view = types.SimpleNamespace(name=row.names[0], names='、'.join(row.names), who=who, row=row.row,
                                 movies=row.movies, path=row.path,
                                 facts='', queue='', position='', doing=_doing(row, activity, paused, busy), error='',
                                 check='', start='', cancel=False, retry='', search='', candidates=[], linked='',
                                 relink=False)
    if row.path is None:
        return _search_view(view, row)
    if row.avwiki:
        # av-wiki 的女优，演员页是查找到、点选或者按名字对上的：写明是哪一个，可以换一个、取消关联
        page = f'{row.page_name}（{row.path}）' if row.page_name else row.path
        view.linked = f'关联的 JavBus 演员页：{page}'
        view.relink = True
    if source is not None and source.checked_at is not None and source.magnets is not None:
        view.facts = f'JavBus 上有磁力 {source.magnets} 部（{_short_time(source.checked_at)} 查看）'
        if source.total is not None and source.total > source.magnets:
            view.facts += f'，另有 {source.total - source.magnets} 部没有磁力，未包含'
    elif source is not None and source.checked_at is not None:
        view.facts = f'查看过（{_short_time(source.checked_at)}），页面上没有读到有磁力的部数'
    else:
        view.facts = '还没有查看她在 JavBus 上有多少部'
    if row.queued or row.done or row.failed:
        view.queue = f'已排队 {row.queued} 部，已补 {row.done} 部，失败 {row.failed} 部'
    walked = source is not None and source.next_page > 0
    if walked and source.reached_end:
        view.position = '已经补到她最早的一部，再点只找新片'
    elif walked and source.oldest is not None:
        view.position = f'往回补到 {source.oldest.isoformat()} 发行的'
    if source is not None and source.error:
        view.error = f'上次出错（{_short_time(source.error_at)}）：{source.error}' if source.error_at else source.error
    busy = bool(view.doing)
    if not busy:
        view.check = '重新查看' if source is not None and source.checked_at is not None else '查看'
    if not busy and not row.queued:
        view.start = '继续补全' if walked else f'补全最新 {backfill.batch_size()} 部'
    view.cancel = bool(row.queued) or (source is not None and source.wanted > 0)
    if row.retryable:
        view.retry = f'重试失败的 {row.retryable} 部'
    return view


def _backfill_view(settings, matched, star_counts):
    '''指定推荐页上"按演员补全作品"那一块；没有指定女优时是 None'''
    if not settings.stars:
        return None
    activity = backfill.activity
    paused = backfill.paused()
    busy = _busy_text(schedule.busy_wait())
    rows = [_row_view(row, activity, paused, busy) for row in pick.backfill_rows(settings, matched, star_counts)]
    listed, _ = backfill.waiting()
    used, limit = backfill.daily_used(), backfill.daily_limit()
    left = max(0, limit - used)
    state, _ = schedule.queue_state()
    pending = any(row.doing for row in rows)
    active = bool(pending or activity is not None or state is not None or (listed and left and not paused))
    return types.SimpleNamespace(rows=rows, batch=backfill.batch_size(), daily=limit, used=used, left=left,
                                 listed=listed, paused=paused, active=active, busy=busy,
                                 tomorrow=schedule.TOMORROW_AT.strftime('%H:%M'))


def _paused_text(paused):
    reason, at = paused
    end = '' if reason.endswith('。') else '。'
    return (f'今天被网站拦截过（{_short_time(at)}）：{reason}{end}补全先停下了，今天不再自动接着抓；'
            '已经排队的影片明天会自动继续，还没做的补全、查看、查找不会再自己去做，要继续请再点一次。')


def _person_text(person):
    return f'{person.name}（含别名：{"、".join(person.aliases)}）' if person.aliases else person.name


def _alias_note(name, people):
    '''
    已选的一个女优名字下面的一行说明：这个名字找到的是谁（正式名字）、她还有哪些别名；
    几位女优共用这个名字时写明是哪几位。没有要说的时返回空
    '''
    if len(people) > 1:
        return (f'{name} 是 {len(people)} 位女优共用的名字，都算：'
                + '、'.join(_person_text(person) for person in people))
    if not people:
        return ''
    person, = people
    if db.fold(person.name) == db.fold(name):
        return f'{name} 含别名：{"、".join(person.aliases)}' if person.aliases else ''
    return f'{name} 就是 {_person_text(person)}'


@route('/pick')
def pick_page():
    '''指定推荐：上半页是推荐设定和按演员补全作品，下半页是符合设定、用户还没打标的影片'''
    settings = pick.load_settings()
    matched = pick.match_names(settings)
    items, page_info = pick.get_items(settings, matched, _page_param())
    for item in items:
        _remove_extra_tags(item)
    star_counts, tag_counts = pick.library_counts(matched)
    liked_stars, liked_tags = pick.liked_choices(settings, matched)
    model_active = settings.model_only and pick.has_model_results()
    msg, errmsg = _take_note()
    if settings.problem:
        # 设定文件坏着的时候每次打开都说，不是一次性的提示
        broken = (f'推荐设定文件 {pick.settings_path()} 读不了或格式不对（{settings.problem}），'
                  '已按没有设定处理；重新添加女优或标签并保存后会覆盖它')
        errmsg = f'{errmsg}；{broken}' if errmsg else broken
    # 整页标不喜欢的结果显示在列表上方（页面最上面是设定区，离列表很远）
    list_note, list_error = _take_note('done')
    notes = [_alias_note(name, matched.people.get(name, [])) for name in settings.stars]
    return template('pick', msg=msg, errmsg=errmsg, list_note=list_note, list_error=list_error, settings=settings,
                    alias_notes=[note for note in notes if note],
                    star_counts=star_counts, tag_counts=tag_counts, liked_stars=liked_stars, liked_tags=liked_tags,
                    items=items, page_info=page_info, pick_count=None if settings.is_empty else page_info[0],
                    model_active=model_active, model_idle=settings.model_only and not model_active,
                    max_names=pick.MAX_NAMES, backfill=_backfill_view(settings, matched, star_counts),
                    paused_text=_paused_text)


@route('/pick/backfill')
def pick_backfill():
    '''指定推荐页上"按演员补全作品"那一块，页面脚本每隔几秒取一次刷新进度；没有指定女优时是空的'''
    response.set_header('Cache-Control', 'no-store')
    settings = pick.load_settings()
    if not settings.stars:
        return ''
    matched = pick.match_names(settings)
    star_counts, _ = pick.library_counts(matched)
    return template('backfill', backfill=_backfill_view(settings, matched, star_counts), paused_text=_paused_text)


def _back_to_backfill(msg='', errmsg=''):
    '''按演员补全的表单处理完，跳回指定推荐页的那一块，带着显示一次的提示'''
    redirect(f'/pick?note={_leave_note(msg, errmsg)}#backfill')


def _chosen_actress():
    '''
    表单里的 name 是已选的一位女优的名字，row 是她那一行的键（一个名字是几位女优共用的别名时，每人一行，靠它分清），
    返回 (这个名字, 她那一行 pick.ActressRow)。名字不在设定里了（页面打开以后被去掉了）、这一行已经没有了，
    或者没带 row 而名字对得上不止一行（旧页面）时，不猜，跳回指定推荐页并提示
    '''
    name = request.POST.getunicode('name')
    if not name or not name.strip() or len(name) > pick.MAX_NAME_LENGTH:
        _abort(400, '没有提交女优的名字，或者名字太长')
    key = request.POST.getunicode('row') or ''
    if len(key) > MAX_ROW_KEY:
        _abort(400, '提交的行太长')
    settings = pick.load_settings()
    matched = pick.match_names(settings)
    star_counts, _ = pick.library_counts(matched)
    found = [row for row in pick.backfill_rows(settings, matched, star_counts)
             if (not key or row.row == key) and any(db.fold(other) == db.fold(name) for other in row.names)]
    if len(found) > 1:
        # 没带 row 的旧页面，这个名字现在是几位女优共用的：猜哪一位都可能落在另一个人身上
        _back_to_backfill(errmsg=f'页面已经过期：「{name}」现在对应好几位女优，没有改动，请刷新页面后再点')
    if found:
        return name, found[0]
    _back_to_backfill(errmsg=f'「{name}」不在指定推荐的女优里了，没有改动')


def _actress_page():
    '''表单里那位女优的名字和她的 JavBus 演员页；还不知道演员页时跳回并提示先查找'''
    name, row = _chosen_actress()
    if row.path is None:
        _back_to_backfill(errmsg=f'还不知道「{name}」的 JavBus 演员页，先点「查找她的 JavBus 页面」')
    return name, row


@route('/pick/backfill/check', method='POST')
def backfill_check():
    '''「查看」：后台去读她的演员页第 1 页（只有 1 次请求），记下 JavBus 上有磁力的部数'''
    name, row = _actress_page()
    source = backfill.request_check(row.path)
    logger.info(f'按演员补全：登记查看演员页 #{source.id}')
    request_queue_run()
    _back_to_backfill(f'已登记查看「{name}」的演员页：只读她的第 1 页，稍等几秒这里会显示结果')


@route('/pick/backfill/start', method='POST')
def backfill_start():
    '''「补全最新 N 部 / 继续补全」：后台翻她的演员页，把库里还没有的排进队列，接着一部一部地抓'''
    name, row = _actress_page()
    refused = backfill.request_fill(row.path)
    if refused == backfill.STILL_LOOKING:
        # 她登记了的补全还没翻（也许是数据库忙留着的）：这次点击也让后台马上再看一轮，不等到下一次定时抓取
        request_queue_run()
        if schedule.busy_wait() is not None:
            _back_to_backfill(f'「{name}」上一次点的补全还留着没做完：数据库刚才被别的程序占着，没来得及翻她的演员页。'
                              '已经让后台马上再试一次，不用再点；做完以后再点才会补下一批')
    if refused:
        _back_to_backfill(errmsg=f'「{name}」没有开始补全：{refused}，抓完再继续补全')
    logger.info(f'按演员补全：登记补全演员页 #{db.ListSource.get(db.ListSource.path == row.path).id}')
    request_queue_run()
    _back_to_backfill(f'已开始补全「{name}」：最多排进 {backfill.batch_size()} 部库里还没有的作品，'
                      '后台一部一部地抓，这里会显示进度')


@route('/pick/backfill/cancel', method='POST')
def backfill_cancel():
    '''取消她还没抓的排队'''
    name, row = _actress_page()
    removed = backfill.cancel(row.path)
    logger.info(f'按演员补全：取消了 {removed} 部的排队')
    if removed:
        _back_to_backfill(f'已取消「{name}」排队的 {removed} 部，已经抓到的留在库里')
    _back_to_backfill(f'「{name}」没有在排队的了，这一次也不再往下翻')


@route('/pick/backfill/retry', method='POST')
def backfill_retry():
    '''她失败的影片重新排队'''
    name, row = _actress_page()
    count = backfill.retry_source(row.path)
    if not count:
        _back_to_backfill(errmsg=f'「{name}」没有失败的影片要重试，没有改动')
    logger.info(f'按演员补全：失败的 {count} 部重新排队')
    request_queue_run()
    _back_to_backfill(f'「{name}」失败的 {count} 部重新排进了队列')


def _actress_to_find():
    '''表单里那位女优的名字和她那一行；她已经有 JavBus 演员页时跳回并提示不用查找'''
    name, row = _chosen_actress()
    if row.path is not None:
        _back_to_backfill(errmsg=f'「{name}」已经有 JavBus 演员页了，不用查找')
    return name, row


@route('/pick/backfill/search', method='POST')
def backfill_search():
    '''「查找她的 JavBus 页面」：后台按名字找演员（1 次请求），名字相同的正好一位就采用，否则列出来让用户点'''
    name, row = _actress_to_find()
    link = backfill.request_search(row.key, row.search)
    logger.info(f'登记查找演员页 #{link.id}')
    request_queue_run()
    _back_to_backfill(f'已登记用「{row.search}」查找她的 JavBus 演员页，稍等几秒这里会显示结果')


@route('/pick/backfill/choose', method='POST')
def backfill_choose():
    '''从查找的结果里点了一位：记下她的 JavBus 演员页'''
    path = request.POST.get('path') or ''
    if not is_star_page(path):
        _abort(400, '提交的不是演员页的地址')
    name, row = _actress_to_find()
    if not backfill.choose_page(row.key, path):
        _back_to_backfill(errmsg='这个演员页不在查找的结果里，没有改动')
    logger.info(f'采用了查找到的演员页（{path}）')
    _back_to_backfill(f'已记下「{name}」的 JavBus 演员页，可以查看和补全了')


def _linked_actress():
    '''表单里那位女优的名字和她那一行；这一行不是关联了 JavBus 演员页的 av-wiki 女优时跳回并提示'''
    name, row = _chosen_actress()
    if row.path is None or not row.avwiki:
        _back_to_backfill(errmsg=f'「{name}」没有可以取消的 JavBus 演员页关联，没有改动')
    return name, row


def _unlinked_note(name, path, removed):
    note = f'已取消「{name}」和 JavBus 演员页 {path} 的关联'
    return note + (f'，这个演员页还没抓的 {removed} 部也不抓了' if removed else '')


@route('/pick/backfill/unlink', method='POST')
def backfill_unlink():
    '''「取消关联」：这个 JavBus 演员页不是她。正式名字按规则重新定，变了的话后台再读一次她的女优页（这里只登记）'''
    name, row = _linked_actress()
    removed = backfill.unlink(row.key, row.path)
    logger.info(f'取消了演员页的关联（{row.path}），这个演员页还没抓的 {removed} 部不抓了')
    request_actress_fill()
    _back_to_backfill(_unlinked_note(name, row.path, removed))


@route('/pick/backfill/relink', method='POST')
def backfill_relink():
    '''「换一个」：这个 JavBus 演员页不是她，从上次的查找结果里另点一位；没有结果时后台重新查找'''
    name, row = _linked_actress()
    searching, removed = backfill.change(row.key, row.path, row.search)
    logger.info(f'换一个演员页：取消了原来的关联（{row.path}），这个演员页还没抓的 {removed} 部不抓了')
    request_actress_fill()
    note = _unlinked_note(name, row.path, removed)
    if searching:
        request_queue_run()
        _back_to_backfill(f'{note}，正在用「{searching}」重新查找，稍等几秒这里会显示结果')
    _back_to_backfill(f'{note}，从下面的查找结果里点一位是她')


@route('/pick/backfill/resume', method='POST')
def backfill_resume():
    '''被拦截暂停以后，用户点"现在继续"'''
    backfill.resume()
    logger.info('按演员补全：用户点了继续，解除今天的暂停')
    request_queue_run()
    _back_to_backfill('补全接着抓了')


def _pick_kind():
    '''推荐设定表单里的类别只接受 star（女优）和 tag（标签）'''
    kind = request.POST.get('kind')
    if kind not in pick.FIELDS:
        _abort(400, '提交的类别只能是女优或标签')
    return kind


def _back_to_pick(msg='', errmsg=''):
    '''推荐设定的表单处理完，跳回指定推荐页，带着显示一次的提示'''
    redirect(f'/pick?note={_leave_note(msg, errmsg)}')


def _change_pick(change):
    '''改推荐设定。写不进文件时不出错误页：原因写进日志，跳回指定推荐页，在页面上方提示'''
    try:
        return change()
    except pick.SettingsError as ex:
        logger.warning(ex)
        _back_to_pick(errmsg=f'推荐设定没有保存：无法写入 {pick.settings_path()}，'
                             '请检查磁盘空间和这个文件的权限（具体原因见运行日志）')


@route('/pick/add', method='POST')
def pick_add():
    '''
    往指定女优或指定标签里加名字。name 是页面上的按钮带的一个完整名字；
    names 是输入框里的文字，可以用换行或逗号隔开多个
    '''
    kind = _pick_kind()
    word = PICK_KIND_NAMES[kind]
    name = request.POST.getunicode('name')
    text = request.POST.getunicode('names') or ''
    if len(text) > PICK_INPUT_LIMIT:
        _back_to_pick(errmsg=f'一次提交的内容太多（超过 {PICK_INPUT_LIMIT} 个字符），请分几次添加')
    names = [name] if name else pick.split_names(text)
    if not any(name.strip() for name in names):
        _back_to_pick(errmsg=f'请先填写要添加的{word}')
    result = _change_pick(lambda: pick.add_names(kind, names))
    if kind == 'star' and result.added:
        # 新选的女优只有搜索卡片上的名字时，由后台任务去读她的女优页补全别名：这里只登记
        request_actress_fill()
    _back_to_pick(*_added_note(word, result))


@route('/pick/remove', method='POST')
def pick_remove():
    kind = _pick_kind()
    word = PICK_KIND_NAMES[kind]
    name = request.POST.getunicode('name')
    if _change_pick(lambda: pick.remove_name(kind, name)):
        _back_to_pick(f'已去掉 1 个{word}')
    _back_to_pick(errmsg=f'要去掉的{word}不在列表里，没有改动')


@route('/pick/options', method='POST')
def pick_options():
    mode = request.POST.get('mode')
    if mode not in pick.MODES:
        _abort(400, '条件关系只能是「同时满足」或「满足任一」')
    model_only = request.POST.get('model_only') == '1'
    _change_pick(lambda: pick.save_options(mode, model_only))
    _back_to_pick('推荐设定已保存')


@route('/pick/clear', method='POST')
def pick_clear():
    '''
    清空推荐设定。没有带 confirmed=1 的先回一个确认页：页面脚本在浏览器里问过之后会带上它，
    脚本没加载好或者关掉了就由这一页来问
    '''
    if request.POST.get('confirmed') != '1':
        return template('confirm', path='/', action=request.path, fields=[], question='确定清空推荐设定吗？',
                        detail='已选的女优和标签都会去掉，条件关系等选项回到默认。', yes='清空设定', cancel='/pick')
    _change_pick(pick.clear_settings)
    _back_to_pick('已清空推荐设定')


def _json_text(data):
    '''
    转成 JSON 文字。尖括号和 & 写成 \\uXXXX 的形式：就算这段文字被当成网页打开，里面的标签值也成不了标记
    '''
    text = json.dumps(data, ensure_ascii=False)
    return text.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


@route('/pick/suggest')
def pick_suggest():
    '''
    指定推荐页输入框的联想：kind=star 只找演员，kind=tag 找所有类型的标签；q 是输入的文字，可以为空。
    返回 JSON 数组，每项是 {"value": 标签值, "type": 类型, "count": 影片数}；kind 不对时是 400 和 {"error": 原因}
    '''
    response.content_type = 'application/json; charset=UTF-8'
    response.set_header('Cache-Control', 'no-store')
    response.set_header('X-Content-Type-Options', 'nosniff')
    kind = request.query.get('kind')
    if kind not in pick.FIELDS:
        response.status = 400
        return _json_text({'error': 'kind 只能是 star 或 tag'})
    return _json_text(pick.suggest(kind, request.query.getunicode('q')))


def _training_result():
    '''
    训练后跳回模型页时网址里的编号对应的结果：(失败原因, 绿色的提示, 黄色的提示)，见 _training_results；
    编号不对或已经过期时返回 None
    '''
    try:
        result_id = int(request.query.get('training', ''))
    except ValueError:
        return None
    with _training_lock:
        return _training_results.get(result_id)


def _training_messages(scoring):
    '''
    训练成功后模型页上方的提示 (绿色的, 黄色的)。训练完紧接着重新打分：绿色的写明打了多少部、推荐多少部；
    打分没做成（scoring 是 None：数据库的写锁等不到）时，黄色的说明模型已经保存、分数什么时候会有
    '''
    done = '训练完成，下面是新模型的数据。'
    if scoring is None:
        return done, (f'模型已保存，但影片还没有重新打分：{db.DATABASE_BUSY}。'
                      '下一次抓取后会自动打分，也可以稍后再点一次"开始训练"')
    if scoring.scored:
        return f'{done}已重新打分 {scoring.scored} 部，其中推荐 {scoring.recommended} 部', ''
    return f'{done}现在没有需要打分的影片', ''


def _percent(share):
    '''0 到 1 之间的比例写成不带小数的百分数'''
    return f'{share * 100:.0f}'


def _model_view(saved):
    '''
    模型页"当前模型数据"里用到的数字和判断。打标少的那一类（minor）单独拿出来说：
    模型有没有用，主要看它认不认得这一类
    '''
    report = saved.report
    recalls = {'喜欢': report.like_recall, '不喜欢': report.dislike_recall}
    minor, minor_count = prepare.smaller_class(report.likes, report.dislikes)
    major = '不喜欢' if minor == '喜欢' else '喜欢'
    scored, major_side = clf.scored_sides(report)
    return {
        'likes': report.likes, 'dislikes': report.dislikes, 'trained_at': saved.trained_at,
        'folds': report.folds, 'repeats': report.repeats, 'auc': _percent(report.auc),
        'major': major, 'major_recall': _percent(recalls[major]),
        'minor': minor, 'minor_recall': _percent(recalls[minor]), 'minor_count': minor_count,
        'margin': prepare.recall_margin(minor_count),
        'target': prepare.TARGET_MINORITY, 'solid': prepare.SOLID_MINORITY,
        'tentative': prepare.for_reference_only(report.likes, report.dislikes),
        'guessing': clf.same_as_guessing(report),
        'alike': clf.all_alike(report), 'scored': scored, 'major_side': major_side,
    }


def _labels_line(saved):
    '''模型页上的一句话：现在有多少条打标，能不能训练，和当前模型训练时比有没有变'''
    likes, dislikes = prepare.count_labels()
    text = f'现在有 {likes + dislikes} 条打标（喜欢 {likes}、不喜欢 {dislikes}），'
    problem = prepare.training_problem(likes, dislikes)
    if problem:
        return f'{text}还不能训练：{problem}'
    if saved is None:
        return f'{text}可以训练'
    if (likes, dislikes) == (saved.report.likes, saved.report.dislikes):
        return f'{text}和训练时一样'
    return f'{text}训练之后有变化，重新训练才会用上'


def _replacing_old_model(error):
    '''
    模型不能用是因为它是旧版本程序训练的，而启动后的自动重新训练还没做完、打标也够训练：
    这段时间页面上说"正在自动重新训练"，不让用户自己去点训练
    '''
    return (isinstance(error, LegacyModelError) and schedule.upgrade_pending()
            and prepare.training_problem(*prepare.count_labels()) is None)


def _model_notice():
    '''
    推荐页上方关于模型的提示：还没有模型、模型不能用、模型等于全猜、结果仅供参考，各一句；模型没有这些情况时为空
    '''
    try:
        report = clf.load().report
    except FileNotFoundError:
        return (f'还没有训练模型。打标满 {prepare.MIN_TRAIN_NUM} 条（其中"喜欢""不喜欢"里少的那一类至少 '
                f'{prepare.MIN_MINORITY} 条）后到"模型"页训练，这里就会按相似度列出推荐')
    except ModelLoadError as ex:
        if _replacing_old_model(ex):
            return f'{OLD_MODEL}：程序正在用现有的打标自动重新训练，稍等片刻再刷新这个页面。下面暂时还是原来的内容'
        return f'模型现在不能用，下面的内容不会更新：{ex}'
    if db.count_scores(0.5)[0] == 0 and prepare.has_unlabeled():
        # 模型存好了、库里却一个分数也没有：升级后第一次启动时打分在等数据库的写锁，或者打分没做成
        if schedule.upgrade_pending():
            return '模型已经训练好了，程序正在给影片打分，打完分才有按相似度排好的推荐，稍等片刻再刷新这个页面'
        return ('模型已经训练好了，但还没有给影片打分，打完分才有按相似度排好的推荐。'
                '下一次抓取后会自动打分，也可以到"模型"页点一次"开始训练"')
    minor, minor_count = prepare.smaller_class(report.likes, report.dislikes)
    if clf.same_as_guessing(report):
        major = '不喜欢' if minor == '喜欢' else '喜欢'
        return f'现在的模型把所有影片都判成{major}，等于没有筛选，下面的排序没有参考价值。原因和办法见"模型"页'
    if prepare.for_reference_only(report.likes, report.dislikes):
        # 说的是训练时的条数；之后又标了的，要重新训练才用得上
        now = dict(zip(('喜欢', '不喜欢'), prepare.count_labels()))[minor]
        advice = (f'现在已经有 {now} 条，到"模型"页重新训练才会用上' if now > minor_count
                  else f'多标一些"{minor}"的影片，再到"模型"页重新训练会更准')
        return f'训练模型时"{minor}"的打标只有 {minor_count} 条，下面的排序仅供参考；{advice}'
    return ''


@route('/model')
def other_settings():
    error_msg = replacing = None
    saved = None
    try:
        saved = clf.load()
    except FileNotFoundError:
        pass
    except ModelLoadError as ex:
        if _replacing_old_model(ex):
            replacing = f'{OLD_MODEL}。程序正在用现有的打标自动重新训练，稍等片刻再刷新这个页面，不用点"开始训练"。'
        else:
            logger.warning(f'模型不能用：{ex}')
            error_msg = str(ex)
    training_error, msg, notice = _training_result() or (None, '', '')
    return template('model', path=request.path, model=None if saved is None else _model_view(saved),
                    labels=_labels_line(saved), error_msg=error_msg, replacing=replacing,
                    training_error=training_error, msg=msg, notice=notice)


@route('/do-training', method='POST')
def do_training():
    training_error = None
    msg = notice = ''
    try:
        # 训练一次只有一个，打分等不到写锁时页面上写的是"等了好一会儿"：不受同时等锁的请求数限制
        with db.unlimited_waiting():
            _, scoring = clf.train_and_recommend()
    except (ValueError, ModelSaveError) as ex:
        training_error = ' '.join(ex.args)
        logger.warning(f'训练失败：{training_error}')
    else:
        msg, notice = _training_messages(scoring)
    with _training_lock:
        result_id = next(_training_ids)
        _training_results[result_id] = (training_error, msg, notice)
        while len(_training_results) > MAX_TRAINING_RESULTS:
            del _training_results[min(_training_results)]
    # 训练完跳回模型页（POST 之后重定向），刷新页面不会再训练一次；结果只在网址里放编号
    redirect(f'/model?training={result_id}')


@route('/do-training')
def do_training_by_get():
    # 训练只接受模型页按钮的 POST：别的网页里的图片、链接或书签用 GET 访问这里时不训练
    redirect('/model')


def _queue_codes(fanhaos, origin):
    '''库里还没有的番号排进抓取队列（记在库里，程序关掉也还在），登记让后台去抓'''
    if fanhaos:
        backfill.enqueue_codes(fanhaos, origin)
        request_queue_run()


@route('/local_fanhao', method=['GET', 'POST'])
def update_local_fanhao():
    msg, errmsg = _take_note()
    if request.POST.submit:
        fanhao_list = request.POST.fanhao
        tag_like = request.POST.tag_like == '1'
        missed_fanhao, local_file_count, tag_file_count = add_local_fanhao(
            fanhao_list, tag_like)
        _queue_codes(missed_fanhao, db.ORIGIN_UPLOAD)
        if tag_file_count:
            request_actress_fill()
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
    return template('local_fanhao', path=request.path, msg=msg, errmsg=errmsg, queue=backfill.code_queue(),
                    reason_length=MAX_REASON_LENGTH)


@route('/local_fanhao/retry', method='POST')
def retry_codes():
    '''上传番号页上"没有抓到的"里，失败的番号重新排队：fanhao 是一个番号，all=1 是全部失败的'''
    fanhao = request.POST.get('fanhao')
    every = request.POST.get('all') == '1'
    if every == (fanhao is not None) or (fanhao is not None and not is_valid_fanhao(fanhao)):
        _abort(400, '要再试的只能是一个番号，或者全部失败的番号')
    count = backfill.retry_codes(None if every else [fanhao])
    if count:
        logger.info(f'失败的番号重新排队：{count} 个')
        request_queue_run()
        redirect(f'/local_fanhao?note={_leave_note(f"已重新排进下载队列：{count} 个番号")}')
    redirect(f'/local_fanhao?note={_leave_note(errmsg="没有失败的番号要再试，没有改动")}')


@route('/local_fanhao/clear', method='POST')
def clear_codes():
    '''清除上传番号页上"没有抓到的"那些记录（失败的、网站上没有的）'''
    count = backfill.forget_problems()
    if not count:
        redirect(f'/local_fanhao?note={_leave_note(errmsg="没有要清除的记录")}')
    logger.info(f'清除了没有抓到的番号记录：{count} 条')
    redirect(f'/local_fanhao?note={_leave_note(f"已清除 {count} 条没有抓到的记录")}')


@route('/local')
def local():
    page = _page_param()
    items, page_info = get_local_items(page=page)
    for local_item in items:
        LocalItem.loadit(local_item)
        _remove_extra_tags(local_item.item)
        _attach_entries(local_item.item)
    msg, errmsg = _take_note()
    return template('local', items=items, page_info=page_info, pending=count_local_items_without_item(),
                    path=request.path, msg=msg, errmsg=errmsg)


@route('/local_play/<id:int>')
def local_play(id):
    local_item = LocalItem.get_or_none(LocalItem.id == id)
    if local_item is None:
        _abort(404, '没有这个本地文件')
    file_path = local_item.path or ''
    parsed = urlparse(file_path)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or not file_path.isprintable():
        _abort(400, '只能播放 http / https 地址的文件')
    LocalItem.update_play(id)
    # 文件的地址不写进日志：文件名里常带着影片标题
    logger.debug(f'播放本地文件 {id}（{local_item.item_id}）')
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
                _queue_codes(result.missed_fanhaos, db.ORIGIN_IMPORT)
                if result.imported:
                    request_actress_fill()
                msg, errmsg = _import_message(result)
        else:
            errmsg = '请上传数据库文件'
    return template('load_db', path=request.path, msg=msg, errmsg=errmsg)


def _every(seconds):
    '''抓取间隔在页面上的说法：整小时、整分钟的按小时、分钟说'''
    for unit, name in ((3600, '小时'), (60, '分钟')):
        if seconds % unit == 0:
            return f'每 {seconds // unit} {name}'
    return f'每 {seconds} 秒'


def _download_task():
    '''关于页"定时抓取"那一行：间隔、现在是不是正在抓取或者在排队、上一次的结果、下一次的时间'''
    if schedule.scheduler is None:
        return '定时任务没有启动。'
    text = f'{_every(int(APP_CONFIG["download.interval"]))}自动抓取一次。'
    state, started = schedule.download_state()
    if state == 'running':
        text += f'现在正在抓取（{format_datetime(started)} 开始）。'
    elif state == 'queued':
        text += '已经到了抓取的时间，正在等前面的任务做完。'
    if schedule.last_download is not None:
        finished_at, summary = schedule.last_download
        text += f'上一次（{format_datetime(finished_at)}）{summary.rstrip("。")}。'
    elif state is None:
        text += '程序启动后还没有抓取过。'
    job = schedule.scheduler.get_job('download')
    if job is not None and job.next_run_time is not None:
        text += f'下一次：{format_datetime(job.next_run_time.astimezone())}。'
    return text


def _actress_task():
    '''
    关于页"补演员名"那一行：演员数据没升级成的原因；还有几部没有演员（喜欢的、在「推荐」里的各几部）、
    到现在补到了几部；今天向 av-wiki 请求了几次、还可以请求几次；升级后的校正还剩多少（或者已经做完）；
    现在是不是正在补或者排着一轮、上一轮处理过影片的结果、上次失败的原因
    '''
    status = avwiki.status()
    text = ''
    migration = actresses.last_migration
    failed = migration is not None and migration.state == 'failed'
    not_upgraded = failed and not migration.again
    if not_upgraded:
        text += (f'演员数据还没有升级（{format_datetime(migration.at)} 试过）：{migration.reason}。'
                 f'{RETRY_NOTE}，在那之前不去 av-wiki 补演员名。')
    elif failed:
        text += (f'旧版本按旧存法写下的演员名还没有整理（{format_datetime(migration.at)} 试过）：{migration.reason}。'
                 f'{RETRY_NOTE}。')
    later = status.waiting - status.due
    which = f'（其中喜欢的 {status.liked} 部，在「推荐」里的 {status.recommended} 部）'
    if not status.waiting:
        text += '现在没有缺演员的影片。'
    elif not_upgraded:
        text += f'还有 {status.waiting} 部影片没有演员{which}，升级做完以后再去 av-wiki 补。'
    elif not later:
        text += f'还有 {status.waiting} 部影片没有演员{which}，下一轮去 av-wiki 补。'
    elif not status.due:
        text += f'还有 {status.waiting} 部影片没有演员{which}，av-wiki 上次没查到，过些天再试。'
    else:
        text += (f'还有 {status.waiting} 部影片没有演员{which}：{status.due} 部下一轮去 av-wiki 补，'
                 f'{later} 部上次没查到、过些天再试。')
    if status.filled:
        text += f'到现在一共补到 {status.filled} 部。'
    left = max(0, status.limit - status.used)
    text += f'今天向 av-wiki 请求了 {status.used} 次，还可以请求 {left} 次（每天最多 {status.limit} 次）'
    text += '，剩下的明天接着补。' if not left and (status.due or status.stale or status.pages) else '。'
    migrated_at, fixed_at = actresses.upgrade_marks()
    if status.stale or status.pages:
        retry = f'（其中 {status.stale_later} 部这次在 av-wiki 上没查到，过几天再查一次）' if status.stale_later else ''
        text += f'数据库升级以后按新存法重新查：还有 {status.stale} 部影片、{status.pages} 位女优的女优页没查完{retry}。'
    elif migrated_at is not None and fixed_at is not None:
        text += f'数据库升级以后的校正已经做完（{format_datetime(fixed_at)}）。'
    state, started = schedule.fill_state()
    if state == 'running':
        text += f'现在正在补一轮（{format_datetime(started)} 开始）。'
    elif state == 'queued':
        text += '已经排了一轮，轮到就开始。'
    last = status.last_round
    if last is None:
        if state is None:
            text += '程序启动后还没有补过。'
    elif not last.due:
        text += f'上一轮（{format_datetime(last.finished_at)}）没有要补的影片。'
    else:
        text += (f'上一轮（{format_datetime(last.finished_at)}）补到 {last.filled} 部，'
                 f'av-wiki 上没有 {last.missing} 部，失败 {last.failed} 部')
        if last.corrected:
            text += f'，按新存法重新查了 {last.corrected} 部'
        if last.pages:
            text += f'，读了 {last.pages} 个女优页'
        if last.stopped:
            text += f'，{last.stopped}就停下了'
        if last.left:
            text += f'，还有 {last.left} 部留到下一轮'
        text += '。'
    if status.last_failure:
        fanhao, detail, checked_at = status.last_failure
        if len(detail) > MAX_REASON_LENGTH:
            detail = detail[:MAX_REASON_LENGTH] + '…'
        text += f'上次失败的原因：{detail}（{fanhao}，{format_datetime(checked_at)}）。'
    return text


def _run_text(run):
    '''抓排队的影片的一次结果，写在关于页上'''
    text = ''
    if run.pages:
        text += f'读了 {run.pages} 个演员页的列表页，排进 {run.queued} 部，'
    text += f'抓了 {run.fetched} 部，新增 {run.added} 部，网站上没有 {run.missing} 部，失败 {run.failed} 部'
    if run.stopped:
        text += f'，{run.stopped}就停下了'
    elif run.held == 'limit':
        text += '，今天按演员补全的额度用完了，明天接着抓'
    elif run.held == 'paused':
        text += '，今天被网站拦截过，按演员补全的明天接着抓'
    return text


def _backfill_task():
    '''
    关于页"补全作品"那一行：在排队几部（按演员补全的、上传番号和导入打标库的）、今天按演员补全抓了几部、还可以抓几部，
    现在是不是正在抓或者排着，被拦截暂停的原因，上一次的结果
    '''
    listed, codes = backfill.waiting()
    if listed or codes:
        text = f'在排队 {listed + codes} 部（按演员补全 {listed} 部，上传番号和导入打标库 {codes} 部）。'
    else:
        text = '现在没有在排队的影片。'
    used, limit = backfill.daily_used(), backfill.daily_limit()
    text += f'今天按演员补全已抓 {used} 部，还可以抓 {max(0, limit - used)} 部（每天最多 {limit} 部）。'
    state, started = schedule.queue_state()
    if state == 'running':
        text += f'现在正在抓（{format_datetime(started)} 开始）。'
    elif state == 'queued':
        text += '已经排了一次，轮到就开始。'
    busy = _busy_text(schedule.busy_wait())
    if busy:
        text += f'{busy}。'
    paused = backfill.paused()
    if paused:
        reason, at = paused
        end = '' if reason.endswith('。') else '。'
        text += (f'今天被网站拦截过（{format_datetime(at)}）：{reason}{end}按演员补全今天不再自动接着抓；'
                 '已经排队的影片明天自动继续，也可以在指定推荐页上点"现在继续"；还没做的补全、查看、查找不会再自己去做，'
                 '要继续请在指定推荐页上再点一次。')
    last = backfill.last_run
    if last is None:
        if state is None:
            text += '程序启动后还没有抓过排队的影片。'
    else:
        text += f'上一次（{format_datetime(last.finished_at)}）{_run_text(last)}。'
    return text


def _log_task():
    '''关于页"运行日志"那一行：日志文件在哪里'''
    path = active_log_file()
    if path is None:
        return '这次运行没有写日志文件，日志只显示在控制台。'
    return f'{path}（写满 {LOG_MAX_BYTES // 1024 // 1024} MB 换一个新文件，另外保留最近 {LOG_BACKUPS} 个旧文件）。'


@route('/about')
def about():
    tasks = [('定时抓取', _download_task()), ('补全作品', _backfill_task()), ('补演员名', _actress_task()),
             ('运行日志', _log_task())]
    return template('about', path=request.path, tasks=tasks)


def _error_page(error):
    '''
    出错时显示带导航栏的中文页面，状态码不变；不显示异常栈（bottle 已把它写进 wsgi.errors）
    '''
    status = error.status_code
    expected = isinstance(error, PageError)
    reason = error.body if expected else ERROR_REASONS[status]
    if status >= 500 and not expected:
        logger.error(f'{request.method} {request.path} 出错：{error.exception!r}')
    return template('error', path=request.path, status=status, title=ERROR_TITLES[status], reason=reason,
                    home=error.home if expected else True)


for _status in ERROR_TITLES:
    bottle.error(_status)(_error_page)


def _home_opens():
    '''
    新加的表还没建成时首页打不打得开：它要用的表都在库里（影片、标签、评分、分数；指定推荐里选了女优时还有按名字
    找人的那几张）。连库里有哪些表都读不了时算打不开
    '''
    needed = [db.Item, db.Tag, db.ItemTag, db.ItemRate, db.ItemScore]
    if pick.load_settings().stars:
        needed += [db.Actress, db.ActressAlias, db.StarLink]
    try:
        present = set(db.db.get_tables())
    except OperationalError:
        return False
    return all(model._meta.table_name in present for model in needed)


def _busy_page(callback):
    '''
    bottle 插件，包住每个路由：数据库的写锁被别的程序占着、等到超时还没轮到，或者等的请求已经太多、这一个没有等时，
    给"数据库正忙"的页面（503）并写一条 WARNING。这不是程序出错：不出"500 程序出错"页，日志里也不留异常栈。
    路由处理期间，这个线程等写锁受 db.MAX_WAITING_REQUESTS 的限制
    '''
    @functools.wraps(callback)
    def wrapper(*args, **kwargs):
        try:
            with db.limited_waiting():
                return callback(*args, **kwargs)
        except OperationalError as ex:
            # 首页自己也打不开时错误页上不给"返回首页"：新表没建成、首页要用的表也还缺，或者老库被独占着（什么都读不了）
            if db.tables_missing and 'no such table' in str(ex):
                logger.warning(f'{request.method} {request.path} 没有完成：{db.TABLES_MISSING}')
                _abort(503, f'{db.TABLES_MISSING}，{TABLES_ADVICE}', home=_home_opens())
            if not db.is_busy(ex):
                raise
            reason = db.busy_reason(ex)
            logger.warning(f'{request.method} {request.path} 没有完成：{reason}')
            _abort(503, f'{reason}，{BUSY_ADVICE}', home=not db.locked_out(ex))
    return wrapper


app = bottle.default_app()
app.install(_busy_page)


class _ErrorLog:
    '''
    代替 WSGI 的 wsgi.errors。请求里没接住的异常，bottle 把异常栈写到 wsgi.errors（waitress 下就是控制台）：
    转成一条 ERROR 日志，这样控制台和日志文件里都有
    '''

    def write(self, text):
        text = text.strip()
        if text:
            logger.error(f'请求出错，异常栈：\n{text}')

    def writelines(self, lines):
        self.write(''.join(lines))

    def flush(self):
        pass


_error_log = _ErrorLog()


def application(environ, start_response):
    '''交给 WSGI 服务器的应用：就是 app，只是把 wsgi.errors 换成写日志的，锁着数据目录时答复里带上 LOCK_HEADER'''
    environ['wsgi.errors'] = _error_log
    if not instance.holds_lock():
        return app(environ, start_response)

    def respond(status, headers, exc_info=None):
        return start_response(status, [*headers, (LOCK_HEADER, '1')], exc_info)

    return app(environ, respond)


class StartupError(Exception):
    '''不能启动（端口无效、端口被占用、这个数据目录已经有一个网页服务在用等），消息直接给用户看'''


def _lock_data_dir(url=None, wait=0):
    '''
    给数据目录上锁（见 instance.py），返回锁上了没有（文件系统不支持加锁时是 False）。
    已经有一个网页服务在用这个数据目录时，最多等它 wait 秒；它还在就抛 StartupError，告诉用户它在哪里
    '''
    deadline = time.monotonic() + wait
    told = False
    while True:
        try:
            return instance.lock_data_dir(url)
        except instance.AlreadyRunning as ex:
            who = f'进程号 {ex.pid}' if ex.pid else ''
            if time.monotonic() >= deadline:
                if ex.empty:
                    # 锁文件里什么都没有：它刚启动、还没写，或者没启动起来、已经清空了，说不出"在运行"
                    raise StartupError(f'另一个 bustag 正在启动或退出（数据目录 {get_data_dir()}），稍后再试') from None
                where = f'：在浏览器里打开 {ex.url} 就行' if ex.url else ''
                raise StartupError(f'bustag 已经在运行（{who}{"，" if who else ""}数据目录 {get_data_dir()}），'
                                   f'不用再启动一个{where}') from None
            if not told:
                told = True
                logger.info(f'数据目录 {get_data_dir()} 正被另一个 bustag 进程{f"（{who}）" if who else ""}占用，'
                            f'等它退出，最多等 {wait} 秒')
        time.sleep(LOCK_RETRY_STEP)


def create_app():
    '''
    网页服务开始前的准备：给数据目录上锁，日志开始写文件，清掉上次运行留下的上传临时文件，
    启动定时任务（只启动一次；打开库时新加的表没建成的话，在后台建好以后再启动）。返回交给 WSGI 服务器的应用。
    用 gunicorn 时只能开 1 个 worker（定时任务在进程内；第二个 worker 会因为数据目录已经被第一个锁住而起不来），
    请求用多个线程处理（gthread worker，下载封面时别的请求不用排队，超时只看 worker 心跳）：
    gunicorn -w 1 --threads 8 "bustag.app.index:create_app()" --bind 127.0.0.1:8000
    gunicorn 重新加载（HUP）时是先起新的 worker、再让旧的退出，所以这里拿不到锁先等一会儿
    '''
    _lock_data_dir(wait=LOCK_WAIT)
    start_file_log()
    log_uncaught_exceptions()
    remove_stale_uploads()
    if db.tables_missing:
        logger.warning(f'{db.TABLES_MISSING}：程序照常启动，网页服务在后台接着加；加好以前，用到这几张表的页面会说明原因')
        threading.Thread(target=_add_tables_then_start, name='bustag-tables', daemon=True).start()
    else:
        start_scheduler()
    return application


def _add_tables_then_start():
    '''
    打开库时没建成的表（库被别的程序占着），在后台每隔 TABLES_RETRY 秒再建一次；建好以后才启动定时任务：
    迁移、补演员名、按演员补全都要用这些表
    '''
    try:
        while not db.create_tables():
            time.sleep(TABLES_RETRY)
    finally:
        db.db.close()
    logger.info('数据库里新加的表已经建好，定时任务开始')
    start_scheduler()


def _listen_address():
    '''
    监听地址和端口。地址：环境变量 BUSTAG_HOST > config.ini 的 [server] host > 127.0.0.1（只有本机能访问）。
    端口或地址不能用时抛 StartupError，写明是哪里来的值
    '''
    port_text = os.environ.get('BUSTAG_PORT') or str(DEFAULT_PORT)
    try:
        port = int(port_text)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise StartupError(f'环境变量 BUSTAG_PORT={port_text} 不是有效的端口号（1-65535）')
    from_env = os.environ.get('BUSTAG_HOST', '').strip()
    from_config = APP_CONFIG.get('server.host', '').strip()
    host = from_env or from_config or DEFAULT_HOST
    source = f'环境变量 BUSTAG_HOST={host}' if from_env else f'config.ini 的 [server] host = {host}'
    if _is_ipv6_wildcard(host):
        raise StartupError(f'{source} 不支持：这样只监听 IPv6，用 127.0.0.1 和局域网里 192.168.x.x 这样的地址都打不开。'
                           '让同一局域网里的设备也能访问请写 0.0.0.0')
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except OSError as ex:
        raise StartupError(f'{source} 不是有效的监听地址（{ex}）。'
                           '只让这台电脑访问写 127.0.0.1，让同一局域网里的设备也能访问写 0.0.0.0') from ex
    return host, port


def _is_ipv6_wildcard(host):
    '''是不是 IPv6 的"所有地址"（::，或者它的别的写法）'''
    try:
        address = ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        return False
    return address.version == 6 and address.is_unspecified


def _local_host(host):
    '''在这台电脑上连这个监听地址时用的地址：0.0.0.0 不能直接连，换成 127.0.0.1'''
    return '127.0.0.1' if host == ALL_ADDRESSES else host


def _page_url(host, port):
    '''在这台电脑的浏览器里打开网页用的网址（IPv6 地址在网址里要加方括号）'''
    address = _local_host(host)
    return f'http://[{address}]:{port}' if ':' in address else f'http://{address}:{port}'


def _is_loopback(host):
    '''这个监听地址是不是只有本机能连'''
    host = host.strip('[]')
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == 'localhost'


def _in_container():
    '''是不是在 Docker / Podman 容器里运行（容器里有这两个标记文件之一）'''
    return any(os.path.exists(marker) for marker in ('/.dockerenv', '/run/.containerenv'))


def _outgoing_address():
    '''
    系统默认从这台电脑的哪个地址出去，没有联网时返回 None。UDP 的 connect 只是让系统选好从哪个地址出去，
    不发出任何数据；目标是保留给文档用的地址（192.0.2.0/24）
    '''
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('192.0.2.1', 9))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith('127.') or address == '0.0.0.0' else address


def _own_addresses():
    '''
    这台电脑各块网卡（包括虚拟网卡）的 IPv4 地址，查不到时是空集。只在 Windows 上查：那里主机名对应的就是这些地址，
    不用联网；别的系统上主机名常常只对应 127.0.1.1，查它还可能要等 DNS
    '''
    if not ON_WINDOWS:
        return set()
    try:
        found = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return set()
    return {info[4][0] for info in found}


def _is_lan(address):
    return any(ipaddress.ip_address(address) in network for network in LAN_NETWORKS)


def _lan_address():
    '''
    这台电脑在局域网里的地址，拿不准时返回 None。拿得准是指：系统默认从一个局域网网段的地址出去，而且这台电脑的
    所有地址里只有它在局域网网段。装了 TUN 模式的代理或 VPN 时，默认的出口是虚拟网卡（有的虚拟网卡用的也是局域网
    网段的地址）；装了虚拟机时会多出几个局域网网段的地址。这些情况下分不清别的设备该连哪一个，写错了不如不写
    '''
    outgoing = _outgoing_address()
    lan = [address for address in _own_addresses() if _is_lan(address)]
    return outgoing if lan == [outgoing] else None


def _access_notes(host, port):
    '''启动时写进日志的两句话：在哪里打开网页，现在谁能访问、怎么改'''
    notes = [f'浏览器访问 {_page_url(host, port)}']
    unprotected = '程序没有登录保护，不要在公共网络（公共 Wi-Fi 等）上这样用'
    if _is_loopback(host):
        notes.append('现在只有这台电脑能打开。要让同一局域网里的手机或电脑访问，在 config.ini 里写上 [server] host = 0.0.0.0'
                     '（或者设置环境变量 BUSTAG_HOST=0.0.0.0），然后重启')
    elif host != ALL_ADDRESSES:
        notes.append(f'能连到这个地址的设备都可以打开。{unprotected}')
    elif _in_container():
        notes.append('正在监听所有网络地址（在容器里运行）：从容器外面用宿主机的地址和映射出来的端口访问。'
                     '程序没有登录保护，不要把这个端口映射到公网')
    else:
        lan = _lan_address()
        if lan:
            notes.append(f'正在监听所有网络地址：同一局域网里的设备可以用 http://{lan}:{port} 访问。{unprotected}')
        else:
            look_up = ('在命令行里运行 ipconfig，看无线或有线网卡的"IPv4 地址"（一般是 192.168 开头）' if ON_WINDOWS else
                       '在终端里运行 ip addr（macOS 上是 ifconfig），看无线或有线网卡的 inet 地址（一般是 192.168 开头）')
            notes.append(f'正在监听所有网络地址：同一局域网里的设备可以用 http://<这台电脑的局域网 IP>:{port} 访问。'
                         f'这台电脑的局域网 IP 程序拿不准，请自己查：{look_up}。{unprotected}')
    return notes


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


def _is_this_computer(host):
    '''
    这个监听地址是不是这台电脑自己的（回环地址，或者哪块网卡上有它）：能在它上面绑一个套接字就是。
    不是的话，连它连到的是别的机器（或者被代理软件接住的一个假的"对方"），那不是在看本机的端口上有没有人占着
    '''
    host = host.strip('[]')
    if _is_loopback(host):
        return True
    try:
        family = socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
        found = [(family, socket.SOCK_STREAM, 0, '', (host, 0))]
    except ValueError:
        try:
            found = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        except OSError:
            return False
    for family, socktype, proto, _, address in found:
        try:
            probe = socket.socket(family, socktype, proto)
        except OSError:
            # 这台电脑没有这一类地址（系统里关掉了 IPv6 时建不了 IPv6 的套接字）
            continue
        try:
            probe.bind(address)
        except OSError:
            continue
        else:
            return True
        finally:
            probe.close()
    return False


def _who_listens(host, port):
    '''
    端口上现在是谁，返回 (哪一类, 版本号, 它是不是锁着自己的数据目录)：('bustag', 版本号, True / False)；
    ('other', None, False) 有程序在听、但不是 bustag；(None, None, False) 连不上，没有程序在听，
    或者这个地址不是这台电脑的（那就不去连它）。直接连本机，不经过任何代理
    '''
    address = _local_host(host)
    if not _is_this_computer(address):
        return None, None, False
    try:
        socket.create_connection((address, port), timeout=CONNECT_TIMEOUT).close()
    except OSError:
        return None, None, False
    connection = http.client.HTTPConnection(address, port, timeout=PROBE_TIMEOUT)
    try:
        connection.request('GET', '/about')
        reply = connection.getresponse()
        locks = reply.getheader(LOCK_HEADER) == '1'
        page = reply.read(MAX_PROBE_SIZE).decode('utf-8', errors='replace')
    except (OSError, http.client.HTTPException):
        return 'other', None, False
    finally:
        connection.close()
    found = BUSTAG_FOOTER.search(page)
    return ('bustag', found.group(1), locks) if found else ('other', None, False)


def _port_taken(host, port, who, locked):
    '''
    端口上已经有程序在听（who 是 _who_listens 的结果）时的报错，给出照做不会出问题的办法：locked 表示这个数据目录
    已经被本进程锁住，那么同样锁着数据目录的另一个 bustag 用的一定是别的数据目录，换个端口一起运行是安全的。
    对方没有说自己锁着数据目录时（旧版本，或者它那边锁不上）不下这个结论
    '''
    kind, version, locks = who
    change_port = f'设置环境变量 BUSTAG_PORT 换一个端口再启动，例如 {port + 1 if port < 65535 else port - 1}'
    if kind != 'bustag':
        return StartupError(f'端口 {port} 被别的程序占着（不是 bustag）。请{change_port}')
    url = _page_url(host, port)
    if locked and locks:
        return StartupError(f'端口 {port} 被另一个 bustag（v{version}）占着，它用的是别的数据目录，页面在 {url}。'
                            f'要同时运行这一个（数据目录 {get_data_dir()}），请{change_port}')
    return StartupError(f'端口 {port} 上已经有一个 bustag（v{version}）在运行，页面在 {url}。'
                        f'没法确认它用的是不是这个数据目录（{get_data_dir()}）：如果是，不要再启动第二个，'
                        f'两个一起用同一个数据目录会出问题；如果不是，可以{change_port}')


def _cannot_listen(host, port, ex, locked):
    '''端口占不上时的报错：有程序在听就说是谁占着，没有就把系统给的原因写出来'''
    who = _who_listens(host, port)
    if who[0] is None:
        return StartupError(f'无法监听 {host}:{port}（{ex}）。请检查监听地址是不是这台电脑的地址、这个端口能不能用')
    return _port_taken(host, port, who, locked)


def _port_is_held(port, exclusive):
    '''
    这个端口上有没有任何程序占着，不管它监听的是哪个地址、IPv4 还是 IPv6：对"所有地址"独占试绑。
    IPv6 的 ::（IPV6_V6ONLY 关掉，同时管 IPv4）连 node 这类监听 :: 同时收 IPv4 的程序也看得到；
    这台电脑没有 IPv6 时试 IPv4 的 0.0.0.0。绑得上返回 False；绑不上返回 True：多半是真有程序占着，
    也可能占着的只在别的具体地址上（127.0.0.2），那不妨碍监听这一个，要连一下才分得清。
    其他的错（设不了选项等）也返回 True：看不出来就当有人占着
    '''
    for family, address in ((socket.AF_INET6, ('::', port, 0, 0)), (socket.AF_INET, ('0.0.0.0', port))):
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            if family == socket.AF_INET6:
                probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            probe.bind(address)
        except OSError:
            return True
        finally:
            probe.close()
        return False
    return True


def _ensure_port_free(host, port, locked):
    '''
    端口已经被占着就报错，不等 waitress 去绑定，分三步查：
    1. Windows 上 SO_REUSEADDR 允许两个进程绑定同一个端口，waitress 默认就设了它：第二个实例不会报错，
       请求会被两个实例随机分走。所以先用独占方式试绑（别的系统没有这种绑法，跳过这一步）。
    2. 对方监听的是所有地址（0.0.0.0）、这边要监听一个具体的地址（127.0.0.1）时，Windows 上独占试绑也是成功的，
       macOS 上 waitress 自己的绑定也成功，两个实例会同时运行。所以再连一下：连得上就是已经有程序在听。
       Windows 上连一个没人听的本机端口要等满超时才返回，每次启动都白等，所以先对所有地址独占试绑一次，
       试得上就是哪里都没有程序，不用连；别的系统连不上是立刻的，照连
    '''
    exclusive = getattr(socket, 'SO_EXCLUSIVEADDRUSE', None)
    if exclusive is not None:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        for family, socktype, proto, _, address in infos:
            probe = socket.socket(family, socktype, proto)
            try:
                probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
                probe.bind(address)
            except OSError as ex:
                raise _cannot_listen(host, port, ex, locked) from ex
            finally:
                probe.close()
        if not _port_is_held(port, exclusive):
            return
    who = _who_listens(host, port)
    if who[0] is not None:
        raise _port_taken(host, port, who, locked)


def _serve():
    host, port = _listen_address()
    # 先上锁：已经有一个网页服务在用这个数据目录时，这里就停下，不去碰它正在写的日志文件
    locked = _lock_data_dir(_page_url(host, port))
    start_file_log()
    log_uncaught_exceptions()
    logger.info(f'bustag v{__version__} 启动，数据目录 {get_data_dir()}')
    _ensure_port_free(host, port, locked)
    try:
        server = create_server(application, host=host, port=port, threads=SERVER_THREADS,
                               channel_request_lookahead=REQUEST_LOOKAHEAD)
    except OSError as ex:
        raise _cannot_listen(host, port, ex, locked) from ex
    create_app()
    _exit_on_sigterm()
    for note in _access_notes(host, port):
        logger.info(note)
    server.run()
    logger.info('服务已停止')


def main():
    freeze_support()
    if getattr(sys, 'frozen', False) and len(sys.argv) > 1:
        # exe 不带参数（双击）时启动网页服务；在命令行里带参数运行（bustag.exe check 等）时交给命令行工具，
        # 和源码的 python -m bustag.main 一样，参数不认识时打印用法
        command_line(args=sys.argv[1:], prog_name=os.path.basename(sys.executable))
        return
    try:
        _serve()
    except KeyboardInterrupt:
        pass
    except StartupError as ex:
        logger.error(ex)
        # 没有启动起来（定时任务也还没有开始）。双击 exe 时窗口要等按回车才关，这期间不该还占着数据目录：
        # 照提示换个端口再启动的那一个会被它挡住
        instance.release()
        wait_for_enter_if_console()
        sys.exit(1)
    except Exception:
        logger.exception('服务异常退出')
        wait_for_enter_if_console()
        sys.exit(1)


if __name__ == '__main__':
    main()
