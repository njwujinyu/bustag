'''
数据目录、资源目录、配置文件和日志级别；导入 bustag.util 时的初始化在子进程里验证
'''
import errno
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
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
            'download.max_page', 'options.proxy', 'server.host')}
        assert flat == {'download.root_path': 'https://www.javbus.com', 'download.count': '100',
                        'download.interval': '10800', 'download.delay': '1.5', 'download.max_page': '30',
                        'options.proxy': '', 'server.host': '127.0.0.1'}
        assert util.APP_CONFIG['download']['count'] == '100'

    def test_default_config_shows_the_backfill_limits_as_comments(self):
        section = util.DEFAULT_CONFIG_TEXT.split('[download]')[1].split('[options]')[0]
        lines = section.splitlines()
        assert '# backfill_count = 60' in lines and '# backfill_daily = 300' in lines
        assert '按演员补全' in section and '每天' in section
        # 写成注释：不写的时候就是默认值，以后默认值改了，旧的 config.ini 也跟着变
        util.load_config()
        assert (util.APP_CONFIG['download.backfill_count'], util.APP_CONFIG['download.backfill_daily']) == ('60', '300')

    def test_default_config_shows_the_daily_av_wiki_limit_as_a_comment(self):
        section = util.DEFAULT_CONFIG_TEXT.split('[download]')[1].split('[options]')[0]
        lines = section.splitlines()
        assert '# avwiki_daily = 200' in lines
        note = lines[lines.index('# avwiki_daily = 200') - 1]
        assert '补演员名' in note and '每天' in note and '请求' in note
        util.load_config()
        assert util.APP_CONFIG['download.avwiki_daily'] == '200'

    @pytest.mark.parametrize('value, reason', [
        ('0', '[download] avwiki_daily = 0 不是有效的次数（需要 ≥ 1 的整数）'),
        ('二百', '[download] avwiki_daily = 二百 不是有效的次数（需要 ≥ 1 的整数）'),
    ])
    def test_the_daily_av_wiki_limit_must_be_a_whole_number(self, data_dir, value, reason):
        (data_dir / 'config.ini').write_text(f'[download]\navwiki_daily = {value}\n', encoding='utf-8')
        with pytest.raises(util.ConfigError) as info:
            util.load_config()
        assert reason in str(info.value)
        (data_dir / 'config.ini').write_text('[download]\navwiki_daily = 1\n', encoding='utf-8')
        util.load_config()
        assert util.APP_CONFIG['download.avwiki_daily'] == '1'

    def test_default_config_explains_how_to_open_up_to_the_local_network(self):
        section = util.DEFAULT_CONFIG_TEXT.split('[server]')[1]
        assert 'host = 127.0.0.1' in section.splitlines()
        assert '0.0.0.0' in section and '没有登录' in section and 'BUSTAG_HOST' in section

    def test_listen_address_from_the_config_file(self, data_dir):
        (data_dir / 'config.ini').write_text('[server]\n# 让局域网里的手机也能打开\nhost = 0.0.0.0\n', encoding='utf-8')
        util.load_config()
        assert util.APP_CONFIG['server.host'] == '0.0.0.0'
        assert util.APP_CONFIG['download.count'] == '100'

    def test_existing_config_is_kept_and_defaults_fill_the_rest(self, data_dir):
        path = data_dir / 'config.ini'
        path.write_text('[download]\ncount = 7\n', encoding='utf-8')
        assert util.create_default_config(str(path)) is False
        util.load_config()
        assert path.read_text(encoding='utf-8') == '[download]\ncount = 7\n'
        assert util.APP_CONFIG['download.count'] == '7'
        assert util.APP_CONFIG['download.interval'] == '10800'
        assert util.APP_CONFIG['options.proxy'] == ''
        # 按演员补全：每次每位 60 部，每天一共 300 部
        assert (util.APP_CONFIG['download.backfill_count'], util.APP_CONFIG['download.backfill_daily']) == ('60', '300')
        # 旧的 config.ini 里没有 [server]：按默认值，只监听本机
        assert util.APP_CONFIG['server.host'] == '127.0.0.1'

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

    @pytest.mark.parametrize('system_encoding, file_encoding', [
        ('utf-8', 'gbk'),
        ('gbk', 'utf-16'),
        ('utf-8', 'utf-16'),
    ], ids=['gbk-file-on-utf8-system', 'utf16-file-on-gbk-system', 'utf16-file-on-utf8-system'])
    def test_file_in_neither_utf8_nor_system_encoding(self, data_dir, monkeypatch, system_encoding, file_encoding):
        monkeypatch.setattr(util.locale, 'getpreferredencoding', lambda do_setlocale=True: system_encoding)
        (data_dir / 'config.ini').write_bytes('[download]\n# 每次抓取的数量\ncount = 7\n'.encode(file_encoding))
        with pytest.raises(util.ConfigError, match='文件编码无法识别'):
            util.load_config()

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
        ('backfill_count', '0', '[download] backfill_count = 0 不是有效的数量（需要 ≥ 1 的整数）'),
        ('backfill_count', '六十', '[download] backfill_count = 六十 不是有效的数量（需要 ≥ 1 的整数）'),
        ('backfill_daily', '-300', '[download] backfill_daily = -300 不是有效的数量（需要 ≥ 1 的整数）'),
        ('backfill_daily', '300.5', '[download] backfill_daily = 300.5 不是有效的数量（需要 ≥ 1 的整数）'),
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
            '[download]\ncount = 1\ninterval = 60\ndelay = 0\nmax_page = 1\nbackfill_count = 1\nbackfill_daily = 1\n',
            encoding='utf-8')
        util.load_config()
        keys = ('count', 'interval', 'delay', 'max_page', 'backfill_count', 'backfill_daily')
        assert [util.APP_CONFIG['download.' + key] for key in keys] == ['1', '60', '0', '1', '1', '1']

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

    @pytest.mark.parametrize('proxy, shown, secret', [
        ('http://alice:s3cret@127.0.0.1:7890', 'http://alice:***@127.0.0.1:7890', 's3cret'),
        ('http://ghp_T0KEN@127.0.0.1:7890', 'http://***@127.0.0.1:7890', 'ghp_T0KEN'),
        ('http://127.0.0.1:7890', 'http://127.0.0.1:7890', None),
    ], ids=['user-and-password', 'user-only', 'no-credentials'])
    def test_debug_log_hides_proxy_credentials(self, data_dir, caplog, proxy, shown, secret):
        caplog.set_level(logging.DEBUG, logger='bustag')
        (data_dir / 'config.ini').write_text(f'[options]\nproxy = {proxy}\n', encoding='utf-8')
        util.load_config()
        log = '\n'.join(caplog.messages)
        assert f'options.proxy = {shown}' in log
        if secret:
            assert secret not in log


