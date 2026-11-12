'''
抓取队列和按演员补全作品。
要抓的详情页记在库里（db.FetchTask），程序关掉再打开也还在：上传番号、导入打标库时库里还没有的番号，
以及从 JavBus 演员页上找到、库里还没有的影片（用户点了"补全"才去找）。排进来的由后台任务一部一部地抓（run），
和定时抓取、打分、补演员名在同一个线程里按顺序执行。
JavBus 的 robots.txt 不欢迎自动抓取，所以按演员补全的量要小：用户点了才抓，每次每位最多 backfill_count 部，
所有演员合计每天最多抓 backfill_daily 部（数的是部，同一部当天再试不重复算）；请求之间的间隔沿用配置的 delay（Fetcher 控制）；
被网站拦截立刻停下，当天不再自动接着抓
'''
import json
from collections import namedtuple
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

from peewee import OperationalError, chunked

from bustag.spider import actresses, bus_spider, db
from bustag.spider.fetcher import BlockedError, FetchError, NotFoundError, RequestCancelled, create_fetcher
from bustag.spider.parser import (ParseError, fanhao_from_path, has_next_page, is_star_page, is_valid_fanhao,
                                  list_page_path, parse_list_counts, parse_list_entries, parse_star_search,
                                  searchstar_path)
from bustag.util import APP_CONFIG, get_now_time, hide_actress, hide_credentials, logger

# 因为网络问题（连不上、超时、服务器出错）同一部最多试这么多次，之后算失败
MAX_TRIES = 3
# 连续这么多次连不上网站（没有收到答复，或者服务器出错）就停下这一次，排着的留到下一次
MAX_FAILURES_IN_A_ROW = 3
# 一位演员的列表最多翻这么多页还没到最后一页就停下：页面结构变了、一直有"下一页"的时候不至于翻个没完
MAX_PAGES = 100
# 按名字找演员时最多记下这么多个候选（结果的第 1 页）
MAX_CANDIDATES = 30
# 上传番号页上"没有抓到的"最多列出这么多个，新排进来的在前
MAX_PROBLEMS_SHOWN = 200
# IN 列表一次最多放这么多个参数（旧版 SQLite 一条语句最多 999 个）
IN_BATCH = 500
# 上传番号、导入打标库这两种来处：抓完以后再查一次同番号的其他条目，失败的在上传番号页上列出来
CODE_ORIGINS = (db.ORIGIN_UPLOAD, db.ORIGIN_IMPORT)
# job_state 里记"按演员补全被网站拦截、今天暂停"的那一行
PAUSE = 'backfill-paused'
# job_state 里记"今天按演员补全抓了几部"的那一行：text 是部数，at 是最近一次增加的时间（不是今天的就作废）
SPENT = 'backfill-spent'

# 写库的事务一开始就拿写锁：网页请求里同时在等锁的数量才管得住（见 db.limited_waiting）
WRITE_LOCK = 'IMMEDIATE'
# 再点"补全"时她登记了的补全还没翻完（后台正在翻，或者轮不到、数据库忙留着），不接受时的说法
STILL_LOOKING = '还在找她的作品'
# 列表页上找不到影片的说法：页面上没有"已有磁力"的数字；数字说她有有磁力的影片（{} 是部数）、却一个影片框也找不到
NO_MOVIES = '没有找到影片，也没有"已有磁力"的数字（页面结构可能变了，或被拦截）'
NO_BOXES = '没有找到影片，页面上却写着有磁力的影片 {} 部（页面结构可能变了，或被拦截）'
# 被拦截时点过"补全"还没翻演员页的、点过"查看"还没读第 1 页的、点过"查找"还没查的，她们那一行上写的原因
DROPPED = '被网站拦截，这次没有翻她的演员页，要继续请再点一次补全'
DROPPED_VIEW = '被网站拦截，这次没有读她的演员页，要继续请再点一次查看'
DROPPED_SEARCH = '被网站拦截，这次没有查找她的 JavBus 演员页，要继续请再点一次查找'
# job_state 里记"被拦截时登记了没做的都丢过了"的那一行：时间是丢的那一刻。拦截的那一刻丢过以后才登记的查看、查找，
# 是用户在暂停期间新点的；暂停比这个记号还晚记下的（旧版本记的暂停），登记了没做的是它留下的
TIDIED = 'backfill-tidied'
# job_state 里记"页面上的数字比已知的多、却记着已经翻到最后一页"的记录整理过了的那一行（见 _reopen_false_ends）
ENDS = 'backfill-ends-checked'
# job_state 里每位演员一行（名字是这个前缀加她的列表页 id）：她这一批补全开始之前，经过的最早一部的发行日期（ISO 格式，
# 没有时是空）。取消这一批时退回到它，前面几批经过的日期留着（见 cancel）
OLDEST = 'backfill-oldest:'

# 上传番号页的"下载队列"：还在排队的个数、没抓到的（db.FetchTask，最多 MAX_PROBLEMS_SHOWN 个）、没列出来的个数
CodeQueue = namedtuple('CodeQueue', 'waiting problems more')


@dataclass
class QueueRun:
    '''一次"抓排队的影片"的结果'''
    finished_at: Optional[datetime] = None
    pages: int = 0         # 读了的列表页（查看、补全）
    queued: int = 0        # 从列表页上新排进队列的
    fetched: int = 0       # 发了请求的详情页
    added: int = 0         # 新入库的
    skipped: int = 0       # 轮到时库里已经有了
    missing: int = 0       # 网站上没有
    failed: int = 0        # 失败，不会再自动试
    retry_later: int = 0   # 这一次没抓到（网络问题），留在队里下次再试
    left: int = 0          # 结束时还在排队的
    left_list: int = 0     # 其中按演员补全的
    held: str = ''         # 按演员补全的为什么今天不接着抓了：'limit' 今天的额度用完了，'paused' 被拦截暂停了
    stopped: str = ''      # 提前停下的原因，正常做完时为空
    blocked: str = ''      # 被拦截时网站那边的原因


