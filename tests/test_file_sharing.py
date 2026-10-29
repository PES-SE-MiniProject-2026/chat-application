import io
import os
import sqlite3
import pytest
import app as chat

SECURE = "https://localhost"
PASSWORD = "A-Strong-Test-Password-123!"

VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
VALID_JPEG = b"\xff\xd8\xff" + b"\x00" * 64
VALID_GIF = b"GIF89a" + b"\x00" * 64
VALID_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24
VALID_PDF = b"%PDF-1.4\n%test pdf document\n%%EOF"
VALID_TXT = b"Hello, this is a plain text file.\nLine 2 with tab:\tDone.\r\n"


@pytest.fixture
def chat_env(tmp_path, monkeypatch):
    db_file = tmp_path / "fileshare_test.db"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(exist_ok=True)

    monkeypatch.setattr(chat, "DATABASE", str(db_file))
    monkeypatch.setattr(chat, "UPLOAD_FOLDER", str(upload_dir))
    monkeypatch.setitem(chat.app.config, "PREFERRED_URL_SCHEME", "https")

    chat.init_db()
    chat.limiter.enabled = False
    chat.app.config["TESTING"] = True
    chat.connected_users.clear()
    chat.sid_to_user.clear()

    return chat, db_file, upload_dir


def create_user(chat_module, username, email):
    client = chat_module.app.test_client()
    client.post("/register", base_url=SECURE, data={
        "username": username,
        "email": email,
        "password": PASSWORD,
    })
    client.post("/login", base_url=SECURE, data={
        "username_or_email": username,
        "password": PASSWORD,
    })
    with chat_module.get_db() as db:
        user = db.execute(
            "SELECT id, username FROM users WHERE username = ?", (username,)
        ).fetchone()
    return client, user["id"]


def connect_socket(chat_module, flask_client, user_id):
    token = chat_module.issue_ws_token(user_id)
    return chat_module.socketio.test_client(
        chat_module.app,
        flask_test_client=flask_client,
        auth={"token": token},
    )


# ---------------------------------------------------------------------------
# 1. Authenticated PNG upload succeeds and persists correct metadata
# ---------------------------------------------------------------------------
def test_authenticated_png_upload_persists_metadata(chat_env):
    chat_mod, db_file, upload_dir = chat_env
    alice_client, alice_id = create_user(chat_mod, "alice", "alice@example.com")
    bob_client, bob_id = create_user(chat_mod, "bob", "bob@example.com")

    response = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "text": "Screenshot caption",
            "file": (io.BytesIO(VALID_PNG), "photo.png"),
        },
    )

    assert response.status_code == 201
    data = response.get_json()
    assert data is not None
    assert data["sender_id"] == alice_id
    assert data["recipient_id"] == bob_id
    assert data["text"] == "Screenshot caption"
    assert data["attachment_filename"] == "photo.png"
    assert data["attachment_size"] == len(VALID_PNG)
    assert data["attachment_mime"] == "image/png"
    assert data["attachment_url"] == f"/attachments/{data['id']}"

    # Verify database persistence
    with chat_mod.get_db() as db:
        row = db.execute(
            "SELECT * FROM messages WHERE id = ?", (data["id"],)
        ).fetchone()

    assert row is not None
    assert row["attachment_filename"] == "photo.png"
    assert row["attachment_size"] == len(VALID_PNG)
    assert row["attachment_mime"] == "image/png"
    assert row["attachment_path"] is not None

    # Verify physical file stored on disk
    stored_path = os.path.join(str(upload_dir), row["attachment_path"])
    assert os.path.isfile(stored_path)
    with open(stored_path, "rb") as f:
        assert f.read() == VALID_PNG


# ---------------------------------------------------------------------------
# 2. Authenticated PDF and TXT upload succeeds
# ---------------------------------------------------------------------------
def test_authenticated_pdf_and_txt_upload_succeed(chat_env):
    chat_mod, _, upload_dir = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")
    _, _ = create_user(chat_mod, "bob", "bob@example.com")

    # PDF upload without caption: text column falls back to filename
    pdf_resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(VALID_PDF), "spec.pdf"),
        },
    )
    assert pdf_resp.status_code == 201
    pdf_data = pdf_resp.get_json()
    assert pdf_data["attachment_mime"] == "application/pdf"
    assert pdf_data["attachment_filename"] == "spec.pdf"
    assert pdf_data["text"] == "spec.pdf"

    # TXT upload with caption
    txt_resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "text": "Notes file",
            "file": (io.BytesIO(VALID_TXT), "notes.txt"),
        },
    )
    assert txt_resp.status_code == 201
    txt_data = txt_resp.get_json()
    assert txt_data["attachment_mime"] == "text/plain"
    assert txt_data["attachment_filename"] == "notes.txt"
    assert txt_data["text"] == "Notes file"


