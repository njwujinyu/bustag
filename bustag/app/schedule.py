'''
定时任务：启动后先看模型要不要升级（upgrade_model_job），约 1 秒后补一轮演员名，约 5 秒后抓取一次，
之后每 interval 秒抓取一次；每次定时抓取之后接着抓排队的影片（上次没抓完的也在里面）。
网页上登记的抓排队的影片、补演员名约 2 秒后执行。抓完自动给影片打分，再补一轮演员名。
抓排队的影片因为数据库被别的程序占着没做成时，用户登记了的事不丢：先马上再排几次，再不行就每隔几分钟再试，直到做成。
所有任务在同一个后台线程里按顺序执行。
'''
import queue
import sys
import threading
import uuid
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone

from apscheduler.executors.base import BaseExecutor, run_job
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from peewee import OperationalError

import bustag.model.classifier as clf
from bustag.model import prepare
from bustag.model.persist import LegacyModelError, ModelLoadError, ModelSaveError
from bustag.spider import actresses, avwiki, backfill, bus_spider, db
from bustag.util import APP_CONFIG, format_datetime, get_now_time, logger

# 启动后的那一轮补演员名排在升级模型之后、第一次抓取之前
STARTUP_FILL_DELAY = 1
FIRST_DOWNLOAD_DELAY = 5
QUEUE_DELAY = 2
ACTRESS_FILL_DELAY = 2
# 按演员补全的今天不接着抓了（额度用完、被拦截暂停）时，第二天这个时刻接着抓；任务的 id
TOMORROW_AT = time(0, 1)
TOMORROW_JOB = 'queue-tomorrow'
# 演员数据的升级没做成、程序一直开着时，每天再试的那一次等写锁最多这么多秒：库还被占着就留到明天，不让后台任务干等 30 秒
RETRY_WAIT = 3
scheduler = None
_start_lock = threading.Lock()
# 最近一次定时抓取：(结束的时间, 结果的一句话)，启动后还没有抓取过时是 None
last_download = None
# 定时抓取正在进行时是它开始的时间，否则是 None
_download_started = None
# 执行任务的那个 SerialExecutor，定时任务没有启动时是 None
_executor = None
_fill_lock = threading.Lock()
# 已经排了一轮补演员名、还没开始跑。这期间再登记不用另排一轮
_fill_queued = False
# 一轮补演员名正在进行时是它开始的时间，否则是 None
_fill_started = None
_queue_lock = threading.Lock()
# 已经排了一次"抓排队的影片"、还没开始跑。这期间再登记不用另排一次
_queue_queued = False
# 正在抓排队的影片时是开始的时间，否则是 None
_queue_started = None
# 启动后的那一次升级模型已经排上、还没做完
_upgrade_pending = False


class SerialExecutor(BaseExecutor):
    '''
    在一个守护线程里按顺序执行任务。APScheduler 自带的线程池不是守护线程，
    程序退出（如 Ctrl+C）时要等正在跑的抓取结束；用守护线程就不用等。
    '''

    def start(self, scheduler, alias):
        super().start(scheduler, alias)
        self._queue = queue.Queue()
        # 已经交过来、还没轮到的任务的 id
        self._waiting = []
        threading.Thread(target=self._work, name='bustag-jobs', daemon=True).start()

    def _do_submit_job(self, job, run_times):
        self._waiting.append(job.id)
        self._queue.put((job, run_times))

    def is_waiting(self, job_id):
        '''这个任务是不是到了时间、排在别的任务后面还没开始'''
        return job_id in self._waiting

    def _work(self):
        while True:
            job, run_times = self._queue.get()
            self._waiting.remove(job.id)
            try:
                events = run_job(job, job._jobstore_alias, run_times, self._logger.name)
            except BaseException:
                self._run_job_error(job.id, *sys.exc_info()[1:])
            else:
                self._run_job_success(job.id, events)


