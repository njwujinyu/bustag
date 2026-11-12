'''
指定推荐：推荐设定（指定的女优、标签、条件关系，存在数据目录的 recommend.json 里）的读写，
以及按设定在已经入库的影片里筛出用户还没打标的
'''
import json
import operator
import os
import re
import threading
from collections import defaultdict, namedtuple
from dataclasses import dataclass, replace
from functools import reduce

from peewee import SQL, Case, chunked, fn

from bustag.spider import actresses, db
# fold 是比较名字用的写法（去掉首尾空白、英文不分大小写、繁体折成简体）：设定里判重、和库里的标签比，都用它
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ActressAlias, Item, ItemRate, ItemTag, Tag, fold
from bustag.util import get_data_path, logger, os_error_text, save_file

SETTINGS_FILE = 'recommend.json'
SETTINGS_VERSION = 1
MAX_NAMES = 100
# 库里有的标签都要能指定：系列名有六七十个字符的，上限留得宽一些
MAX_NAME_LENGTH = 200
# 设定文件写满也只有一百多 KB；比这大得多的肯定不是程序写的，不读进来（每次打开推荐页都要读这个文件）
MAX_FILE_SIZE = 1024 * 1024
MODE_ALL = 'all'
MODE_ANY = 'any'
MODES = (MODE_ALL, MODE_ANY)
# 设定里的两类名字 → Settings 的字段、设定文件里的键
FIELDS = {'star': 'stars', 'tag': 'tags'}
# "从你喜欢的影片里选"最多列多少个女优、多少个标签；输入联想最多返回多少个
MAX_STAR_CHOICES = 20
MAX_TAG_CHOICES = 30
MAX_SUGGESTIONS = 20
# 输入联想里别名那一部分最多取回这么多个别名（几位共用的算一个），按影片数从多到少：留够 MAX_SUGGESTIONS 个的余量
MAX_ALIAS_SUGGESTIONS = 100
# 标签类型在界面上的叫法
TAG_TYPE_NAMES = {'genre': '类别', 'star': '演员', '導演:': '导演', '製作商:': '制作商', '發行商:': '发行商',
                  '系列:': '系列'}
# IN 列表一次最多放这么多个参数（旧版 SQLite 一条语句最多 999 个）
IN_BATCH = 500
# 一组标签在"影片-标签"表里占的行数超过全表的 1 / SCAN_RATIO 时，把 (item_id, tag_id) 的索引整个扫一遍
# 比按 tag_id 一个个标签去找再回表更快（3 万部影片的库上实测的分界大约在 5%）
SCAN_RATIO = 20
# "同时满足"里，标签不超过这么多个的一组对每部候选影片逐个探查；更多的（一般是带着别名的女优们）
# 探查次数太多，先把带这些标签的影片算成一个集合
PROBE_LIMIT = 4
# 改设定（读出来、改、写回去）在这把锁里：两个请求同时保存时后一个排队，不会互相覆盖。
# 只读设定的不拿这把锁：写的一方在目标文件被占用时要重试一两秒，各个页面读设定不该跟着等
_lock = threading.Lock()
# 读设定文件和"把它换成新的"那一下不能同时进行：Windows 上正在被替换的文件打不开（读的一方会把好好的设定
# 当成坏文件），正开着的文件也换不掉。读的那几毫秒、换的那一下各拿一次这把锁；写的一方等着重试的时候不拿，
# 所以读的一方最多等一次替换，读到的总是完整的旧文件或者新文件
_swap = threading.Lock()
# 上一次报告过的坏文件（路径、修改时间、大小）：同一份坏文件只写一次警告，不会每打开一次页面就写一行
_reported = None
_report_lock = threading.Lock()


@dataclass(frozen=True)
class Settings:
    '''
    推荐设定。stars、tags 按添加的先后排列。
    problem 不为空表示设定文件读不了或格式不对，内容是原因，这时其他各项是空设定
    '''
    stars: tuple = ()
    tags: tuple = ()
    mode: str = MODE_ALL
    model_only: bool = False
    problem: str = ''

    @property
    def is_empty(self):
        return not self.stars and not self.tags

    def names(self, kind):
        return getattr(self, FIELDS[kind])


