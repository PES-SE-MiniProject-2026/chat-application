"""
100-user WebSocket and message-delivery performance test.

Run from the project root while app.py is running:
    python tests/performance_test.py

Start the local test server with (PowerShell):
    $env:CHAT_PERFORMANCE_TEST="1"; python app.py

To take the load generator itself out of the measurement, run it as several
processes with:  python tests/run_split_perf.py

Each message carries a wall-clock "sent_at"; the server (in performance mode)
returns its own timestamps, so every latency is split into:
    inbound_ms   client send   -> server handler start
    handler_ms   handler start -> server queues the delivery
    outbound_ms  server queues -> client handler start

The test uses real registration, login, /ws-token, Socket.IO authentication,
and the application's "chat message" event.
Delivery is counted only when the recipient receives the message.
"""

import csv
import json
import os
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests
import socketio
import urllib3

BASE_URL = os.environ.get("PERF_BASE_URL", "https://127.0.0.1:5000")

# Defaults reproduce the original test (100 users, 50 pairs, 250 messages).
# The PERF_* variables exist so run_split_perf.py can run several copies.
TOTAL_USERS = int(os.environ.get("PERF_USERS", "100"))
MESSAGES_PER_PAIR = int(os.environ.get("PERF_MSGS", "5"))
PASSWORD = "LoadTest!2026_Secure"

CONNECTION_WORKERS = 10
CONNECTION_ATTEMPTS = 5
HTTP_ATTEMPTS = 3

MESSAGE_TIMEOUT = 60
TARGET_P95_MS = 200
SEND_INTERVAL_SECONDS = float(os.environ.get("PERF_INTERVAL", "0.15"))

# Set by run_split_perf.py: workers wait here until every worker is connected.
SYNC_DIR = os.environ.get("PERF_SYNC_DIR")
RESULT_DIR = Path(os.environ.get("PERF_RESULT_DIR", str(Path("tests") / "results")))

if TOTAL_USERS < 2 or TOTAL_USERS % 2:
    raise SystemExit("PERF_USERS must be an even number (users are paired).")

RUN_ID = uuid.uuid4().hex[:8]
PREFIX = f"lt{RUN_ID}"

urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)

all_clients = []
sessions = []

clients_by_username = {}

results = []
chat_errors = []

connection_failures = []
connection_attempt_errors = []

send_times = {}
received_messages = set()

lock = threading.Lock()


# ---------------------------------------------------------
# REGISTER ACCOUNT
# ---------------------------------------------------------

def register_account(index):

    username = f"{PREFIX}_{index:03d}"
    email = f"{username}@loadtest.local"

    with requests.Session() as session:

        session.verify = False

        response = session.post(
            f"{BASE_URL}/register",
            data={
                "username": username,
                "email": email,
                "password": PASSWORD
            },
            timeout=60,
            allow_redirects=True
        )

        response.raise_for_status()

        if urlparse(response.url).path.rstrip("/") != "/login":

            raise RuntimeError(
                f"Registration rejected for {username}; "
                f"final URL: {response.url}"
            )

    return username


# ---------------------------------------------------------
# AUTHENTICATE ACCOUNT
# Sequential authentication with network retries
# ---------------------------------------------------------

