"""
Run the performance test as several processes at once and merge the results.

A single Python process holding 100 Socket.IO clients plus 50 sender threads
shares one GIL, so the load generator itself can add a lot of latency. This
launcher starts several copies of tests/performance_test.py (each with its own
users and its own GIL), waits until every copy is connected, releases them all
at the same moment, then merges their CSV files.

Start the server first (PowerShell):
    $env:CHAT_PERFORMANCE_TEST="1"; python app.py

Then, from the project root:
    python tests/run_split_perf.py              # 5 processes x 20 users = 100 users
    python tests/run_split_perf.py 4 26         # 4 processes x 26 users
    python tests/run_split_perf.py 5 20 0.5     # slower send rate (seconds between messages)

Users per process must be an even number (they are paired up).
"""

import csv
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = os.environ.get("PERF_BASE_URL", "https://127.0.0.1:5000")
TARGET_P95_MS = 200

WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
USERS_PER_WORKER = int(sys.argv[2]) if len(sys.argv) > 2 else 20
SEND_INTERVAL = sys.argv[3] if len(sys.argv) > 3 else "0.15"

READY_TIMEOUT = 600
FINISH_TIMEOUT = 300

COLUMNS = [
    "latency_ms",
    "entry_latency_ms",
    "inbound_ms",
    "handler_ms",
    "outbound_ms",
]


def percentile(values, percent):
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(values):
    if not values:
        return None
    return {
        "count": len(values),
        "p50": round(percentile(values, 50), 1),
        "p95": round(percentile(values, 95), 1),
        "p99": round(percentile(values, 99), 1),
        "max": round(max(values), 1),
    }


def write_atomic(path, text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def server_stats(reset=False):
    try:
        response = requests.get(
            f"{BASE_URL}/perf-stats",
            params={"reset": "1"} if reset else None,
            verify=False,
            timeout=10,
        )
        if response.status_code == 404:
            return "disabled (start the server with CHAT_PERFORMANCE_TEST=1)"
        return response.json()
    except Exception as exc:
        return f"unavailable ({type(exc).__name__}: {exc})"


def main():
    if USERS_PER_WORKER < 2 or USERS_PER_WORKER % 2:
        raise SystemExit("Users per process must be an even number.")

    script = Path(__file__).with_name("performance_test.py")
    run_dir = Path("tests") / "results" / f"split_{datetime.now():%Y%m%d_%H%M%S}"
    sync_dir = run_dir / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)

    total_users = WORKERS * USERS_PER_WORKER
    expected = total_users // 2 * 5

    print(f"Starting {WORKERS} processes x {USERS_PER_WORKER} users "
          f"= {total_users} users, {expected} messages")
    print(f"Results folder: {run_dir}")

    # perf-stats only exists in performance mode; this also clears old samples.
    stats_before = server_stats(reset=True)
    if isinstance(stats_before, str):
        print(f"Server /perf-stats: {stats_before}")

    env = dict(os.environ)
    env.update({
        "PERF_USERS": str(USERS_PER_WORKER),
        "PERF_INTERVAL": str(SEND_INTERVAL),
        "PERF_SYNC_DIR": str(sync_dir),
        "PERF_RESULT_DIR": str(run_dir),
    })

    procs = []
    for index in range(WORKERS):
        log = open(run_dir / f"worker_{index}.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        procs.append((proc, log))

    # ------------------------------------------------ wait until all connected
    print("Registering, logging in and connecting (this takes a while)...")
    deadline = time.monotonic() + READY_TIMEOUT
    failed = False

    while True:
        ready = len(list(sync_dir.glob("ready_*")))
        if ready >= WORKERS:
            break
        if any(proc.poll() is not None for proc, _ in procs):
            failed = True
            break
        if time.monotonic() > deadline:
            failed = True
            break
        time.sleep(0.5)

    if failed:
        write_atomic(sync_dir / "go", "abort")
        for proc, log in procs:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
        print("A worker failed before the send phase. Check the worker_*.log "
              f"files in {run_dir}.")
        sys.exit(1)

    write_atomic(sync_dir / "go", str(time.time() + 2.0))
    print("All workers connected. Sending...")

    for proc, log in procs:
        try:
            proc.wait(timeout=FINISH_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    # --------------------------------------------------------------- merge
    columns = {name: [] for name in COLUMNS}
    rows = 0
    for csv_path in sorted(run_dir.glob("performance_*.csv")):
        with csv_path.open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                rows += 1
                for name in COLUMNS:
                    value = row.get(name)
                    if value not in (None, ""):
                        columns[name].append(float(value))

    worker_status = []
    for json_path in sorted(run_dir.glob("performance_*.json")):
        report = json.loads(json_path.read_text(encoding="utf-8"))
        worker_status.append(report.get("status"))

    combined = {name: summarize(values) for name, values in columns.items()}
    lat = combined["latency_ms"]
    passed = (
        rows == expected
        and len(worker_status) == WORKERS
        and lat is not None
        and lat["p95"] <= TARGET_P95_MS
    )

    summary = {
        "workers": WORKERS,
        "users_per_worker": USERS_PER_WORKER,
        "expected_messages": expected,
        "received_messages": rows,
        "send_interval_seconds": float(SEND_INTERVAL),
        "target_p95_ms": TARGET_P95_MS,
        "combined_latency_ms": combined,
        "server_perf_stats": server_stats(),
        "status": "PASS" if passed else "FAIL",
    }
    (run_dir / "combined.json").write_text(
        json.dumps(summary, indent=4), encoding="utf-8"
    )

    print("\n" + "=" * 60)
    print(f"Messages: {rows}/{expected}   Workers reporting: {len(worker_status)}/{WORKERS}")
    print("Latency in ms (count / p50 / p95 / p99 / max):")
    labels = {
        "latency_ms": "end to end (original definition)",
        "entry_latency_ms": "end to end, stamped on client handler entry",
        "inbound_ms": "client send -> server handler start",
        "handler_ms": "server handler",
        "outbound_ms": "server emit -> client handler start",
    }
    for name in COLUMNS:
        stats = combined[name]
        if stats:
            print(f"  {labels[name]:<46} {stats['count']:>4} {stats['p50']:>8} "
                  f"{stats['p95']:>8} {stats['p99']:>8} {stats['max']:>8}")
    print(f"Server /perf-stats: {summary['server_perf_stats']}")
    print(f"RESULT: {summary['status']}   (saved {run_dir / 'combined.json'})")
    print("=" * 60)


if __name__ == "__main__":
    main()
