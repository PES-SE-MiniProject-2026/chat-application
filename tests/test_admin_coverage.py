
import pytest
import app as chat

SECURE = "https://localhost"
PASSWORD = "A-Strong-Test-Password-123!"


@pytest.fixture
def admin_app(tmp_path, monkeypatch):
    db_file = tmp_path / "admin_coverage_test.db"
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


def create_user(module, username, email, role="user", status="active"):
    client = module.app.test_client()

    client.post("/register", base_url=SECURE, data={
        "username": username,
        "email": email,
        "password": PASSWORD,
    })

    with module.get_db() as db:
        db.execute(
            "UPDATE users SET role = ?, account_status = ? WHERE username = ?",
            (role, status, username),
        )
        user = db.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()

    client.post("/login", base_url=SECURE, data={
        "username_or_email": username,
        "password": PASSWORD,
    })

    return client, user["id"]


def test_admin_dashboard_requires_login(admin_app):
    response = admin_app.app.test_client().get("/admin", base_url=SECURE)
    assert response.status_code == 302


def test_admin_dashboard_rejects_normal_user(admin_app):
    client, _ = create_user(admin_app, "normaluser", "normal@example.com")

    response = client.get("/admin", base_url=SECURE)
    assert response.status_code == 403


def test_admin_dashboard_rejects_inactive_admin(admin_app):
    client, user_id = create_user(
        admin_app, "inactiveadmin", "inactive@example.com",
        role="admin", status="suspended"
    )

    # Log in before suspending the account.
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["username"] = "inactiveadmin"

    response = client.get("/admin", base_url=SECURE)
    assert response.status_code == 302

    with client.session_transaction() as session:
        assert "user_id" not in session


def test_admin_dashboard_loads_for_admin(admin_app):
    client, _ = create_user(
        admin_app, "siteadmin", "admin@example.com", role="admin"
    )

    response = client.get("/admin", base_url=SECURE)
    assert response.status_code == 200


def test_update_user_status_success(admin_app):
    admin, _ = create_user(
        admin_app, "admin1", "admin1@example.com", role="admin"
    )
    _, target_id = create_user(
        admin_app, "target1", "target1@example.com"
    )

    response = admin.post(
        f"/admin/users/{target_id}/status",
        base_url=SECURE,
        data={"status": "suspended"},
    )

    assert response.status_code == 302

    with admin_app.get_db() as db:
        user = db.execute(
            "SELECT account_status FROM users WHERE id = ?", (target_id,)
        ).fetchone()

    assert user["account_status"] == "suspended"


@pytest.mark.parametrize("status", ["", "invalid", "deleted"])
def test_update_user_status_rejects_invalid_status(admin_app, status):
    admin, _ = create_user(
        admin_app, "admin2", "admin2@example.com", role="admin"
    )
    _, target_id = create_user(
        admin_app, "target2", "target2@example.com"
    )

    response = admin.post(
        f"/admin/users/{target_id}/status",
        base_url=SECURE,
        data={"status": status},
    )

    assert response.status_code == 302

    with admin_app.get_db() as db:
        user = db.execute(
            "SELECT account_status FROM users WHERE id = ?", (target_id,)
        ).fetchone()

    assert user["account_status"] == "active"


def test_admin_cannot_change_own_status(admin_app):
    admin, admin_id = create_user(
        admin_app, "admin3", "admin3@example.com", role="admin"
    )

    response = admin.post(
        f"/admin/users/{admin_id}/status",
        base_url=SECURE,
        data={"status": "banned"},
    )

    assert response.status_code == 302

    with admin_app.get_db() as db:
        user = db.execute(
            "SELECT account_status FROM users WHERE id = ?", (admin_id,)
        ).fetchone()

    assert user["account_status"] == "active"


def test_update_status_rejects_missing_user(admin_app):
    admin, _ = create_user(
        admin_app, "admin4", "admin4@example.com", role="admin"
    )

    response = admin.post(
        "/admin/users/99999/status",
        base_url=SECURE,
        data={"status": "banned"},
    )

    assert response.status_code == 302


def test_admin_cannot_moderate_another_admin(admin_app):
    admin, _ = create_user(
        admin_app, "admin5", "admin5@example.com", role="admin"
    )
    _, other_admin_id = create_user(
        admin_app, "admin6", "admin6@example.com", role="admin"
    )

    response = admin.post(
        f"/admin/users/{other_admin_id}/status",
        base_url=SECURE,
        data={"status": "suspended"},
    )

    assert response.status_code == 302

    with admin_app.get_db() as db:
        user = db.execute(
            "SELECT account_status FROM users WHERE id = ?",
            (other_admin_id,),
        ).fetchone()

    assert user["account_status"] == "active"


def test_announcement_rejects_empty_message(admin_app):
    admin, _ = create_user(
        admin_app, "admin7", "admin7@example.com", role="admin"
    )

    response = admin.post(
        "/admin/announcements",
        base_url=SECURE,
        data={"announcement": "   "},
    )

    assert response.status_code == 302


def test_announcement_rejects_message_over_1000_characters(admin_app):
    admin, _ = create_user(
        admin_app, "admin8", "admin8@example.com", role="admin"
    )

    response = admin.post(
        "/admin/announcements",
        base_url=SECURE,
        data={"announcement": "A" * 1001},
    )

    assert response.status_code == 302


def test_announcement_broadcasts_valid_message(admin_app):
    admin, _ = create_user(
        admin_app, "admin9", "admin9@example.com", role="admin"
    )

    emitted = []
    original_emit = admin_app.socketio.emit

    def capture_emit(event, data=None, *args, **kwargs):
        emitted.append((event, data))
        return original_emit(event, data, *args, **kwargs)

    admin_app.socketio.emit = capture_emit

    response = admin.post(
        "/admin/announcements",
        base_url=SECURE,
        data={"announcement": "Server maintenance at 10 PM"},
    )

    assert response.status_code == 302
    assert any(
        event == "system announcement"
        and data["message"] == "Server maintenance at 10 PM"
        for event, data in emitted
    )
