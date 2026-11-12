'''
persist data to db
'''
from datetime import date
import datetime
import itertools
import operator
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import reduce
import json
from peewee import *
from peewee import Expression
from enum import IntEnum
from collections import defaultdict, namedtuple
from string import ascii_lowercase, ascii_uppercase
from urllib.parse import urlparse
from bustag import hanzi
from bustag.util import exit_on_startup_error, logger, get_data_path, format_datetime, get_now_time, get_full_url
from bustag.spider.covers import normalize_cover_url

DB_FILE = 'bus.db'
# 写锁被别的连接占着时（另一个进程，或者正在写分数的后台线程）最多等这么多秒，等不到就是 database is locked。
# SQLite 按名义时长累计，实际等的常是它的两倍多（Windows、机器忙时），所以说等不到时不写秒数
BUSY_TIMEOUT = 30
DATABASE_BUSY = '数据库正被别的程序占用（等了好一会儿还没轮到）'
DB_OPTIONS = {'pragmas': {'journal_mode': 'wal'}, 'timeout': BUSY_TIMEOUT}
# 同时在等写锁的网页请求最多这么多个：服务一共只有 8 个线程，别的程序占着写锁时每个写请求都要干等 BUSY_TIMEOUT 秒，
# 8 个都在等的话，连只读的页面也没有线程来处理。多出来的写请求立刻得到"数据库正忙"
MAX_WAITING_REQUESTS = 4
DATABASE_CROWDED = '数据库正被别的程序占用，已经有好几个操作在等它了，不再多等一个'
# 从没换成 WAL 的老库：每条新连接换 WAL、换不了时读一下，各最多等这么多秒。别的程序以 EXCLUSIVE 锁占着整个库时
# 等满 BUSY_TIMEOUT 的话，每个请求都要干等两次 30 秒，线程全卡住，连静态文件都没人答复
LOCKED_WAIT = 0.5
DATABASE_LOCKED_OUT = '数据库正被别的程序独占着，现在连读都读不了'
_limit = threading.local()
_waiting = 0
_waiting_lock = threading.Lock()


class TooManyWaiting(OperationalError):
    '''已经有 MAX_WAITING_REQUESTS 个网页请求在等写锁，这一个不再等'''


class LockedOut(OperationalError):
    '''从没换成 WAL 的老库被别的程序独占着（连读都不让）：这次的连接用不了，不等满 BUSY_TIMEOUT 就报'''


@contextmanager
def _limited(on):
    before = getattr(_limit, 'on', False)
    _limit.on = on
    try:
        yield
    finally:
        _limit.on = before


def limited_waiting():
    '''
    网页请求处理期间：这个线程开始写事务（BEGIN IMMEDIATE）时，等写锁的请求已经有 MAX_WAITING_REQUESTS 个，
    就不再等，抛 TooManyWaiting。后台任务和命令行不在里面，照常等
    '''
    return _limited(True)


def unlimited_waiting():
    '''在 limited_waiting() 里面开一段不受限制的：训练（一次只有一个，打分等不到锁时页面上写的是"等了好一会儿"）'''
    return _limited(False)


def waiting_requests():
    '''现在有几个网页请求在等写锁'''
    return _waiting


def _briefly(conn, sql):
    '''执行 sql；库被锁着时隔一小会儿再试，最多试 LOCKED_WAIT 秒（按钟表算），还不行就抛出最后那次的错'''
    deadline = time.monotonic() + LOCKED_WAIT
    while True:
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.OperationalError as ex:
            if 'locked' not in str(ex) or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


class _Database(SqliteDatabase):
    '''SqliteDatabase，只是网页请求一开始就要写锁的事务（BEGIN IMMEDIATE）受 MAX_WAITING_REQUESTS 的限制'''

    def _set_pragmas(self, conn):
        # 从没换成 WAL 的老库，换的时候要独占它：被别的程序占着时只试一下（LOCKED_WAIT），换不了就照原来的方式打开
        # （读照常，写照常等锁），以后哪一条连接打开时没人占着，就换过去。别的程序独占着整个库（EXCLUSIVE，连读都不让）
        # 时这条连接用不了：读一下也只试一下，读不了就报 LockedOut，这次请求很快得到"数据库正忙"。
        # 这两处不用 SQLite 自己的等待（busy_timeout）：它按名义时长累计，Windows 上实际要等一倍多，机器忙时更久
        conn.execute('PRAGMA busy_timeout = 0')
        try:
            try:
                for pragma, value in self._pragmas:
                    _briefly(conn, f'PRAGMA {pragma} = {value}')
            except sqlite3.OperationalError as ex:
                if 'locked' not in str(ex):
                    raise
                try:
                    _briefly(conn, 'SELECT COUNT(*) FROM sqlite_master')
                except sqlite3.OperationalError as error:
                    if 'locked' not in str(error):
                        raise
                    raise LockedOut('database is locked (another program holds it exclusively)') from error
        finally:
            conn.execute(f'PRAGMA busy_timeout = {int(self._timeout * 1000)}')

    def begin(self, lock_type=None):
        # 普通的事务（BEGIN）开始时不等，到第一次写的时候才拿锁，不在这里算
        if not getattr(_limit, 'on', False) or not (lock_type or self._lock_type):
            return super().begin(lock_type)
        global _waiting
        with _waiting_lock:
            if _waiting >= MAX_WAITING_REQUESTS:
                raise TooManyWaiting(f'database is locked ({_waiting} requests are already waiting for it)')
            _waiting += 1
        try:
            super().begin(lock_type)
        finally:
            with _waiting_lock:
                _waiting -= 1


db = _Database(get_data_path(DB_FILE), **DB_OPTIONS)
# fold 用的对照：英文大写字母 → 小写，繁体字 → 简体字
_FOLDED = str.maketrans(ascii_uppercase + hanzi.TRADITIONAL, ascii_lowercase + hanzi.SIMPLIFIED)
# 一部影片的打分结果：番号、分数（0 到 1，越高越像用户喜欢的）、拉高分数的标签值、拉低分数的标签值
Score = namedtuple('Score', 'fanhao score because against')
# save_scores 的结果：打了分的部数、其中算"推荐"的部数、因为已经是用户打标（或已不在库里）而跳过的部数
ScoreResult = namedtuple('ScoreResult', 'scored recommended skipped')
# 分数和系统评分分批写入，每批的行数受 SQLite 单条语句参数个数的限制
WRITE_BATCH = 100
# IN 列表一次最多放这么多个参数（旧版 SQLite 一条语句最多 999 个）
IN_BATCH = 500
# 列表一页的影片数
PAGE_SIZE = 10
# dislike_untagged 的结果：标成不喜欢的番号、因为已经是用户打标而没动的部数、库里没有而忽略的番号个数
DislikeResult = namedtuple('DislikeResult', 'tagged kept unknown')


class BaseModel(Model):

    class Meta:
        database = db
        legacy_table_names = False


class ExistError(Exception):
    pass


class DBError(Exception):
    pass


def is_busy(error):
    '''这个异常是不是"写锁被别的连接占着"：等到超时还没轮到，或者等的请求已经太多、这一个不再等'''
    return isinstance(error, TooManyWaiting) or (isinstance(error, OperationalError) and 'locked' in str(error))


def locked_out(error):
    '''
    这个异常是不是 LockedOut。连接时出的错 peewee 会换成它自己的 OperationalError 再抛（查询时顺手连接的话还要再换一层），
    原来的异常在 __context__ 那一串里
    '''
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, LockedOut):
            return True
        seen.add(id(error))
        error = error.__context__
    return False


