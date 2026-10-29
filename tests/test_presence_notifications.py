import sqlite3
import pytest
import app as chat

SECURE = "https://localhost"
PASSWORD = "A-Strong-Test-Password-123!"


@pytest.fixture
def chat_app(tmp_path, monkeypatch):
    db_file = tmp_path / "presence_test.db"
    monkeypatch.setattr(chat, "DATABASE", str(db_file))
    monkeypatch.setitem(chat.app.config, "PREFERRED_URL_SCHEME", "https")
    chat.init_db()
    chat.limiter.enabled = False
    chat.app.config["TESTING"] = True
    chat.connected_users.clear()
    chat.sid_to_user.clear()
    return chat, db_file


def create_user(chat_module, username, email):
    c = chat_module.app.test_client()
    c.post("/register", base_url=SECURE, data={
        "username": username,
        "email": email,
        "password": PASSWORD,
    })
    c.post("/login", base_url=SECURE, data={
        "username_or_email": username,
        "password": PASSWORD,
    })
    with chat_module.get_db() as db:
        user = db.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
    return c, user["id"]


def connect_socket(chat_module, flask_client, user_id):
    token = chat_module.issue_ws_token(user_id)
    return chat_module.socketio.test_client(
        chat_module.app,
        flask_test_client=flask_client,
        auth={"token": token},
    )


def find_events(received_list, event_name):
    return [e.get("args", [None])[0] for e in received_list if e.get("name") == event_name]