# add_names 的结果：新加的、已经在列表里的、太长或带控制字符没加的、列表满了没加的，各有几个
AddResult = namedtuple('AddResult', 'added duplicate rejected overflow')


class SettingsError(Exception):
    '''推荐设定没能写进文件（磁盘满、没有写权限、文件一直被占用等）；消息是中文，可以直接显示给用户'''


def settings_path():
    return get_data_path(SETTINGS_FILE)


def _has_control_character(text):
    return any(ch < ' ' or ch == '\x7f' for ch in text)


def _unique(names):
    '''去掉首尾空白和空名字；同一个名字（按 fold 比较）只留第一次出现的写法，顺序不变'''
    seen = set()
    result = []
    for name in names:
        name = name.strip()
        key = fold(name)
        if key and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _names_from_file(data, key):
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ValueError(f'{key} 不是文字列表')
    names = _unique(value)
    if len(names) > MAX_NAMES:
        raise ValueError(f'{key} 超过 {MAX_NAMES} 个')
    if any(len(name) > MAX_NAME_LENGTH or _has_control_character(name) for name in names):
        raise ValueError(f'{key} 里有超过 {MAX_NAME_LENGTH} 个字符或带控制字符的名字')
    return tuple(names)


def _settings_from(data):
    '''
    设定文件的内容（已经解析成 JSON）换成 Settings。缺的键用默认值

    Raises:
        ValueError: 格式不对，消息是中文原因
    '''
    if not isinstance(data, dict):
        raise ValueError('最外层不是 JSON 对象')
    version = data.get('version')
    if type(version) is not int or version != SETTINGS_VERSION:
        raise ValueError(f'version 不是 {SETTINGS_VERSION}')
    stars = _names_from_file(data, 'stars')
    tags = _names_from_file(data, 'tags')
    mode = data.get('mode', MODE_ALL)
    if mode not in MODES:
        raise ValueError('mode 只能是 all 或 any')
    model_only = data.get('model_only', False)
    if not isinstance(model_only, bool):
        raise ValueError('model_only 只能是 true 或 false')
    return Settings(stars=stars, tags=tags, mode=mode, model_only=model_only)


