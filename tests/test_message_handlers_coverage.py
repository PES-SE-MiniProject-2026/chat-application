
import pytest
import app as chat
from datetime import datetime, timedelta, timezone

SECURE = "https://localhost"
PASSWORD = "A-Strong-Test-Password-123!"


@pytest.fixture
def message_app(tmp_path, monkeypatch):
    db_file = tmp_path / "message_handlers_test.db"
    monkeypatch.setattr(chat, "DATABASE", str(db_file))
    monkeypatch.setitem(chat.app.config, "PREFERRED_URL_SCHEME", "https")

    chat.init_db()
    chat.limiter.enabled = False
    chat.app.config["TESTING"] = True
    chat.connected_users.clear()
    chat.sid_to_user.clear()

    with chat.user_cache_lock:
        chat.users_by_username.clear()
        chat.usernames_by_id.clear()

    chat.message_rate_limiter.reset()
    return chat


def create_user(module, username, email):
    client = module.app.test_client()

    client.post("/register", base_url=SECURE, data={
        "username": username,
        "email": email,
        "password": PASSWORD,
    })
    client.post("/login", base_url=SECURE, data={
        "username_or_email": username,
        "password": PASSWORD,
    })

    with module.get_db() as db:
        user = db.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()

    return client, user["id"]


def connect_socket(module, client, user_id):
    token = module.issue_ws_token(user_id)
    return module.socketio.test_client(
        module.app,
        flask_test_client=client,
        auth={"token": token},
    )


def get_events(received, name):
    return [
        event.get("args", [None])[0]
        for event in received
        if event.get("name") == name
    ]


def insert_direct_message(module, sender_id, recipient_id, age_minutes=1,
                          deleted_at=None):
    created_at = (
        datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    ).isoformat()

    with module.get_db() as db:
        cursor = db.execute(
            """INSERT INTO messages
               (sender_id, recipient_id, text, created_at, deleted_at)
               VALUES (?, ?, ?, ?, ?)""",
            (sender_id, recipient_id, "Test direct message",
             created_at, deleted_at),
        )
        return cursor.lastrowid


def insert_group(module, creator_id, member_id):
    with module.get_db() as db:
        cursor = db.execute(
            """INSERT INTO groups (name, created_by, created_at)
               VALUES (?, ?, ?)""",
            ("Test Group", creator_id, module.utc_now()),
        )
        group_id = cursor.lastrowid

        for user_id in (creator_id, member_id):
            db.execute(
                """INSERT INTO group_members
                   (group_id, user_id, joined_at) VALUES (?, ?, ?)""",
                (group_id, user_id, module.utc_now()),
            )

    return group_id


def insert_group_message(module, group_id, sender_id, age_minutes=1,
                         deleted_at=None):
    created_at = (
        datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    ).isoformat()

    with module.get_db() as db:
        cursor = db.execute(
            """INSERT INTO group_messages
               (group_id, sender_id, text, created_at, deleted_at)
               VALUES (?, ?, ?, ?, ?)""",
            (group_id, sender_id, "Test group message",
             created_at, deleted_at),
        )
        return cursor.lastrowid


# Direct chat history

def test_load_chat_history_returns_messages(message_app):
    alice, alice_id = create_user(
        message_app, "alice", "alice@example.com"
    )
    _, bob_id = create_user(message_app, "bob", "bob@example.com")

    message_id = insert_direct_message(message_app, alice_id, bob_id)

    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()

    socket.emit("load chat history", {"recipient": "bob"})

    history_events = get_events(socket.get_received(), "chat history")

    assert len(history_events) == 1
    assert len(history_events[0]["messages"]) == 1
    assert history_events[0]["messages"][0]["id"] == message_id


def test_load_chat_history_excludes_deleted_messages(message_app):
    alice, alice_id = create_user(
        message_app, "alice2", "alice2@example.com"
    )
    _, bob_id = create_user(
        message_app, "bob2", "bob2@example.com"
    )

    insert_direct_message(message_app, alice_id, bob_id)
    insert_direct_message(
        message_app, alice_id, bob_id, deleted_at=message_app.utc_now()
    )

    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()
    socket.emit("load chat history", {"recipient": "bob2"})

    history = get_events(socket.get_received(), "chat history")
    assert len(history) == 1
    assert len(history[0]["messages"]) == 1


def test_load_chat_history_rejects_empty_recipient(message_app):
    alice, alice_id = create_user(
        message_app, "alice3", "alice3@example.com"
    )
    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()

    socket.emit("load chat history", {"recipient": " "})

    errors = get_events(socket.get_received(), "chat error")
    assert errors
    assert errors[-1]["message"] == "Recipient is required."


def test_load_chat_history_rejects_unknown_recipient(message_app):
    alice, alice_id = create_user(
        message_app, "alice4", "alice4@example.com"
    )
    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()

    socket.emit("load chat history", {"recipient": "unknown_user"})

    errors = get_events(socket.get_received(), "chat error")
    assert errors
    assert errors[-1]["message"] == "Invalid recipient."


def test_load_chat_history_rejects_self(message_app):
    alice, alice_id = create_user(
        message_app, "alice5", "alice5@example.com"
    )
    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()

    socket.emit("load chat history", {"recipient": "alice5"})

    errors = get_events(socket.get_received(), "chat error")
    assert errors
    assert errors[-1]["message"] == "Invalid recipient."


