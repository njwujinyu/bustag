'''
数据目录、配置文件和日志
'''
import configparser
import datetime
import locale
import logging
import os
import sys
from urllib.parse import urljoin, urlparse

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


def resolve_data_dir():
    '''
    数据目录：环境变量 BUSTAG_DATA_DIR > exe 所在目录/data > 当前目录/data，不存在就创建
    '''
    env_dir = os.environ.get('BUSTAG_DATA_DIR')
    if env_dir:
        data_dir = os.path.abspath(env_dir)
    elif getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        data_dir = os.path.join(exe_dir, 'data')
    else:
        data_dir = os.path.join(os.getcwd(), 'data')
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


def load_config():
    config_path = get_config_path()
    if not os.path.exists(config_path) and create_default_config(config_path):
        logger.info(f'没有找到配置文件，已生成默认配置：{config_path}')
    conf = configparser.ConfigParser()
    conf.read_dict(DEFAULT_CONFIG)
    conf.read_string(_read_text(config_path), source=config_path)
    APP_CONFIG.clear()
    for section in conf.sections():
        APP_CONFIG[section.lower()] = dict(conf[section])
        for key in conf.options(section):
            value = conf.get(section, key)
            key = section + '.' + key
            APP_CONFIG[key.lower()] = value
    root_path = APP_CONFIG['download.root_path'].strip().rstrip('/') or DEFAULT_ROOT_PATH
    APP_CONFIG['download.root_path'] = root_path
    APP_CONFIG['download']['root_path'] = root_path
    warning = dead_domain_warning(root_path)
    if warning:
        logger.warning(warning)
    logger.debug(APP_CONFIG)


def format_datetime(dt):
    format = '%Y-%m-%d %H:%M:%S'
    return dt.strftime(format)


def to_localtime(utc_dt):
    local_tz = datetime.timezone(datetime.timedelta(hours=8))
    local_dt = utc_dt.replace(tzinfo=datetime.timezone.utc).astimezone(local_tz)
    format = '%Y-%m-%d %H:%M:%S'
    return local_dt.strftime(format)


def check_model_folder():
    model_path = get_data_path(MODEL_PATH)
    if not os.path.exists(model_path):
        os.makedirs(model_path, exist_ok=True)
        logger.debug(f'created model folder: {model_path}')


def init():
    global DATA_DIR
    setup_logging()
    DATA_DIR = resolve_data_dir()
    logger.info(f'数据目录：{DATA_DIR}')
    load_config()
    check_model_folder()


init()
