'''
命令行：download、recommend、check（网站请求走 FakeFetcher），以及 python -m bustag.main 能运行
'''
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from peewee import OperationalError

from bustag import main, util
from bustag.model import classifier
from bustag.model.persist import LEGACY_MODEL, MODEL_FILE, ModelLoadError
from bustag.spider import bus_spider, db
from bustag.spider.bus_spider import DownloadResult
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate
from bustag.spider.fetcher import BlockedError, FetchError

ROOT = Path(__file__).resolve().parent.parent
OK, FAIL, WARN = main.OK, main.FAIL, main.WARN
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 4096
COVER = 'https://www.javbus.com/pics/cover/2qh5_b.jpg'
BUSY_LINE = '数据库正被别的程序占用（等了好一会儿还没轮到），这次没有做成，请稍后再试\n'
# 模型文件的几种坏法：读不出来、是 JSON 但内容不对
BROKEN_MODELS = [b'garbage', b'', json.dumps({'version': 1}).encode(), json.dumps([1, 2, 3]).encode()]


def invoke(*args):
    '''运行命令行；命令里出现 SystemExit 以外的异常时直接抛出，不让它变成退出码 1'''
    result = CliRunner().invoke(main.main, list(args))
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


class TestDownload:
    @pytest.fixture
    def download(self, monkeypatch):
        calls = []

        def fake(result):
            def download(count=None):
                calls.append(count)
                return result
            monkeypatch.setattr(bus_spider, 'download', download)
            return calls
        return fake

    @pytest.mark.parametrize('result, code', [
        (DownloadResult(added=3), 0),
        (DownloadResult(blocked='触发了网站的人机验证'), 1),
        (DownloadResult(error='列表页读取失败'), 1),
    ])
    def test_exit_code(self, download, result, code):
        calls = download(result)
        assert invoke('download').exit_code == code
        assert calls == [None]

    def test_count_option(self, download):
        calls = download(DownloadResult())
        assert invoke('download', '--count', '5').exit_code == 0
        assert calls == [5]

    @pytest.mark.parametrize('value', ['0', '-1', 'abc'])
    def test_invalid_count(self, download, value):
        calls = download(DownloadResult())
        result = invoke('download', '--count', value)
        assert result.exit_code == 2
        assert '--count' in result.output
        assert calls == []


def old_model_files(data_dir):
    '''旧版本程序留下的两个模型文件'''
    for name in ('model.pkl', 'label_binarizer.pkl'):
        (data_dir / 'model' / name).write_bytes(b'left by an older version')


class TestRecommend:
    def test_without_model(self):
        result = invoke('recommend')
        assert result.exit_code == 1
        assert '还没有训练好的模型, 无法推荐' in result.output

    def test_says_how_many_movies_were_scored_and_recommended(self, write_model, make_item):
        with db.db.atomic():
            for n in range(25):
                make_item(f'ABC-{n:03d}', tags=[('genre', 'g1')] if n < 4 else [('star', 's1')])
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, 'ABC-024')
        write_model()
        result = invoke('recommend')
        assert result.exit_code == 0
        assert result.output == '已给 24 部没有打标的影片打分：推荐 20 部，不太像 4 部\n'
        rates = {rate.item_id: (rate.rate_type, rate.rate_value) for rate in ItemRate.select()}
        assert len(rates) == 25
        assert all(rates[f'ABC-{n:03d}'] == (RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE) for n in range(4))
        assert rates['ABC-024'] == (RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE)

    def test_nothing_to_score(self, write_model):
        write_model()
        result = invoke('recommend')
        assert result.exit_code == 0
        assert result.output == '没有需要打分的影片\n'

    @pytest.mark.parametrize('content, reason', [
        (b'garbage', '模型文件 model.json 无法读取'),
        (json.dumps({'version': 1}).encode(), '模型文件 model.json 格式不对'),
    ])
    def test_unusable_model_file(self, data_dir, make_item, content, reason):
        make_item('ABC-001', tags=[('genre', 'g1')])
        (data_dir / MODEL_FILE).write_bytes(content)
        result = invoke('recommend')
        assert result.exit_code == 1
        assert f'无法推荐：{reason}' in result.output
        assert '重新训练' in result.output
        assert ItemRate.get_by_fanhao('ABC-001') is None

    def test_model_files_of_an_older_version(self, data_dir, make_item):
        make_item('ABC-001', tags=[('genre', 'g1')])
        old_model_files(data_dir)
        result = invoke('recommend')
        assert result.exit_code == 1
        assert result.output == f'无法推荐：{LEGACY_MODEL}\n'
        assert ItemRate.get_by_fanhao('ABC-001') is None

    def test_a_database_held_by_another_program_is_one_line_not_a_traceback(self, write_model, make_item,
                                                                            locked_database):
        make_item('ABC-001', tags=[('genre', 'g1')])
        write_model()
        with locked_database():
            result = invoke('recommend')
        assert result.exit_code == 1
        assert result.output == BUSY_LINE
        assert ItemRate.get_by_fanhao('ABC-001') is None
        # 锁放开以后照常
        assert invoke('recommend').output == '已给 1 部没有打标的影片打分：推荐 1 部，不太像 0 部\n'


