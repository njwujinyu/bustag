'''
数据目录、配置文件和日志
'''
import configparser
import datetime
import errno
import locale
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

logger = logging.getLogger('bustag')
DEBUG = False
CONFIG_FILE = 'config.ini'
MODEL_PATH = 'model/'
DEFAULT_ROOT_PATH = 'https://www.javbus.com'
DEAD_DOMAINS = ('busdmm.work', 'cdnbus.bid')
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
# 网页服务的日志文件：<数据目录>/logs/bustag.log，写满 LOG_MAX_BYTES 换一个新文件，另外保留 LOG_BACKUPS 个旧文件
LOG_DIR = 'logs'
LOG_FILE = 'bustag.log'
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 5
# 任何"协议://用户[:密码]@主机"里的认证信息；密码里可以有 @，以最后一个 @ 为界
URL_CREDENTIALS = re.compile(r'(?<=://)([^/\s:@]+)(:[^/\s]*)?@(?=[^/\s@]*(?:[/\s]|$))')
DATA_DIR = None
# 正在写日志文件的 handler；日志文件这次运行建不了时 _log_refused 是 True
_log_handler = None
_log_refused = False
# 日志文件开始写之前说过的警告（warn_early），文件一开始写就补进去
_early_warnings = []
APP_CONFIG = {}
DEFAULT_CONFIG = {
    'download': {
        'root_path': DEFAULT_ROOT_PATH,
        'count': '100',
        'interval': '10800',
        'delay': '1.5',
        'max_page': '30',
        'backfill_count': '60',
        'backfill_daily': '300',
        'avwiki_daily': '200',
    },
    'options': {
        'proxy': '',
    },
    'server': {
        'host': '127.0.0.1',
    },
}
# [download] 里的数字配置：键 → (类型, 最小值, 名称)
NUMBER_OPTIONS = {
    'count': (int, 1, '数量'),
    'interval': (int, 60, '秒数'),
    'delay': (float, 0, '秒数'),
    'max_page': (int, 1, '页数'),
    'backfill_count': (int, 1, '数量'),
    'backfill_daily': (int, 1, '数量'),
    'avwiki_daily': (int, 1, '次数'),
}
DEFAULT_CONFIG_TEXT = '''\
# bustag 配置文件。首次启动时自动生成，修改后重启程序生效。

[download]
# JavBus 网址。网站换域名时改这里
root_path = https://www.javbus.com
# 每次抓取最多新增多少部影片
count = 100
# 自动抓取的间隔（秒），10800 秒 = 3 小时
interval = 10800
# 高级选项，一般不用改。需要时去掉行首的 # 再修改：
# 两次请求之间的最小间隔（秒）
# delay = 1.5
# 每次抓取最多翻多少页列表
# max_page = 30
# 按演员补全作品（在"指定推荐"页上点了才抓）：每位演员每次最多补多少部
# backfill_count = 60
# 按演员补全作品：所有演员加起来每天最多抓多少部
# backfill_daily = 300
# 补演员名（到 av-wiki 查没有演员的影片）：每天最多发多少次请求，到了就停到第二天
# avwiki_daily = 200

[options]
# 代理地址，例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
# 留空表示使用系统或环境变量里的代理设置
proxy =

[server]
# 网页服务的监听地址。127.0.0.1：只有这台电脑能打开网页。
# 要让同一局域网里的手机、电脑也能访问，改成 0.0.0.0；程序没有登录保护，不要在公共网络上这样用。
# 设置了环境变量 BUSTAG_HOST 时以环境变量为准
host = 127.0.0.1
'''