def busy_reason(error):
    '''"数据库正忙"的原因，一句话，给用户看：等锁等到超时，或者等的请求已经太多、这一个没有等，或者老库被别的程序独占着'''
    if isinstance(error, TooManyWaiting):
        return DATABASE_CROWDED
    if locked_out(error):
        return DATABASE_LOCKED_OUT
    return DATABASE_BUSY


def fold(text):
    '''
    比较文字时用的写法：去掉首尾空白，英文字母不分大小写（只管 ASCII 字母），繁体字换成简体字。
    搜索、输入联想、判断名字相同，都是把库里的值和输入的文字各这样折一遍再比，所以简体和繁体互通；
    日文的写法（気、学 这类新字体和假名）不转换
    '''
    return text.strip().translate(_FOLDED)


def _spellings():
    '''折成同一个字的各种写法（fold 的反面）：折过的字 → 它的所有写法连在一起的字符串（含它自己）'''
    alike = defaultdict(set)
    for traditional, simplified in zip(hanzi.TRADITIONAL, hanzi.SIMPLIFIED):
        alike[simplified].update((simplified, traditional))
    for lower, upper in zip(ascii_lowercase, ascii_uppercase):
        alike[lower].update((lower, upper))
    return {char: ''.join(sorted(chars)) for char, chars in alike.items()}


_SPELLINGS = _spellings()
# 一个名字折过以后的各种写法最多列出这么多种（见 spellings），再多就换成 GLOB 去比
MAX_SPELLINGS = 256
# str.strip() 去掉的那些空白字符
_BLANKS = ''.join(char for char in map(chr, range(0x3001)) if char.isspace())


def spellings(folded):
    '''
    folded 是折过（fold）的文字，返回折过以后和它完全相同的各种写法：每个字的简体和繁体、英文字母的大小写，
    两头不带空白。拿它们用 IN 去比，库里的值就不用交给 Python 去折，还能用上索引。多于 MAX_SPELLINGS 种时返回 None
    '''
    options = [_SPELLINGS.get(char, char) for char in folded]
    total = 1
    for option in options:
        total *= len(option)
        if total > MAX_SPELLINGS:
            return None
    return [''.join(chars) for chars in itertools.product(*options)]


def equals_pattern(folded):
    '''GLOB 模式：值逐字折过以后等于 folded（两头不带空白）。写法太多、spellings 列不出来时用它'''
    parts = []
    for char in folded:
        if char in _SPELLINGS:
            parts.append(f'[{_SPELLINGS[char]}]')
        elif char in '*?[':
            parts.append(f'[{char}]')
        else:
            parts.append(char)
    return ''.join(parts)


def padded(column):
    '''SQL 条件：column 的值开头或结尾有空白。这样的值折过以后两头的空白就没了，spellings 和 equals_pattern 比不到它'''
    return glob(column, f'[{_BLANKS}]*') | glob(column, f'*[{_BLANKS}]')


def contains_pattern(text):
    '''
    GLOB 模式：值里有一段文字，逐字折过（fold）以后和 text 折过的一样，就匹配，也就是"fold(值) 里含有 fold(text)"。
    在 SQLite 里直接比，不像 bustag_fold(列) 那样每一行都要从 SQLite 调回 Python 一次：那样几个线程同时搜索，
    每次调回都要抢解释器锁，互相拖慢，比一个一个搜还慢好几倍。每个字写成它的各种写法里的任意一个，
    GLOB 里有特殊意思的字符（* ? [）按字面写。值两头的空白不用管：text 折过以后两头没有空白，
    匹配到的一段不会从值两头的空白里开始或结束
    '''
    parts = ['*']
    for char in fold(text):
        if char in _SPELLINGS:
            parts.append(f'[{_SPELLINGS[char]}]')
        elif char in '*?[':
            parts.append(f'[{char}]')
        else:
            parts.append(char)
    parts.append('*')
    return ''.join(parts)


def glob(column, pattern):
    '''SQL 条件 column GLOB pattern（peewee 没有给它一个方法）'''
    return Expression(column, 'GLOB', pattern)


@db.func('bustag_fold', 1, deterministic=True)
def _fold_in_sql(value):
    '''SQL 里的 bustag_fold(列)：库里的值也用 fold 来折，和输入的文字用的是同一个函数'''
    return None if value is None else fold(str(value))


class Item(BaseModel):
    '''
    item table
    '''
    title = CharField()
    fanhao = CharField(unique=True)
    url = CharField(unique=True)
    release_date = DateField()
    add_date = DateTimeField(default=datetime.datetime.now)
    meta_info = TextField()

    # 各张表的 __repr__ 和这里的日志只写番号和编号：它们会出现在日志和异常栈里，
    # 那里不该有影片标题、标签和演员的名字、本地文件的地址
    def __repr__(self):
        return f'<Item {self.fanhao}>'

    @staticmethod
    def saveit(meta_info):
        item_release_date = date.fromisoformat(meta_info.pop('release_date'))
        item_fanhao = meta_info.pop('fanhao')
        item_title = meta_info.pop('title')
        item_url = urlparse(meta_info.pop('url')).path
        item_meta = json.dumps(meta_info)
        try:
            with db.atomic():
                item = Item.create(fanhao=item_fanhao, title=item_title, url=item_url,
                                   release_date=item_release_date, meta_info=item_meta)
            logger.debug(f'已保存影片 {item_fanhao}')
        except IntegrityError:
            logger.debug(f'影片已在库里：{item_fanhao}')
            raise ExistError()
        else:
            return item

    @staticmethod
    def loadit(item):
        item.url = get_full_url(item.url)
        meta = json.loads(item.meta_info or '{}')
        item.cover_img_url = normalize_cover_url(meta.get('cover_img_url'))
        item.add_date = format_datetime(item.add_date)

    @staticmethod
    def get_by_fanhao(fanhao):
        item = Item.get_or_none(Item.fanhao == fanhao)
        return item

    @staticmethod
    def get_tags_dict(item):
        tags_dict = defaultdict(list)
        for t in item.tags_list:
            tags_dict[t.tag.type_].append(t.tag.value)
        item.tags_dict = tags_dict


class Tag(BaseModel):
    '''
    tag table
    '''
    type_ = CharField(column_name='type')
    value = CharField()
    url = CharField()

    class Meta:
        indexes = (
            # Specify a unique multi-column index
            (('type_', 'value'), True),
        )

    def __repr__(self):
        return f'<Tag {self.id} {self.type_}>'

    @staticmethod
    def saveit(tag_info):
        tag, created = Tag.get_or_create(type_=tag_info.type, value=tag_info.value,
                                         defaults={'url': urlparse(tag_info.link).path})
        if created:
            logger.debug(f'已保存标签 #{tag.id}（{tag.type_}）')
        return tag


class ItemTag(BaseModel):
    item = ForeignKeyField(Item, field='fanhao', backref='tags_list')
    tag = ForeignKeyField(Tag, backref='items')

    class Meta:
        indexes = (
            # Specify a unique multi-column index
            (('item', 'tag'), True),
        )

    @staticmethod
    def saveit(item, tag):
        try:
            item_tag = ItemTag.create(item=item, tag=tag)
            logger.debug(f'影片 {item.fanhao} 加上标签 #{tag.id}')
        except Exception:
            logger.exception(f'影片 {item.fanhao} 加标签 #{tag.id} 失败')
        else:
            return item_tag

    def __repr__(self):
        return f'<ItemTag {self.item_id} - {self.tag_id}>'


