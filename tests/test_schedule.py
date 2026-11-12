'''
定时任务：只启动一次、按番号抓取、补演员名、所有任务在一个守护线程里按顺序执行、出错不影响后面的任务。
抓取、推荐和补演员名都换成记录调用的假函数；启动后的那一轮补演员名和定时抓取的首次运行都推迟到一小时后，
测试期间不会触发
'''
import logging
import re
import sqlite3
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest
from peewee import OperationalError

from bustag import util
from bustag.app import schedule
from bustag.model import persist
from bustag.model.persist import LEGACY_MODEL, MODEL_FILE
from bustag.spider import avwiki, backfill, bus_spider, db
from bustag.spider.bus_spider import DownloadResult
from bustag.spider.db import RATE_TYPE, ItemRate


def warnings_in(caplog):
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


def wait_for(condition, timeout=60):
    # 只是防止一直卡住：慢的机器上后台线程要过一阵才轮到，界限放得很宽
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, '等待超时'
        time.sleep(0.01)


def raising(error):
    '''一个一调用就抛 error 的函数，换掉 backfill.run'''
    def run():
        raise error

    return run


def refused_at_once():
    '''
    一次真的"立刻被拒绝"的写：这条连接上有读了一半的查询，另一条连接提交了一次写，这条连接再写就立刻失败、不会等。
    返回那个 OperationalError
    '''
    db.JobState.create(name='first', text='')
    db.JobState.create(name='second', text='')
    cursor = db.db.execute_sql('SELECT id FROM job_state ORDER BY id')
    cursor.fetchone()

    def write():
        try:
            with db.db.atomic('IMMEDIATE'):
                db.JobState.create(name='web', text='网页上的一次写')
        finally:
            db.db.close()

    thread = threading.Thread(target=write)
    thread.start()
    thread.join(timeout=10)
    try:
        db.JobState.update(text='x').execute()
    except OperationalError as ex:
        return ex
    finally:
        cursor.close()
    raise AssertionError('这一次写没有被立刻拒绝')


# calls 会把 schedule.fill_actresses 换成假函数，要用真的那个的测试从这里取
FILL_ACTRESSES = schedule.fill_actresses


@pytest.fixture(autouse=True)
def nothing_warned_yet(monkeypatch):
    monkeypatch.setattr(schedule, '_warned_about', None)


@pytest.fixture(autouse=True)
def not_busy_before(monkeypatch):
    monkeypatch.setattr(schedule, '_busy_retries', 0)
    monkeypatch.setattr(schedule, '_busy_since', None)
    monkeypatch.setattr(schedule, '_busy_waits', 0)
    monkeypatch.setattr(schedule, '_busy_told', None)
    monkeypatch.setattr(schedule, '_busy_going', False)


@pytest.fixture
def calls(monkeypatch):
    '''按发生顺序记录 download / 抓排队的影片（run）/ recommend / 补演员名（fill）的调用。抓排队的影片每次新增 1 部'''
    log = []

    def download():
        log.append(('download',))
        return DownloadResult(added=3)

    def run():
        log.append(('run',))
        return backfill.QueueRun(fetched=1, added=1)

    monkeypatch.setattr(bus_spider, 'download', download)
    monkeypatch.setattr(backfill, 'run', run)
    monkeypatch.setattr(schedule, 'recommend', lambda: log.append(('recommend',)))
    monkeypatch.setattr(schedule, 'fill_actresses', lambda: log.append(('fill',)))
    return log


@pytest.fixture
def not_started(monkeypatch):
    monkeypatch.setattr(schedule, 'scheduler', None)
    monkeypatch.setattr(schedule, 'FIRST_DOWNLOAD_DELAY', 3600)
    monkeypatch.setattr(schedule, 'STARTUP_FILL_DELAY', 3600)
    monkeypatch.setattr(schedule, '_fill_queued', False)
    monkeypatch.setattr(schedule, '_fill_started', None)
    monkeypatch.setattr(schedule, '_queue_queued', False)
    monkeypatch.setattr(schedule, '_queue_due', None)
    monkeypatch.setattr(schedule, '_queue_job', None)
    monkeypatch.setattr(schedule, '_queue_started', None)
    monkeypatch.setattr(schedule, '_upgrade_pending', False)
    monkeypatch.setattr(schedule, 'last_download', None)


@pytest.fixture
def scheduler(not_started, calls, monkeypatch):
    started = schedule.start_scheduler()
    # 启动后的那一轮补演员名被推迟到了一小时后：当作它已经开始了，之后的登记各排各的
    monkeypatch.setattr(schedule, '_fill_queued', False)
    yield started
    started.shutdown(wait=False)