# 被拦截了、数据库却正被占着没能把暂停记进库里时的 (原因, 时间)：下一次开始时先记，记下以前不对网站发任何请求
_unrecorded = None
# 最近一次做了事（读了列表页或者抓了影片）的结果；还没有做过事时是最近一次，程序启动后还没有跑过时是 None
last_run = None
# 后台正在做的事，给页面显示进度：('page', 列表页的 id, 页码) 正在读列表页，('movie', 列表页的 id 或 None, 番号)
# 正在抓详情页，('search', db.StarLink 的 id, None) 正在按名字找演员；没有在抓时是 None
activity = None


class _Stop(Exception):
    '''这一次到此为止（被拦截、连续连不上网站），排着的留到下一次。消息是停下的原因'''


class _Site:
    '''这一次对网站的全部请求：记着连续连不上了几次，到了 MAX_FAILURES_IN_A_ROW 次就停下'''

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.failures_in_a_row = 0

    def answered(self):
        self.failures_in_a_row = 0

    def unreachable(self):
        self.failures_in_a_row += 1
        if self.failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
            raise _Stop(f'连续 {MAX_FAILURES_IN_A_ROW} 次连不上网站')


def batch_size():
    '''「补全」一次每位最多排进队列的部数（config.ini 的 [download] backfill_count）'''
    return int(APP_CONFIG['download.backfill_count'])


def daily_limit():
    '''按演员补全每天最多抓的部数，所有演员合计（config.ini 的 [download] backfill_daily）'''
    return int(APP_CONFIG['download.backfill_daily'])


def _day_of(now):
    '''now 所在的那一天：(当天 0 点, 第二天 0 点)'''
    midnight = datetime.combine(now.date(), time.min)
    return midnight, midnight + timedelta(days=1)


def daily_used(now=None):
    '''
    今天（0 点起）因为按演员补全而抓了的部数。同一部当天再试一次不重复算。
    取消排队、再上传这一部会删掉或改掉队列里的行，所以每抓一部另记了一笔（见 _spend），取两个数里大的那个：
    还没有另记的（今天还没抓过）就是队列里今天试过的行数
    '''
    start, end = _day_of(now or get_now_time())
    in_queue = (db.FetchTask.select()
                .where((db.FetchTask.origin == db.ORIGIN_LIST) & (db.FetchTask.tried_at >= start)
                       & (db.FetchTask.tried_at < end))
                .count())
    spent = db.JobState.get_or_none(db.JobState.name == SPENT)
    if spent is not None and spent.at is not None and start <= spent.at < end:
        return max(int(spent.text), in_queue)
    return in_queue


def _spend(task, now):
    '''
    按演员补全的这一部要发请求了：今天抓过的数加 1。这一部今天已经算过的（上一次试的时间在今天）不再加。
    数记在 job_state 里，队列里的行以后被删、被改都不影响它
    '''
    if task.origin != db.ORIGIN_LIST:
        return
    start, end = _day_of(now)
    if task.tried_at is not None and start <= task.tried_at < end:
        return
    with db.db.atomic(WRITE_LOCK):
        used = str(daily_used(now) + 1)
        if not db.JobState.update(text=used, at=now).where(db.JobState.name == SPENT).execute():
            db.JobState.create(name=SPENT, text=used, at=now)


def daily_left(now=None):
    '''今天还可以因为按演员补全抓几部'''
    return max(0, daily_limit() - daily_used(now))


def _remember(name, text, at):
    '''job_state 里 name 这一行记成 text 和 at，还没有就新建'''
    if not db.JobState.update(text=text, at=at).where(db.JobState.name == name).execute():
        db.JobState.create(name=name, text=text, at=at)


def _drop_waiting(now, everything=True):
    '''
    被拦截以后，登记了、还没做的都不做了：点过"补全"还没翻她演员页的，点过"查看"还没读她第 1 页的，点过"查找"还没查的。
    整个网站拦着，换一位接着做只会再被拦。第二天也不自己去做（用户点了才抓），那一行写明原因，要继续请再点一次。
    everything 为假时只管等着翻的：点"补全"会解除暂停，所以暂停着、还等着翻的一定是以前留下的；点"查看""查找"不会解除暂停，
    暂停以后才点的是用户要做的，不能丢。返回不做了的位数
    '''
    with db.db.atomic(WRITE_LOCK):
        walks = (db.ListSource.update(wanted=0, checking=False, error=DROPPED, error_at=now)
                 .where(db.ListSource.wanted > 0).execute())
        views = searches = 0
        if everything:
            views = (db.ListSource.update(checking=False, error=DROPPED_VIEW, error_at=now)
                     .where(db.ListSource.checking).execute())
            searches = (db.StarLink.update(state=db.LINK_ERROR, error=DROPPED_SEARCH, checked_at=now)
                        .where(db.StarLink.state == db.LINK_PENDING).execute())
            _remember(TIDIED, '', now)
    if walks:
        logger.info(f'被网站拦截，还没翻演员页的 {walks} 位不再自动翻，要继续请再点一次补全')
    if views or searches:
        waiting = '、'.join(text for text in (f'等着查看的 {views} 位' if views else '',
                                            f'等着查找的 {searches} 位' if searches else '') if text)
        logger.info(f'被网站拦截，{waiting}也不再自动做，要继续请再点一次')
    return walks + views + searches


def _tidied_since(at):
    '''暂停（记在 at）以后，登记了没做的丢过一遍没有：没有，就是旧版本记的暂停，它被拦截时只丢等着翻的'''
    mark = db.JobState.get_or_none(db.JobState.name == TIDIED)
    return mark is not None and mark.at is not None and mark.at >= at


def _tidy_what_an_earlier_version_left():
    '''
    今天的暂停是旧版本记的（没有"丢过了"的记号，见 _tidied_since）：登记了、没做的查看和查找是它留下的，现在就丢掉并补上记号，
    用户接下来点的才算他自己的。要在登记这一次点击的同一个写事务里调：第一轮抓排队的影片开始之前点的，才不会和遗留一起被丢掉
    '''
    stop = paused()
    if stop is not None and not _tidied_since(stop[1]):
        _drop_waiting(get_now_time())


def pause(reason, now=None):
    '''
    被网站拦截了：今天不再自动抓按演员补全的，登记了、还没做的补全、查看、查找也都不做了（排队的影片明天自动接着抓）。
    原因写进库里，重启以后也还在
    '''
    global _unrecorded
    now = now or get_now_time()
    reason = hide_actress(hide_credentials(reason))
    with db.db.atomic(WRITE_LOCK):
        _remember(PAUSE, reason, now)
        _drop_waiting(now)
    _unrecorded = None
    logger.warning(f'按演员补全今天先停下（被网站拦截），排队的影片明天自动接着抓，也可以在网页上点"现在继续"：{reason}')


