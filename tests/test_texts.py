'''
面向用户的文字：bustag 自己写的日志（含 DEBUG）都是简体中文，命令行各命令和选项的帮助文字是中文。
click、APScheduler 等第三方库自带的文字（Usage:、Error: 等）不在此列
'''
import ast
import logging
import re
from pathlib import Path

import click

from bustag import main
from bustag.model import classifier
from bustag.spider import bus_spider, db

PACKAGE = Path(__file__).resolve().parent.parent / 'bustag'
CHINESE = re.compile(r'[\u4e00-\u9fff]')
LEVELS = {'debug', 'info', 'warning', 'error', 'exception', 'critical'}
# 消息不是在调用处写出来的日志：文字由别处生成，都是中文（各自的测试核对了文字）
INDIRECT_MESSAGES = {
    ('util.py', 'logger.warning(warning)'),  # dead_domain_warning() 的提示
    ('util.py', 'logger.error(message)'),  # 启动出错时调用方拼好的一行错误
    ('app/index.py', 'logger.error(ex)'),  # StartupError 的消息
    ('spider/bus_spider.py', 'logger.info(str(result))'),  # DownloadResult 的一行总结
    ('spider/bus_spider.py', 'logger.warning(str(result))'),
    ('spider/bus_spider.py', 'logger.warning(result.stop_reason)'),  # 停止抓取的原因
}


def literal_text(node):
    '''日志消息里写在代码里的文字：字符串、f-string 的固定部分，以及用 + 拼起来的这些'''
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(value.value for value in node.values if isinstance(value, ast.Constant))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return literal_text(node.left) + literal_text(node.right)
    return ''


def logger_calls():
    '''bustag/ 里所有 logger.xxx(...) 调用：(相对路径, 行号, 调用的源码, 消息里写出来的文字)'''
    calls = []
    for path in sorted(PACKAGE.rglob('*.py')):
        source = path.read_text(encoding='utf-8')
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in LEVELS and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == 'logger'):
                text = literal_text(node.args[0]) if node.args else ''
                calls.append((path.relative_to(PACKAGE).as_posix(), node.lineno,
                              ast.get_source_segment(source, node), text))
    return calls


class TestLogMessages:
    def test_scan_sees_the_calls(self):
        calls = {(path, code) for path, _, code, _ in logger_calls()}
        assert ('model/classifier.py', "logger.info('模型训练完成')") in calls
        assert ('spider/db.py', "logger.debug(f'影片已在库里：{item_fanhao}')") in calls

    def test_every_message_is_chinese(self):
        problems = []
        for path, line, code, text in logger_calls():
            if text and not CHINESE.search(text):
                problems.append(f'{path}:{line} {code}')
            elif not text and (path, code) not in INDIRECT_MESSAGES:
                problems.append(f'{path}:{line} {code}（消息不是写在调用处的文字：确认是中文后加进 INDIRECT_MESSAGES）')
        assert problems == []

    def test_indirect_messages_list_is_current(self):
        assert {(path, code) for path, _, code, text in logger_calls() if not text} == INDIRECT_MESSAGES

    def test_crawling_training_and_recommending_log_in_chinese(self, fake_fetcher, page, make_item, caplog):
        caplog.set_level(logging.DEBUG, logger='bustag')
        detail = page('detail-ABP-001-series-no-director.html')
        boxes = ''.join(f'<a class="movie-box" href="https://www.javbus.com/NEW-{n:03d}"></a>' for n in (1, 2))
        fetcher = fake_fetcher({'/': f'<div id="waterfall">{boxes}</div>',
                                '/NEW-001': detail.replace('ABP-001', 'NEW-001'),
                                '/NEW-002': detail.replace('ABP-001', 'NEW-002')})
        assert bus_spider.download(count=10, max_page=1, fetcher=fetcher).added == 2
        with db.db.atomic():
            for n in range(240):
                like = n % 2 == 0
                tags = [('genre', '喜欢的类别' if like else f'类别{n % 7}'), ('star', f'演员{n % 11}')]
                make_item(f'RAT-{n:03d}', tags=tags)
                db.ItemRate.saveit(db.RATE_TYPE.USER_RATE, int(like), f'RAT-{n:03d}')
        classifier.train()
        assert classifier.recommend()[0] == 2
        messages = [record.getMessage() for record in caplog.records if record.name == 'bustag']
        assert any(m.startswith('准确率（precision）') for m in messages)
        assert any(m.startswith('推荐完成：') for m in messages)
        assert [m for m in messages if not CHINESE.search(m)] == []


def test_command_line_help_is_chinese():
    assert CHINESE.search(main.main.help)
    for name, command in main.main.commands.items():
        assert CHINESE.search(command.help or ''), name
        for param in command.params:
            if isinstance(param, click.Option):
                assert CHINESE.search(param.help or ''), f'{name} {param.opts}'
