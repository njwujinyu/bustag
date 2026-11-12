'''
没有演员的影片，到 av-wiki 补演员名。在后台任务里一轮一轮地做，不在网页请求里联网。
每部影片：按番号打开搜索结果，卡片上就有每位女优的名字和女优页；用户喜欢的影片再打开女优页，读她的全部名字
（读音、罗马字、别名，每人只读一次）。一位女优在库里只有一条正式名字（star 标签），其他名字记成别名（见 actresses）。
试过的影片都记在库里（db.ActressLookup）：补到了；网站上没有，隔一段时间再试；网络失败，下一轮再试。
演员数据从旧的存法升级以后，按旧存法补过的影片（stale）在这里按新存法重新查一遍，校正迁移时定不准的地方。
请求要省：每轮最多处理 ROUND_LIMIT 部，每天的请求总数有上限（config.ini 的 [download] avwiki_daily），到了就停到第二天
'''
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import unquote, urlparse

from peewee import OperationalError, chunked

from bustag.spider import actresses, db, parser
from bustag.spider.fetcher import NETWORK_HINT, BlockedError, FetchError, NotFoundError, create_fetcher
from bustag.spider.parser import ParseError, avwiki_search_url, parse_avwiki_cards, parse_avwiki_names
from bustag.util import APP_CONFIG, get_now_time, hide_actress, hide_credentials, logger

# 一轮最多处理这么多部影片（和这么多个女优页），剩下的留到下一轮：一轮做太久会挡住排在后面的抓取和打分
ROUND_LIMIT = 30
# 一轮里连续这么多次请求连不上就停下，这一轮不再请求
MAX_FAILURES_IN_A_ROW = 3
# 网站上没有时隔多少天再试：第 1 次之后 1 天，第 2 次之后 7 天，以后每 30 天。新片的资料页往往晚些才有
MISSING_RETRY_DAYS = (1, 7, 30)
# 迁移后要按新存法重新查的影片搜不到时隔多少天再查：第 1 次之后 1 天，第 2 次之后 7 天；第 3 次还搜不到就保留
# 升级时整理的结果，不再查。一次搜不到可能只是网站一时出错，校正不到的地方就一直留着了
CORRECTION_RETRY_DAYS = (1, 7)
# 每个请求只试一次（没补到的影片下一轮再来），(连接, 读取) 超时
REQUEST_TIMEOUT = (10, 20)
# job_state 里记今天向 av-wiki 发了几次请求的那一行（text 是次数，at 是最近一次计数的时间）
REQUESTS = 'avwiki-requests'
# 数据库被别的程序占着、这一轮停下时记在结果里的原因（关于页上写"……就停下了"）
BUSY_STOP = '数据库正被别的程序占着'


@dataclass
class FillRound:
    '''
    一轮的结果。due 是这一轮开始时该处理的部数（没有演员的、迁移后要重新查的），left 是其中没来得及处理、
    留到下一轮的；filled 是补到了的部数，names 是这些影片挂上的女优数，corrected 是按新存法重新查过的部数；
    pages 是这一轮向女优页发的请求数（卡片上没有名字时读的、读到名字的、404 的、没有名字栏的、连不上的都算），
    requests 是这一轮向 av-wiki 发的全部请求数；unlabeled 是演员变了的影片里不是用户打标的部数
    （不是 0 时这一轮之后打一次分）
    '''
    finished_at: Optional[datetime] = None
    due: int = 0
    filled: int = 0
    names: int = 0
    missing: int = 0
    failed: int = 0
    left: int = 0
    corrected: int = 0
    pages: int = 0
    requests: int = 0
    unlabeled: int = 0
    stopped: str = ''   # 提前停下的原因，正常做完时为空
    error: str = ''     # 这一轮里最后一次失败："番号：原因"
    changed: set = field(default_factory=set, repr=False)


@dataclass
class FillStatus:
    '''页面上显示的状态。last_failure 是 (番号, 原因, 时间)'''
    waiting: int        # 没有演员的影片数
    liked: int          # 其中用户喜欢的
    recommended: int    # 其中在「推荐」里的
    due: int            # 其中下一轮就会去补的；其余的是网站上没有、还没到再试的时间
    filled: int         # 到现在一共补到了多少部
    stale: int          # 迁移后还要重新查的影片
    stale_later: int    # 其中上次没搜到、过几天再查的
    pages: int          # 迁移后还要读女优页的女优
    used: int           # 今天发了几次请求
    limit: int          # 每天最多几次
    last_round: Optional[FillRound]
    last_failure: Optional[tuple]


