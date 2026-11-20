"""库存与流水对账。

接口返回 200 不代表数据是对的。异步写、事务回滚、主从延迟，
这三种情况下响应体完全正常，账却是错的。

对账口径只有一条：
    当前库存  ==  流水合计（期初 + 每一次增减）

只要有一笔库存变动没记账，或记错方向，这个等式立刻不平。
"""

from __future__ import annotations

import pytest


class TestReconcile:
    def test_initial_state_balances(self, factory, db):
        p = factory.product(stock=8)
        assert db.stock(p["id"]) == db.log_sum(p["id"]) == 8

    @pytest.mark.smoke
    def test_after_order_balances(self, factory, db):
        p = factory.product(stock=8)
        factory.order(p["id"], qty=3)
        assert db.stock(p["id"]) == 5
        assert db.log_sum(p["id"]) == 5, "扣库存必须同时记流水"

    def test_after_cancel_balances(self, factory, db):
        p = factory.product(stock=8)
        order = factory.order(p["id"], qty=3)
        factory.client.post(f"/api/orders/{order['id']}/cancel")

        assert db.stock(p["id"]) == 8, "取消应回滚库存"
        assert db.log_sum(p["id"]) == 8, "回滚也要记流水"
        reasons = [x["reason"] for x in db.logs(p["id"])]
        assert "order_cancel" in reasons

    @pytest.mark.xfail(
        strict=True,
        reason="BUG-002：超时取消回滚了库存，但流水记成负数，账实不符。见 02-缺陷记录.md",
    )
    def test_after_timeout_cancel_balances(self, expiring_env):
        client, db, factory = expiring_env
        p = factory.product(stock=5)
        order = factory.order(p["id"], qty=2)
        client.get(f"/api/orders/{order['id']}")

        assert db.stock(p["id"]) == db.log_sum(p["id"]), (
            f"库存 {db.stock(p['id'])} 与流水合计 {db.log_sum(p['id'])} 不一致"
        )


class TestStockRollback:
    """业务规则 R5 / R6：取消和退款都要把库存还回去。"""

    def test_cancel_returns_stock(self, factory, db):
        p = factory.product(stock=4)
        order = factory.order(p["id"], qty=4)
        assert db.stock(p["id"]) == 0

        factory.client.post(f"/api/orders/{order['id']}/cancel")
        assert db.stock(p["id"]) == 4, "取消后库存应全部回来"

    @pytest.mark.xfail(
        strict=True,
        reason="BUG-001：退款只改订单状态，没有回滚库存。见 02-缺陷记录.md",
    )
    def test_refund_returns_stock(self, factory, db):
        p = factory.product(stock=4)
        order = factory.paid_order(p["id"], qty=2)  # 支付后库存 2

        assert factory.client.post(f"/api/orders/{order['id']}/refund").status_code == 200
        assert db.stock(p["id"]) == 4, "退款后库存应回到 4（货退回来了）"