@pytest.mark.parametrize('url, shown', [
    ('http://alice:s3cret@127.0.0.1:7890', 'http://alice:***@127.0.0.1:7890'),
    ('socks5://u:p%40ss@proxy.local:1080', 'socks5://u:***@proxy.local:1080'),
    ('http://user:p@ss@host:8080', 'http://user:***@host:8080'),
    ('http://u:pw@host/path?q=1', 'http://u:***@host/path?q=1'),
    ('alice:s3cret@127.0.0.1:7890', 'alice:***@127.0.0.1:7890'),
    ('http://user:@host:8080', 'http://user:***@host:8080'),
    ('http://alice@127.0.0.1:7890', 'http://***@127.0.0.1:7890'),
    ('socks5h://ghp_T0KEN@proxy.local:1080', 'socks5h://***@proxy.local:1080'),
    ('ghp_T0KEN@127.0.0.1:7890', '***@127.0.0.1:7890'),
    ('http://@127.0.0.1:7890', 'http://@127.0.0.1:7890'),
    ('http://127.0.0.1:7890', 'http://127.0.0.1:7890'),
    ('http://[::1', 'http://[::1'),
    ('10800', '10800'),
    ('', ''),
])
def test_redact_url(url, shown):
    assert util.redact_url(url) == shown