# 写锁没有拿到、一开始就被拒绝时的说法：这条连接读到的数据已经被别的连接改过，再写就立刻失败，不会等
REFUSED_AT_ONCE = '数据库正被别的程序占用，一开始就没有拿到写锁（没有等）'
# 抓排队的影片因为数据库忙没做成、马上再排一次的最多次数：数据库一直被占着时不一次次马上去试，
# 连着这么多次都不行就改成隔几分钟再试（BUSY_RETRY_DELAYS）。这一次库忙结束（做成了、没有要做的事了、出了别的错）就从头数
MAX_BUSY_RETRIES = 3
# 马上再排的几次用完以后，用户登记了的事还没做：隔这么多秒再试一次，一次比一次晚，最后一个一直重复。
# 数据库一空出来，最迟隔最后一个（3 分钟）就接着做
BUSY_RETRY_DELAYS = (60, 120, 180)
_busy_retries = 0
# 从第一次因为数据库忙没做成，到做成一轮（或者没有要做的事了）为止，这一次"库忙"：开始的时间、
# 隔几分钟再试已经排了几次、日志里已经写过的说法（同一个原因同一种打算在这一次库忙里只写一遍），没有时是 None / 0 / None
_busy_since = None
_busy_waits = 0
_busy_told = None
# 再试时先拿的写锁拿到了、正在做：这时页面上不再写"数据库被占着"（又因为库忙没做成时再写）
_busy_going = False
# 抓排队的影片排着的那一次（_queue_queued 为真时）什么时候到点（带时区）和它在调度器里的 id
_queue_due = None
_queue_job = None


def _refused_at_once(error):
    '''这个数据库忙的错是不是一开始就被拒绝的（sqlite3 的错误名是 SQLITE_BUSY_SNAPSHOT，Python 3.11 起带着）'''
    return any(getattr(cause, 'sqlite_errorname', '') == 'SQLITE_BUSY_SNAPSHOT'
               for cause in (error.__cause__, error.__context__))


def _busy_reason_of(error):
    return REFUSED_AT_ONCE if _refused_at_once(error) else db.busy_reason(error)


@contextmanager
def _unless_busy(task):
    '''
    包住一个后台任务里写数据库的那一段：数据库被别的程序占着、写不进去时只写一条日志，后面的任务和下一轮照常进行；
    日志照实写是等到超时还没轮到，还是一开始就被拒绝
    '''
    try:
        yield
    except OperationalError as ex:
        if not db.is_busy(ex):
            raise
        logger.warning(f'{task}没有做成：{_busy_reason_of(ex)}，下一轮照常进行')


def recommend():
    '''抓取后的打分：没有模型、模型不能用或者数据库被占着时只写日志，不影响后面的任务'''
    with _unless_busy('抓取后的打分'):
        try:
            clf.recommend()
        except FileNotFoundError:
            logger.info('还没有训练好的模型，跳过推荐')
        except ModelLoadError as ex:
            logger.warning(f'无法推荐：{ex}')


OLD_MODEL = '现有的模型文件是旧版本程序训练的，新版本换了模型'
ACTRESS_MODEL = '演员的存法升级了（每位女优只留一个正式名字），现有的模型是升级前训练的'
# 模型因为演员的存法改变而要重新训练、打标又不够时，已经为哪一次改变写过警告：每一轮补演员名之后都会看一次，只写一遍
_warned_about = None


def _retrain_reason():
    '''
    现有的模型要重新训练的原因：数据目录里只有旧版本程序训练的模型文件；或者模型是演员的名字整体换了
    （迁移、校正做完）之前训练的，它认识的演员名有些已经不在影片上了。不用重新训练时返回 None
    '''
    try:
        model = clf.load()
    except LegacyModelError:
        return OLD_MODEL
    except (FileNotFoundError, ModelLoadError):
        return None
    changed = actresses.features_changed_at()
    if changed is not None and model.trained_at < format_datetime(changed):
        return ACTRESS_MODEL
    return None