class RATE_TYPE(IntEnum):
    NOT_RATE = 0
    USER_RATE = 1
    SYSTEM_RATE = 2


class RATE_VALUE(IntEnum):
    LIKE = 1
    DISLIKE = 0


def describe_rate(rate_type, rate_value):
    '''评分在日志里的说法，例如"用户打标 喜欢"'''
    kind = {RATE_TYPE.USER_RATE: '用户打标', RATE_TYPE.SYSTEM_RATE: '系统推荐'}.get(rate_type, f'类型 {rate_type}')
    value = {RATE_VALUE.LIKE: '喜欢', RATE_VALUE.DISLIKE: '不喜欢'}.get(rate_value, f'值 {rate_value}')
    return f'{kind} {value}'


class ItemRate(BaseModel):
    rate_type = IntegerField()
    rate_value = IntegerField()
    item = ForeignKeyField(Item, field='fanhao',
                           backref='rated_items', unique=True)
    rete_time = DateTimeField(default=datetime.datetime.now)

    @staticmethod
    def saveit(rate_type, rate_value, fanhao):
        item_rate = None
        try:
            item_rate = ItemRate.create(
                item=fanhao, rate_type=rate_type, rate_value=rate_value)
            logger.debug(f'已保存评分 {fanhao}：{describe_rate(rate_type, rate_value)}')
        except IntegrityError:
            logger.debug(f'{fanhao} 已经有评分，没有改动')
        else:
            return item_rate

    @staticmethod
    def save_user_rate(fanhao, rate_value, replace_user_rate=False):
        '''
        保存用户打标：没有评分时新增；系统推荐的评分改成用户打标；已经是用户打标时，
        replace_user_rate 为真才改（打标页上用户改自己的打标），批量打标和导入不改。
        返回 'added'（原来没有评分）、'updated'（原来是系统推荐的评分）、'changed'（改了用户自己打的标）
        或 'kept'（已经是用户打标，没有动）。
        用户打了标的影片不再留着模型给它的分数（分数只属于还没打标的影片）。先查后写再删分数，
        都在一个事务里，一开始就拿写锁：要么都做了，要么都没做
        '''
        with db.atomic('IMMEDIATE'):
            ItemScore.delete().where(ItemScore.item == fanhao).execute()
            item_rate = ItemRate.get_by_fanhao(fanhao)
            if item_rate is None:
                ItemRate.create(item=fanhao, rate_type=RATE_TYPE.USER_RATE, rate_value=rate_value)
                return 'added'
            by_user = item_rate.rate_type == RATE_TYPE.USER_RATE
            if by_user and not replace_user_rate:
                return 'kept'
            item_rate.rate_type = RATE_TYPE.USER_RATE
            item_rate.rate_value = rate_value
            item_rate.rete_time = get_now_time()
            item_rate.save()
            return 'changed' if by_user else 'updated'

    @staticmethod
    def get_by_fanhao(fanhao):
        item_rate = ItemRate.get_or_none(ItemRate.item_id == fanhao)
        return item_rate


class LocalItem(BaseModel):
    '''
    local item table
    '''
    item = ForeignKeyField(Item, field='fanhao',
                           backref='local_item', unique=True)
    path = CharField(null=True)
    size = IntegerField(null=True)
    add_date = DateTimeField(default=datetime.datetime.now)
    last_view_date = DateTimeField(null=True)
    view_times = IntegerField(default=0)

    @staticmethod
    def saveit(fanhao, path):
        local_item = None
        try:
            local_item = LocalItem.create(
                item=fanhao, path=path)
            logger.debug(f'已登记本地文件 {fanhao}')
        except IntegrityError:
            logger.debug(f'{fanhao} 已经登记过本地文件')
        else:
            return local_item

    def __repr__(self):
        return f'<LocalItem {self.item_id}>'

    @staticmethod
    def update_play(id):
        with db.atomic('IMMEDIATE'):
            nrows = (LocalItem
                     .update({LocalItem.last_view_date: get_now_time(),
                              LocalItem.view_times: LocalItem.view_times+1})
                     .where(LocalItem.id == id)
                     .execute())
        logger.debug(f'本地文件 {id} 观看次数加 1（更新了 {nrows} 行）')
        return LocalItem.get_by_id(id)

    @staticmethod
    def loadit(local_item):
        local_item.last_view_date = format_datetime(
            local_item.last_view_date) if local_item.last_view_date else ''


class ItemCandidate(BaseModel):
    '''
    同一个識別碼在 JavBus 上的其他详情路径。path 是 /番号 或 /番号_日期
    '''
    item = ForeignKeyField(Item, field='fanhao', backref='candidates')
    path = CharField()
    release_date = DateField(null=True)

    class Meta:
        indexes = (
            (('item', 'path'), True),
        )


class ItemScore(BaseModel):
    '''
    模型给影片打的分数，只有还不是用户打标的影片才有。score 在 0 到 1 之间，越高越像用户喜欢的；
    because、against 是拉高、拉低分数最多的几个标签值（JSON 数组）。每次打分整表重写（见 save_scores）
    '''
    item = ForeignKeyField(Item, field='fanhao', backref='scores', unique=True)
    score = FloatField()
    because = TextField()
    against = TextField()


# 补演员名的结果：补到了 / av-wiki 上没有 / 网络失败 / 按旧的存法补过、要按新存法重新查一次（迁移后的校正）
LOOKUP_FILLED = 'filled'
LOOKUP_MISSING = 'missing'
LOOKUP_FAILED = 'failed'
LOOKUP_STALE = 'stale'


class ActressLookup(BaseModel):
    '''
    到 av-wiki 补演员名的记录，试过的影片每部一行，只留最近一次：status 是结果（上面三个之一），
    misses 是连续几次"av-wiki 上没有"，checked_at 是试的时间，next_try 之前不再试，detail 是没补到的原因
    '''
    item = ForeignKeyField(Item, field='fanhao', backref='actress_lookups', unique=True)
    status = CharField()
    misses = IntegerField(default=0)
    checked_at = DateTimeField()
    next_try = DateTimeField()
    detail = TextField(default='')


# 抓取队列里一部影片的状态：在排队 / 抓到了（或者轮到时库里已经有了）/ 失败 / 网站上没有
TASK_QUEUED = 'queued'
TASK_DONE = 'done'
TASK_FAILED = 'failed'
TASK_MISSING = 'missing'
# 排进抓取队列的来处：上传番号 / 导入打标库 / 某个列表页（演员页）的补全
ORIGIN_UPLOAD = 'upload'
ORIGIN_IMPORT = 'import'
ORIGIN_LIST = 'list'


class ListSource(BaseModel):
    '''
    补全作品用的来源列表页，一页一行（现在只有 JavBus 的演员页，kind 是 star），path 是列表第 1 页的路径。
    magnets、total 是页面上写的"已有磁力 N""全部影片 M"（读不到时为空），checked_at 是上次读第 1 页的时间；
    checking 是用户点了"查看"、后台还没读；wanted 是用户点了"补全"、这一次还要排进队列的部数，requested_at 是点的时间；
    next_page 是下次往回补从第几页读起（0 是还没补过），reached_end 是已经翻到了最后一页，oldest 是经过的最早一部的
    发行日期；error、error_at 是最近一次出错的原因和时间，之后读成功了就清掉
    '''
    kind = CharField(default='star')
    path = CharField(unique=True)
    magnets = IntegerField(null=True)
    total = IntegerField(null=True)
    checked_at = DateTimeField(null=True)
    checking = BooleanField(default=False)
    wanted = IntegerField(default=0)
    requested_at = DateTimeField(null=True)
    next_page = IntegerField(default=0)
    reached_end = BooleanField(default=False)
    oldest = DateField(null=True)
    error = TextField(default='')
    error_at = DateTimeField(null=True)

    def __repr__(self):
        return f'<ListSource {self.id}>'


