'''
女优：一个人一条正式名字（star 标签，影片只挂这一条），其他名字（读音、罗马字、别名）记在别名表里，另记她的
av-wiki 女优页和 JavBus 演员页（db.Actress、db.ActressAlias）。这里是按名字找人、一条 star 标签算谁。
还没迁移的库（av-wiki 补的每个名字各是一条 star 标签，地址都是女优页）也认得：同一个女优页的几条算一个人
'''
import contextlib
import json
import operator
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from time import monotonic
from typing import Optional
from urllib.parse import quote, unquote

from peewee import OperationalError, chunked, fn

from bustag.spider import db
from bustag.spider.db import (AVWIKI_PAGE, IN_BATCH, RATE_TYPE, Actress, ActressAlias, ActressLookup, Item,
                              ItemRate, ItemTag, JobState, Tag, fold)
from bustag.spider.parser import is_star_page
from bustag.util import get_data_dir, get_data_path, get_now_time, hide_actress, logger, os_error_text, remove_file

# job_state 里的三行：旧的演员数据整理成新存法的时间（迁移做完的记号）、后台按女优页把迁移的结果校正完的时间、
# 迁移以后又整理旧版本按旧存法写下的名字（再整理）最近一次做成的时间
STORAGE_MARK = 'actress-storage'
FIXED_MARK = 'actress-fixed'
RETIDY_MARK = 'actress-retidied'
# 迁移前的快照，放在数据目录里，文件名带日期
SNAPSHOT_NAME = 'bus-before-actress-upgrade-{}.db'
# 同一天又整理时快照的名字带时分秒；写的时候先是 .tmp，进程被结束时 SQLite 还会留下 -journal
_TIMED_SNAPSHOT = re.compile(re.escape(SNAPSHOT_NAME).replace(r'\{\}', r'(\d{8}-\d{6})') + r'(?:\.tmp)?(?:-journal)?')
# 两位女优的第一个名字相同（不同的女优页）时，后一位的正式名字写成"名字（2）"
SAME_NAME = '{}（{}）'
_NUMBERED = re.compile(r'(.+)（([1-9][0-9]*)）')
# star_link 里记"这些 JavBus 演员页不是她"的那一行：键是这个前缀加上她的女优页，candidates 是 [[演员页, ''], …]
NOT_HER = 'not:avwiki:'
# 女优页地址里照原样写的字符（路径里不用转义的），别的（日文、空格等）按 UTF-8 转义
_PAGE_SAFE = "/:@!$&'()*+,;=-._~"
_ESCAPE = re.compile(r'%[0-9A-F]{2}')


def page_path(page):
    '''
    女优页的地址存进库里的写法：没转义的字符按 UTF-8 转义，转义过的原样留着。网站上的链接本来就是转义的，
    十六进制的大小写也照它的，存下的和网站上的一字不差（旧版本存的也是这样）
    '''
    return quote(page, safe=_PAGE_SAFE + '%')


def _page_forms(page):
    '''同一个女优页的地址可能的几种写法：原样、解码的、转义成十六进制大写的、小写的'''
    plain = unquote(page)
    escaped = quote(plain, safe=_PAGE_SAFE)
    return {page, plain, escaped, _ESCAPE.sub(lambda match: match.group(0).lower(), escaped)}


def numbered_base(value):
    '''value 是"名字（N）"（N 不小于 2）这种为了和同名的人分开而编的写法时，返回括号前的名字，否则返回 None'''
    match = _NUMBERED.fullmatch(value or '')
    if match is None or int(match.group(2)) < 2:
        return None
    return match.group(1)


def _search_form(name, aliases):
    '''查找 JavBus 演员页时用的名字：编了号的"名字（2）"用括号前的名字（得是她的一个名字），网站上没有带编号的写法'''
    base = numbered_base(name)
    if base is not None and fold(base) in {fold(alias) for alias in aliases}:
        return base
    return name


@dataclass(frozen=True)
class Person:
    '''
    一个人。key 是 actress:<女优的 id>、avwiki:<女优页>（还没迁移的库里的 av-wiki 女优）、javbus:<演员页>（JavBus 的演员）
    或 tag:<标签 id>（地址两样都不是的 star 标签）；name 是正式名字，aliases 是其他名字；tags 是算她的 star 标签的 id；
    javbus、avwiki 是两个地址（没有时为空）；actress 是 db.Actress 的 id（没有那一行时为 None）
    '''
    key: str
    name: str
    aliases: tuple
    tags: frozenset
    javbus: str
    avwiki: str
    actress: Optional[int]

    @property
    def search_key(self):
        '''查找她的 JavBus 演员页时代表她的键（db.StarLink 的 key）：她的女优页，没有时是名字本身'''
        return f'avwiki:{self.avwiki}' if self.avwiki else f'name:{fold(self.name)}'

    @property
    def search_name(self):
        '''查找她的 JavBus 演员页时用的名字（见 _search_form）'''
        return _search_form(self.name, self.aliases)


_in = db.in_values
# Tag.select(Tag.id, Tag.value, Tag.url) 结果里各列的名字
TAG_COLUMNS = ('id', 'value', 'url')


def _select(query, columns, *conditions):
    '''按几个条件各查一次（条件为 None 的跳过），合在一起。columns 是结果里各列的名字（见 db.fetch_rows）'''
    rows = []
    for condition in conditions:
        if condition is not None:
            rows.extend(db.fetch_rows(query.where(condition), *columns))
    return rows


def tags_named(folded_names, star_only=True):
    '''
    值折过（db.fold）以后等于其中某个名字的标签（star_only 时只要 star）：{折过的名字: [(id, value, url)]}。
    库里的值不交给 Python 去折：按 db.spellings 列出各种写法用 IN 比（写法太多时用 GLOB），只有两头带空白的值
    单独查出来在 Python 里核对，这样几个请求同时进行时不会互相等解释器锁
    '''
    wanted = {name for name in folded_names if name}
    by_spelling, too_many = {}, []
    for name in wanted:
        spelled = db.spellings(name)
        if spelled is None:
            too_many.append(name)
        else:
            by_spelling.update((spelling, name) for spelling in spelled)
    kind = (Tag.type_ == 'star') if star_only else None
    query = Tag.select(Tag.id, Tag.value, Tag.url)
    found = defaultdict(dict)

    def where(condition):
        return condition if kind is None else kind & condition

    for chunk in chunked(list(by_spelling), IN_BATCH):
        for tag_id, value, url in query.where(where(Tag.value.in_(chunk))).tuples():
            found[by_spelling[value]][tag_id] = (tag_id, value, url)
    loose = [(tag_id, value, url) for chunk in chunked(too_many, 50)
             for tag_id, value, url in query.where(where(reduce(operator.or_, (
                 db.glob(Tag.value, db.equals_pattern(name)) for name in chunk)))).tuples()]
    loose.extend(query.where(where(db.padded(Tag.value))).tuples())
    for tag_id, value, url in loose:
        name = fold(value)
        if name in wanted:
            found[name][tag_id] = (tag_id, value, url)
    return {name: sorted(rows.values()) for name, rows in found.items()}


