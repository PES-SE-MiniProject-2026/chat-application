
import sqlite3

import app as chat_app
from werkzeug.security import check_password_hash


def setup_database(tmp_path, monkeypatch):
    database_path = tmp_path / "test_users.db"
    monkeypatch.setattr(chat_app, "DATABASE", str(database_path))
    chat_app.init_db()
    chat_app.app.config["TESTING"] = True
    return chat_app.app.test_client(), database_path


def test_password_is_hashed_on_registration(tmp_path, monkeypatch):
    client, database_path = setup_database(tmp_path, monkeypatch)

    password = "A-Strong-Test-Password-123!"

    response = client.post(
        "/register",
        data={
            "username": "test_user",
            "email": "test@example.com",
            "password": password,
        },
        base_url="https://localhost",
    )

    assert response.status_code == 302

    with sqlite3.connect(database_path) as db:
        row = db.execute(
            "SELECT password_hash FROM users WHERE username = ?",
            ("test_user",),
        ).fetchone()

    assert row is not None
    stored_hash = row[0]
    assert stored_hash != password
    assert check_password_hash(stored_hash, password)


def test_login_and_logout(tmp_path, monkeypatch):
    client, _ = setup_database(tmp_path, monkeypatch)

    client.post(
        "/register",
        data={
            "username": "test_user",
            "email": "test@example.com",
            "password": "A-Strong-Test-Password-123!",
        },
        base_url="https://localhost",
    )

    response = client.post(
        "/login",
        data={
            "username_or_email": "test_user",
            "password": "A-Strong-Test-Password-123!",
        },
        base_url="https://localhost",
    )

    assert response.status_code == 302

    response = client.get(
        "/", base_url="https://localhost"
    )
    assert response.status_code == 200

    response = client.post(
        "/logout", base_url="https://localhost"
    )
    assert response.status_code == 302

    response = client.get(
        "/", base_url="https://localhost"
    )
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_wrong_password_is_rejected(tmp_path, monkeypatch):
    client, _ = setup_database(tmp_path, monkeypatch)

    client.post(
        "/register",
        data={
            "username": "test_user",
            "email": "test@example.com",
            "password": "A-Strong-Test-Password-123!",
        },
        base_url="https://localhost",
    )

    response = client.post(
        "/login",
        data={
            "username_or_email": "test_user",
            "password": "Incorrect-Password-123!",
        },
        base_url="https://localhost",
    )

    assert response.status_code == 200
    assert b"Invalid username/email or password." in response.data

    response = client.get(
        "/", base_url="https://localhost"
    )
    assert response.status_code == 302