class TestStartScheduler:
    def test_started_once(self, scheduler):
        assert schedule.scheduler is scheduler
        assert schedule.start_scheduler() is scheduler
        assert scheduler.timezone.utcoffset(None) == timedelta(0)
        job = scheduler.get_job('download')
        assert job.trigger.interval == timedelta(seconds=int(util.APP_CONFIG['download.interval']))
        delay = (job.next_run_time - datetime.now(timezone.utc)).total_seconds()
        assert 3500 < delay <= 3600
        fill = scheduler.get_job('fill-actresses')
        assert fill.name == '补演员名'
        assert 3500 < (fill.next_run_time - datetime.now(timezone.utc)).total_seconds() <= 3600

    def test_interval_from_config(self, not_started, calls, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.interval', '600')
        started = schedule.start_scheduler()
        try:
            assert started.get_job('download').trigger.interval == timedelta(seconds=600)
        finally:
            started.shutdown(wait=False)

    def test_concurrent_calls_start_one_scheduler(self, not_started, calls):
        barrier = threading.Barrier(5)
        results = []

        def start():
            barrier.wait()
            results.append(schedule.start_scheduler())

        threads = [threading.Thread(target=start) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        try:
            assert len(results) == 5
            assert all(result is results[0] for result in results)
        finally:
            for started in {id(r): r for r in results}.values():
                started.shutdown(wait=False)


class TestRequestQueueRun:
    '''上传番号、导入打标库以后登记"有排队的影片要抓"：约 2 秒后在后台任务线程里抓一次'''

    def test_scheduler_not_started(self, not_started, caplog):
        # 番号已经记在库里，下次启动后的第一次定时抓取会接着抓
        assert schedule.request_queue_run() is False
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_a_run_is_queued_to_start_shortly(self, scheduler):
        before = datetime.now(timezone.utc)
        assert schedule.request_queue_run() is True
        job, = [job for job in scheduler.get_jobs() if job.name == '抓排队的影片']
        # 约 2 秒后开始；上界放宽：机器忙时登记和这一行之间可能隔很久，要紧的是不是几个小时以后
        assert 1.5 <= (job.next_run_time - before).total_seconds() <= 60

    def test_requests_made_before_the_run_starts_share_it(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 0.2)
        assert [schedule.request_queue_run() for _ in range(3)] == [True, False, False]
        wait_for(lambda: ('fill',) in calls)
        time.sleep(0.5)
        assert calls == [('run',), ('recommend',), ('fill',)]

    def test_a_request_made_while_a_run_is_going_gets_another_run(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 0)
        runs = []
        release = threading.Event()

        def run():
            runs.append(threading.current_thread().name)
            if len(runs) == 1:
                release.wait(timeout=10)
            return backfill.QueueRun()

        monkeypatch.setattr(backfill, 'run', run)
        assert schedule.request_queue_run() is True
        try:
            wait_for(lambda: runs)
            # 这一次已经开始了，之后才排进来的番号不一定轮得到：再排一次
            assert schedule.request_queue_run() is True
            assert schedule.request_queue_run() is False
        finally:
            release.set()
        wait_for(lambda: len(runs) == 2)
        assert runs == ['bustag-jobs', 'bustag-jobs']

    def test_the_run_scores_once_then_fills_actresses(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 0)
        schedule.request_queue_run()
        wait_for(lambda: len(calls) == 3)
        assert calls == [('run',), ('recommend',), ('fill',)]

    def test_a_run_that_added_nothing_does_not_score(self, calls, monkeypatch):
        monkeypatch.setattr(backfill, 'run', lambda: calls.append(('run',)) or backfill.QueueRun(fetched=2, missing=2))
        schedule.queue_job()
        assert calls == [('run',), ('fill',)]

    def test_a_download_whose_time_has_come_waits_behind_the_job_that_is_running(self, scheduler, calls, monkeypatch):
        # 所有任务排成一队：抓排队的影片还没做完时到了定时抓取的时间，定时抓取排在后面等
        busy, release = threading.Event(), threading.Event()

        def slow():
            busy.set()
            assert release.wait(timeout=10)
            return backfill.QueueRun()

        monkeypatch.setattr(backfill, 'run', slow)
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 0)
        assert schedule.download_state() == (None, None)
        schedule.request_queue_run()
        assert busy.wait(timeout=10)
        assert schedule.download_state() == (None, None)
        scheduler.modify_job('download', next_run_time=datetime.now(timezone.utc))
        wait_for(lambda: schedule.download_state() == ('queued', None))
        release.set()
        wait_for(lambda: ('download',) in calls)
        wait_for(lambda: schedule.download_state() == (None, None))

    def test_jobs_run_one_at_a_time_in_a_daemon_thread(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 0)
        monkeypatch.setattr(schedule, 'ACTRESS_FILL_DELAY', 0)
        events = []
        release = threading.Event()

        def run():
            thread = threading.current_thread()
            events.append(('run', thread.name, thread.daemon))
            release.wait(timeout=10)
            events.append(('run done',))
            return backfill.QueueRun()

        def fill_actresses():
            events.append(('fill', threading.current_thread().name))

        monkeypatch.setattr(backfill, 'run', run)
        monkeypatch.setattr(schedule, 'fill_actresses', fill_actresses)
        schedule.request_queue_run()
        try:
            wait_for(lambda: events)
            schedule.request_actress_fill()
            time.sleep(0.3)
            assert events == [('run', 'bustag-jobs', True)]
        finally:
            release.set()
        # 等两个任务都做完再结束，免得任务线程在假函数还原之后才调用它们
        wait_for(lambda: len(events) == 4)
        assert events == [('run', 'bustag-jobs', True), ('run done',), ('fill', 'bustag-jobs'), ('fill', 'bustag-jobs')]

    def test_failing_run_does_not_stop_later_jobs(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 0)
        monkeypatch.setattr(schedule, 'ACTRESS_FILL_DELAY', 0)

        def broken():
            calls.append(('run',))
            raise RuntimeError('抓取出错')

        monkeypatch.setattr(backfill, 'run', broken)
        schedule.request_queue_run()
        wait_for(lambda: ('fill',) in calls)
        schedule.request_actress_fill()
        wait_for(lambda: calls.count(('fill',)) == 2)
        assert calls == [('run',), ('fill',), ('fill',)]

    def test_an_error_in_the_run_is_only_logged(self, calls, monkeypatch, caplog):
        def broken():
            raise RuntimeError('数据库被占用')

        monkeypatch.setattr(backfill, 'run', broken)
        schedule.queue_job()
        record, = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert record.getMessage() == '抓排队的影片出错'
        assert calls == [('fill',)]

    def test_a_run_that_meets_a_busy_database_only_logs(self, calls, monkeypatch, caplog):
        def busy():
            raise OperationalError('database is locked')

        monkeypatch.setattr(backfill, 'run', busy)
        schedule.queue_job()
        assert warnings_in(caplog) == ['抓排队的影片没有做成：数据库正被别的程序占用（等了好一会儿还没轮到），下一轮照常进行']
        assert calls == [('fill',)]

    def test_a_run_refused_at_once_does_not_say_it_waited(self, calls, monkeypatch, caplog):
        monkeypatch.setattr(backfill, 'run', raising(refused_at_once()))
        schedule.queue_job()
        assert warnings_in(caplog) == [
            '抓排队的影片没有做成：数据库正被别的程序占用，一开始就没有拿到写锁（没有等），下一轮照常进行']

    def test_a_run_the_busy_database_stopped_is_queued_again_at_once(self, scheduler, calls, monkeypatch, caplog):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)
        monkeypatch.setattr(backfill, 'run', raising(OperationalError('database is locked')))
        assert schedule.queue_state() == (None, None)
        schedule.queue_job()
        assert schedule.queue_state() == ('queued', None)
        assert warnings_in(caplog) == ['抓排队的影片没有做成：数据库正被别的程序占用（等了好一会儿还没轮到），马上再排一轮']

    def test_the_log_of_a_run_refused_at_once_and_queued_again_says_both(self, scheduler, calls, monkeypatch, caplog):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)
        monkeypatch.setattr(backfill, 'run', raising(refused_at_once()))
        schedule.queue_job()
        assert warnings_in(caplog) == [
            '抓排队的影片没有做成：数据库正被别的程序占用，一开始就没有拿到写锁（没有等），马上再排一轮']

    def test_a_database_that_stays_busy_is_not_asked_again_and_again(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)
        monkeypatch.setattr(backfill, 'run', raising(OperationalError('database is locked')))
        schedule.queue_job()
        states = [schedule.queue_state()[0]]
        for _ in range(schedule.MAX_BUSY_RETRIES):
            # 排上的那一次轮到了
            fire_queued_run(scheduler)
            states.append(schedule.queue_state()[0])
        assert states == ['queued'] * schedule.MAX_BUSY_RETRIES + [None]

    def test_a_run_that_gets_through_starts_the_count_again(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, '_busy_retries', schedule.MAX_BUSY_RETRIES)
        schedule.queue_job()
        assert schedule._busy_retries == 0

    def test_a_click_that_queued_a_run_meanwhile_serves_as_the_new_run(self, scheduler, calls, monkeypatch, caplog):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)

        def busy():
            # 这一轮开始以后网页上又点了一次，已经排上了下一轮
            schedule.request_queue_run()
            raise OperationalError('database is locked')

        monkeypatch.setattr(backfill, 'run', busy)
        schedule.queue_job()
        assert len([job for job in scheduler.get_jobs() if job.name == '抓排队的影片']) == 1
        assert schedule._busy_retries == 0
        assert warnings_in(caplog) == ['抓排队的影片没有做成：数据库正被别的程序占用（等了好一会儿还没轮到），马上再排一轮']

    def test_the_run_after_a_download_is_queued_again_too(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)
        monkeypatch.setattr(backfill, 'run', raising(OperationalError('database is locked')))
        schedule.download_job()
        assert schedule.queue_state() == ('queued', None)

    def test_a_busy_run_that_is_queued_again_leaves_the_actress_names_to_the_next_one(
            self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)
        monkeypatch.setattr(backfill, 'run', raising(OperationalError('database is locked')))
        schedule.queue_job()
        for _ in range(schedule.MAX_BUSY_RETRIES - 1):
            fire_queued_run(scheduler)
        # 排上了下一次的几次都没有去补演员名；排不上了的最后一次补一轮
        assert calls == []
        fire_queued_run(scheduler)
        assert calls == [('fill',)]

    def test_a_click_that_queued_a_run_during_a_failed_one_also_leaves_the_actress_names_to_it(
            self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)

        def broken():
            schedule.request_queue_run()
            raise RuntimeError('出错了')

        monkeypatch.setattr(backfill, 'run', broken)
        schedule.queue_job()
        assert calls == [] and schedule.queue_state() == ('queued', None)

    def test_a_database_an_old_version_holds_exclusively_is_told_and_asked_again_only_a_few_times(
            self, scheduler, calls, monkeypatch, caplog):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)

        def held():
            raise db.LockedOut('database is locked (another program holds it exclusively)')

        monkeypatch.setattr(backfill, 'run', held)
        schedule.queue_job()
        states = [schedule.queue_state()[0]]
        for _ in range(schedule.MAX_BUSY_RETRIES):
            fire_queued_run(scheduler)
            states.append(schedule.queue_state()[0])
        assert states == ['queued'] * schedule.MAX_BUSY_RETRIES + [None]
        assert set(warnings_in(caplog)) == {
            f'抓排队的影片没有做成：{db.DATABASE_LOCKED_OUT}，马上再排一轮',
            f'抓排队的影片没有做成：{db.DATABASE_LOCKED_OUT}，下一轮照常进行'}

    def test_the_other_background_jobs_are_never_queued_again_when_the_database_is_busy(
            self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)
        monkeypatch.setattr(schedule, 'ACTRESS_FILL_DELAY', 3600)
        busy = OperationalError('database is locked')

        def raises(*args, **kwargs):
            raise busy

        monkeypatch.setattr(schedule.actresses, 'migrate', raises)
        monkeypatch.setattr(bus_spider, 'download', raises)
        monkeypatch.setattr(schedule.clf, 'recommend', raises)
        monkeypatch.setattr(schedule.avwiki, 'fill_round', raises)
        queued_before = [job.id for job in scheduler.get_jobs()]
        schedule.upgrade_model_job()
        schedule.download_job()
        schedule.recommend()
        FILL_ACTRESSES()
        assert [job.id for job in scheduler.get_jobs()] == queued_before
        assert schedule.queue_state() == (None, None)

    def test_the_state_tells_that_a_run_is_going_and_since_when(self, not_started, monkeypatch):
        started = datetime(2026, 11, 6, 12, 0, 0)
        monkeypatch.setattr(schedule, 'get_now_time', lambda: started)
        seen = []
        monkeypatch.setattr(backfill, 'run', lambda: seen.append(schedule.queue_state()) or backfill.QueueRun())
        monkeypatch.setattr(schedule, 'fill_actresses', lambda: None)
        assert schedule.queue_state() == (None, None)
        schedule.queue_job()
        assert seen == [('running', started)]
        assert schedule.queue_state() == (None, None)

    def test_the_state_tells_that_a_requested_run_is_queued_until_it_starts(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 0.2)
        seen = []
        monkeypatch.setattr(backfill, 'run', lambda: seen.append(schedule.queue_state()[0]) or backfill.QueueRun())
        schedule.request_queue_run()
        assert schedule.queue_state() == ('queued', None)
        wait_for(lambda: seen)
        assert seen == ['running']
        wait_for(lambda: schedule.queue_state() == (None, None))