def _aliases_named(folded_names):
    '''别名折过以后等于其中某个名字的女优：{折过的名字: [女优的 id]}'''
    found = defaultdict(list)
    condition = _in(ActressAlias.key, {name for name in folded_names if name})
    if condition is not None:
        query = (ActressAlias.select(ActressAlias.key, ActressAlias.actress)
                 .where(condition).order_by(ActressAlias.actress))
        for key, actress_id in query.tuples():
            if actress_id not in found[key]:
                found[key].append(actress_id)
    return found


class _People:
    '''
    一批人：先登记要找的 star 标签、女优、JavBus 演员页，再一次查出他们各自的全部标签、别名和地址（resolve）
    '''

    def __init__(self):
        self.tag_rows = {}       # 标签 id -> (value, url)
        self.actress_ids = set()
        self.pages = set()       # 名字查找到的 JavBus 演员页

    def add_tags(self, rows):
        for tag_id, value, url in rows:
            self.tag_rows[tag_id] = (value, url)

    def resolve(self, aliases=True):
        '''aliases=False 时不查别名（只要知道每条标签算谁、她的正式名字时）'''
        rows = self.tag_rows
        av_urls = {url for _, url in rows.values() if url.startswith(AVWIKI_PAGE)}
        star_urls = {url for _, url in rows.values() if is_star_page(url)} | self.pages
        query = Actress.select(Actress.id, Actress.tag, Actress.avwiki, Actress.javbus)
        actresses = {row[0]: row for row in _select(
            query, ('id', 'tag_id', 'avwiki', 'javbus'), _in(Actress.id, self.actress_ids), _in(Actress.tag, rows),
            _in(Actress.avwiki, av_urls), _in(Actress.javbus, star_urls))}
        missing = [tag_id for _, tag_id, _, _ in actresses.values() if tag_id not in rows]
        self.add_tags(_select(Tag.select(Tag.id, Tag.value, Tag.url), TAG_COLUMNS, _in(Tag.id, missing)))
        # 还不知道 JavBus 演员页的女优、还没迁移的女优页，用"查找她的 JavBus 页面"找到的那个
        legacy = av_urls - {row[2] for row in actresses.values()}
        keys = [f'avwiki:{avwiki}' for _, _, avwiki, javbus in actresses.values() if avwiki and not javbus]
        found = db.found_pages(keys + [f'avwiki:{url}' for url in legacy])
        self.by_canonical, self.by_avwiki, self.javbus_of = {}, {}, {}
        # JavBus 演员页 → 算谁：知道这个演员页的女优（几位都知道时取先入库的），其次是给她查找到这个演员页的、
        # 还没迁移的女优页
        self.claims = {}
        for actress_id, tag_id, avwiki, javbus in sorted(actresses.values()):
            canonical_url = rows[tag_id][1]
            page = javbus or (canonical_url if is_star_page(canonical_url) else found.get(f'avwiki:{avwiki}', ''))
            self.by_canonical[tag_id] = actress_id
            if avwiki:
                self.by_avwiki[avwiki] = actress_id
            if page:
                self.claims.setdefault(page, f'actress:{actress_id}')
            self.javbus_of[actress_id] = page
        self.legacy_pages = {url: found[f'avwiki:{url}'] for url in sorted(legacy) if f'avwiki:{url}' in found}
        for url, page in self.legacy_pages.items():
            self.claims.setdefault(page, f'avwiki:{url}')
        # 这些人的全部 star 标签：她的女优页、JavBus 演员页上的那些也算她
        urls = set(self.by_avwiki) | set(self.claims) | av_urls | star_urls
        same_urls = _in(Tag.url, urls)
        if same_urls is not None:
            self.add_tags(_select(Tag.select(Tag.id, Tag.value, Tag.url).where(Tag.type_ == 'star'), TAG_COLUMNS,
                                  same_urls))
        self.actresses = actresses
        self.aliases = defaultdict(list)
        query = ActressAlias.select(ActressAlias.id, ActressAlias.actress, ActressAlias.name)
        for _, actress_id, name in sorted(_select(query, ('id', 'actress_id', 'name'),
                                                  _in(ActressAlias.actress, actresses) if aliases else None)):
            self.aliases[actress_id].append(name)
        self.members = defaultdict(set)
        for tag_id in rows:
            self.members[self.key_of(tag_id)].add(tag_id)
        return self

    def key_of(self, tag_id):
        '''这条 star 标签算谁（Person.key）'''
        url = self.tag_rows[tag_id][1]
        if tag_id in self.by_canonical:
            return f'actress:{self.by_canonical[tag_id]}'
        if url in self.by_avwiki:
            return f'actress:{self.by_avwiki[url]}'
        if url in self.claims:
            return self.claims[url]
        if url.startswith(AVWIKI_PAGE):
            return f'avwiki:{url}'
        if is_star_page(url):
            return f'javbus:{url}'
        return f'tag:{tag_id}'

    def page_key(self, page):
        '''名字查找到的 JavBus 演员页算谁'''
        return self.claims.get(page, f'javbus:{page}')

    def person(self, key, name=''):
        tags = self.members.get(key, set())
        values = [self.tag_rows[tag_id][0] for tag_id in sorted(tags)]
        kind, _, rest = key.partition(':')
        javbus = avwiki = ''
        actress = None
        if kind == 'actress':
            actress = int(rest)
            _, tag_id, avwiki, _ = self.actresses[actress]
            avwiki = avwiki or ''
            javbus = self.javbus_of[actress]
            name = self.tag_rows[tag_id][0]
            others = self.aliases[actress] + values
        elif kind == 'avwiki':
            avwiki = rest
            javbus = self.legacy_pages.get(rest, '')
            own = [self.tag_rows[tag_id][0] for tag_id in sorted(tags) if self.tag_rows[tag_id][1] == rest]
            name, others = own[0], own[1:] + values
        elif kind == 'javbus':
            javbus = rest
            if values:
                name, others = values[-1], values[:-1]
            else:
                others = []
        else:
            name, others = values[0], []
        aliases, seen = [], {fold(name)}
        for other in others:
            if fold(other) not in seen and not her_number(other, name):
                seen.add(fold(other))
                aliases.append(other)
        return Person(key, name, tuple(aliases), frozenset(tags), javbus, avwiki, actress)


def _order(person):
    return (min(person.tags) if person.tags else float('inf'), person.key)


def find_people(names):
    '''
    名字 → 这个名字找到的人（Person 的列表，先入库的人在前）。名字按 db.fold 比（简繁互通、英文不分大小写、
    首尾空白不算）：正式名字、别名、还没迁移的库里任何一条 star 标签的名字都算；"查找她的 JavBus 页面"按这个名字
    找到的演员页上的人也算。一个名字可以找到几个人（几位女优共用的别名），都在列表里；谁都没找到时是空列表
    '''
    keys = {name: fold(name) for name in names}
    folded = set(keys.values())
    named = tags_named(folded)
    aliased = _aliases_named(folded)
    by_name = db.found_pages([f'name:{key}' for key in folded if key])
    people = _People()
    for rows in named.values():
        people.add_tags(rows)
    people.actress_ids = {actress_id for ids in aliased.values() for actress_id in ids}
    people.pages = set(by_name.values())
    people.resolve()
    result = {}
    for name, key in keys.items():
        wanted = [people.key_of(tag_id) for tag_id, _, _ in named.get(key, [])]
        wanted += [f'actress:{actress_id}' for actress_id in aliased.get(key, [])]
        page = by_name.get(f'name:{key}')
        if page:
            wanted.append(people.page_key(page))
        found = [people.person(person_key, name) for person_key in dict.fromkeys(wanted)]
        result[name] = sorted(found, key=_order)
    return result


