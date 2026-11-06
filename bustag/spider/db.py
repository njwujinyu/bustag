'''
persist data to db
'''
from datetime import date
import datetime
import operator
from functools import reduce
import json
from peewee import *
from enum import IntEnum
from collections import defaultdict
from urllib.parse import urlparse
from bustag.util import logger, get_data_path, format_datetime, get_now_time, get_full_url
from bustag.spider.covers import normalize_cover_url

DB_FILE = 'bus.db'
db = SqliteDatabase(get_data_path(DB_FILE), pragmas={
    'journal_mode': 'wal'})


class BaseModel(Model):

    class Meta:
        database = db
        legacy_table_names = False


class ExistError(Exception):
    pass


class DBError(Exception):
    pass


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

    def __repr__(self):
        return f'<Item:{self.fanhao} {self.title}>'

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
            logger.debug(f'save item:  {item}')
        except IntegrityError:
            logger.debug(f'Item exists: {item_fanhao}')
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
        return f'<Tag {self.value}>'

    @staticmethod
    def saveit(tag_info):
        tag, created = Tag.get_or_create(type_=tag_info.type, value=tag_info.value,
                                         defaults={'url': urlparse(tag_info.link).path})
        if created:
            logger.debug(f'save tag:  {tag}')
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
            logger.debug(f'save tag_item: {item_tag}')
        except Exception as ex:
            logger.exception(ex)
        else:
            return item_tag

    def __repr__(self):
        return f'<ItemTag {self.item.fanhao} - {self.tag.value}>'


class RATE_TYPE(IntEnum):
    NOT_RATE = 0
    USER_RATE = 1
    SYSTEM_RATE = 2


class RATE_VALUE(IntEnum):
    LIKE = 1
    DISLIKE = 0


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
            logger.debug(f'save ItemRate: {item_rate}')
        except IntegrityError:
            logger.debug(f'ItemRate exists: {fanhao}')
        else:
            return item_rate

    @staticmethod
    def save_user_rate(fanhao, rate_value, replace_user_rate=False):
        '''
        保存用户打标：没有评分时新增；系统推荐的评分改成用户打标；已经是用户打标时，
        replace_user_rate 为真才改（打标页上用户改自己的打标），批量打标和导入不改。
        返回 'added'、'updated' 或 'kept'
        '''
        item_rate = ItemRate.get_by_fanhao(fanhao)
        if item_rate is None:
            if ItemRate.saveit(RATE_TYPE.USER_RATE, rate_value, fanhao) is not None:
                return 'added'
            # 查询之后、新增之前，别的请求先写入了这个番号的评分
            item_rate = ItemRate.get_by_fanhao(fanhao)
        if item_rate.rate_type == RATE_TYPE.USER_RATE and not replace_user_rate:
            return 'kept'
        item_rate.rate_type = RATE_TYPE.USER_RATE
        item_rate.rate_value = rate_value
        item_rate.rete_time = get_now_time()
        item_rate.save()
        return 'updated'

    @staticmethod
    def confirm_system_rate(fanhao, is_correct):
        '''
        推荐页上确认系统推荐：推荐对了照原值、推荐错了把评分反过来，改成用户打标，返回 True。
        已经是用户打标时不改、返回 False：双击按钮、后退到旧页面再点、两个标签页都点，都不会把用户的选择翻回去。
        判断和修改在同一条 UPDATE 里，两个请求同时到也只改一次
        '''
        changes = {ItemRate.rate_type: RATE_TYPE.USER_RATE}
        if not is_correct:
            changes[ItemRate.rate_value] = Case(ItemRate.rate_value, [(RATE_VALUE.DISLIKE, RATE_VALUE.LIKE)],
                                                RATE_VALUE.DISLIKE)
        updated = (ItemRate.update(changes)
                   .where((ItemRate.item_id == fanhao) & (ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE))
                   .execute())
        return updated > 0

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
            logger.debug(f'save LocalItem: {fanhao}')
        except IntegrityError:
            logger.debug(f'LocalItem exists: {fanhao}')
        else:
            return local_item

    def __repr__(self):
        return f'<LocalItem {self.item_id}({self.path})>'

    @staticmethod
    def update_play(id):
        nrows = (LocalItem
                 .update({LocalItem.last_view_date: get_now_time(),
                          LocalItem.view_times: LocalItem.view_times+1})
                 .where(LocalItem.id == id)
                 .execute())
        logger.debug(f'update LocalItem {id} : rows:{nrows}')
        return LocalItem.get_by_id(id)

    @staticmethod
    def loadit(local_item):
        local_item.last_view_date = format_datetime(
            local_item.last_view_date) if local_item.last_view_date else ''


