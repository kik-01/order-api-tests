"""并发竞态探针：连跑 N 轮并发用例，统计抖动。

用法：python perf/race_probe.py [--rounds 25]
继承当前环境变量（SHOP_DB / MYSQL_*），同一脚本可探 SQLite 或 MySQL。
README 与测试报告里的「N 轮无抖动」用本脚本可复现，不是手工口头数据。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = "race or same_key_under_concurrency or 20_buyers"


def main() -> int:
    parser = argparse.ArgumentParser(description="并发竞态抖动探针")
    parser.add_argument("--rounds", type=int, default=25)
    args = parser.parse_args()

    fails = 0
    for i in range(1, args.rounds + 1):
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-k", KEY],
            cwd=ROOT, capture_output=True, text=True,
        )
        lines = proc.stdout.strip().splitlines()
        tail = lines[-1] if lines else proc.stderr.strip().splitlines()[-1:] or [""]
        tail = tail if isinstance(tail, str) else tail[0]
        if proc.returncode != 0 or "passed" not in tail:
            fails += 1
            print(f"round {i} FAIL: {tail}")

    backend = os.environ.get("SHOP_DB", "sqlite")
    print(f"backend={backend} rounds={args.rounds} fails={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())