def people_of_tags(tag_ids):
    '''这些标签里的 star 标签各算谁：{标签 id: Person}。不查别名表：Person.aliases 里只有算她的其他标签的值'''
    people = _People()
    people.add_tags(_select(Tag.select(Tag.id, Tag.value, Tag.url).where(Tag.type_ == 'star'), TAG_COLUMNS,
                            _in(Tag.id, tag_ids)))
    wanted = list(people.tag_rows)
    people.resolve(aliases=False)
    made = {}
    result = {}
    for tag_id in wanted:
        key = people.key_of(tag_id)
        if key not in made:
            made[key] = people.person(key)
        result[tag_id] = made[key]
    return result


def _fill_movies():
    '''演员是补名写的影片（补名记录是 filled 或 stale）：只有这些影片上的演员可以换'''
    return ActressLookup.select(ActressLookup.item).where(ActressLookup.status.in_([db.LOOKUP_FILLED, db.LOOKUP_STALE]))


def _claimed(tag_id, actress_id=None):
    '''这条标签已经是别的女优的正式名字'''
    return Actress.select().where((Actress.tag == tag_id) & (Actress.id != (actress_id or 0))).exists()


def _javbus_given(tag):
    '''JavBus 带来的演员标签：地址不是 av-wiki 女优页，或者挂在演员不是补名写的影片上（原文相同，共用了补名建的那一行）'''
    if not tag.url.startswith(AVWIKI_PAGE):
        return True
    return ItemTag.select().where((ItemTag.tag == tag.id) & ItemTag.item.not_in(_fill_movies())).exists()


def _number_taken(value):
    '''
    "名字（N）"已经有人用着：折过以后和哪条 star 标签的值、或者哪位女优的别名相同。设定里存的名字按折过的写法认人
    （见 find_people），和别人的号只差简繁、大小写也会认成两个人
    '''
    key = fold(value)
    return bool(tags_named({key})) or ActressAlias.select().where(ActressAlias.key == key).exists()


def _numbered(name, page):
    '''两位女优的第一个名字相同：新建"名字（2）"（这个号有人用着就 3、4……，见 _number_taken）当后一位的正式名字'''
    number = 2
    while _number_taken(SAME_NAME.format(name, number)):
        number += 1
    return Tag.create(type_='star', value=SAME_NAME.format(name, number), url=page or '')


def _own_number(name, actress_id, page):
    '''
    给她编过的"name（N）"（括号前的名字按 fold 比）：她现在的正式名字就是它时返回那条标签；她改用 JavBus 演员页上的名字
    期间号留在别名里（见 _switch），换回来时用回它（那条标签换名时删了就照原样新建，已经是别人的不用）。
    再读女优页、校正、取消关联都不另编号，否则设定里存着的名字、模型都要跟着换
    '''
    if actress_id is None:
        return None
    key = fold(name)
    tag = Tag.select().join(Actress, on=(Actress.tag == Tag.id)).where(Actress.id == actress_id).first()
    if tag is not None and fold(numbered_base(tag.value) or '') == key:
        return tag
    kept = (ActressAlias.select(ActressAlias.name)
            .where(ActressAlias.actress == actress_id).order_by(ActressAlias.id.desc()).tuples())
    for alias in [alias for (alias,) in kept if fold(numbered_base(alias) or '') == key]:
        tag = Tag.get_or_none((Tag.type_ == 'star') & (Tag.value == alias))
        if tag is None:
            return Tag.create(type_='star', value=alias, url=page or '')
        if tag.url == (page or '') and not _claimed(tag.id, actress_id):
            return tag
    return None


def her_number(value, name):
    '''
    value 是给正式名字是 name 的这位编的号：括号前的名字和她的正式名字（或正式名字括号前的部分）折过以后相同的
    "名字（N）"。这样的别名只用来认人（设定里存着它），不当她的名字列出来
    '''
    base = numbered_base(value)
    return base is not None and fold(base) in {fold(name), fold(numbered_base(name) or '')}


def _made_up(tag, actress):
    '''
    这条标签是为了和同名的人分开给她编的"名字（N）"（地址是她的女优页）：不是网站上的名字，换下来时不当别名
    （她改用 JavBus 演员页上的名字期间除外，见 _switch）
    '''
    return numbered_base(tag.value) is not None and tag.url == (actress.avwiki or '')


def rejected_pages(page):
    '''用户说过不是这位女优（女优页 page）的 JavBus 演员页'''
    return rejected_by_page([page]).get(page, set()) if page else set()


def rejected_by_page(pages):
    '''几位女优（女优页）各自说过不是她的 JavBus 演员页，一次查完：{女优页: 演员页的集合}，没有的不在里面'''
    keys = _in(db.StarLink.key, {NOT_HER + page for page in pages if page})
    if keys is None:
        return {}
    return {row.key[len(NOT_HER):]: {path for path, _ in row.candidate_list} for row in db.StarLink.select().where(keys)}


def _set_rejected(page, paths):
    key = NOT_HER + page
    if not paths:
        db.StarLink.delete().where(db.StarLink.key == key).execute()
        return
    value = json.dumps([[path, ''] for path in sorted(paths)])
    if not db.StarLink.update(candidates=value).where(db.StarLink.key == key).execute():
        db.StarLink.create(key=key, name='', state=db.LINK_REJECTED, candidates=value)


def _choose(names, page, javbus, actress_id=None):
    '''
    按女优页上的名字顺序（只知道卡片上的名字时就是那一个）定正式名字，返回 (标签, JavBus 演员页)：
    1. 她的 JavBus 演员页已知、库里有这个地址的标签、名字（按 fold 比）是她的一个名字 → 用它（几条时取排得最靠前的）；
       名字都不是她的（用户点选的演员页上是别的写法）时不按这一条改名字；
    2. 她的名字里有和 JavBus 带来的演员标签原文相同的 → 排得最靠前的那条（另一条仍是 JavBus 自己的标签）；
    3. 她的第一个名字：库里没有就新建（地址是她的女优页）；已经是别人的（别的女优的正式名字，或者不是她的 JavBus
       演员页的标签）就用"名字（2）"，她现在的正式名字已经是这个名字编了号的就还用它。
    已经是别的女优正式名字的标签都不用（两个女优页是两个人，不并）；用户说过不是她的 JavBus 演员页、她的演员页已知时
    别的 JavBus 演员页，上面的标签也都不用（演员页不同就是不同的人）
    '''
    rejected = rejected_pages(page)

    def elsewhere(tag):
        return tag.url in rejected or bool(javbus) and is_star_page(tag.url) and tag.url != javbus

    if javbus:
        folded = [fold(name) for name in names]
        named = [tag for tag in Tag.select().where((Tag.type_ == 'star') & (Tag.url == javbus)).order_by(Tag.id)
                 if fold(tag.value) in folded and not _claimed(tag.id, actress_id)]
        if named:
            return min(named, key=lambda tag: folded.index(fold(tag.value))), javbus
    for name in names:
        tag = Tag.get_or_none((Tag.type_ == 'star') & (Tag.value == name))
        if tag is not None and not _claimed(tag.id, actress_id) and not elsewhere(tag) and _javbus_given(tag):
            return tag, javbus or (tag.url if is_star_page(tag.url) else '')
    tag = Tag.get_or_none((Tag.type_ == 'star') & (Tag.value == names[0]))
    if tag is None:
        tag = Tag.create(type_='star', value=names[0], url=page or '')
    elif _claimed(tag.id, actress_id) or elsewhere(tag):
        tag = _own_number(names[0], actress_id, page) or _numbered(names[0], page)
    return tag, javbus