def _upgrade_model():
    '''模型要重新训练（见 _retrain_reason）、打标又够时重新训练并打分。返回这一次有没有训练好（训练好了就打过分了）'''
    global _warned_about
    reason = _retrain_reason()
    if reason is None:
        return False
    problem = prepare.training_problem(*prepare.count_labels())
    if problem:
        repeat = (reason, actresses.features_changed_at())
        if reason == ACTRESS_MODEL and _warned_about == repeat:
            return False
        _warned_about = repeat
        logger.warning(f'{reason}；没有自动重新训练：{problem}。打标够了以后请到网页的"模型"页点击"开始训练"')
        return False
    logger.info(f'{reason}：用现有的打标自动重新训练，并给影片重新打分')
    try:
        clf.train_and_recommend()
    except (ValueError, ModelSaveError) as ex:
        logger.warning(f'自动重新训练没有完成：{ex}。请到网页的"模型"页点击"开始训练"再试一次')
        return False
    return True


def upgrade_model_job():
    '''
    启动后的升级：先把旧存法的演员数据换成新存法（actresses.migrate，没有旧数据时什么都不做）；再看模型——
    数据目录里只有旧版本程序训练的模型、或者模型是演员数据升级前训练的，打标够训练就自动重新训练并给影片重新打分，
    升级后不用再到"模型"页点一次；不够时只写日志，"模型"页和推荐页上有提示
    '''
    global _upgrade_pending
    try:
        with _unless_busy('演员数据升级'):
            actresses.migrate()
        _upgrade_model()
    finally:
        _upgrade_pending = False


def upgrade_pending():
    '''启动后的那一次升级模型（upgrade_model_job）是不是已经排上、还没做完。这期间页面上不该让用户自己去训练'''
    return _upgrade_pending


def _chosen_stars():
    '''指定推荐里选了的女优名字：她们的女优页也要读（补全别名）'''
    from bustag.app import pick
    return pick.load_settings().stars


def _retry_upgrade():
    '''
    启动时的演员数据升级（或者再整理旧版本写下的名字）没做成、程序又一直开着：每天第一轮补演员名之前再试一次。
    只等 RETRY_WAIT 秒写锁，库还被占着就留到明天（actresses.migrate 先看库空不空，空着才做快照）
    '''
    migration = actresses.last_migration
    if migration is None or migration.state != 'failed' or migration.at.date() >= get_now_time().date():
        return
    logger.info(f'演员数据上次没有升级成（{format_datetime(migration.at)}），今天再试一次')
    actresses.migrate(wait=RETRY_WAIT)


def fill_actresses():
    '''
    到 av-wiki 补一轮演员名（演员数据的升级上次没做成的话，每天第一轮之前先再试一次）。之后最多打一次分：
    迁移后的校正做完、模型是那之前训练的，打标够就重新训练（训练完就打了分）；没有重新训练、这一轮里不是用户打标的
    影片的演员又变了，才单独打一次分。出什么错都只写日志，不影响后面的任务
    '''
    global _fill_started
    _fill_started = get_now_time()
    result = None
    try:
        _retry_upgrade()
    except Exception:
        logger.exception('再试演员数据升级时出错')
    try:
        with _unless_busy('补演员名'):
            result = avwiki.fill_round(chosen=_chosen_stars())
    except Exception:
        logger.exception('补演员名出错')
    finally:
        _fill_started = None
    trained = False
    with _unless_busy('补演员名之后看模型'):
        try:
            trained = _upgrade_model()
        except Exception:
            logger.exception('补演员名之后看模型时出错')
    if result is not None and result.unlabeled and not trained:
        logger.info(f'补演员名改了 {result.unlabeled} 部没有用户打标的影片的演员，重新打一次分')
        recommend()


def fill_actresses_job():
    global _fill_queued
    with _fill_lock:
        _fill_queued = False
    fill_actresses()


def fill_state():
    '''
    补演员名现在在做什么，给关于页用：('running', 开始的时间) 是一轮正在进行；('queued', None) 是已经排了一轮、
    还没开始；(None, None) 是都不是
    '''
    started = _fill_started
    if started is not None:
        return 'running', started
    if _fill_queued:
        return 'queued', None
    return None, None


BusyWait = namedtuple('BusyWait', 'since next_at')


def busy_wait():
    '''
    抓排队的影片因为数据库被别的程序占着没做成、用户登记了的事留着等再试时：BusyWait(第一次没做成的时间, 下一次再试的时间)，
    下一次还没排上（正在试）时第二项是 None；没有这回事时返回 None。时间都是本地时间
    '''
    since = _busy_since
    if since is None or _busy_going:
        return None
    due = _queue_due if _queue_queued else None
    return BusyWait(since, None if due is None else due.astimezone().replace(tzinfo=None))