def authenticate_account(username):

    last_error = None

    for attempt in range(1, HTTP_ATTEMPTS + 1):

        session = requests.Session()
        session.verify = False

        try:

            response = session.post(
                f"{BASE_URL}/login",
                data={
                    "username_or_email": username,
                    "password": PASSWORD
                },
                timeout=60,
                allow_redirects=True
            )

            response.raise_for_status()

            if urlparse(response.url).path.rstrip("/") not in ("", "/"):

                raise RuntimeError(
                    f"Login failed for {username}; "
                    f"final URL: {response.url}"
                )

            cookies = session.cookies.get_dict()

            if not cookies:

                raise RuntimeError(
                    f"No session cookie issued for {username}"
                )

            cookie_header = "; ".join(
                f"{key}={value}"
                for key, value in cookies.items()
            )

            return {
                "username": username,
                "session": session,
                "cookie": cookie_header
            }

        except requests.exceptions.ConnectionError as exc:

            last_error = (
                f"{type(exc).__name__}: {exc}"
            )

            session.close()

            if attempt < HTTP_ATTEMPTS:

                wait_time = min(2 ** (attempt - 1), 5)

                print(
                    f"Login connection failed for {username}. "
                    f"Retrying in {wait_time} seconds "
                    f"({attempt}/{HTTP_ATTEMPTS})..."
                )

                time.sleep(wait_time)

        except Exception:

            session.close()
            raise

    raise RuntimeError(
        f"Authentication failed for {username} "
        f"after {HTTP_ATTEMPTS} attempts: {last_error}"
    )


# ---------------------------------------------------------
# CONNECT WEBSOCKET CLIENT
# ---------------------------------------------------------

def connect_client(account):

    username = account["username"]
    session = account["session"]

    last_error = None

    for attempt in range(1, CONNECTION_ATTEMPTS + 1):

        client = socketio.Client(
            reconnection=False,
            ssl_verify=False,
            logger=False,
            engineio_logger=False
        )

        @client.on("chat message")
        def handle_received_message(message):

            # Taken before the lock so waiting for the lock is not counted.
            received_perf = time.perf_counter()
            received_wall = time.time()

            marker = (
                message.get("text", "")
                if isinstance(message, dict)
                else ""
            )

            with lock:

                if (
                    marker in send_times
                    and marker not in received_messages
                ):

                    latency = (
                        time.perf_counter() - send_times[marker]
                    ) * 1000

                    row = {
                        "message": marker,
                        # Same definition as before (stamped after the lock).
                        "latency_ms": round(latency, 3),
                        # Stamped on handler entry, before any lock wait.
                        "entry_latency_ms": round(
                            (received_perf - send_times[marker]) * 1000, 3
                        ),
                        "inbound_ms": None,
                        "handler_ms": None,
                        "outbound_ms": None,
                    }

                    stamps = (
                        message.get("_perf")
                        if isinstance(message, dict)
                        else None
                    )

                    if isinstance(stamps, dict):
                        try:
                            row["inbound_ms"] = round((stamps["server_in"] - stamps["sent_at"]) * 1000, 3)
                            row["handler_ms"] = round((stamps["server_emit"] - stamps["server_in"]) * 1000, 3)
                            row["outbound_ms"] = round((received_wall - stamps["server_emit"]) * 1000, 3)
                        except (KeyError, TypeError):
                            pass

                    results.append(row)

                    received_messages.add(marker)

            # Acknowledge actual message receipt.
            return {"received": True}

        @client.on("chat error")
        def handle_chat_error(data):

            with lock:

                chat_errors.append({
                    "username": username,
                    "error": str(data)
                })

        try:

            # Obtain a fresh WebSocket token for every attempt.
            token_response = session.get(
                f"{BASE_URL}/ws-token",
                timeout=30
            )

            token_response.raise_for_status()

            token = token_response.json().get("token")

            if not token:

                raise RuntimeError(
                    "/ws-token returned no token"
                )

            client.connect(
                BASE_URL,
                headers={
                    "Cookie": account["cookie"]
                },
                auth={
                    "token": token
                },
                transports=["websocket"],
                wait_timeout=30
            )

            if not client.connected:

                raise RuntimeError(
                    "Socket.IO client did not enter connected state"
                )

            with lock:

                all_clients.append(client)

                clients_by_username[username] = client

                if attempt > 1:

                    connection_attempt_errors.append({
                        "username": username,
                        "recovered_on_attempt": attempt
                    })

            return username

        except Exception as exc:

            last_error = (
                f"{type(exc).__name__}: {exc}"
            )

            try:
                client.disconnect()
            except Exception:
                pass

            if attempt < CONNECTION_ATTEMPTS:

                wait_time = min(
                    0.5 * (2 ** (attempt - 1)),
                    4
                )

                time.sleep(wait_time)

    raise RuntimeError(
        f"{username} failed after "
        f"{CONNECTION_ATTEMPTS} attempts: {last_error}"
    )