# Windows 上目标文件正被读取（或者被杀毒软件扫描）时 os.replace 会 PermissionError，按这些间隔（秒）重试
REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)
# 读写文件失败时 C 运行库按错误码给的原因总是英文，常见的几种在提示里换成中文
ERRNO_TEXTS = {
    errno.EACCES: '拒绝访问：没有权限，或者正被别的程序占用',
    errno.EPERM: '拒绝访问：没有权限，或者正被别的程序占用',
    errno.EBUSY: '正被别的程序占用',
    errno.EISDIR: '它是一个文件夹，不是文件',
    errno.ENOTDIR: '路径里有一段不是文件夹',
    errno.ENOENT: '找不到这个文件或文件夹',
    errno.EEXIST: '已经有同名的文件或文件夹',
    errno.ENAMETOOLONG: '文件名太长',
    errno.ENOSPC: '磁盘空间不足',
    errno.EROFS: '磁盘是只读的',
    errno.EIO: '磁盘读写出错',
    errno.EMFILE: '同时打开的文件太多',
    errno.EINVAL: '参数无效',
    errno.ENOLCK: '没有可用的文件锁',
    errno.EOPNOTSUPP: '不支持这个操作',
    errno.ENOTSUP: '不支持这个操作',
}


class ConfigError(Exception):
    '''配置文件有问题（读写失败、格式不对、数字不合法），消息是一行中文，直接给用户看'''


def os_error_text(error):
    '''
    OSError 的原因，放进给用户看的提示里。Windows 自己的报错是系统语言的；原因是英文时（C 运行库按错误码
    给的那一类），常见的几种换成中文，没有对应说法的原样返回
    '''
    text = error.strerror or str(error)
    if text.isascii():
        text = ERRNO_TEXTS.get(error.errno, text)
    return text


def remove_file(path):
    '''删掉一个文件，删不掉（已经不在了、正被占用）就算了'''
    try:
        os.remove(path)
    except OSError:
        pass


def write_temp_file(path, write):
    '''在 path 所在目录建临时文件，用 write(文件对象) 写入并落盘，返回临时文件路径；写入失败时删掉临时文件'''
    fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        remove_file(temp_path)
        raise
    return temp_path


def replace_file(source, target, swap=None):
    '''
    用 source 原子地替换 target；target 暂时被占用（PermissionError）时等一会儿重试。
    swap 是一把锁（或别的上下文管理器）：只在真正替换的那一下拿着，等着重试的时候不拿。
    同一个进程里读 target 的一方读的时候也拿它，两边就不会撞上：Windows 上正在被替换的文件打不开
    （拒绝访问），正开着的文件也换不掉
    '''
    def replace():
        if swap is None:
            os.replace(source, target)
        else:
            with swap:
                os.replace(source, target)

    for delay in REPLACE_RETRY_DELAYS:
        try:
            replace()
            return
        except PermissionError:
            time.sleep(delay)
    replace()


def save_file(path, data, swap=None):
    '''
    把 data（字节）存成 path：先写同目录的临时文件再替换，别处读到的要么是原来的文件、要么是新的。
    失败时抛 OSError，原来的文件不变，也不留下临时文件。swap 见 replace_file
    '''
    temp_path = write_temp_file(path, lambda f: f.write(data))
    try:
        replace_file(temp_path, path, swap)
    finally:
        remove_file(temp_path)


def _data_dir_path():
    '''
    数据目录：环境变量 BUSTAG_DATA_DIR > exe 所在目录/data > 当前目录/data
    '''
    env_dir = os.environ.get('BUSTAG_DATA_DIR')
    if env_dir:
        return os.path.abspath(env_dir)
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        return os.path.join(exe_dir, 'data')
    return os.path.join(os.getcwd(), 'data')


def resolve_data_dir():
    '''
    数据目录（规则见 _data_dir_path），不存在就创建
    '''
    data_dir = _data_dir_path()
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_data_dir():
    return DATA_DIR


def get_data_path(file):
    return os.path.join(DATA_DIR, file)


def get_config_path():
    return get_data_path(CONFIG_FILE)


def get_resource_dir():
    '''
    模板和静态文件所在目录（含 views/ 和 static/）
    '''
    meipass = getattr(sys, '_MEIPASS', None)
    if getattr(sys, 'frozen', False) and meipass:
        return os.path.join(meipass, 'bustag', 'app')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app')


def hide_credentials(text):
    '''
    隐去一段文字里的认证信息：配置里的代理地址换成 redact_url 的写法（可以不带协议），
    其他带认证信息的网址（环境变量里的代理等）同样只留用户名
    '''
    proxy = APP_CONFIG.get('options.proxy', '').strip()
    if proxy:
        text = text.replace(proxy, redact_url(proxy))
    return URL_CREDENTIALS.sub(lambda found: f'{found.group(1)}:***@' if found.group(2) else '***@', text)


