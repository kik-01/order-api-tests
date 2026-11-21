"""Run a small, reproducible JMeter baseline load test against the order API."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from werkzeug.serving import make_server


ROOT = Path(__file__).resolve().parents[1]
PERF_DIR = Path(__file__).resolve().parent
JMX = PERF_DIR / "order_api_load_test.jmx"
sys.path.insert(0, str(ROOT))

from app.server import create_app  # noqa: E402


logging.getLogger("werkzeug").setLevel(logging.ERROR)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_jmeter(explicit: str | None = None) -> Path:
    candidates = [
        Path(explicit) if explicit else None,
        Path(os.environ.get("JMETER_HOME", "")) / "bin" / "jmeter.bat" if os.environ.get("JMETER_HOME") else None,
        Path(r"D:\测试工具\JMeter\bin\jmeter.bat"),
        Path(r"D:\测试工具\apache-jmeter-5.6.3\bin\jmeter.bat"),
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate

    command = shutil.which("jmeter")
    if command:
        return Path(command)
    raise FileNotFoundError("未找到 JMeter。可通过 --jmeter 指定 jmeter.bat。")


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def summarize(jtl: Path, output: Path, config: dict) -> dict:
    elapsed: list[float] = []
    success = 0
    total = 0
    status_codes: dict[str, int] = {}
    start = None
    end = None

    with jtl.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            total += 1
            value = float(row.get("elapsed") or 0)
            elapsed.append(value)
            if str(row.get("success", "")).lower() == "true":
                success += 1
            code = row.get("responseCode") or "NO_RESPONSE"
            status_codes[code] = status_codes.get(code, 0) + 1
            timestamp = int(row.get("timeStamp") or 0)
            duration = int(row.get("time") or 0)
            start = timestamp if start is None else min(start, timestamp)
            end = timestamp + duration if end is None else max(end, timestamp + duration)

    wall_seconds = max((end - start) / 1000, 0.001) if start and end else 0.0
    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": config,
        "samples": total,
        "successful": success,
        "failed": total - success,
        "error_rate_percent": round((total - success) / total * 100, 2) if total else 0.0,
        "throughput_per_second": round(total / wall_seconds, 2) if wall_seconds else 0.0,
        "avg_ms": round(sum(elapsed) / total, 2) if total else 0.0,
        "p95_ms": round(percentile(elapsed, 0.95), 2),
        "min_ms": round(min(elapsed), 2) if elapsed else 0.0,
        "max_ms": round(max(elapsed), 2) if elapsed else 0.0,
        "response_codes": status_codes,
    }
    output.write_text(
        "\n".join(
            [
                "# JMeter Baseline Result",
                "",
                f"- Generated: {result['generated_at']}",
                f"- Users: {config['threads']}",
                f"- Ramp-up: {config['ramp_up']} s",
                f"- Duration: {config['duration']} s",
                f"- Samples: {result['samples']}",
                f"- Success: {result['successful']}",
                f"- Errors: {result['failed']} ({result['error_rate_percent']}%)",
                f"- Average: {result['avg_ms']} ms",
                f"- P95: {result['p95_ms']} ms",
                f"- Throughput: {result['throughput_per_second']} requests/s",
                f"- Response codes: {json.dumps(status_codes, ensure_ascii=False)}",
                "",
                "> This is a local Flask + SQLite baseline, not production capacity.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (output.parent / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the JMeter order API baseline load test")
    parser.add_argument("--jmeter", help="Path to jmeter.bat")
    parser.add_argument("--threads", type=int, default=10)
    parser.add_argument("--ramp-up", type=int, default=3)
    parser.add_argument("--duration", type=int, default=20)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = PERF_DIR / "results" / stamp
    report_dir = output_dir / "html-report"
    output_dir.mkdir(parents=True, exist_ok=False)

    db_path = output_dir / "load-test.db"
    port = free_port()
    app = create_app(str(db_path), auto_cancel_seconds=1800)
    server = make_server("127.0.0.1", port, app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                requests.get(f"{base_url}/api/products/999999", timeout=1)
                break
            except requests.ConnectionError:
                time.sleep(0.05)
        else:
            raise RuntimeError("被测服务未能启动")

        created = requests.post(
            f"{base_url}/api/products",
            json={"name": "JMeter Load Product", "price_cents": 12800, "stock": 200000},
            timeout=5,
        )
        created.raise_for_status()
        product_id = created.json()["id"]

        jmeter = find_jmeter(args.jmeter)
        jtl = output_dir / "samples.jtl"
        command = [
            str(jmeter),
            "-n",
            "-t",
            str(JMX),
            "-l",
            str(jtl),
            "-e",
            "-o",
            str(report_dir),
            "-j",
            str(output_dir / "jmeter.log"),
            f"-Jhost=127.0.0.1",
            f"-Jport={port}",
            f"-Jproduct_id={product_id}",
            f"-Jthreads={args.threads}",
            f"-Jramp_up={args.ramp_up}",
            f"-Jduration={args.duration}",
        ]
        print("Running:", " ".join(command))
        completed = subprocess.run(command, cwd=ROOT)
        if completed.returncode not in (0, 1):
            raise RuntimeError(f"JMeter 执行失败，退出码 {completed.returncode}")

        result = summarize(
            jtl,
            output_dir / "summary.md",
            {"threads": args.threads, "ramp_up": args.ramp_up, "duration": args.duration},
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"Report: {report_dir / 'index.html'}")
        return 0 if result["failed"] == 0 else 1
    finally:
        server.shutdown()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
