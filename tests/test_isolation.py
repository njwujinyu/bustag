'''
测试环境本身：数据目录在临时位置、默认不能联网、每个测试从空表开始
'''
import os
import socket
from pathlib import Path

import pytest
import requests

from bustag import util
from bustag.spider import db
from bustag.spider.fetcher import Fetcher

REPO = Path(__file__).resolve().parent.parent


def test_data_lives_outside_the_repository(data_dir):
    session_dir = Path(os.environ['BUSTAG_DATA_DIR']).resolve()
    assert REPO not in session_dir.parents
    assert Path(db.db.database).resolve().parent == session_dir
    assert Path(util.get_data_dir()) == data_dir
    assert (data_dir / 'model').is_dir()


def test_tables_start_empty(make_item):
    assert [model.select().count() for model in (db.Item, db.Tag, db.ItemTag, db.ItemRate, db.LocalItem)] == [0] * 5
    make_item('ABC-001', tags=[('genre', 'g1')])
    assert db.Item.select().count() == 1


def test_socket_access_is_blocked(no_network):
    with pytest.raises(RuntimeError):
        socket.getaddrinfo('www.javbus.com', 443)
    with pytest.raises(RuntimeError):
        socket.create_connection(('127.0.0.1', 9))
    assert len(no_network) == 2
    no_network.clear()


def test_requests_and_fetcher_cannot_reach_the_network(no_network, monkeypatch):
    monkeypatch.setattr('bustag.spider.fetcher.time.sleep', lambda seconds: None)
    with pytest.raises(RuntimeError):
        requests.get('https://www.javbus.com/', timeout=1)
    fetcher = Fetcher('https://www.javbus.com', delay=0, retries=3)
    with pytest.raises(RuntimeError):
        fetcher.get('/')
    # Fetcher 不会把拦截当成网络错误去重试
    assert len(no_network) == 2
    no_network.clear()


def test_proxy_environment_is_ignored():
    assert os.environ.get('NO_PROXY') == '*'
    assert requests.utils.get_environ_proxies('https://www.javbus.com/') == {}
