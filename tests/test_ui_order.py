"""UI 层用例：Playwright 操作页面，断言 DOM 之后再用 API 层同一套对账口径查库。

为什么 UI 用例也要查库：页面显示「下单成功」不代表库里真的扣了库存、记了流水。
UI → API → DB 三层对齐，才是「点出来的绿」和「账真的平」的区别。

运行：python -m pytest -m ui  （默认 pytest 只跑接口层，见 pytest.ini）
页面只调用既有 API，不含独立业务逻辑，所以 UI 缺陷最终都能落到接口/数据层解释。
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.ui


def _open(page, server):
    page.goto(f"{server[0]}/")


def _submit(page, pid: int, qty: int):
    page.select_option("[data-testid=product-select]", str(pid))
    page.fill("[data-testid=qty-input]", str(qty))
    page.click("[data-testid=submit-btn]")


def _row(page):
    return page.locator("[data-testid=order-row]")


def test_ui_create_order_and_reconcile(page, server, factory, db):
    """UI 下单成功：页面出现 pending 订单，且库里库存与流水对账平。"""
    p = factory.product(stock=10)
    _open(page, server)
    _submit(page, p["id"], 2)

    expect(_row(page)).to_have_count(1)
    expect(_row(page).locator("[data-testid=order-status]")).to_have_text("pending")
    assert db.stock(p["id"]) == 8, "UI 下单后库存应扣 2"
    assert db.log_sum(p["id"]) == db.stock(p["id"]), "UI 下单后账必须平"


def test_ui_invalid_qty_leaves_no_dirty_data(page, server, factory, db):
    """UI 提交非法数量：页面报错，库里不留脏数据。"""
    p = factory.product(stock=10)
    _open(page, server)
    _submit(page, p["id"], 0)

    expect(page.locator("[data-testid=msg]")).to_contain_text("大于 0")
    assert db.order_count(p["id"]) == 0
    assert db.stock(p["id"]) == 10


def test_ui_insufficient_stock_shows_error(page, server, factory, db):
    """UI 超库存下单：页面提示库存不足，库存分毫不动。"""
    p = factory.product(stock=1)
    _open(page, server)
    _submit(page, p["id"], 5)

    expect(page.locator("[data-testid=msg]")).to_contain_text("库存不足")
    assert db.stock(p["id"]) == 1
    assert db.order_count(p["id"]) == 0


def test_ui_pay_updates_status(page, server, factory, db):
    """UI 点支付：页面状态变 paid，库里订单状态一致。"""
    p = factory.product(stock=5)
    _open(page, server)
    _submit(page, p["id"], 1)
    expect(_row(page)).to_have_count(1)

    oid = _row(page).get_attribute("data-oid")
    page.click(f"[data-testid=pay-btn][data-oid='{oid}']")
    expect(_row(page).locator("[data-testid=order-status]")).to_have_text("paid")
    assert db.order(oid)["status"] == "paid"


def test_ui_double_submit_creates_one_order(page, server, factory, db):
    """UI 连点两下提交（同一幂等键）：只成一单、只扣一次库存。"""
    p = factory.product(stock=10)
    _open(page, server)
    page.select_option("[data-testid=product-select]", str(p["id"]))
    page.fill("[data-testid=qty-input]", "1")
    page.dblclick("[data-testid=submit-btn]")

    expect(_row(page)).to_have_count(1)
    assert db.order_count(p["id"]) == 1, "重复提交不能变两张订单"
    assert db.stock(p["id"]) == 9
    assert db.log_sum(p["id"]) == db.stock(p["id"])


def test_ui_cancel_restores_stock_then_pay_rejected(page, server, factory, db):
    """UI 取消回滚库存；再对已取消订单点支付，页面拒绝且状态不变。"""
    p = factory.product(stock=5)
    _open(page, server)
    _submit(page, p["id"], 2)
    expect(_row(page)).to_have_count(1)
    oid = _row(page).get_attribute("data-oid")

    page.click(f"[data-testid=cancel-btn][data-oid='{oid}']")
    expect(_row(page).locator("[data-testid=order-status]")).to_have_text("cancelled")
    assert db.stock(p["id"]) == 5, "取消后库存应回滚"

    page.click(f"[data-testid=pay-btn][data-oid='{oid}']")
    expect(page.locator("[data-testid=msg]")).to_contain_text("不允许")
    expect(_row(page).locator("[data-testid=order-status]")).to_have_text("cancelled")
    assert db.log_sum(p["id"]) == db.stock(p["id"]), "非法流转后账仍要平"