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


class TestImportInit:
    def run(self, cwd, data_dir=None):
        code = ('import json\n'
                'from bustag import util\n'
                'print(json.dumps({"data": util.DATA_DIR, "root": util.APP_CONFIG["download.root_path"]}))\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith('BUSTAG_')}
        env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING='utf-8')
        if data_dir is not None:
            env['BUSTAG_DATA_DIR'] = str(data_dir)
        result = subprocess.run([sys.executable, '-c', code], cwd=str(cwd), env=env, capture_output=True,
                                encoding='utf-8', errors='replace', timeout=120)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1]), result.stderr

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