# ---------------------------------------------------------------------------
# Requirement A: Database migration & safety
# ---------------------------------------------------------------------------
def test_existing_database_without_last_seen_upgraded_safely(tmp_path, monkeypatch):
    old_db = tmp_path / "old_users.db"
    monkeypatch.setattr(chat, "DATABASE", str(old_db))

    # Create old schema without last_seen column
    with sqlite3.connect(str(old_db)) as conn:
        conn.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            INSERT INTO users (username, email, password_hash)
            VALUES ('old_alice', 'old_alice@example.com', 'dummy_hash')
        """)
        conn.commit()

    # Run init_db which should detect missing column and add it
    chat.init_db()

    with sqlite3.connect(str(old_db)) as conn:
        conn.row_factory = sqlite3.Row
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
        assert "last_seen" in columns

        user = conn.execute("SELECT * FROM users WHERE username = 'old_alice'").fetchone()
        assert user["last_seen"] is None


def test_rerunning_init_db_is_idempotent(chat_app):
    chat_module, _ = chat_app
    # Run init_db multiple times; should succeed without error
    chat_module.init_db()
    chat_module.init_db()

    with chat_module.get_db() as db:
        columns = [row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()]
        assert columns.count("last_seen") == 1


# ---------------------------------------------------------------------------
# Requirement B: Presence (Online / Offline / Multiple tabs / Last-seen)
# ---------------------------------------------------------------------------
def test_first_connection_broadcasts_online_presence(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")

    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_alice.get_received()  # flush initial events

    # Bob connects (first connection for Bob)
    s_bob = connect_socket(chat_module, c_bob, uid_bob)

    alice_events = find_events(s_alice.get_received(), "presence update")
    assert any(e.get("username") == "bob" and e.get("is_online") is True and e.get("last_seen") is None for e in alice_events)

    with chat_module.get_db() as db:
        bob_row = db.execute("SELECT last_seen FROM users WHERE id = ?", (uid_bob,)).fetchone()
        assert bob_row["last_seen"] is None


def test_multiple_tabs_do_not_broadcast_duplicate_online(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")

    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_bob_tab1 = connect_socket(chat_module, c_bob, uid_bob)
    s_alice.get_received()  # flush Alice's received events

    # Bob connects a second tab
    s_bob_tab2 = connect_socket(chat_module, c_bob, uid_bob)

    alice_events = find_events(s_alice.get_received(), "presence update")
    # Alice should NOT receive another presence update saying Bob came online
    assert not any(e.get("username") == "bob" for e in alice_events)


def test_closing_one_tab_keeps_user_online(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")

    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_bob_tab1 = connect_socket(chat_module, c_bob, uid_bob)
    s_bob_tab2 = connect_socket(chat_module, c_bob, uid_bob)
    s_alice.get_received()

    # Bob closes tab 1
    s_bob_tab1.disconnect()

    alice_events = find_events(s_alice.get_received(), "presence update")
    # Bob is still online on tab 2, so no offline transition should be emitted
    assert not any(e.get("username") == "bob" for e in alice_events)

    with chat_module.get_db() as db:
        bob_row = db.execute("SELECT last_seen FROM users WHERE id = ?", (uid_bob,)).fetchone()
        assert bob_row["last_seen"] is None


def test_closing_final_tab_records_last_seen_and_broadcasts_offline(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")

    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_bob = connect_socket(chat_module, c_bob, uid_bob)
    s_alice.get_received()

    # Bob closes his only/final socket
    s_bob.disconnect()

    alice_events = find_events(s_alice.get_received(), "presence update")
    offline_event = next((e for e in alice_events if e.get("username") == "bob"), None)
    assert offline_event is not None
    assert offline_event["is_online"] is False
    assert offline_event["last_seen"] is not None

    with chat_module.get_db() as db:
        bob_row = db.execute("SELECT last_seen FROM users WHERE id = ?", (uid_bob,)).fetchone()
        assert bob_row["last_seen"] == offline_event["last_seen"]


def test_get_presence_request_authenticated(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")

    # Bob connects and then disconnects to set a last_seen
    s_bob = connect_socket(chat_module, c_bob, uid_bob)
    s_bob.disconnect()

    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_alice.get_received()

    # Alice queries Bob's presence
    s_alice.emit("get presence", {"username": "bob"})

    alice_events = find_events(s_alice.get_received(), "presence update")
    assert len(alice_events) == 1
    assert alice_events[0]["username"] == "bob"
    assert alice_events[0]["is_online"] is False
    assert alice_events[0]["last_seen"] is not None

    # Bob comes online
    s_bob = connect_socket(chat_module, c_bob, uid_bob)
    s_alice.get_received()

    s_alice.emit("get presence", {"username": "bob"})
    alice_events = find_events(s_alice.get_received(), "presence update")
    assert len(alice_events) == 1
    assert alice_events[0]["username"] == "bob"
    assert alice_events[0]["is_online"] is True
    assert alice_events[0]["last_seen"] is None


def test_get_presence_unknown_or_invalid_target(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_alice.get_received()

    # Non-existent user
    s_alice.emit("get presence", {"username": "non_existent"})
    errors = find_events(s_alice.get_received(), "chat error")
    assert any(e.get("message") == "User not found." for e in errors)

    # Empty username
    s_alice.emit("get presence", {"username": "   "})
    errors = find_events(s_alice.get_received(), "chat error")
    assert any(e.get("message") == "Username is required." for e in errors)


def test_logout_triggers_offline_presence(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")

    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_bob = connect_socket(chat_module, c_bob, uid_bob)
    s_alice.get_received()

    # Bob logs out via HTTP POST /logout
    c_bob.post("/logout", base_url=SECURE)

    alice_events = find_events(s_alice.get_received(), "presence update")
    assert any(e.get("username") == "bob" and e.get("is_online") is False for e in alice_events)


# ---------------------------------------------------------------------------
# Requirement C: Missed-message notifications on reconnect
# ---------------------------------------------------------------------------
def test_missed_message_summary_on_reconnect(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")
    c_charlie, uid_charlie = create_user(chat_module, "charlie", "charlie@example.com")

    # Bob is currently offline
    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_charlie = connect_socket(chat_module, c_charlie, uid_charlie)

    s_alice.emit("chat message", {"recipient": "bob", "text": "Msg 1 from Alice"})
    s_alice.emit("chat message", {"recipient": "bob", "text": "Msg 2 from Alice"})
    s_charlie.emit("chat message", {"recipient": "bob", "text": "Msg 1 from Charlie"})

    # Bob connects
    s_bob = connect_socket(chat_module, c_bob, uid_bob)
    bob_events = s_bob.get_received()

    missed_events = find_events(bob_events, "missed messages")
    assert len(missed_events) == 1
    assert missed_events[0]["total"] == 3
    assert missed_events[0]["senders"]["alice"] == 2
    assert missed_events[0]["senders"]["charlie"] == 1

    # Pending chat messages are also delivered
    chat_msgs = find_events(bob_events, "chat message")
    assert len(chat_msgs) == 3


def test_no_missed_messages_event_when_none_pending(chat_app):
    chat_module, _ = chat_app
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")

    s_bob = connect_socket(chat_module, c_bob, uid_bob)
    bob_events = s_bob.get_received()

    missed_events = find_events(bob_events, "missed messages")
    assert len(missed_events) == 0


# ---------------------------------------------------------------------------
# Requirement D: Unread-message counts per conversation
# ---------------------------------------------------------------------------
def test_initial_unread_counts_on_connect(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")
    c_charlie, uid_charlie = create_user(chat_module, "charlie", "charlie@example.com")

    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_charlie = connect_socket(chat_module, c_charlie, uid_charlie)

    s_alice.emit("chat message", {"recipient": "bob", "text": "Hello 1"})
    s_alice.emit("chat message", {"recipient": "bob", "text": "Hello 2"})
    s_charlie.emit("chat message", {"recipient": "bob", "text": "Hello from Charlie"})

    # Bob connects
    s_bob = connect_socket(chat_module, c_bob, uid_bob)
    unread_events = find_events(s_bob.get_received(), "unread counts")
    assert len(unread_events) == 1
    assert unread_events[0]["total"] == 3
    assert unread_events[0]["counts"]["alice"] == 2
    assert unread_events[0]["counts"]["charlie"] == 1


def test_new_incoming_message_updates_unread_counts_realtime(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")

    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_bob = connect_socket(chat_module, c_bob, uid_bob)
    s_bob.get_received()

    # Alice sends a message to Bob while Bob is connected
    s_alice.emit("chat message", {"recipient": "bob", "text": "Live message"})

    bob_events = s_bob.get_received()
    unread_events = find_events(bob_events, "unread counts")
    assert len(unread_events) == 1
    assert unread_events[0]["total"] == 1
    assert unread_events[0]["counts"]["alice"] == 1


def test_reading_message_reduces_unread_count_for_specific_sender(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")
    c_charlie, uid_charlie = create_user(chat_module, "charlie", "charlie@example.com")

    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_charlie = connect_socket(chat_module, c_charlie, uid_charlie)

    s_alice.emit("chat message", {"recipient": "bob", "text": "Msg 1 from Alice"})
    s_alice.emit("chat message", {"recipient": "bob", "text": "Msg 2 from Alice"})
    s_charlie.emit("chat message", {"recipient": "bob", "text": "Msg from Charlie"})

    s_bob = connect_socket(chat_module, c_bob, uid_bob)
    received = s_bob.get_received()
    chat_msgs = find_events(received, "chat message")

    # Find Alice's first message id
    alice_msg1 = next(m for m in chat_msgs if m["username"] == "alice" and m["text"] == "Msg 1 from Alice")

    # Bob marks Alice's first message as read
    s_bob.emit("message read", {"message_id": alice_msg1["id"]})

    bob_events = s_bob.get_received()
    unread_events = find_events(bob_events, "unread counts")
    assert len(unread_events) == 1
    # Total should now be 2 (1 Alice, 1 Charlie)
    assert unread_events[0]["total"] == 2
    assert unread_events[0]["counts"]["alice"] == 1
    assert unread_events[0]["counts"]["charlie"] == 1


def test_unauthorized_user_cannot_mark_message_read(chat_app):
    chat_module, _ = chat_app
    c_alice, uid_alice = create_user(chat_module, "alice", "alice@example.com")
    c_bob, uid_bob = create_user(chat_module, "bob", "bob@example.com")
    c_charlie, uid_charlie = create_user(chat_module, "charlie", "charlie@example.com")

    s_alice = connect_socket(chat_module, c_alice, uid_alice)
    s_alice.emit("chat message", {"recipient": "bob", "text": "Private for Bob"})

    s_bob = connect_socket(chat_module, c_bob, uid_bob)
    chat_msgs = find_events(s_bob.get_received(), "chat message")
    msg_id = chat_msgs[0]["id"]

    # Charlie connects and attempts to mark the message read
    s_charlie = connect_socket(chat_module, c_charlie, uid_charlie)
    s_charlie.emit("message read", {"message_id": msg_id})

    # Verify message is still unread in database
    with chat_module.get_db() as db:
        row = db.execute("SELECT read_at FROM messages WHERE id = ?", (msg_id,)).fetchone()
        assert row["read_at"] is None
