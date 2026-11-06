'''
数据目录、配置文件和日志
'''
import configparser
import datetime
import locale
import logging
import math
import os
import sys
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

logger = logging.getLogger('bustag')
DEBUG = False
CONFIG_FILE = 'config.ini'
MODEL_PATH = 'model/'
DEFAULT_ROOT_PATH = 'https://www.javbus.com'
DEAD_DOMAINS = ('busdmm.work', 'cdnbus.bid')
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
DATA_DIR = None
APP_CONFIG = {}
DEFAULT_CONFIG = {
    'download': {
        'root_path': DEFAULT_ROOT_PATH,
        'count': '100',
        'interval': '10800',
        'delay': '1.5',
        'max_page': '30',
    },
    'options': {
        'proxy': '',
    },
}
# [download] 里的数字配置：键 → (类型, 最小值, 名称)
NUMBER_OPTIONS = {
    'count': (int, 1, '数量'),
    'interval': (int, 60, '秒数'),
    'delay': (float, 0, '秒数'),
    'max_page': (int, 1, '页数'),
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

[options]
# 代理地址，例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
# 留空表示使用系统或环境变量里的代理设置
proxy =
'''


class ConfigError(Exception):
    '''配置文件有问题（读写失败、格式不对、数字不合法），消息是一行中文，直接给用户看'''


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


def setup_logging():
    global DEBUG
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    debug_env = os.environ.get('BUSTAG_DEBUG', '')
    DEBUG = debug_env not in ('', '0') or bool(os.environ.get('TESTING'))
    logging.basicConfig(format=LOG_FORMAT, datefmt='%Y-%m-%d %H:%M:%S')
    logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)


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
        raise ConfigError(f'无法读写配置文件 {config_path}：{ex.strerror or ex}') from ex
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


def _exit_on_startup_error(message):
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
        _exit_on_startup_error(f'无法创建数据目录 {_data_dir_path()}：{ex.strerror or ex}')
    logger.info(f'数据目录：{DATA_DIR}')
    try:
        load_config()
        check_model_folder()
    except ConfigError as ex:
        _exit_on_startup_error(str(ex))
    except OSError as ex:
        _exit_on_startup_error(f'无法创建模型目录 {get_data_path(MODEL_PATH)}：{ex.strerror or ex}')


init()
