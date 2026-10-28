import pytest

from app import app, socketio


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["PREFERRED_URL_SCHEME"] = "https"  # test requests default to HTTPS
    return app.test_client()


def test_http_is_redirected_to_https(client):
    resp = client.get("/login", base_url="http://localhost")
    assert resp.status_code == 301
    assert resp.headers["Location"].startswith("https://")


def test_https_login_page_loads(client):
    resp = client.get("/login")
    assert resp.status_code == 200


def test_session_cookie_is_secure_and_httponly():
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True


def test_insecure_socket_connection_rejected():
    insecure = socketio.test_client(app)  # connects over plain HTTP
    assert not insecure.is_connected()