class TestBusyDatabase:
    def test_any_command_that_meets_a_busy_database_ends_with_one_line(self, monkeypatch):
        def busy(count=None):
            raise OperationalError('database is locked')

        monkeypatch.setattr(bus_spider, 'download', busy)
        result = invoke('download')
        assert result.exit_code == 1
        assert result.output == BUSY_LINE

    def test_a_database_held_exclusively_says_so(self, monkeypatch):
        # 从没换成 WAL 的老库被别的程序独占着：没有等，不能说"等了好一会儿"
        def held(count=None):
            raise db.LockedOut('database is locked (held exclusively by another program)')

        monkeypatch.setattr(bus_spider, 'download', held)
        result = invoke('download')
        assert result.exit_code == 1
        assert result.output == f'{db.DATABASE_LOCKED_OUT}，这次没有做成，请稍后再试\n'

    def test_other_database_errors_are_not_hidden(self, monkeypatch):
        def broken(count=None):
            raise OperationalError('no such table: item')

        monkeypatch.setattr(bus_spider, 'download', broken)
        with pytest.raises(OperationalError, match='no such table'):
            invoke('download')

    def test_tables_not_added_at_startup_are_added_before_the_command_runs(self, monkeypatch):
        # 打开库时别的程序占着写锁、新加的表没建成：命令开始前再建一次（这次等满 30 秒）
        attempts, calls = [], []
        monkeypatch.setattr(db, 'tables_missing', True)
        monkeypatch.setattr(db, 'create_tables', lambda: attempts.append('try') or True)
        monkeypatch.setattr(bus_spider, 'download', lambda count=None: calls.append(count) or DownloadResult())
        assert invoke('download').exit_code == 0
        assert attempts == ['try'] and calls == [None]

    def test_tables_that_still_cannot_be_added_end_the_command_with_one_line(self, monkeypatch):
        calls = []
        monkeypatch.setattr(db, 'tables_missing', True)
        monkeypatch.setattr(db, 'create_tables', lambda: False)
        monkeypatch.setattr(bus_spider, 'download', lambda count=None: calls.append(count) or DownloadResult())
        result = invoke('download')
        assert result.exit_code == 1
        assert result.output == f'{db.TABLES_MISSING}，这次没有做成，请关掉正开着 bus.db 的程序后再试\n'
        assert calls == []


@pytest.fixture
def site(monkeypatch, fake_fetcher, page):
    '''
    main.create_fetcher 换成 FakeFetcher：首页、首页第一部影片的详情页、封面都正常。
    详情页用 ABP-001 的样本（check 只看能不能解析，不核对番号）
    '''
    fetcher = fake_fetcher({
        '/': page('list-home.html'),
        '/IENF-465': page('detail-ABP-001-series-no-director.html'),
        COVER: (JPEG, 'image/jpeg'),
    })
    fetcher.created = []

    def create_fetcher(**overrides):
        fetcher.created.append(overrides)
        return fetcher

    monkeypatch.setattr(main, 'create_fetcher', create_fetcher)
    return fetcher


def model_path(data_dir):
    return data_dir / MODEL_FILE