def _how_long(delta):
    seconds = max(0, int(delta.total_seconds()))
    return f'{seconds} 秒' if seconds < 60 else f'约 {round(seconds / 60)} 分钟'


def _work_left():
    '''用户登记了、还没做的事（查找、查看、补全、排着要抓的）还有没有。读不了数据库（库忙）时当作有'''
    try:
        return backfill.has_work()
    except OperationalError as ex:
        if db.is_busy(ex):
            return True
        raise


def _did_something(result):
    '''这一次抓排队的影片有没有做事（读了列表页、排了队、抓了或者跳过了影片）'''
    return bool(result.pages or result.queued or result.fetched or result.skipped or result.missing or result.failed
                or result.retry_later)


def _end_busy(recovered=False, result=None):
    '''
    这一次"库忙"结束了：做成了一轮（recovered，result 是它的结果，写一条日志），或者没有要做的事了（取消、被拦截）、
    出了别的错。不管怎么结束，马上再排用了几次（_busy_retries）都从头数：下一次库忙照样先马上再排几次
    '''
    global _busy_retries, _busy_since, _busy_waits, _busy_told, _busy_going
    since = _busy_since
    _busy_retries, _busy_since, _busy_waits, _busy_told, _busy_going = 0, None, 0, None, False
    if recovered and since is not None:
        went_on = '抓排队的影片接着做了' if _did_something(result) else '没有要接着做的事了'
        logger.info(f'数据库空出来了，{went_on}（从 {since.strftime("%H:%M")} 起，'
                    f'等了 {_how_long(get_now_time() - since)}）')


def _plan_retry():
    '''
    抓排队的影片这一次因为数据库忙没做成：再排一次。返回打算：'now' 马上再排（前 MAX_BUSY_RETRIES 次，排着的那一次
    是 QUEUE_DELAY 秒以后的），'later' 用户登记了的事还没做、之后每隔几分钟再试（BUSY_RETRY_DELAYS），
    'none' 没有再排（登记了的事没有了：用户取消了、被网站拦截清掉了；或者定时任务没有启动）。
    马上再排用完以后一律是 'later'：已经排着一次（上一次没做成时排的、或者网页上的点击排的）就不另排，打算也不变，
    所以定时抓取里的抓排队的影片在这期间撞上库忙，不会让日志换一种说法
    '''
    global _busy_retries, _busy_waits
    if _busy_retries < MAX_BUSY_RETRIES:
        if request_queue_run():
            _busy_retries += 1
        return 'now' if _queue_queued else 'none'
    if not _work_left():
        return 'none'
    if _queue_queued:
        return 'later'
    delay = BUSY_RETRY_DELAYS[min(_busy_waits, len(BUSY_RETRY_DELAYS) - 1)]
    if _queue_run_at(delay):
        _busy_waits += 1
        return 'later'
    return 'none'


def _cut_short(error):
    '''
    抓排队的影片这一次因为数据库忙没做成：再排一次（见 _plan_retry），日志写原因和打算。同一个原因、同一种打算在这一次
    库忙里只写一遍，之后的写成调试信息（库被占着很久时不刷屏）；不再排了的每次都写
    '''
    global _busy_since, _busy_told, _busy_going
    reason = _busy_reason_of(error)
    _busy_going = False
    if _busy_since is None:
        _busy_since = get_now_time()
    plan = _plan_retry()
    if plan == 'none':
        _end_busy()
        logger.warning(f'抓排队的影片没有做成：{reason}，下一轮照常进行')
        return
    log = logger.debug if _busy_told == (reason, plan) else logger.warning
    _busy_told = (reason, plan)
    if plan == 'now':
        log(f'抓排队的影片没有做成：{reason}，马上再排一轮')
    else:
        log(f'抓排队的影片还是没有做成：{reason}。登记的补全、查看、查找都留着，之后每隔几分钟再试一次，'
            '数据库一空出来就接着做（库被占着的时候不会去请求网站）')


