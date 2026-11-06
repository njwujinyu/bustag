'''
定时任务：启动后约 5 秒抓取一次，之后每 interval 秒抓取一次；按番号抓取的任务约 2 秒后执行。
每次抓完自动推荐。所有任务在同一个后台线程里按顺序执行。
'''
import queue
import sys
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.executors.base import BaseExecutor, run_job
from apscheduler.schedulers.background import BackgroundScheduler

from bustag.model.persist import ModelLoadError
from bustag.spider import bus_spider
from bustag.util import APP_CONFIG, logger

FIRST_DOWNLOAD_DELAY = 5
FANHAO_DOWNLOAD_DELAY = 2
scheduler = None
_start_lock = threading.Lock()


class SerialExecutor(BaseExecutor):
    '''
    在一个守护线程里按顺序执行任务。APScheduler 自带的线程池不是守护线程，
    程序退出（如 Ctrl+C）时要等正在跑的抓取结束；用守护线程就不用等。
    '''

    def start(self, scheduler, alias):
        super().start(scheduler, alias)
        self._queue = queue.Queue()
        threading.Thread(target=self._work, name='bustag-jobs', daemon=True).start()

    def _do_submit_job(self, job, run_times):
        self._queue.put((job, run_times))

    def _work(self):
        while True:
            job, run_times = self._queue.get()
            try:
                events = run_job(job, job._jobstore_alias, run_times, self._logger.name)
            except BaseException:
                self._run_job_error(job.id, *sys.exc_info()[1:])
            else:
                self._run_job_success(job.id, events)


def recommend():
    '''抓取后的推荐：没有模型或模型读不了时只写日志，不影响后面的任务'''
    # 模型模块要导入 scikit-learn，较慢，只在用到时导入
    import bustag.model.classifier as clf

    try:
        clf.recommend()
    except FileNotFoundError:
        logger.info('还没有训练好的模型，跳过推荐')
    except ModelLoadError as ex:
        logger.warning(f'无法推荐：{ex}')


def download_job():
    bus_spider.download()
    recommend()


def download_fanhaos_job(fanhaos):
    bus_spider.download_fanhaos(fanhaos)
    recommend()


def start_scheduler():
    '''
    启动定时任务，不阻塞；重复调用只启动一次。返回调度器
    '''
    global scheduler
    with _start_lock:
        if scheduler is not None:
            return scheduler
        interval = int(APP_CONFIG['download.interval'])
        new_scheduler = BackgroundScheduler(
            executors={'default': SerialExecutor()},
            job_defaults={'coalesce': True, 'max_instances': 1, 'misfire_grace_time': None},
            timezone=timezone.utc)
        first_run = datetime.now(timezone.utc) + timedelta(seconds=FIRST_DOWNLOAD_DELAY)
        new_scheduler.add_job(download_job, 'interval', seconds=interval, next_run_time=first_run,
                              id='download', name='定时抓取')
        new_scheduler.start()
        scheduler = new_scheduler
        logger.info(f'定时任务已启动：约 {FIRST_DOWNLOAD_DELAY} 秒后开始抓取，之后每 {interval} 秒抓取一次')
        return scheduler


def add_download_job(fanhaos):
    '''
    约 2 秒后抓取指定番号，抓完推荐。返回 APScheduler 的 Job；
    番号为空或定时任务没有启动时返回 None（后者写一条警告日志）
    '''
    fanhaos = list(fanhaos)
    if not fanhaos:
        return None
    if scheduler is None:
        logger.warning(f'定时任务没有启动，{len(fanhaos)} 个番号没有加入下载队列')
        return None
    run_date = datetime.now(timezone.utc) + timedelta(seconds=FANHAO_DOWNLOAD_DELAY)
    job = scheduler.add_job(download_fanhaos_job, 'date', run_date=run_date, args=[fanhaos],
                            name='按番号抓取')
    logger.info(f'已加入下载队列：{len(fanhaos)} 个番号，约 {FANHAO_DOWNLOAD_DELAY} 秒后开始抓取')
    return job