STAR = '/star/a1'


def queued_runs(scheduler):
    return [job for job in scheduler.get_jobs() if job.name == '抓排队的影片']


def run_as_the_scheduler_does(scheduler, job):
    '''任务到点了：像调度器那样把它从任务库里拿掉，再在这个线程里跑它登记的函数和参数'''
    scheduler.remove_job(job.id)
    job.func(*job.args, **job.kwargs)


def fire_queued_run(scheduler):
    '''
    排着的那一次抓排队的影片到点了（见 run_as_the_scheduler_does）。
    返回它是排在多少秒以后的（排的时候定的；到点之前每一次只差几毫秒）
    '''
    jobs = queued_runs(scheduler)
    assert len(jobs) == 1, [job.next_run_time for job in jobs]
    wait = (jobs[0].next_run_time - datetime.now(timezone.utc)).total_seconds()
    run_as_the_scheduler_does(scheduler, jobs[0])
    return wait


def seconds_until_queued_run(scheduler):
    job, = queued_runs(scheduler)
    return (job.next_run_time - datetime.now(timezone.utc)).total_seconds()


class TestWaitingForTheDatabaseToBeFree:
    '''
    数据库被别的程序占了很久、马上再排的几次用完以后，用户登记了、没做的事不用等到下一次定时抓取：过几分钟再排一次，
    一次比一次晚、最晚隔三分钟；库一空出来就接着做。库还被占着时再试什么也不做（不进 backfill.run，也就不对网站发请求）。
    一次库忙不管占多久，日志只写几条
    '''

    @pytest.fixture
    def held(self, scheduler, calls, monkeypatch):
        '''
        库被别的程序占着：backfill.run 一调用、先拿写锁，都因为库忙失败。held.free() 是占着的程序放开了
        （之后 run 做成）；held.runs 是 run 被调用的次数
        '''
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 30)
        taken = {'held': True}
        runs = []

        def run():
            runs.append('run')
            if taken['held']:
                raise OperationalError('database is locked')
            return backfill.QueueRun(fetched=1, added=1)

        def ensure_writable():
            if taken['held']:
                raise OperationalError('database is locked')

        monkeypatch.setattr(backfill, 'run', run)
        monkeypatch.setattr(backfill, 'ensure_writable', ensure_writable)
        return types.SimpleNamespace(runs=runs, free=lambda: taken.update(held=False))

    def test_after_the_quick_retries_a_run_is_queued_minutes_later_and_the_waits_stop_growing(self, scheduler, held):
        backfill.request_check(STAR)
        schedule.queue_job()
        waits = [fire_queued_run(scheduler) for _ in range(schedule.MAX_BUSY_RETRIES + 4)]
        # 先是 3 次马上再排（测试里 30 秒），之后 1 分钟、2 分钟、3 分钟，再往后一直是 3 分钟
        assert [round(wait / 10) * 10 for wait in waits] == [30, 30, 30, 60, 120, 180, 180]

    def test_a_database_that_is_still_held_is_not_asked_anything(self, scheduler, held):
        backfill.request_check(STAR)
        schedule.queue_job()
        for _ in range(8):
            fire_queued_run(scheduler)
        # 第一次库忙以后，每一次再试都是先拿写锁、拿不到就什么也不做：run 只被调用过第一回
        assert held.runs == ['run']

    def test_once_the_database_is_free_the_next_run_goes_through_and_ends_the_waiting(self, scheduler, held):
        backfill.request_check(STAR)
        schedule.queue_job()
        for _ in range(6):
            fire_queued_run(scheduler)
        assert schedule.busy_wait() is not None
        held.free()
        fire_queued_run(scheduler)
        assert held.runs == ['run', 'run']
        assert schedule.busy_wait() is None
        assert schedule.queue_state() == (None, None)
        assert queued_runs(scheduler) == []

    def test_what_is_waited_for_is_what_the_user_clicked_and_nothing_else_is_queued(self, scheduler, held):
        # 没有登记了的事：马上再排的几次用完以后不再排
        schedule.queue_job()
        for _ in range(schedule.MAX_BUSY_RETRIES):
            fire_queued_run(scheduler)
        assert queued_runs(scheduler) == []
        assert schedule.busy_wait() is None

    @pytest.mark.parametrize('ending', ['cancel', 'block'])
    def test_what_ended_while_waiting_is_not_waited_for(self, scheduler, held, ending):
        backfill.request_fill(STAR)
        schedule.queue_job()
        for _ in range(schedule.MAX_BUSY_RETRIES):
            fire_queued_run(scheduler)
        assert len(queued_runs(scheduler)) == 1
        if ending == 'cancel':
            backfill.cancel(STAR)
        else:
            backfill.pause('HTTP 403')
        fire_queued_run(scheduler)
        assert queued_runs(scheduler) == []
        assert schedule.busy_wait() is None

    def test_an_error_that_is_not_a_busy_database_ends_the_waiting(self, scheduler, held, monkeypatch):
        backfill.request_check(STAR)
        schedule.queue_job()
        fire_queued_run(scheduler)
        assert schedule.busy_wait() is not None
        monkeypatch.setattr(backfill, 'ensure_writable', lambda: None)
        monkeypatch.setattr(backfill, 'run', raising(RuntimeError('出错了')))
        fire_queued_run(scheduler)
        assert queued_runs(scheduler) == []
        assert schedule.busy_wait() is None

    def test_the_state_stops_saying_it_waits_once_the_write_lock_is_had(self, scheduler, held, monkeypatch):
        backfill.request_check(STAR)
        schedule.queue_job()
        seen = {}

        def got_the_lock():
            seen['at the lock'] = schedule.busy_wait()

        def run():
            seen['going'] = schedule.busy_wait()
            return backfill.QueueRun()

        monkeypatch.setattr(backfill, 'ensure_writable', got_the_lock)
        monkeypatch.setattr(backfill, 'run', run)
        fire_queued_run(scheduler)
        # 等写锁的时候还在等（正在试）；拿到写锁、开始做以后页面上不再写"数据库被占着"
        assert seen['at the lock'] is not None and seen['at the lock'].next_at is None
        assert seen['going'] is None

    def test_a_run_that_meets_the_busy_database_again_says_it_waits_again(self, scheduler, held, monkeypatch):
        backfill.request_check(STAR)
        schedule.queue_job()
        monkeypatch.setattr(backfill, 'ensure_writable', lambda: None)
        fire_queued_run(scheduler)
        # 写锁拿到了，run 又因为库忙失败（run 还是 held 里的那个）：又是在等
        since, next_at = schedule.busy_wait()
        assert next_at is not None

    def test_a_click_brings_the_run_that_waits_forward(self, scheduler, held):
        backfill.request_check(STAR)
        schedule.queue_job()
        for _ in range(schedule.MAX_BUSY_RETRIES):
            fire_queued_run(scheduler)
        assert round(seconds_until_queued_run(scheduler) / 10) * 10 == 60
        assert schedule.request_queue_run() is True
        assert round(seconds_until_queued_run(scheduler) / 10) * 10 == 30
        # 已经排着一次不比这个晚的，不再另排
        assert schedule.request_queue_run() is False
        assert len(queued_runs(scheduler)) == 1

    def test_a_click_that_comes_while_the_database_is_still_held_asks_the_site_for_nothing(self, scheduler, held):
        backfill.request_check(STAR)
        schedule.queue_job()
        for _ in range(schedule.MAX_BUSY_RETRIES + 1):
            fire_queued_run(scheduler)
        schedule.request_queue_run()
        fire_queued_run(scheduler)
        assert held.runs == ['run']

    def test_the_state_tells_since_when_and_when_the_next_try_is(self, scheduler, held, monkeypatch):
        started = datetime(2026, 11, 6, 12, 0, 0)
        monkeypatch.setattr(schedule, 'get_now_time', lambda: started)
        backfill.request_check(STAR)
        assert schedule.busy_wait() is None
        schedule.queue_job()
        since, next_at = schedule.busy_wait()
        assert since == started
        # 下一次再试的时间是本地时间；排的时候是 30 秒以后
        assert abs((next_at - datetime.now()).total_seconds() - 30) < 10

    def test_one_busy_spell_writes_a_few_lines_however_long_it_lasts(self, scheduler, held, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        backfill.request_check(STAR)
        schedule.queue_job()
        for _ in range(12):
            fire_queued_run(scheduler)
        held.free()
        fire_queued_run(scheduler)
        busy = '数据库正被别的程序占用（等了好一会儿还没轮到）'
        first, again = warnings_in(caplog)
        assert first == f'抓排队的影片没有做成：{busy}，马上再排一轮'
        assert again.startswith(f'抓排队的影片还是没有做成：{busy}') and '每隔几分钟再试' in again
        done, = [record.getMessage() for record in caplog.records if '数据库空出来了' in record.getMessage()]
        assert done.startswith('数据库空出来了，抓排队的影片接着做了')

    def test_a_run_that_finds_nothing_left_to_do_says_so_and_not_that_it_went_on(self, scheduler, held, caplog, monkeypatch):
        caplog.set_level(logging.INFO, logger='bustag')
        backfill.request_check(STAR)
        schedule.queue_job()
        fire_queued_run(scheduler)
        monkeypatch.setattr(backfill, 'ensure_writable', lambda: None)
        monkeypatch.setattr(backfill, 'run', lambda: backfill.QueueRun())
        fire_queued_run(scheduler)
        done, = [record.getMessage() for record in caplog.records if '数据库空出来了' in record.getMessage()]
        assert done.startswith('数据库空出来了，没有要接着做的事了') and '接着做了' not in done

    def test_a_different_reason_is_told_again(self, scheduler, held, caplog, monkeypatch):
        backfill.request_check(STAR)
        schedule.queue_job()
        monkeypatch.setattr(backfill, 'ensure_writable', raising(db.LockedOut('database is locked (x)')))
        monkeypatch.setattr(backfill, 'run', raising(db.LockedOut('database is locked (x)')))
        fire_queued_run(scheduler)
        first, second = warnings_in(caplog)
        assert db.DATABASE_BUSY in first and db.DATABASE_LOCKED_OUT in second

    @pytest.mark.parametrize('comes', ['the midnight job', 'a crawl', 'a click'])
    def test_only_one_run_waits_whatever_comes_in_while_it_waits(self, scheduler, held, comes):
        backfill.request_check(STAR)
        schedule.queue_job()
        for _ in range(schedule.MAX_BUSY_RETRIES):
            fire_queued_run(scheduler)
        waiting, = queued_runs(scheduler)
        if comes == 'the midnight job':
            # 第二天 0 点 1 分的任务也是抓排队的影片：它不是排着的这一个，开始时不能把"已经排着一次"的记号去掉
            schedule._plan_tomorrow('limit')
            run_as_the_scheduler_does(scheduler, scheduler.get_job(schedule.TOMORROW_JOB))
        elif comes == 'a crawl':
            schedule.download_job()
        else:
            schedule.request_queue_run()
        assert [job.id for job in queued_runs(scheduler)] == [waiting.id]
        assert schedule.queue_state() == ('queued', None)
        # 之后还是一条链：每一次到点只排下一个
        fire_queued_run(scheduler)
        assert len(queued_runs(scheduler)) == 1
        held.free()
        fire_queued_run(scheduler)
        assert queued_runs(scheduler) == []
        assert schedule.busy_wait() is None and schedule.queue_state() == (None, None)

    def test_the_midnight_job_that_meets_the_busy_database_leaves_the_waiting_run_to_say_when_it_comes(
            self, scheduler, held):
        backfill.request_check(STAR)
        schedule.queue_job()
        for _ in range(schedule.MAX_BUSY_RETRIES):
            fire_queued_run(scheduler)
        schedule._plan_tomorrow('paused')
        run_as_the_scheduler_does(scheduler, scheduler.get_job(schedule.TOMORROW_JOB))
        # 页面上"约几点自动再试"说的还是排着的那一次
        since, next_at = schedule.busy_wait()
        assert next_at is not None and abs((next_at - datetime.now()).total_seconds() - 60) < 10

    def test_a_crawl_that_meets_the_busy_database_while_a_later_run_waits_writes_nothing_more(
            self, scheduler, held, caplog):
        backfill.request_check(STAR)
        schedule.queue_job()
        for _ in range(schedule.MAX_BUSY_RETRIES):
            fire_queued_run(scheduler)
        written = warnings_in(caplog)
        assert len(written) == 2 and '马上再排一轮' in written[0] and '每隔几分钟再试' in written[1]
        for _ in range(3):
            schedule.download_job()
            fire_queued_run(scheduler)
        # 下一次尝试在几分钟以后：不说"马上再排一轮"，也不因为定时抓取撞上了就多写
        assert warnings_in(caplog) == written

    def test_a_crawl_that_meets_the_busy_database_when_nothing_is_waited_for_says_nothing_is_queued(
            self, scheduler, held, caplog):
        backfill.request_fill(STAR)
        schedule.queue_job()
        for _ in range(schedule.MAX_BUSY_RETRIES):
            fire_queued_run(scheduler)
        backfill.cancel(STAR)
        schedule.download_job()
        assert warnings_in(caplog)[-1].endswith('下一轮照常进行')
        assert schedule.busy_wait() is None

    @pytest.mark.parametrize('ending', ['cancelled', 'blocked', 'failed', 'nothing registered'])
    def test_the_quick_tries_are_given_back_when_a_busy_spell_ends_without_going_through(
            self, scheduler, held, monkeypatch, ending):
        busy = raising(OperationalError('database is locked'))
        if ending != 'nothing registered':
            backfill.request_fill(STAR)
        schedule.queue_job()
        for _ in range(schedule.MAX_BUSY_RETRIES):
            fire_queued_run(scheduler)
        if ending == 'cancelled':
            backfill.cancel(STAR)
        elif ending == 'blocked':
            backfill.pause('HTTP 403')
        elif ending == 'failed':
            monkeypatch.setattr(backfill, 'ensure_writable', lambda: None)
            monkeypatch.setattr(backfill, 'run', raising(RuntimeError('出错了')))
        if queued_runs(scheduler):
            fire_queued_run(scheduler)
        assert queued_runs(scheduler) == [] and schedule.busy_wait() is None
        assert schedule._busy_retries == 0
        # 下一次库忙，照样先马上再排几次，不是一上来就隔 1 分钟
        monkeypatch.setattr(backfill, 'ensure_writable', busy)
        monkeypatch.setattr(backfill, 'run', busy)
        backfill.request_check(STAR)
        schedule.queue_job()
        assert round(seconds_until_queued_run(scheduler) / 10) * 10 == 30
        waits = [fire_queued_run(scheduler) for _ in range(schedule.MAX_BUSY_RETRIES + 1)]
        assert [round(wait / 10) * 10 for wait in waits] == [30, 30, 30, 60]


class TestRequestActressFill:
    '''网页请求里登记"有影片要补演员名"：约 2 秒后在后台任务线程里补一轮'''

    def test_scheduler_not_started(self, not_started, caplog):
        assert schedule.request_actress_fill() is False
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_a_round_is_queued_to_start_shortly(self, scheduler):
        before = datetime.now(timezone.utc)
        assert schedule.request_actress_fill() is True
        after = datetime.now(timezone.utc)
        job, = [job for job in scheduler.get_jobs() if job.name == '补演员名' and job.id != 'fill-actresses']
        # 登记那一刻起 ACTRESS_FILL_DELAY 秒：按登记前后各看一次钟，机器再慢也不会误报
        delay = timedelta(seconds=schedule.ACTRESS_FILL_DELAY)
        assert before + delay <= job.next_run_time <= after + delay

    def test_requests_made_before_the_round_starts_share_it(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'ACTRESS_FILL_DELAY', 0.2)
        assert [schedule.request_actress_fill() for _ in range(3)] == [True, False, False]
        wait_for(lambda: calls)
        time.sleep(0.5)
        assert calls == [('fill',)]

    def test_a_request_made_while_a_round_is_running_gets_another_round(self, scheduler, monkeypatch):
        monkeypatch.setattr(schedule, 'ACTRESS_FILL_DELAY', 0)
        rounds = []
        release = threading.Event()

        def fill_actresses():
            rounds.append(threading.current_thread().name)
            if len(rounds) == 1:
                release.wait(timeout=10)

        monkeypatch.setattr(schedule, 'fill_actresses', fill_actresses)
        assert schedule.request_actress_fill() is True
        try:
            wait_for(lambda: rounds)
            # 这一轮已经开始了，轮到它之后才打标的影片不在它的名单里：再排一轮
            assert schedule.request_actress_fill() is True
            assert schedule.request_actress_fill() is False
        finally:
            release.set()
        wait_for(lambda: len(rounds) == 2)
        assert rounds == ['bustag-jobs', 'bustag-jobs']

    def test_waits_for_the_running_download(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 0)
        monkeypatch.setattr(schedule, 'ACTRESS_FILL_DELAY', 0)
        release = threading.Event()

        def run():
            calls.append(('run',))
            release.wait(timeout=10)
            return backfill.QueueRun(added=1)

        monkeypatch.setattr(backfill, 'run', run)
        schedule.request_queue_run()
        try:
            wait_for(lambda: calls)
            schedule.request_actress_fill()
            time.sleep(0.3)
            assert calls == [('run',)]
        finally:
            release.set()
        wait_for(lambda: len(calls) == 4)
        assert calls == [('run',), ('recommend',), ('fill',), ('fill',)]


class TestFillActresses:
    def test_runs_one_round(self, monkeypatch):
        rounds = []
        monkeypatch.setattr(avwiki, 'fill_round', lambda chosen=(): rounds.append('round'))
        schedule.fill_actresses()
        assert rounds == ['round']

    def test_a_round_the_busy_database_stopped_only_warns(self, monkeypatch, caplog):
        def busy(chosen=()):
            raise OperationalError('database is locked')

        monkeypatch.setattr(avwiki, 'fill_round', busy)
        schedule.fill_actresses()
        assert warnings_in(caplog) == ['补演员名没有做成：数据库正被别的程序占用（等了好一会儿还没轮到），下一轮照常进行']
        assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []

    def test_a_round_that_breaks_for_another_reason_still_logs_the_error(self, monkeypatch, caplog):
        def broken(chosen=()):
            raise RuntimeError('出错了')

        monkeypatch.setattr(avwiki, 'fill_round', broken)
        schedule.fill_actresses()
        record, = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert record.getMessage() == '补演员名出错' and record.exc_info is not None

    def test_the_actresses_chosen_for_recommendations_go_with_the_round(self, monkeypatch):
        from bustag.app import pick
        pick.add_names('star', ['演员甲', '演员乙'])
        pick.add_names('tag', ['标签一'])
        seen = []
        monkeypatch.setattr(avwiki, 'fill_round', lambda chosen=(): seen.append(chosen))
        schedule.fill_actresses()
        assert seen == [('演员甲', '演员乙')]

    def test_a_round_that_changed_movies_nobody_tagged_scores_once(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        scorings = []
        monkeypatch.setattr(schedule, 'recommend', lambda: scorings.append('score'))
        monkeypatch.setattr(avwiki, 'fill_round', lambda chosen=(): avwiki.FillRound(filled=3, unlabeled=2))
        schedule.fill_actresses()
        assert scorings == ['score']
        assert '补演员名改了 2 部没有用户打标的影片的演员，重新打一次分' in caplog.messages
        monkeypatch.setattr(avwiki, 'fill_round', lambda chosen=(): avwiki.FillRound(filled=3, unlabeled=0))
        schedule.fill_actresses()
        assert scorings == ['score']

    @pytest.fixture
    def model_to_train_again(self, monkeypatch):
        '''这一轮之后模型要重新训练（校正刚做完、模型比它旧），打标够训练；记下打分和训练'''
        events = []
        monkeypatch.setattr(schedule, 'recommend', lambda: events.append('score'))
        monkeypatch.setattr(schedule, '_retrain_reason', lambda: schedule.ACTRESS_MODEL)
        monkeypatch.setattr(schedule.prepare, 'count_labels', lambda: (190, 40))
        monkeypatch.setattr(schedule.prepare, 'training_problem', lambda likes, dislikes: '')
        monkeypatch.setattr(avwiki, 'fill_round', lambda chosen=(): avwiki.FillRound(corrected=20, unlabeled=4))
        return events

    def test_a_round_after_which_the_model_is_trained_again_scores_only_once(self, model_to_train_again,
                                                                             monkeypatch):
        monkeypatch.setattr(schedule.clf, 'train_and_recommend', lambda: model_to_train_again.append('train, score'))
        schedule.fill_actresses()
        assert model_to_train_again == ['train, score']

    def test_when_training_again_fails_the_round_scores_with_the_model_it_has(self, model_to_train_again,
                                                                              monkeypatch):
        def broken():
            raise ValueError('训练数据不足')

        monkeypatch.setattr(schedule.clf, 'train_and_recommend', broken)
        schedule.fill_actresses()
        assert model_to_train_again == ['score']

    def test_after_the_correction_a_model_trained_before_is_trained_again(self, monkeypatch):
        reasons = []
        monkeypatch.setattr(avwiki, 'fill_round', lambda chosen=(): avwiki.FillRound())
        monkeypatch.setattr(schedule, '_upgrade_model', lambda: reasons.append('looked'))
        schedule.fill_actresses()
        assert reasons == ['looked']

    def test_an_error_in_the_round_is_only_logged(self, monkeypatch, caplog):
        def broken(chosen=()):
            raise RuntimeError('数据库被占用')

        monkeypatch.setattr(avwiki, 'fill_round', broken)
        schedule.fill_actresses()
        record, = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert record.getMessage() == '补演员名出错'
        assert record.exc_info[1].args == ('数据库被占用',)

    def test_the_state_tells_that_a_round_is_running_and_since_when(self, not_started, monkeypatch):
        started = datetime(2026, 11, 6, 12, 0, 0)
        monkeypatch.setattr(schedule, 'get_now_time', lambda: started)
        seen = []
        monkeypatch.setattr(avwiki, 'fill_round', lambda chosen=(): seen.append(schedule.fill_state()))
        assert schedule.fill_state() == (None, None)
        schedule.fill_actresses()
        assert seen == [('running', started)]
        assert schedule.fill_state() == (None, None)

    def test_a_round_that_fails_is_no_longer_running(self, not_started, monkeypatch):
        def broken(chosen=()):
            raise RuntimeError('数据库被占用')

        monkeypatch.setattr(avwiki, 'fill_round', broken)
        schedule.fill_actresses()
        assert schedule.fill_state() == (None, None)

    def test_the_state_tells_that_the_round_after_startup_is_queued(self, not_started, calls):
        started = schedule.start_scheduler()
        try:
            assert schedule.fill_state() == ('queued', None)
        finally:
            started.shutdown(wait=False)

    def test_the_state_tells_that_a_requested_round_is_queued_until_it_starts(self, scheduler, monkeypatch):
        monkeypatch.setattr(schedule, 'ACTRESS_FILL_DELAY', 0.2)
        seen = []
        monkeypatch.setattr(avwiki, 'fill_round', lambda chosen=(): seen.append(schedule.fill_state()[0]))
        monkeypatch.setattr(schedule, 'fill_actresses', FILL_ACTRESSES)
        assert schedule.fill_state() == (None, None)
        schedule.request_actress_fill()
        assert schedule.fill_state() == ('queued', None)
        wait_for(lambda: seen)
        assert seen == ['running']
        wait_for(lambda: schedule.fill_state() == (None, None))

    def test_a_round_that_fails_does_not_stop_later_jobs(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'ACTRESS_FILL_DELAY', 0)
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 0)

        def broken(chosen=()):
            calls.append(('round',))
            raise RuntimeError('数据库被占用')

        monkeypatch.setattr(avwiki, 'fill_round', broken)
        monkeypatch.setattr(schedule, 'fill_actresses', FILL_ACTRESSES)
        schedule.request_actress_fill()
        wait_for(lambda: calls)
        schedule.request_queue_run()
        wait_for(lambda: len(calls) == 4)
        assert calls == [('round',), ('run',), ('recommend',), ('round',)]


def old_model_files(data_dir):
    '''旧版本程序留下的两个模型文件'''
    for name in ('model.pkl', 'label_binarizer.pkl'):
        (data_dir / 'model' / name).write_bytes(b'left by an older version')


def add_tagged(make_item, likes, dislikes):
    '''用户打标的影片："喜欢"的都带 g0 和 s0'''
    with db.db.atomic():
        for n in range(likes + dislikes):
            like = n < likes
            fanhao = f'TAG-{n:03d}'
            make_item(fanhao, tags=[('genre', f'g{1 + n % 9}')] + ([('genre', 'g0'), ('star', 's0')] if like else []))
            ItemRate.saveit(RATE_TYPE.USER_RATE, int(like), fanhao)


class TestUpgradeModel:
    '''启动后的一次性任务：数据目录里只有旧版本程序训练的模型时，打标够就自动重新训练并打分'''

    def test_old_model_is_retrained_and_the_movies_are_scored_again(self, data_dir, make_item, caplog):
        add_tagged(make_item, likes=190, dislikes=40)
        for n in range(25):
            make_item(f'NEW-{n:03d}', tags=[('genre', 'g0'), ('star', 's0')] if n < 5 else [('genre', f'g{1 + n % 9}')])
            # 旧模型把没打标的全判成了喜欢
            ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, 1, f'NEW-{n:03d}')
        old_model_files(data_dir)
        caplog.set_level(logging.INFO, logger='bustag')
        schedule.upgrade_model_job()
        assert caplog.messages[0] == '现有的模型文件是旧版本程序训练的，新版本换了模型：用现有的打标自动重新训练，并给影片重新打分'
        assert '模型训练完成' in caplog.messages
        assert '打分完成：给 25 部没有用户打标的影片打了分，推荐 20 部，不太像 5 部' in caplog.messages
        assert (data_dir / MODEL_FILE).is_file()
        system = ItemRate.select().where(ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE)
        assert sum(rate.rate_value for rate in system) == 20
        assert db.ItemScore.select().count() == 25
        # 再启动一次：已经是新模型，不再训练
        caplog.clear()
        schedule.upgrade_model_job()
        assert caplog.messages == []

    def test_old_model_without_enough_tags_is_only_reported(self, data_dir, make_item, caplog):
        add_tagged(make_item, likes=3, dislikes=0)
        make_item('NEW-001', tags=[('genre', 'g1')])
        old_model_files(data_dir)
        caplog.set_level(logging.INFO, logger='bustag')
        schedule.upgrade_model_job()
        # 只有这一句：不会先说一遍"自动重新训练"
        assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
            ('WARNING', '现有的模型文件是旧版本程序训练的，新版本换了模型；没有自动重新训练：训练数据不足, 无法训练模型. 需要200, 当前3。'
                        '打标够了以后请到网页的"模型"页点击"开始训练"')]
        assert not (data_dir / MODEL_FILE).exists()
        assert ItemRate.get_by_fanhao('NEW-001') is None

    def test_old_model_that_cannot_be_replaced_is_only_reported(self, data_dir, make_item, caplog, monkeypatch):
        add_tagged(make_item, likes=190, dislikes=40)
        old_model_files(data_dir)

        def disk_full(path, write):
            raise OSError(28, '磁盘空间不足')

        monkeypatch.setattr(util, 'write_temp_file', disk_full)
        schedule.upgrade_model_job()
        warnings = warnings_in(caplog)
        assert len(warnings) == 1
        assert warnings[0].startswith('自动重新训练没有完成：无法保存模型文件到 ')
        assert warnings[0].endswith('请到网页的"模型"页点击"开始训练"再试一次')
        assert not (data_dir / MODEL_FILE).exists()

    def test_the_upgrade_is_pending_from_startup_until_the_job_is_done(self, not_started, calls, data_dir, make_item,
                                                                       monkeypatch):
        add_tagged(make_item, likes=190, dislikes=40)
        old_model_files(data_dir)
        training, release = threading.Event(), threading.Event()

        def slow_training():
            training.set()
            release.wait(timeout=10)

        monkeypatch.setattr(schedule.clf, 'train_and_recommend', slow_training)
        assert schedule.upgrade_pending() is False
        started = schedule.start_scheduler()
        try:
            # 排上了、正在训练，都算还没做完；做完就不算了
            assert schedule.upgrade_pending() is True
            assert training.wait(timeout=10)
            assert schedule.upgrade_pending() is True
            release.set()
            wait_for(lambda: not schedule.upgrade_pending())
        finally:
            release.set()
            started.shutdown(wait=False)

    def test_an_upgrade_that_fails_is_no_longer_pending(self, monkeypatch):
        def broken():
            raise RuntimeError('模型目录读不了')

        monkeypatch.setattr(schedule, '_upgrade_pending', True)
        monkeypatch.setattr(schedule, '_retrain_reason', broken)
        with pytest.raises(RuntimeError):
            schedule.upgrade_model_job()
        assert schedule.upgrade_pending() is False

    def test_a_database_held_by_another_program_leaves_the_scoring_to_the_next_download(self, data_dir, make_item,
                                                                                        caplog, locked_database):
        add_tagged(make_item, likes=190, dislikes=40)
        make_item('NEW-001', tags=[('genre', 'g0'), ('star', 's0')])
        old_model_files(data_dir)
        with locked_database():
            schedule.upgrade_model_job()
        assert (data_dir / MODEL_FILE).is_file()
        assert warnings_in(caplog) == [
            '模型已保存，但数据库正被别的程序占用（等了好一会儿还没轮到），影片还没有重新打分：下一次抓取后会自动打分']
        assert ItemRate.get_by_fanhao('NEW-001') is None

    @pytest.mark.parametrize('state', ['no-model', 'current-model', 'broken-model', 'current-model-next-to-old-files'])
    def test_nothing_is_trained_unless_only_old_files_are_there(self, data_dir, write_model, monkeypatch, caplog, state):
        trainings = []
        monkeypatch.setattr(schedule.clf, 'train_and_recommend', lambda: trainings.append('train'))
        if state.startswith('current-model'):
            write_model()
        if state == 'broken-model':
            (data_dir / MODEL_FILE).write_bytes(b'garbage')
        if state.endswith('old-files'):
            old_model_files(data_dir)
        caplog.set_level(logging.INFO, logger='bustag')
        schedule.upgrade_model_job()
        assert trainings == []
        assert caplog.messages == []

    def legacy_names(self, cast):
        '''一部按旧的存法补过名字的影片：一位女优的三个名字各是一条 star 标签'''
        cast.movie('OLD-001')
        ItemRate.saveit(RATE_TYPE.USER_RATE, 1, 'OLD-001')
        cast.legacy('OLD-001', 'jia', '演员甲', 'えんいんこう', 'Actor A')
        db.save_lookup('OLD-001', db.LOOKUP_FILLED, 0, datetime(2026, 11, 1), datetime(2026, 11, 1))

    def test_the_actresses_are_upgraded_then_a_model_trained_before_is_trained_again(
            self, data_dir, make_item, cast, write_model, caplog):
        add_tagged(make_item, likes=190, dislikes=40)
        self.legacy_names(cast)
        write_model()
        caplog.set_level(logging.INFO, logger='bustag')
        schedule.upgrade_model_job()
        assert db.Actress.select().count() == 1
        assert cast.stars_of('OLD-001') == ['演员甲']
        reason = '演员的存法升级了（每位女优只留一个正式名字），现有的模型是升级前训练的'
        assert f'{reason}：用现有的打标自动重新训练，并给影片重新打分' in caplog.messages
        assert '模型训练完成' in caplog.messages
        assert schedule.clf.load().trained_at >= util.format_datetime(
            db.JobState.get(db.JobState.name == 'actress-storage').at)
        # 再启动一次：不再迁移，也不再训练
        caplog.clear()
        schedule.upgrade_model_job()
        assert caplog.messages == []

    def test_tidying_the_few_movies_an_older_version_wrote_does_not_train_again(self, data_dir, make_item, cast, caplog,
                                                                                  write_model, monkeypatch):
        add_tagged(make_item, likes=190, dislikes=40)
        self.legacy_names(cast)
        write_model()
        schedule.upgrade_model_job()
        storage = db.JobState.get(db.JobState.name == 'actress-storage').at
        db.JobState.create(name='actress-fixed', text='done', at=storage)
        trained = schedule.clf.load().trained_at
        # 降级用过旧版本：它按旧存法给一部喜欢的影片补了名字；过些日子换回新版本
        cast.movie('OLD-002')
        ItemRate.saveit(RATE_TYPE.USER_RATE, 1, 'OLD-002')
        cast.legacy('OLD-002', 'jia', '演员甲', 'Actor A')
        db.save_lookup('OLD-002', db.LOOKUP_FILLED, 0, datetime(2026, 11, 1), datetime(2026, 11, 1))
        monkeypatch.setattr(schedule.actresses, 'get_now_time', lambda: datetime(2099, 1, 1))
        trainings = []
        monkeypatch.setattr(schedule.clf, 'train_and_recommend', lambda: trainings.append('train'))
        caplog.set_level(logging.INFO, logger='bustag')
        caplog.clear()
        schedule.upgrade_model_job()
        assert cast.stars_of('OLD-002') == ['演员甲']
        assert [message for message in caplog.messages if '又整理了一次' in message]
        assert trainings == [] and schedule._retrain_reason() is None
        assert schedule.clf.load().trained_at == trained

    def test_without_enough_tags_the_upgrade_only_warns_about_the_model(self, data_dir, make_item, cast, write_model,
                                                                          caplog):
        add_tagged(make_item, likes=3, dislikes=0)
        self.legacy_names(cast)
        write_model()
        schedule.upgrade_model_job()
        assert db.Actress.select().count() == 1
        assert warnings_in(caplog) == [
            '演员的存法升级了（每位女优只留一个正式名字），现有的模型是升级前训练的；没有自动重新训练：'
            '训练数据不足, 无法训练模型. 需要200, 当前4。打标够了以后请到网页的"模型"页点击"开始训练"']
        # 每一轮补演员名之后都会再看一次模型：同一个原因只说一遍
        caplog.clear()
        schedule._upgrade_model()
        assert warnings_in(caplog) == []

    def test_a_model_trained_after_the_upgrade_is_left_alone(self, data_dir, make_item, cast, write_model,
                                                             monkeypatch):
        self.legacy_names(cast)
        schedule.upgrade_model_job()
        write_model()
        model = persist.read_model(str(data_dir / MODEL_FILE))
        persist.save_model(str(data_dir / MODEL_FILE), persist.SavedModel(
            model.intercept, model.tags, model.report, '2099-01-01 00:00:00'))
        trainings = []
        monkeypatch.setattr(schedule.clf, 'train_and_recommend', lambda: trainings.append('train'))
        schedule.upgrade_model_job()
        assert trainings == []

    def test_an_upgrade_that_failed_is_reported_and_the_model_is_still_looked_at(self, data_dir, make_item, cast,
                                                                                 monkeypatch, caplog):
        self.legacy_names(cast)
        old_model_files(data_dir)
        add_tagged(make_item, likes=3, dislikes=0)

        def disk_full(path):
            raise OSError(28, '磁盘空间不足')

        monkeypatch.setattr(schedule.actresses, '_snapshot', disk_full)
        schedule.upgrade_model_job()
        assert db.Actress.select().count() == 0
        warnings = warnings_in(caplog)
        assert warnings[0] == ('演员数据没有升级：升级前的快照没有做成（磁盘空间不足）。'
                               '程序照常可用，开着的话明天再试一次，下次启动时也会再试；在那之前不去 av-wiki 补演员名')
        assert warnings[1].startswith('现有的模型文件是旧版本程序训练的')

    def test_runs_once_right_after_startup_then_actresses_are_filled_before_the_first_download(
            self, not_started, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'STARTUP_FILL_DELAY', 0.1)
        monkeypatch.setattr(schedule, 'FIRST_DOWNLOAD_DELAY', 0.4)
        monkeypatch.setattr(schedule, 'upgrade_model_job', lambda: calls.append(('upgrade',)))
        started = schedule.start_scheduler()
        try:
            wait_for(lambda: len(calls) == 6)
            # 第一次定时抓取也把上次没抓完的排队影片接着抓
            assert calls == [('upgrade',), ('fill',), ('download',), ('run',), ('recommend',), ('fill',)]
            # 一次性的任务，执行过就不在了
            assert [job.id for job in started.get_jobs()] == ['download']
        finally:
            started.shutdown(wait=False)

    def test_a_request_made_before_the_startup_round_shares_it(self, not_started, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'STARTUP_FILL_DELAY', 0.3)
        started = schedule.start_scheduler()
        try:
            assert schedule.request_actress_fill() is False
            wait_for(lambda: calls)
            time.sleep(0.3)
            assert calls == [('fill',)]
        finally:
            started.shutdown(wait=False)


