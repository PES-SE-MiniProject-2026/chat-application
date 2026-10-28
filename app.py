import os
import re
import ssl
import sqlite3
import threading
from contextlib import contextmanager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash
)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# Set CHAT_SECRET_KEY in your environment before deployment.
app.config["SECRET_KEY"] = os.environ.get(
    "CHAT_SECRET_KEY",
    DEFAULT_SECRET,
)

# Configuration (CHAT-SR-001: secure transport)
DEFAULT_SECRET = "development-only-change-this-key"
DEBUG = os.environ.get("CHAT_DEBUG", "1") == "1"
BEHIND_PROXY = os.environ.get("CHAT_BEHIND_PROXY", "0") == "1"
HTTPS_HOST = os.environ.get("CHAT_HOST", "localhost")
HTTPS_PORT = int(os.environ.get("CHAT_HTTPS_PORT", "5000"))
HTTP_REDIRECT_PORT = int(os.environ.get("CHAT_HTTP_PORT", "5080"))

CERT_FILE = os.environ.get(
    "CHAT_CERT_FILE",
    os.path.join(app.root_path, "certs", "cert.pem"),
)
KEY_FILE = os.environ.get(
    "CHAT_KEY_FILE",
    os.path.join(app.root_path, "certs", "key.pem"),
)
ALLOWED_ORIGINS = os.environ.get(
    "CHAT_ALLOWED_ORIGINS",
    f"https://localhost:{HTTPS_PORT},https://127.0.0.1:{HTTPS_PORT}",
).split(",")

app.config["SECRET_KEY"] = os.environ.get(
    "CHAT_SECRET_KEY",
    DEFAULT_SECRET,
)

if not DEBUG and app.config["SECRET_KEY"] == DEFAULT_SECRET:
    raise RuntimeError("Set CHAT_SECRET_KEY before running in production.")



app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# The login cookie is never sent over plain HTTP.
app.config["SESSION_COOKIE_SECURE"] = True

DATABASE = os.path.join(app.root_path, "users.db")

# Only pages served from these HTTPS origins may open a Socket.IO connection.
socketio = SocketIO(app, cors_allowed_origins=ALLOWED_ORIGINS)