class _LogFormatter(logging.Formatter):
    '''控制台和日志文件共用的格式。第三方库的警告、异常栈里可能带着完整的代理地址，显示和写进文件之前把密码隐去'''

    def format(self, record):
        return hide_credentials(super().format(record))


def setup_logging():
    global DEBUG
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    debug_env = os.environ.get('BUSTAG_DEBUG', '')
    DEBUG = debug_env not in ('', '0') or bool(os.environ.get('TESTING'))
    if not logging.root.handlers:
        console = logging.StreamHandler()
        console.setFormatter(_LogFormatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
        logging.root.addHandler(console)
    logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)


def get_log_path():
    return os.path.join(DATA_DIR, LOG_DIR, LOG_FILE)


class _LogFileHandler(RotatingFileHandler):
    '''
    写不进去（磁盘满了、文件被别的程序占着）时不让程序出错，也不让 logging 每写一条就往控制台打一遍异常栈：
    只在控制台说一次
    '''

    def __init__(self, path):
        super().__init__(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding='utf-8')
        self._complained = False

    def _complain(self, text):
        if not self._complained:
            self._complained = True
            logger.warning(text)

    def doRollover(self):
        try:
            super().doRollover()
        except OSError as ex:
            # 旧文件挪不走（多半是别的程序正开着它）。再试只会每写一条日志就把更早的旧文件往后挤掉一个，
            # 所以这次运行不再换文件，接着写在原来的文件里
            self.maxBytes = 0
            if self.stream is None:
                self.stream = self._open()
            self._complain(f'日志文件 {self.baseFilename} 换不了新文件（{os_error_text(ex)}），这次运行先接着写在原来的文件里')

    def handleError(self, record):
        error = sys.exc_info()[1]
        reason = os_error_text(error) if isinstance(error, OSError) else error
        self._complain(f'日志文件 {self.baseFilename} 写不进去（{reason}），日志先只显示在控制台')