def _aliases_of(actress):
    return [name for (name,) in ActressAlias.select(ActressAlias.name)
            .where(ActressAlias.actress == actress.id).order_by(ActressAlias.id).tuples()]


def _put_aliases(actress, names):
    '''
    她的别名换成 names（按先后，见 db.add_aliases）。正式名字是 JavBus 演员页上的标签时，她自己这一侧和它对应的那个名字
    （写法一样，或者只差简繁、大小写）也记在它原来的位置上：取消这个演员页的关联时按它换回她自己的名字，
    不然就落到排在后面的读音上了
    '''
    beside = is_star_page(Tag.select(Tag.url).where(Tag.id == actress.tag_id).scalar() or '')
    ActressAlias.delete().where(ActressAlias.actress == actress.id).execute()
    db.add_aliases(actress, names, spelling=beside)


def _switch(actress, tag):
    '''
    正式名字换成 tag：她挂在补名写的影片上的旧名字改挂新的，旧名字记成别名、排在别名的最前面（取消 JavBus 演员页的
    关联时按这个先后换回它；给她编的"名字（N）"不记，换成 JavBus 演员页上的标签时除外：记在最后，只为认人；旧名字是
    JavBus 演员页上的标签时排在最后，和它只差简繁、大小写的她自己的写法在前），旧的那一行没人挂了、又是补名建的就删掉。
    JavBus 带来的影片上的挂接不动。返回改了演员的影片
    '''
    old = Tag.get_by_id(actress.tag_id)
    already = {fanhao for (fanhao,) in ItemTag.select(ItemTag.item).where(ItemTag.tag == tag.id).tuples()}
    changed = set()
    for link in list(ItemTag.select().where((ItemTag.tag == old.id) & ItemTag.item.in_(_fill_movies()))):
        if link.item_id in already:
            link.delete_instance()
        else:
            ItemTag.update(tag=tag.id).where(ItemTag.id == link.id).execute()
        changed.add(link.item_id)
    Actress.update(tag=tag.id).where(Actress.id == actress.id).execute()
    actress.tag = tag.id
    rest = _aliases_of(actress)
    if _made_up(old, actress):
        # 改用 JavBus 演员页上的名字期间她的号还是她的：设定里可能存着它，别人也不该再编到它，取消关联时用回它（见 _own_number）
        names = rest + [old.value] if is_star_page(tag.url) else rest
    elif is_star_page(old.url):
        names = rest + [old.value]
    else:
        names = [old.value] + rest
    _put_aliases(actress, names)
    if old.url.startswith(AVWIKI_PAGE) and not ItemTag.select().where(ItemTag.tag == old.id).exists():
        old.delete_instance()
    return changed


def _settle(actress, names):
    '''按她现在知道的名字和地址重新定正式名字（见 _choose），返回改了演员的影片'''
    tag, javbus = _choose(names, actress.avwiki, actress.javbus, actress.id)
    if javbus and javbus != actress.javbus:
        Actress.update(javbus=javbus).where(Actress.id == actress.id).execute()
        actress.javbus = javbus
    if tag.id == actress.tag_id:
        return set()
    return _switch(actress, tag)


def _names_of(actress):
    '''她现在知道的名字：正式名字在前，再是别名'''
    own = Tag.select(Tag.value).where(Tag.id == actress.tag_id).scalar()
    return [own] + _aliases_of(actress)


def actress_on_page(page):
    '''
    这个 av-wiki 女优页的女优，库里还没有时为 None。地址转义了的、解码的都认得（见 _page_forms）；以前的版本按两种写法
    各建了一位时，写法一字不差的那位优先，没有就是先入库的
    '''
    found = list(Actress.select().where(Actress.avwiki.in_(sorted(_page_forms(page)))).order_by(Actress.id))
    return next((actress for actress in found if actress.avwiki == page), found[0] if found else None)


def add_from_card(page, name):
    '''搜索卡片上的名字新建一位女优（只知道这一个名字，女优页还没读）；已经有了就是她。返回 Actress'''
    with db.db.atomic('IMMEDIATE'):
        actress = actress_on_page(page)
        if actress is None:
            tag, javbus = _choose([name], page, '')
            actress = db.add_actress(tag, avwiki=page, javbus=javbus, state=db.ACTRESS_CARD)
            logger.info(f'新的女优 #{actress.id}（只有搜索卡片上的名字）')
    return actress


def read_page(page, names, now):
    '''
    读到了这个女优页上的名字（按页面上的顺序）：没有这位女优就新建；名字都记成别名，按页面上的顺序重新定正式名字。
    返回 (Actress, 改了演员的影片)
    '''
    with db.db.atomic('IMMEDIATE'):
        actress = actress_on_page(page)
        if actress is None:
            tag, javbus = _choose(names, page, '')
            actress = db.add_actress(tag, avwiki=page, javbus=javbus, state=db.ACTRESS_PAGE, names_at=now)
            _put_aliases(actress, names)
            logger.info(f'新的女优 #{actress.id}（读了女优页，{len(names)} 个名字）')
            return actress, set()
        Actress.update(state=db.ACTRESS_PAGE, names_at=now).where(Actress.id == actress.id).execute()
        changed = _settle(actress, names)
        # 别名按女优页上的顺序排，以前记下的、页面上已经没有的留在后面：以前的版本记成别名的"名字（N）"也留着，
        # 设定里可能存着它（编出来的名字不再新记成别名，见 _switch）
        _put_aliases(actress, names + _aliases_of(actress))
        logger.info(f'女优 #{actress.id} 的女优页读到了 {len(names)} 个名字' +
                    (f'，正式名字换了，改了 {len(changed)} 部影片的演员' if changed else ''))
        return actress, changed


def page_missing(page, now):
    '''女优页读不到名字（网站上没有这一页、页面上没有名字栏）：记下读过的时间，不再一轮一轮地读'''
    with db.db.atomic('IMMEDIATE'):
        Actress.update(names_at=now).where(Actress.avwiki.in_(sorted(_page_forms(page)))).execute()


def found_javbus(key, path, name):
    '''
    "查找她的 JavBus 页面"找到（或用户选了）演员页 path，name 是演员页上的名字：key 是 avwiki:<女优页> 的，记成这位女优的
    JavBus 演员页。name（按 fold 比）是她的一个名字时按规则重定正式名字（库里有这个演员页的这条标签就换成它）；不是她的名字
    （用户在几个候选里点了别的写法）时只记下演员页：按演员补全从那里抓、那里的影片按人算是她的，正式名字和影片上的挂接
    都不动。她已经有演员页时什么都不做。在调用方的事务里，返回改了演员的影片
    '''
    if not key.startswith('avwiki:'):
        return set()
    actress = actress_on_page(key[len('avwiki:'):])
    if actress is None or actress.javbus:
        return set()
    Actress.update(javbus=path).where(Actress.id == actress.id).execute()
    actress.javbus = path
    names = _names_of(actress)
    if fold(name) not in {fold(own) for own in names}:
        return set()
    return _settle(actress, names)