# When running behind nginx or another TLS-terminating proxy, trust its
# X-Forwarded-Proto header so request.is_secure reflects the client's scheme.
# Applied after SocketIO() so it wraps the Socket.IO middleware too.
if BEHIND_PROXY:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Tracks connected Socket.IO clients for this single-process development server.
# For multiple workers/servers, use a shared presence store such as Redis.
connected_users = {}
sid_to_user = {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# HTTPS enforcement
# ---------------------------------------------------------------------------
@app.before_request
def enforce_https():
    """Redirect any plain-HTTP request to HTTPS."""
    if request.is_secure:
        return None
    return redirect(request.url.replace("http://", "https://", 1), code=301)


@app.after_request
def add_security_headers(response):
    if request.is_secure and not DEBUG:
        # Tell browsers to use HTTPS only for this site.
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.errorhandler(500)
def internal_error(_error):
    # Generic message only; details stay in the server log.
    return "Something went wrong. Please try again later.", 500


def start_http_redirector():
    """Dev only: listen on plain HTTP and send every request to HTTPS.

    The target host is fixed in config, not taken from the Host header,
    so it cannot be abused as an open redirect.
    """
    class Redirect(BaseHTTPRequestHandler):
        def _go(self):
            self.send_response(301)
            self.send_header(
                "Location", f"https://{HTTPS_HOST}:{HTTPS_PORT}{self.path}"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _go

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", HTTP_REDIRECT_PORT), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@contextmanager
def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 1000),
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                read_at TEXT,
                FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(recipient_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_recipient_delivery
            ON messages(recipient_id, delivered_at)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages(sender_id, recipient_id, id)
        """)


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


@app.route("/")
@login_required
def index():
    return render_template("index.html", username=session["username"])


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", username):
            flash(
                "Username must be 3–30 characters using letters, numbers or underscores.",
                "error",
            )
            return render_template("register.html")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid email address.", "error")
            return render_template("register.html")

        
        if len(password) < 12 or len(password) > 128:
            flash(
                "Password must be between 12 and 128 characters.",
                "error",
            )
            return render_template("register.html")

        common_passwords = {
            "password1234",
            "qwerty123456",
            "123456789012",
            "welcome12345",
        }

        if password.lower() in common_passwords:
            flash(
                "Choose a stronger password.",
                "error",
            )
            return render_template("register.html")


        password_hash = generate_password_hash(password)
        try:
            with get_db() as db:
                db.execute(
                    """INSERT INTO users (username, email, password_hash)
                       VALUES (?, ?, ?)""",
                    (username, email, password_hash),
                )
        except sqlite3.IntegrityError:
            flash("That username or email is already registered.", "error")
            return render_template("register.html")

        flash("Account created successfully. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        username_or_email = request.form.get("username_or_email", "").strip()
        password = request.form.get("password", "")
        with get_db() as db:
            user = db.execute(
                """SELECT id, username, password_hash FROM users
                   WHERE username = ? COLLATE NOCASE
                      OR email = ? COLLATE NOCASE""",
                (username_or_email, username_or_email),
            ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid username/email or password.", "error")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    user_id = session.get("user_id")

    # Disconnect every active Socket.IO connection for this user.
    if user_id is not None:
        active_sids = list(connected_users.get(user_id, set()))

        for sid in active_sids:
            socketio.server.disconnect(sid, namespace="/")

    # Clear the HTTP login session.
    session.clear()

    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------
def user_room(user_id):
    return f"user_{user_id}"


def message_payload(row):
    return {
        "id": row["id"],
        "sender_id": row["sender_id"],
        "recipient_id": row["recipient_id"],
        "username": row["sender_username"],
        "recipient_username": row["recipient_username"],
        "text": row["text"],
        "created_at": row["created_at"],
        "status": "read" if row["read_at"] else (
            "delivered" if row["delivered_at"] else "sent"
        ),
    }


def fetch_message(db, message_id):
    return db.execute("""
        SELECT m.*, s.username AS sender_username,
               r.username AS recipient_username
        FROM messages m
        JOIN users s ON s.id = m.sender_id
        JOIN users r ON r.id = m.recipient_id
        WHERE m.id = ?
    """, (message_id,)).fetchone()


def mark_message_delivered(message_id, sender_id, *args):
    # Mark delivered only after the client acknowledges receipt.
    if not args or not isinstance(args[0], dict):
        return

    if not args[0].get("received"):
        return

    with get_db() as db:
        row = fetch_message(db, message_id)

        if row is None or row["delivered_at"] is not None:
            return

        db.execute(
            """
            UPDATE messages
            SET delivered_at = ?
            WHERE id = ? AND delivered_at IS NULL
            """,
            (utc_now(), message_id),
        )

    # Emitted after the commit.
    socketio.emit(
        "message status",
        {"message_id": message_id, "status": "delivered"},
        to=user_room(sender_id),
    )


# ---------------------------------------------------------------------------
# Socket.IO events
# ---------------------------------------------------------------------------
@socketio.on_error_default
def handle_socket_error(error):
    # Log details on the server, send only a generic message to the client.
    app.logger.exception("Socket.IO handler error: %s", error)
    emit("chat error", {"message": "Something went wrong. Please try again."})


@socketio.on("connect")
def handle_connect(auth=None):
    # Reject any Socket.IO connection that is not over TLS (https/wss).
    if not request.is_secure:
        print("Rejected insecure Socket.IO connection")
        return False

    user_id = session.get("user_id")
    if not user_id:
        return False

    sid = request.sid
    sid_to_user[sid] = user_id
    join_room(user_room(user_id))
    connected_users.setdefault(user_id, set()).add(sid)
    print(f"Client connected: user_id={user_id}")

    # Collect messages saved while this recipient was offline.
    payloads = []
    with get_db() as db:
        pending = db.execute("""
            SELECT id FROM messages
            WHERE recipient_id = ? AND delivered_at IS NULL
            ORDER BY id
        """, (user_id,)).fetchall()

        for pending_row in pending:
            row = fetch_message(db, pending_row["id"])
            if row:
                payloads.append((row["id"], row["sender_id"], message_payload(row)))

    # Emit only after the DB block has closed.
    for mid, sender, payload in payloads:
        socketio.emit(
            "chat message",
            payload,
            to=sid,
            callback=lambda *args, mid=mid, sender=sender:
                mark_message_delivered(mid, sender, *args),
        )


@socketio.on("disconnect")
def handle_disconnect(reason=None):
    user_id = sid_to_user.pop(request.sid, None)

    if user_id is not None and user_id in connected_users:
        connected_users[user_id].discard(request.sid)

        if not connected_users[user_id]:
            connected_users.pop(user_id, None)

    print(f"Client disconnected: user_id={user_id}")


@socketio.on("chat message")
def handle_chat_message(data):
    sender_id = session.get("user_id")
    if not sender_id or not isinstance(data, dict):
        return

    recipient_username = str(data.get("recipient", "")).strip()
    message_text = str(data.get("text", "")).strip()
    if not recipient_username or not message_text or len(message_text) > 1000:
        emit("chat error", {"message": "Choose a recipient and enter a message (1–1000 characters)."})
        return

    with get_db() as db:
        recipient = db.execute(
            "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
            (recipient_username,),
        ).fetchone()

        if recipient is None:
            emit("chat error", {"message": "Recipient not found. Check the username."})
            return
        if recipient["id"] == sender_id:
            emit("chat error", {"message": "You cannot send a direct message to yourself."})
            return

        cursor = db.execute("""
            INSERT INTO messages
                (sender_id, recipient_id, text, created_at)
            VALUES (?, ?, ?, ?)
        """, (sender_id, recipient["id"], message_text, utc_now()))
        message_id = cursor.lastrowid
        payload = message_payload(fetch_message(db, message_id))
        recipient_id = recipient["id"]
    # The commit has happened here, so other connections can see the row.

    emit("message sent", payload)

    # A message is persisted regardless of recipient presence.
    for sid in list(connected_users.get(recipient_id, set())):
        socketio.emit(
            "chat message",
            payload,
            to=sid,
            callback=lambda *args, mid=message_id, sender=sender_id:
                mark_message_delivered(mid, sender, *args),
        )


@socketio.on("message read")
def handle_message_read(data):
    reader_id = session.get("user_id")
    print("READ EVENT RECEIVED:", data, "reader:", reader_id)

    if not reader_id or not isinstance(data, dict):
        return

    try:
        message_id = int(data.get("message_id"))
    except (TypeError, ValueError):
        return

    with get_db() as db:
        row = fetch_message(db, message_id)

        # Only the intended recipient may mark a message as read.
        if row is None or row["recipient_id"] != reader_id:
            return

        now = utc_now()

        db.execute(
            """
            UPDATE messages
            SET delivered_at = COALESCE(delivered_at, ?),
                read_at = COALESCE(read_at, ?)
            WHERE id = ?
            """,
            (now, now, message_id),
        )
        sender_id = row["sender_id"]

    # Emitted after the commit.
    emit(
        "message status",
        {"message_id": message_id, "status": "read"},
        to=user_room(sender_id),
    )


@socketio.on("typing")
def handle_typing(data):
    sender_id = session.get("user_id")
    if not sender_id or not isinstance(data, dict):
        return

    recipient_username = str(data.get("recipient", "")).strip()
    is_typing = bool(data.get("is_typing", False))
    with get_db() as db:
        recipient = db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            (recipient_username,),
        ).fetchone()

    if recipient and recipient["id"] != sender_id:
        emit(
            "typing",
            {"username": session["username"], "is_typing": is_typing},
            to=user_room(recipient["id"]),
        )


# ---------------------------------------------------------------------------
# Dev entry point (HTTPS). In deployment, use gunicorn behind a TLS proxy.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()

    if not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)):
        raise SystemExit(
            "TLS certificate not found. Create certs/cert.pem and certs/key.pem "
            "(see the openssl command) or set CHAT_CERT_FILE / CHAT_KEY_FILE."
        )

    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2  # no SSLv3 / TLS 1.0 / 1.1
    tls.load_cert_chain(CERT_FILE, KEY_FILE)

    start_http_redirector()

    socketio.run(
        app,
        host="127.0.0.1",
        port=HTTPS_PORT,
        debug=DEBUG,
        use_reloader=False,  # keeps the redirector from starting twice
        ssl_context=tls,
        allow_unsafe_werkzeug=True,
    )