# 最近一轮处理过影片的结果；还没有哪一轮处理过影片时是最近一轮（没有要补的），程序启动后还没有跑过时是 None
last_round = None


def daily_limit():
    '''每天最多向 av-wiki 发多少次请求（config.ini 的 [download] avwiki_daily）'''
    return int(APP_CONFIG['download.avwiki_daily'])


def daily_used(now=None):
    '''今天（0 点起）向 av-wiki 发了几次请求。记在库里，重启以后也算数'''
    now = now or get_now_time()
    row = db.JobState.get_or_none(db.JobState.name == REQUESTS)
    if row is None or row.at is None or row.at.date() != now.date():
        return 0
    return int(row.text) if row.text.isdigit() else 0


def _count_request(now):
    used = str(daily_used(now) + 1)
    with db.db.atomic('IMMEDIATE'):
        if not db.JobState.update(text=used, at=now).where(db.JobState.name == REQUESTS).execute():
            db.JobState.create(name=REQUESTS, text=used, at=now)


class _Unreachable(Exception):
    '''请求没有成功（连不上、超时、服务器出错、被拦截），这部影片下一轮再试'''

    def __init__(self, error):
        # Fetcher 的报错末尾带着检查 root_path 的建议，那是给 JavBus 的，这里不要。
        # 原因要写进日志、库里和关于页：读的是女优页时，地址里她的名字不写
        super().__init__(hide_actress(str(error).replace(NETWORK_HINT, '').rstrip('。')))


class _NotOnSite(Exception):
    '''网站答复了，但上面没有这部影片的演员（或者没有这位女优的名字）'''


class _OverLimit(Exception):
    '''今天的请求数到了上限'''


class _Site:
    '''
    一轮里对 av-wiki 的全部请求：数着请求（也记进库里算今天的用量）、连续失败了几次、读过的女优页，
    该停的时候 stopped 是原因
    '''

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.failures_in_a_row = 0
        self.requests = 0
        self.page_requests = 0
        self.stopped = ''
        self.over_limit = False
        self.pages = {}      # 女优页路径（解码的写法）-> 名字的列表，或者上次读它时的异常
        self.searches = {}   # 搜索地址 -> 页面，或者那次搜索的异常

    def get(self, url, actress_page=False):
        now = get_now_time()
        if daily_used(now) >= daily_limit():
            self.stopped = f'今天向 av-wiki 的请求到了每天 {daily_limit()} 次的上限'
            self.over_limit = True
            raise _OverLimit()
        _count_request(now)
        self.requests += 1
        if actress_page:
            self.page_requests += 1
        try:
            html = self.fetcher.get_html(url)
        except NotFoundError:
            self.failures_in_a_row = 0
            raise
        except BlockedError as ex:
            self.stopped = '被 av-wiki 拦截'
            raise _Unreachable(ex) from ex
        except FetchError as ex:
            self.failures_in_a_row += 1
            if self.failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                self.stopped = f'连续 {self.failures_in_a_row} 次请求失败'
            raise _Unreachable(ex) from ex
        self.failures_in_a_row = 0
        return html

    def cards(self, fanhao):
        '''
        搜索结果卡片上的女优：[(女优页路径, 卡片上的名字)]，路径是存进库里的写法（actresses.page_path）。
        同一个搜索地址（同一个識別碼的几条影片，番号带不带发行日期）一轮里只请求一次，结果几条都用
        '''
        url = avwiki_search_url(fanhao)
        if url not in self.searches:
            try:
                self.searches[url] = self.get(url)
            except NotFoundError:
                self.searches[url] = _NotOnSite('av-wiki 上没有这个番号的页面')
            except _Unreachable as ex:
                self.searches[url] = ex
        html = self.searches[url]
        if isinstance(html, Exception):
            raise html
        cards = parse_avwiki_cards(html, fanhao)
        if not cards:
            raise _NotOnSite('av-wiki 的搜索结果里没有这个番号')
        found = {}
        for url, name in cards:
            found.setdefault(actresses.page_path(urlparse(url).path), name)
        return list(found.items())

    def names(self, page):
        '''女优页上的名字，按页面上的顺序。同一个女优页（地址转义不转义都算）一轮里只读一次，读不到的也不再读第二次'''
        key = unquote(page)
        if key not in self.pages:
            try:
                self.pages[key] = parse_avwiki_names(self.get(parser.AVWIKI_ORIGIN + page, actress_page=True))
            except (NotFoundError, ParseError) as ex:
                self.pages[key] = _NotOnSite(hide_actress(f'女优页没有读到名字：{ex}'))
            except _Unreachable as ex:
                self.pages[key] = ex
        found = self.pages[key]
        if isinstance(found, Exception):
            raise found
        return found