@pytest.mark.parametrize('text, shown', [
    ('https://av-wiki.net/av-actress/mingzi-jia/', 'https://av-wiki.net/av-actress/…/'),
    ('https://av-wiki.net/av-actress/mingzi-jia', 'https://av-wiki.net/av-actress/…'),
    ('https://av-wiki.net/av-actress/%E5%90%8D%E5%AD%97/?page=2', 'https://av-wiki.net/av-actress/…/?page=2'),
    ("Max retries exceeded with url: /av-actress/mingzi-jia/ (Caused by ReadTimeoutError('…'))",
     "Max retries exceeded with url: /av-actress/…/ (Caused by ReadTimeoutError('…'))"),
    ('读了 /av-actress/mingzi-jia/ 和 /av-actress/mingzi-yi/', '读了 /av-actress/…/ 和 /av-actress/…/'),
    ('HTTP 503：https://av-wiki.net/av-actress/mingzi-jia/。', 'HTTP 503：https://av-wiki.net/av-actress/…/。'),
    # JavBus 按名字找演员页，地址里是她的名字
    ('https://www.javbus.com/searchstar/%E6%BC%94%E5%91%98%E7%94%B2', 'https://www.javbus.com/searchstar/…'),
    ('页面不存在（HTTP 404）：https://www.javbus.com/searchstar/演员甲。', '页面不存在（HTTP 404）：https://www.javbus.com/searchstar/…。'),
    ('url: /searchstar/mingzi%20jia/2 (Caused by x)', 'url: /searchstar/…/2 (Caused by x)'),
    # 别的地址原样：搜索页按番号查，网站的详情页、封面地址里只有番号和编号，演员页的地址里是编号
    ('https://av-wiki.net/?s=ABC-001', 'https://av-wiki.net/?s=ABC-001'),
    ('https://www.javbus.com/ABC-001', 'https://www.javbus.com/ABC-001'),
    ('https://www.javbus.com/star/a1/2', 'https://www.javbus.com/star/a1/2'),
    ('https://av-wiki.net/av-actress/', 'https://av-wiki.net/av-actress/'),
    ('', ''),
])
def test_hide_actress(text, shown):
    assert util.hide_actress(text) == shown


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


@pytest.mark.parametrize('error, text', [
    (PermissionError(errno.EACCES, 'Permission denied'), '拒绝访问：没有权限，或者正被别的程序占用'),
    (IsADirectoryError(errno.EISDIR, 'Is a directory'), '它是一个文件夹，不是文件'),
    (FileNotFoundError(errno.ENOENT, 'No such file or directory'), '找不到这个文件或文件夹'),
    (OSError(errno.ENOSPC, 'No space left on device'), '磁盘空间不足'),
    (OSError(errno.EROFS, 'Read-only file system'), '磁盘是只读的'),
    (OSError(errno.ENOLCK, 'No locks available'), '没有可用的文件锁'),
    (OSError(errno.EOPNOTSUPP, 'Operation not supported'), '不支持这个操作'),
    (OSError(errno.EINVAL, 'Invalid argument'), '参数无效'),
    # 系统自己给的中文原因、调用方写的中文原因原样用
    (PermissionError(errno.EACCES, '拒绝访问。'), '拒绝访问。'),
    (OSError(errno.ENOSPC, '磁盘空间不足'), '磁盘空间不足'),
    # 没有对应说法的英文原因、没有错误码的，原样给出
    (OSError(errno.EXDEV, 'Invalid cross-device link'), 'Invalid cross-device link'),
    (OSError('没有错误码'), '没有错误码'),
], ids=['denied', 'is-a-folder', 'not-found', 'disk-full', 'read-only', 'no-locks', 'not-supported', 'invalid',
        'chinese-from-the-system', 'chinese-from-the-caller', 'no-chinese-for-it', 'no-code'])