def start_file_log():
    '''
    把日志同时写进 <数据目录>/logs/bustag.log，返回这个文件的路径。只有网页服务调用：命令行的命令可能和网页服务
    同时运行，Windows 上两个进程不能给同一个日志文件换新文件。
    加在根 logger 上，所以第三方库（waitress、apscheduler 等）的警告和错误也在里面。
    文件建不了（目录只读、磁盘满）时只写一条警告，返回 None，程序照常运行。
    重复调用只生效一次：建不了的那一次之后也不再试，免得同一句警告说两遍
    '''
    global _log_handler, _log_refused
    if _log_handler is not None:
        return _log_handler.baseFilename
    if _log_refused:
        return None
    path = get_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = _LogFileHandler(path)
    except OSError as ex:
        _log_refused = True
        logger.warning(f'日志文件 {path} 写不了（{os_error_text(ex)}），这次运行的日志只显示在控制台')
        return None
    handler.setFormatter(_LogFormatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logging.getLogger().addHandler(handler)
    _log_handler = handler
    for text in _early_warnings:
        # 只写进文件：这些话当时已经显示在控制台了
        handler.handle(logger.makeRecord(logger.name, logging.WARNING, __file__, 0, text, (), None))
    _early_warnings.clear()
    return handler.baseFilename


def warn_early(text):
    '''
    说一句警告：显示在控制台，也写进日志文件。启动时有的话要在日志文件开始写之前说（要先给数据目录上锁，
    被拒绝的那个实例才不会去碰正在运行的那个的日志文件），这样的话先记着，日志文件一开始写就补进去
    '''
    logger.warning(text)
    if _log_handler is None:
        _early_warnings.append(text)


def active_log_file():
    '''这次运行正在写的日志文件；没有在写（命令行，或者文件建不了）时返回 None'''
    return None if _log_handler is None else _log_handler.baseFilename


def log_uncaught_exceptions():
    '''
    哪里都没接住的异常（主线程和其他线程里的）连同异常栈写成一条 ERROR 日志，不再只打在控制台上。Ctrl+C 照旧
    '''
    def in_main_thread(exc_type, exc, traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, traceback)
            return
        logger.error('程序出错退出', exc_info=(exc_type, exc, traceback))

    def in_thread(args):
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread is not None else '（已结束）'
        logger.error(f'线程 {name} 里有没接住的异常', exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = in_main_thread
    threading.excepthook = in_thread


def get_now_time():
    return datetime.datetime.now()


def get_full_url(path):
    root_path = APP_CONFIG['download.root_path']
    full_url = urljoin(root_path + '/', path)
    return full_url


def redact_url(url):
    '''
    隐去网址里的认证信息，用于日志和自检输出：有密码时把密码换成 ***（http://user:***@host:port）；
    只有用户名时把用户名换成 ***（http://***@host:port），这个位置常放的是访问令牌。
    不带 scheme 的写法（user:pass@host:port）也处理；没有认证信息或解析失败时原样返回
    '''
    prefix = '' if '://' in url else '//'
    try:
        parts = urlsplit(prefix + url)
    except ValueError:
        return url
    userinfo, _, hostinfo = parts.netloc.rpartition('@')
    if not userinfo:
        return url
    user, has_password, _ = userinfo.partition(':')
    shown = f'{user}:***' if has_password else '***'
    return urlunsplit(parts._replace(netloc=f'{shown}@{hostinfo}'))[len(prefix):]


# 地址里带着演员名字的两种页面：av-wiki 的女优页 /av-actress/<名字的罗马字>/，JavBus 按名字找演员 /searchstar/<名字>
_ACTRESS_IN_ADDRESS = re.compile(r'''(/av-actress/|/searchstar/)[^/\s'"<>。，、：；）)]+''')


def hide_actress(text):
    '''
    把文字里女优页、按名字找演员的地址中的名字换成 …，用于日志和给用户看的报错：那里只该有定位问题用得上的东西，
    知道是哪一类页面读不到就够了，是哪一位用不上。别的地址原样
    '''
    return _ACTRESS_IN_ADDRESS.sub(r'\1…', text)


def is_dead_domain(root_path):
    host = (urlparse(root_path).hostname or '').lower()
    return any(host == domain or host.endswith('.' + domain) for domain in DEAD_DOMAINS)


def dead_domain_warning(root_path):
    '''
    root_path 是已知失效的域名时返回提示文字，否则返回 None
    '''
    if not is_dead_domain(root_path):
        return None
    return (f'!!! 配置里的 root_path = {root_path} 是已经失效的旧域名，抓取会失败。'
            f'请打开 {get_config_path()}，把 root_path 改成 {DEFAULT_ROOT_PATH}，然后重启程序。')


def create_default_config(config_path):
    try:
        with open(config_path, 'x', encoding='utf-8') as f:
            f.write(DEFAULT_CONFIG_TEXT)
    except FileExistsError:
        return False
    return True


def _read_text(path):
    with open(path, 'rb') as f:
        data = f.read()
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False))


def _config_error(config_path, reason):
    return ConfigError(f'配置文件 {config_path} 有错：{reason}，请修改或删除后重启（删除后会重新生成默认配置）')


def _parse_error_reason(ex):
    '''configparser 的解析错误换成中文原因，带行号'''
    lineno = getattr(ex, 'lineno', None)
    if lineno is None and getattr(ex, 'errors', None):
        lineno = ex.errors[0][0]
    where = f'第 {lineno} 行' if lineno else '有一行'
    if isinstance(ex, configparser.MissingSectionHeaderError):
        return f'{where}不属于任何节，文件开头要先写节名（如 [download]）'
    if isinstance(ex, configparser.DuplicateSectionError):
        return f'{where}的 [{ex.section}] 重复出现'
    if isinstance(ex, configparser.DuplicateOptionError):
        return f'{where}的 {ex.option} 在 [{ex.section}] 里重复出现'
    if isinstance(ex, configparser.ParsingError):
        return f'{where}无法识别，每行应该是"键 = 值"、[节名] 或 # 开头的注释'
    return f'{where}无法识别（{type(ex).__name__}）'


