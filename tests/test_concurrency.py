"""并发测试。

串行用例永远发现不了超卖 —— 因为「先查库存再扣减」在单线程下是对的，
两个请求同时进来才会出问题。

单线程下「先查库存再扣减」永远是对的，只有并发才能把它证伪：
20 个人抢 10 件，最后必须真的只卖出 10 件。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest


def _parallel(n: int, fn):
    """用独立会话并发执行：requests.Session 不是为多线程共享设计的。"""
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(fn, range(n)))


class TestOversell:
    @pytest.mark.smoke
    def test_20_buyers_10_items(self, factory, db):
        p = factory.product(stock=10)

        def buy(_):
            c = factory.client.clone()
            return c.post("/api/orders", json={"product_id": p["id"], "qty": 1}).status_code

        codes = _parallel(20, buy)

        assert codes.count(201) == 10, f"应恰好 10 单成功，实际 {codes.count(201)}"
        assert codes.count(409) == 10, f"其余应因库存不足被拒，实际 409 有 {codes.count(409)}"
        assert codes.count(201) + codes.count(409) == 20, (
            f"不应出现 5xx 等其他状态：{sorted(set(codes))}"
        )
        assert db.stock(p["id"]) == 0, f"库存应清零，实际 {db.stock(p['id'])}"
        assert db.order_count(p["id"]) == 10
        assert db.log_sum(p["id"]) == 0, "流水合计应与库存一致"

    def test_stock_never_goes_negative(self, factory, db):
        p = factory.product(stock=5)

        def buy(_):
            c = factory.client.clone()
            return c.post("/api/orders", json={"product_id": p["id"], "qty": 2}).status_code

        codes = _parallel(10, buy)

        assert codes.count(201) == 2, f"5 件库存、每次买 2 件，只能成 2 单，实际 {codes.count(201)}"
        assert db.stock(p["id"]) == 1, "剩 1 件"
        assert db.stock(p["id"]) >= 0, "库存不允许为负"


class TestConcurrentIdempotency:
    def test_same_key_under_concurrency(self, factory, db):
        """同一个幂等键被并发提交 10 次，只能产生 1 张订单、扣 1 次库存。

        这是幂等最难的一种：先查后插在并发下会有窗口期，
        必须靠数据库唯一约束或行锁兜住。
        """
        p = factory.product(stock=100)
        key = factory.key()

        def buy(_):
            c = factory.client.clone()
            return c.post(
                "/api/orders",
                json={"product_id": p["id"], "qty": 1, "idempotency_key": key},
            ).status_code

        codes = _parallel(10, buy)

        assert db.order_count(p["id"]) == 1, f"应只有 1 张订单，实际 {db.order_count(p['id'])}"
        assert db.stock(p["id"]) == 99, f"库存只该扣 1 件，实际扣了 {100 - db.stock(p['id'])}"
        assert 201 in codes, f"至少有一次创建成功：{codes}"
        assert all(c in (200, 201) for c in codes), f"重键请求都应成功返回，实际 {sorted(set(codes))}"


class TestStateRace:
    def test_pay_and_cancel_race(self, factory, db):
        """同一张订单被「支付」和「取消」同时打到，只能有一个成功。

        这个场景比超卖更隐蔽：两个请求本身都合法，谁先谁后都说得通，
        但绝不能都成功——否则订单既 paid 又 cancelled，钱收了、库存也还了。
        """
        p = factory.product(stock=5)
        order = factory.order(p["id"], qty=2)

        def act(action: str):
            def run(_):
                c = factory.client.clone()
                return c.post(f"/api/orders/{order['id']}/{action}").status_code

            return run

        with ThreadPoolExecutor(max_workers=2) as pool:
            pay_future = pool.submit(act("pay"), 0)
            cancel_future = pool.submit(act("cancel"), 0)
            codes = [pay_future.result(), cancel_future.result()]

        assert sorted(codes) == [200, 409], f"应恰好一成一败，实际 {codes}"

        final = factory.client.get(f"/api/orders/{order['id']}").json()["status"]
        assert final in {"paid", "cancelled"}, f"终态非法：{final}"
        assert db.log_sum(p["id"]) == db.stock(p["id"]), "无论谁赢，账都必须平"

        if final == "cancelled":
            assert db.stock(p["id"]) == 5, "取消成功就该把库存还回去"
        else:
            assert db.stock(p["id"]) == 3, "支付成功则库存保持扣减"