def _read(site, page, result, now):
    '''读这位女优的女优页、按页面上的名字定她的正式名字。读不到不影响别的，只是以后再读（网站上没有的不再读）'''
    try:
        names = site.names(page)
    except _NotOnSite as ex:
        actresses.page_missing(page, now)
        logger.info(f'女优页读不到名字，以后不再读：{ex}')
        return
    except _Unreachable as ex:
        logger.warning(f'女优页没有读到，以后再读：{ex}')
        return
    _, changed = actresses.read_page(page, names, now)
    result.changed |= changed


def _people(site, fanhao, now):
    '''
    这部影片上的女优（db.Actress，按卡片上的先后）。库里还没有的按卡片上的名字新建；卡片上没有名字的
    要读她的女优页才知道她叫什么
    '''
    people = []
    for page, name in site.cards(fanhao):
        actress = actresses.actress_on_page(page)
        if actress is None and name:
            actress = actresses.add_from_card(page, name)
        elif actress is None:
            actress, _ = actresses.read_page(page, site.names(page), now)
        people.append(actress)
    return people


def _look_up(site, fanhao, read_pages, replace, result, now):
    '''
    补一部影片（replace：按新存法重新查一部迁移前补过的）。返回挂上的女优数；已经不需要补时（补的过程中有了演员，
    或者不再是要校正的）返回 None。挂上以后再读她们的女优页（read_pages：用户喜欢的、要校正的影片）
    '''
    people = _people(site, fanhao, now)
    written = actresses.set_movie_actresses(fanhao, people, replace)
    if written is None:
        return None
    # 先记下补到了：读女优页时要知道这部影片的演员是补名写的（换正式名字时只改这样的影片）
    _record(fanhao, db.LOOKUP_FILLED, now)
    result.changed.add(fanhao)
    if read_pages:
        for actress in people:
            fresh = db.Actress.get_or_none(db.Actress.id == actress.id)
            if fresh is None or fresh.names_at is not None or site.stopped:
                continue
            try:
                _read(site, fresh.avwiki, result, now)
            except _OverLimit:
                break
    return written


def _record(fanhao, status, now, detail=''):
    '''记下结果，算出下一次什么时候再试。返回离下一次还有几天'''
    previous = db.ActressLookup.get_or_none(db.ActressLookup.item == fanhao)
    misses = previous.misses if previous is not None else 0
    days = 0
    if status == db.LOOKUP_FILLED:
        misses = 0
    elif status == db.LOOKUP_MISSING:
        misses += 1
        days = MISSING_RETRY_DAYS[min(misses, len(MISSING_RETRY_DAYS)) - 1]
    db.save_lookup(fanhao, status, misses, now, now + timedelta(days=days), detail)
    return days


def _not_found_again(fanhao, now):
    '''
    要按新存法重新查的影片这次没搜到：还没查够次数就记下几天后再查（返回天数，补名记录还是 stale，原因不记：
    这不算失败），查够了就保留升级时整理的演员、记成补到了（返回 0）
    '''
    row = db.ActressLookup.get_or_none(db.ActressLookup.item == fanhao)
    misses = (row.misses if row is not None else 0) + 1
    if misses > len(CORRECTION_RETRY_DAYS):
        _record(fanhao, db.LOOKUP_FILLED, now)
        return 0
    days = CORRECTION_RETRY_DAYS[misses - 1]
    db.save_lookup(fanhao, db.LOOKUP_STALE, misses, now, now + timedelta(days=days))
    return days


