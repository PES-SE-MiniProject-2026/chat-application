"""Tests for message-send rate limiting (#13) and account-status enforcement."""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def appmod(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DEBUG", "1")
    monkeypatch.setenv("CHAT_MSG_RATE_LIMIT", "3")
    monkeypatch.setenv("CHAT_MSG_RATE_WINDOW", "10")
    monkeypatch.setenv("CHAT_UPLOAD_FOLDER", str(tmp_path / "uploads"))
    sys.modules.pop("app", None)
    mod = importlib.import_module("app")
    mod.app.config["PREFERRED_URL_SCHEME"] = "https"  # connect() requires TLS
    mod.DATABASE = str(tmp_path / "test.db")
    mod.init_db()
    mod.message_rate_limiter.reset()
    return mod


def add_user(mod, name, status="active"):
    with mod.get_db() as db:
        cur = db.execute(
            "INSERT INTO users (username, email, password_hash, account_status)"
            " VALUES (?, ?, 'x', ?)",
            (name, f"{name}@example.com", status),
        )
        return cur.lastrowid


def socket_client(mod, user_id):
    http = mod.app.test_client()
    with http.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["username"] = "u"
    return mod.socketio.test_client(
        mod.app, flask_test_client=http,
        auth={"token": mod.issue_ws_token(user_id)},
    )


# ---------------------------------------------------------------- limiter unit
def test_limiter_allows_up_to_limit_then_blocks():
    clock = FakeClock()
    import app as mod
    lim = mod.SlidingWindowRateLimiter(3, 10, clock=clock)
    assert [lim.check(1)[0] for _ in range(3)] == [True, True, True]
    allowed, retry = lim.check(1)
    assert not allowed and 0 < retry <= 10


def test_limiter_recovers_after_window():
    clock = FakeClock()
    import app as mod
    lim = mod.SlidingWindowRateLimiter(2, 10, clock=clock)
    lim.check(1); lim.check(1)
    assert not lim.check(1)[0]
    clock.now += 10.1
    assert lim.check(1)[0]


def test_limiter_rejected_attempts_do_not_extend_lockout():
    clock = FakeClock()
    import app as mod
    lim = mod.SlidingWindowRateLimiter(1, 10, clock=clock)
    lim.check(1)
    for _ in range(50):
        assert not lim.check(1)[0]
    clock.now += 10.1
    assert lim.check(1)[0]


def test_limiter_is_per_user():
    clock = FakeClock()
    import app as mod
    lim = mod.SlidingWindowRateLimiter(1, 10, clock=clock)
    assert lim.check(1)[0]
    assert lim.check(2)[0]
    assert not lim.check(1)[0]


# ------------------------------------------------------------- socket events
def test_direct_messages_are_rate_limited(appmod):
    alice = add_user(appmod, "alice")
    add_user(appmod, "bob")
    client = socket_client(appmod, alice)
    assert client.is_connected()
    client.get_received()

    for i in range(3):
        client.emit("chat message", {"recipient": "bob", "text": f"hi {i}"})
    received = client.get_received()
    assert sum(1 for r in received if r["name"] == "message sent") == 3

    client.emit("chat message", {"recipient": "bob", "text": "too many"})
    errors = [r for r in client.get_received() if r["name"] == "chat error"]
    assert errors and "too quickly" in errors[0]["args"][0]["message"]
    assert errors[0]["args"][0]["retry_after"] >= 1

    with appmod.get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 3  # the blocked message was never stored


def test_limit_is_shared_across_a_users_connections(appmod):
    alice = add_user(appmod, "alice")
    add_user(appmod, "bob")
    c1, c2 = socket_client(appmod, alice), socket_client(appmod, alice)
    for c in (c1, c2):
        c.get_received()
    for i in range(3):
        c1.emit("chat message", {"recipient": "bob", "text": str(i)})
    c2.emit("chat message", {"recipient": "bob", "text": "from tab 2"})
    errors = [r for r in c2.get_received() if r["name"] == "chat error"]
    assert errors and "too quickly" in errors[0]["args"][0]["message"]


def test_other_users_unaffected(appmod):
    alice = add_user(appmod, "alice")
    bob = add_user(appmod, "bob")
    ca, cb = socket_client(appmod, alice), socket_client(appmod, bob)
    ca.get_received(); cb.get_received()
    for i in range(4):
        ca.emit("chat message", {"recipient": "bob", "text": str(i)})
    cb.emit("chat message", {"recipient": "alice", "text": "still fine"})
    names = [r["name"] for r in cb.get_received()]
    assert "message sent" in names
    assert not any(
        r["name"] == "chat error" for r in cb.get_received()
    )


def test_group_messages_are_rate_limited(appmod):
    alice = add_user(appmod, "alice")
    client = socket_client(appmod, alice)
    client.get_received()
    for _ in range(3):
        client.emit("group message", {"group_id": 999, "text": "x"})
    client.get_received()
    client.emit("group message", {"group_id": 999, "text": "x"})
    errors = [r for r in client.get_received() if r["name"] == "chat error"]
    assert errors and "too quickly" in errors[0]["args"][0]["message"]


def test_rate_limit_can_be_disabled_for_load_tests(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "RATE_LIMITS_ENABLED", False)
    assert all(appmod.message_rate_limit_wait(1) is None for _ in range(100))


# --------------------------------------------------- account status over HTTP
def test_suspended_user_loses_http_access(appmod):
    uid = add_user(appmod, "carol")
    http = appmod.app.test_client()
    with http.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = "carol"

    assert http.get("/ws-token", base_url="https://localhost").status_code == 200

    with appmod.get_db() as db:
        db.execute("UPDATE users SET account_status='suspended' WHERE id=?", (uid,))

    resp = http.get("/ws-token", base_url="https://localhost")
    assert resp.status_code == 403
    assert resp.get_json()["error"]
    with http.session_transaction() as sess:
        assert "user_id" not in sess


def test_suspended_user_page_request_redirects_to_login(appmod):
    uid = add_user(appmod, "dave", status="banned")
    http = appmod.app.test_client()
    with http.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = "dave"
    resp = http.get("/", base_url="https://localhost")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_suspended_login_shows_page_not_json(appmod):
    from werkzeug.security import generate_password_hash
    uid = add_user(appmod, "erin", status="suspended")
    with appmod.get_db() as db:
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash("correct horse battery"), uid))
    http = appmod.app.test_client()
    resp = http.post(
        "/login", base_url="https://localhost",
        data={"username_or_email": "erin", "password": "correct horse battery"},
    )
    assert resp.status_code == 403
    assert resp.mimetype == "text/html"
    assert b"suspended or banned" in resp.data


def test_upload_is_rate_limited(appmod):
    uid = add_user(appmod, "frank")
    add_user(appmod, "gina")
    http = appmod.app.test_client()
    with http.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = "frank"
    for _ in range(3):
        appmod.message_rate_limit_wait(uid)  # use up the allowance
    resp = http.post("/upload", base_url="https://localhost",
                     data={"recipient": "gina"})
    assert resp.status_code == 429
    assert resp.get_json()["retry_after"] >= 1
    assert int(resp.headers["Retry-After"]) >= 1