class FetchTask(BaseModel):
    '''
    抓取队列：要抓的详情页，同一个详情页（path）只排一次。origin 是从哪来的（ORIGIN_*），source 是哪个列表页、
    page 是在它的第几页找到的（只有 origin 是 list 时才有）。status 见 TASK_*，detail 是没抓到的原因，
    tries 是因为网络问题试过的次数，tried_at 是最近一次真的发了请求的时间（一天抓了多少部按它数）
    '''
    path = CharField(unique=True)
    fanhao = CharField()
    origin = CharField()
    source = ForeignKeyField(ListSource, null=True, backref='tasks')
    page = IntegerField(null=True)
    status = CharField(default=TASK_QUEUED)
    detail = TextField(default='')
    tries = IntegerField(default=0)
    added_at = DateTimeField(default=datetime.datetime.now)
    tried_at = DateTimeField(null=True)

    class Meta:
        indexes = (
            (('status', 'origin'), False),
            (('source', 'status'), False),
        )

    def __repr__(self):
        return f'<FetchTask {self.fanhao}>'


# 给一位女优查找 JavBus 演员页的结果：等着查 / 已采用 / 有几位、等用户选 / 没找到 / 出错。
# 另有一种行记着用户说过"不是她"的演员页（键见 actresses.NOT_HER）
LINK_PENDING = 'pending'
LINK_FOUND = 'found'
LINK_CHOOSE = 'choose'
LINK_NONE = 'none'
LINK_ERROR = 'error'
LINK_REJECTED = 'rejected'


class StarLink(BaseModel):
    '''
    只有 av-wiki 名字（或者库里还没有带这个名字的影片）的女优，到 JavBus 按名字找到的演员页。
    key 是这个人：avwiki:<女优页的路径>（同一个女优页的几个名字共用）或 name:<折叠后的名字>；name 是拿去搜的名字；
    state 见 LINK_*；candidates 是搜到的演员（JSON：[[演员页路径, 名字], …]）；path 是采用的演员页；
    checked_at、error 是查的时间、出错的原因
    '''
    key = CharField(unique=True)
    name = CharField()
    state = CharField(default=LINK_PENDING)
    candidates = TextField(default='[]')
    path = CharField(default='')
    checked_at = DateTimeField(null=True)
    error = TextField(default='')

    @property
    def candidate_list(self):
        '''搜到的演员：[[演员页路径, 名字], …]'''
        return json.loads(self.candidates or '[]')

    def __repr__(self):
        return f'<StarLink {self.id}>'


class JobState(BaseModel):
    '''后台任务要跨过重启记住的几件事，一件一行：name 是哪一件，text、at 是内容和时间'''
    name = CharField(unique=True)
    text = TextField(default='')
    at = DateTimeField(null=True)


def in_values(column, values):
    '''
    column IN values。值整个作为一个 JSON 数组交给 SQLite（json_each 展开）：几千个值也只有一个参数，
    不用分段（旧版 SQLite 一条语句的参数个数有上限），组 SQL 时也不用在 Python 里一个个处理。values 为空时返回 None
    '''
    values = list(values)
    if not values:
        return None
    return column.in_(SQL('(SELECT value FROM json_each(?))', [json.dumps(values)]))


def fetch_rows(query, *columns):
    '''
    查询的全部行（元组的列表），columns 是结果里各列的名字。SQLite 把结果拼成一个 JSON 数组、一次交回来：
    一行一行地取，每取一行都要放开、再抢一次解释器锁，几千行的结果在几个请求同时进行时互相等，比排队还慢。
    行的先后不一定和查询里的 ORDER BY 一样，要先后时取回来以后自己排。
    只收整数、小数、文字和空值。整数、文字取回来和逐行取的一样；小数在常用的范围里（大约 1e-80 到 1e100）也一样，
    比这更大、更小的末位可能差一两个单位（相对差不到 1e-15：SQLite 把这样的数写成文字时算得不精确）；
    负零取回来是 0.0（SQLite 写数时不留它的负号）；二进制（BLOB）的值 JSON 放不下，SQLite 直接报 OperationalError
    （JSON cannot hold BLOB values）
    '''
    sql, params = query.sql()
    # 小数（REAL）按 17 位有效数字写成 JSON 的数：SQLite 自己写只留 15 位，0.1 + 0.2 取回来就成了 0.3
    names = ', '.join(f'''CASE typeof("{name}") WHEN 'real' THEN json(printf('%!.17g', "{name}")) ELSE "{name}" END'''
                      for name in columns)
    text = db.execute_sql(f'SELECT json_group_array(json_array({names})) FROM ({sql})', params).fetchone()[0]
    return [tuple(row) for row in json.loads(text)]


def found_pages(keys):
    '''这些人里已经找到（采用了）JavBus 演员页的：{db.StarLink 的 key: 演员页路径}'''
    wanted = in_values(StarLink.key, dict.fromkeys(keys))
    if wanted is None:
        return {}
    return dict(fetch_rows(StarLink.select(StarLink.key, StarLink.path).where(wanted & (StarLink.state == LINK_FOUND)),
                           'key', 'path'))


# av-wiki 女优页的路径都以它开头：/av-actress/<slug>/
AVWIKI_PAGE = '/av-actress/'
# 一位女优的资料读到了哪一步：只知道搜索卡片上的名字 / 女优页读过了 / 迁移时从旧数据整理出来、还要读一遍女优页来校正
ACTRESS_CARD = 'card'
ACTRESS_PAGE = 'page'
ACTRESS_LEGACY = 'legacy'


class Actress(BaseModel):
    '''
    一位女优。tag 是她的正式名字（star 标签，影片只挂这一条）；avwiki、javbus 是她的 av-wiki 女优页、JavBus 演员页的路径
    （没有时为空）；state 是资料读到了哪一步（ACTRESS_*），names_at 是最近一次读女优页的时间。
    只有 av-wiki 补来的女优有这一行：JavBus 的演员标签本身就是"一个人一条名字 + 演员页的地址"
    '''
    tag = ForeignKeyField(Tag, unique=True, backref='actress_rows')
    avwiki = CharField(null=True, unique=True)
    javbus = CharField(default='')
    state = CharField(default=ACTRESS_CARD)
    names_at = DateTimeField(null=True)

    def __repr__(self):
        return f'<Actress {self.id}>'


class ActressAlias(BaseModel):
    '''
    女优的其他名字（读音、罗马字、别名），一个名字一行；同一个名字可以属于几位女优。
    key 是 fold(name)：按名字找人时直接拿它和折过的名字比，用得上索引
    '''
    actress = ForeignKeyField(Actress, backref='aliases')
    name = CharField()
    key = CharField(index=True)

    class Meta:
        indexes = (
            (('actress', 'name'), True),
        )

    def __repr__(self):
        return f'<ActressAlias {self.actress_id}>'