def _fill(site, movies, result, now):
    '''movies 是 [(番号, 'stale' | 'liked' | 'other')]'''
    for done, (fanhao, kind) in enumerate(movies):
        if site.stopped:
            result.left += len(movies) - done
            later = '剩下的明天接着补' if site.over_limit else '这一轮先停下，剩下的下一轮再试'
            logger.warning(f'补演员名：{site.stopped}，{later}')
            return
        stale = kind == 'stale'
        try:
            written = _look_up(site, fanhao, kind != 'other', stale, result, now)
        except _OverLimit:
            result.left += len(movies) - done
            logger.warning(f'补演员名：{site.stopped}，剩下的明天接着补')
            return
        except _NotOnSite as ex:
            if stale:
                days = _not_found_again(fanhao, now)
                if days:
                    result.missing += 1
                    logger.info(f'{fanhao} 这次在 av-wiki 上没有查到（{ex}），升级时整理的演员先留着，{days} 天后再查一次')
                else:
                    result.corrected += 1
                    logger.info(f'{fanhao} 在 av-wiki 上一直查不到（{ex}），保留升级时整理的演员')
                continue
            result.missing += 1
            days = _record(fanhao, db.LOOKUP_MISSING, now, str(ex))
            logger.info(f'{fanhao} 在 av-wiki 上没有找到演员（{ex}），{days} 天后再试')
            continue
        except _Unreachable as ex:
            reason = str(ex)
            logger.warning(f'{fanhao} 补演员名失败：{reason}')
        except Exception as ex:
            if isinstance(ex, OperationalError) and db.is_busy(ex):
                # 数据库被别的程序占着：结果写不进去（或者连这一次请求要记的数都记不进去，那就没有发请求）。不是这部影片的错，
                # 这一部和剩下的下一轮再来；已经发出去的请求，发之前就记进了今天的数里
                result.left += len(movies) - done
                site.stopped = BUSY_STOP
                logger.warning(f'补演员名：{db.busy_reason(ex)}，这一轮先停下，剩下的下一轮再试')
                return
            reason = hide_actress(hide_credentials(f'程序出错：{ex!r}'))
            logger.exception(f'{fanhao} 补演员名时程序出错')
        else:
            if written is not None:
                if stale:
                    result.corrected += 1
                    logger.info(f'{fanhao} 按新存法重新查了演员：{written} 位女优')
                else:
                    result.filled += 1
                    result.names += written
                    logger.info(f'{fanhao} 从 av-wiki 补了 {written} 位女优')
            continue
        result.failed += 1
        result.error = f'{fanhao}：{reason}'
        if stale:
            row = db.ActressLookup.get_or_none(db.ActressLookup.item == fanhao)
            db.save_lookup(fanhao, db.LOOKUP_STALE, row.misses if row else 0, now, now, reason)
        else:
            _record(fanhao, db.LOOKUP_FAILED, now, reason)


def _same_codes(site, movies, now):
    '''
    这一轮搜过的识别码（搜到了卡片，或者网站上没有这个番号）的其他影片：没有演员、已经到期、这一轮没排上的
    （同一个识别码的几条被每轮的部数上限分开了）。[(番号, 'liked' | 'other')]
    '''
    searched = {url for url, found in site.searches.items() if not isinstance(found, _Unreachable)}
    taken = {fanhao for fanhao, _ in movies}
    return [(fanhao, 'liked' if liked else 'other') for fanhao, liked in db.starless_due(now)
            if fanhao not in taken and avwiki_search_url(fanhao) in searched]


def _fill_same_codes(site, movies, result, now):
    '''
    这一轮没排上、同一个识别码搜过了的影片，用这一轮的搜索结果一起补上：不再请求搜索，不占这一轮的部数；
    喜欢的影片要读女优页时照常算进每天的请求数，连续失败、被拦截照常停下
    '''
    extra = _same_codes(site, movies, now)
    if not extra:
        return
    logger.info(f'同一个识别码的另外 {len(extra)} 部用这一轮的搜索结果一起补')
    result.left -= len(extra)
    _fill(site, extra, result, now)


def _read_pending(site, chosen, result, now):
    '''还要读的女优页（迁移后校正的、喜欢的影片上的、指定推荐里选了的），每页 1 次请求'''
    for actress in actresses.pages_to_read(chosen, ROUND_LIMIT):
        if site.stopped:
            return
        try:
            _read(site, actress.avwiki, result, now)
        except _OverLimit:
            logger.warning(f'补演员名：{site.stopped}，剩下的明天接着补')
            return