def record_block(reason):
    '''
    被网站拦截了：记下今天的暂停（见 pause）。数据库正被占着、记不下来时先留在内存里，下一次抓排队的影片开始时
    先补记，记下之前不对网站发任何请求；数据库忙不会抛出来
    '''
    global _unrecorded
    now = get_now_time()
    try:
        pause(reason, now)
    except OperationalError as ex:
        if not db.is_busy(ex):
            raise
        _unrecorded = (reason, now)
        logger.warning('被网站拦截，数据库正被占着、没能记下今天的暂停：下一次开始前先记，记下之前不再对网站发请求')


def _record_unrecorded_block():
    '''上一次被拦截时没能记下的暂停，先记下来。隔了一天的就不记了：暂停只管当天'''
    global _unrecorded
    if _unrecorded is None:
        return
    reason, at = _unrecorded
    if _day_of(at) == _day_of(get_now_time()):
        pause(reason, at)
    _unrecorded = None


def paused(now=None):
    '''今天是不是因为被拦截而暂停了：是的话返回 (原因, 时间)，不是返回 None。暂停只管当天，第二天自动解除'''
    row = db.JobState.get_or_none(db.JobState.name == PAUSE)
    if row is None or row.at is None:
        return None
    start, end = _day_of(now or get_now_time())
    if not start <= row.at < end:
        return None
    return row.text, row.at


def resume():
    '''
    用户点了"继续"（或者又点了补全）：解除今天的暂停，没能记下的拦截也不算了。暂停是旧版本记的话，它留下的登记了没做的
    查看和查找要在解除之前丢掉（见 _tidy_what_an_earlier_version_left）：暂停一解除，第一轮就认不出它们是遗留了
    '''
    global _unrecorded
    _unrecorded = None
    with db.db.atomic(WRITE_LOCK):
        _tidy_what_an_earlier_version_left()
        db.JobState.delete().where(db.JobState.name == PAUSE).execute()


def _source_for(path):
    if not is_star_page(path):
        raise ValueError(f'不是演员页的路径：{path!r}')
    source, _ = db.ListSource.get_or_create(path=path, defaults={'kind': 'star'})
    return source


def request_check(path):
    '''「查看」：登记读这位演员页的第 1 页（后台去读，只有 1 次请求），返回这个列表页的一行'''
    with db.db.atomic(WRITE_LOCK):
        _tidy_what_an_earlier_version_left()
        source = _source_for(path)
        db.ListSource.update(checking=True).where(db.ListSource.id == source.id).execute()
    return source


def queued_count(source):
    return (db.FetchTask.select()
            .where((db.FetchTask.source == source) & (db.FetchTask.status == db.TASK_QUEUED))
            .count())


def request_fill(path, count=None):
    '''
    「补全 / 继续补全」：登记这一次要往队列里排几部（默认 batch_size()），后台去翻她的演员页。
    接受了返回 None；她还有在排队的、或者上一次还没排完时不接受，返回原因（一位同时只排一批）。
    这是用户点的，被拦截以后的暂停也就解除了
    '''
    with db.db.atomic(WRITE_LOCK):
        source = _source_for(path)
        if source.wanted > 0:
            return STILL_LOOKING
        waiting = queued_count(source)
        if waiting:
            return f'她还有 {waiting} 部在排队'
        _tidy_what_an_earlier_version_left()
        (db.ListSource.update(wanted=count or batch_size(), requested_at=get_now_time(), error='', error_at=None)
         .where(db.ListSource.id == source.id).execute())
        _remember(f'{OLDEST}{source.id}', source.oldest.isoformat() if source.oldest else '', get_now_time())
        resume()
    return None


def cancel(path):
    '''
    取消这位还没抓的排队，返回取消的部数。"往回补到哪里"退回到取消的那些里最早的一页（经过的最早一部的日期退回到这一批开始之前的，
    前面几批经过的日期留着），下次继续补全会重新排它们；还没开始翻的那一次也不翻了。后台正好在读她的演员页的话，requested_at 清空就是告诉它这一次补全取消了：
    那一页回来以后它什么都不排，也不再改"往回补到哪里"
    '''
    source = db.ListSource.get_or_none(db.ListSource.path == path)
    if source is None:
        return 0
    mine = (db.FetchTask.source == source) & (db.FetchTask.status == db.TASK_QUEUED)
    with db.db.atomic(WRITE_LOCK):
        pages = [page for (page,) in db.FetchTask.select(db.FetchTask.page).where(mine).tuples() if page]
        removed = db.FetchTask.delete().where(mine).execute()
        values = {db.ListSource.wanted: 0, db.ListSource.requested_at: None}
        if pages:
            source = db.ListSource.get_by_id(source.id)
            values[db.ListSource.next_page] = min(pages + ([source.next_page] if source.next_page else []))
            values[db.ListSource.reached_end] = False
            # 经过的最早一部的日期里有取消掉的那些：退回这一批开始之前的，下次补全走到哪里再记。
            # 没有记着的（旧版本排的一批）就清掉
            mark = db.JobState.get_or_none(db.JobState.name == f'{OLDEST}{source.id}')
            values[db.ListSource.oldest] = date.fromisoformat(mark.text) if mark is not None and mark.text else None
        db.ListSource.update(values).where(db.ListSource.id == source.id).execute()
    return removed


def retry_source(path):
    '''这位演员失败的影片重新排队，返回排回去的部数'''
    source = db.ListSource.get_or_none(db.ListSource.path == path)
    if source is None:
        return 0
    with db.db.atomic(WRITE_LOCK):
        return (db.FetchTask.update(status=db.TASK_QUEUED, detail='', tries=0)
                .where((db.FetchTask.source == source) & (db.FetchTask.status == db.TASK_FAILED))
                .execute())


