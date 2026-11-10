"""
Standalone SQLite write benchmark (does not import or touch app.py or users.db).

It repeats what handle_chat_message does for each message (open a new
connection, three PRAGMAs, INSERT, commit, close) and times it in two places:

  1. HERE  - a folder inside the project (on your machine: inside OneDrive)
  2. TEMP  - your system temp folder (normally not synced)

and three ways of writing:

  per-message connection, one thread at a time   (what the app does)
  per-message connection, 25 threads at once      (the load test's burst)
  one shared connection behind a lock             (possible fix)

Run from the project root:
    python tests/db_bench.py
Takes about 30 seconds and deletes its own files afterwards.
"""

import shutil
import sqlite3
import statistics
import tempfile
import threading
import time
from pathlib import Path

INSERTS = 200
THREADS = 25


def percentile(values, percent):
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def setup(folder):
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    path = folder / "bench.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "sender_id INTEGER, recipient_id INTEGER, text TEXT, created_at TEXT, "
        "delivered_at TEXT)"
    )
    connection.commit()
    connection.close()
    return str(path)


def insert_like_app(path):
    # Same sequence as get_db() + the INSERT in handle_chat_message.
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA synchronous = NORMAL")
    try:
        cursor = connection.execute(
            "INSERT INTO messages (sender_id, recipient_id, text, created_at) "
            "VALUES (?, ?, ?, ?)",
            (1, 2, "benchmark", "2026-01-01T00:00:00"),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def timed(function, *args):
    start = time.perf_counter()
    function(*args)
    return (time.perf_counter() - start) * 1000


def sequential(path):
    return [timed(insert_like_app, path) for _ in range(INSERTS)]


def concurrent(path):
    times = []
    lock = threading.Lock()
    barrier = threading.Barrier(THREADS)

    def worker():
        barrier.wait()
        for _ in range(INSERTS // THREADS):
            elapsed = timed(insert_like_app, path)
            with lock:
                times.append(elapsed)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return times


def shared_connection(path):
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.execute("PRAGMA synchronous = NORMAL")
    write_lock = threading.Lock()
    times = []
    times_lock = threading.Lock()
    barrier = threading.Barrier(THREADS)

    def insert():
        with write_lock:
            connection.execute(
                "INSERT INTO messages (sender_id, recipient_id, text, created_at) "
                "VALUES (?, ?, ?, ?)",
                (1, 2, "benchmark", "2026-01-01T00:00:00"),
            )
            connection.commit()

    def worker():
        barrier.wait()
        for _ in range(INSERTS // THREADS):
            elapsed = timed(insert)
            with times_lock:
                times.append(elapsed)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    connection.close()
    return times


def report(label, times):
    print(
        f"  {label:<46} median {statistics.median(times):7.1f}   "
        f"p95 {percentile(times, 95):7.1f}   max {max(times):7.1f}  ms"
    )


def main():
    locations = {
        "HERE (project folder)": Path("bench_db_here").resolve(),
        "TEMP (system temp)": Path(tempfile.gettempdir()) / "bench_db_temp",
    }

    for name, folder in locations.items():
        print(f"\n{name}: {folder}")

        path = setup(folder)
        report("per-message connection, 1 thread", sequential(path))

        path = setup(folder)
        report(f"per-message connection, {THREADS} threads at once", concurrent(path))

        path = setup(folder)
        report(f"shared connection + lock, {THREADS} threads at once", shared_connection(path))

        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    main()