# ---------------------------------------------------------------------------
# 3. Unsupported extensions are rejected
# ---------------------------------------------------------------------------
def test_unsupported_extensions_are_rejected(chat_env):
    chat_mod, _, upload_dir = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")
    _, _ = create_user(chat_mod, "bob", "bob@example.com")

    for bad_ext in ("malware.exe", "page.html", "script.js", "vector.svg", "archive.zip", "test.py"):
        resp = alice_client.post(
            "/upload",
            base_url=SECURE,
            data={
                "recipient": "bob",
                "file": (io.BytesIO(b"dangerous content"), bad_ext),
            },
        )
        assert resp.status_code == 400
        assert "unsupported" in resp.get_json()["error"].lower()

    # Ensure no files written and no messages stored
    assert len(os.listdir(str(upload_dir))) == 0
    with chat_mod.get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# 4. Mismatched binary magic bytes are rejected
# ---------------------------------------------------------------------------
def test_mismatched_binary_magic_bytes_rejected(chat_env):
    chat_mod, _, upload_dir = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")
    _, _ = create_user(chat_mod, "bob", "bob@example.com")

    test_cases = [
        ("fake.png", b"MZ\x90\x00" + b"\x00" * 20),           # EXE disguised as PNG
        ("fake.jpg", b"<html><body>test</body></html>"),       # HTML disguised as JPG
        ("fake.gif", b"RIFF\x00\x00\x00\x00WEBP"),             # WebP disguised as GIF
        ("fake.webp", b"%PDF-1.4\nfake webp"),                 # PDF disguised as WebP
        ("fake.pdf", b"\x89PNG\r\n\x1a\nfake pdf"),            # PNG disguised as PDF
    ]

    for filename, content in test_cases:
        resp = alice_client.post(
            "/upload",
            base_url=SECURE,
            data={
                "recipient": "bob",
                "file": (io.BytesIO(content), filename),
            },
        )
        assert resp.status_code == 400
        assert "signature" in resp.get_json()["error"].lower()

    assert len(os.listdir(str(upload_dir))) == 0


# ---------------------------------------------------------------------------
# 5. Invalid text files are rejected
# ---------------------------------------------------------------------------
def test_invalid_text_files_rejected(chat_env):
    chat_mod, _, upload_dir = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")
    _, _ = create_user(chat_mod, "bob", "bob@example.com")

    # Contains NUL byte
    resp1 = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(b"Hello\x00World"), "nul.txt"),
        },
    )
    assert resp1.status_code == 400
    assert "nul" in resp1.get_json()["error"].lower()

    # Invalid UTF-8
    resp2 = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(b"\xff\xfe\x00\x12"), "bad_utf8.txt"),
        },
    )
    assert resp2.status_code == 400

    # Disallowed control character (ASCII bell \x07)
    resp3 = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(b"Hello\x07World"), "ctrl.txt"),
        },
    )
    assert resp3.status_code == 400
    assert "control" in resp3.get_json()["error"].lower()

    assert len(os.listdir(str(upload_dir))) == 0


# ---------------------------------------------------------------------------
# 6. Requests over 5 MiB return 413
# ---------------------------------------------------------------------------
def test_requests_over_5mib_return_413(chat_env):
    chat_mod, _, upload_dir = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")
    _, _ = create_user(chat_mod, "bob", "bob@example.com")

    oversized_data = b"\x89PNG\r\n\x1a\n" + (b"A" * (5 * 1024 * 1024 + 1024))
    resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(oversized_data), "huge.png"),
        },
    )
    assert resp.status_code == 413
    assert len(os.listdir(str(upload_dir))) == 0


# ---------------------------------------------------------------------------
# 7. Anonymous upload is rejected or redirected
# ---------------------------------------------------------------------------
def test_anonymous_upload_rejected(chat_env):
    chat_mod, _, upload_dir = chat_env
    anon_client = chat_mod.app.test_client()

    resp = anon_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(VALID_PNG), "test.png"),
        },
    )
    # Existing route conventions redirect to login (302) or return 401
    assert resp.status_code in (302, 401)
    assert len(os.listdir(str(upload_dir))) == 0