def accept_javbus(key, path, name):
    '''用户从查找结果里点了演员页 path：她说过不是她的也以这次为准（不再算拒绝），然后照 found_javbus 记下'''
    if key.startswith('avwiki:'):
        page = key[len('avwiki:'):]
        rejected = rejected_pages(page)
        if path in rejected:
            _set_rejected(page, rejected - {path})
    return found_javbus(key, path, name)


def unlink_javbus(page, path):
    '''
    用户说 JavBus 演员页 path 不是这位女优（女优页 page）：记下来，以后按名字自动查找、定正式名字都不再用它；她的演员页
    是它就清掉。正式名字按规则重新定：原来是那个演员页的标签时按她自己这一侧的名字定（换下来的旧正式名字排在别名最前面，
    和那条标签只差简繁、大小写的她自己的写法也在别名里，见 _put_aliases），补名写的影片跟着改回来；
    正式名字因此变了，就清掉读女优页的时间，后台再读一次、按页面上的先后再定一遍。
    还没迁移的库里没有她这一行，只记下拒绝。在调用方的事务里，返回改了演员的影片
    '''
    _set_rejected(page, rejected_pages(page) | {path})
    actress = actress_on_page(page)
    if actress is None:
        return set()
    if actress.javbus == path:
        Actress.update(javbus='').where(Actress.id == actress.id).execute()
        actress.javbus = ''
    names = _names_of(actress)
    own = Tag.get_by_id(actress.tag_id)
    if own.url == path:
        names = names[1:] + names[:1]
        if fold(own.value) not in {fold(name) for name in names[:-1]}:
            # 以前的版本换成 JavBus 的写法时没留下她自己的写法：查找这个演员页时用的名字就是她当时的正式名字；
            # 没有查找记录的，是补名时她的一个名字和这条标签一字不差、直接用了它，这个写法就是她的名字（排在女优页上
            # 第几个已经分不出，按第一个算，读到女优页时再按页面上的先后定）
            searched = db.StarLink.get_or_none(db.StarLink.key == f'avwiki:{page}')
            if searched is None:
                names.insert(0, own.value)
            elif fold(searched.name) == fold(own.value):
                names.insert(0, searched.name)
    before = actress.tag_id
    changed = _settle(actress, names)
    if actress.tag_id != before:
        Actress.update(names_at=None).where(Actress.id == actress.id).execute()
    return changed


def search_name_of(page):
    '''这位女优（女优页 page）现在查找 JavBus 演员页时用的名字（见 _search_form）；库里还没有她时是空字符串'''
    actress = actress_on_page(page)
    if actress is None:
        return ''
    names = _names_of(actress)
    return _search_form(names[0], names[1:])


def reconcile():
    '''
    知道 JavBus 演员页的女优，库里后来有了那个演员页的标签（例如按演员补全抓来的影片）、名字是她的一个名字、她的正式名字
    还不是它：按规则换过去。演员页上只有别的写法的（用户点选的）不动。返回改了演员的影片
    '''
    later = defaultdict(list)
    query = (Actress.select(Actress.id, Tag.value)
             .join(Tag, on=((Tag.url == Actress.javbus) & (Tag.type_ == 'star') & (Tag.id != Actress.tag)))
             .where(Actress.javbus != ''))
    for actress_id, value in query.tuples():
        later[actress_id].append(fold(value))
    changed = set()
    for actress_id, values in later.items():
        if not set(values) & {fold(name) for name in _names_of(Actress.get_by_id(actress_id))}:
            continue
        with db.db.atomic('IMMEDIATE'):
            actress = Actress.get_by_id(actress_id)
            changed |= _settle(actress, _names_of(actress))
    return changed


def set_movie_actresses(fanhao, people, replace=False):
    '''
    影片挂上这几位女优（db.Actress）的正式名字。replace 为假（补名）：影片还没有演员时才写；replace 为真（校正）：
    补名记录还是 stale 时换掉它的全部演员。返回挂上的女优数；影片已经不在、已经有了演员、或者不再要校正时返回 None
    '''
    with db.db.atomic('IMMEDIATE'):
        if Item.get_or_none(Item.fanhao == fanhao) is None:
            return None
        if replace:
            row = ActressLookup.get_or_none(ActressLookup.item == fanhao)
            if row is None or row.status != db.LOOKUP_STALE:
                return None
            stars = ItemTag.select(ItemTag.id).join(Tag).where((ItemTag.item == fanhao) & (Tag.type_ == 'star'))
            ItemTag.delete().where(ItemTag.id.in_(stars)).execute()
        elif db.has_star(fanhao):
            return None
        tags = list(dict.fromkeys(Actress.get_by_id(actress.id).tag_id for actress in people))
        if tags:
            ItemTag.insert_many([{'item': fanhao, 'tag': tag_id} for tag_id in tags]).execute()
        return len(tags)


def pages_to_read(chosen=(), limit=None):
    '''
    还要读女优页的女优（db.Actress，先入库的在前）：迁移整理出来、还没读过的（校正）；喜欢的影片上、
    只有卡片名字的；指定推荐里选了的（chosen 是设定里的女优名字）、只有卡片名字的。读过的（包括读不到的）不再读
    '''
    unread = Actress.names_at.is_null()
    liked = (ItemTag.select(ItemTag.tag)
             .join(ItemRate, on=(ItemRate.item == ItemTag.item))
             .where((ItemRate.rate_type == RATE_TYPE.USER_RATE) & (ItemRate.rate_value == db.RATE_VALUE.LIKE)))
    picked = [person.actress for found in find_people(chosen).values() for person in found if person.actress]
    query = (Actress.select().where(unread & Actress.avwiki.is_null(False)
                                    & ((Actress.state == db.ACTRESS_LEGACY) | Actress.tag.in_(liked)
                                       | Actress.id.in_(picked or [0])))
             .order_by(Actress.id).limit(limit))
    return list(query)


def upgrade_marks():
    '''(迁移做完的时间, 迁移后的校正做完的时间)，还没有的是 None'''
    return _mark(STORAGE_MARK), _mark(FIXED_MARK)


def correction_left():
    '''迁移的结果还要校正的：(还要重新查的影片数, 还要读女优页的女优数)'''
    movies = ActressLookup.select().where(ActressLookup.status == db.LOOKUP_STALE).count()
    pages = Actress.select().where((Actress.state == db.ACTRESS_LEGACY) & Actress.names_at.is_null()).count()
    return movies, pages


def finish_correction(now):
    '''迁移的结果都校正完了（没有还要重新查的影片、还要读的女优页）时记下时间，只记一次。记下了返回 True'''
    if _mark(STORAGE_MARK) is None or _mark(FIXED_MARK) is not None or any(correction_left()):
        return False
    with db.db.atomic('IMMEDIATE'):
        _set_mark(FIXED_MARK, now)
    logger.info('演员数据升级后的校正做完了')
    return True


