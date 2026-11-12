'''
推荐设定：数据目录里的 recommend.json 的读写、校验和并发保存
'''
import errno
import json
import logging
import os
import re
import threading
import time

import pytest

from bustag import util
from bustag.app import pick
from bustag.app.pick import AddResult, Settings


def settings_file(data_dir):
    return data_dir / 'recommend.json'


def write_file(data_dir, content):
    data = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode('utf-8')
    settings_file(data_dir).write_bytes(data)


def stored(data_dir):
    return json.loads(settings_file(data_dir).read_text(encoding='utf-8'))


def warnings_in(caplog):
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


class TestLoad:
    def test_missing_file_means_empty_settings(self, data_dir, caplog):
        settings = pick.load_settings()
        assert settings == Settings(stars=(), tags=(), mode='all', model_only=False, problem='')
        assert settings.is_empty
        assert not settings_file(data_dir).exists()
        assert warnings_in(caplog) == []

    def test_reads_the_documented_format(self, data_dir):
        write_file(data_dir, {'version': 1, 'stars': ['演员甲', '演员乙'], 'tags': ['标签一'],
                              'mode': 'any', 'model_only': True})
        settings = pick.load_settings()
        assert settings == Settings(stars=('演员甲', '演员乙'), tags=('标签一',), mode='any', model_only=True)
        assert not settings.is_empty
        assert settings.names('star') == ('演员甲', '演员乙')
        assert settings.names('tag') == ('标签一',)

    def test_file_saved_with_a_byte_order_mark_is_read(self, data_dir):
        text = json.dumps({'version': 1, 'stars': ['演员甲']}, ensure_ascii=False)
        write_file(data_dir, b'\xef\xbb\xbf' + text.encode('utf-8'))
        assert pick.load_settings().stars == ('演员甲',)

    @pytest.mark.parametrize('content, expected', [
        ({'version': 1}, Settings()),
        ({'version': 1, 'tags': ['标签一']}, Settings(tags=('标签一',))),
        ({'version': 1, 'mode': 'any'}, Settings(mode='any')),
    ], ids=['only-version', 'only-tags', 'only-mode'])
    def test_missing_keys_use_the_defaults(self, data_dir, content, expected):
        write_file(data_dir, content)
        assert pick.load_settings() == expected

    def test_names_are_trimmed_and_listed_once(self, data_dir):
        write_file(data_dir, {'version': 1, 'stars': [' 演员甲 ', '演员甲', 'Abc', 'aBC', '  '],
                              'tags': ['4K', '4k\u3000']})
        settings = pick.load_settings()
        assert settings.stars == ('演员甲', 'Abc')
        assert settings.tags == ('4K',)

    @pytest.mark.parametrize('content', [
        b'{"version": 1, "stars": [',
        b'\xff\xfe\x00not utf-8',
        b'',
        b'[' * 200000,
        [],
        'text',
        {'stars': ['演员甲']},
        {'version': 2, 'stars': ['演员甲']},
        {'version': '1', 'stars': ['演员甲']},
        {'version': True, 'stars': ['演员甲']},
        {'version': 1, 'stars': '演员甲'},
        {'version': 1, 'stars': ['演员甲', 7]},
        {'version': 1, 'tags': {'标签一': 1}},
        {'version': 1, 'mode': 'both'},
        {'version': 1, 'model_only': 'yes'},
        {'version': 1, 'model_only': 1},
        {'version': 1, 'stars': ['x' * 201]},
        {'version': 1, 'tags': [f'标签{n}' for n in range(101)]},
        {'version': 1, 'stars': ['演员\x00甲']},
    ], ids=['truncated', 'not-utf8', 'empty-file', 'nested-too-deep', 'list', 'string', 'no-version', 'newer-version',
            'version-as-text', 'version-as-bool', 'stars-not-a-list', 'star-not-text', 'tags-not-a-list',
            'unknown-mode', 'model-only-as-text', 'model-only-as-number', 'name-too-long', 'too-many-names',
            'control-character'])
    def test_broken_file_counts_as_no_settings_and_is_reported(self, data_dir, caplog, content):
        write_file(data_dir, content)
        before = settings_file(data_dir).read_bytes()
        settings = pick.load_settings()
        assert (settings.stars, settings.tags, settings.mode, settings.model_only) == ((), (), 'all', False)
        assert settings.is_empty
        assert settings.problem
        messages = warnings_in(caplog)
        assert len(messages) == 1
        assert messages[0].startswith(f'推荐设定文件 {settings_file(data_dir)} ')
        assert settings.problem in messages[0]
        assert settings_file(data_dir).read_bytes() == before

    def test_the_same_broken_file_is_reported_once(self, data_dir, caplog):
        write_file(data_dir, b'{broken')
        for _ in range(3):
            assert pick.load_settings().problem
        assert len(warnings_in(caplog)) == 1
        write_file(data_dir, b'{broken again')
        assert pick.load_settings().problem
        assert len(warnings_in(caplog)) == 2

    def test_a_file_far_larger_than_any_settings_is_not_read_in(self, data_dir, caplog, monkeypatch):
        padded = json.dumps({'version': 1, 'stars': ['演员甲']}).encode('utf-8') + b' ' * (pick.MAX_FILE_SIZE + 1)
        write_file(data_dir, padded)
        monkeypatch.setattr(pick.json, 'loads', lambda text: pytest.fail('不该去解析这么大的文件'))
        settings = pick.load_settings()
        assert settings.is_empty
        assert settings.problem == f'文件超过 {pick.MAX_FILE_SIZE} 字节'
        assert len(warnings_in(caplog)) == 1

    def test_a_full_settings_file_is_well_under_the_size_limit(self, data_dir):
        pick.add_names('star', [f'{n:02d}' + '演' * 198 for n in range(100)])
        pick.add_names('tag', [f'{n:02d}' + '签' * 198 for n in range(100)])
        assert len(pick.load_settings().stars) == len(pick.load_settings().tags) == 100
        assert settings_file(data_dir).stat().st_size < pick.MAX_FILE_SIZE // 4

    def test_a_folder_in_place_of_the_file_counts_as_no_settings(self, data_dir, caplog):
        settings_file(data_dir).mkdir()
        settings = pick.load_settings()
        assert settings.is_empty and settings.problem
        # 系统给的原因是英文（Permission denied / Is a directory），页面上要的是中文
        assert re.fullmatch(r'读取失败：[^A-Za-z]+', settings.problem)
        assert len(warnings_in(caplog)) == 1


