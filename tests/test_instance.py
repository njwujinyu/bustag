'''
数据目录的锁：同一个数据目录同时只让一个网页服务用。用真的文件锁，"另一个进程"有的用另开的文件句柄代替
（操作系统按句柄算锁，同一个进程里另开一个句柄一样锁不上），有的真的起一个子进程
'''
import errno
import json
import logging
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bustag import util
from bustag.app import instance

ROOT = Path(__file__).resolve().parent.parent
# 另一个进程：锁上数据目录，打印自己的进程号，然后一直等着
HOLDER = ('import os, sys\n'
          'from bustag.app import instance\n'
          'instance.lock_data_dir("http://127.0.0.1:8123")\n'
          'print(os.getpid(), flush=True)\n'
          'sys.stdin.read()\n')


@pytest.fixture(autouse=True)
def released(monkeypatch):
    '''每个测试从没有拿锁开始，结束时把拿到的锁放掉'''
    monkeypatch.setattr(instance, '_held', None)
    monkeypatch.setattr(instance, '_unlockable', False)
    yield
    if instance._held is not None:
        instance._held.close()


@pytest.fixture
def log_file(data_dir, monkeypatch):
    '''日志文件还没开始写；返回它的路径。结束后把加在根 logger 上的 handler 拿掉、关掉'''
    monkeypatch.setattr(util, '_log_handler', None)
    monkeypatch.setattr(util, '_log_refused', False)
    monkeypatch.setattr(util, '_early_warnings', [])
    yield data_dir / 'logs' / 'bustag.log'
    handler = util._log_handler
    if handler is not None:
        logging.getLogger().removeHandler(handler)
        handler.close()


def lock_path(data_dir):
    return data_dir / instance.LOCK_FILE


def written(data_dir):
    return json.loads(lock_path(data_dir).read_text(encoding='utf-8'))


@pytest.fixture
def held_elsewhere(data_dir):
    '''像另一个进程那样拿着锁：另开一个句柄加锁，锁文件的内容写成 text。结束时放掉'''
    files = []

    def hold(text):
        file = open(lock_path(data_dir), 'w+b')
        files.append(file)
        instance._lock(file.fileno())
        file.seek(0)
        file.write(text.encode('utf-8'))
        file.flush()
        return file

    yield hold
    for file in files:
        file.close()


