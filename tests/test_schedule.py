'''
定时任务：只启动一次、按番号抓取、所有任务在一个守护线程里按顺序执行、出错不影响后面的任务。
抓取和推荐都换成记录调用的假函数；定时抓取的首次运行推迟到一小时后，测试期间不会触发
'''
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from bustag import util
from bustag.app import schedule
from bustag.model.persist import MODEL_FILE
from bustag.spider import bus_spider


def wait_for(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, '等待超时'
        time.sleep(0.01)


@pytest.fixture
def calls(monkeypatch):
    '''按发生顺序记录 download / download_fanhaos / recommend 的调用'''
    log = []
    monkeypatch.setattr(bus_spider, 'download', lambda: log.append(('download',)))
    monkeypatch.setattr(bus_spider, 'download_fanhaos', lambda fanhaos: log.append(('download_fanhaos', fanhaos)))
    monkeypatch.setattr(schedule, 'recommend', lambda: log.append(('recommend',)))
    return log


@pytest.fixture
def not_started(monkeypatch):
    monkeypatch.setattr(schedule, 'scheduler', None)
    monkeypatch.setattr(schedule, 'FIRST_DOWNLOAD_DELAY', 3600)


@pytest.fixture
def scheduler(not_started, calls):
    started = schedule.start_scheduler()
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


class TestAddDownloadJob:
    def test_nothing_to_download(self, not_started, caplog):
        assert schedule.add_download_job([]) is None
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_scheduler_not_started(self, not_started, caplog):
        assert schedule.add_download_job(['ABC-001', 'ABC-002']) is None
        assert '定时任务没有启动，2 个番号没有加入下载队列' in caplog.messages

    def test_job_starts_after_a_short_delay(self, scheduler):
        before = datetime.now(timezone.utc)
        job = schedule.add_download_job(['ABC-001'])
        assert job.name == '按番号抓取'
        assert 1.5 <= (job.next_run_time - before).total_seconds() <= 3

    def test_job_downloads_then_recommends(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'FANHAO_DOWNLOAD_DELAY', 0)
        job = schedule.add_download_job(iter(['ABC-001', 'ABC-002']))
        assert job.args == (['ABC-001', 'ABC-002'],)
        wait_for(lambda: len(calls) == 2)
        assert calls == [('download_fanhaos', ['ABC-001', 'ABC-002']), ('recommend',)]

    def test_jobs_run_one_at_a_time_in_a_daemon_thread(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'FANHAO_DOWNLOAD_DELAY', 0)
        events = []
        release = threading.Event()

        def download_fanhaos(fanhaos):
            thread = threading.current_thread()
            events.append(('start', fanhaos[0], thread.name, thread.daemon))
            if fanhaos == ['ABC-001']:
                release.wait(timeout=10)
            events.append(('end', fanhaos[0]))

        monkeypatch.setattr(bus_spider, 'download_fanhaos', download_fanhaos)
        schedule.add_download_job(['ABC-001'])
        try:
            wait_for(lambda: events)
            schedule.add_download_job(['ABC-002'])
            time.sleep(0.3)
            assert events == [('start', 'ABC-001', 'bustag-jobs', True)]
        finally:
            release.set()
        # 等两个任务都推荐完再结束，免得任务线程在假函数还原之后才调用 recommend
        wait_for(lambda: len(calls) == 2)
        assert events == [('start', 'ABC-001', 'bustag-jobs', True), ('end', 'ABC-001'),
                          ('start', 'ABC-002', 'bustag-jobs', True), ('end', 'ABC-002')]
        assert calls == [('recommend',), ('recommend',)]

    def test_failing_job_does_not_stop_later_jobs(self, scheduler, calls, monkeypatch):
        monkeypatch.setattr(schedule, 'FANHAO_DOWNLOAD_DELAY', 0)

        def download_fanhaos(fanhaos):
            calls.append(('download_fanhaos', fanhaos))
            if fanhaos == ['ABC-001']:
                raise RuntimeError('抓取出错')

        monkeypatch.setattr(bus_spider, 'download_fanhaos', download_fanhaos)
        schedule.add_download_job(['ABC-001'])
        wait_for(lambda: calls)
        schedule.add_download_job(['ABC-002'])
        wait_for(lambda: ('recommend',) in calls)
        assert calls == [('download_fanhaos', ['ABC-001']), ('download_fanhaos', ['ABC-002']), ('recommend',)]


class TestJobs:
    def test_download_job_downloads_then_recommends(self, calls):
        schedule.download_job()
        assert calls == [('download',), ('recommend',)]

    def test_recommend_skips_without_model(self, caplog):
        caplog.set_level(logging.INFO, logger='bustag')
        schedule.recommend()
        assert '还没有训练好的模型，跳过推荐' in caplog.messages

    def test_recommend_with_unreadable_model_only_logs(self, data_dir, caplog):
        (data_dir / MODEL_FILE).write_bytes(b'garbage')
        schedule.recommend()
        assert any(m.startswith('无法推荐：模型文件 model.pkl 无法读取') and m.endswith('重新训练')
                   for m in caplog.messages)
