'''
本地文件番号（/local_fanhao）和打标数据导入（/load_db）
'''
import glob
import os
import re
import tempfile
from dataclasses import dataclass, field

from peewee import SqliteDatabase, DatabaseError

from bustag.spider.db import Item, LocalItem, ItemRate, RATE_VALUE, db, DBError
from bustag.spider.parser import is_valid_fanhao
from bustag.util import logger, get_data_dir

# 上传的数据库先存成数据目录里的 upload-<随机>.db，导入完就删
UPLOAD_PREFIX = 'upload-'
UPLOAD_SUFFIX = '.db'
# 下面两个事务都是先读后写：WAL 模式下普通事务读过之后，别的事务先提交了，它再写就直接报
# database is locked（等多久都没用）。一开始就拿写锁，两次提交同时进行时后一个排队等前一个
WRITE_LOCK = 'IMMEDIATE'
# 先找带横线的番号，横线前可以有数字（300MIUM-1506、T28-123）；找不到再认不带横线的写法（ABP001）
FANHAO_WITH_DASH = re.compile(r'([0-9]*[A-Z]+[0-9]*)-([0-9]+)')
FANHAO_WITHOUT_DASH = re.compile(r'([0-9]*[A-Z]+)([0-9]+)')


def find_fanhao(text):
    '''
    从一行文字里找出番号，统一成大写、带横线的写法；找不到返回 None
    '''
    text = text.strip().upper()
    match = FANHAO_WITH_DASH.search(text) or FANHAO_WITHOUT_DASH.search(text)
    if match is None:
        return None
    return f'{match.group(1)}-{match.group(2)}'


def add_local_fanhao(fanhao, tag_like):
    '''
    Args:
        fanhao: 多行文字，每行是"番号"或"番号,文件地址"
        tag_like: 是否把这些番号都打标为喜欢（已有的用户打标不改）

    Returns:
        (要下载的番号, 新增的本地文件数, 新增或由系统推荐改为用户打标的条数)
    '''
    items = []
    for row in fanhao.splitlines():
        # URL 里也可能有逗号，只拆第一个
        text, _, path = row.partition(',')
        matched = find_fanhao(text)
        if matched:
            logger.debug(f'matched fanhao {matched}')
            items.append((matched, path.strip() or None))
    missed_fanhaos = []
    checked = set()
    local_file_added = 0
    tag_file_added = 0
    with db.atomic(WRITE_LOCK):
        for fanhao, path in items:
            if path and LocalItem.saveit(fanhao, path):
                local_file_added += 1
            if tag_like and ItemRate.save_user_rate(fanhao, RATE_VALUE.LIKE) != 'kept':
                tag_file_added += 1
            if fanhao not in checked:
                checked.add(fanhao)
                if not Item.get_by_fanhao(fanhao):
                    missed_fanhaos.append(fanhao)
    logger.debug(f'missed_fanhaos:{missed_fanhaos}')
    logger.debug(f'tag_file_added:{tag_file_added}')
    logger.debug(f'local_file_added:{local_file_added}')
    return missed_fanhaos, local_file_added, tag_file_added


def save_upload(upload):
    '''
    把上传的文件存成数据目录里的临时文件，返回路径。每次上传的文件名都不同，同时上传的两个文件互不覆盖
    '''
    fd, path = tempfile.mkstemp(prefix=UPLOAD_PREFIX, suffix=UPLOAD_SUFFIX, dir=get_data_dir())
    try:
        with os.fdopen(fd, 'wb') as f:
            upload.save(f)
    except BaseException:
        _remove_db_files(path)
        raise
    return path


def remove_stale_uploads():
    '''
    删掉上次运行留下的上传临时文件（导入途中程序被关掉时会留下）
    '''
    pattern = os.path.join(glob.escape(get_data_dir()), f'{UPLOAD_PREFIX}*{UPLOAD_SUFFIX}*')
    for path in glob.glob(pattern):
        try:
            os.remove(path)
        except OSError as ex:
            logger.warning(f'临时文件删除失败 {path}：{ex}')


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


@dataclass
class TagsImport:
    '''
    导入打标数据的结果
    '''
    added: int = 0      # 新增的用户打标
    updated: int = 0    # 原来是系统推荐、改成了用户打标
    kept: int = 0       # 本地已经有用户打标，保持不变
    invalid: int = 0    # 番号不合法或评分不是 0 / 1，跳过
    missed_fanhaos: list = field(default_factory=list)  # 库里还没有影片信息、要下载的番号

    @property
    def imported(self):
        return self.added + self.updated


def load_tags_db(db_name):
    '''
    从上传的数据库文件导入用户打标，读完后关闭并删除这个文件。
    番号转成大写，番号不合法或评分不是 0 / 1 的跳过；本地已有的用户打标不改

    Raises:
        DBError: 不是 SQLite 文件，或者不是 bustag 的数据库
    '''
    db_upload = SqliteDatabase(db_name)
    try:
        tag_data = _read_user_rates(db_upload)
    finally:
        db_upload.close()
        _remove_db_files(db_name)
    result = TagsImport()
    checked = set()
    with db.atomic(WRITE_LOCK):
        for rate_value, fanhao in tag_data:
            fanhao = fanhao.strip().upper() if isinstance(fanhao, str) else None
            if not is_valid_fanhao(fanhao) or rate_value not in (RATE_VALUE.LIKE, RATE_VALUE.DISLIKE):
                result.invalid += 1
                continue
            outcome = ItemRate.save_user_rate(fanhao, rate_value)
            if outcome == 'added':
                result.added += 1
            elif outcome == 'updated':
                result.updated += 1
            else:
                result.kept += 1
            if fanhao not in checked:
                checked.add(fanhao)
                if not Item.get_by_fanhao(fanhao):
                    result.missed_fanhaos.append(fanhao)
    logger.info(f'导入打标数据：新增 {result.added} 条，由系统推荐改为用户打标 {result.updated} 条，'
                f'已有用户打标 {result.kept} 条，跳过不合法的 {result.invalid} 条，'
                f'{len(result.missed_fanhaos)} 个番号需要下载')
    return result
