"""
Reliability test for CHAT-NF-003 (TC-Rel-01): auto-reconnect within 5 s and
99.5% availability.

Run from the project root while app.py is running:
    set CHAT_RATE_LIMITS=0 && python app.py          (server terminal)
    python tests/reliability_test.py                  (test terminal)

What it does
  * Registers N users and connects them to the server THROUGH a small TCP
    proxy that sits between the clients and the server.
  * Injects network interruptions by breaking the proxy:
        --fault cut        drop every connection and refuse new ones
        --fault blackhole  silently swallow traffic (like a dead Wi-Fi link);
                           the clients only notice via the ping timeout, so
                           use an --outage longer than PING_INTERVAL +
                           PING_TIMEOUT (10 s by default)
  * Restores the network and measures, per client, the time from "network is
    back" to "Socket.IO connected again". Pass criterion: every client <= 5 s.
  * While the clients are cut off, a control user sends direct messages to
    some of them; after reconnecting each must receive its message
    (offline delivery, CHAT-F-011), so recovery is checked for message loss.
  * Polls GET /health on the server every second for the whole run and reports
    availability against the 99.5% target.

The reconnect loop in this script mirrors the browser policy in index.html
(fresh /ws-token on every attempt, backoff 0.5 s -> 2 s, 3 s attempt timeout).
The browser itself records each outage in window.reconnectLog (milliseconds);
use that for a manual cross-check.

Results are written to tests/results/reliability_<run id>.json and .csv.
"""

import argparse
import csv
import json
import random
import socket
import statistics
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
import socketio
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PASSWORD = "Reliab!lity2026_Test"
RUN_ID = uuid.uuid4().hex[:8]
PREFIX = f"rl{RUN_ID}"

AVAILABILITY_TARGET = 99.5  # percent, CHAT-NF-003
RECONNECT_TARGET_MS = 5000  # CHAT-NF-003


