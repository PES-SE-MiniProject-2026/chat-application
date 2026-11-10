"""Tests for the shared SQLite connection used on the message send path."""
import importlib
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def appmod(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DEBUG", "1")
    monkeypatch.setenv("CHAT_MSG_RATE_LIMIT", "1000")
    monkeypatch.setenv("CHAT_UPLOAD_FOLDER", str(tmp_path / "uploads"))
    monkeypatch.delenv("CHAT_SHARED_DB", raising=False)
    sys.modules.pop("app", None)
    mod = importlib.import_module("app")
    mod.app.config["PREFERRED_URL_SCHEME"] = "https"
    mod.DATABASE = str(tmp_path / "test.db")
    mod.init_db()
    mod.message_rate_limiter.reset()
    yield mod
    mod.close_shared_db()


def add_user(mod, name):
    with mod.get_db() as db:
        return db.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, 'x')",
            (name, f"{name}@example.com"),
        ).lastrowid


def insert_message(db, sender, recipient, text):
    return db.execute(
        "INSERT INTO messages (sender_id, recipient_id, text, created_at)"
        " VALUES (?, ?, ?, '2026-01-01T00:00:00')",
        (sender, recipient, text),
    ).lastrowid


def test_concurrent_writes_all_persist_with_unique_ids(appmod):
    a, b = add_user(appmod, "alice"), add_user(appmod, "bob")
    ids, errors = [], []
    lock = threading.Lock()
    barrier = threading.Barrier(25)

    def worker(n):
        barrier.wait()
        for i in range(8):
            try:
                with appmod.shared_db() as db:
                    mid = insert_message(db, a, b, f"{n}-{i}")
                with lock:
                    ids.append(mid)
            except Exception as exc:  # pragma: no cover - failure path
                with lock:
                    errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(ids) == 200 and len(set(ids)) == 200
    with appmod.get_db() as db:  # a different connection sees the commits
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 200


def test_failed_write_rolls_back_and_connection_stays_usable(appmod):
    a, b = add_user(appmod, "alice"), add_user(appmod, "bob")
    with pytest.raises(Exception):
        with appmod.shared_db() as db:
            insert_message(db, a, b, "will be rolled back")
            raise RuntimeError("boom")
    with appmod.shared_db() as db:
        insert_message(db, a, b, "kept")
    with appmod.get_db() as db:
        texts = [r["text"] for r in db.execute("SELECT text FROM messages")]
    assert texts == ["kept"]


def test_shared_connection_follows_database_path(appmod, tmp_path):
    a, b = add_user(appmod, "alice"), add_user(appmod, "bob")
    with appmod.shared_db() as db:
        insert_message(db, a, b, "in first db")

    first_db = appmod.DATABASE
    appmod.DATABASE = str(tmp_path / "second.db")
    appmod.init_db()
    a2, b2 = add_user(appmod, "carol"), add_user(appmod, "dave")
    with appmod.shared_db() as db:
        insert_message(db, a2, b2, "in second db")

    with appmod.get_db() as db:
        assert [r["text"] for r in db.execute("SELECT text FROM messages")] == [
            "in second db"
        ]
    appmod.DATABASE = first_db
    with appmod.get_db() as db:
        assert [r["text"] for r in db.execute("SELECT text FROM messages")] == [
            "in first db"
        ]


def test_can_be_switched_off(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "SHARED_DB_ENABLED", False)
    a, b = add_user(appmod, "alice"), add_user(appmod, "bob")
    with appmod.shared_db() as db:
        insert_message(db, a, b, "plain get_db path")
    with appmod.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_socket_message_is_stored_and_delivered(appmod):
    alice, bob = add_user(appmod, "alice"), add_user(appmod, "bob")

    def client(user_id):
        http = appmod.app.test_client()
        with http.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["username"] = "u"
        return appmod.socketio.test_client(
            appmod.app, flask_test_client=http,
            auth={"token": appmod.issue_ws_token(user_id)},
        )

    ca, cb = client(alice), client(bob)
    ca.get_received()
    cb.get_received()

    ca.emit("chat message", {"recipient": "bob", "text": "hello bob"})

    assert any(r["name"] == "message sent" for r in ca.get_received())
    delivered = [r for r in cb.get_received() if r["name"] == "chat message"]
    assert delivered and delivered[0]["args"][0]["text"] == "hello bob"
    with appmod.get_db() as db:
        row = db.execute("SELECT text, sender_id, recipient_id FROM messages").fetchone()
    assert (row["text"], row["sender_id"], row["recipient_id"]) == ("hello bob", alice, bob)