def add_aliases(actress, names, spelling=False):
    '''
    给女优记上这些别名，返回新记的个数。去掉首尾空白；空的、和她的正式名字或已经记过的别名折过以后相同的不再记。
    spelling 为真时和正式名字折过以后相同的名字也记一个（第一个）：她自己那一侧的写法（见 actresses._put_aliases）
    '''
    own = Tag.select(Tag.value).where(Tag.id == actress.tag_id).scalar() or ''
    known = set() if spelling else {fold(own)}
    known.update(key for (key,) in ActressAlias.select(ActressAlias.key)
                 .where(ActressAlias.actress == actress.id).tuples())
    rows = []
    for name in names:
        name = (name or '').strip()
        key = fold(name)
        if key and key not in known:
            known.add(key)
            rows.append({'actress': actress.id, 'name': name, 'key': key})
    for batch in chunked(rows, WRITE_BATCH):
        ActressAlias.insert_many(batch).execute()
    return len(rows)


def add_actress(tag, avwiki=None, javbus='', state=ACTRESS_CARD, aliases=(), names_at=None):
    '''新建一位女优：正式名字是 tag（star 标签），aliases 是她的其他名字。返回 Actress'''
    actress = Actress.create(tag=tag, avwiki=avwiki, javbus=javbus or '', state=state, names_at=names_at)
    add_aliases(actress, aliases)
    return actress


def find_item(fanhao):
    '''按番号找；按番号抓取时如果库里把这条记在了另一条路径下，再用路径找'''
    item = Item.get_or_none(Item.fanhao == fanhao)
    if item is not None:
        return item
    return Item.get_or_none(Item.url == '/' + fanhao)


def has_star(fanhao):
    return (ItemTag.select()
            .join(Tag)
            .where((ItemTag.item == fanhao) & (Tag.type_ == 'star'))
            .exists())


def dislike_untagged(fanhaos):
    '''
    把这些番号里还不是用户亲手打标的影片（没有评分的、只有系统评分的）标成不喜欢，返回 DislikeResult。
    已经是用户打标的不动，库里没有的番号忽略。全部在一个事务里：要么都标了，要么都没标
    '''
    tagged, kept, unknown = [], 0, 0
    with db.atomic('IMMEDIATE'):
        for fanhao in fanhaos:
            if Item.get_by_fanhao(fanhao) is None:
                unknown += 1
            elif ItemRate.save_user_rate(fanhao, RATE_VALUE.DISLIKE) == 'kept':
                kept += 1
            else:
                tagged.append(fanhao)
    return DislikeResult(tagged, kept, unknown)


def _candidate_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def save_candidates(fanhao, entries):
    '''用这一次查到的路径换掉旧的候选。番号还不在库里时不记'''
    if Item.get_by_fanhao(fanhao) is None:
        return
    with db.atomic('IMMEDIATE'):
        ItemCandidate.delete().where(ItemCandidate.item == fanhao).execute()
        seen = set()
        for entry in entries:
            path = entry['path']
            if path in seen:
                continue
            seen.add(path)
            ItemCandidate.create(item=fanhao, path=path,
                                 release_date=_candidate_date(entry.get('release_date')))


def other_entries(fanhao):
    '''除了当前这条详情以外的候选，按记下来的顺序'''
    item = Item.get_or_none(Item.fanhao == fanhao)
    if item is None:
        return []
    current = urlparse(item.url).path
    rows = (ItemCandidate.select()
            .where(ItemCandidate.item == fanhao)
            .order_by(ItemCandidate.id))
    return [row for row in rows if row.path != current]


def _replace_tags(item, tags):
    ItemTag.delete().where(ItemTag.item == item.fanhao).execute()
    for tag_info in tags:
        tag = Tag.saveit(tag_info)
        if tag:
            ItemTag.saveit(item, tag)


def _move_rate(old_fanhao, new_fanhao):
    old_rate = ItemRate.get_or_none(ItemRate.item == old_fanhao)
    if old_rate is None:
        return
    ItemRate.delete().where(ItemRate.item == new_fanhao).execute()
    old_rate.item = new_fanhao
    old_rate.save()


def _move_local(old_fanhao, new_fanhao):
    old_local = LocalItem.get_or_none(LocalItem.item == old_fanhao)
    if old_local is None:
        return
    LocalItem.delete().where(LocalItem.item == new_fanhao).execute()
    old_local.item = new_fanhao
    old_local.save()


def _move_candidates(old_fanhao, new_fanhao):
    taken = {row.path for row in ItemCandidate.select().where(ItemCandidate.item == new_fanhao)}
    for row in list(ItemCandidate.select().where(ItemCandidate.item == old_fanhao)):
        if row.path in taken:
            row.delete_instance()
        else:
            row.item = new_fanhao
            row.save()


def _forget_covers(*fanhaos):
    from bustag.spider.covers import forget_cover
    for fanhao in dict.fromkeys(fanhaos):
        try:
            forget_cover(fanhao)
        except OSError as ex:
            logger.warning(f'封面缓存没有清掉 {fanhao}：{ex}')


def replace_with_detail(old_fanhao, meta, tags):
    '''
    用另一条详情替换 old_fanhao。番号变了就把打标和本地文件挪到新番号上，再删掉旧番号，
    旧番号不再留在喜欢列表里。返回新番号
    '''
    meta = dict(meta)
    new_fanhao = meta['fanhao']
    url = urlparse(meta['url']).path
    payload = json.dumps({
        'cover_img_url': meta.get('cover_img_url') or '',
        'length': meta.get('length'),
    })
    release = date.fromisoformat(meta['release_date'])
    with db.atomic('IMMEDIATE'):
        old = Item.get_or_none(Item.fanhao == old_fanhao)
        if old is None:
            raise DBError('没有这个番号')
        if new_fanhao == old_fanhao:
            old.title = meta['title']
            old.url = url
            old.release_date = release
            old.meta_info = payload
            old.save()
            _replace_tags(old, tags)
        else:
            target = Item.get_or_none(Item.fanhao == new_fanhao)
            fields = dict(title=meta['title'], url=url, release_date=release, meta_info=payload)
            if target is None:
                target = Item.create(fanhao=new_fanhao, **fields)
            else:
                for name, value in fields.items():
                    setattr(target, name, value)
                target.save()
            _replace_tags(target, tags)
            _move_rate(old_fanhao, new_fanhao)
            _move_local(old_fanhao, new_fanhao)
            _move_candidates(old_fanhao, new_fanhao)
            ItemTag.delete().where(ItemTag.item == old_fanhao).execute()
            ItemCandidate.delete().where(ItemCandidate.item == old_fanhao).execute()
            Item.delete().where(Item.fanhao == old_fanhao).execute()
        # 分数是按原来的标签算的，标签换了就不作数了，等下一次打分；演员也要按新的详情重新看要不要补
        ItemScore.delete().where(ItemScore.item.in_([old_fanhao, new_fanhao])).execute()
        ActressLookup.delete().where(ActressLookup.item.in_([old_fanhao, new_fanhao])).execute()
    _forget_covers(old_fanhao, new_fanhao)
    return new_fanhao


def _starless():
    '''一个演员都没有的影片，连同评分和补演员名的记录（没有评分、没试过的影片这两张表的各列为空）'''
    has_star = (ItemTag.select(ItemTag.id)
                .join(Tag)
                .where((ItemTag.item == Item.fanhao) & (Tag.type_ == 'star')))
    return (Item.select(Item.fanhao)
            .join(ItemRate, JOIN.LEFT_OUTER)
            .switch(Item)
            .join(ActressLookup, JOIN.LEFT_OUTER)
            .where(~fn.EXISTS(has_star)))