def request_search(key, name):
    '''
    「查找她的 JavBus 页面」：登记用 name 去 JavBus 按名字找演员（后台去找，只有 1 次请求）。key 是这个人
    （db.StarLink 的 key），再查一次时从头来过
    '''
    with db.db.atomic(WRITE_LOCK):
        _tidy_what_an_earlier_version_left()
        link, created = db.StarLink.get_or_create(key=key, defaults={'name': name})
        if not created:
            (db.StarLink.update(name=name, state=db.LINK_PENDING, candidates='[]', path='', error='')
             .where(db.StarLink.id == link.id).execute())
    return link


def choose_page(key, path):
    '''
    用户从查找的结果里点了一位：path 必须是结果里的一个，采用了返回 True。key 是一位 av-wiki 女优的，
    这个演员页同时记成她的 JavBus 演员页（见 actresses.accept_javbus：演员页上的名字是她的一个名字时正式名字可能换成它）
    '''
    with db.db.atomic(WRITE_LOCK):
        link = db.StarLink.get_or_none(db.StarLink.key == key)
        names = {} if link is None else {candidate[0]: candidate[1] for candidate in link.candidate_list}
        if path not in names:
            return False
        db.StarLink.update(state=db.LINK_FOUND, path=path, error='').where(db.StarLink.id == link.id).execute()
        actresses.accept_javbus(key, path, names[path])
    return True


def _keep_queue(path):
    '''这个演员页还关联着别人（别的女优、按名字查找到它的人）：取消某一位的关联时它的排队留着'''
    return (db.Actress.select().where(db.Actress.javbus == path).exists()
            or db.StarLink.select().where((db.StarLink.path == path) & (db.StarLink.state == db.LINK_FOUND)).exists())


def unlink(key, path):
    '''
    「取消关联」：key 是一位 av-wiki 女优（avwiki:<女优页>），JavBus 演员页 path 不是她。记下来，清掉她的演员页和查找记录，
    正式名字按规则重新定（见 actresses.unlink_javbus）；这个演员页还没抓的排队不抓了（还关联着别人时留着）。
    返回不抓了的部数
    '''
    with db.db.atomic(WRITE_LOCK):
        actresses.unlink_javbus(key[len('avwiki:'):], path)
        db.StarLink.delete().where(db.StarLink.key == key).execute()
        return 0 if _keep_queue(path) else cancel(path)


def change(key, path, name):
    '''
    「换一个」：和取消关联一样记下"不是她"、正式名字按规则重新定，再让用户从上次的查找结果里另点一位；
    没有上次的结果时重新查找（后台 1 次请求），用她现在的正式名字查（库里还没有她时用 name）。
    返回 (重新查找用的名字，不用重新查找时是 None, 这个演员页不抓了的部数)
    '''
    page = key[len('avwiki:'):]
    with db.db.atomic(WRITE_LOCK):
        actresses.unlink_javbus(page, path)
        link = db.StarLink.get_or_none(db.StarLink.key == key)
        searching = None
        if link is not None and link.candidate_list:
            db.StarLink.update(state=db.LINK_CHOOSE, path='', error='').where(db.StarLink.id == link.id).execute()
        else:
            searching = actresses.search_name_of(page) or name
            request_search(key, searching)
        removed = 0 if _keep_queue(path) else cancel(path)
    return searching, removed


def _search(site, link, result):
    '''按名字找演员：名字相同的正好一位就采用，有别的结果就列出来等用户选，没有结果记下没找到'''
    global activity
    activity = ('search', link.id, None)
    values = {db.StarLink.checked_at: get_now_time(), db.StarLink.error: ''}
    try:
        html = site.fetcher.get_html(searchstar_path(link.name))
    except BlockedError as ex:
        result.blocked = str(ex)
        values.update({db.StarLink.state: db.LINK_ERROR, db.StarLink.error: f'被网站拦截：{ex}'})
        db.StarLink.update(values).where(db.StarLink.id == link.id).execute()
        raise _Stop('被网站拦截') from ex
    except NotFoundError:
        site.answered()
        candidates = []
    except FetchError as ex:
        values.update({db.StarLink.state: db.LINK_ERROR, db.StarLink.error: str(ex)})
        db.StarLink.update(values).where(db.StarLink.id == link.id).execute()
        logger.warning(f'查找演员页 #{link.id} 出错：{ex}')
        site.unreachable()
        return
    else:
        site.answered()
        candidates = parse_star_search(html)[:MAX_CANDIDATES]
    # 用户说过不是她的演员页不自动采用（还列出来，用户可以再点它）
    refused = actresses.rejected_pages(link.key[len('avwiki:'):]) if link.key.startswith('avwiki:') else set()
    same = [(path, name) for path, name in candidates if db.fold(name) == db.fold(link.name) and path not in refused]
    if len(same) == 1:
        values.update({db.StarLink.state: db.LINK_FOUND, db.StarLink.path: same[0][0]})
        outcome = '名字相同的正好一位，已采用'
    elif candidates:
        values[db.StarLink.state] = db.LINK_CHOOSE
        outcome = f'名字相同的有 {len(same)} 位，等用户选'
    else:
        values[db.StarLink.state] = db.LINK_NONE
        outcome = '没有找到'
    values[db.StarLink.candidates] = json.dumps([list(candidate) for candidate in candidates], ensure_ascii=False)
    with db.db.atomic(WRITE_LOCK):
        db.StarLink.update(values).where(db.StarLink.id == link.id).execute()
        if len(same) == 1:
            actresses.found_javbus(link.key, *same[0])
    logger.info(f'查找演员页 #{link.id}：找到 {len(candidates)} 位，{outcome}')


def enqueue_codes(fanhaos, origin):
    '''
    把番号排进抓取队列（上传番号、导入打标库时库里还没有的那些），返回排进去的个数。
    番号会拼进网址，不合法的不排；已经在排队的不重复排；以前没抓到的（失败、网站上没有）、以前抓到过后来又不在库里的，
    用户再提交一次就重新排
    '''
    wanted = {}
    for fanhao in fanhaos:
        fanhao = (fanhao or '').strip()
        if is_valid_fanhao(fanhao):
            wanted.setdefault('/' + fanhao, fanhao)
    added = 0
    with db.db.atomic(WRITE_LOCK):
        for paths in chunked(list(wanted), IN_BATCH):
            known = {task.path: task for task in db.FetchTask.select().where(db.FetchTask.path.in_(paths))}
            again = [known[path].id for path in paths if path in known and known[path].status != db.TASK_QUEUED]
            if again:
                (db.FetchTask.update(status=db.TASK_QUEUED, origin=origin, source=None, page=None, detail='', tries=0,
                                     added_at=get_now_time())
                 .where(db.FetchTask.id.in_(again)).execute())
            new = [{'path': path, 'fanhao': wanted[path], 'origin': origin, 'added_at': get_now_time()}
                   for path in paths if path not in known]
            if new:
                db.FetchTask.insert_many(new).execute()
            added += len(again) + len(new)
    return added