class TestLock:
    def test_the_lock_file_tells_who_holds_it_and_where_the_pages_are(self, data_dir):
        assert instance.lock_data_dir('http://127.0.0.1:8000') is True
        assert written(data_dir) == {'pid': os.getpid(), 'url': 'http://127.0.0.1:8000'}

    def test_a_second_service_on_the_same_data_dir_is_refused_and_told_where_the_first_is(self, data_dir,
                                                                                         held_elsewhere):
        held_elsewhere('{"pid": 4242, "url": "http://127.0.0.1:8123"}')
        with pytest.raises(instance.AlreadyRunning) as info:
            instance.lock_data_dir('http://127.0.0.1:8000')
        assert (info.value.pid, info.value.url) == (4242, 'http://127.0.0.1:8123')
        # 被拒绝的这一个什么也没改
        assert written(data_dir) == {'pid': 4242, 'url': 'http://127.0.0.1:8123'}
        assert instance._held is None

    @pytest.mark.parametrize('text, pid', [
        ('{"pid": 4242, "url": null}', 4242),
        ('{"pid": 4242}', 4242),
        ('', None),
        ('not json at all', None),
        ('[1, 2, 3]', None),
        ('{"pid": "x", "url": 5}', None),
        ('{"pid": 4242, "url": "javascript:alert(1)"}', 4242),
    ], ids=['no-address', 'no-url-key', 'empty', 'garbage', 'not-an-object', 'wrong-types', 'not-a-web-address'])
    def test_when_the_address_cannot_be_read_only_running_is_known(self, held_elsewhere, text, pid):
        held_elsewhere(text)
        with pytest.raises(instance.AlreadyRunning) as info:
            instance.lock_data_dir('http://127.0.0.1:8000')
        assert (info.value.pid, info.value.url) == (pid, None)

    @pytest.fixture
    def sleeps(self, monkeypatch):
        '''把 instance 里的等待记下来，不真的等'''
        waited = []

        def sleep(seconds):
            waited.append(seconds)

        monkeypatch.setattr(instance.time, 'sleep', sleep)
        return waited

    def test_a_lock_file_that_is_still_being_written_is_read_again(self, data_dir, held_elsewhere, monkeypatch):
        # 拿着锁的那个进程刚启动，还没把进程号和地址写进去；这一个等了一会儿读到的是空的，再等一次就读到了
        holder = held_elsewhere('')
        waited = []

        def sleep(seconds):
            waited.append(seconds)
            if len(waited) == 2:
                holder.seek(0)
                holder.write(b'{"pid": 4242, "url": "http://127.0.0.1:8123"}')
                holder.flush()

        monkeypatch.setattr(instance.time, 'sleep', sleep)
        with pytest.raises(instance.AlreadyRunning) as info:
            instance.lock_data_dir('http://127.0.0.1:8000')
        assert waited == [instance.READ_DELAY] * 2
        assert (info.value.pid, info.value.url, info.value.empty) == (4242, 'http://127.0.0.1:8123', False)

    def test_a_lock_file_that_stays_empty_is_said_to_be_empty(self, data_dir, held_elsewhere, sleeps):
        # 拿着锁的那个进程刚启动失败、已经把文件清空，正要放锁；或者还没来得及写
        held_elsewhere('')
        with pytest.raises(instance.AlreadyRunning) as info:
            instance.lock_data_dir('http://127.0.0.1:8000')
        assert sleeps == [instance.READ_DELAY] * 2
        assert (info.value.pid, info.value.url, info.value.empty) == (None, None, True)

    @pytest.mark.parametrize('text', ['not json at all', '[1, 2, 3]', '{"pid": "x", "url": 5}'])
    def test_a_lock_file_with_something_unreadable_in_it_is_not_waited_on_twice(self, data_dir, held_elsewhere,
                                                                               sleeps, text):
        held_elsewhere(text)
        with pytest.raises(instance.AlreadyRunning) as info:
            instance.lock_data_dir('http://127.0.0.1:8000')
        assert sleeps == [instance.READ_DELAY]
        assert (info.value.pid, info.value.url, info.value.empty) == (None, None, False)

    def test_a_lock_file_that_is_read_at_once_is_waited_on_once(self, data_dir, held_elsewhere, sleeps):
        held_elsewhere('{"pid": 4242, "url": "http://127.0.0.1:8123"}')
        with pytest.raises(instance.AlreadyRunning):
            instance.lock_data_dir('http://127.0.0.1:8000')
        assert sleeps == [instance.READ_DELAY]

    def test_a_lock_file_left_behind_does_not_block(self, data_dir):
        # 上一次运行被强行结束（或者容器被杀掉）之后文件还在，但锁已经随进程没了
        lock_path(data_dir).write_text('{"pid": 4242, "url": "http://127.0.0.1:8123"}', encoding='utf-8')
        assert instance.lock_data_dir('http://127.0.0.1:8000') is True
        assert written(data_dir) == {'pid': os.getpid(), 'url': 'http://127.0.0.1:8000'}

    def test_asking_again_in_the_same_process_keeps_the_lock(self, data_dir):
        assert instance.lock_data_dir('http://127.0.0.1:8000') is True
        held = instance._held
        # 不知道地址的再问一次（create_app）不把已经写好的地址抹掉
        assert instance.lock_data_dir() is True
        assert instance._held is held
        assert written(data_dir) == {'pid': os.getpid(), 'url': 'http://127.0.0.1:8000'}
        assert instance.lock_data_dir('http://127.0.0.1:8001') is True
        assert written(data_dir) == {'pid': os.getpid(), 'url': 'http://127.0.0.1:8001'}

    def test_whether_this_process_holds_the_lock(self, data_dir, held_elsewhere):
        assert instance.holds_lock() is False
        instance.lock_data_dir('http://127.0.0.1:8000')
        assert instance.holds_lock() is True
        instance.release()
        assert instance.holds_lock() is False
        held_elsewhere('{"pid": 4242, "url": null}')
        with pytest.raises(instance.AlreadyRunning):
            instance.lock_data_dir('http://127.0.0.1:8000')
        assert instance.holds_lock() is False

    def test_a_released_lock_can_be_taken_by_another_and_tells_nothing_about_this_one(self, data_dir, held_elsewhere):
        instance.lock_data_dir('http://127.0.0.1:8000')
        instance.release()
        assert instance._held is None
        # 没有启动起来的这一个准备用的地址不留在锁文件里（那个端口上可能是别人）
        assert lock_path(data_dir).read_bytes() == b''
        held_elsewhere('{"pid": 4242, "url": "http://127.0.0.1:8001"}')
        with pytest.raises(instance.AlreadyRunning) as info:
            instance.lock_data_dir('http://127.0.0.1:8000')
        assert (info.value.pid, info.value.url) == (4242, 'http://127.0.0.1:8001')

    def test_releasing_without_the_lock_does_nothing(self, data_dir, held_elsewhere):
        instance.release()
        assert not lock_path(data_dir).exists()
        held_elsewhere('{"pid": 4242, "url": "http://127.0.0.1:8123"}')
        instance.release()
        assert written(data_dir) == {'pid': 4242, 'url': 'http://127.0.0.1:8123'}

    def test_the_lock_can_be_taken_again_after_it_was_released(self, data_dir):
        instance.lock_data_dir('http://127.0.0.1:8000')
        instance.release()
        assert instance.lock_data_dir('http://127.0.0.1:8001') is True
        assert written(data_dir) == {'pid': os.getpid(), 'url': 'http://127.0.0.1:8001'}

    def test_without_an_address_only_the_process_is_written(self, data_dir):
        assert instance.lock_data_dir() is True
        assert written(data_dir) == {'pid': os.getpid(), 'url': None}

    def test_others_can_still_read_the_file_while_it_is_locked(self, data_dir):
        instance.lock_data_dir('http://127.0.0.1:8000')
        with open(lock_path(data_dir), 'rb') as other:
            assert json.loads(other.read()) == {'pid': os.getpid(), 'url': 'http://127.0.0.1:8000'}

    @pytest.mark.parametrize('error, reason', [(OSError(errno.ENOLCK, 'No locks available'), '没有可用的文件锁'),
                                               (OSError(errno.EOPNOTSUPP, 'Operation not supported'), '不支持这个操作'),
                                               (OSError(errno.EINVAL, 'Invalid argument'), '参数无效'),
                                               (OSError(errno.ENOSYS, '这个系统没有文件锁'), '这个系统没有文件锁')],
                             ids=['no-locks', 'not-supported', 'invalid', 'no-way-to-lock'])
    def test_a_file_system_without_locks_only_costs_a_warning(self, data_dir, monkeypatch, caplog, error, reason):
        def unsupported(fd):
            raise error

        monkeypatch.setattr(instance, '_lock', unsupported)
        assert instance.lock_data_dir('http://127.0.0.1:8000') is False
        warning, = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning == (f'数据目录 {data_dir} 所在的磁盘不支持文件锁（{reason}），'
                           '这次没法检查是不是已经有一个 bustag 在用它，请自己留意不要重复启动')
        assert instance._held is None

    def test_a_lock_file_that_cannot_be_opened_only_costs_a_warning(self, data_dir, caplog):
        lock_path(data_dir).mkdir()
        assert instance.lock_data_dir('http://127.0.0.1:8000') is False
        warning, = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning.startswith(f'锁文件 {lock_path(data_dir)} 打不开（')
        assert warning.endswith('），这次没法检查是不是已经有一个 bustag 在用这个数据目录，请自己留意不要重复启动')

    def test_the_reason_the_lock_file_cannot_be_opened_is_given_in_chinese(self, data_dir, monkeypatch, caplog):
        def denied(*args, **kwargs):
            raise PermissionError(errno.EACCES, 'Permission denied')

        monkeypatch.setattr(instance.os, 'open', denied)
        assert instance.lock_data_dir('http://127.0.0.1:8000') is False
        warning, = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning == (f'锁文件 {lock_path(data_dir)} 打不开（拒绝访问：没有权限，或者正被别的程序占用），'
                           '这次没法检查是不是已经有一个 bustag 在用这个数据目录，请自己留意不要重复启动')

    @pytest.mark.parametrize('cause', ['cannot-open', 'no-locks'])
    def test_what_cannot_be_locked_is_said_once(self, data_dir, monkeypatch, caplog, cause):
        # 启动时先后有两处给数据目录上锁（检查端口之前一次，建应用时一次）
        if cause == 'cannot-open':
            lock_path(data_dir).mkdir()
        else:
            def unsupported(fd):
                raise OSError(errno.ENOLCK, 'No locks available')

            monkeypatch.setattr(instance, '_lock', unsupported)
        assert instance.lock_data_dir('http://127.0.0.1:8000') is False
        assert instance.lock_data_dir() is False
        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1
        assert not instance.holds_lock()

    @pytest.mark.parametrize('cause', ['cannot-open', 'no-locks'])
    def test_what_cannot_be_locked_is_in_the_log_file_too(self, data_dir, log_file, monkeypatch, caplog, cause):
        # 上锁在日志文件开始写之前：警告当时只到了控制台，日志文件一开始写就补进去，各一遍
        if cause == 'cannot-open':
            lock_path(data_dir).mkdir()
        else:
            def unsupported(fd):
                raise OSError(errno.ENOLCK, 'No locks available')

            monkeypatch.setattr(instance, '_lock', unsupported)
        assert instance.lock_data_dir('http://127.0.0.1:8000') is False
        assert not log_file.exists()
        util.start_file_log()
        assert instance.lock_data_dir() is False
        warning, = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning.endswith('这次没法检查是不是已经有一个 bustag 在用它，请自己留意不要重复启动'
                                if cause == 'no-locks' else
                                '这次没法检查是不是已经有一个 bustag 在用这个数据目录，请自己留意不要重复启动')
        line, = log_file.read_text(encoding='utf-8').splitlines()
        assert line.endswith(f' - WARNING - {warning}')

    def test_a_new_lock_file_is_an_ordinary_file_not_a_program(self, data_dir, monkeypatch):
        modes = []
        os_open = os.open

        def recording(path, flags, mode=0o777, **kwargs):
            modes.append(mode)
            return os_open(path, flags, mode, **kwargs)

        monkeypatch.setattr(instance.os, 'open', recording)
        instance.lock_data_dir('http://127.0.0.1:8000')
        assert modes == [0o644]
        assert stat.S_IMODE(os.stat(lock_path(data_dir)).st_mode) & 0o111 == 0


