"""数据库连接层：SQLite / MySQL 双后端。

默认 SQLite：免安装免密码，clone 下来 pip install 就能跑，CI 零依赖。
设环境变量 SHOP_DB=mysql 切到 MySQL：验证 InnoDB 行锁下的并发与幂等。

SQL 主体两种库通用，占位符 ? 由本层翻译成 MySQL 的 %s。
真正的差异只有四处，也是面试要讲清的四处：
1. 建表 DDL（AUTOINCREMENT / TEXT / REAL  vs  AUTO_INCREMENT / VARCHAR / DOUBLE）；
2. 事务开启（SQLite 的 BEGIN IMMEDIATE 拿整库写锁  vs  MySQL 的 START TRANSACTION 只锁碰到的行）；
3. 状态机读（SQLite 普通 SELECT 就够  vs  MySQL 必须 FOR UPDATE 锁定读，否则快照读放过非法流转）；
4. 锁错误判定（SQLite 的 "database is locked"  vs  MySQL 的 1205 锁等待超时 / 1213 死锁）。
"""

from __future__ import annotations

import os
import sqlite3

try:
    import pymysql
    import pymysql.cursors
except ImportError:  # 只跑 SQLite 时允许不装 pymysql
    pymysql = None

MODE = os.environ.get("SHOP_DB", "sqlite").lower()

# 两种库的错误基类合在一起，上层 except DBError 不用分后端
DBError = (sqlite3.Error,) + ((pymysql.Error,) if pymysql else ())


def mysql_config() -> dict:
    return dict(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "shop_test"),
        charset="utf8mb4",
    )


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    price_cents INTEGER NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    product_id      INTEGER NOT NULL,
    qty             INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL,
    status          TEXT    NOT NULL,
    idempotency_key TEXT    UNIQUE,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    delta      INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    order_id   TEXT,
    created_at REAL    NOT NULL
);
"""

MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id          INT PRIMARY KEY AUTO_INCREMENT,
    name        VARCHAR(255) NOT NULL,
    price_cents INT NOT NULL,
    stock       INT NOT NULL DEFAULT 0
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS orders (
    id              VARCHAR(32) PRIMARY KEY,
    product_id      INT NOT NULL,
    qty             INT NOT NULL,
    amount_cents    INT NOT NULL,
    status          VARCHAR(16) NOT NULL,
    idempotency_key VARCHAR(64) NULL UNIQUE,
    created_at      DOUBLE NOT NULL,
    updated_at      DOUBLE NOT NULL
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS stock_log (
    id         INT PRIMARY KEY AUTO_INCREMENT,
    product_id INT NOT NULL,
    delta      INT NOT NULL,
    reason     VARCHAR(32) NOT NULL,
    order_id   VARCHAR(32),
    created_at DOUBLE NOT NULL
) ENGINE=InnoDB;
"""


def is_lock_error(exc: Exception) -> bool:
    """只把「锁竞争」判为可重试；语法错等真 bug 直接抛，不被重试藏起来。"""
    if isinstance(exc, sqlite3.OperationalError):
        return "locked" in str(exc)
    if pymysql and isinstance(exc, pymysql.err.OperationalError):
        return exc.args[0] in (1205, 1213)  # 锁等待超时 / 死锁
    return False


def is_unique_violation(exc: Exception) -> bool:
    """MySQL 幂等兜底：并发同键插入撞唯一约束（1062）。SQLite 靠库锁不会走到这。"""
    return bool(pymysql) and isinstance(exc, pymysql.err.IntegrityError) and exc.args[0] == 1062


class Connection:
    """统一封装：execute 返回的游标在两种库下都有 fetchone/fetchall/rowcount/lastrowid，
    行统一可按列名取值（sqlite3.Row / DictCursor）。"""

    def __init__(self, raw, mode: str):
        self.raw = raw
        self.mode = mode

    def execute(self, sql: str, params: tuple = ()):
        if self.mode == "mysql":
            cur = self.raw.cursor(pymysql.cursors.DictCursor)
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self.raw.execute(sql, params)

    def begin(self):
        # SQLite：BEGIN IMMEDIATE 立刻拿整库写锁，check-then-insert 无窗口期
        # MySQL：START TRANSACTION，行锁在碰到行时才加，幂等要靠唯一约束兜底
        self.execute("BEGIN IMMEDIATE" if self.mode == "sqlite" else "START TRANSACTION")

    def locking_read(self, sql: str, params: tuple = ()):
        """状态机「先查状态再流转」必须用锁定读。

        SQLite 的 BEGIN IMMEDIATE 已拿整库写锁，普通 SELECT 就够；
        MySQL 默认 REPEATABLE READ 的普通 SELECT 是快照读：并发下另一个事务
        已提交的状态变更看不见，check-then-act 会放过非法流转（支付/取消都成功）。
        加 FOR UPDATE 读最新已提交版本并锁住该行，把窗口期堵上。
        """
        if self.mode == "mysql":
            return self.execute(sql + " FOR UPDATE", params)
        return self.execute(sql, params)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def connect(db_path: str | None = None) -> Connection:
    if MODE == "mysql":
        if pymysql is None:
            raise RuntimeError("SHOP_DB=mysql 需要先 pip install pymysql")
        cfg = mysql_config()
        raw = pymysql.connect(autocommit=True, connect_timeout=5, **cfg)
        return Connection(raw, "mysql")
    raw = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA busy_timeout=15000")
    raw.execute("PRAGMA foreign_keys=ON")
    return Connection(raw, "sqlite")


def init_schema(db_path: str | None = None) -> None:
    if MODE == "mysql":
        if pymysql is None:
            raise RuntimeError("SHOP_DB=mysql 需要先 pip install pymysql")
        cfg = mysql_config()
        db_name = cfg.pop("database")
        admin = pymysql.connect(autocommit=True, connect_timeout=5, **cfg)
        try:
            with admin.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` DEFAULT CHARSET utf8mb4")
        finally:
            admin.close()
        conn = connect()
        try:
            for stmt in MYSQL_SCHEMA.strip().split(";"):
                if stmt.strip():
                    conn.execute(stmt)
        finally:
            conn.close()
        return
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(SQLITE_SCHEMA)
        raw.execute("PRAGMA journal_mode=WAL")
    finally:
        raw.close()