def _file_signature(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _broken(path, reason):
    global _reported
    signature = (path, _file_signature(path))
    with _report_lock:
        first_time = signature != _reported
        _reported = signature
    if first_time:
        logger.warning(f'推荐设定文件 {path} 读不了或格式不对（{reason}），已按没有设定处理；'
                       '在网页的"指定推荐"页重新保存设定后会覆盖它')
    return Settings(problem=reason)


def _read():
    '''读设定文件。没有文件就是空设定；读不了或格式不对也按空设定处理，原因放在 problem 里并写一行警告'''
    path = settings_path()
    try:
        with _swap, open(path, 'rb') as f:
            raw = f.read(MAX_FILE_SIZE + 1)
    except FileNotFoundError:
        return Settings()
    except OSError as ex:
        return _broken(path, f'读取失败：{os_error_text(ex)}')
    if len(raw) > MAX_FILE_SIZE:
        return _broken(path, f'文件超过 {MAX_FILE_SIZE} 字节')
    try:
        # 记事本另存为 UTF-8 时会在开头加 BOM
        data = json.loads(raw.decode('utf-8-sig'))
    except UnicodeDecodeError:
        return _broken(path, '不是 UTF-8 编码')
    except (ValueError, RecursionError):
        return _broken(path, '不是有效的 JSON')
    try:
        return _settings_from(data)
    except ValueError as ex:
        return _broken(path, str(ex))


def _write(settings):
    '''
    先写同目录的临时文件再替换，别的请求读到的要么是旧文件要么是新文件

    Raises:
        SettingsError: 写不进去，原来的文件没有改动
    '''
    path = settings_path()
    content = {'version': SETTINGS_VERSION, 'stars': list(settings.stars), 'tags': list(settings.tags),
               'mode': settings.mode, 'model_only': settings.model_only}
    data = json.dumps(content, ensure_ascii=False, indent=2).encode('utf-8')
    try:
        save_file(path, data, swap=_swap)
    except OSError as ex:
        raise SettingsError(f'推荐设定没有保存：无法写入 {path}（{os_error_text(ex)}）') from ex


def load_settings():
    return _read()


def split_names(text):
    '''
    输入框里的文字拆成名字：换行、制表符（从表格里复制的一行几格）、英文逗号、中文逗号都是分隔符，
    去掉每个名字两端的空白和空名字
    '''
    pieces = (piece.strip() for piece in re.split(r'[\r\n\t,，]', text or ''))
    return [piece for piece in pieces if piece]


def add_names(kind, names):
    '''
    把名字加进指定女优（kind='star'）或指定标签（kind='tag'），返回 AddResult。
    已经在列表里的（按 fold 比较）不重复加；超过 MAX_NAME_LENGTH 个字符或带控制字符的不加；
    列表满 MAX_NAMES 个之后的不加。一个都没加上时不写文件

    Raises:
        ValueError: kind 不认识
        SettingsError: 写不进文件
    '''
    if kind not in FIELDS:
        raise ValueError(f'不认识的类别 {kind!r}')
    with _lock:
        settings = _read()
        current = list(settings.names(kind))
        listed = {fold(name) for name in current}
        added = duplicate = rejected = overflow = 0
        for name in _unique(names):
            if len(name) > MAX_NAME_LENGTH or _has_control_character(name):
                rejected += 1
            elif fold(name) in listed:
                duplicate += 1
            elif len(current) >= MAX_NAMES:
                overflow += 1
            else:
                current.append(name)
                added += 1
        if added:
            _write(replace(settings, **{FIELDS[kind]: tuple(current)}))
    return AddResult(added, duplicate, rejected, overflow)


def remove_name(kind, name):
    '''
    从指定女优或指定标签里去掉一个名字（按 fold 比较）。去掉了返回 True；本来就不在列表里返回 False、不写文件

    Raises:
        ValueError: kind 不认识
        SettingsError: 写不进文件
    '''
    if kind not in FIELDS:
        raise ValueError(f'不认识的类别 {kind!r}')
    key = fold(name or '')
    with _lock:
        settings = _read()
        kept = tuple(listed for listed in settings.names(kind) if fold(listed) != key)
        if len(kept) == len(settings.names(kind)):
            return False
        _write(replace(settings, **{FIELDS[kind]: kept}))
    return True


def save_options(mode, model_only):
    '''
    保存条件关系（MODE_ALL 同时满足 / MODE_ANY 满足任一）和"只看在「推荐」里的"，已选的名字不变

    Raises:
        ValueError: mode 不认识
        SettingsError: 写不进文件
    '''
    if mode not in MODES:
        raise ValueError(f'不认识的条件关系 {mode!r}')
    with _lock:
        _write(replace(_read(), mode=mode, model_only=bool(model_only)))


def clear_settings():
    '''
    回到空设定（没有名字、同时满足、不看模型）

    Raises:
        SettingsError: 写不进文件
    '''
    with _lock:
        _write(Settings())


# 设定里的名字在库里对应的标签。stars、tags 都是"名字 → 标签 id 的集合"（库里没有这个名字时是空集合）；
# people 是"女优名字 → 这个名字找到的人（actresses.Person 的列表）"：正式名字、别名、地址和算她的全部标签；
# movies 是"标签 id → 带这个标签的影片数"，一部都没有的标签不在里面
Matched = namedtuple('Matched', 'stars tags people movies')


def match_names(settings):
    '''
    找出设定里每个名字在库里对应的标签，返回 Matched。
    女优：按人找（actresses.find_people）。她的正式名字、任何一个别名（简繁互通、英文不分大小写）都是她，
    算她的全部标签都算；一个名字是几位女优共用的别名时几位都算。
    标签：任意类型里值相同的标签，不跟着别名走
    '''
    people = actresses.find_people(settings.stars)
    stars = {name: set().union(*(person.tags for person in people[name])) for name in settings.stars}
    keys = {name: fold(name) for name in settings.tags}
    named = actresses.tags_named(set(keys.values()), star_only=False)
    tags = {name: {tag_id for tag_id, _, _ in named.get(key, [])} for name, key in keys.items()}
    movies = {}
    for tag_ids in chunked(sorted(set().union(*stars.values(), *tags.values())), IN_BATCH):
        query = (ItemTag.select(ItemTag.tag, fn.COUNT(ItemTag.id))
                 .where(ItemTag.tag.in_(tag_ids))
                 .group_by(ItemTag.tag))
        movies.update(query.tuples())
    return Matched(stars, tags, people, movies)


def _tag_id_list(tag_ids):
    '''
    标签 id 直接写进 SQL（都是从库里读出来的整数）：已选的名字多、别名多时 id 会有上千个，
    当参数传会超过旧版 SQLite 一条语句 999 个参数的上限
    '''
    return SQL('(%s)' % ', '.join(str(int(tag_id)) for tag_id in sorted(tag_ids)))


def _weight(tag_ids, matched):
    '''这些标签在"影片-标签"表里一共有多少行'''
    return sum(matched.movies.get(tag_id, 0) for tag_id in tag_ids)


def _carrying(tag_ids, matched):
    '''带其中任意一个标签的影片的番号（子查询，和外层无关，只算一次）'''
    tag = ItemTag.tag
    # 表里最大的行号，当作总行数的近似值
    all_rows = ItemTag.select(fn.MAX(ItemTag.id)).scalar() or 0
    if _weight(tag_ids, matched) * SCAN_RATIO > all_rows:
        # 写成表达式后 SQLite 不走 tag_id 的索引，改为把 (item_id, tag_id) 的索引从头扫一遍
        tag = ItemTag.tag + 0
    return ItemTag.select(ItemTag.item).where(tag.in_(_tag_id_list(tag_ids))).distinct()


def _carries(tag_ids):
    '''外层的这部影片带其中任意一个标签（每部影片在 (item_id, tag_id) 的索引上逐个探查）'''
    return fn.EXISTS(ItemTag.select(SQL('1')).where((ItemTag.item == Item.fanhao)
                                                    & ItemTag.tag.in_(_tag_id_list(tag_ids))))


def _matching(settings, matched):
    '''
    "符合设定"的条件，没有影片可能符合时返回 None。
    同时满足：选了女优就要有其中至少一位，选了标签就要每个都有；满足任一：有任意一位女优或任意一个标签。
    女优和标签都没选时没有影片符合
    '''
    star_ids = set().union(*matched.stars.values())
    tag_groups = list(matched.tags.values())
    if settings.mode == MODE_ANY:
        wanted = star_ids.union(*tag_groups)
        return Item.fanhao.in_(_carrying(wanted, matched)) if wanted else None
    groups = tag_groups + ([star_ids] if settings.stars else [])
    if not groups or not all(groups):
        return None
    # 影片最少的一组先算出来，其余各组只在这些影片里核对
    groups.sort(key=lambda tag_ids: _weight(tag_ids, matched))
    conditions = [Item.fanhao.in_(_carrying(groups[0], matched))]
    for tag_ids in groups[1:]:
        if len(tag_ids) <= PROBE_LIMIT:
            conditions.append(_carries(tag_ids))
        else:
            conditions.append(Item.fanhao.in_(_carrying(tag_ids, matched)))
    return reduce(operator.and_, conditions)


def has_model_results():
    '''库里有没有模型的推荐结果（系统评分）'''
    return ItemRate.select().where(ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE).exists()


def _candidates(settings, matched):
    '''
    指定推荐的候选：符合设定、用户还没有亲手打标的影片（没有评分的，加上只有系统评分的）。
    "只看在「推荐」里的"在库里已经有系统评分时才生效，这时只留系统评分为喜欢的（也就是推荐页"推荐"里的那些）
    '''
    matching = _matching(settings, matched)
    if matching is None:
        return Item.id.is_null()
    by_model = ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE
    if settings.model_only and has_model_results():
        return matching & by_model & (ItemRate.rate_value == RATE_VALUE.LIKE)
    return matching & (ItemRate.rate_type.is_null() | by_model)


def _model_order():
    '''
    在"推荐"里的排最前，模型还没打分的其次，在"不太像"里的排最后；每一档里分数高的在前。
    "推荐"就是分数最高的那一部分，所以有分数的影片整体上是按分数从高到低排的
    '''
    recommended = (ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE) & (ItemRate.rate_value == RATE_VALUE.LIKE)
    return Case(None, [(recommended, 0), (ItemRate.rate_type.is_null(), 1)], 2), *db.by_score()


def count_items(settings, matched):
    '''符合设定的未打标影片数。matched 是 match_names(settings) 的结果'''
    return db.count_items(_candidates(settings, matched))


def get_items(settings, matched, page):
    '''
    符合设定的未打标影片的一页，返回值和 db.get_items 一样；分数相同（或都没有分数）时的先后、每页条数、
    页码超出时的处理也一样
    '''
    return db.list_items(_candidates(settings, matched), order_first=_model_order(), page=page, sort_once=True)


def _also_carrying(tag_ids, without):
    '''带 tag_ids 里任意一个标签、但不带 without 这个标签的影片数'''
    if not tag_ids:
        return 0
    other = ItemTag.alias()
    carries_it = fn.EXISTS(other.select(SQL('1')).where((other.item == ItemTag.item) & (other.tag == without)))
    rows = ItemTag.select(ItemTag.item).where(ItemTag.tag.in_(_tag_id_list(tag_ids)) & ~carries_it)
    return len({fanhao for (fanhao,) in rows.tuples()})


def _count_movies(groups, matched):
    '''groups 是"名字 → 标签 id 的集合"，返回"名字 → 带其中任意一个标签的影片数"'''
    counts = {}
    for name, tag_ids in groups.items():
        carried = sorted((tag_id for tag_id in tag_ids if tag_id in matched.movies),
                         key=matched.movies.get, reverse=True)
        # 一部影片可能同时带着其中几个标签（别名、同名的製作商和發行商），不能直接相加：
        # 影片最多的那个标签用已经数好的数，其余的只数没带它的影片
        counts[name] = matched.movies[carried[0]] + _also_carrying(carried[1:], carried[0]) if carried else 0
    return counts


def library_counts(matched):
    '''每个已选的名字，库里有多少部影片带它（不管打没打标）。返回 (女优名 → 部数, 标签名 → 部数)'''
    return _count_movies(matched.stars, matched), _count_movies(matched.tags, matched)


# "按演员补全作品"里的一行，一位女优：row 是这一行的键（她的 Person.key，库里没有这个名字的人时是 name:<折叠后的名字>），
# names 是设定里找到她的名字，movies 是库里算她的影片数，path 是她的 JavBus 演员页（不知道时为 None），
# key 是查找她的演员页用的键（avwiki:<女优页> 或 name:<折叠后的名字>，db.StarLink 的 key），search 是查找时用的名字，
# link 是查找的结果（db.StarLink，还没查过时为 None），source 是 path 对应的 db.ListSource（还没有点过时为 None），
# queued、done、failed、retryable 是她排在队里还没抓的、抓到了的、没抓到的、可以重试的部数；
# name 是她的正式名字（库里没有这个名字的人时是设定里的名字），avwiki 是她的 av-wiki 女优页（不是 av-wiki 的女优时为空：
# 只有这样的女优的演员页是关联上的、可以取消），page_name 是 path 上的演员名（不知道时为空），
# rejected 是她说过不是她的演员页
ActressRow = namedtuple('ActressRow', 'row names movies path key search link source queued done failed retryable name '
                                      'avwiki page_name rejected')


def _page_names(paths, links):
    '''
    这些演员页上的演员名：{演员页: 名字}。查找结果里记着的用它，没有时是库里这个地址最新的一条标签，一次查完；
    都没有的不在里面。links 是这些演员页各自那一行的查找记录（db.StarLink，没有时为 None）
    '''
    names = {}
    for path, link in zip(paths, links):
        for candidate in (link.candidate_list if link is not None else []):
            if candidate[0] == path:
                names[path] = candidate[1]
    rest = db.in_values(Tag.url, {path for path in paths if path not in names})
    if rest is not None:
        for _, url, value in sorted(Tag.select(Tag.id, Tag.url, Tag.value).where((Tag.type_ == 'star') & rest).tuples()):
            names[url] = value
    return names


def _people_movies(people, matched):
    '''几个人各自在库里的影片数：{序号: 部数}。带着她几条标签的影片只算一次，几个人一起用一条 SQL 数'''
    counts, several = {}, {}
    for number, person in enumerate(people):
        carried = [tag_id for tag_id in person.tags if tag_id in matched.movies]
        if len(carried) > 1:
            several[number] = carried
        else:
            counts[number] = matched.movies[carried[0]] if carried else 0
    counts.update(_distinct_movies(several, within=None))
    return counts


def backfill_rows(settings, matched, star_counts):
    '''
    指定推荐页上"按演员补全作品"的各行（ActressRow），一个人一行，按设定里名字的先后。一个名字找到几个人时
    每人一行；库里没有这个名字的人时这个名字自己一行。star_counts 是 library_counts 算好的"名字 → 库里的部数"
    '''
    groups = {}
    for name in settings.stars:
        found = matched.people.get(name) or []
        if not found:
            group = groups.setdefault(f'name:{fold(name)}', {'names': [], 'person': None})
            group['names'].append(name)
        for person in found:
            group = groups.setdefault(person.key, {'names': [], 'person': person})
            group['names'].append(name)
    search_keys = [group['person'].search_key if group['person'] else row for row, group in groups.items()]
    links = {}
    for chunk in chunked(search_keys, IN_BATCH):
        links.update((link.key, link) for link in db.StarLink.select().where(db.StarLink.key.in_(chunk)))
    paths = [group['person'].javbus for group in groups.values() if group['person'] and group['person'].javbus]
    sources = {}
    for chunk in chunked(paths, IN_BATCH):
        sources.update((source.path, source) for source in db.ListSource.select().where(db.ListSource.path.in_(chunk)))
    stats = defaultdict(lambda: defaultdict(int))
    ids = [source.id for source in sources.values()]
    for chunk in chunked(ids, IN_BATCH):
        query = (db.FetchTask.select(db.FetchTask.source, db.FetchTask.status, fn.COUNT(db.FetchTask.id))
                 .where(db.FetchTask.source.in_(chunk))
                 .group_by(db.FetchTask.source, db.FetchTask.status))
        for source_id, status, count in query.tuples():
            stats[source_id][status] = count
    avwiki_people = [group['person'] for group in groups.values() if group['person'] and group['person'].avwiki]
    rejected = actresses.rejected_by_page([person.avwiki for person in avwiki_people])
    linked = [person for person in avwiki_people if person.javbus]
    page_names = _page_names([person.javbus for person in linked], [links.get(person.search_key) for person in linked])
    people = [group['person'] for group in groups.values() if group['person']]
    counted = _people_movies(people, matched)
    movie_counts = {person.key: counted.get(number, 0) for number, person in enumerate(people)}
    rows = []
    for row, group in groups.items():
        names, person = group['names'], group['person']
        if person is None:
            movies = star_counts.get(names[0], 0)
        else:
            movies = movie_counts[person.key]
        path = person.javbus if person and person.javbus else None
        source = sources.get(path) if path else None
        counts = stats[source.id] if source is not None else {}
        key = person.search_key if person else row
        avwiki = person.avwiki if person else ''
        rows.append(ActressRow(
            row=row, names=names, movies=movies, path=path, key=key,
            search=person.search_name if person else names[0], link=links.get(key), source=source,
            queued=counts.get(db.TASK_QUEUED, 0), done=counts.get(db.TASK_DONE, 0),
            failed=counts.get(db.TASK_FAILED, 0) + counts.get(db.TASK_MISSING, 0),
            retryable=counts.get(db.TASK_FAILED, 0), name=person.name if person else names[0], avwiki=avwiki,
            page_name=page_names.get(path, '') if path and avwiki else '',
            rejected=frozenset(rejected.get(avwiki, ()))))
    return rows


def _liked():
    return ItemRate.select(ItemRate.item).where((ItemRate.rate_type == RATE_TYPE.USER_RATE)
                                                & (ItemRate.rate_value == RATE_VALUE.LIKE))


def _distinct_movies(groups, within):
    '''
    几组标签（一个标签只在一组里），每组带其中任意一个标签的影片数（一部影片带着一组里的几个标签只算一次）：
    {组的序号: 部数}，一部都没有的组不在里面。within 不是 None 时只数这些影片（子查询）。
    标签 id 写进 SQL（都是从库里读出来的整数），组数多时也不会超过参数个数的上限
    '''
    if not groups:
        return {}
    group = Case(ItemTag.tag, [(SQL(str(int(tag_id))), SQL(str(int(number))))
                               for number, tag_ids in groups.items() for tag_id in tag_ids])
    tag_ids = [tag_id for tag_ids in groups.values() for tag_id in tag_ids]
    condition = ItemTag.tag.in_(_tag_id_list(tag_ids))
    if within is not None:
        condition &= ItemTag.item.in_(within)
    query = ItemTag.select(group, fn.COUNT(fn.DISTINCT(ItemTag.item))).where(condition).group_by(group)
    return dict(query.tuples())


def liked_choices(settings, matched):
    '''
    用户打成喜欢的影片里出现最多的女优（最多 MAX_STAR_CHOICES 个）和其他标签（最多 MAX_TAG_CHOICES 个），
    已经选了的不列（女优：选了她的任何一个名字都算选了她）。一格是一个人（显示她的正式名字）、一个标签；
    同一个名字的几种写法（简体和繁体、不同类型里的同一个值）算一格，显示最先入库的那个。
    返回 (女优, 标签)，都是 (名字, 喜欢的影片里有几部带它) 的列表
    '''
    chosen_people = {person.key for found in matched.people.values() for person in found}
    chosen_stars = {fold(name) for name in settings.stars}
    chosen_tags = {fold(name) for name in settings.tags}
    # 先按标签数出带它的喜欢的影片（整个结果一次取回，见 db.fetch_rows），合并成人、合并写法在 Python 里做：
    # 在 SQL 里按折过的写法分组要每一行调回 Python 一次，几个请求同时进行时会互相等解释器锁。
    # 不按标签类型过滤：一过滤 SQLite 就改成从标签表查起，每个标签都要到喜欢的影片里探一遍，慢上百倍
    query = (ItemTag.select(ItemTag.tag, (Tag.type_ == 'star').alias('star'), Tag.value,
                            fn.COUNT(fn.DISTINCT(ItemTag.item)).alias('movies'))
             .join(Tag)
             .where(ItemTag.item.in_(_liked()))
             .group_by(ItemTag.tag))
    rows = db.fetch_rows(query, 'tag_id', 'star', 'value', 'movies')
    people = actresses.people_of_tags([tag_id for tag_id, star, _, _ in rows if star])
    groups = {}
    for tag_id, star, value, count in sorted(rows):
        name = people[tag_id].name if star else value
        group = groups.setdefault((bool(star), fold(name)), {'name': name, 'first': tag_id, 'tags': {}, 'people': set()})
        group['tags'][tag_id] = count
        if star:
            group['people'].add(people[tag_id].key)
    merged = {number: list(group['tags']) for number, group in enumerate(groups.values()) if len(group['tags']) > 1}
    counts = _distinct_movies(merged, within=_liked())
    ranked = []
    for number, ((star, key), group) in enumerate(groups.items()):
        count = counts[number] if number in merged else next(iter(group['tags'].values()))
        ranked.append((-count, group['first'], star, key, group))
    stars, tags = [], []
    for negative, _, star, key, group in sorted(ranked, key=lambda entry: entry[:2]):
        if star:
            if len(stars) < MAX_STAR_CHOICES and key not in chosen_stars and not group['people'] & chosen_people:
                stars.append((group['name'], -negative))
        elif len(tags) < MAX_TAG_CHOICES and key not in chosen_tags:
            tags.append((group['name'], -negative))
    return stars, tags


def _alias_keys(text):
    '''
    别名里含 text（折过的）、主人在库里有影片的：按别名合并（几位女优共用的别名，她们的影片数加在一起），按影片数
    从多到少、一样多时按最先入库的那位主人和她这条别名的先后，取前 MAX_ALIAS_SUGGESTIONS 个折过的别名。
    在 SQLite 里数好再取：一两个字母能对上几千条别名，不把它们全取回来
    '''
    # 先按每条别名数好主人的影片（按 id 分组不用排序），再按折过的别名合并：直接按别名分组要把几万行连接的结果排一遍
    each = (ActressAlias.select(ActressAlias.key, ActressAlias.actress, ActressAlias.id,
                                fn.COUNT(ItemTag.id).alias('movies'))
            .join(db.Actress).join(ItemTag, on=(ItemTag.tag == db.Actress.tag))
            .where(fn.instr(ActressAlias.key, text) > 0)
            .group_by(ActressAlias.id)).alias('each')
    movies = fn.SUM(each.c.movies)
    # 只有一个 MIN 聚合时，SQLite 取别名 id 的那一行就是 MIN 取到的那一行：最先入库的主人的这条别名
    first = fn.MIN(each.c.actress_id)
    query = (ActressAlias.select(each.c.key, movies, first, each.c.id).from_(each)
             .group_by(each.c.key)
             .order_by(movies.desc(), first, each.c.id)
             .limit(MAX_ALIAS_SUGGESTIONS))
    return [key for key, _, _, _ in query.tuples()]


def _alias_suggestions(keys):
    '''
    这些折过的别名各是谁的（主人在库里有影片的才算）：{折过的别名: (别名, [(正式名字, 影片数)…])}，
    先入库的主人在前，别名的写法用最先入库的那位的；一个别名的主人全列出来
    '''
    condition = db.in_values(ActressAlias.key, keys)
    if condition is None:
        return {}
    movies = fn.COUNT(ItemTag.id)
    query = (ActressAlias.select(ActressAlias.name, ActressAlias.key, ActressAlias.actress, ActressAlias.id, Tag.value,
                                 movies)
             .join(db.Actress).join(Tag, on=(Tag.id == db.Actress.tag))
             .join(ItemTag, on=(ItemTag.tag == db.Actress.tag))
             .where(condition)
             .group_by(ActressAlias.id))
    found = {}
    for alias, key, _, _, name, count in sorted(query.tuples(), key=lambda row: (row[2], row[3])):
        # 正式名字是 JavBus 演员页上的写法时，别名里留着她自己那一侧的同一个名字：不是别人的名字，不算；
        # 给她编的号（见 actresses.her_number）留着只为认人，也不算
        if fold(name) == key or actresses.her_number(alias, name):
            continue
        entry = found.setdefault(key, (alias, []))
        entry[1].append((name, count))
    return found


def suggest(kind, text):
    '''
    输入框的联想：值里含 text 的标签（按 fold 的写法比：英文不分大小写，简体繁体互通），带它的影片多的在前，
    最多 MAX_SUGGESTIONS 个；text 为空时就是影片最多的那些。kind='star' 只找演员，再加上别名里含 text 的女优；
    kind='tag' 找所有类型，不看别名。
    每项是 {'value': 标签值, 'type': 类型的中文叫法, 'count': 影片数}；别名多一个 'alias_of'（是哪几位的别名，
    她们的正式名字）；一个演员名同时又是别的女优的别名时多一个 'also_alias_of'

    Raises:
        ValueError: kind 不认识
    '''
    if kind not in FIELDS:
        raise ValueError(f'不认识的类别 {kind!r}')
    text = fold(text or '')
    if len(text) > MAX_NAME_LENGTH:
        return []
    movies = fn.COUNT(ItemTag.id)
    query = (Tag.select(Tag.value, Tag.type_, movies)
             .join(ItemTag)
             .group_by(Tag.id)
             .order_by(movies.desc(), Tag.id)
             .limit(MAX_SUGGESTIONS))
    if kind == 'star':
        query = query.where(Tag.type_ == 'star')
    if text:
        # 先找出值里含这段文字的标签，再数带它们的影片
        matching = Tag.alias()
        query = query.where(Tag.id.in_(matching.select(matching.id)
                                       .where(db.glob(matching.value, db.contains_pattern(text)))))
    rows = [{'value': value, 'type': TAG_TYPE_NAMES.get(type_, type_.rstrip(':：')), 'count': count}
            for value, type_, count in query.tuples()]
    if kind != 'star' or not text:
        return rows
    # 列出的演员名同时是别人的别名时，那条别名可能排在取回的个数以外：和取回的一起查它的主人
    aliases = _alias_suggestions(set(_alias_keys(text)) | {fold(row['value']) for row in rows})
    for row in rows:
        same = aliases.pop(fold(row['value']), None)
        if same:
            row['also_alias_of'] = [name for name, _ in same[1]]
            row['count'] += sum(count for _, count in same[1])
    rows += [{'value': alias, 'type': TAG_TYPE_NAMES['star'], 'count': sum(count for _, count in owners),
              'alias_of': [name for name, _ in owners]} for alias, owners in aliases.values()]
    # 排序是稳定的：一样多时标签在前、别名在后，各自保持原来的先后
    return sorted(rows, key=lambda row: -row['count'])[:MAX_SUGGESTIONS]