class TestAnotherProcess:
    @pytest.fixture
    def holder(self, data_dir):
        '''
        真的另起一个进程拿着这个数据目录的锁，等它锁好再返回 (进程, 它报的进程号)。
        虚拟环境里的 python.exe 在 Windows 上是个启动器，真正的解释器是它的子进程，两个进程号不一样
        '''
        env = {k: v for k, v in os.environ.items() if not k.startswith('BUSTAG_')}
        env.update(BUSTAG_DATA_DIR=str(data_dir), PYTHONPATH=str(ROOT), PYTHONIOENCODING='utf-8')
        process = subprocess.Popen([sys.executable, '-c', HOLDER], cwd=str(data_dir), env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
        try:
            line = process.stdout.readline().strip()
            assert line.isdigit(), process.stderr.read()
            yield process, int(line)
        finally:
            process.kill()
            process.communicate(timeout=30)

    def test_the_lock_is_seen_by_another_process_and_gone_once_that_process_is_killed(self, data_dir, holder):
        process, pid = holder
        with pytest.raises(instance.AlreadyRunning) as info:
            instance.lock_data_dir('http://127.0.0.1:8000')
        assert (info.value.pid, info.value.url) == (pid, 'http://127.0.0.1:8123')
        process.kill()
        process.wait(timeout=30)
        # 进程一没，锁就没了，不用清理什么（给系统一点时间把那个进程收掉）
        deadline = time.monotonic() + 5
        while True:
            try:
                assert instance.lock_data_dir('http://127.0.0.1:8000') is True
                break
            except instance.AlreadyRunning:
                assert time.monotonic() < deadline, '那个进程已经结束，锁却还在'
                time.sleep(0.05)
        assert written(data_dir) == {'pid': os.getpid(), 'url': 'http://127.0.0.1:8000'}