def save(meta_info, tags):
    '''
    影片和标签在一个事务里入库。返回新建的 Item；番号或 url 已存在时返回 None
    '''
    item_title = meta_info['title']
    with db.atomic():
        try:
            item = Item.saveit(meta_info)
        except ExistError:
            logger.debug(f'item exists: {item_title}')
            return None
        for tag_info in tags:
            tag = Tag.saveit(tag_info)
            if tag:
                ItemTag.saveit(item, tag)
    return item


def _clamp_page(page, total_pages):
    '''页码超过总页数时（例如处理完最后一页的最后一部）按最后一页查，没有数据时是第 1 页'''
    return min(page, max(total_pages, 1))


def get_items(rate_type=None, rate_value=None, page=1, page_size=10):
    '''
    get required items based on some conditions。
    page 为 None 时返回全部；page 超过总页数时返回最后一页，page_info 里是实际的页码
    '''
    items_list = []
    clauses = []
    if rate_type is not None:
        clauses.append(ItemRate.rate_type == rate_type)
    else:
        clauses.append(ItemRate.rate_type.is_null())
    if rate_value is not None:
        clauses.append(ItemRate.rate_value == rate_value)
    q = (Item.select(Item, ItemRate)
         .join(ItemRate, JOIN.LEFT_OUTER, attr='item_rate')
         .where(reduce(operator.and_, clauses))
         .order_by(Item.id.desc())
         )
    total_items = q.count()
    total_pages = (total_items + page_size - 1) // page_size
    if not page is None:
        page = _clamp_page(page, total_pages)
        q = q.paginate(page, page_size)
    items = get_tags_for_items(q)
    for item in items:
        Item.loadit(item)
        item_rate = getattr(item, 'item_rate', None)
        item.rate_value = item_rate.rate_value if item_rate is not None else None
        items_list.append(item)

    page_info = (total_items, total_pages, page, page_size)
    return items_list, page_info


def get_local_items(page=1, page_size=10):
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


def get_today_update_count():
    now = get_now_time()
    year, month, day = now.year, now.month, now.day
    q = Item.select().where((Item.add_date.year == year)
                            & (Item.add_date.month == month)
                            & (Item.add_date.day == day)
                            )
    return q.count()


def get_today_recommend_count():
    now = get_now_time()
    year, month, day = now.year, now.month, now.day
    q = ItemRate.select().where((ItemRate.rete_time.year == year)
                                & (ItemRate.rete_time.month == month)
                                & (ItemRate.rete_time.day == day)
                                & (ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE)
                                & (ItemRate.rate_value == RATE_VALUE.LIKE)
                                )
    return q.count()


def get_tags_for_items(items_query):
    item_tag_query = ItemTag.select()
    tag_query = Tag.select()
    items_with_tags = prefetch(items_query, item_tag_query, tag_query)
    items = []
    for item in items_with_tags:
        Item.get_tags_dict(item)
        items.append(item)

    return items


def init():
    db.connect(reuse_if_open=True)
    db.create_tables([Item, Tag, ItemTag, ItemRate, LocalItem])


init()
