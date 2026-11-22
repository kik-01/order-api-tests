"""日志：控制台 INFO + 文件 DEBUG。

为什么要有文件日志：并发用例失败时，第一件事是数请求条数、看状态码时序，
pytest 捕获的控制台输出会被截断，留不下完整链路。logs/api.log 就是排查入口。
"""

from __future__ import annotations

import logging
import pathlib

LOG_DIR = pathlib.Path(__file__).resolve().parents[1] / "logs"


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
    file_handler = logging.FileHandler(LOG_DIR / "api.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    logger.addHandler(console)
    logger.addHandler(file_handler)

    logger._configured = True  # type: ignore[attr-defined]
    return logger