class TestTryingTheUpgradeAgain:
    '''演员数据的升级没做成、程序一直开着：每天第一轮补演员名之前再试一次，库被占着时只等一小会儿'''

    TODAY = datetime(2026, 11, 12, 1, 0, 0)

    @pytest.fixture
    def failed_yesterday(self, cast, monkeypatch):
        cast.movie('OLD-001')
        ItemRate.saveit(RATE_TYPE.USER_RATE, 1, 'OLD-001')
        cast.legacy('OLD-001', 'jia', '演员甲', 'えんいんこう')
        db.save_lookup('OLD-001', db.LOOKUP_FILLED, 0, datetime(2026, 11, 1), datetime(2026, 11, 1))
        monkeypatch.setattr(schedule, 'get_now_time', lambda: self.TODAY)
        monkeypatch.setattr(schedule.actresses, 'get_now_time', lambda: self.TODAY)
        monkeypatch.setattr(schedule.actresses, 'last_migration', schedule.actresses.Migration(
            'failed', datetime(2026, 11, 11, 9, 0, 0), reason='数据库正被别的程序占用（等了好一会儿还没轮到）'))
        rounds = []
        monkeypatch.setattr(avwiki, 'fill_round', lambda chosen=(): rounds.append(schedule.actresses.migrated()))
        return rounds

    def snapshots(self, data_dir):
        return sorted(path.name for path in data_dir.iterdir() if path.name.startswith('bus-before'))

    def test_it_is_tried_again_before_the_first_round_of_the_day(self, failed_yesterday, data_dir, cast):
        schedule.fill_actresses()
        # 这一轮补名开始时已经是新存法了
        assert failed_yesterday == [True]
        assert schedule.actresses.last_migration.state == 'done'
        assert cast.stars_of('OLD-001') == ['演员甲']
        assert self.snapshots(data_dir) == ['bus-before-actress-upgrade-20261112.db']

    def test_it_is_tried_once_a_day(self, failed_yesterday, monkeypatch):
        tries = []
        monkeypatch.setattr(schedule.actresses, 'migrate', lambda wait=None: tries.append(wait))
        schedule.fill_actresses()
        assert tries == [schedule.RETRY_WAIT]
        monkeypatch.setattr(schedule.actresses, 'last_migration', schedule.actresses.Migration('failed', self.TODAY))
        schedule.fill_actresses()
        assert tries == [schedule.RETRY_WAIT]

    def test_nothing_is_tried_when_the_upgrade_went_through(self, failed_yesterday, monkeypatch):
        tries = []
        monkeypatch.setattr(schedule.actresses, 'migrate', lambda wait=None: tries.append(wait))
        monkeypatch.setattr(schedule.actresses, 'last_migration', schedule.actresses.Migration(
            'done', datetime(2026, 11, 11, 9, 0, 0)))
        schedule.fill_actresses()
        assert tries == []

    def test_a_database_still_held_is_given_up_quickly_without_a_snapshot(self, failed_yesterday, data_dir, cast):
        other = sqlite3.connect(db.db.database, isolation_level=None)
        other.execute('BEGIN IMMEDIATE')
        try:
            began = time.monotonic()
            schedule.fill_actresses()
            took = time.monotonic() - began
        finally:
            other.execute('ROLLBACK')
            other.close()
        # 只等那几秒，不等满平常的 30 秒；界限放宽到慢的机器上也不会误报
        assert took < db.BUSY_TIMEOUT / 2
        migration = schedule.actresses.last_migration
        assert (migration.state, migration.at) == ('failed', self.TODAY)
        # 说的是按钟表量的秒数，至少是它等的那几秒
        waited = re.fullmatch(r'数据库正被别的程序占用（等了 (\d+) 秒还没轮到）', migration.reason)
        assert waited and int(waited.group(1)) >= schedule.RETRY_WAIT
        assert self.snapshots(data_dir) == []
        assert cast.stars_of('OLD-001') == ['演员甲', 'えんいんこう']
        assert db.db.execute_sql('PRAGMA busy_timeout').fetchone()[0] == db.BUSY_TIMEOUT * 1000
        assert failed_yesterday == [False]

    def test_a_download_the_busy_database_cut_short_only_puts_the_try_off_to_the_next_download(
            self, failed_yesterday, not_started, monkeypatch):
        # 库被占着时那一轮抓排队的影片没做成、不去补演员名，演员数据升级的再试也跟着留到下一次定时抓取，没有被饿掉
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)
        monkeypatch.setattr(bus_spider, 'download', lambda: DownloadResult(added=1))
        monkeypatch.setattr(schedule, 'recommend', lambda: None)
        tries = []
        # 启动后的升级（没有等待时间）由调度器自己去做，这里只数每天再试的那一种
        monkeypatch.setattr(schedule.actresses, 'migrate', lambda wait=None: wait and tries.append(wait))
        errors = [OperationalError('database is locked')]

        def run():
            if errors:
                raise errors.pop()
            return backfill.QueueRun(fetched=1, added=1)

        monkeypatch.setattr(backfill, 'run', run)
        started = schedule.start_scheduler()
        try:
            schedule.download_job()
            assert (tries, failed_yesterday) == ([], [])
            schedule.download_job()
        finally:
            started.shutdown(wait=False)
        assert tries == [schedule.RETRY_WAIT]
        assert len(failed_yesterday) == 1

    def test_a_check_that_meets_a_locked_database_is_a_failed_try(self, failed_yesterday, monkeypatch):
        def locked():
            raise OperationalError('database is locked')

        monkeypatch.setattr(schedule.actresses, 'migrated', locked)
        schedule.fill_actresses()
        assert schedule.actresses.last_migration.state == 'failed'
        assert schedule.actresses.last_migration.at == self.TODAY