def test_os_error_text(error, text):
    assert util.os_error_text(error) == text


class TestSaveFile:
    '''
    模型文件和推荐设定都这样保存：先写同目录的临时文件再替换，别处读到的要么是原来的文件、要么是新的；
    失败时原来的文件不变，也不留下临时文件
    '''

    @pytest.fixture
    def path(self, tmp_path):
        '''一个空文件夹里要保存的文件的路径'''
        (tmp_path / 'saved').mkdir()
        return tmp_path / 'saved' / 'saved.json'

    def test_saves_the_bytes_and_leaves_no_temporary_file(self, path):
        util.save_file(str(path), b'first')
        util.save_file(str(path), b'second')
        assert path.read_bytes() == b'second'
        assert [entry.name for entry in path.parent.iterdir()] == ['saved.json']

    def test_a_write_that_fails_half_way_keeps_the_old_file(self, path, monkeypatch):
        path.write_bytes(b'old')
        real_write_temp = util.write_temp_file

        def half_then_full(target, write):
            def fails(f):
                f.write(b'ne')
                raise OSError(errno.ENOSPC, '磁盘空间不足')
            return real_write_temp(target, fails)

        monkeypatch.setattr(util, 'write_temp_file', half_then_full)
        with pytest.raises(OSError, match='磁盘空间不足'):
            util.save_file(str(path), b'new')
        assert path.read_bytes() == b'old'
        assert [entry.name for entry in path.parent.iterdir()] == ['saved.json']

    def test_a_target_that_is_briefly_in_use_is_tried_again(self, path, monkeypatch):
        path.write_bytes(b'old')
        real_replace = os.replace
        attempts, waits = [], []

        def in_use_twice(source, target):
            attempts.append(target)
            if len(attempts) <= 2:
                raise PermissionError(errno.EACCES, '另一个程序正在使用此文件')
            real_replace(source, target)

        monkeypatch.setattr(util.os, 'replace', in_use_twice)
        monkeypatch.setattr(util.time, 'sleep', waits.append)
        util.save_file(str(path), b'new')
        assert path.read_bytes() == b'new'
        assert waits == list(util.REPLACE_RETRY_DELAYS[:2]) and len(attempts) == 3

    def test_a_target_that_stays_in_use_fails_and_leaves_nothing_behind(self, path, monkeypatch):
        path.write_bytes(b'old')
        attempts = []

        def in_use(source, target):
            attempts.append(target)
            raise PermissionError(errno.EACCES, '另一个程序正在使用此文件')

        monkeypatch.setattr(util.os, 'replace', in_use)
        monkeypatch.setattr(util.time, 'sleep', lambda seconds: None)
        with pytest.raises(PermissionError):
            util.save_file(str(path), b'new')
        assert len(attempts) == len(util.REPLACE_RETRY_DELAYS) + 1
        assert path.read_bytes() == b'old'
        assert [entry.name for entry in path.parent.iterdir()] == ['saved.json']

    def test_the_swap_lock_is_held_for_each_replacement_and_not_while_waiting_to_try_again(self, path, monkeypatch):
        # 读这个文件的一方读的时候拿同一把锁，就不会撞上替换的那一下；等着重试的时候不拿，读的一方不用跟着等
        path.write_bytes(b'old')
        real_replace = os.replace
        steps = []

        class Swap:
            def __enter__(self):
                steps.append('lock')

            def __exit__(self, *exc):
                steps.append('unlock')

        def in_use_twice(source, target):
            steps.append('replace')
            if steps.count('replace') <= 2:
                raise PermissionError(errno.EACCES, '另一个程序正在使用此文件')
            real_replace(source, target)

        monkeypatch.setattr(util.os, 'replace', in_use_twice)
        monkeypatch.setattr(util.time, 'sleep', lambda seconds: steps.append('wait'))
        util.save_file(str(path), b'new', swap=Swap())
        assert steps == ['lock', 'replace', 'unlock', 'wait', 'lock', 'replace', 'unlock', 'wait', 'lock', 'replace', 'unlock']
        assert path.read_bytes() == b'new'


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


