"""HTTP 客户端：所有请求从这里出去。

只做四件事：拼地址、带鉴权式的通用头、记日志、按方法决定要不要重试。
"""

from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger("api")

# 只有这些方法重试是安全的：服务端重复执行不会改变结果。
# POST 不重试 —— 读超时说明请求可能已经被处理，重发一次就是重复下单。
SAFE_TO_RETRY = frozenset({"GET", "HEAD", "OPTIONS"})


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 10.0, retries: int = 1):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update(
            {"Content-Type": "application/json", "Accept": "application/json"}
        )

    def request(self, method: str, path: str, retries: int | None = None, **kwargs):
        method = method.upper()
        url = path if path.startswith("http") else f"{self.base_url}{path}"

        if retries is not None:
            attempts = retries + 1
        elif method in SAFE_TO_RETRY:
            attempts = self.retries + 1
        else:
            attempts = 1

        last_exc: Exception | None = None
        for i in range(attempts):
            started = time.monotonic()
            if "json" in kwargs:
                # 只在 DEBUG 落请求体，且截断，避免日志爆炸
                payload = json.dumps(kwargs["json"], ensure_ascii=False)
                log.debug("%s %s body=%s", method, url, payload[:500])
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                log.warning("%s %s 第 %d/%d 次失败: %s", method, url, i + 1, attempts, type(exc).__name__)
                if i + 1 < attempts:
                    time.sleep(0.5 * (i + 1))
                    continue
                raise

            cost_ms = (time.monotonic() - started) * 1000
            log.info("%s %s -> %s (%.0fms)", method, url, resp.status_code, cost_ms)
            return resp

        raise last_exc  # type: ignore[misc]

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw):
        return self.request("POST", path, **kw)

    def clone(self) -> "ApiClient":
        """复制一个独立会话，供多线程使用。requests.Session 不适合跨线程共享。"""
        return ApiClient(self.base_url, self.timeout, self.retries)
