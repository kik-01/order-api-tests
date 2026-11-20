"""订单状态机测试。

真实系统的 bug 很少出在「功能不能用」，而是出在「不该能用的地方居然能用」。
状态机是这类问题最集中的地方：取消了的单子还能发货、退过款的还能完成。

规则见 README R3 / R4。
"""

from __future__ import annotations

import pytest


class TestHappyPath:
    """正向链路：每一步都按规则走，状态与库存都要对。"""

    @pytest.mark.smoke
    def test_full_lifecycle(self, factory, db):
        p = factory.product(stock=5)
        order = factory.order(p["id"], qty=2)
        assert order["status"] == "pending"
        assert order["amount_cents"] == p["price_cents"] * 2
        assert db.stock(p["id"]) == 3, "下单后库存应减 2"

        for action, expected in [
            ("pay", "paid"),
            ("ship", "shipped"),
            ("complete", "completed"),
        ]:
            resp = factory.client.post(f"/api/orders/{order['id']}/{action}")
            assert resp.status_code == 200, f"{action} 失败: {resp.text}"
            assert resp.json()["status"] == expected

    def test_amount_is_computed_by_server(self, factory):
        """金额必须由服务端算，不能被请求体带偏 —— 客户端传价格是最常见的漏洞。"""
        p = factory.product(price_cents=9900, stock=10)
        resp = factory.client.post(
            "/api/orders",
            json={"product_id": p["id"], "qty": 3, "amount_cents": 1},
        )
        assert resp.status_code == 201
        assert resp.json()["amount_cents"] == 29700, "服务端应忽略客户端传来的金额"


class TestIllegalTransition:
    """非法流转必须被拒绝，且返回 409 而不是 200。"""

    def test_cannot_ship_before_pay(self, factory):
        p = factory.product()
        order = factory.order(p["id"])
        resp = factory.client.post(f"/api/orders/{order['id']}/ship")
        assert resp.status_code == 409, f"未支付就发货应被拒绝，实际 {resp.status_code}"
        assert resp.json()["current_status"] == "pending"

    def test_cannot_pay_cancelled_order(self, factory):
        p = factory.product()
        order = factory.order(p["id"])
        assert factory.client.post(f"/api/orders/{order['id']}/cancel").status_code == 200

        resp = factory.client.post(f"/api/orders/{order['id']}/pay")
        assert resp.status_code == 409, "已取消的订单不能被支付"

    def test_cannot_ship_refunded_order(self, factory):
        p = factory.product()
        order = factory.paid_order(p["id"])
        assert factory.client.post(f"/api/orders/{order['id']}/refund").status_code == 200

        resp = factory.client.post(f"/api/orders/{order['id']}/ship")
        assert resp.status_code == 409, "已退款的订单不能发货"

    def test_cannot_complete_pending_order(self, factory):
        p = factory.product()
        order = factory.order(p["id"])
        resp = factory.client.post(f"/api/orders/{order['id']}/complete")
        assert resp.status_code == 409, "未发货的订单不能直接完成"

    def test_repeated_complete_is_rejected(self, factory):
        """终态不可再流转：重复点击「确认收货」不应该每次都返回 200。"""
        p = factory.product()
        order = factory.paid_order(p["id"])
        factory.client.post(f"/api/orders/{order['id']}/ship")
        assert factory.client.post(f"/api/orders/{order['id']}/complete").status_code == 200

        again = factory.client.post(f"/api/orders/{order['id']}/complete")
        assert again.status_code == 409, "已完成订单重复完成应被拒绝"

    def test_unknown_order_returns_404(self, client):
        assert client.post("/api/orders/not-a-real-id/pay").status_code == 404


class TestTimeoutCancel:
    """待支付超时自动取消（R3 补充规则）。"""

    def test_expired_order_is_cancelled_on_read(self, expiring_env):
        client, _, factory = expiring_env
        p = factory.product(stock=3)
        order = factory.order(p["id"], qty=1)

        got = client.get(f"/api/orders/{order['id']}").json()
        assert got["status"] == "cancelled", "超过支付时限的订单应自动取消"

    def test_expired_order_releases_stock(self, expiring_env):
        client, db, factory = expiring_env
        p = factory.product(stock=2)
        order = factory.order(p["id"], qty=2)
        assert db.stock(p["id"]) == 0

        client.get(f"/api/orders/{order['id']}")  # 读这一单才会触发过期处理

        # 库存是否回到 2 由规则 R5 决定；失败就说明超时取消没释放库存
        assert db.stock(p["id"]) == 2, "超时取消后库存应回滚"

    def test_pay_after_timeout_cancel_is_rejected(self, expiring_env):
        """超时被自动取消的订单，再支付必须被拒 —— 否则等于给已取消的单收了钱。"""
        client, _, factory = expiring_env
        p = factory.product(stock=3)
        order = factory.order(p["id"], qty=1)

        assert client.get(f"/api/orders/{order['id']}").json()["status"] == "cancelled"

        resp = client.post(f"/api/orders/{order['id']}/pay")

        assert resp.status_code == 409, f"已取消订单不应能支付，实际 {resp.status_code}"
        assert resp.json()["current_status"] == "cancelled"