class TestSplitNames:
    @pytest.mark.parametrize('text, expected', [
        ('演员甲', ['演员甲']),
        ('演员甲,演员乙，演员丙', ['演员甲', '演员乙', '演员丙']),
        ('演员甲\n演员乙\r\n演员丙\r演员丁', ['演员甲', '演员乙', '演员丙', '演员丁']),
        ('  演员甲  ,, ，\n\n 演员乙 ', ['演员甲', '演员乙']),
        ('first name、second', ['first name、second']),
        ('演员甲,演员甲', ['演员甲', '演员甲']),
        ('演员甲\t演员乙\t\t 演员丙\n演员丁\t演员戊', ['演员甲', '演员乙', '演员丙', '演员丁', '演员戊']),
        ('', []),
        ('  \n ,，\u3000\t', []),
        (None, []),
    ], ids=['one', 'commas', 'lines', 'blanks', 'space-and-dun-hao-stay', 'repeats-stay', 'cells-of-a-table', 'empty',
            'only-separators', 'none'])
    def test_split_on_lines_commas_and_tabs(self, text, expected):
        assert pick.split_names(text) == expected


class TestAdd:
    def test_adds_in_order_and_saves(self, data_dir):
        assert pick.add_names('star', ['演员甲', '演员乙']) == AddResult(added=2, duplicate=0, rejected=0, overflow=0)
        pick.add_names('tag', ['标签一'])
        pick.add_names('star', ['演员丙'])
        assert pick.load_settings() == Settings(stars=('演员甲', '演员乙', '演员丙'), tags=('标签一',))
        assert stored(data_dir) == {'version': 1, 'stars': ['演员甲', '演员乙', '演员丙'], 'tags': ['标签一'],
                                    'mode': 'all', 'model_only': False}
        assert '演员甲' in settings_file(data_dir).read_text(encoding='utf-8')

    def test_a_name_already_listed_is_not_added_again(self):
        pick.add_names('tag', ['4K', '标签一'])
        result = pick.add_names('tag', [' 4k ', '标签一', '标签二', '标签二'])
        assert result == AddResult(added=1, duplicate=2, rejected=0, overflow=0)
        assert pick.load_settings().tags == ('4K', '标签一', '标签二')

    def test_the_other_spelling_of_a_listed_name_is_the_same_name(self):
        pick.add_names('tag', ['女大學生', '演員甲'])
        result = pick.add_names('tag', ['女大学生', '演员甲', '学生'])
        assert result == AddResult(added=1, duplicate=2, rejected=0, overflow=0)
        # 留着的是先加的那个写法；用另一种写法也去得掉
        assert pick.load_settings().tags == ('女大學生', '演員甲', '学生')
        assert pick.remove_name('tag', '女大学生') is True
        assert pick.load_settings().tags == ('演員甲', '学生')

    def test_the_same_name_can_be_a_star_and_a_tag(self):
        pick.add_names('star', ['演员甲'])
        assert pick.add_names('tag', ['演员甲']).added == 1
        assert pick.load_settings() == Settings(stars=('演员甲',), tags=('演员甲',))

    def test_names_longer_than_200_characters_are_rejected(self):
        assert pick.MAX_NAME_LENGTH == 200
        result = pick.add_names('star', ['甲' * 200, '乙' * 201])
        assert result == AddResult(added=1, duplicate=0, rejected=1, overflow=0)
        assert pick.load_settings().stars == ('甲' * 200,)

    def test_names_with_control_characters_are_rejected(self):
        result = pick.add_names('tag', ['标签\t一', '标签\x00二', '标签\x7f三', '标签四'])
        assert result == AddResult(added=1, duplicate=0, rejected=3, overflow=0)
        assert pick.load_settings().tags == ('标签四',)

    def test_at_most_100_names_of_each_kind(self):
        pick.add_names('star', [f'演员{n}' for n in range(98)])
        result = pick.add_names('star', ['演员0', '新演员1', '新演员2', '新演员3', '新演员4'])
        assert result == AddResult(added=2, duplicate=1, rejected=0, overflow=2)
        settings = pick.load_settings()
        assert len(settings.stars) == 100
        assert settings.stars[-2:] == ('新演员1', '新演员2')
        assert pick.add_names('tag', ['标签一']).added == 1

    @pytest.mark.parametrize('names', [[], ['', '   ', '\u3000']], ids=['nothing', 'only-blanks'])
    def test_nothing_to_add_does_not_create_the_file(self, data_dir, names):
        assert pick.add_names('star', names) == AddResult(added=0, duplicate=0, rejected=0, overflow=0)
        assert not settings_file(data_dir).exists()

    def test_unknown_kind_is_refused(self, data_dir):
        with pytest.raises(ValueError):
            pick.add_names('genre', ['标签一'])
        assert not settings_file(data_dir).exists()

    def test_adding_replaces_a_broken_file(self, data_dir):
        write_file(data_dir, b'{broken')
        assert pick.add_names('star', ['演员甲']).added == 1
        assert stored(data_dir) == {'version': 1, 'stars': ['演员甲'], 'tags': [], 'mode': 'all', 'model_only': False}
        assert pick.load_settings().problem == ''

    def test_no_temporary_file_is_left_behind(self, data_dir):
        pick.add_names('star', ['演员甲'])
        pick.add_names('tag', ['标签一'])
        assert sorted(path.name for path in data_dir.iterdir() if path.is_file()) == ['recommend.json']


