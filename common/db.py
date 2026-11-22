"""直连数据库查数，用于「接口说完的事，库里是不是真的发生了」。

接口测试只断言响应体，遇到异步写、事务回滚、主从延迟照样会绿。
对账必须绕过接口，直接看表。连接方式由 app/dbi.py 决定（SQLite / MySQL），
本文件的 SQL 两种库通用。
"""

from __future__ import annotations

from contextlib import contextmanager

from app import dbi


class Db:
    def __init__(self, path: str | None):
        self.path = path

    @contextmanager
    def conn(self):
        c = dbi.connect(self.path)
        try:
            yield c
        finally:
            c.close()

    def stock(self, product_id: int) -> int:
        with self.conn() as c:
            row = c.execute(
                "SELECT stock FROM products WHERE id=?", (product_id,)
            ).fetchone()
        return -1 if row is None else row["stock"]

    def log_sum(self, product_id: int) -> int:
        """库存流水合计。期初记 +stock，之后每次变动都对冲，合计应等于当前库存。"""
        with self.conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(delta), 0) AS s FROM stock_log WHERE product_id=?",
                (product_id,),
            ).fetchone()
        return row["s"]

    def logs(self, product_id: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT delta, reason, order_id FROM stock_log"
                " WHERE product_id=? ORDER BY id",
                (product_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def order(self, oid: str) -> dict | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        return None if row is None else dict(row)

    def order_count(self, product_id: int) -> int:
        with self.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE product_id=?", (product_id,)
            ).fetchone()
        return row["n"]

    def purge_product(self, product_id: int) -> None:
        """清掉某个商品相关的全部数据，让用例可重复执行。"""
        with self.conn() as c:
            c.execute("DELETE FROM stock_log WHERE product_id=?", (product_id,))
            c.execute("DELETE FROM orders WHERE product_id=?", (product_id,))
            c.execute("DELETE FROM products WHERE id=?", (product_id,))
            c.commit()