def _queued(origins):
    return (db.FetchTask.status == db.TASK_QUEUED) & db.FetchTask.origin.in_(origins)


def _count(origins):
    return db.FetchTask.select().where(_queued(origins)).count()


def waiting():
    '''还在排队的：(按演员补全的部数, 上传番号和导入打标库的部数)'''
    return _count((db.ORIGIN_LIST,)), _count(CODE_ORIGINS)


def _next_task(origins, after_id):
    '''排在 after_id 后面、来处是 origins 之一的下一部；没有了返回 None'''
    return (db.FetchTask.select()
            .where(_queued(origins) & (db.FetchTask.id > after_id))
            .order_by(db.FetchTask.id)
            .first())


def _settle(task, status, detail=None, tries=None, tried_at=None):
    '''记下这一部这一次的结果。这一行已经被删掉（取消排队、清除记录）时什么也不改'''
    values = {db.FetchTask.status: status}
    if detail is not None:
        values[db.FetchTask.detail] = detail
    if tries is not None:
        values[db.FetchTask.tries] = tries
    if tried_at is not None:
        values[db.FetchTask.tried_at] = tried_at
    db.FetchTask.update(values).where(db.FetchTask.id == task.id).execute()


def _is_detail_path(path):
    return path.startswith('/') and is_valid_fanhao(path[1:])


def _after(site, task):
    '''上传番号、导入打标库的番号：和以前一样，再查一次同番号的其他条目（失败只记日志）'''
    if task.origin in CODE_ORIGINS:
        bus_spider._after_fanhao(site.fetcher, task.fanhao)


def _still_queued(task):
    '''这一部还在队里等着抓（取消排队会把它删掉）'''
    return (db.FetchTask.select()
            .where((db.FetchTask.id == task.id) & (db.FetchTask.status == db.TASK_QUEUED)).exists())


def _fetch(site, task, result):
    '''抓一部并入库，结果记进队列。被拦截、连续连不上网站时抛 _Stop，这一部留在队里'''
    global activity
    if not _is_detail_path(task.path):
        _settle(task, db.TASK_FAILED, '番号不合法')
        result.failed += 1
        logger.warning(f'{task.fanhao} 抓取失败：番号不合法')
        return
    if bus_spider._in_library(task.fanhao, task.path):
        _settle(task, db.TASK_DONE)
        result.skipped += 1
        logger.debug(f'{task.fanhao} 已在库里，跳过')
        _after(site, task)
        return
    activity = ('movie', task.source_id, task.fanhao)
    now = get_now_time()
    result.fetched += 1
    charged = []

    def proceed():
        '''
        等完间隔、发出去之前问一次：等间隔的几秒里用户可能取消了排队，这一行不在了就不发；要发的话先记下今天又抓了一部。
        发出去了的一定算，没发出去的不算；重试再发请求时不重复记
        '''
        if not _still_queued(task):
            return False
        if not charged:
            _spend(task, now)
            charged.append(True)
        return True

    try:
        meta, tags = bus_spider.fetch_detail(site.fetcher, task.path, task.fanhao, proceed=proceed)
    except RequestCancelled:
        result.fetched -= 1
        return
    except BlockedError as ex:
        # 先记下被拦截：下面写库没有做成（数据库正忙）时，这一次的拦截也不会丢
        result.blocked = str(ex)
        _settle(task, db.TASK_QUEUED, tried_at=now)
        logger.warning(f'抓排队的影片被网站拦截，先停下：{ex}')
        raise _Stop('被网站拦截') from ex
    except NotFoundError as ex:
        site.answered()
        _settle(task, db.TASK_MISSING, str(ex), tried_at=now)
        result.missing += 1
        logger.warning(f'{task.fanhao} 网站上没有：{ex}')
        return
    except ParseError as ex:
        site.answered()
        _settle(task, db.TASK_FAILED, str(ex), tried_at=now)
        result.failed += 1
        logger.warning(f'{task.fanhao} 抓取失败：{ex}')
        return
    except FetchError as ex:
        tries = task.tries + 1
        if tries >= MAX_TRIES:
            _settle(task, db.TASK_FAILED, str(ex), tries=tries, tried_at=now)
            result.failed += 1
            logger.warning(f'{task.fanhao} 抓取失败（试了 {tries} 次）：{ex}')
        else:
            _settle(task, db.TASK_QUEUED, str(ex), tries=tries, tried_at=now)
            result.retry_later += 1
            logger.warning(f'{task.fanhao} 这次没抓到，下次再试：{ex}')
        site.unreachable()
        return
    site.answered()
    fanhao = meta['fanhao']
    if db.save(meta, tags) is None:
        result.skipped += 1
        logger.debug(f'{fanhao} 已在库里，跳过')
    else:
        result.added += 1
        logger.info(f'新增 {fanhao}')
    _settle(task, db.TASK_DONE, '', tried_at=now)
    _after(site, task)


def _fetch_codes(site, result):
    last_id = 0
    while True:
        task = _next_task(CODE_ORIGINS, last_id)
        if task is None:
            return
        last_id = task.id
        _fetch(site, task, result)


def _fetch_listed(site, result):
    '''按演员补全排进来的，按排进来的先后抓，到了今天的额度就停'''
    last_id = 0
    while daily_left() > 0:
        task = _next_task((db.ORIGIN_LIST,), last_id)
        if task is None:
            return
        last_id = task.id
        _fetch(site, task, result)


def _set_error(source, text):
    (db.ListSource.update(error=text, error_at=get_now_time()).where(db.ListSource.id == source.id).execute())
    logger.warning(f'按演员补全：演员页 #{source.id} {text}')


class _PageGone(Exception):
    '''列表的这一页不存在（404）'''


