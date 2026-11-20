"""入参校验：等价类 + 边界值。

业务规则（R1–R7）验证的是"流程对不对"，这一组验证"不该进的东西进不来"。
这是最容易漏的一层：需求文档通常只写正常值，异常值要测试自己列等价类，
而真实线上故障里，参数校验缺失（负数、类型错、缺字段）占比很高。

7 + 8 条用例，靠两张参数表覆盖 5 个字段的无效等价类，不重复写样板代码。
"""

from __future__ import annotations

import pytest

# ---------------- 下单入参：7 个无效等价类 ----------------

ORDER_INVALID = [
    ("qty_zero", {"qty": 0}, 400),
    ("qty_negative", {"qty": -1}, 400),
    ("qty_float", {"qty": 1.5}, 400),
    ("qty_string", {"qty": "2"}, 400),
    ("qty_null", {"qty": None}, 400),
    ("product_not_exist", {"product_id": 999999}, 404),
    ("product_id_string", {"product_id": "abc"}, 400),
]


@pytest.mark.parametrize("case_id, override, expect", ORDER_INVALID, ids=[c[0] for c in ORDER_INVALID])
def test_create_order_rejects_invalid_params(factory, client, case_id, override, expect):
    product = factory.product(stock=10)
    body = {"product_id": product["id"], "qty": 1}
    body.update(override)

    resp = client.post("/api/orders", json=body)

    assert resp.status_code == expect, f"{case_id}: 期望 {expect}，实际 {resp.status_code} {resp.text}"


# ---------------- 建商品入参：8 个无效等价类 ----------------

PRODUCT_INVALID = [
    ("name_empty", {"name": ""}, 400),
    ("name_null", {"name": None}, 400),
    ("price_zero", {"price_cents": 0}, 400),
    ("price_negative", {"price_cents": -100}, 400),
    ("price_float", {"price_cents": 12.5}, 400),
    ("price_string", {"price_cents": "12800"}, 400),
    ("stock_negative", {"stock": -1}, 400),
    ("stock_string", {"stock": "10"}, 400),
]


@pytest.mark.parametrize("case_id, override, expect", PRODUCT_INVALID, ids=[c[0] for c in PRODUCT_INVALID])
def test_create_product_rejects_invalid_params(client, case_id, override, expect):
    body = {"name": "青花瓷马克杯", "price_cents": 12800, "stock": 10}
    body.update(override)

    resp = client.post("/api/products", json=body)

    assert resp.status_code == expect, f"{case_id}: 期望 {expect}，实际 {resp.status_code} {resp.text}"


# ---------------- 无效参数不留脏数据 ----------------

def test_invalid_order_does_not_touch_stock(factory, db, client):
    """被拒绝的请求不能改动任何数据 —— 只断状态码不够，要查库。"""
    product = factory.product(stock=10)

    assert client.post("/api/orders", json={"product_id": product["id"], "qty": 0}).status_code == 400
    assert client.post("/api/orders", json={"product_id": product["id"], "qty": 999}).status_code == 409

    assert db.stock(product["id"]) == 10, "失败的请求不应扣库存"
    assert db.order_count(product["id"]) == 0, "失败的请求不应留下订单"
    assert db.log_sum(product["id"]) == 10, "失败的请求不应写流水"