def run_queue():
    '''
    抓一次排队的影片，返回 backfill.QueueRun；出什么错都只写日志（返回 None），不影响后面的任务。
    数据库被别的程序占着没做成时再排一次（见 _plan_retry）；已经因此没做成过的，每次再试先拿一下写锁，
    还被占着就什么也不做，也就不对网站发请求
    '''
    global _queue_started, _busy_going
    _queue_started = get_now_time()
    try:
        try:
            if _busy_since is not None and _work_left():
                backfill.ensure_writable()
                _busy_going = True
            result = backfill.run()
        except OperationalError as ex:
            if not db.is_busy(ex):
                raise
            _cut_short(ex)
            return None
        _end_busy(recovered=True, result=result)
        return result
    except Exception:
        _end_busy()
        logger.exception('抓排队的影片出错')
    finally:
        _queue_started = None
    return None


def _run_cut_short(result):
    '''
    抓排队的影片这一次没有做成、下一次已经排上了：补演员名也留给下一次（它做完也要补一轮），
    数据库一直被占着时不一轮接一轮地去 av-wiki 请求
    '''
    return result is None and _queue_queued


def _plan_tomorrow(held):
    '''
    按演员补全的还在排队、今天却不接着抓了（held 是 backfill.held_reason() 的结果）：排一个第二天 0 点 1 分的
    抓排队的影片。已经排了的换成这一个，不会排出两个
    '''
    if not held or scheduler is None:
        return
    run_at = datetime.combine(get_now_time().date() + timedelta(days=1), TOMORROW_AT).astimezone()
    scheduler.add_job(queue_job, 'date', run_date=run_at, id=TOMORROW_JOB, name='抓排队的影片', replace_existing=True)
    why = '今天的额度用完了' if held == 'limit' else '今天被网站拦截过'
    logger.info(f'按演员补全{why}，排队的明天 {TOMORROW_AT.strftime("%H:%M")} 接着抓')


def queue_job(job_id=None):
    '''
    抓排队的影片；新增了影片就打一次分，再补一轮演员名。_queue_run_at 排的那一次开始时带着自己的 id，
    "已经排着一次"的记号这时才去掉；第二天 0 点 1 分的任务和别的直接调用不带 id，它们开始时排着的那一次还在，
    不能动这个记号（动了就会再排出第二个来）
    '''
    global _queue_queued
    if job_id is not None:
        with _queue_lock:
            if job_id == _queue_job:
                _queue_queued = False
    result = run_queue()
    if _run_cut_short(result):
        return
    if result is not None and result.added:
        recommend()
    fill_actresses()
    if result is not None:
        _plan_tomorrow(result.held)


def queue_state():
    '''
    抓排队的影片现在在做什么，给页面用：('running', 开始的时间) 是正在抓；('queued', None) 是已经排了一次、
    还没开始；(None, None) 是都不是
    '''
    started = _queue_started
    if started is not None:
        return 'running', started
    if _queue_queued:
        return 'queued', None
    return None, None


def download_job():
    '''
    定时抓取：抓首页，接着抓排队的影片，一起打一次分，再补一轮演员名。
    首页被拦截时不抓排队的，按演员补全今天也暂停（拦截是整个网站的事）
    '''
    global last_download, _download_started
    _download_started = get_now_time()
    blocked = ''
    try:
        with _unless_busy('定时抓取'):
            result = bus_spider.download()
            last_download = (get_now_time(), str(result))
            blocked = result.blocked or ''
    finally:
        _download_started = None
    queued = None
    cut_short = False
    if blocked:
        backfill.record_block(blocked)
    else:
        queued = run_queue()
        cut_short = _run_cut_short(queued)
    recommend()
    if not cut_short:
        fill_actresses()
    if queued is not None:
        _plan_tomorrow(queued.held)
    elif blocked:
        with _unless_busy('排第二天的抓取'):
            _plan_tomorrow(backfill.held_reason())


def download_state():
    '''
    定时抓取现在在做什么，给关于页用：('running', 开始的时间) 是正在抓取；('queued', None) 是到了时间、
    排在别的任务后面还没开始；(None, None) 是都不是。抓完以后同一个任务里的打分、补演员名不算在抓取里
    '''
    started = _download_started
    if started is not None:
        return 'running', started
    executor = _executor
    if executor is not None and executor.is_waiting('download'):
        return 'queued', None
    return None, None