class TestRemove:
    def test_removes_one_name_and_keeps_the_order(self, data_dir):
        pick.add_names('star', ['演员甲', 'Abc', '演员丙'])
        pick.add_names('tag', ['Abc'])
        assert pick.remove_name('star', ' aBC ') is True
        assert pick.load_settings() == Settings(stars=('演员甲', '演员丙'), tags=('Abc',))
        assert stored(data_dir)['stars'] == ['演员甲', '演员丙']

    @pytest.mark.parametrize('name', ['演员乙', '', '   ', None])
    def test_a_name_that_is_not_listed_changes_nothing(self, data_dir, name):
        pick.add_names('star', ['演员甲'])
        before = settings_file(data_dir).read_bytes()
        assert pick.remove_name('star', name) is False
        assert settings_file(data_dir).read_bytes() == before

    def test_removing_from_missing_settings_does_not_create_the_file(self, data_dir):
        assert pick.remove_name('tag', '标签一') is False
        assert not settings_file(data_dir).exists()

    def test_unknown_kind_is_refused(self):
        pick.add_names('star', ['演员甲'])
        with pytest.raises(ValueError):
            pick.remove_name('genre', '演员甲')
        assert pick.load_settings().stars == ('演员甲',)


class TestOptions:
    def test_saves_mode_and_model_only_and_keeps_the_names(self, data_dir):
        pick.add_names('star', ['演员甲'])
        pick.add_names('tag', ['标签一'])
        pick.save_options('any', True)
        assert pick.load_settings() == Settings(stars=('演员甲',), tags=('标签一',), mode='any', model_only=True)
        assert stored(data_dir) == {'version': 1, 'stars': ['演员甲'], 'tags': ['标签一'], 'mode': 'any',
                                    'model_only': True}
        pick.save_options('all', False)
        assert pick.load_settings() == Settings(stars=('演员甲',), tags=('标签一',))

    def test_options_can_be_saved_before_any_name(self, data_dir):
        pick.save_options('any', False)
        assert pick.load_settings() == Settings(mode='any')

    @pytest.mark.parametrize('mode', ['both', '', None, 'ALL', 1])
    def test_unknown_mode_is_refused(self, data_dir, mode):
        pick.save_options('any', True)
        with pytest.raises(ValueError):
            pick.save_options(mode, False)
        assert pick.load_settings() == Settings(mode='any', model_only=True)

    def test_model_only_is_stored_as_a_json_boolean(self, data_dir):
        pick.save_options('all', 1)
        assert stored(data_dir)['model_only'] is True
        pick.save_options('all', '')
        assert stored(data_dir)['model_only'] is False