@dataclass
class Migration:
    '''
    一次迁移的结果。state：done 做完了 / skipped 不用做（没有旧数据，或者已经做过）/ failed 没做成（reason 是原因）；
    again 是"迁移过的库里，又整理旧版本后来按旧存法写下的名字"；snapshot 是迁移前的快照；
    actresses、movies、aliases 是新整理出的女优数、改了演员的影片数、从标签变成别名的名字数
    '''
    state: str
    at: datetime
    reason: str = ''
    snapshot: str = ''
    actresses: int = 0
    movies: int = 0
    aliases: int = 0
    again: bool = False


# 这次启动以来最近一次迁移的结果（关于页用），还没试过时是 None
last_migration = None


def _legacy():
    '''还没整理的旧行：地址是 av-wiki 女优页、又不是哪位女优正式名字的 star 标签'''
    return Tag.select(Tag.id).where((Tag.type_ == 'star') & Tag.url.startswith(AVWIKI_PAGE)
                                    & Tag.id.not_in(Actress.select(Actress.tag)))


def _mark(name):
    '''job_state 里这一行的时间，没有时为 None'''
    row = JobState.get_or_none(JobState.name == name)
    return None if row is None else row.at


def _set_mark(name, now):
    if not JobState.update(text='done', at=now).where(JobState.name == name).execute():
        JobState.create(name=name, text='done', at=now)


def migrated():
    '''库里的演员数据是不是新存法：迁移做过了，或者根本没有旧数据（新装的、旧版本从没补过名字的库）'''
    return _mark(STORAGE_MARK) is not None or not _legacy().exists()


def _written_the_old_way():
    '''
    迁移以后又按旧存法补了名字的影片（降级用过旧版本）：演员是补名写的、又挂着旧行。迁移把补名写的影片上的旧行都换成了
    正式名字，新版本补名只挂正式名字，换正式名字时也只把补名写的影片改挂新的，所以正常的库里没有这样的影片；
    迁移以后留着的旧行只挂在演员不是补名写的影片上（JavBus 的影片共用了那一行、没有补名记录的影片），不算
    '''
    return ItemTag.select(ItemTag.item).where(ItemTag.tag.in_(_legacy()) & ItemTag.item.in_(_fill_movies())).distinct()


def features_changed_at():
    '''
    最近一次演员的名字整体换了的时间（迁移、校正做完），模型是这之前训练的就该重新训练；从没换过时为 None。
    再整理不算：它只动旧版本降级期间补了名字的几部喜欢的影片，和平时补上一部的演员一样，不用为它重新训练
    '''
    marks = [at for at in (_mark(STORAGE_MARK), _mark(FIXED_MARK)) if at is not None]
    return max(marks) if marks else None


def _last_tidy():
    '''最近一次做成整理（迁移，或者迁移以后的再整理）的时间，还没有时为 None'''
    marks = [at for at in (_mark(STORAGE_MARK), _mark(RETIDY_MARK)) if at is not None]
    return max(marks) if marks else None


def _drop_unsaved(now, done):
    '''
    今天带时分秒的快照里比最近一次做成的整理（done）还晚的，是那以后提交之前就被打断的整理留下的（库那时没有变）：
    连同没写完的临时文件删掉。做成了的那几次的（名字里的时间就是那次的记号）、当天第一份、别的日子的都不碰
    '''
    folder = get_data_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        match = _TIMED_SNAPSHOT.fullmatch(name)
        if match is None:
            continue
        made = datetime.strptime(match.group(1), '%Y%m%d-%H%M%S')
        if made.date() == now.date() and made > done.replace(microsecond=0):
            remove_file(os.path.join(folder, name))


def _snapshot(now):
    '''
    迁移前的一致性快照：VACUUM INTO 数据目录里的 bus-before-actress-upgrade-<年月日>.db（先写临时文件再改名）。
    今天已经做成过一次整理，那一次的快照留着，这一份另起名字 …-<年月日>-<时分秒>.db（今天被打断的几次留下的先删掉，
    见 _drop_unsaved）；别的情况下同一天的同名文件是前一次没做成留下的（那时库没有变），直接换掉。返回文件路径
    '''
    path = get_data_path(SNAPSHOT_NAME.format(now.strftime('%Y%m%d')))
    done = _last_tidy()
    if done is not None and done.date() == now.date() and os.path.exists(path):
        _drop_unsaved(now, done)
        path = get_data_path(SNAPSHOT_NAME.format(now.strftime('%Y%m%d-%H%M%S')))
    temp = path + '.tmp'
    remove_file(temp)
    try:
        db.db.execute_sql('VACUUM INTO ?', (temp,))
        os.replace(temp, path)
    except BaseException:
        remove_file(temp)
        raise
    return path


def _snapshot_problem(error):
    if isinstance(error, OSError):
        return os_error_text(error)
    text = str(error)
    if 'full' in text:
        return '磁盘空间不足'
    if 'unable to open' in text:
        return '数据目录写不了文件'
    return text


def _failed(now, reason, again):
    if again:
        logger.warning(f'旧版本按旧存法写下的演员名没有整理：{reason}。程序照常可用，开着的话明天再试一次，下次启动时也会再试')
    else:
        logger.warning(f'演员数据没有升级：{reason}。程序照常可用，开着的话明天再试一次，下次启动时也会再试；'
                       '在那之前不去 av-wiki 补演员名')
    return Migration('failed', now, reason, again=again)


def _database_problem(error, waited):
    '''
    数据库出的错换成没做成的原因；waited 是这一次从开始等写锁到出错按钟表过了多少秒（SQLite 自己按名义时长累计，
    机器忙时实际等的常是设定的两倍多）
    '''
    if not db.is_busy(error):
        return f'数据库出错（{error}）'
    if db.locked_out(error):
        return db.busy_reason(error)
    seconds = round(waited)
    return f'数据库正被别的程序占用（等了 {seconds} 秒还没轮到）' if seconds else '数据库正被别的程序占用（等了不到 1 秒还没轮到）'


def migrate(wait=None):
    '''
    旧的存法（av-wiki 补名时每个名字各是一条 star 标签）换成一个人一条正式名字 + 别名表，升级后第一次启动时做。
    已经迁移过的库里又有了旧版本按旧存法补的影片（降级用过旧版本，见 _written_the_old_way）时再整理一次，
    只动这些影片。没有要整理的时候什么都不做。先做快照，做不成就不整理；然后在一个事务里做完，中途出错或进程被杀，
    库还是原来的样子（这一次的快照删掉），下次再做。
    wait 不是 None 时是程序开着时的每天再试：这一次等写锁最多 wait 秒，做快照之前先拿一下写锁，库被占着就不做快照。
    返回 Migration，也记在 last_migration 里
    '''
    global last_migration
    now = get_now_time()
    again = last_migration.again if last_migration is not None else False
    started = monotonic()
    try:
        with db.waiting_at_most(wait) if wait is not None else contextlib.nullcontext():
            only = None if not migrated() else {fanhao for (fanhao,) in _written_the_old_way().tuples()}
            again = only is not None
            if again and not only:
                result = Migration('skipped', now)
            else:
                if wait is not None:
                    with db.db.atomic('IMMEDIATE'):
                        pass
                result = _with_snapshot(now, only)
    except OperationalError as ex:
        result = _failed(now, _database_problem(ex, monotonic() - started), again)
    last_migration = result
    return result