def test_the_console_hides_credentials_like_the_file(saved_log_level, monkeypatch, capsys):
    monkeypatch.setattr(logging.root, 'handlers', [])
    monkeypatch.setitem(util.APP_CONFIG, 'options.proxy', 'alice:s3cret@127.0.0.1:7890')
    util.setup_logging()
    logging.getLogger('urllib3.connectionpool').warning(
        'Retrying after connection broken by proxy http://bob:pa55@10.0.0.1:3128: /')
    try:
        raise ValueError('Failed to parse: alice:s3cret@127.0.0.1:7890')
    except ValueError:
        util.logger.exception('抓取出错')
    shown = capsys.readouterr().err
    assert 's3cret' not in shown and 'pa55' not in shown
    assert 'proxy http://bob:***@10.0.0.1:3128: /' in shown
    assert 'ValueError: Failed to parse: alice:***@127.0.0.1:7890' in shown
    assert log_line('ERROR', '抓取出错').search(shown)


def warnings_of(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def log_line(level, message):
    '''日志文件里的一行：时间 - 级别 - 文字，和控制台的格式一样'''
    return re.compile(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d - ' + level + ' - ' + re.escape(message))


@pytest.fixture
def log_file(data_dir, monkeypatch):
    '''
    开着日志文件的测试用：返回日志文件的路径（start_file_log 之后才有）。
    结束后把加在根 logger 上的 handler 拿掉、关掉，还原没接住的异常的去处
    '''
    monkeypatch.setattr(util, '_log_handler', None)
    monkeypatch.setattr(util, '_log_refused', False)
    monkeypatch.setattr(util, '_early_warnings', [])
    monkeypatch.setattr(sys, 'excepthook', sys.excepthook)
    monkeypatch.setattr(threading, 'excepthook', threading.excepthook)
    yield data_dir / 'logs' / 'bustag.log'
    handler = util._log_handler
    if handler is not None:
        logging.getLogger().removeHandler(handler)
        handler.close()


def lines_of(path):
    return path.read_text(encoding='utf-8').splitlines()


class TestFileLog:
    '''网页服务把日志同时写进 <数据目录>/logs/bustag.log'''

    def test_log_lines_go_to_the_file_in_the_format_of_the_console(self, log_file):
        assert not log_file.exists()
        assert util.start_file_log() == str(log_file)
        assert util.get_log_path() == str(log_file)
        util.logger.info('定时任务已启动')
        util.logger.warning('封面下载失败 ABC-001')
        first, second = lines_of(log_file)
        assert log_line('INFO', '定时任务已启动').fullmatch(first)
        assert log_line('WARNING', '封面下载失败 ABC-001').fullmatch(second)
        assert util.LOG_FORMAT == '%(asctime)s - %(levelname)s - %(message)s'

    def test_nothing_is_written_before_it_is_started(self, log_file):
        util.logger.info('命令行的日志只在控制台')
        assert not log_file.exists()
        assert util.active_log_file() is None
        util.start_file_log()
        assert util.active_log_file() == str(log_file)

    def test_starting_twice_writes_each_line_once(self, log_file):
        assert util.start_file_log() == util.start_file_log() == str(log_file)
        util.logger.info('只写一遍')
        assert len(lines_of(log_file)) == 1

    def test_a_warning_given_before_the_file_is_started_is_written_into_it_then(self, log_file, caplog):
        util.warn_early('锁文件 x 打不开')
        assert not log_file.exists()
        assert warnings_of(caplog) == ['锁文件 x 打不开']
        util.start_file_log()
        line, = lines_of(log_file)
        assert log_line('WARNING', '锁文件 x 打不开').fullmatch(line)
        # 控制台那一遍是之前说的；补进文件的这一遍不再经过控制台
        assert warnings_of(caplog) == ['锁文件 x 打不开']
        util.logger.info('以后的行照常写')
        assert len(lines_of(log_file)) == 2

    def test_what_was_said_before_is_written_once_however_often_the_file_is_started(self, log_file):
        util.warn_early('只补一遍')
        util.start_file_log()
        util.start_file_log()
        assert len(lines_of(log_file)) == 1

    def test_a_warning_given_after_the_file_is_started_goes_to_both_at_once(self, log_file, caplog):
        util.start_file_log()
        util.warn_early('文件开了以后说的')
        assert warnings_of(caplog) == ['文件开了以后说的']
        assert len(lines_of(log_file)) == 1
        util.start_file_log()
        assert len(lines_of(log_file)) == 1

    def test_a_log_file_that_cannot_be_made_has_nothing_to_catch_up_on(self, data_dir, log_file, caplog):
        (data_dir / 'logs').write_text('这里被一个文件占了', encoding='utf-8')
        util.warn_early('之前说的')
        assert util.start_file_log() is None
        said, refused = warnings_of(caplog)
        assert said == '之前说的' and refused.startswith(f'日志文件 {log_file} 写不了（')

    def test_warnings_and_errors_of_other_libraries_are_written_their_chatter_is_not(self, log_file):
        util.start_file_log()
        logging.getLogger('waitress.queue').warning('Task queue depth is 1')
        logging.getLogger('apscheduler.executors.default').error('Job "download_job" raised an exception')
        logging.getLogger('apscheduler.scheduler').info('Scheduler started')
        logging.getLogger('urllib3.connectionpool').debug('Starting new HTTPS connection')
        text = log_file.read_text(encoding='utf-8')
        assert ' - WARNING - Task queue depth is 1' in text
        assert ' - ERROR - Job "download_job" raised an exception' in text
        assert 'Scheduler started' not in text and 'Starting new HTTPS connection' not in text

    def test_tracebacks_are_written(self, log_file):
        util.start_file_log()
        try:
            raise RuntimeError('数据库文件坏了')
        except RuntimeError:
            util.logger.exception('服务异常退出')
        text = log_file.read_text(encoding='utf-8')
        assert ' - ERROR - 服务异常退出\nTraceback (most recent call last):' in text
        assert 'RuntimeError: 数据库文件坏了' in text

    def test_a_full_file_is_set_aside_and_only_five_old_ones_are_kept(self, log_file, monkeypatch):
        monkeypatch.setattr(util, 'LOG_MAX_BYTES', 2000)
        util.start_file_log()
        for n in range(400):
            util.logger.info(f'第 {n:03d} 行，凑够长度：{"字" * 20}')
        names = sorted(path.name for path in log_file.parent.iterdir())
        assert names == ['bustag.log'] + [f'bustag.log.{n}' for n in range(1, util.LOG_BACKUPS + 1)]
        assert util.LOG_BACKUPS == 5
        # 换文件发生在写下一行之前，所以每个文件最多超出一行
        assert all(path.stat().st_size < 2000 + 200 for path in log_file.parent.iterdir())
        # 最新的在 bustag.log 里，往前依次是 .1、.2……，中间不缺行；最老的已经删掉
        kept = [line for name in reversed(names) for line in lines_of(log_file.parent / name)]
        numbers = [int(re.search(r'第 (\d+) 行', line).group(1)) for line in kept]
        assert numbers == list(range(numbers[0], 400)) and numbers[0] > 0

    def test_the_real_limit_is_one_megabyte(self):
        assert util.LOG_MAX_BYTES == 1024 * 1024

    def test_a_log_folder_that_cannot_be_made_only_costs_a_warning(self, data_dir, log_file, caplog):
        (data_dir / 'logs').write_text('这里被一个文件占了', encoding='utf-8')
        assert util.start_file_log() is None
        # 网页服务启动时会调两次（占端口之前一次，create_app 里一次），只说一遍，也不再去试
        (data_dir / 'logs').unlink()
        assert util.start_file_log() is None
        assert not log_file.exists()
        warning, = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning.startswith(f'日志文件 {log_file} 写不了（')
        assert warning.endswith('），这次运行的日志只显示在控制台')
        assert util.active_log_file() is None
        util.logger.info('程序照常运行')

    def test_the_reason_a_log_file_cannot_be_made_is_given_in_chinese(self, log_file, monkeypatch, caplog):
        def denied(path):
            raise PermissionError(errno.EACCES, 'Permission denied')

        monkeypatch.setattr(util, '_LogFileHandler', denied)
        assert util.start_file_log() is None
        assert warnings_of(caplog) == [f'日志文件 {log_file} 写不了（拒绝访问：没有权限，或者正被别的程序占用），这次运行的日志只显示在控制台']

    def test_the_reason_a_file_cannot_be_set_aside_is_given_in_chinese(self, log_file, monkeypatch, caplog):
        monkeypatch.setattr(util, 'LOG_MAX_BYTES', 500)
        util.start_file_log()

        def locked(source, dest):
            raise PermissionError(errno.EACCES, 'Permission denied')

        monkeypatch.setattr(util._log_handler, 'rotate', locked)
        for n in range(40):
            util.logger.info(f'第 {n:02d} 行，凑够长度：{"字" * 20}')
        assert warnings_of(caplog) == [f'日志文件 {log_file} 换不了新文件（拒绝访问：没有权限，或者正被别的程序占用），这次运行先接着写在原来的文件里']

    def test_a_write_that_fails_is_reported_once_and_does_not_stop_the_program(self, log_file, caplog, capsys):
        util.start_file_log()

        class FullDisk:
            def write(self, text):
                raise OSError(28, 'No space left on device')

            def flush(self):
                pass

            def close(self):
                pass

            def seek(self, *args):
                pass

            def tell(self):
                return 0

        util._log_handler.stream.close()
        util._log_handler.stream = FullDisk()
        for n in range(3):
            util.logger.info(f'第 {n} 行')
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [f'日志文件 {log_file} 写不进去（磁盘空间不足），日志先只显示在控制台']
        # logging 自己不再往控制台打"Logging error"和异常栈
        assert 'Logging error' not in capsys.readouterr().err

    def test_a_file_that_cannot_be_set_aside_is_written_on(self, log_file, monkeypatch, caplog):
        monkeypatch.setattr(util, 'LOG_MAX_BYTES', 500)
        util.start_file_log()
        attempts = []

        def locked(source, dest):
            attempts.append(dest)
            raise PermissionError(13, '另一个程序正在使用此文件')

        monkeypatch.setattr(util._log_handler, 'rotate', locked)
        for n in range(40):
            util.logger.info(f'第 {n:02d} 行，凑够长度：{"字" * 20}')
        # 只试了一次：这次运行接着写在原来的文件里，一行不丢
        assert len(attempts) == 1
        assert [path.name for path in log_file.parent.iterdir()] == ['bustag.log']
        written = [line for line in lines_of(log_file) if '凑够长度' in line]
        assert len(written) == 40
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert warnings[0].startswith(f'日志文件 {log_file} 换不了新文件（')
        assert warnings[0].endswith('），这次运行先接着写在原来的文件里')

    @pytest.mark.parametrize('proxy, secret, shown', [
        ('http://alice:s3cret@127.0.0.1:7890', 's3cret', 'http://alice:***@127.0.0.1:7890'),
        ('alice:s3cret@127.0.0.1:7890', 's3cret', 'alice:***@127.0.0.1:7890'),
        ('http://ghp_T0KEN@127.0.0.1:7890', 'ghp_T0KEN', 'http://***@127.0.0.1:7890'),
    ], ids=['user-and-password', 'no-scheme', 'token-only'])
    def test_the_proxy_password_never_reaches_the_file(self, log_file, monkeypatch, proxy, secret, shown):
        monkeypatch.setitem(util.APP_CONFIG, 'options.proxy', f' {proxy} ')
        util.start_file_log()
        logging.getLogger('urllib3.connectionpool').warning(f'Retrying after connection broken by proxy {proxy}: /')
        try:
            raise ValueError(f'Failed to parse: {proxy}')
        except ValueError:
            util.logger.exception('抓取出错')
        text = log_file.read_text(encoding='utf-8')
        assert secret not in text
        # 一处在第三方库的警告里，一处在异常栈最后一行的报错里
        assert text.count(shown) == 2

    def test_credentials_in_any_other_address_are_hidden_too(self, log_file, monkeypatch):
        monkeypatch.setenv('HTTPS_PROXY', 'http://bob:pa55@10.0.0.1:3128')
        util.start_file_log()
        logging.getLogger('requests').warning('proxy http://bob:pa55@10.0.0.1:3128 and socks5://carol:t0p@proxy.local:1080/')
        util.logger.info('普通的地址不动：http://127.0.0.1:8000/tagit?like=1 和 a@b 这样的文字')
        text = log_file.read_text(encoding='utf-8')
        assert 'pa55' not in text and 't0p' not in text
        assert 'http://bob:***@10.0.0.1:3128 and socks5://carol:***@proxy.local:1080/' in text
        assert '普通的地址不动：http://127.0.0.1:8000/tagit?like=1 和 a@b 这样的文字' in text


class TestUncaughtExceptions:
    def test_an_exception_nobody_caught_in_a_thread_is_logged_with_its_traceback(self, log_file):
        util.start_file_log()
        util.log_uncaught_exceptions()

        def work():
            raise RuntimeError('线程里出错了')

        thread = threading.Thread(target=work, name='bustag-jobs')
        thread.start()
        thread.join(timeout=10)
        text = log_file.read_text(encoding='utf-8')
        assert ' - ERROR - 线程 bustag-jobs 里有没接住的异常\nTraceback (most recent call last):' in text
        assert 'RuntimeError: 线程里出错了' in text

    def test_an_exception_nobody_caught_in_the_main_thread_is_logged_with_its_traceback(self, log_file):
        util.start_file_log()
        util.log_uncaught_exceptions()
        try:
            raise RuntimeError('主线程里出错了')
        except RuntimeError as ex:
            sys.excepthook(type(ex), ex, ex.__traceback__)
        text = log_file.read_text(encoding='utf-8')
        assert ' - ERROR - 程序出错退出\nTraceback (most recent call last):' in text
        assert 'RuntimeError: 主线程里出错了' in text

    def test_ctrl_c_is_not_an_error(self, log_file, monkeypatch):
        util.start_file_log()
        util.log_uncaught_exceptions()
        passed_on = []
        monkeypatch.setattr(sys, '__excepthook__', lambda *args: passed_on.append(args[0]))
        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
        assert passed_on == [KeyboardInterrupt]
        assert not log_file.exists() or log_file.read_text(encoding='utf-8') == ''


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

    @pytest.mark.parametrize('broken, error, message', [
        ('resolve_data_dir', OSError(errno.EROFS, 'Read-only file system'), '无法创建数据目录 {data}：磁盘是只读的'),
        ('check_model_folder', OSError(errno.ENOSPC, 'No space left on device'),
         '无法创建模型目录 {model}：磁盘空间不足'),
        ('_read_text', PermissionError(errno.EACCES, 'Permission denied'), '无法读写配置文件 {config}：拒绝访问：没有权限，或者正被别的程序占用'),
    ], ids=['data-dir', 'model-dir', 'config'])
    def test_the_reason_is_given_in_chinese(self, data_dir, monkeypatch, caplog, waited, broken, error, message):
        def fail(*args):
            raise error

        (data_dir / 'config.ini').write_text('[download]\ncount = 100\n', encoding='utf-8')
        monkeypatch.setenv('BUSTAG_DATA_DIR', str(data_dir))
        monkeypatch.setattr(util, broken, fail)
        with pytest.raises(SystemExit):
            util.init()
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [message.format(data=data_dir, model=util.get_data_path(util.MODEL_PATH),
                                         config=data_dir / 'config.ini')]

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