# ---------------------------------------------------------
# SEND MESSAGES
# ---------------------------------------------------------

def send_messages(pair_index, usernames):

    sender_username = usernames[pair_index * 2]

    recipient_username = usernames[pair_index * 2 + 1]

    sender = clients_by_username[sender_username]

    for message_index in range(MESSAGES_PER_PAIR):

        marker = (
            f"PERF_{RUN_ID}_{pair_index}_{message_index}"
        )

        with lock:

            send_times[marker] = time.perf_counter()

        sender.emit(
            "chat message",
            {
                "recipient": recipient_username,
                "text": marker,
                "sent_at": time.time()
            }
        )

        time.sleep(SEND_INTERVAL_SECONDS)


# ---------------------------------------------------------
# PERCENTILE CALCULATION
# ---------------------------------------------------------

def percentile(values, percent):

    if not values:
        return None

    ordered = sorted(values)

    position = (
        (len(ordered) - 1) * percent / 100
    )

    lower = int(position)

    upper = min(
        lower + 1,
        len(ordered) - 1
    )

    fraction = position - lower

    return (
        ordered[lower] * (1 - fraction)
        + ordered[upper] * fraction
    )


def summarize(values):

    values = [v for v in values if v is not None]

    if not values:
        return None

    return {
        "count": len(values),
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "p99": round(percentile(values, 99), 3),
        "max": round(max(values), 3)
    }


# ---------------------------------------------------------
# START BARRIER (only used by run_split_perf.py)
# ---------------------------------------------------------

def wait_for_go_signal(timeout=600):

    sync_dir = Path(SYNC_DIR)
    sync_dir.mkdir(parents=True, exist_ok=True)

    (sync_dir / f"ready_{RUN_ID}").write_text("ready", encoding="utf-8")

    go_file = sync_dir / "go"
    deadline = time.monotonic() + timeout

    print("Connected; waiting for the other workers...")

    while time.monotonic() < deadline:

        if go_file.exists():

            content = go_file.read_text(encoding="utf-8").strip()

            if content == "abort":
                raise RuntimeError("Another worker failed; run aborted.")

            try:
                start_at = float(content)
            except ValueError:
                time.sleep(0.05)
                continue

            time.sleep(max(0.0, start_at - time.time()))
            return

        time.sleep(0.05)

    raise RuntimeError("Timed out waiting for the go signal.")


# ---------------------------------------------------------
# MAIN TEST
# ---------------------------------------------------------