# ---------------------------------------------------------------------------
# 8. Anonymous download is rejected or redirected
# ---------------------------------------------------------------------------
def test_anonymous_download_rejected(chat_env):
    chat_mod, _, _ = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")
    _, _ = create_user(chat_mod, "bob", "bob@example.com")

    up_resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(VALID_PNG), "test.png"),
        },
    )
    msg_id = up_resp.get_json()["id"]

    anon_client = chat_mod.app.test_client()
    down_resp = anon_client.get(f"/attachments/{msg_id}", base_url=SECURE)
    assert down_resp.status_code in (302, 401)


# ---------------------------------------------------------------------------
# 9. Unrelated third-party download returns 403
# ---------------------------------------------------------------------------
def test_third_party_download_returns_403(chat_env):
    chat_mod, _, _ = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")
    _, _ = create_user(chat_mod, "bob", "bob@example.com")
    charlie_client, _ = create_user(chat_mod, "charlie", "charlie@example.com")

    up_resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(VALID_PNG), "test.png"),
        },
    )
    msg_id = up_resp.get_json()["id"]

    # Charlie attempts to download attachment from Alice-Bob conversation
    resp = charlie_client.get(f"/attachments/{msg_id}", base_url=SECURE)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 10. Sender and recipient can download the attachment
# ---------------------------------------------------------------------------
def test_sender_and_recipient_can_download_attachment(chat_env):
    chat_mod, _, _ = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")
    bob_client, _ = create_user(chat_mod, "bob", "bob@example.com")

    up_resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(VALID_PDF), "spec.pdf"),
        },
    )
    msg_id = up_resp.get_json()["id"]

    # Sender download
    s_resp = alice_client.get(f"/attachments/{msg_id}", base_url=SECURE)
    assert s_resp.status_code == 200
    assert s_resp.data == VALID_PDF
    assert s_resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "default-src 'none'" in s_resp.headers.get("Content-Security-Policy", "")

    # Recipient download
    r_resp = bob_client.get(f"/attachments/{msg_id}", base_url=SECURE)
    assert r_resp.status_code == 200
    assert r_resp.data == VALID_PDF
    assert r_resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "default-src 'none'" in r_resp.headers.get("Content-Security-Policy", "")


# ---------------------------------------------------------------------------
# 11. Missing attachment or message returns 404
# ---------------------------------------------------------------------------
def test_missing_attachment_or_message_returns_404(chat_env):
    chat_mod, _, upload_dir = chat_env
    alice_client, alice_id = create_user(chat_mod, "alice", "alice@example.com")
    bob_client, bob_id = create_user(chat_mod, "bob", "bob@example.com")

    # Non-existent message
    resp1 = alice_client.get("/attachments/99999", base_url=SECURE)
    assert resp1.status_code == 404

    # Text message without attachment
    with chat_mod.get_db() as db:
        c = db.execute(
            "INSERT INTO messages (sender_id, recipient_id, text, created_at) VALUES (?, ?, ?, ?)",
            (alice_id, bob_id, "Plain text message", chat_mod.utc_now()),
        )
        text_msg_id = c.lastrowid

    resp2 = alice_client.get(f"/attachments/{text_msg_id}", base_url=SECURE)
    assert resp2.status_code == 404

    # File missing from disk
    up_resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(VALID_PNG), "deleted.png"),
        },
    )
    del_msg_id = up_resp.get_json()["id"]
    with chat_mod.get_db() as db:
        row = db.execute("SELECT attachment_path FROM messages WHERE id = ?", (del_msg_id,)).fetchone()
    os.remove(os.path.join(str(upload_dir), row["attachment_path"]))

    resp3 = bob_client.get(f"/attachments/{del_msg_id}", base_url=SECURE)
    assert resp3.status_code == 404


# ---------------------------------------------------------------------------
# 12. Self-recipient upload is rejected
# ---------------------------------------------------------------------------
def test_self_recipient_upload_is_rejected(chat_env):
    chat_mod, _, upload_dir = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")

    resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "alice",
            "file": (io.BytesIO(VALID_PNG), "self.png"),
        },
    )
    assert resp.status_code == 400
    assert "yourself" in resp.get_json()["error"].lower()
    assert len(os.listdir(str(upload_dir))) == 0