def _read_page(site, source, page, result, proceed=None):
    '''
    读列表的一页，返回 html；读不到时记下原因、返回 None；被拦截、连续连不上网站时抛 _Stop；这一页不存在时抛 _PageGone。
    proceed 是等完间隔、发请求之前再问一次的函数（见 Fetcher.get）：说不发时也返回 None，什么都不记
    '''
    global activity
    activity = ('page', source.id, page)
    try:
        if proceed is None:
            html = site.fetcher.get_html(list_page_path(source.path, page))
        else:
            html = site.fetcher.get_html(list_page_path(source.path, page), proceed=proceed)
    except RequestCancelled:
        return None
    except BlockedError as ex:
        result.blocked = str(ex)
        _set_error(source, f'第 {page} 页被网站拦截：{ex}')
        raise _Stop('被网站拦截') from ex
    except NotFoundError as ex:
        site.answered()
        result.pages += 1
        raise _PageGone(str(ex)) from ex
    except FetchError as ex:
        _set_error(source, f'第 {page} 页读不到：{ex}')
        site.unreachable()
        return None
    site.answered()
    result.pages += 1
    return html


def _first_page(source, html):
    '''
    第 1 页上的两个数记下来。页面上没有影片，又说不出她没有有磁力的影片（没有"已有磁力"的数字，或者数字大于 0）时
    记下原因，返回 False
    '''
    magnets, total = parse_list_counts(html)
    boxes = parse_list_entries(html)
    if magnets is None and not boxes:
        _set_error(source, f'第 1 页{NO_MOVIES}')
        return False
    (db.ListSource.update(magnets=magnets, total=total, checked_at=get_now_time(), checking=False, error='',
                          error_at=None)
     .where(db.ListSource.id == source.id).execute())
    if magnets is None:
        logger.info(f'按演员补全：演员页 #{source.id} 第 1 页上没有找到"已有磁力"的数字')
    else:
        logger.info(f'按演员补全：演员页 #{source.id} 有磁力 {magnets} 部，全部 {total} 部')
    if magnets and not boxes:
        _set_error(source, f'第 1 页{NO_BOXES.format(magnets)}')
        return False
    return True


def ensure_writable():
    '''
    先拿一次写锁再放开。数据库被别的程序占着时，读了演员页也记不下来，白读一次，所以这时先不去请求：
    和别处的写一样，等到超时还拿不到就抛"库忙"
    '''
    with db.db.atomic(WRITE_LOCK):
        pass


def _check(site, source, result):
    '''「查看」：只读第 1 页。数据库被别的程序占着、这一次没做完时，登记的查看留着，下一轮接着做'''
    kept = False
    try:
        try:
            ensure_writable()
            html = _read_page(site, source, 1, result)
            if html is not None:
                _first_page(source, html)
        except _PageGone as ex:
            _set_error(source, f'第 1 页读不到：{ex}')
    except OperationalError as ex:
        kept = db.is_busy(ex)
        raise
    finally:
        if not kept:
            db.ListSource.update(checking=False).where(db.ListSource.id == source.id).execute()


def _known(path):
    '''库里已经有（番号或者详情地址），或者已经在队列里（不管抓没抓到）'''
    return (bus_spider._in_library(fanhao_from_path(path), path)
            or db.FetchTask.select().where(db.FetchTask.path == path).exists())


def _take(source, request, page, entries, more, going_back):
    '''
    这一页上库里没有、队列里也没有的，排进队列，最多排到她这一次还要排的部数（以库里记着的为准）；排满了就不再往后看，
    没看到的留给下一次。同一个事务里记下往回补的进度：下次从第几页读起（整页都看完了是下一页，没看完是这一页）、
    经过的最早一部的发行日期，是她的最后一页（more 为假）时再记下翻到底了；"继续补全"先看新片的那几页不记（going_back 为假）。
    返回 (排进去的部数, 认识的部数, 还要排几部, 是不是她的最后一页)。
    request 是这一次补全登记的时间：读这一页期间用户取消了（也许又点了一次补全）时它已经变了，什么都不改，返回 None
    '''
    picked, known, seen = [], 0, []
    now = get_now_time()
    with db.db.atomic(WRITE_LOCK):
        current = db.ListSource.get_by_id(source.id)
        if current.requested_at != request:
            return None
        room = current.wanted
        for path, released in entries:
            if len(picked) >= room:
                break
            seen.append((path, released))
            if _known(path):
                known += 1
            else:
                picked.append({'path': path, 'fanhao': fanhao_from_path(path), 'origin': db.ORIGIN_LIST,
                               'source': source.id, 'page': page, 'added_at': now})
        if picked:
            db.FetchTask.insert_many(picked).execute()
        finished = len(seen) == len(entries)
        last = finished and not more
        values = {db.ListSource.wanted: room - len(picked)}
        if going_back or last:
            values[db.ListSource.next_page] = page + 1 if finished else page
            dates = [released for _, released in seen if released]
            if dates:
                earliest = date.fromisoformat(min(dates))
                values[db.ListSource.oldest] = min(earliest, current.oldest) if current.oldest else earliest
        if last:
            values[db.ListSource.reached_end] = True
        db.ListSource.update(values).where(db.ListSource.id == source.id).execute()
    return len(picked), known, room - len(picked), last


def _mark_end(source, request):
    '''她的列表翻到底了。这一次补全在读页期间被取消的（"往回补到哪里"已经退回去了）不改'''
    (db.ListSource.update(reached_end=True)
     .where((db.ListSource.id == source.id) & (db.ListSource.requested_at == request)).execute())


def _still_requested(source, request):
    '''这一次补全（登记的时间是 request）还在：没有被取消（取消会清掉 requested_at），也没有取消后又点了新的一次'''
    current = db.ListSource.get_or_none(db.ListSource.id == source.id)
    return current is not None and current.wanted > 0 and current.requested_at == request


