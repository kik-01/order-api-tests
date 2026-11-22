"""公共夹具：起一个真实的 HTTP 服务，跑完关掉。

被测服务用 werkzeug 起在子线程里，测试结束 shutdown。
不用 Flask 自带 app.run()：它没有优雅退出的入口，跑完 pytest 会挂住。

数据库默认 SQLite（临时文件）；设 SHOP_DB=mysql 时改用 MySQL 测试库，
用例与对账 SQL 不变，验证的是 InnoDB 行锁下的同一套业务规则。
"""

from __future__ import annotations

import logging
import pathlib
import socket
import sys
import threading
import time
from contextlib import contextmanager

import pytest
import requests
from werkzeug.serving import make_server

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import dbi  # noqa: E402
from app.server import create_app  # noqa: E402
from common.client import ApiClient  # noqa: E402
from common.db import Db  # noqa: E402
from common.factory import Factory  # noqa: E402
from common.logger import setup as setup_logging  # noqa: E402

setup_logging()
log = logging.getLogger("api")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextmanager
def _running_server(tmp_path: pathlib.Path, auto_cancel_seconds):
    db_path = None if dbi.MODE == "mysql" else str(tmp_path / "shop.db")
    app = create_app(db_path, auto_cancel_seconds=auto_cancel_seconds)
    port = _free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    # 等端口真正可用，避免首批请求 ConnectionError
    for _ in range(50):
        try:
            requests.get(f"{base_url}/api/orders/not-exist", timeout=1)
            break
        except requests.ConnectionError:
            time.sleep(0.05)
    else:
        raise RuntimeError("被测服务启动失败")

    try:
        yield base_url, db_path
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope="session", autouse=True)
def _clean_mysql():
    """MySQL 测试库是常驻的：开跑前清空三张表，避免上次残留订单污染计数断言。"""
    if dbi.MODE == "mysql":
        c = dbi.connect()
        try:
            for table in ("stock_log", "orders", "products"):
                c.execute(f"DELETE FROM {table}")
            c.commit()
        finally:
            c.close()
    yield


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    with _running_server(tmp_path_factory.mktemp("db"), auto_cancel_seconds=1800) as s:
        yield s


@pytest.fixture(scope="session")
def expiring_server(tmp_path_factory):
    """待支付订单立刻过期的服务，专供超时取消用例。"""
    with _running_server(tmp_path_factory.mktemp("db2"), auto_cancel_seconds=0) as s:
        yield s


def _make_env(server):
    base_url, db_path = server
    client = ApiClient(base_url)
    db = Db(db_path)
    factory = Factory(client, db)
    yield client, db, factory
    factory.cleanup()


@pytest.fixture
def env(server):
    yield from _make_env(server)


@pytest.fixture
def expiring_env(expiring_server):
    yield from _make_env(expiring_server)


@pytest.fixture
def client(env):
    return env[0]


@pytest.fixture
def db(env):
    return env[1]


@pytest.fixture
def factory(env):
    return env[2]


# ---------- UI 层夹具（pytest -m ui 才会用到） ----------

@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()