# Direct message deletion

def test_sender_can_delete_own_message(message_app):
    alice, alice_id = create_user(
        message_app, "alice6", "alice6@example.com"
    )
    _, bob_id = create_user(
        message_app, "bob6", "bob6@example.com"
    )

    message_id = insert_direct_message(message_app, alice_id, bob_id)

    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()
    socket.emit("delete message", {"message_id": message_id})

    with message_app.get_db() as db:
        message = db.execute(
            "SELECT deleted_at FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()

    assert message["deleted_at"] is not None


def test_user_cannot_delete_another_users_message(message_app):
    alice, alice_id = create_user(
        message_app, "alice7", "alice7@example.com"
    )
    bob, bob_id = create_user(
        message_app, "bob7", "bob7@example.com"
    )

    message_id = insert_direct_message(message_app, alice_id, bob_id)

    socket = connect_socket(message_app, bob, bob_id)
    socket.get_received()
    socket.emit("delete message", {"message_id": message_id})

    errors = get_events(socket.get_received(), "chat error")
    assert errors
    assert errors[-1]["message"] == "You can only delete your own messages."

    with message_app.get_db() as db:
        message = db.execute(
            "SELECT deleted_at FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()

    assert message["deleted_at"] is None


@pytest.mark.parametrize("message_id", ["invalid", None])
def test_delete_message_rejects_invalid_id(message_app, message_id):
    alice, alice_id = create_user(
        message_app, "alice8", "alice8@example.com"
    )
    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()

    socket.emit("delete message", {"message_id": message_id})

    errors = get_events(socket.get_received(), "chat error")
    assert errors
    assert errors[-1]["message"] == "Invalid message."


def test_delete_message_rejects_expired_message(message_app):
    alice, alice_id = create_user(
        message_app, "alice9", "alice9@example.com"
    )
    _, bob_id = create_user(
        message_app, "bob9", "bob9@example.com"
    )

    message_id = insert_direct_message(
        message_app, alice_id, bob_id, age_minutes=20
    )

    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()
    socket.emit("delete message", {"message_id": message_id})

    errors = get_events(socket.get_received(), "chat error")
    assert errors
    assert "time limit has expired" in errors[-1]["message"]


def test_delete_message_rejects_already_deleted_message(message_app):
    alice, alice_id = create_user(
        message_app, "alice10", "alice10@example.com"
    )
    _, bob_id = create_user(
        message_app, "bob10", "bob10@example.com"
    )

    message_id = insert_direct_message(
        message_app, alice_id, bob_id,
        deleted_at=message_app.utc_now()
    )

    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()
    socket.emit("delete message", {"message_id": message_id})

    errors = get_events(socket.get_received(), "chat error")
    assert errors
    assert errors[-1]["message"] == "Message is already deleted."


# Group message deletion

def test_sender_can_delete_own_group_message(message_app):
    alice, alice_id = create_user(
        message_app, "alice11", "alice11@example.com"
    )
    _, bob_id = create_user(
        message_app, "bob11", "bob11@example.com"
    )

    group_id = insert_group(message_app, alice_id, bob_id)
    message_id = insert_group_message(message_app, group_id, alice_id)

    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()
    socket.emit("delete group message", {"message_id": message_id})

    with message_app.get_db() as db:
        message = db.execute(
            "SELECT deleted_at FROM group_messages WHERE id = ?",
            (message_id,),
        ).fetchone()

    assert message["deleted_at"] is not None


def test_user_cannot_delete_another_users_group_message(message_app):
    alice, alice_id = create_user(
        message_app, "alice12", "alice12@example.com"
    )
    bob, bob_id = create_user(
        message_app, "bob12", "bob12@example.com"
    )

    group_id = insert_group(message_app, alice_id, bob_id)
    message_id = insert_group_message(message_app, group_id, alice_id)

    socket = connect_socket(message_app, bob, bob_id)
    socket.get_received()
    socket.emit("delete group message", {"message_id": message_id})

    errors = get_events(socket.get_received(), "chat error")
    assert errors
    assert errors[-1]["message"] == (
        "You can only delete your own group messages."
    )


def test_delete_group_message_rejects_expired_message(message_app):
    alice, alice_id = create_user(
        message_app, "alice13", "alice13@example.com"
    )
    _, bob_id = create_user(
        message_app, "bob13", "bob13@example.com"
    )

    group_id = insert_group(message_app, alice_id, bob_id)
    message_id = insert_group_message(
        message_app, group_id, alice_id, age_minutes=20
    )

    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()
    socket.emit("delete group message", {"message_id": message_id})

    errors = get_events(socket.get_received(), "chat error")
    assert errors
    assert "time limit has expired" in errors[-1]["message"]


def test_delete_group_message_rejects_already_deleted(message_app):
    alice, alice_id = create_user(
        message_app, "alice14", "alice14@example.com"
    )
    _, bob_id = create_user(
        message_app, "bob14", "bob14@example.com"
    )

    group_id = insert_group(message_app, alice_id, bob_id)
    message_id = insert_group_message(
        message_app, group_id, alice_id,
        deleted_at=message_app.utc_now()
    )

    socket = connect_socket(message_app, alice, alice_id)
    socket.get_received()
    socket.emit("delete group message", {"message_id": message_id})

    errors = get_events(socket.get_received(), "chat error")
    assert errors
    assert errors[-1]["message"] == "Message is already deleted."
