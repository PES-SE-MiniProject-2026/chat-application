
import pytest
import app as chat

SECURE = "https://localhost"
PASSWORD = "A-Strong-Test-Password-123!"


@pytest.fixture
def chat_app(tmp_path, monkeypatch):
    db_file = tmp_path / "access_control_test.db"

    monkeypatch.setattr(chat, "DATABASE", str(db_file))
    monkeypatch.setitem(chat.app.config, "PREFERRED_URL_SCHEME", "https")

    chat.init_db()
    chat.limiter.enabled = False
    chat.app.config["TESTING"] = True
    chat.connected_users.clear()
    chat.sid_to_user.clear()

    return chat, db_file


def create_user(chat_module, username, email):
    client = chat_module.app.test_client()

    client.post(
        "/register",
        base_url=SECURE,
        data={
            "username": username,
            "email": email,
            "password": PASSWORD,
        },
    )

    client.post(
        "/login",
        base_url=SECURE,
        data={
            "username_or_email": username,
            "password": PASSWORD,
        },
    )

    with chat_module.get_db() as db:
        user = db.execute(
            "SELECT id FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    return client, user["id"]


def connect_socket(chat_module, flask_client, user_id):
    token = chat_module.issue_ws_token(user_id)

    return chat_module.socketio.test_client(
        chat_module.app,
        flask_test_client=flask_client,
        auth={"token": token},
    )


def find_events(received, event_name):
    return [
        event.get("args", [None])[0]
        for event in received
        if event.get("name") == event_name
    ]


def create_group(chat_module, creator_id, member_id):
    with chat_module.get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO groups (name, created_by, created_at)
            VALUES (?, ?, ?)
            """,
            ("Test Group", creator_id, chat_module.utc_now()),
        )

        group_id = cursor.lastrowid

        db.execute(
            """
            INSERT INTO group_members (group_id, user_id, joined_at)
            VALUES (?, ?, ?), (?, ?, ?)
            """,
            (
                group_id,
                creator_id,
                chat_module.utc_now(),
                group_id,
                member_id,
                chat_module.utc_now(),
            ),
        )

    return group_id


def test_non_member_cannot_load_group_history(chat_app):
    module, _ = chat_app

    alice, alice_id = create_user(
        module, "alice", "alice@example.com"
    )
    bob, bob_id = create_user(
        module, "bob", "bob@example.com"
    )
    charlie, charlie_id = create_user(
        module, "charlie", "charlie@example.com"
    )

    group_id = create_group(module, alice_id, bob_id)

    socket = connect_socket(module, charlie, charlie_id)
    socket.get_received()

    socket.emit(
        "load group history",
        {"group_id": group_id},
    )

    events = socket.get_received()

    errors = find_events(events, "chat error")
    histories = find_events(events, "group history")

    assert errors
    assert any(
        "not a member" in event["message"].lower()
        for event in errors
    )
    assert not histories


def test_non_member_cannot_send_group_message(chat_app):
    module, _ = chat_app

    alice, alice_id = create_user(
        module, "alice", "alice@example.com"
    )
    bob, bob_id = create_user(
        module, "bob", "bob@example.com"
    )
    charlie, charlie_id = create_user(
        module, "charlie", "charlie@example.com"
    )

    group_id = create_group(module, alice_id, bob_id)

    socket = connect_socket(module, charlie, charlie_id)
    socket.get_received()

    socket.emit(
        "group message",
        {
            "group_id": group_id,
            "text": "Unauthorized message",
        },
    )

    events = socket.get_received()
    errors = find_events(events, "chat error")

    assert errors
    assert any(
        "not a member" in event["message"].lower()
        for event in errors
    )

    with module.get_db() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM group_messages WHERE group_id = ?",
            (group_id,),
        ).fetchone()[0]

    assert count == 0


def test_typing_notification_not_sent_without_conversation(chat_app):
    module, _ = chat_app

    alice, alice_id = create_user(
        module, "alice", "alice@example.com"
    )
    bob, bob_id = create_user(
        module, "bob", "bob@example.com"
    )

    alice_socket = connect_socket(module, alice, alice_id)
    bob_socket = connect_socket(module, bob, bob_id)

    alice_socket.get_received()
    bob_socket.get_received()

    alice_socket.emit(
        "typing",
        {
            "recipient": "bob",
            "is_typing": True,
        },
    )

    bob_events = bob_socket.get_received()

    assert not find_events(bob_events, "typing")


def test_typing_notification_sent_for_existing_conversation(chat_app):
    module, _ = chat_app

    alice, alice_id = create_user(
        module, "alice", "alice@example.com"
    )
    bob, bob_id = create_user(
        module, "bob", "bob@example.com"
    )

    with module.get_db() as db:
        db.execute(
            """
            INSERT INTO messages
                (sender_id, recipient_id, text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                alice_id,
                bob_id,
                "Hello",
                module.utc_now(),
            ),
        )

    alice_socket = connect_socket(module, alice, alice_id)
    bob_socket = connect_socket(module, bob, bob_id)

    alice_socket.get_received()
    bob_socket.get_received()

    alice_socket.emit(
        "typing",
        {
            "recipient": "bob",
            "is_typing": True,
        },
    )

    bob_events = bob_socket.get_received()
    typing_events = find_events(bob_events, "typing")

    assert typing_events
    assert typing_events[0]["username"] == "alice"
    assert typing_events[0]["is_typing"] is True
