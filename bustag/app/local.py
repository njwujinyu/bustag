'''
handle local file related functions
'''
import os
import re
from peewee import SqliteDatabase, DatabaseError

from bustag.spider.db import Item, LocalItem, ItemRate, RATE_TYPE, RATE_VALUE, db, DBError
from bustag.util import logger, get_data_path

UPLOADED_DB = 'uploaded.db'


def add_local_fanhao(fanhao, tag_like):
    '''
    Args:
        fanhao:str - ',' separeted (fanhao, path)
    '''
    rows = fanhao.splitlines()
    items = []
    missed_fanhaos = []
    local_file_added = 0
    tag_file_added = 0
    pattern = r'([A-Z]+)-?([0-9]+)'
    for row in rows:
        if ',' in row:
            # URL 里也可能有逗号，只拆第一个
            fanhao, path = row.split(',', 1)
        else:
            fanhao = row
            path = None

        fanhao = fanhao.strip().upper()
        match = re.search(pattern, fanhao)
        if match and len(match.groups()) == 2:
            series, num = match.groups()
            matched_fanhao = f'{series}-{num}'
            path = path.strip() if path else None
            logger.debug(f'matched fanhao {matched_fanhao}')
            items.append((matched_fanhao, path))
    with db.atomic():
        for item in items:
            fanhao, path = item
            # if path is not None, add to local item
            if path:
                local_item = LocalItem.saveit(fanhao, path)
                if local_item:
                    local_file_added += 1
            # if tag_like is True, add it to item_rate table
            if tag_like:
                item_rate = ItemRate.saveit(
                    RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, fanhao)
                if item_rate:
                    tag_file_added += 1
            if not Item.get_by_fanhao(fanhao):
                # add to get from spider
                missed_fanhaos.append(fanhao)
    logger.debug(f'missed_fanhaos:{missed_fanhaos}')
    logger.debug(f'tag_file_added:{tag_file_added}')
    logger.debug(f'local_file_added:{local_file_added}')
    return missed_fanhaos, local_file_added, tag_file_added


def _remove_db_files(db_name):
    for suffix in ('', '-wal', '-shm', '-journal'):
        try:
            os.remove(db_name + suffix)
        except FileNotFoundError:
            pass
        except OSError as ex:
            logger.warning(f'临时文件删除失败 {db_name + suffix}：{ex}')


def _read_user_rates(db_upload):
    '''
    从上传的库里读出用户打标数据 [(rate_value, fanhao), ...]，库不是 bustag 的数据库时抛 DBError
    '''
    try:
        tables = set(db_upload.get_tables())
    except DatabaseError as ex:
        raise DBError('不是有效的 SQLite 数据库文件') from ex
    if not {'item', 'item_rate'} <= tables:
        raise DBError('不是 bustag 的数据库：找不到 item、item_rate 表')
    sql_old = '''select item_rate.rate_value, item.fanhao
                from item_rate inner
                join item on item_rate.item_id = item.id
                where item_rate.rate_type=1 '''

    sql_new = '''select item_rate.rate_value, item.fanhao
                from item_rate inner
                join item on item_rate.item_id = item.fanhao
                where item_rate.rate_type=1 '''
    try:
        # 旧版数据库的 item_rate.item_id 指向 item.id，新版指向 item.fanhao
        rows = db_upload.execute_sql(sql_old).fetchall()
        if not rows:
            rows = db_upload.execute_sql(sql_new).fetchall()
    except DatabaseError as ex:
        raise DBError(f'数据库结构和 bustag 不一致：{ex}') from ex
    return rows


def load_tags_db():
    '''
    load user tags data from uploaded db file (data/uploaded.db)，读完后关闭并删除这个临时文件

    Raises:
        DBError: 不是 SQLite 文件，或者不是 bustag 的数据库
    '''
    db_name = get_data_path(UPLOADED_DB)
    db_upload = SqliteDatabase(db_name)
    try:
        tag_data = _read_user_rates(db_upload)
    finally:
        db_upload.close()
        _remove_db_files(db_name)
    missed_fanhaos = []
    tag_file_added = 0
    with db.atomic():
        for rate_value, fanhao in tag_data:
            item_rate = ItemRate.saveit(
                RATE_TYPE.USER_RATE, rate_value, fanhao)
            if item_rate:
                tag_file_added += 1
            if not Item.get_by_fanhao(fanhao):
                # add to get from spider
                missed_fanhaos.append(fanhao)
    logger.debug(tag_data)
    logger.info(f'导入打标数据：新增 {tag_file_added} 条打标')
    logger.info(f'导入打标数据：{len(missed_fanhaos)} 个番号需要下载')
    return tag_file_added, missed_fanhaos