def _unlabeled(fanhaos):
    '''这些影片里不是用户打标的部数'''
    rated = set()
    for chunk in chunked(sorted(fanhaos), db.IN_BATCH):
        rated.update(fanhao for (fanhao,) in db.ItemRate.select(db.ItemRate.item).where(
            (db.ItemRate.rate_type == db.RATE_TYPE.USER_RATE) & db.ItemRate.item.in_(chunk)).tuples())
    return len(set(fanhaos) - rated)


def _keep(result):
    '''没有做事的一轮不顶掉以前做过事的那一轮：每次抓取之后都会补一轮，多半没有要补的，页面上要看的是上一次补得怎么样'''
    global last_round
    if last_round is None or not (last_round.due or last_round.pages or last_round.requests):
        last_round = result


def fill_round(fetcher=None, chosen=()):
    '''
    补一轮：先做迁移后要重新查的影片，再按先后补没有演员的影片（各取到 ROUND_LIMIT 部；同一个识别码这一轮搜过了的
    另外几部也用这次的结果补上，见 _fill_same_codes），最后读还要读的女优页（chosen 是指定推荐里选了的女优名字）。演员数据还没升级（迁移没做成）时什么都不做：新的存法不往旧库里写。
    两次请求之间的间隔由 Fetcher 按配置的 delay 控制。返回 FillRound，也记在 last_round 里
    '''
    global last_round
    now = get_now_time()
    result = FillRound(finished_at=now)
    if not actresses.migrated():
        result.stopped = '演员数据还没有升级（见启动时的日志），升级做完以后才补演员名'
        logger.debug(f'补演员名：{result.stopped}')
        return result
    result.changed |= actresses.reconcile()
    stale = db.stale_due(now, ROUND_LIMIT)
    movies = [(fanhao, 'stale') for fanhao in stale]
    movies += [(fanhao, 'liked' if liked else 'other')
               for fanhao, liked in db.starless_due(now, ROUND_LIMIT - len(stale))]
    result.due = db.count_stale_due(now) + db.count_starless(now)[1]
    result.left = result.due - len(movies)
    pending = actresses.pages_to_read(chosen, 1)
    if not movies and not pending:
        result.unlabeled = _unlabeled(result.changed)
        logger.debug('补演员名：没有到期要补的影片')
        _keep(result)
        return result
    if movies:
        corrected = f'（其中按新存法重新查的 {len(stale)} 部）' if stale else ''
        logger.info(f'开始补演员名：要补的有 {result.due} 部，这一轮处理 {len(movies)} 部{corrected}')
    own = fetcher is None
    site = None
    try:
        if own:
            fetcher = create_fetcher(retries=0, timeout=REQUEST_TIMEOUT)
        site = _Site(fetcher)
        _fill(site, movies, result, now)
        if result.left and not site.stopped:
            _fill_same_codes(site, movies, result, now)
        if not site.stopped:
            _read_pending(site, chosen, result, now)
    finally:
        if own and fetcher is not None:
            fetcher.close()
    result.stopped = site.stopped
    result.requests = site.requests
    result.pages = site.page_requests
    result.unlabeled = _unlabeled(result.changed)
    result.finished_at = get_now_time()
    actresses.finish_correction(result.finished_at)
    left = f'，还有 {result.left} 部留到下一轮' if result.left else ''
    corrected = f'，按新存法重新查了 {result.corrected} 部' if result.corrected else ''
    logger.info(f'补演员名结束：补到 {result.filled} 部，av-wiki 上没有 {result.missing} 部，'
                f'失败 {result.failed} 部{corrected}{left}；读了 {result.pages} 个女优页，'
                f'这一轮请求 {result.requests} 次（今天一共 {daily_used(result.finished_at)} 次）')
    last_round = result
    return result


def status():
    '''现在的状态：还有多少部在等、累计补到多少部、迁移后的校正还剩多少、今天的请求数、最近一轮的结果、最近一次失败'''
    now = get_now_time()
    waiting, due, liked, recommended = db.count_starless(now)
    stale, pages = actresses.correction_left()
    failure = db.latest_failed_lookup()
    # 以前的版本把女优页的整个地址记进了原因，拿出来给页面用时同样不带名字
    return FillStatus(
        waiting=waiting, liked=liked, recommended=recommended, due=due, filled=db.count_lookups(db.LOOKUP_FILLED),
        stale=stale, stale_later=stale - db.count_stale_due(now), pages=pages, used=daily_used(now),
        limit=daily_limit(), last_round=last_round,
        last_failure=None if failure is None else (failure.item_id, hide_actress(failure.detail), failure.checked_at))