def _number_error(key, value):
    '''[download] 里的数字配置不合法时返回原因，合法时返回 None'''
    kind, minimum, name = NUMBER_OPTIONS[key]
    try:
        number = kind(value)
    except ValueError:
        number = None
    if number is not None and math.isfinite(number) and number >= minimum:
        return None
    need = f'≥ {minimum} 的整数' if kind is int else f'≥ {minimum} 的数字'
    return f'[download] {key} = {value or "（空）"} 不是有效的{name}（需要 {need}）'


def load_config():
    '''
    读取 config.ini（不存在时先生成默认配置），结果填进 APP_CONFIG（始终是同一个 dict）。
    数字配置校验过，但 APP_CONFIG 里存的仍是字符串

    Raises:
        ConfigError: 读写失败、格式不对或者数字不合法；这时 APP_CONFIG 保持原样
    '''
    config_path = get_config_path()
    try:
        if not os.path.exists(config_path) and create_default_config(config_path):
            logger.info(f'没有找到配置文件，已生成默认配置：{config_path}')
        text = _read_text(config_path)
    except OSError as ex:
        raise ConfigError(f'无法读写配置文件 {config_path}：{os_error_text(ex)}') from ex
    except UnicodeDecodeError as ex:
        raise _config_error(config_path, '文件编码无法识别，请用 UTF-8 编码保存') from ex
    # 不做 % 插值：代理密码里常有 %40 这类转义，要原样交给 requests
    conf = configparser.ConfigParser(interpolation=None)
    conf.read_dict(DEFAULT_CONFIG)
    try:
        conf.read_string(text, source=config_path)
    except configparser.Error as ex:
        raise _config_error(config_path, _parse_error_reason(ex)) from ex
    config = {}
    for section in conf.sections():
        config[section.lower()] = dict(conf[section])
        for key in conf.options(section):
            config[f'{section}.{key}'.lower()] = conf.get(section, key)
    for key in NUMBER_OPTIONS:
        reason = _number_error(key, config['download.' + key])
        if reason:
            raise _config_error(config_path, reason)
    root_path = config['download.root_path'].strip().rstrip('/') or DEFAULT_ROOT_PATH
    config['download.root_path'] = root_path
    config['download']['root_path'] = root_path
    APP_CONFIG.clear()
    APP_CONFIG.update(config)
    warning = dead_domain_warning(root_path)
    if warning:
        logger.warning(warning)
    logger.debug('配置：' + '，'.join(f'{key} = {redact_url(value)}'
                                    for key, value in APP_CONFIG.items() if isinstance(value, str)))


def format_datetime(dt):
    format = '%Y-%m-%d %H:%M:%S'
    return dt.strftime(format)


def check_model_folder():
    model_path = get_data_path(MODEL_PATH)
    if not os.path.exists(model_path):
        os.makedirs(model_path, exist_ok=True)
        logger.debug(f'已创建模型目录 {model_path}')


def wait_for_enter_if_console():
    '''
    双击运行 exe 时，出错信息会随窗口一起消失，所以先等用户按回车。
    源码运行、Docker、重定向输出等没有交互控制台的情况不等。
    '''
    if not getattr(sys, 'frozen', False):
        return
    if sys.stdin is None or not sys.stdin.isatty():
        return
    try:
        input('按回车键退出 ...')
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def exit_on_startup_error(message):
    '''启动时就不能往下运行：输出一行中文错误（双击 exe 时等按回车），以退出码 1 结束'''
    logger.error(message)
    wait_for_enter_if_console()
    raise SystemExit(1)


def init():
    '''
    导入 bustag.util 时执行：日志、数据目录、配置文件、模型目录。
    数据目录建不了或者配置文件有错时只输出一行中文错误，以退出码 1 结束
    '''
    global DATA_DIR
    setup_logging()
    try:
        DATA_DIR = resolve_data_dir()
    except OSError as ex:
        exit_on_startup_error(f'无法创建数据目录 {_data_dir_path()}：{os_error_text(ex)}')
    logger.info(f'数据目录：{DATA_DIR}')
    try:
        load_config()
        check_model_folder()
    except ConfigError as ex:
        exit_on_startup_error(str(ex))
    except OSError as ex:
        exit_on_startup_error(f'无法创建模型目录 {get_data_path(MODEL_PATH)}：{os_error_text(ex)}')


init()