class TestJobs:
    def test_a_job_that_meets_an_old_database_held_exclusively_says_so(self, calls, monkeypatch, caplog):
        def held():
            raise db.LockedOut('database is locked (another program holds it exclusively)')

        monkeypatch.setattr(bus_spider, 'download', held)
        schedule.download_job()
        assert f'定时抓取没有做成：{db.DATABASE_LOCKED_OUT}，下一轮照常进行' in warnings_in(caplog)

    def test_download_job_downloads_runs_the_queue_scores_once_then_fills_actresses(self, calls):
        schedule.download_job()
        assert calls == [('download',), ('run',), ('recommend',), ('fill',)]

    def test_download_job_remembers_how_the_download_went(self, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'last_download', None)
        monkeypatch.setattr(schedule, 'get_now_time', lambda: datetime(2026, 11, 6, 12, 0, 0))
        schedule.download_job()
        assert schedule.last_download == (datetime(2026, 11, 6, 12, 0, 0), '抓取结束：新增 3 部，跳过 0 部，失败 0 部，用时 0 秒')

    def test_the_state_tells_that_a_download_is_running_and_since_when(self, calls, monkeypatch):
        started = datetime(2026, 11, 6, 12, 0, 0)
        monkeypatch.setattr(schedule, 'get_now_time', lambda: started)
        seen = []

        def download():
            seen.append(schedule.download_state())
            return DownloadResult(added=1)

        monkeypatch.setattr(bus_spider, 'download', download)
        # 抓完以后同一个任务里还有打分和补演员名：那时抓取已经结束了
        monkeypatch.setattr(schedule, 'recommend', lambda: seen.append(schedule.download_state()))
        assert schedule.download_state() == (None, None)
        schedule.download_job()
        assert seen == [('running', started), (None, None)]
        assert schedule.download_state() == (None, None)

    @pytest.mark.parametrize('error', [OperationalError('database is locked'), RuntimeError('意料之外的错')])
    def test_a_download_that_fails_is_no_longer_running(self, calls, monkeypatch, error):
        def broken():
            raise error

        monkeypatch.setattr(bus_spider, 'download', broken)
        try:
            schedule.download_job()
        except RuntimeError:
            pass
        assert schedule.download_state() == (None, None)

    def test_recommend_skips_without_model(self, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        schedule.recommend()
        assert '还没有训练好的模型，跳过推荐' in caplog.messages

    def test_recommend_with_unreadable_model_only_logs(self, data_dir, make_item, caplog):
        make_item('ABC-001', tags=[('genre', 'g1')])
        (data_dir / MODEL_FILE).write_bytes(b'garbage')
        schedule.recommend()
        assert any(m.startswith('无法推荐：模型文件 model.json 无法读取') and m.endswith('重新训练')
                   for m in caplog.messages)
        assert ItemRate.get_by_fanhao('ABC-001') is None

    def test_recommend_with_model_files_of_an_older_version_only_logs(self, data_dir, make_item, caplog):
        make_item('ABC-001', tags=[('genre', 'g1')])
        (data_dir / 'model' / 'model.pkl').write_bytes(b'left by an older version')
        schedule.recommend()
        assert f'无法推荐：{LEGACY_MODEL}' in caplog.messages
        assert ItemRate.get_by_fanhao('ABC-001') is None

    def test_recommend_with_the_database_held_by_another_program_only_logs(self, write_model, make_item, caplog,
                                                                           locked_database):
        make_item('ABC-001', tags=[('genre', 'g1')])
        write_model()
        with locked_database():
            schedule.recommend()
        assert warnings_in(caplog) == ['抓取后的打分没有做成：数据库正被别的程序占用（等了好一会儿还没轮到），下一轮照常进行']
        assert ItemRate.get_by_fanhao('ABC-001') is None

    def test_a_download_that_meets_a_busy_database_only_logs_and_the_rest_of_the_job_goes_on(self, calls, monkeypatch,
                                                                                             caplog):
        def busy():
            calls.append(('download',))
            raise OperationalError('database is locked')

        monkeypatch.setattr(bus_spider, 'download', busy)
        monkeypatch.setattr(schedule, 'last_download', None)
        schedule.download_job()
        assert calls == [('download',), ('run',), ('recommend',), ('fill',)]
        assert warnings_in(caplog) == ['定时抓取没有做成：数据库正被别的程序占用（等了好一会儿还没轮到），下一轮照常进行']
        assert schedule.last_download is None

    def test_a_download_whose_queue_run_the_busy_database_stopped_leaves_the_actress_names_to_the_next_run(
            self, scheduler, calls, monkeypatch):
        # 抓排队的影片没做成、下一次已经排上了：同一轮不再去 av-wiki 补演员名（请求发出去了结果也写不进库）
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)
        monkeypatch.setattr(backfill, 'run', raising(OperationalError('database is locked')))
        schedule.download_job()
        assert calls == [('download',), ('recommend',)]
        assert schedule.queue_state() == ('queued', None)

    def test_a_download_whose_queue_run_failed_for_another_reason_still_fills_the_actress_names(
            self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)
        monkeypatch.setattr(backfill, 'run', raising(RuntimeError('出错了')))
        schedule.download_job()
        assert calls == [('download',), ('recommend',), ('fill',)]

    def test_a_blocked_download_still_fills_the_actress_names_when_a_click_queued_a_run(
            self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'QUEUE_DELAY', 3600)
        monkeypatch.setattr(bus_spider, 'download', lambda: DownloadResult(blocked='HTTP 403：访问被拒绝'))
        schedule.request_queue_run()
        schedule.download_job()
        assert calls == [('recommend',), ('fill',)]

    def test_a_blocked_download_leaves_the_queue_alone_and_pauses_the_backfill(self, calls, monkeypatch):
        def blocked():
            calls.append(('download',))
            return DownloadResult(blocked='HTTP 403：访问被拒绝')

        monkeypatch.setattr(bus_spider, 'download', blocked)
        schedule.download_job()
        assert calls == [('download',), ('recommend',), ('fill',)]
        assert backfill.paused()[0] == 'HTTP 403：访问被拒绝'

    def test_a_blocked_download_also_drops_the_actress_pages_that_were_waiting_to_be_read(self, calls, monkeypatch):
        db.ListSource.create(path='/star/a1', wanted=60)
        monkeypatch.setattr(bus_spider, 'download', lambda: DownloadResult(blocked='HTTP 403：访问被拒绝'))
        schedule.download_job()
        waiting = db.ListSource.get()
        assert (waiting.wanted, waiting.error) == (0, '被网站拦截，这次没有翻她的演员页，要继续请再点一次补全')