def _lookup_is_due(now):
    return ActressLookup.id.is_null() | (ActressLookup.next_try <= now)


def _tier():
    '''补演员名的先后：0 用户喜欢的，1 在「推荐」里的（系统评分为喜欢），2 其余'''
    return Case(None, [((ItemRate.rate_type == RATE_TYPE.USER_RATE) & (ItemRate.rate_value == RATE_VALUE.LIKE), 0),
                       ((ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE) & (ItemRate.rate_value == RATE_VALUE.LIKE), 1)],
                2)


def starless_due(now, limit=None):
    '''
    现在该去补演员名的影片：没有演员，没试过或者已经到了再试的时间。返回 [(番号, 是不是用户喜欢的)]。
    先后：用户喜欢的 → 在「推荐」里的 → 其余；同一档里没试过的在前、新入库的在前，试过的按上次试的时间从早到晚，
    这样补不上的不会一直排在别的前面
    '''
    query = (_starless()
             .select(Item.fanhao, _tier())
             .where(_lookup_is_due(now))
             .order_by(_tier(), ActressLookup.id.is_null(False), ActressLookup.checked_at, Item.id.desc())
             .limit(limit))
    return [(fanhao, tier == 0) for fanhao, tier in query.tuples()]


def count_starless(now):
    '''(没有演员的影片数, 其中现在该去补的, 其中用户喜欢的, 其中在「推荐」里的)'''
    tier = _tier()
    return tuple(_starless()
                 .select(fn.COUNT(Item.id), fn.COALESCE(fn.SUM(_lookup_is_due(now)), 0),
                         fn.COALESCE(fn.SUM(tier == 0), 0), fn.COALESCE(fn.SUM(tier == 1), 0))
                 .scalar(as_tuple=True))


def _stale():
    return ActressLookup.status == LOOKUP_STALE


def stale_due(now, limit=None):
    '''迁移后要按新存法重新查一次的影片（补名记录是 stale、到了该试的时候）的番号，上次试得早的在前'''
    query = (ActressLookup.select(ActressLookup.item)
             .where(_stale() & (ActressLookup.next_try <= now))
             .order_by(ActressLookup.checked_at, ActressLookup.id)
             .limit(limit))
    return [fanhao for (fanhao,) in query.tuples()]


def count_stale_due(now):
    return ActressLookup.select().where(_stale() & (ActressLookup.next_try <= now)).count()


def save_lookup(fanhao, status, misses, checked_at, next_try, detail=''):
    '''记下这部影片最近一次补演员名的结果，换掉原来的那一条。影片已经不在库里时不记，返回 False'''
    values = dict(status=status, misses=misses, checked_at=checked_at, next_try=next_try, detail=detail)
    with db.atomic('IMMEDIATE'):
        if Item.get_by_fanhao(fanhao) is None:
            return False
        if not ActressLookup.update(**values).where(ActressLookup.item == fanhao).execute():
            ActressLookup.create(item=fanhao, **values)
    return True


def count_lookups(status):
    '''最近一次补演员名是这个结果的影片数'''
    return ActressLookup.select().where(ActressLookup.status == status).count()


def latest_failed_lookup():
    '''
    还在等补演员名的影片里（没有演员的，迁移后要重新查的），最近一次因为网络失败没补到的那条记录；没有时返回 None
    '''
    waiting = ((ActressLookup.status == LOOKUP_FAILED) & ActressLookup.item.in_(_starless())
               | _stale() & (ActressLookup.detail != ''))
    return ActressLookup.select().where(waiting).order_by(ActressLookup.checked_at.desc()).first()


def save(meta_info, tags):
    '''
    影片和标签在一个事务里入库。返回新建的 Item；番号或 url 已存在时返回 None
    '''
    with db.atomic():
        try:
            item = Item.saveit(meta_info)
        except ExistError:
            return None
        for tag_info in tags:
            tag = Tag.saveit(tag_info)
            if tag:
                ItemTag.saveit(item, tag)
    return item


def _clamp_page(page, total_pages):
    '''页码超过总页数时（例如处理完最后一页的最后一部）按最后一页查，没有数据时是第 1 页'''
    return min(page, max(total_pages, 1))


def _clean_keyword(keyword):
    '''搜索词去掉两端空白；空白表示不额外过滤。过长的只留前 100 个字符'''
    if keyword is None:
        return ''
    return str(keyword).strip()[:100]


# 番号后面可以带发行日期（GEBB-062_2026-10-31）；搜索时日期写了一半也算
_DATE_SUFFIX = re.compile(r'_[0-9-]*$')


def _code_fragment(keyword):
    '''
    像番号前缀的片段（至少两个字符，含字母，只由字母数字和横线组成）。
    纯数字不算，避免一位数字把别的番号都带出来
    '''
    folded = keyword.strip().upper()
    if len(folded) < 2 or len(folded) > 40:
        return ''
    if not any('A' <= ch <= 'Z' for ch in folded):
        return ''
    if any(not (ch.isdigit() or 'A' <= ch <= 'Z' or ch == '-') for ch in folded):
        return ''
    return folded


def _fanhao_keyword(keyword):
    '''
    番号条件，整个搜索词都是番号的写法时才有。find_fanhao 能认出的写法（后面可以带发行日期，写了一半也行）
    先规范成带横线的大写，再按开头（含相等和日期后缀）或去掉横线后包含来匹配。认不出时，只接受像番号前缀的片段。
    词里只是夹着一段像番号的字母数字（"s1某某"）不算：按它去找，会把去掉横线后碰巧含这几个字符的番号都带出来
    '''
    from bustag.app.local import find_fanhao
    text = _DATE_SUFFIX.sub('', keyword.strip().upper())
    canonical = find_fanhao(text)
    if canonical and canonical.replace('-', '') == text.replace('-', ''):
        compact = canonical.replace('-', '')
        return (Item.fanhao.startswith(canonical)
                | fn.REPLACE(Item.fanhao, '-', '').contains(compact))
    fragment = _code_fragment(keyword)
    if fragment:
        return Item.fanhao.startswith(fragment)
    return None


def _people_tags(pattern):
    '''
    搜索词碰到了某位女优（她的正式名字、任何一个别名、算她的标签里含这个词）时，算她的全部 star 标签的 id：正式名字，
    以及地址是她的女优页、JavBus 演员页的那些。分几步各查一次（比的还是在 SQLite 里比）：写成嵌着几层子查询的
    一个条件放进列表的查询里时，SQLite 会把它们算好几遍
    '''
    stars = list(Tag.select(Tag.id, Tag.url).where((Tag.type_ == 'star') & glob(Tag.value, pattern)).tuples())
    urls = {url for _, url in stars if url}
    conditions = [condition for condition in (in_values(Actress.tag, [tag_id for tag_id, _ in stars]),
                                              in_values(Actress.avwiki, urls), in_values(Actress.javbus, urls))
                  if condition is not None]
    hit = set(ActressAlias.select(ActressAlias.actress).where(glob(ActressAlias.name, pattern)).scalars())
    if conditions:
        hit.update(Actress.select(Actress.id).where(reduce(operator.or_, conditions)).scalars())
    tags, pages = set(), set()
    wanted = in_values(Actress.id, hit)
    if wanted is not None:
        for tag_id, avwiki, javbus in Actress.select(Actress.tag, Actress.avwiki, Actress.javbus).where(wanted).tuples():
            tags.add(tag_id)
            pages.update(page for page in (avwiki, javbus) if page)
    same_pages = in_values(Tag.url, pages)
    if same_pages is not None:
        tags.update(Tag.select(Tag.id).where((Tag.type_ == 'star') & same_pages).scalars())
    return tags