def main():

    start_time = time.perf_counter()

    print("=" * 60)
    print("CHAT APPLICATION - PERFORMANCE TEST")
    print("=" * 60)

    print(f"Server: {BASE_URL}")
    print(f"Concurrent users: {TOTAL_USERS}")
    print(f"Messages per pair: {MESSAGES_PER_PAIR}")
    print(f"Connection attempts per client: {CONNECTION_ATTEMPTS}")
    print(f"HTTP authentication attempts: {HTTP_ATTEMPTS}")
    print(f"Test run ID: {RUN_ID}")

    # -----------------------------------------------------
    # STEP 1: REGISTER
    # -----------------------------------------------------

    print("\n[1/4] Registering test accounts...")

    usernames = []

    for index in range(TOTAL_USERS):

        username = register_account(index)

        usernames.append(username)

        if (index + 1) % 10 == 0:

            print(
                f"Registered: {index + 1}/{TOTAL_USERS}"
            )

    usernames.sort()

    print(
        f"Successfully registered: "
        f"{len(usernames)}/{TOTAL_USERS}"
    )

    if len(usernames) != TOTAL_USERS:

        raise RuntimeError(
            "Not all test accounts were registered."
        )

    # -----------------------------------------------------
    # STEP 2: AUTHENTICATE SEQUENTIALLY
    # -----------------------------------------------------

    print("\n[2/4] Authenticating test accounts...")

    accounts = {}

    for index, username in enumerate(usernames, start=1):

        account = authenticate_account(username)

        accounts[username] = account

        sessions.append(account["session"])

        if index % 10 == 0:

            print(
                f"Authenticated: {index}/{TOTAL_USERS}"
            )

    print(
        f"Successfully authenticated: "
        f"{len(accounts)}/{TOTAL_USERS}"
    )

    if len(accounts) != TOTAL_USERS:

        raise RuntimeError(
            "Authentication incomplete."
        )

    # -----------------------------------------------------
    # STEP 3: CONNECT 100 WEBSOCKET CLIENTS
    # -----------------------------------------------------

    print(
        f"\n[3/4] Connecting all {TOTAL_USERS} WebSocket clients..."
    )

    with ThreadPoolExecutor(
        max_workers=CONNECTION_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                connect_client,
                accounts[username]
            ): username
            for username in usernames
        }

        completed = 0

        for future in as_completed(futures):

            username = futures[future]

            completed += 1

            try:

                future.result()

            except Exception as exc:

                connection_failures.append({
                    "username": username,
                    "error": str(exc)
                })

            if completed % 10 == 0:

                print(
                    f"Connection attempts completed: "
                    f"{completed}/{TOTAL_USERS}"
                )

    connected = len(all_clients)

    print(
        f"Connected clients: {connected}/{TOTAL_USERS}"
    )

    print(
        f"Final connection failures: "
        f"{len(connection_failures)}"
    )

    print(
        f"Transient failures recovered by retry: "
        f"{len(connection_attempt_errors)}"
    )

    if connected != TOTAL_USERS:

        for failure in connection_failures:

            print(
                f"{failure['username']}: "
                f"{failure['error']}"
            )

        raise RuntimeError(
            f"{TOTAL_USERS} WebSocket clients could not be established "
            "after retries; message test not run."
        )

    if SYNC_DIR:

        wait_for_go_signal()

    # -----------------------------------------------------
    # STEP 4: SEND 250 MESSAGES
    # -----------------------------------------------------

    print(
        f"\n[4/4] Sending messages between {TOTAL_USERS // 2} user pairs..."
    )

    expected_messages = (
        (TOTAL_USERS // 2) * MESSAGES_PER_PAIR
    )

    with ThreadPoolExecutor(
        max_workers=TOTAL_USERS // 2
    ) as executor:

        futures = [

            executor.submit(
                send_messages,
                pair_index,
                usernames
            )

            for pair_index in range(TOTAL_USERS // 2)
        ]

        for future in as_completed(futures):

            future.result()

    print(
        f"Expected messages: {expected_messages}"
    )

    # Wait for actual recipient acknowledgements.
    deadline = time.monotonic() + MESSAGE_TIMEOUT

    while time.monotonic() < deadline:

        with lock:

            received_count = len(results)

        if received_count >= expected_messages:
            break

        time.sleep(0.1)

    # -----------------------------------------------------
    # CALCULATE RESULTS
    # -----------------------------------------------------

    with lock:

        latency_values = [
            row["latency_ms"]
            for row in results
        ]

        result_rows = list(results)

        errors_snapshot = list(chat_errors)

    received_count = len(latency_values)

    lost_count = (
        expected_messages - received_count
    )

    p95 = percentile(
        latency_values,
        95
    )

    p99 = percentile(
        latency_values,
        99
    )

    elapsed = (
        time.perf_counter() - start_time
    )

    passed = (
        connected == TOTAL_USERS
        and received_count == expected_messages
        and p95 is not None
        and p95 <= TARGET_P95_MS
        and not errors_snapshot
    )

    # -----------------------------------------------------
    # CREATE REPORT
    # -----------------------------------------------------

    report = {

        "run_id": RUN_ID,

        "server": BASE_URL,

        "total_users": TOTAL_USERS,

        "connected_clients": connected,

        "connection_failures": len(connection_failures),

        "transient_connection_failures_recovered":
            len(connection_attempt_errors),

        "expected_messages": expected_messages,

        "received_messages": received_count,

        "lost_messages": lost_count,

        "delivery_rate_percent": round(
            received_count / expected_messages * 100,
            2
        ),

        "test_duration_seconds": round(
            elapsed,
            3
        ),

        "average_latency_ms": round(
            statistics.mean(latency_values),
            3
        ) if latency_values else None,

        "median_latency_ms": round(
            statistics.median(latency_values),
            3
        ) if latency_values else None,

        "p95_latency_ms": round(
            p95,
            3
        ) if p95 is not None else None,

        "p99_latency_ms": round(
            p99,
            3
        ) if p99 is not None else None,

        "maximum_latency_ms": round(
            max(latency_values),
            3
        ) if latency_values else None,

        "latency_breakdown_ms": {
            "client_to_client_stamped_on_entry": summarize(
                [row["entry_latency_ms"] for row in result_rows]
            ),
            "inbound": summarize(
                [row["inbound_ms"] for row in result_rows]
            ),
            "handler": summarize(
                [row["handler_ms"] for row in result_rows]
            ),
            "outbound": summarize(
                [row["outbound_ms"] for row in result_rows]
            ),
        },

        "target_p95_ms": TARGET_P95_MS,

        "chat_errors": errors_snapshot,

        "connection_retry_details":
            connection_attempt_errors,

        "status": "PASS" if passed else "FAIL"
    }

    # -----------------------------------------------------
    # SAVE JSON AND CSV
    # -----------------------------------------------------

    output_dir = RESULT_DIR

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    json_path = (
        output_dir /
        f"performance_{RUN_ID}.json"
    )

    csv_path = (
        output_dir /
        f"performance_{RUN_ID}.csv"
    )

    json_path.write_text(
        json.dumps(report, indent=4),
        encoding="utf-8"
    )

    with csv_path.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=[
                "message",
                "latency_ms",
                "entry_latency_ms",
                "inbound_ms",
                "handler_ms",
                "outbound_ms"
            ]
        )

        writer.writeheader()

        writer.writerows(result_rows)

    # -----------------------------------------------------
    # DISPLAY FINAL RESULTS
    # -----------------------------------------------------

    print("\n" + "=" * 60)

    print("PERFORMANCE TEST RESULTS")

    print("=" * 60)

    for key, value in report.items():

        if key not in (
            "chat_errors",
            "connection_retry_details",
            "latency_breakdown_ms"
        ):

            print(
                f"{key}: {value}"
            )

    print("\nLatency breakdown (ms):")

    for name, stats in report["latency_breakdown_ms"].items():

        print(f"  {name}: {stats}")

    if errors_snapshot:

        print("\nChat errors:")

        for item in errors_snapshot:

            print(item)

    print("\nSaved files:")

    print(json_path)

    print(csv_path)

    print("=" * 60)

    print(
        f"RESULT: {report['status']}"
    )

    print("=" * 60)


# ---------------------------------------------------------
# EXECUTION AND CLEANUP
# ---------------------------------------------------------

if __name__ == "__main__":

    try:

        main()

    except Exception as exc:

        print("\nPERFORMANCE TEST ERROR:")

        print(
            f"{type(exc).__name__}: {exc}"
        )

    finally:

        print("\nCleaning up connections...")

        for client in all_clients:

            try:

                if client.connected:
                    client.disconnect()

            except Exception:
                pass

        for session in sessions:

            try:
                session.close()

            except Exception:
                pass

        print("Cleanup completed.")