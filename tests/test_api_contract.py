"""接口契约与查询类接口。

单开一组的理由：10 个接口里有 2 个（查商品、订单列表与状态过滤）在业务规则用例里只被"顺带调用"，
没有独立断言。接口文档写了参数和返回结构，就该有用例钉住——
否则研发改了字段名或过滤条件，测试全绿也发现不了。
"""

from __future__ import annotations

import time

import pytest

ORDER_FIELDS = {"id", "product_id", "qty", "amount_cents", "status", "created_at"}
PRODUCT_FIELDS = {"id", "name", "price_cents", "stock"}


class TestProductQuery:
    @pytest.mark.smoke
    def test_get_product_returns_created_fields(self, factory, client):
        created = factory.product(name="青花瓷马克杯", price_cents=12800, stock=7)

        resp = client.get(f"/api/products/{created['id']}")

        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == PRODUCT_FIELDS, f"字段与文档不一致：{sorted(body)}"
        assert body["name"] == "青花瓷马克杯"
        assert body["price_cents"] == 12800
        assert body["stock"] == 7

    def test_get_unknown_product_returns_404(self, client):
        resp = client.get("/api/products/999999")
        assert resp.status_code == 404
        assert resp.json()["code"] == 404


class TestOrderQuery:
    def test_order_payload_has_expected_fields(self, factory):
        product = factory.product()
        order = factory.order(product["id"], qty=2)

        assert set(order) == ORDER_FIELDS, f"字段与文档不一致：{sorted(order)}"
        assert order["amount_cents"] == product["price_cents"] * 2

    def test_list_orders_filters_by_status(self, factory, client):
        product = factory.product(stock=20)
        pending_a = factory.order(product["id"], qty=1)
        pending_b = factory.order(product["id"], qty=1)
        paid = factory.paid_order(product["id"], qty=1)

        resp = client.get("/api/orders", params={"status": "pending"})

        assert resp.status_code == 200
        rows = resp.json()
        assert all(r["status"] == "pending" for r in rows), "过滤后仍出现非 pending 的订单"
        ids = {r["id"] for r in rows}
        assert {pending_a["id"], pending_b["id"]} <= ids, "待支付订单应从过滤结果里返回"
        assert paid["id"] not in ids, "已支付订单不应出现在 pending 过滤结果里"

    def test_list_orders_with_unknown_status_returns_empty(self, factory, client):
        factory.product(stock=5)

        resp = client.get("/api/orders", params={"status": "no_such_status"})

        assert resp.status_code == 200, "未知状态应返回空列表，而不是报错"
        assert resp.json() == []

    def test_list_orders_sorted_by_created_at_desc(self, factory, client):
        product = factory.product(stock=20)
        older = factory.order(product["id"], qty=1)
        time.sleep(0.01)  # 保证两个订单的 created_at 不同，否则排序断言无意义
        newer = factory.order(product["id"], qty=1)

        rows = client.get("/api/orders").json()

        stamps = [r["created_at"] for r in rows]
        assert stamps == sorted(stamps, reverse=True), "列表应按创建时间倒序"
        order_ids = [r["id"] for r in rows]
        assert order_ids.index(newer["id"]) < order_ids.index(older["id"]), "新订单应排在旧订单前面"
