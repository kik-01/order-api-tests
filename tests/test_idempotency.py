"""幂等测试。

客户端超时重试、用户连点两下、消息队列重投 —— 同一个下单请求到达两次是常态。
没有幂等键，就是两张订单、扣两次库存、发两次货。

规则见 README R2。
"""

from __future__ import annotations

import pytest


class TestCreateIdempotency:
    @pytest.mark.smoke
    def test_same_key_creates_one_order(self, factory, db):
        p = factory.product(stock=10)
        key = factory.key()

        first = factory.order(p["id"], qty=2, key=key, expect=201)
        second = factory.order(p["id"], qty=2, key=key, expect=200)

        assert second["id"] == first["id"], "同一个幂等键必须返回同一张订单"
        assert db.order_count(p["id"]) == 1, f"应只有 1 张订单，实际 {db.order_count(p['id'])}"
        assert db.stock(p["id"]) == 8, "库存只能扣一次"

    def test_same_key_ignores_changed_payload(self, factory, db):
        """同键不同参：按「首次请求为准」返回原订单，不能让第二次的参数生效。

        这一条最容易被忽略 —— 客户端重试时参数变了（比如用户改了数量），
        如果服务端拿新参数重新下单，就等于用同一个键做了两件事。
        """
        p = factory.product(stock=10)
        key = factory.key()

        first = factory.order(p["id"], qty=2, key=key, expect=201)
        second = factory.order(p["id"], qty=5, key=key, expect=200)

        assert second["id"] == first["id"]
        assert second["qty"] == 2, "应以首次请求的 2 件为准，不能被改成 5 件"
        assert db.stock(p["id"]) == 8, "库存只能扣首次的 2 件"

    def test_different_keys_create_two_orders(self, factory, db):
        p = factory.product(stock=10)
        a = factory.order(p["id"], qty=1, key=factory.key(), expect=201)
        b = factory.order(p["id"], qty=1, key=factory.key(), expect=201)

        assert a["id"] != b["id"]
        assert db.order_count(p["id"]) == 2
        assert db.stock(p["id"]) == 8

    def test_without_key_creates_two_orders(self, factory, db):
        """不传键就没有幂等保护 —— 这是设计如此，但要在文档里写清，
        免得前端以为连点两下没关系。"""
        p = factory.product(stock=10)
        factory.order(p["id"], qty=1, expect=201)
        factory.order(p["id"], qty=1, expect=201)
        assert db.order_count(p["id"]) == 2

    def test_failed_order_does_not_leave_trace(self, factory, db):
        """库存不足导致的失败，不能留下脏数据。

        半成品订单（有单无货）在真实系统里很难清，对账时也会对不上。
        """
        p = factory.product(stock=1)
        key = factory.key()

        failed = factory.order(p["id"], qty=5, key=key, expect=409)
        assert failed["code"] == 409

        assert db.order_count(p["id"]) == 0, "下单失败不应产生订单"
        assert db.stock(p["id"]) == 1, "下单失败不该扣库存"