class TestCheck:
    def test_everything_fine(self, site, data_dir):
        result = invoke('check')
        lines = result.output.splitlines()
        assert result.exit_code == 0
        assert [line for line in lines if line.startswith('==')] == ['== 配置', '== 网站', '== 数据']
        assert f'{OK} 数据目录：{data_dir}' in lines
        assert (f'{OK} 运行日志：{data_dir / "logs" / "bustag.log"}'
                '（网页服务运行时写在这里，命令行的输出只显示在控制台）') in lines
        # 命令行可以和网页服务同时运行：不写日志文件，也不给数据目录上锁
        assert not (data_dir / 'logs').exists()
        assert not (data_dir / 'bustag.lock').exists()
        assert f'{OK} root_path：https://www.javbus.com' in lines
        assert f'{OK} 代理：没有设置，也没有系统 / 环境变量代理（直连）' in lines
        assert any(re.fullmatch(rf'{OK} 连通：GET https://www\.javbus\.com/ → 200，用时 \d+\.\d\d 秒，没有被拦截', line)
                   for line in lines)
        assert f'{OK} 列表解析：首页找到 28 个影片链接（正常约 30 个）' in lines
        # 只写番号和数量：标题、类别和演员的名字不在输出里（用户常把这段输出贴出来求助）
        detail = rf'{OK} 详情解析：ABP-001，标题 \d+ 个字，类别 3 个，演员 1 个，封面 /pics/cover/2qh5_b\.jpg'
        assert sum(bool(re.fullmatch(detail, line)) for line in lines) == 1
        assert sum('详情解析' in line for line in lines) == 1
        assert f'{OK} 封面：{COVER} → image/jpeg，4.0 KB' in lines
        assert any(line.startswith(f'{OK} 数据库：') and line.endswith('：影片 0 部，打标 0 条') for line in lines)
        assert f'{WARN} 模型：还没有训练模型' in lines
        assert '   建议：打标 200 条以上后，在网页的"模型"页训练' in lines
        assert lines[-1] == '检查完成，关键项全部正常'
        assert site.requests == ['/', '/IENF-465', COVER]
        assert site.created == [{'retries': 1}]
        assert site.closed

    @pytest.mark.parametrize('route, reply, line', [
        ('/', BlockedError('触发了网站的人机验证'), f'{FAIL} 连通：GET https://www.javbus.com/ 被拦截：触发了网站的人机验证'),
        ('/', FetchError('连接超时'), f'{FAIL} 连通：GET https://www.javbus.com/ 失败：连接超时'),
        ('/IENF-465', None, f'{FAIL} 详情解析：/IENF-465：页面不存在（HTTP 404）：https://www.javbus.com/IENF-465'),
        (COVER, (b'<html></html>', 'text/html'), f'{FAIL} 封面：{COVER}：不是图片（content-type: text/html）'),
        (COVER, FetchError('页面不存在（HTTP 404）'), f'{FAIL} 封面：{COVER}：页面不存在（HTTP 404）'),
    ])
    def test_site_problems(self, site, route, reply, line):
        site.routes[route] = reply
        result = invoke('check')
        assert line in result.output.splitlines()
        assert result.exit_code == 1
        assert result.output.splitlines()[-1] == '有检查项失败，请按上面的建议处理'
        assert site.closed

    def test_blocked_skips_remaining_site_checks(self, site):
        site.routes['/'] = BlockedError('触发了网站的人机验证')
        output = invoke('check').output
        assert '   后面的联网检查已跳过' in output
        assert '列表解析' not in output
        assert site.requests == ['/']
        assert '== 数据' in output

    def test_home_page_without_movies(self, site, page):
        site.routes['/'] = page('driver-verify.html')
        result = invoke('check')
        assert f'{FAIL} 列表解析：首页没有找到影片链接' in result.output
        assert '   建议：页面结构可能变了，请到项目 Issues 反馈' in result.output
        assert result.exit_code == 1

    def test_dead_domain(self, site, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'download.root_path', 'https://www.busdmm.work')
        result = invoke('check')
        assert f'{FAIL} root_path：https://www.busdmm.work 是已经失效的旧域名' in result.output
        assert f'   建议：打开 {util.get_config_path()}，把 root_path 改成 https://www.javbus.com' in result.output
        assert result.exit_code == 1

    def test_configured_proxy(self, site, monkeypatch):
        monkeypatch.setitem(util.APP_CONFIG, 'options.proxy', ' http://127.0.0.1:7890 ')
        assert f'{OK} 代理：http://127.0.0.1:7890（config.ini 里设置）' in invoke('check').output

    def test_environment_proxy(self, site, monkeypatch):
        monkeypatch.delenv('NO_PROXY')
        monkeypatch.setenv('HTTPS_PROXY', 'http://10.0.0.1:3128')
        assert f'{OK} 代理：没有设置，使用系统 / 环境变量里的代理 http://10.0.0.1:3128' in invoke('check').output

    @pytest.mark.parametrize('proxy, shown, secret', [
        ('http://alice:s3cret@127.0.0.1:7890', 'http://alice:***@127.0.0.1:7890', 's3cret'),
        ('http://ghp_T0KEN@127.0.0.1:7890', 'http://***@127.0.0.1:7890', 'ghp_T0KEN'),
        ('http://127.0.0.1:7890', 'http://127.0.0.1:7890', None),
    ], ids=['user-and-password', 'user-only', 'no-credentials'])
    def test_proxy_credentials_are_hidden(self, site, monkeypatch, proxy, shown, secret):
        monkeypatch.setitem(util.APP_CONFIG, 'options.proxy', proxy)
        output = invoke('check').output
        assert f'{OK} 代理：{shown}（config.ini 里设置）' in output
        if secret:
            assert secret not in output

    @pytest.mark.parametrize('proxy, shown, secret', [
        ('http://bob:pa55@10.0.0.1:3128', 'http://bob:***@10.0.0.1:3128', 'pa55'),
        ('http://ghp_T0KEN@10.0.0.1:3128', 'http://***@10.0.0.1:3128', 'ghp_T0KEN'),
        ('http://10.0.0.1:3128', 'http://10.0.0.1:3128', None),
    ], ids=['user-and-password', 'user-only', 'no-credentials'])
    def test_environment_proxy_credentials_are_hidden(self, site, monkeypatch, proxy, shown, secret):
        monkeypatch.delenv('NO_PROXY')
        monkeypatch.setenv('HTTPS_PROXY', proxy)
        output = invoke('check').output
        assert f'{OK} 代理：没有设置，使用系统 / 环境变量里的代理 {shown}' in output
        if secret:
            assert secret not in output

    def test_data_counts(self, site, make_item):
        make_item('ABC-001')
        make_item('ABC-002')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-002')
        assert '：影片 2 部，打标 1 条' in invoke('check').output

    def test_usable_model(self, site, write_model):
        path = write_model()
        assert f'{OK} 模型：可用（{path}）' in invoke('check').output.splitlines()

    @pytest.mark.parametrize('content', BROKEN_MODELS)
    def test_unusable_model_file_gives_the_same_reason_as_the_web_page_and_recommend(self, site, data_dir, content):
        '''⚠ 后面的文字就是网页"模型"页（classifier.load）和 recommend 报的原因'''
        model_path(data_dir).write_bytes(content)
        with pytest.raises(ModelLoadError) as info:
            classifier.load()
        result = invoke('check')
        assert f'{WARN} 模型：{info.value}' in result.output.splitlines()
        assert f'{OK} 模型' not in result.output
        assert result.exit_code == 0
        assert invoke('recommend').output == f'无法推荐：{info.value}\n'

    def test_model_files_of_an_older_version_need_retraining(self, site, data_dir):
        old_model_files(data_dir)
        result = invoke('check')
        assert f'{WARN} 模型：{LEGACY_MODEL}' in result.output.splitlines()
        assert f'{OK} 模型' not in result.output
        assert result.exit_code == 0


def test_module_runs_as_script(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith('BUSTAG_')}
    env.update(BUSTAG_DATA_DIR=str(tmp_path / 'data'), PYTHONPATH=str(ROOT), PYTHONIOENCODING='utf-8')
    result = subprocess.run([sys.executable, '-m', 'bustag.main', '--help'], cwd=str(tmp_path), env=env,
                            capture_output=True, encoding='utf-8', errors='replace', timeout=120)
    assert result.returncode == 0, result.stderr
    for command in ('download', 'recommend', 'check'):
        assert re.search(rf'^\s+{command}\s', result.stdout, re.MULTILINE)