def _with_snapshot(now, only):
    again = only is not None
    try:
        snapshot = _snapshot(now)
    except (OSError, OperationalError) as ex:
        problem = _snapshot_problem(ex)
        return _failed(now, f'整理前的快照没有做成（{problem}）' if again else f'升级前的快照没有做成（{problem}）', again)
    return _migrate(now, snapshot, only)


def _migrate(now, snapshot, only):
    again = only is not None
    started = monotonic()
    try:
        with db.db.atomic('IMMEDIATE'):
            result = _Rewrite(now, only).run()
            # 再整理的那几部记成了要重新查（"关于"页的校正进度按它们数），升级、校正做完的两个记号不动
            # （见 features_changed_at），另记这一次的时间（同一天的快照起名要看它）
            _set_mark(RETIDY_MARK if again else STORAGE_MARK, now)
    except Exception as ex:
        remove_file(snapshot)
        if isinstance(ex, OperationalError):
            return _failed(now, _database_problem(ex, monotonic() - started), again)
        logger.exception('演员数据升级时程序出错')
        return _failed(now, hide_actress(f'程序出错：{ex!r}'), again)
    result.snapshot = snapshot
    result.again = again
    if again:
        logger.info(f'旧版本按旧存法写下的演员名又整理了一次：新整理出 {result.actresses} 位女优，改了 {result.movies} 部'
                    f'影片的演员，{result.aliases} 个名字从标签变成了别名；整理前的快照在 {snapshot}')
    else:
        logger.info(f'演员数据升级完成：整理出 {result.actresses} 位女优，改了 {result.movies} 部影片的演员，'
                    f'{result.aliases} 个名字从标签变成了别名；升级前的快照在 {snapshot}')
    return result


