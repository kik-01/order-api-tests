"""陶瓷商城订单服务（本地模拟后端）。

为什么要有这个服务：jsonplaceholder 那类公开 API 不持久化、不校验参数，
拿它做被测对象，等于在测一个永远不会出问题的东西。
这个服务有状态、有事务、有并发，真实系统会出问题的地方，这里也会出。

业务规则由项目内需求说明定义，测试用例从规则推导。
数据库默认 SQLite（免安装可复现），设 SHOP_DB=mysql 切 MySQL 验证行锁并发，见 app/dbi.py。

启动：python -m app.server          # 默认 127.0.0.1:8000，浏览器打开 / 有最小下单页
"""

from __future__ import annotations

import os
import time
import uuid

from flask import Flask, jsonify, render_template, request

from app import dbi

# 状态机：当前状态 -> 允许流转到的状态
# pending  -> paid | cancelled
# paid     -> shipped | refunded
# shipped  -> completed
# 终态：completed / cancelled / refunded
STATE_MACHINE = {
    "pending": {"paid", "cancelled"},
    "paid": {"shipped", "refunded"},
    "shipped": {"completed"},
    "cancelled": set(),
    "completed": set(),
    "refunded": set(),
}


def create_app(db_path: str | None, auto_cancel_seconds: float = 1800) -> Flask:
    """建应用。SQLite 时 db_path 由调用方给（测试用临时文件）；MySQL 时忽略 db_path。"""
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["AUTO_CANCEL_SECONDS"] = auto_cancel_seconds

    dbi.init_schema(db_path)

    # ---------- 基础工具 ----------

    def get_conn() -> dbi.Connection:
        # 每个请求独立连接：SQLite 连接不能跨线程复用，MySQL 连接 likewise 不共享
        return dbi.connect(app.config["DB_PATH"])

    def err(msg: str, code: int, **extra):
        body = {"code": code, "message": msg}
        body.update(extra)
        return jsonify(body), code

    def order_to_dict(row) -> dict:
        return {
            "id": row["id"],
            "product_id": row["product_id"],
            "qty": row["qty"],
            "amount_cents": row["amount_cents"],
            "status": row["status"],
            "created_at": row["created_at"],
        }

    # ---------- 页面（UI 层测试的被测对象） ----------

    @app.get("/")
    def index():
        return render_template("index.html")

    # ---------- 商品 ----------

    @app.post("/api/products")
    def create_product():
        body = request.get_json(silent=True) or {}
        name = body.get("name")
        price = body.get("price_cents")
        stock = body.get("stock", 0)

        if not name or not isinstance(price, int) or price <= 0:
            return err("商品名必填，价格必须是正整数（分）", 400)
        if not isinstance(stock, int) or stock < 0:
            return err("库存不能为负", 400)

        conn = get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO products(name, price_cents, stock) VALUES(?,?,?)",
                (name, price, stock),
            )
            pid = cur.lastrowid
            # 期初库存也要记一笔，否则对账时没有基准
            conn.execute(
                "INSERT INTO stock_log(product_id, delta, reason, order_id, created_at)"
                " VALUES(?,?,?,?,?)",
                (pid, stock, "init", None, time.time()),
            )
        finally:
            conn.close()

        return jsonify({"id": pid, "name": name, "price_cents": price, "stock": stock}), 201

    @app.get("/api/products")
    def list_products():
        conn = get_conn()
        try:
            rows = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
        finally:
            conn.close()
        return jsonify([dict(r) for r in rows])

    @app.get("/api/products/<int:pid>")
    def get_product(pid: int):
        conn = get_conn()
        try:
            row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return err("商品不存在", 404)
        return jsonify(dict(row))

    # ---------- 下单 ----------

    def _retry_on_lock(fn, attempts: int = 3):
        """并发写会短时撞锁；只重试锁竞争，其他错误直接抛——
        不加区分的重试会把真 bug 藏起来。SQLite 撞整库写锁，MySQL 撞行锁等待。"""
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return fn()
            except dbi.DBError as exc:
                last_exc = exc
                if dbi.is_lock_error(exc) and attempt < attempts - 1:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    @app.post("/api/orders")
    def create_order():
        body = request.get_json(silent=True) or {}
        pid = body.get("product_id")
        qty = body.get("qty")
        key = body.get("idempotency_key")

        if not isinstance(pid, int) or not isinstance(qty, int):
            return err("product_id 与 qty 必须是整数", 400)
        if qty <= 0:
            return err("购买数量必须大于 0", 400)

        return _retry_on_lock(lambda: _create_order_once(pid, qty, key))

    def _create_order_once(pid: int, qty: int, key: str | None):
        conn = get_conn()
        try:
            conn.begin()

            # 幂等：同一个 key 只产生一张订单，重复提交返回已有订单
            if key:
                exist = conn.execute(
                    "SELECT * FROM orders WHERE idempotency_key=?", (key,)
                ).fetchone()
                if exist is not None:
                    conn.commit()
                    return jsonify(order_to_dict(exist)), 200

            prod = conn.execute(
                "SELECT * FROM products WHERE id=?", (pid,)
            ).fetchone()
            if prod is None:
                conn.rollback()
                return err("商品不存在", 404)

            # 关键：扣减与「库存是否足够」在同一条 SQL 里完成，
            # 分成「先查再减」两步会在并发下超卖
            cur = conn.execute(
                "UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                (qty, pid, qty),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return err("库存不足", 409, stock=prod["stock"], requested=qty)

            oid = uuid.uuid4().hex[:12]
            now = time.time()
            try:
                conn.execute(
                    "INSERT INTO orders(id, product_id, qty, amount_cents, status,"
                    " idempotency_key, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (oid, pid, qty, prod["price_cents"] * qty, "pending", key, now, now),
                )
            except dbi.DBError as exc:
                # MySQL 行锁下「先查后插」有窗口期：并发同键会撞唯一约束。
                # 撞了就回滚、回查赢家的订单返回 200 —— 唯一约束才是幂等的最终兜底。
                if key and dbi.is_unique_violation(exc):
                    conn.rollback()
                    exist = conn.execute(
                        "SELECT * FROM orders WHERE idempotency_key=?", (key,)
                    ).fetchone()
                    if exist is not None:
                        return jsonify(order_to_dict(exist)), 200
                raise
            conn.execute(
                "INSERT INTO stock_log(product_id, delta, reason, order_id, created_at)"
                " VALUES(?,?,?,?,?)",
                (pid, -qty, "order_create", oid, now),
            )
            conn.commit()

            row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            return jsonify(order_to_dict(row)), 201
        except Exception:
            try:
                conn.rollback()
            except dbi.DBError:
                pass
            raise
        finally:
            conn.close()

    # ---------- 查询 ----------

    @app.get("/api/orders/<oid>")
    def get_order(oid: str):
        conn = get_conn()
        try:
            row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if row is None:
                return err("订单不存在", 404)

            # 懒过期：待支付订单超时后在看的时候才取消，省一个定时任务
            timeout = app.config["AUTO_CANCEL_SECONDS"]
            if row["status"] == "pending" and time.time() - row["created_at"] >= timeout:
                conn.execute(
                    "UPDATE orders SET status='cancelled', updated_at=? WHERE id=?",
                    (time.time(), oid),
                )
                conn.execute(
                    "UPDATE products SET stock=stock+? WHERE id=?",
                    (row["qty"], row["product_id"]),
                )
                conn.execute(
                    "INSERT INTO stock_log(product_id, delta, reason, order_id, created_at)"
                    " VALUES(?,?,?,?,?)",
                    (row["product_id"], -row["qty"], "timeout_cancel", oid, time.time()),
                )
                row = conn.execute(
                    "SELECT * FROM orders WHERE id=?", (oid,)
                ).fetchone()
        finally:
            conn.close()
        return jsonify(order_to_dict(row))

    @app.get("/api/orders")
    def list_orders():
        status = request.args.get("status")
        conn = get_conn()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM orders WHERE status=? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM orders ORDER BY created_at DESC"
                ).fetchall()
        finally:
            conn.close()
        return jsonify([order_to_dict(r) for r in rows])

    # ---------- 状态流转 ----------

    def _transit_once(oid: str, target: str, on_success=None):
        conn = get_conn()
        try:
            conn.begin()
            row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if row is None:
                conn.rollback()
                return err("订单不存在", 404)

            allowed = STATE_MACHINE.get(row["status"], set())
            if target not in allowed:
                conn.rollback()
                return err(
                    f"订单当前状态 {row['status']} 不允许变更为 {target}",
                    409,
                    current_status=row["status"],
                )

            conn.execute(
                "UPDATE orders SET status=?, updated_at=? WHERE id=?",
                (target, time.time(), oid),
            )
            if on_success is not None:
                on_success(conn, row)

            conn.commit()
            new_row = conn.execute(
                "SELECT * FROM orders WHERE id=?", (oid,)
            ).fetchone()
            return jsonify(order_to_dict(new_row)), 200
        except Exception:
            try:
                conn.rollback()
            except dbi.DBError:
                pass
            raise
        finally:
            conn.close()

    def transit(oid: str, target: str, on_success=None):
        """状态流转也要重试：并发下会等锁。

        没有这层包装时，"支付"和"取消"同时到达，输了锁的那个直接 500，
        看起来像业务缺陷，其实是数据库锁竞争的环境问题。
        """
        return _retry_on_lock(lambda: _transit_once(oid, target, on_success))

    @app.post("/api/orders/<oid>/pay")
    def pay_order(oid: str):
        return transit(oid, "paid")

    @app.post("/api/orders/<oid>/ship")
    def ship_order(oid: str):
        # 状态是否合法统一交给状态机判断。不在这里先查一遍再转 ——
        # 查完到转之间订单可能被另一个请求取消，那就把已取消的单发出去了。
        return transit(oid, "shipped")

    def _release_stock(conn, order_row, reason: str):
        conn.execute(
            "UPDATE products SET stock=stock+? WHERE id=?",
            (order_row["qty"], order_row["product_id"]),
        )
        conn.execute(
            "INSERT INTO stock_log(product_id, delta, reason, order_id, created_at)"
            " VALUES(?,?,?,?,?)",
            (order_row["product_id"], order_row["qty"], reason, order_row["id"], time.time()),
        )

    @app.post("/api/orders/<oid>/cancel")
    def cancel_order(oid: str):
        return transit(
            oid, "cancelled", on_success=lambda conn, row: _release_stock(conn, row, "order_cancel")
        )

    @app.post("/api/orders/<oid>/refund")
    def refund_order(oid: str):
        return transit(oid, "refunded")

    @app.post("/api/orders/<oid>/complete")
    def complete_order(oid: str):
        return transit(oid, "completed")

    return app


def main() -> None:
    db_path = os.environ.get("SHOP_DB_PATH", os.path.join(os.path.dirname(__file__), "shop.db"))
    app = create_app(db_path)
    app.run(host="127.0.0.1", port=8000, threaded=True, debug=False)


if __name__ == "__main__":
    main()