def _keyword_clause(keyword):
    '''
    标题里含这个词，或者带着值里含这个词的标签，或者番号符合上面的条件。标题、标签值和搜索词都按同一种写法
    比（见 fold）：英文不分大小写，简体繁体互通；比的办法见 contains_pattern，库里的值一个也不用交给 Python 去折。
    标签那一半分两步：先查出值里含这个词的标签和碰到的女优的全部 star 标签（见 _people_tags），作为一个值列表交给
    SQLite；然后逐部影片看它带的标签在不在里面，一个标签也没碰到就不用看。tag_id 写成表达式（+ 0）是为了不让 SQLite
    拿这些标签一个个去查索引：符合的标签可能有上万个，而一部影片只有十来个标签，读出这部影片的标签逐个核对要快得多
    '''
    pattern = contains_pattern(keyword)
    parts = [glob(Item.title, pattern)]
    tags = set(Tag.select(Tag.id).where(glob(Tag.value, pattern)).scalars()) | _people_tags(pattern)
    carried = in_values(ItemTag.tag + 0, tags)
    if carried is not None:
        parts.append(fn.EXISTS(ItemTag.select(SQL('1')).where((ItemTag.item == Item.fanhao) & carried)))
    fanhao = _fanhao_keyword(keyword)
    if fanhao is not None:
        parts.append(fanhao)
    return reduce(operator.or_, parts)


def get_items(rate_type=None, rate_value=None, page=1, page_size=PAGE_SIZE, keyword=None, order_first=()):
    '''
    按评分条件分页取影片。page 为 None 时返回全部；page 超过总页数时返回最后一页，
    page_info 里是实际的页码。
    keyword 非空时，在这些条件里再匹配番号、标题，或已经入库的标签值（英文不分大小写，简体繁体互通）。
    新入库的在前；order_first 里的排序（例如 by_score()）排在它前面
    '''
    clauses = []
    if rate_type is not None:
        clauses.append(ItemRate.rate_type == rate_type)
    else:
        clauses.append(ItemRate.rate_type.is_null())
    if rate_value is not None:
        clauses.append(ItemRate.rate_value == rate_value)
    keyword = _clean_keyword(keyword)
    if keyword:
        clauses.append(_keyword_clause(keyword))
    # 带搜索词时每个标题都要折一遍才能比，条件只算一次
    return list_items(reduce(operator.and_, clauses), order_first=order_first, page=page, page_size=page_size,
                      sort_once=bool(order_first or keyword))


def by_score():
    '''列表的排序：分数高的在前，没有分数的排在最后'''
    return ItemScore.score.is_null(), ItemScore.score.desc()


def _items_with_rate(where):
    return (Item.select(Item, ItemRate, ItemScore)
            .join(ItemRate, JOIN.LEFT_OUTER, attr='item_rate')
            .switch(Item)
            .join(ItemScore, JOIN.LEFT_OUTER, attr='item_score')
            .where(where))


def _load_rate_and_score(item):
    '''把评分和分数放到 item 上：rate_value、score（没有时为 None）、because、against（没有时为空列表）'''
    item_rate = getattr(item, 'item_rate', None)
    item.rate_value = item_rate.rate_value if item_rate is not None else None
    item_score = getattr(item, 'item_score', None)
    scored = item_score is not None and item_score.score is not None
    item.score = item_score.score if scored else None
    item.because = json.loads(item_score.because) if scored else []
    item.against = json.loads(item_score.against) if scored else []


def _page_ids(q, page, page_size):
    '''
    排好序的查询 q 的第 page 页的番号，和符合条件的总数，一条语句查出来：总数用窗口函数数。
    页码超过最后一页时这一页没有行，总数要再数一次。
    只有这一页的番号回到 Python：整个列表的番号一个个交给 Python，几个线程同时这样做，每取一行都要抢解释器锁，
    互相拖慢，比一个一个来还慢好几倍
    '''
    rows = list(q.select(Item.id, fn.COUNT(SQL('*')).over()).limit(page_size).offset((page - 1) * page_size).tuples())
    if rows:
        return [item_id for item_id, _ in rows], rows[0][1]
    return [], q.count()


def list_items(where, order_first=(), page=1, page_size=PAGE_SIZE, sort_once=False):
    '''
    按条件分页取影片，连同评分、分数、标签和显示用的字段。where 里可以用 Item、ItemRate 和 ItemScore 的列
    （没有评分、没有分数的影片这两张表的各列为空）。新入库的在前，order_first 里的排序排在它前面。
    page 为 None 时返回全部；page 超过总页数时返回最后一页，page_info 里是实际的页码。

    默认的做法里 where 要算四次（总数、这一页、这一页的标签两次），只按入库先后排时每次取够一页就停。
    where 算起来慢、order_first 又让 SQLite 必须把全部结果排一遍时用 sort_once：
    一条语句查出这一页的番号和总数（见 _page_ids），where 只算一次
    '''
    items_list = []
    order = (*order_first, Item.id.desc())
    q = _items_with_rate(where).order_by(*order)
    if sort_once and page is not None:
        ids, total_items = _page_ids(q, page, page_size)
        total_pages = (total_items + page_size - 1) // page_size
        clamped = _clamp_page(page, total_pages)
        if clamped != page and total_items:
            ids, _ = _page_ids(q, clamped, page_size)
        page = clamped
        q = _items_with_rate(Item.id.in_(ids)).order_by(*order)
    else:
        total_items = q.count()
        total_pages = (total_items + page_size - 1) // page_size
        if not page is None:
            page = _clamp_page(page, total_pages)
            q = q.paginate(page, page_size)
    items = get_tags_for_items(q)
    for item in items:
        Item.loadit(item)
        _load_rate_and_score(item)
        items_list.append(item)

    page_info = (total_items, total_pages, page, page_size)
    return items_list, page_info


def count_items(where):
    '''符合条件的影片数，where 的写法和 list_items 一样'''
    return _items_with_rate(where).count()


