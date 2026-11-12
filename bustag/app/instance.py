'''
同一个数据目录同时只让一个网页服务用：两个实例各自定时抓取、各自训练和打分，互相不知道对方，会把数据弄乱。
网页服务启动时给数据目录里的 bustag.lock 加操作系统级的文件锁。锁跟着进程走：进程结束、被强行结束、崩溃，
锁都自动没了，所以上次留下的锁文件不碍事。文件里写着进程号和访问地址，给后来想启动的那一个提示用。
命令行的命令（check、download、recommend）不加锁，它们可以和网页服务同时运行
'''
import errno
import json
import os
import time

from bustag.util import get_data_dir, get_data_path, os_error_text, warn_early

try:
    import msvcrt
except ImportError:
    msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None

LOCK_FILE = 'bustag.lock'
# Windows 上被锁住的字节别的进程读不了（读的范围碰到它就报错），所以锁的是离文件内容很远的一个字节
# （可以锁在文件末尾以后），内容照样能读
LOCK_OFFSET = 1 << 30
# 新建的锁文件的权限：普通文件。os.open 默认给的权限带着可执行位
LOCK_FILE_MODE = 0o644
# 锁文件的内容只有几十个字节，最多读这么多
MAX_INFO_SIZE = 4096
# 锁已经被别的进程拿着时的错误码。别的错误码说明这个文件系统不支持加锁（一些网络盘、NAS 挂载）
BUSY_ERRORS = (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK)
# 没拿到锁时等这么多秒再读锁文件：拿到锁的那个进程可能正在往里写。读到空的再等一次：它可能刚启动、还没写，
# 或者没启动起来、已经把文件清空正要放锁
READ_DELAY = 0.2
# 拿着锁的文件，进程结束前一直开着
_held = None
# 这次运行锁不了（锁文件打不开，或者文件系统不支持加锁）时是 True
_unlockable = False


class AlreadyRunning(Exception):
    '''
    这个数据目录已经有一个网页服务在用。pid 是它的进程号，url 是它的访问地址，读不到时是 None；
    empty 表示锁文件里什么都没有（它正在启动，或者正在退出），这时进程号和地址当然也读不到
    '''

    def __init__(self, pid=None, url=None, empty=False):
        super().__init__(pid, url, empty)
        self.pid = pid
        self.url = url
        self.empty = empty


def _lock(fd):
    '''给打开的文件加独占锁，不等待。已经被锁着、或者这个文件系统不支持时抛 OSError（按错误码区分）'''
    if msvcrt is not None:
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    elif fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        raise OSError(errno.ENOSYS, '这个系统没有文件锁')


def _read(file):
    '''锁文件里写的 (进程号, 访问地址)，读不出来的那一项是 None；文件是空的时返回 None'''
    try:
        file.seek(0)
        content = file.read(MAX_INFO_SIZE)
        if not content:
            return None
        info = json.loads(content.decode('utf-8'))
    except (OSError, ValueError):
        return None, None
    if not isinstance(info, dict):
        return None, None
    pid, url = info.get('pid'), info.get('url')
    return (pid if isinstance(pid, int) else None,
            url if isinstance(url, str) and url.startswith(('http://', 'https://')) else None)


def _write(file, url):
    file.seek(0)
    file.truncate()
    file.write(json.dumps({'pid': os.getpid(), 'url': url}).encode('utf-8'))
    file.flush()


def lock_data_dir(url=None):
    '''
    给数据目录上锁，在锁文件里写上本进程的进程号和访问地址（不知道地址时写 null）。返回 True。
    已经有别的进程锁着时抛 AlreadyRunning。锁文件打不开、文件系统不支持加锁时写一条警告、返回 False：
    没法检查，程序照常启动；这次运行里再调用不再试，直接返回 False，免得同一句警告说两遍。
    同一个进程里再调用不会重复加锁，给了地址就更新锁文件里的地址
    '''
    global _held, _unlockable
    if _held is not None:
        if url is not None:
            _write(_held, url)
        return True
    if _unlockable:
        return False
    path = get_data_path(LOCK_FILE)
    advice = '请自己留意不要重复启动'
    try:
        # 不带缓冲：读写的范围就是要的那几个字节，不会碰到别的进程锁着的那个字节
        file = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0), LOCK_FILE_MODE),
                         'r+b', buffering=0)
    except OSError as ex:
        _unlockable = True
        warn_early(f'锁文件 {path} 打不开（{os_error_text(ex)}），'
                   f'这次没法检查是不是已经有一个 bustag 在用这个数据目录，{advice}')
        return False
    try:
        _lock(file.fileno())
    except OSError as ex:
        if ex.errno in BUSY_ERRORS:
            time.sleep(READ_DELAY)
            info = _read(file)
            if info is None:
                time.sleep(READ_DELAY)
                info = _read(file)
            file.close()
            pid, running_at = info or (None, None)
            raise AlreadyRunning(pid, running_at, empty=info is None) from None
        file.close()
        _unlockable = True
        warn_early(f'数据目录 {get_data_dir()} 所在的磁盘不支持文件锁（{os_error_text(ex)}），'
                   f'这次没法检查是不是已经有一个 bustag 在用它，{advice}')
        return False
    _write(file, url)
    _held = file
    return True


def holds_lock():
    '''本进程现在是不是锁着数据目录'''
    return _held is not None


def release():
    '''
    把锁放掉（没有拿着时什么都不做）。给没有启动起来、但进程还要留一会儿的情况用：窗口等着按回车的时候，
    不该挡住下一次启动。锁文件留着，里面的内容清掉：准备用的访问地址没有用上，不留给后来的人看
    '''
    global _held
    if _held is None:
        return
    try:
        _held.seek(0)
        _held.truncate()
    except OSError:
        pass
    _held.close()
    _held = None
