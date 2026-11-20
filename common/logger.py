"""日志：控制台 INFO + 文件 DEBUG，按 2MB 轮转，敏感字段打码。

为什么要有文件日志：并发和重试用例失败时，第一件事是数请求条数、看状态码时序，
pytest 捕获的控制台输出会被截断，留不下完整链路。

（这一层沿用旧框架 api-framework 的 common/logger.py，去掉多环境配置，只保留轮转与打码。）
"""

from __future__ import annotations

import logging
import pathlib
import re
from logging.handlers import RotatingFileHandler

LOG_DIR = pathlib.Path(__file__).resolve().parents[1] / "logs"

# 现在是本地模拟服务，没有真实凭证；接真实业务后 token / 手机号会进日志，先挡住
_MASKS = [
    (
        re.compile(r"(?i)((?:token|password|phone|idempotency_key)\"?\s*[:=]\s*\"?)([^\s\",}]+)"),
        r"\1***",
    ),
]


class _MaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # 格式化失败时原样放行，别让日志把用例搞挂
            return True
        for pattern, repl in _MASKS:
            msg = pattern.sub(repl, msg)
        record.msg, record.args = msg, ()
        return True


def setup(level: int = logging.INFO) -> logging.Logger:
    """幂等：多次 import 只挂一套 handler。"""
    logger = logging.getLogger("api")
    if getattr(logger, "_configured", False):
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    LOG_DIR.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_DIR / "api.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    for handler in (console, file_handler):
        handler.addFilter(_MaskFilter())
        logger.addHandler(handler)

    logger._configured = True  # type: ignore[attr-defined]
    return logger