# ---------------------------------------------------------------------------
# 13. Unknown recipient is rejected
# ---------------------------------------------------------------------------
def test_unknown_recipient_is_rejected(chat_env):
    chat_mod, _, upload_dir = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")

    resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "ghost_user",
            "file": (io.BytesIO(VALID_PNG), "ghost.png"),
        },
    )
    assert resp.status_code == 404
    assert "recipient not found" in resp.get_json()["error"].lower()
    assert len(os.listdir(str(upload_dir))) == 0


# ---------------------------------------------------------------------------
# 14. Failed filesystem or database operations clean up temporary files
# ---------------------------------------------------------------------------
def test_failed_operations_cleanup_files_and_records(chat_env, monkeypatch):
    chat_mod, _, upload_dir = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")
    _, _ = create_user(chat_mod, "bob", "bob@example.com")

    # Simulate database insertion failure
    orig_get_db = chat_mod.get_db

    class FailingConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            if "INSERT INTO messages" in sql:
                raise sqlite3.OperationalError("Simulated disk error on insert")
            return self._conn.execute(sql, *args)

        def commit(self):
            return self._conn.commit()

        def rollback(self):
            return self._conn.rollback()

        def close(self):
            return self._conn.close()

    from contextlib import contextmanager

    @contextmanager
    def mock_get_db():
        with orig_get_db() as conn:
            yield FailingConnection(conn)

    monkeypatch.setattr(chat_mod, "get_db", mock_get_db)

    resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(VALID_PNG), "fail.png"),
        },
    )
    assert resp.status_code == 500

    # Ensure no file was left on disk
    assert len(os.listdir(str(upload_dir))) == 0

    # Restore DB and verify no message was inserted
    monkeypatch.setattr(chat_mod, "get_db", orig_get_db)
    with chat_mod.get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# 15. Compatibility with Socket.IO, delivery ack, read receipt, and unread counts
# ---------------------------------------------------------------------------
def test_socket_events_delivery_ack_read_receipt_and_unread_counts(chat_env):
    chat_mod, _, _ = chat_env
    alice_client, alice_id = create_user(chat_mod, "alice", "alice@example.com")
    bob_client, bob_id = create_user(chat_mod, "bob", "bob@example.com")

    alice_ws = connect_socket(chat_mod, alice_client, alice_id)
    bob_ws = connect_socket(chat_mod, bob_client, bob_id)

    # Bob's initial unread count is 0
    bob_events = bob_ws.get_received()
    unread_events = [e["args"][0] for e in bob_events if e["name"] == "unread counts"]
    assert len(unread_events) > 0
    assert unread_events[-1]["total"] == 0

    # Alice uploads an attachment
    resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "text": "Attachment for you",
            "file": (io.BytesIO(VALID_PNG), "photo.png"),
        },
    )
    assert resp.status_code == 201
    msg_id = resp.get_json()["id"]

    # Bob receives real-time chat message with attachment metadata
    bob_events = bob_ws.get_received()
    chat_events = [e["args"][0] for e in bob_events if e["name"] == "chat message"]
    assert len(chat_events) == 1
    assert chat_events[0]["id"] == msg_id
    assert chat_events[0]["attachment_filename"] == "photo.png"
    assert chat_events[0]["attachment_mime"] == "image/png"

    # Bob's socket acknowledges delivery
    chat_mod.mark_message_delivered(msg_id, alice_id, {"received": True})

    # Alice receives delivery status
    alice_events = alice_ws.get_received()
    status_events = [e["args"][0] for e in alice_events if e["name"] == "message status"]
    assert any(e["message_id"] == msg_id and e["status"] == "delivered" for e in status_events)

    # Bob marks message as read
    bob_ws.emit("message read", {"message_id": msg_id})

    # Alice receives read status
    alice_events = alice_ws.get_received()
    status_events = [e["args"][0] for e in alice_events if e["name"] == "message status"]
    assert any(e["message_id"] == msg_id and e["status"] == "read" for e in status_events)

    # Bob's unread counts drop back to 0
    bob_events = bob_ws.get_received()
    unread_events = [e["args"][0] for e in bob_events if e["name"] == "unread counts"]
    assert unread_events[-1]["total"] == 0


