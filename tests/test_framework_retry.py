"""框架自身也要有测试。

「写操作不重试」这条规则，如果只是写在代码注释里，改起来没人拦得住。
用一个假的 session 把它钉死：以后谁把 POST 也加进重试，这里立刻红。

内化口径（一句话）：读超时说明请求可能已被处理，写操作重发就是重复下单；
假 session 只数调用次数，不发真请求。
"""

from __future__ import annotations

import pytest
import requests

from common.client import ApiClient


class FakeSession:
    """不真发请求，只记录被调用了几次，然后按需抛异常。"""

    def __init__(self, exc: Exception | None = None):
        self.headers: dict = {}
        self.calls = 0
        self._exc = exc

    def request(self, *args, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"{}"
        return resp


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """重试之间的退避对测试没有意义，直接掐掉等待。"""
    monkeypatch.setattr("common.client.time.sleep", lambda *_: None)


def test_get_is_retried():
    client = ApiClient("http://example.invalid", retries=2)
    fake = FakeSession(requests.ConnectionError("connection reset"))
    client.session = fake

    with pytest.raises(requests.ConnectionError):
        client.get("/anything")

    assert fake.calls == 3, f"GET 应重试到 3 次，实际 {fake.calls}"


def test_post_is_not_retried():
    client = ApiClient("http://example.invalid", retries=2)
    fake = FakeSession(requests.ConnectionError("read timeout"))
    client.session = fake

    with pytest.raises(requests.ConnectionError):
        client.post("/orders", json={"product_id": 1, "qty": 1})

    assert fake.calls == 1, (
        f"POST 不应自动重试：读超时说明请求可能已被处理，"
        f"重发就是重复下单。实际被调用 {fake.calls} 次"
    )


def test_post_can_retry_when_asked_explicitly():
    """确实需要重试时（比如上游支持幂等键），必须显式要求。"""
    client = ApiClient("http://example.invalid", retries=0)
    fake = FakeSession(requests.ConnectionError("read timeout"))
    client.session = fake

    with pytest.raises(requests.ConnectionError):
        client.post("/orders", json={}, retries=1)

    assert fake.calls == 2


def test_non_network_error_is_not_retried():
    """只重试网络类错误；ValueError 这类是真 bug，重试会把它藏起来。"""
    client = ApiClient("http://example.invalid", retries=2)
    fake = FakeSession(ValueError("bad payload"))
    client.session = fake

    with pytest.raises(ValueError):
        client.get("/anything")

    assert fake.calls == 1, f"非网络错误不应重试，实际被调用 {fake.calls} 次"