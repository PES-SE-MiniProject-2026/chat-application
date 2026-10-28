import pytest
import app as chat

SECURE = "https://localhost"
PASSWORD = "a-long-test-password"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "DATABASE", str(tmp_path / "test.db"))
    # Flask's test client builds the Socket.IO handshake environ, and uses this
    # setting for the URL scheme, so the handshake looks like HTTPS.
    monkeypatch.setitem(chat.app.config, "PREFERRED_URL_SCHEME", "https")
    chat.init_db()
    chat.limiter.enabled = False
    chat.app.config["TESTING"] = True

    c = chat.app.test_client()
    c.post("/register", base_url=SECURE, data={
        "username": "alice", "email": "alice@example.com", "password": PASSWORD})
    c.post("/login", base_url=SECURE, data={
        "username_or_email": "alice", "password": PASSWORD})
    return c


def connect(flask_client, auth):
    return chat.socketio.test_client(chat.app, flask_test_client=flask_client, auth=auth)


def test_valid_token_connects(client):
    assert connect(client, {"token": chat.issue_ws_token(1)}).is_connected()

def test_missing_token_rejected(client):
    assert not connect(client, {}).is_connected()

def test_no_auth_payload_rejected(client):
    assert not connect(client, None).is_connected()

def test_garbage_token_rejected(client):
    assert not connect(client, {"token": "abc"}).is_connected()

def test_non_string_token_rejected(client):
    assert not connect(client, {"token": 12345}).is_connected()

def test_tampered_token_rejected(client):
    assert not connect(client, {"token": chat.issue_ws_token(1)[:-2] + "xx"}).is_connected()

def test_expired_token_rejected(client, monkeypatch):
    tok = chat.issue_ws_token(1)
    monkeypatch.setattr(chat, "WS_TOKEN_MAX_AGE", -1)
    assert not connect(client, {"token": tok}).is_connected()

def test_token_for_another_user_rejected(client):
    assert not connect(client, {"token": chat.issue_ws_token(999)}).is_connected()

def test_token_without_login_session_rejected(client):
    anonymous = chat.app.test_client()
    assert not connect(anonymous, {"token": chat.issue_ws_token(1)}).is_connected()

def test_ws_token_endpoint_requires_login():
    r = chat.app.test_client().get("/ws-token", base_url=SECURE)
    assert r.status_code == 401

def test_ws_token_endpoint_returns_token_when_logged_in(client):
    r = client.get("/ws-token", base_url=SECURE)
    assert r.status_code == 200
    assert chat.verify_ws_token(r.get_json()["token"]) == 1
    assert r.headers["Cache-Control"] == "no-store"