def save_scores(scores, recommended_count):
    '''
    一次打分的结果整体入库：分数表换成这一次的，系统评分按分数重新划分——分数最高的 recommended_count(打分的部数)
    部是"喜欢"（推荐），其余是"不喜欢"（不太像），同分时在 scores 里排在前面的优先。
    scores 是 Score 的列表，返回 ScoreResult。

    全部在一个事务里，一开始就拿写锁。scores 是事务之前算出来的，这期间用户亲手打了标（或者被删掉）的影片
    不算在这一次里：不记分数，用户的打标原样不动。已有的系统评分只有值变了才改（连同时间），
    所以"今日推荐"数的仍是今天才进入推荐的影片
    '''
    now = get_now_time()
    with db.atomic('IMMEDIATE'):
        not_user_rated = ItemRate.rate_type.is_null() | (ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE)
        current = dict(Item.select(Item.fanhao, ItemRate.rate_value)
                       .join(ItemRate, JOIN.LEFT_OUTER)
                       .where(not_user_rated)
                       .tuples())
        ranked = sorted((row for row in scores if row.fanhao in current), key=lambda row: -row.score)
        recommended = min(len(ranked), max(0, recommended_count(len(ranked))))
        new_rates = []
        changed = {RATE_VALUE.LIKE: [], RATE_VALUE.DISLIKE: []}
        for position, row in enumerate(ranked):
            value = RATE_VALUE.LIKE if position < recommended else RATE_VALUE.DISLIKE
            if current[row.fanhao] is None:
                new_rates.append({'item': row.fanhao, 'rate_type': RATE_TYPE.SYSTEM_RATE, 'rate_value': value,
                                  'rete_time': now})
            elif current[row.fanhao] != value:
                changed[value].append(row.fanhao)
        ItemScore.delete().execute()
        for batch in chunked(ranked, WRITE_BATCH):
            ItemScore.insert_many([{'item': row.fanhao, 'score': row.score,
                                    'because': json.dumps(row.because, ensure_ascii=False),
                                    'against': json.dumps(row.against, ensure_ascii=False)}
                                   for row in batch]).execute()
        for value, fanhaos in changed.items():
            for batch in chunked(fanhaos, WRITE_BATCH):
                (ItemRate.update(rate_value=value, rete_time=now)
                 .where(ItemRate.item.in_(batch) & (ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE))
                 .execute())
        for batch in chunked(new_rates, WRITE_BATCH):
            ItemRate.insert_many(batch).on_conflict_ignore().execute()
    return ScoreResult(len(ranked), recommended, len(scores) - len(ranked))


def count_scores(at_least):
    '''(打过分的影片数, 其中分数不低于 at_least 的部数)'''
    return ItemScore.select(fn.COUNT(ItemScore.id),
                            fn.COALESCE(fn.SUM(ItemScore.score >= at_least), 0)).scalar(as_tuple=True)


def get_local_items(page=1, page_size=PAGE_SIZE):
    '''
    有文件地址、影片信息已经下载到的本地文件，新加的在前；page 超过总页数时返回最后一页。
    影片信息还没下载到的不列出，也不算进总数，个数用 count_local_items_without_item() 另查
    '''
    items = []
    q = (LocalItem.select(LocalItem)
         .join(Item)
         .where(LocalItem.path.is_null(False))
         .order_by(LocalItem.id.desc())
         )
    total_items = q.count()
    total_pages = (total_items + page_size - 1) // page_size
    if not page is None:
        page = _clamp_page(page, total_pages)
        q = q.paginate(page, page_size)

    item_query = Item.select()
    item_tag_query = ItemTag.select()
    tag_query = Tag.select()
    items_with_tags = prefetch(q, item_query, item_tag_query, tag_query)

    for local_item in items_with_tags:
        Item.loadit(local_item.item)
        Item.get_tags_dict(local_item.item)
        items.append(local_item)

    page_info = (total_items, total_pages, page, page_size)
    return items, page_info


def count_local_items_without_item():
    '''
    有文件地址、但影片信息还没下载到的本地文件数（正在下载，或者番号在网站上不存在、下载失败）
    '''
    return (LocalItem.select()
            .join(Item, JOIN.LEFT_OUTER)
            .where(LocalItem.path.is_null(False) & Item.id.is_null())
            .count())


def _today(column):
    '''
    column 里的时间在今天：当天零点（含）到第二天零点（不含）。库里的时间是"年-月-日 时:分:秒"的文字，
    按文字比大小就是按时间比；不用 column.year 那样的写法，那在 SQLite 上是每一行调用一次 Python 函数
    '''
    midnight = datetime.datetime.combine(get_now_time().date(), datetime.time.min)
    return (column >= midnight) & (column < midnight + datetime.timedelta(days=1))


def get_today_update_count():
    '''今天入库的影片数'''
    return Item.select().where(_today(Item.add_date)).count()


def get_today_recommend_count():
    '''今天进入"推荐"的影片数：系统评分为喜欢、评分是今天给的'''
    return (ItemRate.select()
            .where(_today(ItemRate.rete_time)
                   & (ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE)
                   & (ItemRate.rate_value == RATE_VALUE.LIKE))
            .count())


def get_tags_for_items(items_query):
    item_tag_query = ItemTag.select()
    tag_query = Tag.select()
    items_with_tags = prefetch(items_query, item_tag_query, tag_query)
    items = []
    for item in items_with_tags:
        Item.get_tags_dict(item)
        items.append(item)

    return items


TABLES = (Item, Tag, ItemTag, ItemRate, LocalItem, ItemCandidate, ItemScore, ActressLookup, ListSource, FetchTask,
          JobState, StarLink, Actress, ActressAlias)
# 打开库时建表等写锁最多这么多秒：库被别的程序一直占着时，启动不跟着等满 BUSY_TIMEOUT，没建成的由后台接着建
CREATE_WAIT = 3
TABLES_MISSING = '新版本要在数据库里加几张表，数据库正被别的程序占用，还没加上'
# 库里还缺新版本要用的表（或索引）：打开库时等不到写锁，没建成。建好以前是 True
tables_missing = False


@contextmanager
def waiting_at_most(seconds):
    '''这一段里，这个线程的连接等写锁（别的连接占着库时）最多 seconds 秒，出来以后恢复成 BUSY_TIMEOUT'''
    db.connect(reuse_if_open=True)
    db.execute_sql(f'PRAGMA busy_timeout = {int(seconds * 1000)}')
    try:
        yield
    finally:
        db.execute_sql(f'PRAGMA busy_timeout = {int(db.timeout * 1000)}')


def create_tables(wait=BUSY_TIMEOUT):
    '''
    建库里还没有的表和索引；都有了的库什么都不用写，也用不着写锁。等写锁最多 wait 秒，等不到时返回 False
    （tables_missing 是 True），建好了返回 True。别的错误照常抛出
    '''
    global tables_missing
    try:
        with waiting_at_most(wait):
            db.create_tables(TABLES)
    except OperationalError as ex:
        # 连接本身也可能用不了（老库被别的程序独占着，见 LockedOut）：看不出缺不缺表，按没建成算，以后再建
        if not is_busy(ex):
            raise
        tables_missing = True
        return False
    tables_missing = False
    return True


NO_JSON = ('bustag 要用 SQLite 的 JSON 函数，这台电脑上 Python 带的 SQLite（{}）没有。SQLite 3.38 以后都自带 JSON 函数：'
           '请换用带着较新 SQLite 的 Python（python.org 下载的安装包都可以），或者用 Docker 镜像、Windows 的 exe')


def json_problem():
    '''
    in_values、fetch_rows 和几条大查询要用 SQLite 的 JSON 函数（3.38 起自带，更早的要编译时打开）。没有时返回一句说明，
    有的话返回空字符串。用一个内存库试：和数据库文件无关，库被别的程序占着时也试得了
    '''
    connection = sqlite3.connect(':memory:')
    try:
        connection.execute("SELECT json_array(1), (SELECT COUNT(*) FROM json_each('[1]'))")
    except sqlite3.OperationalError:
        return NO_JSON.format(sqlite3.sqlite_version)
    finally:
        connection.close()
    return ''


def init():
    '''
    导入时执行：看 SQLite 带不带 JSON 函数，建缺的表。表没建成（tables_missing）时这里不说什么：网页服务会在后台接着建，
    命令行在命令开始前再建一次，各自说各自的
    '''
    problem = json_problem()
    if problem:
        exit_on_startup_error(problem)
    create_tables(CREATE_WAIT)


init()
