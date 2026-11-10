
import pytest
import app as chat

SECURE = "https://localhost"
PASSWORD = "A-Strong-Test-Password-123!"


@pytest.fixture
def group_app(tmp_path, monkeypatch):
    db_file = tmp_path / "group_handlers_test.db"
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


def events(received, name):
    return [
        event.get("args", [None])[0]
        for event in received
        if event.get("name") == name
    ]


def create_group(module, creator_id, member_ids=()):
    with module.get_db() as db:
        cursor = db.execute(
            "INSERT INTO groups (name, created_by, created_at) VALUES (?, ?, ?)",
            ("Test Group", creator_id, module.utc_now()),
        )
        group_id = cursor.lastrowid

        for user_id in (creator_id, *member_ids):
            db.execute(
                """INSERT INTO group_members (group_id, user_id, joined_at)
                   VALUES (?, ?, ?)""",
                (group_id, user_id, module.utc_now()),
            )

    return group_id


def test_create_group_success(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    bob, bob_id = create_user(module, "bob", "bob@example.com")
    socket = connect_socket(module, alice, alice_id)
    socket.get_received()

    socket.emit("create group", {"name": "Project Team", "members": ["bob"]})

    with module.get_db() as db:
        group = db.execute(
            "SELECT id, name, created_by FROM groups WHERE name = ?",
            ("Project Team",),
        ).fetchone()
        members = db.execute(
            "SELECT user_id FROM group_members WHERE group_id = ?",
            (group["id"],),
        ).fetchall()

    assert group["created_by"] == alice_id
    assert {row["user_id"] for row in members} == {alice_id, bob_id}
    assert events(socket.get_received(), "chat error") == []


@pytest.mark.parametrize("name", ["", " "])
def test_create_group_rejects_empty_name(group_app, name):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    socket = connect_socket(module, alice, alice_id)
    socket.get_received()

    socket.emit("create group", {"name": name, "members": []})

    errors = events(socket.get_received(), "chat error")
    assert errors
    assert "group name" in errors[0]["message"].lower()


def test_create_group_rejects_name_over_50_characters(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    socket = connect_socket(module, alice, alice_id)
    socket.get_received()

    socket.emit("create group", {"name": "G" * 51, "members": []})

    assert events(socket.get_received(), "chat error")


def test_create_group_rejects_unknown_member(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    socket = connect_socket(module, alice, alice_id)
    socket.get_received()

    socket.emit(
        "create group",
        {"name": "Project Team", "members": ["unknown_user"]},
    )

    assert events(socket.get_received(), "chat error")

    with module.get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM groups").fetchone()[0]

    assert count == 0


def test_add_group_member_success(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    bob, bob_id = create_user(module, "bob", "bob@example.com")
    socket = connect_socket(module, alice, alice_id)
    socket.get_received()

    group_id = create_group(module, alice_id)

    socket.emit(
        "add group member",
        {"group_id": group_id, "username": "bob"},
    )

    with module.get_db() as db:
        member = db.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, bob_id),
        ).fetchone()

    assert member is not None
    assert events(socket.get_received(), "chat error") == []


def test_add_group_member_rejects_duplicate(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    bob, bob_id = create_user(module, "bob", "bob@example.com")
    socket = connect_socket(module, alice, alice_id)
    socket.get_received()

    group_id = create_group(module, alice_id, [bob_id])

    socket.emit("add group member", {
        "group_id": group_id, "username": "bob"
    })

    errors = events(socket.get_received(), "chat error")
    assert errors
    assert "already" in errors[0]["message"].lower()


def test_add_group_member_rejects_non_admin(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    bob, bob_id = create_user(module, "bob", "bob@example.com")
    charlie, charlie_id = create_user(module, "charlie", "charlie@example.com")
    group_id = create_group(module, alice_id, [bob_id])

    socket = connect_socket(module, bob, bob_id)
    socket.get_received()

    socket.emit("add group member", {
        "group_id": group_id, "username": "charlie"
    })

    assert events(socket.get_received(), "chat error")


def test_remove_group_member_success(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    bob, bob_id = create_user(module, "bob", "bob@example.com")
    socket = connect_socket(module, alice, alice_id)
    socket.get_received()

    group_id = create_group(module, alice_id, [bob_id])

    socket.emit("remove group member", {
        "group_id": group_id, "username": "bob"
    })

    with module.get_db() as db:
        member = db.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, bob_id),
        ).fetchone()

    assert member is None
    assert events(socket.get_received(), "chat error") == []


def test_group_admin_cannot_remove_themselves(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    socket = connect_socket(module, alice, alice_id)
    socket.get_received()
    group_id = create_group(module, alice_id)

    socket.emit("remove group member", {
        "group_id": group_id, "username": "alice"
    })

    assert events(socket.get_received(), "chat error")


def test_non_admin_cannot_remove_group_member(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    bob, bob_id = create_user(module, "bob", "bob@example.com")
    charlie, charlie_id = create_user(module, "charlie", "charlie@example.com")
    group_id = create_group(module, alice_id, [bob_id, charlie_id])

    socket = connect_socket(module, bob, bob_id)
    socket.get_received()

    socket.emit("remove group member", {
        "group_id": group_id, "username": "charlie"
    })

    assert events(socket.get_received(), "chat error")


def test_member_can_leave_group(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    bob, bob_id = create_user(module, "bob", "bob@example.com")
    group_id = create_group(module, alice_id, [bob_id])

    socket = connect_socket(module, bob, bob_id)
    socket.get_received()

    socket.emit("leave group", {"group_id": group_id})

    with module.get_db() as db:
        member = db.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, bob_id),
        ).fetchone()

    assert member is None
    assert events(socket.get_received(), "chat error") == []


def test_group_admin_cannot_leave(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    socket = connect_socket(module, alice, alice_id)
    socket.get_received()
    group_id = create_group(module, alice_id)

    socket.emit("leave group", {"group_id": group_id})

    assert events(socket.get_received(), "chat error")


def test_group_message_is_saved_and_broadcast(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    bob, bob_id = create_user(module, "bob", "bob@example.com")
    group_id = create_group(module, alice_id, [bob_id])

    alice_socket = connect_socket(module, alice, alice_id)
    bob_socket = connect_socket(module, bob, bob_id)
    alice_socket.get_received()
    bob_socket.get_received()

    alice_socket.emit("group message", {
        "group_id": group_id, "text": "Hello group"
    })

    with module.get_db() as db:
        message = db.execute(
            "SELECT text, sender_id FROM group_messages WHERE group_id = ?",
            (group_id,),
        ).fetchone()

    assert message["text"] == "Hello group"
    assert message["sender_id"] == alice_id

    received = events(bob_socket.get_received(), "group message")
    assert any(item["text"] == "Hello group" for item in received)


def test_group_message_rejects_empty_text(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    group_id = create_group(module, alice_id)

    socket = connect_socket(module, alice, alice_id)
    socket.get_received()

    socket.emit("group message", {"group_id": group_id, "text": "  "})

    assert events(socket.get_received(), "chat error")


def test_load_groups_returns_memberships(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    bob, bob_id = create_user(module, "bob", "bob@example.com")
    group_id = create_group(module, alice_id, [bob_id])

    socket = connect_socket(module, bob, bob_id)
    socket.get_received()

    socket.emit("load groups")

    results = events(socket.get_received(), "groups list")
    assert results
    assert any(group["group_id"] == group_id for group in results[0]["groups"])


def test_load_group_history_returns_messages(group_app):
    module = group_app
    alice, alice_id = create_user(module, "alice", "alice@example.com")
    bob, bob_id = create_user(module, "bob", "bob@example.com")
    group_id = create_group(module, alice_id, [bob_id])

    with module.get_db() as db:
        db.execute(
            """INSERT INTO group_messages
               (group_id, sender_id, text, created_at)
               VALUES (?, ?, ?, ?)""",
            (group_id, alice_id, "History message", module.utc_now()),
        )

    socket = connect_socket(module, bob, bob_id)
    socket.get_received()

    socket.emit("load group history", {"group_id": group_id})

    histories = events(socket.get_received(), "group history")
    assert histories
    assert histories[0]["group_name"] == "Test Group"
    assert histories[0]["messages"][0]["text"] == "History message"