# ---------------------------------------------------------------------------
# Fault-injecting TCP proxy
# ---------------------------------------------------------------------------
class FaultProxy:
    """Transparent TCP forwarder that can be switched to cut / blackhole."""

    def __init__(self, listen_port, target_host, target_port):
        self.listen_port = listen_port
        self.target = (target_host, target_port)
        self.mode = "pass"
        self._lock = threading.Lock()
        self._pairs = []   # (client_sock, upstream_sock)
        self._held = []    # sockets accepted while blackholed
        self._server = None

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", self.listen_port))
        self._server.listen(256)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        self.set_mode("cut")
        try:
            self._server.close()
        except OSError:
            pass

    def set_mode(self, mode):
        with self._lock:
            self.mode = mode
            if mode == "cut" or mode == "pass":
                # "cut": break everything now. "pass": discard leftovers from a
                # blackhole so no stream resumes half-way through a TLS record.
                victims = self._pairs + [(s, None) for s in self._held]
                self._pairs, self._held = [], []
            else:
                victims = []
        for a, b in victims:
            for s in (a, b):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def _accept_loop(self):
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        mode = self.mode
        if mode == "cut":
            client.close()
            return
        if mode == "blackhole":
            with self._lock:
                self._held.append(client)  # accepted, never answered
            return
        try:
            upstream = socket.create_connection(self.target, timeout=3)
            upstream.settimeout(None)
        except OSError:
            client.close()
            return
        with self._lock:
            self._pairs.append((client, upstream))
        threading.Thread(target=self._pump, args=(client, upstream), daemon=True).start()
        threading.Thread(target=self._pump, args=(upstream, client), daemon=True).start()

    def _pump(self, src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                if self.mode == "blackhole":
                    continue  # swallow
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Availability monitor (GET /health on the server, not through the proxy)
# ---------------------------------------------------------------------------
class AvailabilityMonitor(threading.Thread):
    def __init__(self, base_url, interval):
        super().__init__(daemon=True)
        self.base_url = base_url
        self.interval = interval
        self.samples = []  # (monotonic time, ok)
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            ok = False
            try:
                r = requests.get(f"{self.base_url}/health", timeout=2, verify=False)
                ok = r.status_code == 200
            except requests.RequestException:
                ok = False
            self.samples.append((time.monotonic(), ok))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        self.join(timeout=5)

    def summary(self):
        total = len(self.samples)
        up = sum(1 for _, ok in self.samples if ok)
        pct = 100.0 * up / total if total else 0.0
        window = (self.samples[-1][0] - self.samples[0][0]) if total > 1 else 0.0
        return {
            "samples": total,
            "samples_up": up,
            "availability_percent": round(pct, 3),
            "observed_downtime_s": round((total - up) * self.interval, 1),
            "window_s": round(window, 1),
            "allowed_downtime_s_for_window": round(window * (1 - AVAILABILITY_TARGET / 100), 1),
        }


# ---------------------------------------------------------------------------
# Test client with browser-like reconnect policy
# ---------------------------------------------------------------------------
class ChatTestClient:
    def __init__(self, username, cookie_header, base_url):
        self.username = username
        self.cookie = cookie_header
        self.base_url = base_url  # URL of the proxy
        self.connects = []        # monotonic time of every successful connect
        self.received = {}        # marker text -> monotonic time
        self.closing = False
        self._reconnecting = False
        self._lock = threading.Lock()
        self.sio = socketio.Client(reconnection=False, ssl_verify=False)
        self.sio.on("connect", self._on_connect)
        self.sio.on("disconnect", self._on_disconnect)
        self.sio.on("chat message", self._on_message)

    # -- events
    def _on_connect(self):
        self.connects.append(time.monotonic())

    def _on_disconnect(self, *_):
        if not self.closing:
            self.start_reconnect()

    def _on_message(self, payload):
        text = payload.get("text") if isinstance(payload, dict) else None
        if text:
            self.received.setdefault(text, time.monotonic())

    # -- connecting
    def _fetch_token(self):
        r = requests.get(
            f"{self.base_url}/ws-token",
            headers={"Cookie": self.cookie},
            timeout=3,
            verify=False,
        )
        if r.status_code != 200:
            raise RuntimeError(f"/ws-token returned {r.status_code}")
        return r.json()["token"]

    def connect_once(self):
        token = self._fetch_token()  # fresh token on every attempt
        self.sio.connect(
            self.base_url,
            headers={"Cookie": self.cookie},
            auth={"token": token},
            transports=["websocket"],
            wait_timeout=3,
        )

    def start_reconnect(self):
        with self._lock:
            if self._reconnecting:
                return
            self._reconnecting = True
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _reconnect_loop(self):
        delay = 0.5
        try:
            while not self.closing:
                try:
                    self.connect_once()
                    return
                except Exception:
                    try:
                        self.sio.disconnect()
                    except Exception:
                        pass
                    time.sleep(delay * random.uniform(0.7, 1.3))
                    delay = min(delay * 2, 2.0)
        finally:
            with self._lock:
                self._reconnecting = False

    def close(self):
        self.closing = True
        try:
            self.sio.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def register_and_login(base_url, username):
    s = requests.Session()
    s.verify = False
    s.post(
        f"{base_url}/register",
        data={"username": username, "email": f"{username}@reliability.local", "password": PASSWORD},
        timeout=30,
    )
    r = s.post(
        f"{base_url}/login",
        data={"username_or_email": username, "password": PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    cookies = s.cookies.get_dict()
    if not cookies:
        raise RuntimeError(f"Login failed for {username}")
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def wait_for(predicate, timeout, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return False


def server_healthy(base_url):
    try:
        return requests.get(f"{base_url}/health", timeout=1, verify=False).status_code == 200
    except requests.RequestException:
        return False


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100 * len(ordered) + 0.5)) - 1))
    return ordered[k]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="https://127.0.0.1:5000")
    ap.add_argument("--clients", type=int, default=10)
    ap.add_argument("--rounds", type=int, default=3, help="number of interruptions")
    ap.add_argument("--outage", type=float, default=8.0, help="seconds the network stays broken")
    ap.add_argument("--fault", choices=["cut", "blackhole"], default="cut")
    ap.add_argument("--proxy-port", type=int, default=5443)
    ap.add_argument("--monitor-interval", type=float, default=1.0)
    args = ap.parse_args()

    parsed = urlparse(args.base_url)
    host, port = parsed.hostname, parsed.port or 443
    proxy = FaultProxy(args.proxy_port, host, port)
    proxy_url = f"https://127.0.0.1:{args.proxy_port}"

    if not server_healthy(args.base_url):
        raise SystemExit(f"Server not healthy at {args.base_url}/health. Start app.py first.")

    print(f"Run {RUN_ID}: {args.clients} clients, {args.rounds} interruptions, "
          f"fault={args.fault}, outage {args.outage}s")

    proxy.start()

    usernames = [f"{PREFIX}_{i:02d}" for i in range(args.clients)]
    clients = []
    for name in usernames:
        cookie = register_and_login(args.base_url, name)
        clients.append(ChatTestClient(name, cookie, proxy_url))
    control_name = f"{PREFIX}_ctl"
    control_cookie = register_and_login(args.base_url, control_name)
    control = ChatTestClient(control_name, control_cookie, args.base_url)  # direct, never cut

    for c in clients:
        c.connect_once()
    control.connect_once()
    if not wait_for(lambda: all(len(c.connects) >= 1 for c in clients), 15):
        raise SystemExit("Not all clients could connect at start")

    monitor = AvailabilityMonitor(args.base_url, args.monitor_interval)
    monitor.start()

    rows, round_summaries = [], []

    try:
        for rnd in range(1, args.rounds + 1):
            time.sleep(2)
            connects_before = {c.username: len(c.connects) for c in clients}
            markers = {}

            # ---- break the network
            proxy.set_mode(args.fault)
            t_fault = time.monotonic()

            # messages sent to the cut-off users while they are away
            for i, c in enumerate(clients[:5]):
                text = f"r{rnd}-{RUN_ID}-{i}"
                markers[c.username] = text
                control.sio.emit("chat message", {"recipient": c.username, "text": text})
                time.sleep(0.2)

            time.sleep(max(0.0, args.outage - (time.monotonic() - t_fault)))
            t_restore = time.monotonic()
            proxy.set_mode("pass")

            # ---- wait for recovery
            wait_for(lambda: all(len(c.connects) > connects_before[c.username] for c in clients), 30)
            wait_for(lambda: all(m in clients[i].received for i, m in
                                 [(usernames.index(u), m) for u, m in markers.items()]), 10)

            reconnect_ms = []
            for c in clients:
                later = [t for t in c.connects if t >= t_restore]
                ms = round((later[0] - t_restore) * 1000) if later else None
                got = None
                if c.username in markers:
                    got = markers[c.username] in c.received
                rows.append({"round": rnd, "user": c.username, "reconnect_ms": ms, "missed_message_recovered": got})
                if ms is not None:
                    reconnect_ms.append(ms)

            failed = [r for r in rows if r["round"] == rnd and r["reconnect_ms"] is None]
            lost = [r for r in rows if r["round"] == rnd and r["missed_message_recovered"] is False]
            summary = {
                "round": rnd,
                "clients": len(clients),
                "reconnected": len(reconnect_ms),
                "max_ms": max(reconnect_ms) if reconnect_ms else None,
                "p95_ms": percentile(reconnect_ms, 95),
                "mean_ms": round(statistics.mean(reconnect_ms)) if reconnect_ms else None,
                "never_reconnected": len(failed),
                "messages_lost": len(lost),
            }
            round_summaries.append(summary)
            print(f"Round {rnd}: reconnected {summary['reconnected']}/{summary['clients']}, "
                  f"max {summary['max_ms']} ms, p95 {summary['p95_ms']} ms, "
                  f"messages lost {summary['messages_lost']}")
    finally:
        monitor.stop()
        for c in clients:
            c.close()
        control.close()
        proxy.stop()

    # ---- verdict
    all_ms = [r["reconnect_ms"] for r in rows if r["reconnect_ms"] is not None]
    never = sum(1 for r in rows if r["reconnect_ms"] is None)
    lost_total = sum(1 for r in rows if r["missed_message_recovered"] is False)
    availability = monitor.summary()

    reconnect_ok = bool(all_ms) and never == 0 and max(all_ms) <= RECONNECT_TARGET_MS
    availability_ok = availability["availability_percent"] >= AVAILABILITY_TARGET
    messages_ok = lost_total == 0

    result = {
        "run_id": RUN_ID,
        "config": {k: v for k, v in vars(args).items()},
        "reconnection": {
            "target_ms": RECONNECT_TARGET_MS,
            "samples": len(all_ms),
            "max_ms": max(all_ms) if all_ms else None,
            "p95_ms": percentile(all_ms, 95),
            "mean_ms": round(statistics.mean(all_ms)) if all_ms else None,
            "never_reconnected": never,
            "passed": reconnect_ok,
        },
        "message_recovery": {"messages_lost": lost_total, "passed": messages_ok},
        "availability": {**availability, "target_percent": AVAILABILITY_TARGET, "passed": availability_ok},
        "rounds": round_summaries,
    }

    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"reliability_{RUN_ID}.json").write_text(json.dumps(result, indent=2))
    with open(out_dir / f"reliability_{RUN_ID}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["round", "user", "reconnect_ms", "missed_message_recovered"])
        w.writeheader()
        w.writerows(rows)

    print("\n==== CHAT-NF-003 / TC-Rel-01 ====")
    print(f"Reconnect <= {RECONNECT_TARGET_MS} ms : {'PASS' if reconnect_ok else 'FAIL'} "
          f"(max {result['reconnection']['max_ms']} ms, p95 {result['reconnection']['p95_ms']} ms, "
          f"never reconnected: {never})")
    print(f"Messages recovered     : {'PASS' if messages_ok else 'FAIL'} (lost: {lost_total})")
    print(f"Availability >= {AVAILABILITY_TARGET}%  : {'PASS' if availability_ok else 'FAIL'} "
          f"({availability['availability_percent']}% over {availability['window_s']} s, "
          f"downtime {availability['observed_downtime_s']} s, "
          f"allowed {availability['allowed_downtime_s_for_window']} s)")
    print(f"Results: {out_dir / ('reliability_' + RUN_ID + '.json')}")
    raise SystemExit(0 if (reconnect_ok and messages_ok and availability_ok) else 1)


if __name__ == "__main__":
    main()
