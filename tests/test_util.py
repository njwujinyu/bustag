'''
数据目录、资源目录、配置文件、日志级别和时间换算；导入 bustag.util 时的初始化在子进程里验证
'''
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from bustag import util

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / 'bustag' / 'app'


class TestDataDir:
    def test_environment_variable_wins(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('BUSTAG_DATA_DIR', os.path.join('relative', 'data'))
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
        monkeypatch.setattr(sys, 'executable', str(tmp_path / 'app' / 'bustag.exe'))
        result = util.resolve_data_dir()
        assert os.path.isabs(result)
        assert os.path.samefile(result, tmp_path / 'relative' / 'data')

    def test_exe_folder_when_frozen(self, tmp_path, monkeypatch):
        monkeypatch.delenv('BUSTAG_DATA_DIR')
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
        monkeypatch.setattr(sys, 'executable', str(tmp_path / 'app' / 'bustag.exe'))
        assert util.resolve_data_dir() == str(tmp_path / 'app' / 'data')
        assert (tmp_path / 'app' / 'data').is_dir()

    @pytest.mark.parametrize('value', [None, ''])
    def test_current_folder_by_default(self, tmp_path, monkeypatch, value):
        monkeypatch.chdir(tmp_path)
        if value is None:
            monkeypatch.delenv('BUSTAG_DATA_DIR')
        else:
            monkeypatch.setenv('BUSTAG_DATA_DIR', value)
        assert os.path.samefile(util.resolve_data_dir(), tmp_path / 'data')


class TestResourceDir:
    def test_source_checkout(self):
        assert os.path.samefile(util.get_resource_dir(), APP_DIR)

    def test_frozen_bundle(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
        monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
        assert util.get_resource_dir() == os.path.join(str(tmp_path), 'bustag', 'app')

    def test_frozen_without_bundle_folder(self, monkeypatch):
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
        monkeypatch.delattr(sys, '_MEIPASS', raising=False)
        assert os.path.samefile(util.get_resource_dir(), APP_DIR)


class TestConfig:
    def test_default_config_is_created(self, data_dir):
        util.load_config()
        assert (data_dir / 'config.ini').read_text(encoding='utf-8') == util.DEFAULT_CONFIG_TEXT
        flat = {key: util.APP_CONFIG[key] for key in (
            'download.root_path', 'download.count', 'download.interval', 'download.delay',
            'download.max_page', 'options.proxy')}
        assert flat == {'download.root_path': 'https://www.javbus.com', 'download.count': '100',
                        'download.interval': '10800', 'download.delay': '1.5', 'download.max_page': '30',
                        'options.proxy': ''}
        assert util.APP_CONFIG['download']['count'] == '100'

    def test_existing_config_is_kept_and_defaults_fill_the_rest(self, data_dir):
        path = data_dir / 'config.ini'
        path.write_text('[download]\ncount = 7\n', encoding='utf-8')
        assert util.create_default_config(str(path)) is False
        util.load_config()
        assert path.read_text(encoding='utf-8') == '[download]\ncount = 7\n'
        assert util.APP_CONFIG['download.count'] == '7'
        assert util.APP_CONFIG['download.interval'] == '10800'
        assert util.APP_CONFIG['options.proxy'] == ''

    def test_utf8_bom_and_trailing_slash(self, data_dir):
        text = '\ufeff[download]\n# 网站换域名时改这里\nroot_path = https://www.busjav.casa/ \n'
        (data_dir / 'config.ini').write_bytes(text.encode('utf-8'))
        util.load_config()
        assert util.APP_CONFIG['download.root_path'] == 'https://www.busjav.casa'
        assert util.APP_CONFIG['download']['root_path'] == 'https://www.busjav.casa'

    def test_config_saved_in_system_encoding(self, data_dir, monkeypatch):
        monkeypatch.setattr(util.locale, 'getpreferredencoding', lambda do_setlocale=True: 'gbk')
        text = '[options]\n# 代理地址\nproxy = http://127.0.0.1:7890\n'
        (data_dir / 'config.ini').write_bytes(text.encode('gbk'))
        util.load_config()
        assert util.APP_CONFIG['options.proxy'] == 'http://127.0.0.1:7890'

    def test_empty_root_path_uses_default(self, data_dir):
        (data_dir / 'config.ini').write_text('[download]\nroot_path =\n', encoding='utf-8')
        util.load_config()
        assert util.APP_CONFIG['download.root_path'] == util.DEFAULT_ROOT_PATH

    def test_dead_domain_is_reported(self, data_dir, caplog):
        (data_dir / 'config.ini').write_text('[download]\nroot_path = https://www.busdmm.work\n', encoding='utf-8')
        util.load_config()
        assert any('已经失效的旧域名' in m and str(data_dir / 'config.ini') in m for m in caplog.messages)

    def test_reload_refills_the_same_dict(self, data_dir):
        config = util.APP_CONFIG
        config['stale'] = 'value'
        util.load_config()
        assert util.APP_CONFIG is config
        assert 'stale' not in config

    def test_percent_is_kept_as_written(self, data_dir):
        proxy = 'http://alice:p%40ss@127.0.0.1:7890'
        (data_dir / 'config.ini').write_text(f'[options]\nproxy = {proxy}\n', encoding='utf-8')
        util.load_config()
        assert util.APP_CONFIG['options.proxy'] == proxy

    @pytest.mark.parametrize('text, reason', [
        ('[download]\ncount = 1\ncount = 2\n', '第 3 行的 count 在 [download] 里重复出现'),
        ('[download]\ncount = 1\n\n[download]\n', '第 4 行的 [download] 重复出现'),
        ('count = 5\n', '第 1 行不属于任何节，文件开头要先写节名（如 [download]）'),
        ('[download]\ncount = 5\n这一行没有等号\n', '第 3 行无法识别，每行应该是"键 = 值"、[节名] 或 # 开头的注释'),
    ])
    def test_syntax_errors_name_the_line(self, data_dir, text, reason):
        path = data_dir / 'config.ini'
        path.write_text(text, encoding='utf-8')
        with pytest.raises(util.ConfigError) as info:
            util.load_config()
        assert str(info.value) == (f'配置文件 {path} 有错：{reason}，'
                                   '请修改或删除后重启（删除后会重新生成默认配置）')

    @pytest.mark.parametrize('key, value, reason', [
        ('interval', '3h', '[download] interval = 3h 不是有效的秒数（需要 ≥ 60 的整数）'),
        ('interval', '59', '[download] interval = 59 不是有效的秒数（需要 ≥ 60 的整数）'),
        ('interval', '600.5', '[download] interval = 600.5 不是有效的秒数（需要 ≥ 60 的整数）'),
        ('count', '0', '[download] count = 0 不是有效的数量（需要 ≥ 1 的整数）'),
        ('count', '一百', '[download] count = 一百 不是有效的数量（需要 ≥ 1 的整数）'),
        ('count', '', '[download] count = （空） 不是有效的数量（需要 ≥ 1 的整数）'),
        ('max_page', '-3', '[download] max_page = -3 不是有效的页数（需要 ≥ 1 的整数）'),
        ('delay', '-1', '[download] delay = -1 不是有效的秒数（需要 ≥ 0 的数字）'),
        ('delay', 'nan', '[download] delay = nan 不是有效的秒数（需要 ≥ 0 的数字）'),
        ('delay', 'inf', '[download] delay = inf 不是有效的秒数（需要 ≥ 0 的数字）'),
        ('delay', '1.5s', '[download] delay = 1.5s 不是有效的秒数（需要 ≥ 0 的数字）'),
    ])
    def test_invalid_numbers(self, data_dir, key, value, reason):
        path = data_dir / 'config.ini'
        path.write_text(f'[download]\n{key} = {value}\n', encoding='utf-8')
        with pytest.raises(util.ConfigError) as info:
            util.load_config()
        assert str(info.value) == (f'配置文件 {path} 有错：{reason}，'
                                   '请修改或删除后重启（删除后会重新生成默认配置）')

    def test_boundary_numbers_are_kept_as_strings(self, data_dir):
        (data_dir / 'config.ini').write_text(
            '[download]\ncount = 1\ninterval = 60\ndelay = 0\nmax_page = 1\n', encoding='utf-8')
        util.load_config()
        assert [util.APP_CONFIG['download.' + key] for key in ('count', 'interval', 'delay', 'max_page')] == \
            ['1', '60', '0', '1']

    def test_failed_reload_keeps_current_config(self, data_dir):
        path = data_dir / 'config.ini'
        path.write_text('[download]\ncount = 7\n', encoding='utf-8')
        util.load_config()
        before = dict(util.APP_CONFIG)
        path.write_text('[download]\ncount = 0\n', encoding='utf-8')
        with pytest.raises(util.ConfigError):
            util.load_config()
        assert util.APP_CONFIG == before

    def test_undecodable_file(self, data_dir, monkeypatch):
        monkeypatch.setattr(util.locale, 'getpreferredencoding', lambda do_setlocale=True: 'ascii')
        path = data_dir / 'config.ini'
        path.write_bytes(b'[download]\ncount = \xff\n')
        with pytest.raises(util.ConfigError, match='文件编码无法识别，请用 UTF-8 编码保存') as info:
            util.load_config()
        assert str(path) in str(info.value)

    def test_debug_log_hides_proxy_password(self, data_dir, caplog):
        caplog.set_level(logging.DEBUG, logger='bustag')
        (data_dir / 'config.ini').write_text('[options]\nproxy = http://alice:s3cret@127.0.0.1:7890\n',
                                             encoding='utf-8')
        util.load_config()
        log = '\n'.join(caplog.messages)
        assert 'options.proxy = http://alice:***@127.0.0.1:7890' in log
        assert 's3cret' not in log


@pytest.mark.parametrize('url, shown', [
    ('http://alice:s3cret@127.0.0.1:7890', 'http://alice:***@127.0.0.1:7890'),
    ('socks5://u:p%40ss@proxy.local:1080', 'socks5://u:***@proxy.local:1080'),
    ('http://user:p@ss@host:8080', 'http://user:***@host:8080'),
    ('http://u:pw@host/path?q=1', 'http://u:***@host/path?q=1'),
    ('alice:s3cret@127.0.0.1:7890', 'alice:***@127.0.0.1:7890'),
    ('http://alice@127.0.0.1:7890', 'http://alice@127.0.0.1:7890'),
    ('http://127.0.0.1:7890', 'http://127.0.0.1:7890'),
    ('http://[::1', 'http://[::1'),
    ('10800', '10800'),
    ('', ''),
])
def test_redact_url(url, shown):
    assert util.redact_url(url) == shown


@pytest.mark.parametrize('root_path, dead', [
    ('https://www.busdmm.work', True),
    ('https://busdmm.work/', True),
    ('https://WWW.CDNBUS.BID', True),
    ('https://www.javbus.com', False),
    ('https://notbusdmm.work', False),
    ('', False),
])
def test_dead_domain_warning(root_path, dead):
    warning = util.dead_domain_warning(root_path)
    assert (warning is not None) == dead
    if dead:
        assert root_path in warning
        assert util.DEFAULT_ROOT_PATH in warning


@pytest.mark.parametrize('utc, local', [
    (datetime(2026, 11, 6, 16, 30), '2026-11-07 00:30:00'),
    (datetime(2026, 1, 1, 0, 0, 5), '2026-01-01 08:00:05'),
])
def test_to_localtime_is_fixed_utc_plus_8(utc, local):
    assert util.to_localtime(utc) == local


def test_full_url_follows_root_path(monkeypatch):
    monkeypatch.setitem(util.APP_CONFIG, 'download.root_path', 'https://www.busjav.casa')
    assert util.get_full_url('/ABC-001') == 'https://www.busjav.casa/ABC-001'
    assert util.get_full_url('ABC-001') == 'https://www.busjav.casa/ABC-001'


def test_check_model_folder(data_dir):
    shutil.rmtree(data_dir / 'model')
    util.check_model_folder()
    util.check_model_folder()
    assert (data_dir / 'model').is_dir()


@pytest.fixture
def saved_log_level(monkeypatch):
    level = util.logger.level
    monkeypatch.setattr(util, 'DEBUG', util.DEBUG)
    monkeypatch.delenv('BUSTAG_DEBUG', raising=False)
    monkeypatch.delenv('TESTING', raising=False)
    yield
    util.logger.setLevel(level)


@pytest.mark.parametrize('env, level', [
    ({}, logging.INFO),
    ({'BUSTAG_DEBUG': '0'}, logging.INFO),
    ({'BUSTAG_DEBUG': ''}, logging.INFO),
    ({'BUSTAG_DEBUG': '1'}, logging.DEBUG),
    ({'BUSTAG_DEBUG': 'true'}, logging.DEBUG),
    ({'TESTING': '1'}, logging.DEBUG),
])
def test_log_level(saved_log_level, monkeypatch, env, level):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    util.setup_logging()
    assert util.logger.level == level
    assert util.DEBUG == (level == logging.DEBUG)


class TestStartupErrors:
    @pytest.fixture
    def waited(self, monkeypatch):
        '''init() 不重新配置日志；记录 wait_for_enter_if_console 的调用'''
        calls = []
        monkeypatch.setattr(util, 'setup_logging', lambda: None)
        monkeypatch.setattr(util, 'wait_for_enter_if_console', lambda: calls.append('wait'))
        return calls

    def test_bad_config_exits_after_one_error_line(self, data_dir, monkeypatch, caplog, waited):
        path = data_dir / 'config.ini'
        path.write_text('[download]\ninterval = 3h\n', encoding='utf-8')
        monkeypatch.setenv('BUSTAG_DATA_DIR', str(data_dir))
        with pytest.raises(SystemExit) as info:
            util.init()
        assert info.value.code == 1
        assert waited == ['wait']
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [f'配置文件 {path} 有错：[download] interval = 3h 不是有效的秒数（需要 ≥ 60 的整数），'
                          '请修改或删除后重启（删除后会重新生成默认配置）']

    def test_data_dir_that_cannot_be_created(self, tmp_path, monkeypatch, caplog, waited):
        blocker = tmp_path / 'occupied'
        blocker.write_text('这是一个文件，不是目录', encoding='utf-8')
        monkeypatch.setenv('BUSTAG_DATA_DIR', str(blocker))
        with pytest.raises(SystemExit) as info:
            util.init()
        assert info.value.code == 1
        assert waited == ['wait']
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert errors[0].startswith(f'无法创建数据目录 {blocker}：')

    @pytest.mark.parametrize('frozen, tty, waits', [
        (True, True, True),
        (True, False, False),
        (False, True, False),
    ])
    def test_wait_for_enter_only_in_exe_console(self, monkeypatch, frozen, tty, waits):
        prompts = []

        class Stdin:
            def isatty(self):
                return tty

        monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
        monkeypatch.setattr(sys, 'stdin', Stdin())
        monkeypatch.setattr('builtins.input', prompts.append)
        util.wait_for_enter_if_console()
        assert bool(prompts) == waits


class TestImportInit:
    def start(self, cwd, data_dir=None, code=None):
        code = code or ('import json\n'
                        'from bustag import util\n'
                        'print(json.dumps({"data": util.DATA_DIR, "root": util.APP_CONFIG["download.root_path"]}))\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith('BUSTAG_')}
        env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING='utf-8')
        if data_dir is not None:
            env['BUSTAG_DATA_DIR'] = str(data_dir)
        return subprocess.run([sys.executable, '-c', code], cwd=str(cwd), env=env, capture_output=True,
                              encoding='utf-8', errors='replace', timeout=120)

    def run(self, cwd, data_dir=None):
        result = self.start(cwd, data_dir)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1]), result.stderr

    @pytest.mark.parametrize('text, reason', [
        ('[download]\ncount = 1\ncount = 2\n', '第 3 行的 count 在 [download] 里重复出现'),
        ('[download]\ninterval = 3h\n', '[download] interval = 3h 不是有效的秒数（需要 ≥ 60 的整数）'),
    ])
    def test_bad_config_stops_with_one_line(self, tmp_path, text, reason):
        data = tmp_path / 'broken'
        data.mkdir()
        (data / 'config.ini').write_text(text, encoding='utf-8')
        result = self.start(tmp_path, data, code='import bustag.main\n')
        assert result.returncode == 1
        assert 'Traceback' not in result.stderr
        errors = [line for line in result.stderr.splitlines() if ' - ERROR - ' in line]
        assert len(errors) == 1
        assert errors[0].endswith(f'配置文件 {data / "config.ini"} 有错：{reason}，'
                                  '请修改或删除后重启（删除后会重新生成默认配置）')

    def test_creates_data_folder_config_and_model_folder(self, tmp_path):
        data = tmp_path / 'custom'
        info, log = self.run(tmp_path, data)
        assert os.path.samefile(info['data'], data)
        assert info['root'] == util.DEFAULT_ROOT_PATH
        assert (data / 'config.ini').read_text(encoding='utf-8') == util.DEFAULT_CONFIG_TEXT
        assert (data / 'model').is_dir()
        assert '没有找到配置文件，已生成默认配置' in log

    def test_defaults_to_data_in_current_folder(self, tmp_path):
        info, _ = self.run(tmp_path)
        assert os.path.samefile(info['data'], tmp_path / 'data')
        assert (tmp_path / 'data' / 'config.ini').is_file()