def _walk(site, source, result):
    '''
    「补全」：从第 1 页往后翻，把库里没有的排进队列，最多排到这一次登记的部数。补过的先看前面有没有新片（第 1 页
    一部都不认识时接着看下一页），遇到认识的就跳到上次停下的那一页接着往回补；已经翻到最后一页的只看新片。
    读一页要几秒，用户可能在这期间点了取消：回来以后看库里这一次补全还在不在（见 cancel），不在了就不再排、不再改进度。
    数据库被别的程序占着、这一次没做完时，用户点的这一次补全留着（wanted 还是还要排的部数），下一轮接着做：已经排进去的
    认得、不重复排，所以不会超过一次最多排的部数；多读一次列表页而已
    '''
    source = db.ListSource.get_by_id(source.id)
    request = source.requested_at
    resume = source.next_page if source.next_page > 1 else 0
    going_back = not resume
    page, taken = 1, 0
    kept = False
    try:
        ensure_writable()
        while True:
            if page > MAX_PAGES:
                _set_error(source, f'翻了 {MAX_PAGES} 页还没有到最后一页，先停下（页面结构可能变了）')
                break
            current = db.ListSource.get_by_id(source.id)
            if current.wanted <= 0 or current.requested_at != request:
                break
            try:
                # 等间隔的几秒里用户可能取消了：等完以后、发出去之前再看一眼这一次补全还在不在
                html = _read_page(site, source, page, result, proceed=lambda: _still_requested(source, request))
            except _PageGone as ex:
                if page == 1:
                    _set_error(source, f'第 1 页读不到：{ex}')
                else:
                    _mark_end(source, request)
                break
            if html is None or (page == 1 and not _first_page(source, html)):
                break
            entries = parse_list_entries(html)
            if not entries and page > 1:
                magnets, _ = parse_list_counts(html)
                if magnets:
                    _set_error(source, f'第 {page} 页{NO_BOXES.format(magnets)}')
                else:
                    _mark_end(source, request)
                break
            step = _take(source, request, page, entries, has_next_page(html), going_back)
            if step is None:
                break
            picked, known, left, last = step
            taken += picked
            if last or left <= 0:
                break
            if not going_back and (known or page + 1 >= resume):
                if current.reached_end:
                    break
                going_back = True
                page = max(page + 1, resume)
                continue
            page += 1
    except OperationalError as ex:
        kept = db.is_busy(ex)
        raise
    finally:
        result.queued += taken
        if kept:
            logger.info(f'按演员补全：演员页 #{source.id} 数据库正被占着，这一次先排进 {taken} 部，'
                        '登记的补全留着，下一轮接着做')
        else:
            # 这一次补全被取消了（也许又点了一次）就不动：wanted 已经是取消时清的，新的一次留给下一轮
            (db.ListSource.update(wanted=0, checking=False)
             .where((db.ListSource.id == source.id) & (db.ListSource.requested_at == request)).execute())
            done = db.ListSource.get_by_id(source.id)
            where = '已经翻到最后一页' if done.reached_end else f'下次从第 {max(1, done.next_page)} 页接着往回补'
            logger.info(f'按演员补全：演员页 #{source.id} 这一次排进 {taken} 部，{where}')


def _start_text(codes, listed, left_today, held):
    parts = []
    if codes:
        parts.append(f'上传番号和导入打标库的 {codes} 部')
    if listed:
        if held == 'paused':
            parts.append(f'按演员补全的 {listed} 部（今天暂停）')
        else:
            parts.append(f'按演员补全的 {listed} 部（今天还可以抓 {left_today} 部）')
    return '，'.join(parts)


def _summary(result):
    '''一次的结果写成一句话（日志用，不含开头的"抓排队的影片结束："）'''
    text = f'抓了 {result.fetched} 部，新增 {result.added} 部，网站上没有 {result.missing} 部，失败 {result.failed} 部'
    if result.left:
        text += f'，还有 {result.left} 部在排队'
    if result.stopped:
        text += f'（{result.stopped}）'
    elif result.held == 'limit':
        text += '（今天按演员补全的额度用完了，明天接着抓）'
    elif result.held == 'paused':
        text += '（今天被网站拦截过，按演员补全的明天接着抓）'
    return text


def held_reason():
    '''还有按演员补全的要做、今天却不接着做的原因：'paused' 被拦截暂停、'limit' 今天的额度用完，或者空'''
    waiting = _count((db.ORIGIN_LIST,)) or db.ListSource.select().where(db.ListSource.wanted > 0).exists()
    if not waiting:
        return ''
    if paused():
        return 'paused'
    if _count((db.ORIGIN_LIST,)) and daily_left() <= 0:
        return 'limit'
    return ''


def _work(site, result):
    '''先查找演员页、再查看、再补全（翻列表页、排队），再抓上传番号和导入的，最后抓按演员补全的'''
    # 每一类先整个取出来再一个一个做：一边逐行取、一边写库的话，这条连接的读快照停在取之前，读一页要几秒，
    # 网页上在这期间提交了一次写（点"喜欢"、取消排队……）以后，这里的下一次写就会立刻被数据库拒绝
    for link in list(db.StarLink.select().where(db.StarLink.state == db.LINK_PENDING).order_by(db.StarLink.id)):
        _search(site, link, result)
    for source in list(db.ListSource.select().where(db.ListSource.checking & (db.ListSource.wanted <= 0))
                       .order_by(db.ListSource.id)):
        _check(site, source, result)
    if not paused():
        for source in list(db.ListSource.select().where(db.ListSource.wanted > 0)
                           .order_by(db.ListSource.requested_at, db.ListSource.id)):
            _walk(site, source, result)
    codes = _count(CODE_ORIGINS)
    listed = _count((db.ORIGIN_LIST,))
    hold = held_reason()
    if codes or (listed and not hold):
        logger.info(f'开始抓排队的影片：{_start_text(codes, listed, daily_left(), hold)}')
    _fetch_codes(site, result)
    if not paused():
        _fetch_listed(site, result)


def has_work():
    '''这一次有没有要发请求的事。查找、查看是用户点的一次请求，被拦截暂停的时候也照常做'''
    if (db.StarLink.select().where(db.StarLink.state == db.LINK_PENDING).exists()
            or db.ListSource.select().where(db.ListSource.checking).exists() or _count(CODE_ORIGINS)):
        return True
    if paused():
        return False
    return (db.ListSource.select().where(db.ListSource.wanted > 0).exists()
            or (_count((db.ORIGIN_LIST,)) > 0 and daily_left() > 0))


