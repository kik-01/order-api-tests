"""测试数据工厂。

每条用例自己造一份独立数据，跑完就删。
这样同一个用例连跑一百次结果一样，也不会互相污染 —— 
「跑第二次就红」的用例，在 CI 里等于没有。
"""

from __future__ import annotations

import uuid

from common.client import ApiClient
from common.db import Db


class Factory:
    def __init__(self, client: ApiClient, db: Db):
        self.client = client
        self.db = db
        self._products: list[int] = []

    def product(self, name: str = "青花瓷马克杯", price_cents: int = 12800, stock: int = 10) -> dict:
        resp = self.client.post(
            "/api/products",
            json={"name": name, "price_cents": price_cents, "stock": stock},
        )
        assert resp.status_code == 201, f"造商品失败: {resp.status_code} {resp.text}"
        data = resp.json()
        self._products.append(data["id"])
        return data

    def order(self, product_id: int, qty: int = 1, key: str | None = None, expect: int = 201) -> dict:
        body: dict = {"product_id": product_id, "qty": qty}
        if key is not None:
            body["idempotency_key"] = key
        resp = self.client.post("/api/orders", json=body)
        assert resp.status_code == expect, (
            f"下单期望 {expect}，实际 {resp.status_code}: {resp.text}"
        )
        return resp.json() if resp.content else {}

    def paid_order(self, product_id: int, qty: int = 1) -> dict:
        order = self.order(product_id, qty)
        resp = self.client.post(f"/api/orders/{order['id']}/pay")
        assert resp.status_code == 200, f"支付失败: {resp.status_code} {resp.text}"
        return resp.json()

    @staticmethod
    def key(prefix: str = "idem") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:10]}"

    def cleanup(self) -> None:
        for pid in self._products:
            self.db.purge_product(pid)
        self._products.clear()