def start_scheduler():
    '''
    启动定时任务，不阻塞；重复调用只启动一次。返回调度器
    '''
    global scheduler, _executor, _fill_queued, _upgrade_pending
    with _start_lock:
        if scheduler is not None:
            return scheduler
        interval = int(APP_CONFIG['download.interval'])
        _executor = SerialExecutor()
        new_scheduler = BackgroundScheduler(
            executors={'default': _executor},
            job_defaults={'coalesce': True, 'max_instances': 1, 'misfire_grace_time': None},
            timezone=timezone.utc)
        now = datetime.now(timezone.utc)
        new_scheduler.add_job(upgrade_model_job, 'date', run_date=now, id='upgrade-model', name='升级模型')
        new_scheduler.add_job(fill_actresses_job, 'date', run_date=now + timedelta(seconds=STARTUP_FILL_DELAY),
                              id='fill-actresses', name='补演员名')
        new_scheduler.add_job(download_job, 'interval', seconds=interval,
                              next_run_time=now + timedelta(seconds=FIRST_DOWNLOAD_DELAY),
                              id='download', name='定时抓取')
        with _fill_lock:
            _fill_queued = True
        _upgrade_pending = True
        new_scheduler.start()
        scheduler = new_scheduler
        logger.info(f'定时任务已启动：约 {FIRST_DOWNLOAD_DELAY} 秒后开始抓取，之后每 {interval} 秒抓取一次')
        return scheduler


def _queue_run_at(delay):
    '''
    排一次抓排队的影片，delay 秒后开始。已经排着一次、而且不比这个晚的不另排，返回 False；已经排着的更晚
    （数据库忙以后留的"过几分钟再试"）、这一次要求更早时，改成这一次的，返回 True；定时任务没有启动时返回 False
    '''
    global _queue_queued, _queue_due, _queue_job
    with _queue_lock:
        if scheduler is None:
            return False
        run_date = datetime.now(timezone.utc) + timedelta(seconds=delay)
        if _queue_queued:
            if _queue_due is None or _queue_due <= run_date:
                return False
            try:
                scheduler.reschedule_job(_queue_job, trigger='date', run_date=run_date)
            except JobLookupError:
                # 已经到点、交给后台线程了，只是还没轮到它：它开始时看得到现在登记的
                return False
            _queue_due = run_date
            return True
        job_id = uuid.uuid4().hex
        scheduler.add_job(queue_job, 'date', run_date=run_date, id=job_id, args=[job_id], name='抓排队的影片')
        _queue_queued, _queue_due, _queue_job = True, run_date, job_id
        return True


def request_queue_run():
    '''
    登记"有排队的影片要抓"：约 2 秒后在后台任务线程里抓一次，轮到时排着的都会抓。已经排了一次、还没开始、
    而且不比这个晚时不再另排，返回 False；排着的那一次更晚（数据库忙以后留的"过几分钟再试"）时提前到这个时间，返回 True。
    定时任务没有启动时也返回 False（影片记在库里，下次启动后接着抓）
    '''
    if not _queue_run_at(QUEUE_DELAY):
        return False
    logger.debug(f'已登记抓排队的影片，约 {QUEUE_DELAY} 秒后开始')
    return True


def request_actress_fill():
    '''
    登记"有影片要补演员名"：约 2 秒后在后台任务线程里补一轮，轮到时该补的影片都会处理。
    已经排了一轮、还没开始时不再另排，返回 False；定时任务没有启动时也返回 False（下次启动后的那一轮会补上）
    '''
    global _fill_queued
    with _fill_lock:
        if scheduler is None or _fill_queued:
            return False
        run_date = datetime.now(timezone.utc) + timedelta(seconds=ACTRESS_FILL_DELAY)
        scheduler.add_job(fill_actresses_job, 'date', run_date=run_date, name='补演员名')
        _fill_queued = True
    logger.debug(f'已登记补演员名，约 {ACTRESS_FILL_DELAY} 秒后开始')
    return True