class TestClear:
    def test_clear_goes_back_to_the_defaults(self, data_dir):
        pick.add_names('star', ['演员甲'])
        pick.add_names('tag', ['标签一'])
        pick.save_options('any', True)
        pick.clear_settings()
        assert pick.load_settings() == Settings()
        assert stored(data_dir) == {'version': 1, 'stars': [], 'tags': [], 'mode': 'all', 'model_only': False}

    def test_clear_replaces_a_broken_file(self, data_dir):
        write_file(data_dir, b'{broken')
        pick.clear_settings()
        assert pick.load_settings() == Settings()


class TestWriting:
    def test_failed_write_keeps_the_old_file_and_says_why(self, data_dir, monkeypatch):
        pick.add_names('star', ['演员甲'])
        before = settings_file(data_dir).read_bytes()

        def disk_full(source, target, swap=None):
            raise OSError(errno.ENOSPC, '磁盘空间不足')

        monkeypatch.setattr(util, 'replace_file', disk_full)
        for change in (lambda: pick.add_names('star', ['演员乙']), lambda: pick.remove_name('star', '演员甲'),
                       lambda: pick.save_options('any', True), pick.clear_settings):
            with pytest.raises(pick.SettingsError) as info:
                change()
            assert str(info.value) == f'推荐设定没有保存：无法写入 {settings_file(data_dir)}（磁盘空间不足）'
        assert settings_file(data_dir).read_bytes() == before
        assert sorted(path.name for path in data_dir.iterdir() if path.is_file()) == ['recommend.json']

    def test_data_folder_that_cannot_be_written_is_reported(self, data_dir, monkeypatch):
        def read_only(path, write):
            raise PermissionError(errno.EACCES, '拒绝访问')

        monkeypatch.setattr(util, 'write_temp_file', read_only)
        with pytest.raises(pick.SettingsError, match='拒绝访问'):
            pick.add_names('tag', ['标签一'])
        assert not settings_file(data_dir).exists()

    def test_the_reason_a_write_failed_is_given_in_chinese(self, data_dir, monkeypatch):
        def disk_full(path, write):
            raise OSError(errno.ENOSPC, 'No space left on device')

        monkeypatch.setattr(util, 'write_temp_file', disk_full)
        with pytest.raises(pick.SettingsError) as info:
            pick.add_names('tag', ['标签一'])
        assert str(info.value) == f'推荐设定没有保存：无法写入 {settings_file(data_dir)}（磁盘空间不足）'

    def test_write_waits_while_the_file_is_briefly_locked(self, data_dir, monkeypatch):
        real_replace = os.replace
        attempts = []

        def locked_twice(source, target):
            attempts.append(target)
            if len(attempts) <= 2:
                raise PermissionError(errno.EACCES, '另一个程序正在使用此文件')
            return real_replace(source, target)

        monkeypatch.setattr(util.os, 'replace', locked_twice)
        monkeypatch.setattr(util.time, 'sleep', lambda seconds: None)
        assert pick.add_names('star', ['演员甲']).added == 1
        assert len(attempts) == 3
        assert pick.load_settings().stars == ('演员甲',)

    def test_reading_does_not_wait_for_a_write_that_is_stuck(self, data_dir, monkeypatch):
        pick.add_names('star', ['演员甲'])
        waiting, release = threading.Event(), threading.Event()
        outcome = []

        def refused(source, target):
            # 目标文件只读、被别的程序占着时，替换一直被拒绝，要重试好几次才放弃
            raise PermissionError(errno.EACCES, '另一个程序正在使用此文件')

        def wait_before_trying_again(seconds):
            waiting.set()
            release.wait(timeout=10)

        def write():
            try:
                pick.add_names('star', ['演员乙'])
            except pick.SettingsError as ex:
                outcome.append(str(ex))

        monkeypatch.setattr(util.os, 'replace', refused)
        monkeypatch.setattr(util.time, 'sleep', wait_before_trying_again)
        writer = threading.Thread(target=write)
        writer.start()
        try:
            assert waiting.wait(timeout=10)
            started = time.monotonic()
            # 写的一方正等着再试：别的页面这时读设定不用等它，读到的是原来的那份完整的文件
            # （它要等到 release，最多 10 秒：不到一半就算没有等它，慢的机器上也不会误报）
            assert pick.load_settings() == Settings(stars=('演员甲',))
            assert time.monotonic() - started < 5
        finally:
            release.set()
            writer.join(timeout=10)
        assert outcome == [f'推荐设定没有保存：无法写入 {settings_file(data_dir)}（另一个程序正在使用此文件）']
        assert pick.load_settings() == Settings(stars=('演员甲',))

    def test_a_read_never_meets_the_file_while_it_is_being_replaced(self, data_dir, monkeypatch):
        # Windows 上正在被替换的那一瞬间去打开文件会被拒绝（拒绝访问），读的一方就会把好好的设定当成坏文件。
        # 所以读和"替换的那一下"不同时进行：替换开始以后才来读的，等它换完，读到新的那一份
        pick.add_names('star', ['演员甲'])
        replacing, finish = threading.Event(), threading.Event()
        real_replace = os.replace
        read = []

        def slow_replace(source, target):
            replacing.set()
            assert finish.wait(timeout=10)
            real_replace(source, target)

        monkeypatch.setattr(util.os, 'replace', slow_replace)
        writer = threading.Thread(target=pick.add_names, args=('star', ['演员乙']))
        reader = threading.Thread(target=lambda: read.append(pick.load_settings()))
        writer.start()
        try:
            assert replacing.wait(timeout=10)
            reader.start()
            reader.join(timeout=0.5)
            assert reader.is_alive() and read == []
        finally:
            finish.set()
            writer.join(timeout=10)
            reader.join(timeout=10)
        assert read == [Settings(stars=('演员甲', '演员乙'))]

    def test_simultaneous_saves_lose_nothing(self, data_dir):
        pick.add_names('star', ['原有演员'])
        workers, each, readers = 8, 6, 4
        start = threading.Barrier(workers + readers)
        errors = []

        def add(worker):
            try:
                start.wait(timeout=10)
                for n in range(each):
                    kind = 'star' if n % 2 == 0 else 'tag'
                    assert pick.add_names(kind, [f'名字{worker}-{n}']).added == 1
            except Exception as ex:
                errors.append(repr(ex))

        def read():
            try:
                start.wait(timeout=10)
                for _ in range(300):
                    settings = pick.load_settings()
                    assert settings.problem == '' and settings.stars[0] == '原有演员'
            except Exception as ex:
                errors.append(repr(ex))

        threads = [threading.Thread(target=add, args=(worker,)) for worker in range(workers)]
        threads.extend(threading.Thread(target=read) for _ in range(readers))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert errors == []
        settings = pick.load_settings()
        expected = {f'名字{worker}-{n}' for worker in range(workers) for n in range(each)}
        assert set(settings.stars) | set(settings.tags) == expected | {'原有演员'}
        assert len(settings.stars) == 1 + workers * each // 2
        assert stored(data_dir)['version'] == 1
        assert sorted(path.name for path in data_dir.iterdir() if path.is_file()) == ['recommend.json']