def _known_movies(source):
    '''她的影片里库里和队里已经有的番号：队列里她的每一行（不管抓没抓到），库里带她的演员页标签的影片'''
    queued = {fanhao for (fanhao,) in db.FetchTask.select(db.FetchTask.fanhao)
              .where(db.FetchTask.source == source.id).tuples()}
    in_library = {fanhao for (fanhao,) in db.ItemTag.select(db.ItemTag.item).join(db.Tag)
                  .where((db.Tag.type_ == 'star') & (db.Tag.url == source.path)).tuples()}
    return queued | in_library


def _reopen_false_ends():
    '''
    库里可能有这样的标记：没有影片框的页面被记成了"已经翻到最后一页"，以后只找新片，再也往回补不了。
    1. 第 1 页上没有影片框：她的影片一部都没有看见过，没排过队、没有经过的发行日期，页面上却写着有磁力的影片。
       去掉标记，下一次补全从头往回补。每次开始都查。
    2. 第 2 页起没有影片框：前面的页排过队了，认不出来；只能看页面上写的有磁力的部数比库里和队里已知的（_known_movies）多。
       只去掉"已经翻到最后一页"的标记，往回补到哪里留着，下一次补全先看第 1 页的新片，再从停下的那一页接着往回补。
       只在第一次开始时整理一次（记在 job_state）：这样的标记现在不会再产生；每次都按这个整理的话，"查看"发现了新片
       的演员页面上的数字也比已知的多，真的补到底的演员下一次补全就要多读好几页
    '''
    walked = db.FetchTask.select(db.FetchTask.source).where(db.FetchTask.source.is_null(False))
    false_ends = (db.ListSource.reached_end & db.ListSource.oldest.is_null() & (db.ListSource.magnets > 0)
                  & db.ListSource.id.not_in(walked))
    if db.ListSource.select().where(false_ends).exists():
        fixed = db.ListSource.update(reached_end=False, next_page=0).where(false_ends).execute()
        logger.info(f'修正了 {fixed} 位演员页的记录：第 1 页上没有影片框就被记成了"已经翻到最后一页"，下次补全从头往回补')
    if db.JobState.get_or_none(db.JobState.name == ENDS) is not None:
        return
    with db.db.atomic(WRITE_LOCK):
        ended = list(db.ListSource.select().where(db.ListSource.reached_end & (db.ListSource.magnets > 0)))
        behind = [source.id for source in ended if source.magnets > len(_known_movies(source))]
        if behind:
            db.ListSource.update(reached_end=False).where(db.ListSource.id.in_(behind)).execute()
        _remember(ENDS, '', get_now_time())
    if behind:
        logger.info(f'修正了 {len(behind)} 位演员页的记录：没有影片框的页面被记成了"已经翻到最后一页"，'
                    '页面上有磁力的部数却比库里和队里已知的多，下次补全接着往回补')


def run(fetcher=None):
    '''
    抓一次：先做用户点了的「查看」「补全」（读演员页、排进队列），再把排队的详情页一部一部地抓，每抓完一部就记进库里。
    返回 QueueRun，也记在 last_run 里。和定时抓取共用 bus_spider 的抓取锁：别的抓取正在进行时等它结束再开始
    '''
    global last_run, activity
    result = QueueRun()
    with bus_spider._lock:
        _record_unrecorded_block()
        _reopen_false_ends()
        stop = paused()
        if stop is not None:
            # 暂停着却还有登记了没做的：和刚被拦截时一样，不做，也不留到明天
            _drop_waiting(get_now_time(), everything=not _tidied_since(stop[1]))
        if not has_work():
            logger.debug('没有排队的影片')
        else:
            own = fetcher is None
            try:
                if own:
                    # 不重试：被限流（429）也要立刻停下；网络问题由"同一部最多试 MAX_TRIES 次"兜着
                    fetcher = create_fetcher(retries=0)
                try:
                    _work(_Site(fetcher), result)
                except _Stop as ex:
                    result.stopped = str(ex)
                finally:
                    # 被拦截了就一定记下暂停，哪怕这一次是因为数据库出错才没做完的
                    if result.blocked:
                        record_block(result.blocked)
            finally:
                activity = None
                if own and fetcher is not None:
                    fetcher.close()
        result.left = db.FetchTask.select().where(db.FetchTask.status == db.TASK_QUEUED).count()
        result.left_list = _count((db.ORIGIN_LIST,))
        result.held = held_reason()
    result.finished_at = get_now_time()
    busy = result.fetched or result.skipped or result.missing or result.failed or result.retry_later
    if busy:
        if result.stopped:
            logger.warning(f'抓排队的影片结束：{_summary(result)}')
        else:
            logger.info(f'抓排队的影片结束：{_summary(result)}')
    if busy or result.pages or last_run is None or not (last_run.pages or last_run.fetched or last_run.skipped):
        last_run = result
    return result


def _problems():
    return (db.FetchTask.status.in_([db.TASK_FAILED, db.TASK_MISSING])) & db.FetchTask.origin.in_(CODE_ORIGINS)


def code_queue():
    '''上传番号页上的"下载队列"：还在排队的个数，没抓到的（新排进来的在前），返回 CodeQueue'''
    waiting = _count(CODE_ORIGINS)
    query = db.FetchTask.select().where(_problems())
    problems = list(query.order_by(db.FetchTask.id.desc()).limit(MAX_PROBLEMS_SHOWN))
    more = query.count() - len(problems) if len(problems) == MAX_PROBLEMS_SHOWN else 0
    return CodeQueue(waiting, problems, more)


def retry_codes(fanhaos=None):
    '''上传番号、导入打标库里失败的番号重新排队（fanhaos 为空时是全部失败的），返回排回去的个数。网站上没有的不动'''
    where = (db.FetchTask.status == db.TASK_FAILED) & db.FetchTask.origin.in_(CODE_ORIGINS)
    if fanhaos is not None:
        where &= db.FetchTask.fanhao.in_(list(fanhaos))
    with db.db.atomic(WRITE_LOCK):
        return db.FetchTask.update(status=db.TASK_QUEUED, detail='', tries=0).where(where).execute()


def forget_problems():
    '''清除上传番号、导入打标库里没抓到的记录（失败的、网站上没有的），返回清除的条数'''
    with db.db.atomic(WRITE_LOCK):
        return db.FetchTask.delete().where(_problems()).execute()