class _Rewrite:
    '''
    迁移本身（在调用方的事务里）。离线能定的先定：
    - 哪些影片的演员是补名写的：有补名记录（filled）；或者没有记录、有用户打标、上面的每个女优页都"在"、
      至少有一个女优页有两行在它上面（JavBus 一位演员只给一个名字，凑不出一个女优页的两行）。别的影片一条不动。
    - 女优页 U "在"影片 M 上：U 的行里、M 补名时已经有的那些，M 带着一半以上（别人和她共用的一个读音不算她在）。
    - 补名写的影片按挂上的先后排开（一次补名写下的是一位女优接一位女优的名字）：在的女优页的行归她；夹在同一位
      女优两行中间的、排在第一位前面或最后一位后面的也归她；夹在两位之间的，只有别的影片证明它是其中一位的才归她，
      否则不归任何人（拿不准，不并）：JavBus 行原样留着；旧行不挂（建它的那位不在这部上，是她和左右某一位共用的
      名字，左右两位都已经在了，去掉这一行不会少人），名字照样是建它的那位的别名。
    - 每位女优的名字顺序取她挂得最全的那一段；正式名字：她的名字里 JavBus 带来的标签排得最靠前的那条，没有时是
      她的第一个名字；那个名字已经是别人的正式名字（两位女优同名）时用"名字（2）"。
    定不准的（女优页上真正的顺序、两位之间的名字）由后台补名任务重新读女优页、重新搜索来校正：
    这些影片的补名记录改成 stale，这些女优的资料记成 legacy。
    only 不是 None 时是在迁移过的库里再整理一次：只有这些影片算补名写的，已经有女优的女优页并进她（正式名字不变）
    '''

    def __init__(self, now, only=None):
        self.now = now
        self.only = only

    def run(self):
        legacy = {tag_id: (value, url) for tag_id, value, url in
                  Tag.select(Tag.id, Tag.value, Tag.url).where(Tag.id.in_(_legacy())).tuples()}
        groups = defaultdict(list)
        for tag_id in sorted(legacy):
            groups[legacy[tag_id][1]].append(tag_id)
        self.legacy, self.groups = legacy, groups
        self._load_movies()
        self._find_fills()
        self._assign()
        self._order_names()
        self._decide()
        result = Migration('done', self.now)
        result.actresses = self._write_actresses()
        result.movies = self._relink()
        result.aliases = self._drop_rows()
        self._mark_stale()
        return result

    def _load_movies(self):
        movies = ItemTag.select(ItemTag.item).where(ItemTag.tag.in_(_legacy()))
        self.seqs = defaultdict(list)
        self.info = dict(self.legacy)
        # 标签的类型不放进条件里、回到 Python 再挑：放进去的话 SQLite 会从全部 star 标签查起，
        # 每条再到这些影片里各探一遍，影片和演员一多就要几分钟
        query = (ItemTag.select(ItemTag.id, ItemTag.item, ItemTag.tag, Tag.type_, Tag.value, Tag.url)
                 .join(Tag).where(ItemTag.item.in_(movies)).order_by(ItemTag.id))
        for link_id, fanhao, tag_id, kind, value, url in query.tuples():
            if kind == 'star':
                self.seqs[fanhao].append((link_id, tag_id))
                self.info[tag_id] = (value, url)
        self.first_link = dict(ItemTag.select(ItemTag.tag, fn.MIN(ItemTag.id)).where(ItemTag.tag.in_(_legacy()))
                               .group_by(ItemTag.tag).tuples())
        self.lookups = dict(ActressLookup.select(ActressLookup.item, ActressLookup.status)
                            .where(ActressLookup.item.in_(movies)).tuples())
        self.user_rated = {fanhao for (fanhao,) in ItemRate.select(ItemRate.item).where(
            (ItemRate.rate_type == RATE_TYPE.USER_RATE) & ItemRate.item.in_(movies)).tuples()}

    def _page_of(self, tag_id):
        '''旧行所在的女优页，不是旧行时为 None'''
        row = self.legacy.get(tag_id)
        return row[1] if row else None

    def _find_fills(self):
        self.present = {}
        self.fills = set()
        for fanhao, seq in self.seqs.items():
            last = seq[-1][0]
            on = {tag_id for _, tag_id in seq}
            pages = {self._page_of(tag_id) for _, tag_id in seq} - {None}
            present = set()
            for page in pages:
                earlier = [tag_id for tag_id in self.groups[page] if self.first_link.get(tag_id, last + 1) <= last]
                if 2 * len([tag_id for tag_id in earlier if tag_id in on]) > len(earlier):
                    present.add(page)
            self.present[fanhao] = present
            status = self.lookups.get(fanhao)
            if self.only is not None:
                if fanhao in self.only:
                    self.fills.add(fanhao)
            elif status in (db.LOOKUP_FILLED, db.LOOKUP_STALE):
                self.fills.add(fanhao)
            elif status is None and fanhao in self.user_rated and pages == present and any(
                    sum(1 for _, tag_id in seq if self._page_of(tag_id) == page) >= 2 for page in present):
                self.fills.add(fanhao)

    def _assign(self):
        '''补名写的影片上每个位置归哪位女优（女优页），归不了的是 None'''
        self.owners, self.own = {}, {}
        self.certain = defaultdict(set)
        for fanhao in self.fills:
            seq, present = self.seqs[fanhao], self.present[fanhao]
            own = [self._page_of(tag_id) if self._page_of(tag_id) in present else None for _, tag_id in seq]
            owners = list(own)
            owned = [i for i, page in enumerate(own) if page]
            for i, page in enumerate(own):
                if page or not owned:
                    continue
                before = max((j for j in owned if j < i), default=None)
                after = min((j for j in owned if j > i), default=None)
                if before is None or after is None or own[before] == own[after]:
                    owners[i] = own[after if before is None else before]
            self.owners[fanhao], self.own[fanhao] = owners, own
            for i, page in enumerate(owners):
                if page:
                    self.certain[page].add(seq[i][1])
        # 夹在两位女优之间的：别的影片上已经证明它是其中一位的名字，就归她
        for fanhao in self.fills:
            seq, own, owners = self.seqs[fanhao], self.own[fanhao], self.owners[fanhao]
            owned = [i for i, page in enumerate(own) if page]
            for i, page in enumerate(owners):
                if page or not owned:
                    continue
                before = max(j for j in owned if j < i)
                after = min(j for j in owned if j > i)
                known = [side for side in {own[before], own[after]} if seq[i][1] in self.certain[side]]
                if len(known) == 1:
                    owners[i] = known[0]

    def _order_names(self):
        '''每位女优的名字（标签 id）按顺序：她挂得最全的那一段（一样全时取先补的那部），再加上她别的旧行'''
        best = {}
        for fanhao in self.fills:
            seq, owners = self.seqs[fanhao], self.owners[fanhao]
            segments = defaultdict(list)
            for (_, tag_id), page in zip(seq, owners):
                if page:
                    segments[page].append(tag_id)
            for page, tag_ids in segments.items():
                rank = (-len(tag_ids), seq[0][0])
                if page not in best or rank < best[page][0]:
                    best[page] = (rank, tag_ids)
        self.order = {}
        for page, rows in self.groups.items():
            tag_ids = list(best[page][1]) if page in best else []
            tag_ids += [tag_id for tag_id in rows + sorted(self.certain[page]) if tag_id not in tag_ids]
            self.order[page] = tag_ids

    def _decide(self):
        '''定每位女优的正式名字（标签 id）'''
        existing = {avwiki: (actress_id, tag_id) for actress_id, tag_id, avwiki in
                    Actress.select(Actress.id, Actress.tag, Actress.avwiki).where(Actress.avwiki.in_(list(self.groups)))
                    .tuples()}
        self.existing = existing
        self.claimed = {tag_id for (tag_id,) in Actress.select(Actress.tag).tuples()}
        self.canonical = {page: tag_id for page, (_, tag_id) in existing.items()}
        # JavBus 带来的演员标签：地址不是女优页，或者挂在演员不是补名写的影片上（原文相同，共用了补名建的那一行）
        given = {tag_id for fanhao, seq in self.seqs.items() if fanhao not in self.fills for _, tag_id in seq}
        javbus = {tag_id for tags in self.order.values() for tag_id in tags
                  if tag_id in given or not self.info[tag_id][1].startswith(AVWIKI_PAGE)}
        pending = sorted((page for page in self.groups if page not in existing), key=lambda page: self.groups[page][0])
        # 用户说过不是她的 JavBus 演员页（还没迁移的库里也能取消关联）：那里的标签不是她的名字
        refused = {row.key[len(NOT_HER):]: {path for path, _ in row.candidate_list}
                   for row in db.StarLink.select().where(db.StarLink.key.startswith(NOT_HER))}
        # 第一个名字是别的女优页建的那一行的，等那几位先定：那一行是建它的那位的名字
        first = [page for page in pending if self._page_of(self.order[page][0]) in (page, None)]
        for page in first + [page for page in pending if page not in first]:
            sure = self.certain[page] | set(self.groups[page])
            not_hers = refused.get(page, set())
            given = [tag_id for tag_id in self.order[page] if tag_id in sure and tag_id in javbus
                     and tag_id not in self.claimed and self.info[tag_id][1] not in not_hers]
            choice = given[0] if given else self.order[page][0]
            if choice in self.claimed or self.info[choice][1] in not_hers:
                choice = self._same_name(self.info[choice][0], page)
            self.canonical[page] = choice
            self.claimed.add(choice)

    def _same_name(self, name, page):
        '''两位女优的第一个名字相同：新建"名字（2）"（这个号有人用着就 3、4……，见 _number_taken）当她的正式名字'''
        number = 2
        while _number_taken(SAME_NAME.format(name, number)):
            number += 1
        tag = Tag.create(type_='star', value=SAME_NAME.format(name, number), url=page)
        self.info[tag.id] = (tag.value, page)
        return tag.id

    def _write_actresses(self):
        found = db.found_pages([f'avwiki:{page}' for page in self.groups])
        made = 0
        for page in sorted(self.groups, key=lambda page: self.groups[page][0]):
            names = [self.info[tag_id][0] for tag_id in self.order[page]]
            if page in self.existing:
                actress = Actress.get_by_id(self.existing[page][0])
            else:
                url = self.info[self.canonical[page]][1]
                javbus = url if is_star_page(url) else found.get(f'avwiki:{page}', '')
                actress = Actress.create(tag=self.canonical[page], avwiki=page, javbus=javbus, state=db.ACTRESS_LEGACY)
                made += 1
            db.add_aliases(actress, names)
        return made

    def _relink(self):
        moved = 0
        for fanhao in sorted(self.fills):
            seq, owners = self.seqs[fanhao], self.owners[fanhao]
            # 有人在的影片上，归不了人的位置都夹在两位不同的女优之间
            between = any(owners)
            targets = []
            for (_, tag_id), page in zip(seq, owners):
                if page:
                    target = self.canonical[page]
                elif tag_id in self.legacy and between:
                    continue
                elif tag_id in self.legacy:
                    target = self.canonical[self._page_of(tag_id)]
                else:
                    target = tag_id
                if target not in targets:
                    targets.append(target)
            if targets != [tag_id for _, tag_id in seq]:
                ItemTag.delete().where(ItemTag.id.in_([link_id for link_id, _ in seq])).execute()
                ItemTag.insert_many([{'item': fanhao, 'tag': tag_id} for tag_id in targets]).execute()
                moved += 1
        return moved

    def _drop_rows(self):
        '''不是正式名字、也没有影片挂着的旧行删掉（名字已经记成别名）'''
        canonical = set(self.canonical.values())
        unused = [tag_id for tag_id in self.legacy if tag_id not in canonical]
        dropped = 0
        for chunk in chunked(unused, IN_BATCH):
            linked = {tag_id for (tag_id,) in ItemTag.select(ItemTag.tag).where(ItemTag.tag.in_(chunk)).tuples()}
            gone = [tag_id for tag_id in chunk if tag_id not in linked]
            if gone:
                dropped += Tag.delete().where(Tag.id.in_(gone)).execute()
        return dropped

    def _mark_stale(self):
        '''补名写的影片要按新存法重新查一次（校正）：补名记录改成 stale，现在就到期'''
        for fanhao in sorted(self.fills):
            values = dict(status=db.LOOKUP_STALE, misses=0, next_try=self.now, detail='')
            if not ActressLookup.update(**values).where(ActressLookup.item == fanhao).execute():
                ActressLookup.create(item=fanhao, checked_at=self.now, **values)
