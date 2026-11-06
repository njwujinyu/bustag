'''
命令行：download、recommend、check（网站请求走 FakeFetcher），以及 python -m bustag.main 能运行
'''
import os
import pickle
import re
import subprocess
import sys
from pathlib import Path

import pytest
import sklearn
import sklearn.base
from click.testing import CliRunner
from sklearn.neighbors import KNeighborsClassifier

from bustag import main, util
from bustag.model import classifier
from bustag.model.persist import ModelLoadError
from bustag.spider import bus_spider
from bustag.spider.bus_spider import DownloadResult
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate
from bustag.spider.fetcher import BlockedError, FetchError

ROOT = Path(__file__).resolve().parent.parent
OK, FAIL, WARN = main.OK, main.FAIL, main.WARN
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 4096
COVER = 'https://www.javbus.com/pics/cover/2qh5_b.jpg'
SCORES = {'precision': 0.8, 'recall': 0.7, 'f1': 0.75}


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


class TestRecommend:
    def test_without_model(self):
        result = invoke('recommend')
        assert result.exit_code == 0
        assert '还没有训练好的模型, 无法推荐' in result.output

    def test_with_model(self, monkeypatch):
        calls = []
        monkeypatch.setattr(classifier, 'recommend', lambda: calls.append('recommend') or (3, 1))
        assert invoke('recommend').exit_code == 0
        assert calls == ['recommend']


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
    return data_dir / classifier.MODEL_FILE


class TestCheck:
    def test_everything_fine(self, site, data_dir):
        result = invoke('check')
        lines = result.output.splitlines()
        assert result.exit_code == 0
        assert [line for line in lines if line.startswith('==')] == ['== 配置', '== 网站', '== 数据']
        assert f'{OK} 数据目录：{data_dir}' in lines
        assert f'{OK} root_path：https://www.javbus.com' in lines
        assert f'{OK} 代理：没有设置，也没有系统 / 环境变量代理（直连）' in lines
        assert any(re.fullmatch(rf'{OK} 连通：GET https://www\.javbus\.com/ → 200，用时 \d+\.\d\d 秒，没有被拦截', line)
                   for line in lines)
        assert f'{OK} 列表解析：首页找到 28 个影片链接（正常约 30 个）' in lines
        detail = next(line for line in lines if '详情解析' in line)
        assert detail.startswith(f'{OK} 详情解析：ABP-001 ')
        assert detail.endswith('，类别 3 个，演员 1 个，封面 /pics/cover/2qh5_b.jpg')
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

    def test_data_counts(self, site, make_item):
        make_item('ABC-001')
        make_item('ABC-002')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ABC-001')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-002')
        assert '：影片 2 部，打标 1 条' in invoke('check').output

    def test_usable_model(self, site, data_dir):
        model_path(data_dir).write_bytes(pickle.dumps(
            {'model': KNeighborsClassifier(), 'scores': SCORES, 'sklearn_version': sklearn.__version__}))
        assert f'{OK} 模型：可用（{model_path(data_dir)}）' in invoke('check').output

    def test_unreadable_model(self, site, data_dir):
        model_path(data_dir).write_bytes(b'garbage')
        result = invoke('check')
        assert (f'{WARN} 模型：{model_path(data_dir)} 读取失败（UnpicklingError），'
                '多半是旧版本 scikit-learn 训练的，版本不兼容') in result.output
        assert '   建议：在网页的"模型"页重新训练' in result.output
        assert result.exit_code == 0

    def test_model_from_other_sklearn_version(self, site, data_dir, monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(sklearn.base, '__version__', '0.21.3')
            content = pickle.dumps({'model': KNeighborsClassifier(), 'scores': SCORES, 'sklearn_version': '0.21.3'})
        model_path(data_dir).write_bytes(content)
        output = invoke('check').output
        assert f'{WARN} 模型：可以读取，但是用 scikit-learn 0.21.3 训练的（当前 {sklearn.__version__}）' in output
        assert '   建议：在网页的"模型"页重新训练' in output

    @pytest.mark.xfail(strict=True, reason='产品缺陷：check 对格式不对的模型文件报「可用」，网页和推荐却读不了')
    def test_model_with_wrong_format(self, site, data_dir):
        model_path(data_dir).write_bytes(pickle.dumps({'model': KNeighborsClassifier()}))
        with pytest.raises(ModelLoadError):
            classifier.load()
        assert f'{OK} 模型' not in invoke('check').output


def test_module_runs_as_script(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith('BUSTAG_')}
    env.update(BUSTAG_DATA_DIR=str(tmp_path / 'data'), PYTHONPATH=str(ROOT), PYTHONIOENCODING='utf-8')
    result = subprocess.run([sys.executable, '-m', 'bustag.main', '--help'], cwd=str(tmp_path), env=env,
                            capture_output=True, encoding='utf-8', errors='replace', timeout=120)
    assert result.returncode == 0, result.stderr
    for command in ('download', 'recommend', 'check'):
        assert re.search(rf'^\s+{command}\s', result.stdout, re.MULTILINE)