class TestTomorrow:
    '''因为今天的额度用完、或者被拦截暂停，还有按演员补全的在排队：第二天 0 点 1 分接着抓'''

    @pytest.fixture
    def afternoon(self, monkeypatch):
        '''今天下午三点半（真实的日期：排出来的任务要在将来，过去的时刻调度器会马上执行）；返回明天 0 点 1 分'''
        today = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
        monkeypatch.setattr(schedule, 'get_now_time', lambda: today)
        return datetime.combine(today.date() + timedelta(days=1), datetime.min.time()).replace(minute=1).astimezone()

    @pytest.mark.parametrize('held', ['limit', 'paused'])
    def test_a_run_held_back_comes_back_after_midnight(self, scheduler, calls, afternoon, monkeypatch, held):
        monkeypatch.setattr(backfill, 'run', lambda: backfill.QueueRun(left=3, left_list=3, held=held))
        schedule.queue_job()
        job = scheduler.get_job('queue-tomorrow')
        assert job.next_run_time == afternoon
        assert job.name == '抓排队的影片'

    def test_planning_it_twice_keeps_one_job(self, scheduler, calls, afternoon, monkeypatch):
        monkeypatch.setattr(backfill, 'run', lambda: backfill.QueueRun(left=3, left_list=3, held='limit'))
        schedule.queue_job()
        schedule.download_job()
        assert len([job for job in scheduler.get_jobs() if job.id == 'queue-tomorrow']) == 1

    def test_nothing_held_back_plans_nothing(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(backfill, 'run', lambda: backfill.QueueRun(left=2, left_list=0))
        schedule.queue_job()
        assert scheduler.get_job('queue-tomorrow') is None

    def test_without_a_scheduler_nothing_is_planned(self, not_started, calls, monkeypatch):
        monkeypatch.setattr(backfill, 'run', lambda: backfill.QueueRun(left=3, left_list=3, held='limit'))
        schedule.queue_job()
        assert schedule.scheduler is None

    def test_other_database_errors_in_a_job_are_not_hidden(self, calls, monkeypatch):
        def broken():
            raise OperationalError('no such table: item')

        monkeypatch.setattr(bus_spider, 'download', broken)
        with pytest.raises(OperationalError, match='no such table'):
            schedule.download_job()

    def test_recommend_scores_the_movies_the_user_has_not_tagged(self, write_model, make_item, caplog):
        make_item('ABC-001', tags=[('genre', 'g1')])
        make_item('ABC-002', tags=[('star', 's1')])
        write_model()
        caplog.set_level(logging.INFO, logger='bustag')
        schedule.recommend()
        assert '打分完成：给 2 部没有用户打标的影片打了分，推荐 2 部，不太像 0 部' in caplog.messages
        assert ItemRate.select().count() == 2