# ---------------------------------------------------------------------------
# 16. Existing databases migrate without losing messages & init_db is idempotent
# ---------------------------------------------------------------------------
def test_database_migration_preserves_messages_and_is_idempotent(tmp_path, monkeypatch):
    old_db = tmp_path / "legacy.db"
    upload_dir = tmp_path / "legacy_uploads"
    upload_dir.mkdir(exist_ok=True)

    # 1. Create legacy database without attachment columns
    conn = sqlite3.connect(str(old_db))
    conn.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 1000),
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            read_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO users (username, email, password_hash) VALUES ('legacy_alice', 'la@example.com', 'hash')"
    )
    conn.execute(
        "INSERT INTO users (username, email, password_hash) VALUES ('legacy_bob', 'lb@example.com', 'hash')"
    )
    conn.execute(
        "INSERT INTO messages (sender_id, recipient_id, text, created_at) VALUES (1, 2, 'Pre-migration text message', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # 2. Point app at legacy database and run init_db()
    monkeypatch.setattr(chat, "DATABASE", str(old_db))
    monkeypatch.setattr(chat, "UPLOAD_FOLDER", str(upload_dir))
    monkeypatch.setitem(chat.app.config, "PREFERRED_URL_SCHEME", "https")

    chat.init_db()

    # 3. Verify existing text message is preserved and attachment columns exist as NULL
    with chat.get_db() as db:
        old_msg = db.execute("SELECT * FROM messages WHERE id = 1").fetchone()
        assert old_msg["text"] == "Pre-migration text message"
        assert old_msg["attachment_filename"] is None
        assert old_msg["attachment_path"] is None
        assert old_msg["attachment_size"] is None
        assert old_msg["attachment_mime"] is None

    # 4. Repeated execution of init_db() is safe and idempotent
    chat.init_db()
    chat.init_db()

    # 5. New attachment upload functions normally on the migrated database
    client, _ = create_user(chat, "migrated_sender", "ms@example.com")
    resp = client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "legacy_bob",
            "text": "New attachment after migration",
            "file": (io.BytesIO(VALID_PNG), "migrated.png"),
        },
    )
    assert resp.status_code == 201
    assert resp.get_json()["attachment_filename"] == "migrated.png"


# ---------------------------------------------------------------------------
# 17. Malicious filename is sanitized safely without XSS or traversal
# ---------------------------------------------------------------------------
def test_malicious_filename_is_sanitized_safely(chat_env):
    chat_mod, _, upload_dir = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")
    bob_client, _ = create_user(chat_mod, "bob", "bob@example.com")

    malicious_filename = "../../../<script>alert('xss')</script>.png"
    resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(VALID_PNG), malicious_filename),
        },
    )
    assert resp.status_code == 201
    data = resp.get_json()
    safe_name = data["attachment_filename"]

    # Verify no script tags or traversal slashes in display filename
    assert "<script>" not in safe_name
    assert "/" not in safe_name
    assert "\\" not in safe_name
    assert safe_name.endswith(".png")

    # Verify download succeeds using sanitized name
    dl_resp = bob_client.get(f"/attachments/{data['id']}", base_url=SECURE)
    assert dl_resp.status_code == 200
    assert dl_resp.data == VALID_PNG


# ---------------------------------------------------------------------------
# 18. Inline image preview query parameter
# ---------------------------------------------------------------------------
def test_inline_image_preview_query_parameter(chat_env):
    chat_mod, _, _ = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")
    bob_client, _ = create_user(chat_mod, "bob", "bob@example.com")

    up_resp = alice_client.post(
        "/upload",
        base_url=SECURE,
        data={
            "recipient": "bob",
            "file": (io.BytesIO(VALID_PNG), "photo.png"),
        },
    )
    msg_id = up_resp.get_json()["id"]

    # With inline=1, image is served inline
    inline_resp = bob_client.get(f"/attachments/{msg_id}?inline=1", base_url=SECURE)
    assert inline_resp.status_code == 200
    assert inline_resp.headers.get("Content-Type") == "image/png"
    assert "attachment" not in inline_resp.headers.get("Content-Disposition", "")

    # Without inline=1, attachment disposition is used
    download_resp = bob_client.get(f"/attachments/{msg_id}", base_url=SECURE)
    assert download_resp.status_code == 200
    assert "attachment" in download_resp.headers.get("Content-Disposition", "")


# ---------------------------------------------------------------------------
# 19. Index page contains composer attachment controls
# ---------------------------------------------------------------------------
def test_index_page_contains_composer_attachment_controls(chat_env):
    chat_mod, _, _ = chat_env
    alice_client, _ = create_user(chat_mod, "alice", "alice@example.com")

    resp = alice_client.get("/", base_url=SECURE)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert 'id="attach-button"' in html
    assert 'id="file-input"' in html
    assert 'id="file-preview-bar"' in html
    assert 'id="file-remove-button"' in html
    assert 'accept=".png,.jpg,.jpeg,.gif,.webp,.pdf,.txt"' in html
