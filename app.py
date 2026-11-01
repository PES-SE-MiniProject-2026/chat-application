import os
import re
import ssl
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash,
    jsonify, send_from_directory, abort,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import (
    SocketIO, emit, join_room, ConnectionRefusedError,
)
from itsdangerous import URLSafeTimedSerializer, BadSignature
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)


# Set CHAT_SECRET_KEY in your environment before deployment.
DEFAULT_SECRET = "development-only-change-this-key"

app.config["SECRET_KEY"] = os.environ.get(
    "CHAT_SECRET_KEY",
    DEFAULT_SECRET,
)

# Configuration (CHAT-SR-001: secure transport)

DEBUG = os.environ.get("CHAT_DEBUG", "1") == "1"
BEHIND_PROXY = os.environ.get("CHAT_BEHIND_PROXY", "0") == "1"
HTTPS_HOST = os.environ.get("CHAT_HOST", "127.0.0.1")
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
    f"https://127.0.0.1:{HTTPS_PORT}",
).split(",")

if not DEBUG and app.config["SECRET_KEY"] == DEFAULT_SECRET:
    raise RuntimeError("Set CHAT_SECRET_KEY before running in production.")


app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# The login cookie is never sent over plain HTTP.
app.config["SESSION_COOKIE_SECURE"] = True

# Maximum request size (5 MiB for file uploads)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

UPLOAD_FOLDER = os.environ.get(
    "CHAT_UPLOAD_FOLDER",
    os.path.join(app.root_path, "uploads"),
)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

DATABASE = os.path.join(app.root_path, "users.db")

# ---------------------------------------------------------------------------
# WebSocket authentication tokens (CHAT-SR-003)
# ---------------------------------------------------------------------------
# A short-lived signed token is issued to logged-in users by /ws-token and
# must be presented in the Socket.IO handshake. The client fetches a fresh
# token on every connect/reconnect, so a short lifetime is enough.
WS_TOKEN_MAX_AGE = 60  # seconds
ws_serializer = URLSafeTimedSerializer(
    app.config["SECRET_KEY"],
    salt="chat-ws-auth",
)


def issue_ws_token(user_id):
    return ws_serializer.dumps({"uid": user_id})


def verify_ws_token(token):
    """Return the user id inside a valid, unexpired token, otherwise None."""
    if not isinstance(token, str) or not token or len(token) > 512:
        return None
    try:
        data = ws_serializer.loads(token, max_age=WS_TOKEN_MAX_AGE)
    except BadSignature:  # also covers SignatureExpired
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return uid if isinstance(uid, int) else None


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

MESSAGE_DELETE_WINDOW_MINUTES = 15

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


@app.errorhandler(RequestEntityTooLarge)
@app.errorhandler(413)
def request_entity_too_large(_error):
    if request.path.startswith("/upload") or request.is_json or "application/json" in request.headers.get("Accept", ""):
        return jsonify(error="File exceeds maximum allowed size (5 MiB)."), 413
    return "File exceeds maximum allowed size (5 MiB).", 413


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
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT
            )
        """)
        columns = [
            row["name"]
            for row in db.execute("PRAGMA table_info(users)").fetchall()
        ]

        if "account_status" not in columns:
            db.execute("""
                ALTER TABLE users
                ADD COLUMN account_status TEXT NOT NULL DEFAULT 'active'
            """)

        # Safe migration: add last_seen if users table already existed without it
        user_columns = [
            row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
        ]
                # Safe migration: add last_seen if missing
        if "last_seen" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")

        # Safe migrations for administration and user moderation
        if "role" not in user_columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"
            )

        if "account_status" not in user_columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN account_status TEXT NOT NULL DEFAULT 'active'"
            )

                # Promote the configured local administrator account
        admin_username = os.environ.get("CHAT_ADMIN_USERNAME", "").strip()

        if admin_username:
            db.execute(
                "UPDATE users SET role = 'admin' WHERE username = ? COLLATE NOCASE",
                (admin_username,)
            )

        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 1000),
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                read_at TEXT,
                attachment_filename TEXT,
                attachment_path TEXT,
                attachment_size INTEGER,
                attachment_mime TEXT,
                FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(recipient_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Safe migration: add attachment columns if messages table already existed without them
        msg_columns = [
            row["name"] for row in db.execute("PRAGMA table_info(messages)").fetchall()
        ]
        if "attachment_filename" not in msg_columns:
            db.execute("ALTER TABLE messages ADD COLUMN attachment_filename TEXT")
        if "attachment_path" not in msg_columns:
            db.execute("ALTER TABLE messages ADD COLUMN attachment_path TEXT")
        if "attachment_size" not in msg_columns:
            db.execute("ALTER TABLE messages ADD COLUMN attachment_size INTEGER")
        if "attachment_mime" not in msg_columns:
            db.execute("ALTER TABLE messages ADD COLUMN attachment_mime TEXT")  
        # Safe migration: add deleted_at for soft message deletion
        if "deleted_at" not in msg_columns:
            db.execute(
                "ALTER TABLE messages ADD COLUMN deleted_at TEXT"
            )


        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_recipient_delivery
            ON messages(recipient_id, delivered_at)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages(sender_id, recipient_id, id)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_unread
            ON messages(recipient_id, sender_id, read_at)
        """)
                # Group chat tables
        db.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(created_by) REFERENCES users(id)
                    ON DELETE CASCADE
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS group_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                UNIQUE(group_id, user_id),
                FOREIGN KEY(group_id) REFERENCES groups(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id)
                    ON DELETE CASCADE
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_group_members_user
            ON group_members(user_id, group_id)
        """)

        # Group messages table
        db.execute("""
            CREATE TABLE IF NOT EXISTS group_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 1000),
                created_at TEXT NOT NULL,
                deleted_at TEXT,
                FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE,
                FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Add deleted_at to existing group_messages tables
        columns = [
            row["name"]
            for row in db.execute("PRAGMA table_info(group_messages)").fetchall()
        ]

        if "deleted_at" not in columns:
            db.execute("ALTER TABLE group_messages ADD COLUMN deleted_at TEXT")

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_group_messages_history
            ON group_messages(group_id, id)
        """)


# ---------------------------------------------------------------------------
# File attachment validation & helpers
# ---------------------------------------------------------------------------
ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
}


def validate_attachment(file_storage):
    """Validate uploaded file type, size, extension, and content signatures.

    Returns: (is_valid, error_message, ext, canonical_mime, content_bytes)
    """
    if not file_storage or not file_storage.filename:
        return False, "No file selected.", None, None, None

    filename = file_storage.filename
    _, ext = os.path.splitext(filename)
    ext = ext.lower()

    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        return False, f"Unsupported file type '{ext}'. Allowed types: PNG, JPEG, GIF, WebP, PDF, TXT.", None, None, None

    canonical_mime = ALLOWED_ATTACHMENT_EXTENSIONS[ext]

    content = file_storage.read()
    if len(content) == 0:
        return False, "Uploaded file is empty.", None, None, None

    if len(content) > 5 * 1024 * 1024:
        return False, "File exceeds maximum allowed size (5 MiB).", None, None, None

    # Binary magic bytes & format signature verification
    if ext == ".png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            return False, "File content does not match PNG signature.", None, None, None
    elif ext in (".jpg", ".jpeg"):
        if not content.startswith(b"\xff\xd8\xff"):
            return False, "File content does not match JPEG signature.", None, None, None
    elif ext == ".gif":
        if not (content.startswith(b"GIF87a") or content.startswith(b"GIF89a")):
            return False, "File content does not match GIF signature.", None, None, None
    elif ext == ".webp":
        if len(content) < 12 or not content.startswith(b"RIFF") or content[8:12] != b"WEBP":
            return False, "File content does not match WebP signature.", None, None, None
    elif ext == ".pdf":
        if not content.startswith(b"%PDF-"):
            return False, "File content does not match PDF signature.", None, None, None
    elif ext == ".txt":
        if b"\x00" in content:
            return False, "Text file contains NUL bytes.", None, None, None
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            return False, "Text file is not valid UTF-8.", None, None, None
        for ch in decoded:
            code = ord(ch)
            if (code < 32 and code not in (9, 10, 13)) or code == 127:
                return False, "Text file contains disallowed control characters.", None, None, None

    return True, "", ext, canonical_mime, content


def sanitize_display_filename(filename, ext):
    """Normalize display filename to a safe basename, remove control characters, and cap length."""
    raw = os.path.basename(filename)
    clean = re.sub(r"[\x00-\x1f\x7f]", "", raw).strip()
    safe = secure_filename(clean)
    if not safe or safe == ext.lstrip("."):
        safe = f"attachment_{uuid.uuid4().hex[:8]}{ext}"
    if len(safe) > 200:
        base, extension = os.path.splitext(safe)
        safe = base[: 200 - len(extension)] + extension
    return safe


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

def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))

        with get_db() as db:
            user = db.execute(
                "SELECT role, account_status FROM users WHERE id = ?",
                (session["user_id"],)
            ).fetchone()

        if user is None or user["account_status"] != "active":
            session.clear()
            flash("Your account is not active.", "error")
            return redirect(url_for("login"))

        if user["role"] != "admin":
            abort(403)

        return view(*args, **kwargs)

    return wrapped_view

@app.route("/")
@login_required
def index():
    with get_db() as db:
        user = db.execute(
            "SELECT role FROM users WHERE id = ?",
            (session["user_id"],)
        ).fetchone()

    return render_template(
        "index.html",
        username=session["username"],
        is_admin=user is not None and user["role"] == "admin"
    )

@app.route("/ws-token")
@limiter.limit("30 per minute")
def ws_token():
    """Issue a short-lived WebSocket handshake token to a logged-in user."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify(error="unauthorized"), 401

    response = jsonify(token=issue_ws_token(user_id))
    response.headers["Cache-Control"] = "no-store"
    return response


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
                """SELECT id, username, password_hash, account_status FROM users
                   WHERE username = ? COLLATE NOCASE
                      OR email = ? COLLATE NOCASE""",
                (username_or_email, username_or_email),
            ).fetchone()



        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid username/email or password.", "error")
            return render_template("login.html")

        session.clear()
        if user["account_status"] != "active":
            return jsonify({
                "success": False,
                "message": "Your account is suspended or banned. Contact an administrator."
            }), 403
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

@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    with get_db() as db:
        users = db.execute(
            """
            SELECT id, username, email, role, account_status, created_at
            FROM users
            ORDER BY username
            """
        ).fetchall()

    return render_template("admin.html", users=users)

@app.route("/admin/users/<int:user_id>/status", methods=["POST"])
@login_required
@admin_required
def update_user_status(user_id):
    new_status = request.form.get("status", "").strip().lower()

    if new_status not in ("active", "suspended", "banned"):
        flash("Invalid account status.", "error")
        return redirect(url_for("admin_dashboard"))

    if user_id == session["user_id"]:
        flash("You cannot change your own account status.", "error")
        return redirect(url_for("admin_dashboard"))

    with get_db() as db:
        target = db.execute(
            "SELECT id, role FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        if target is None:
            flash("User not found.", "error")
            return redirect(url_for("admin_dashboard"))

        if target["role"] == "admin":
            flash("You cannot moderate another administrator.", "error")
            return redirect(url_for("admin_dashboard"))

        db.execute(
            "UPDATE users SET account_status = ? WHERE id = ?",
            (new_status, user_id)
        )

    if new_status in ("suspended", "banned"):
        active_sids = list(connected_users.get(user_id, set()))

        for sid in active_sids:
            socketio.server.disconnect(sid, namespace="/")

    flash(f"User account status updated to {new_status}.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/announcements", methods=["POST"])
@login_required
@admin_required
def broadcast_announcement():
    message = request.form.get("announcement", "").strip()

    if not message:
        flash("Announcement cannot be empty.", "error")
        return redirect(url_for("admin_dashboard"))

    if len(message) > 1000:
        flash("Announcement cannot exceed 1000 characters.", "error")
        return redirect(url_for("admin_dashboard"))

    announcement = {
        "message": message,
        "sender": session["username"],
        "created_at": utc_now()
    }

    socketio.emit("system announcement", announcement)

    flash("Announcement broadcast successfully.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    sender_id = session.get("user_id")
    if not sender_id:
        return jsonify(error="Unauthorized"), 401

    recipient_username = request.form.get("recipient", "").strip()
    if not recipient_username:
        return jsonify(error="Recipient is required."), 400

    with get_db() as db:
        recipient = db.execute(
            "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
            (recipient_username,),
        ).fetchone()

    if recipient is None:
        return jsonify(error="Recipient not found."), 404

    if recipient["id"] == sender_id:
        return jsonify(error="You cannot send an attachment to yourself."), 400

    caption = request.form.get("text", "").strip()
    if caption and len(caption) > 1000:
        return jsonify(error="Caption must not exceed 1000 characters."), 400

    if "file" not in request.files:
        return jsonify(error="No file selected."), 400

    file_storage = request.files["file"]
    is_valid, err_msg, ext, mime_type, content = validate_attachment(file_storage)
    if not is_valid:
        if "exceeds" in err_msg.lower():
            return jsonify(error=err_msg), 413
        return jsonify(error=err_msg), 400

    display_name = sanitize_display_filename(file_storage.filename, ext)
    message_text = caption if caption else display_name

    storage_name = f"{uuid.uuid4().hex}{ext}"
    target_folder = UPLOAD_FOLDER
    os.makedirs(target_folder, exist_ok=True)
    file_path = os.path.join(target_folder, storage_name)
    file_written = False

    try:
        with open(file_path, "wb") as f:
            f.write(content)
        file_written = True

        with get_db() as db:
            cursor = db.execute("""
                INSERT INTO messages (
                    sender_id, recipient_id, text, created_at,
                    attachment_filename, attachment_path, attachment_size, attachment_mime
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sender_id, recipient["id"], message_text, utc_now(),
                display_name, storage_name, len(content), mime_type,
            ))
            message_id = cursor.lastrowid
            row = fetch_message(db, message_id)
            payload = message_payload(row)
            recipient_id = recipient["id"]
            recipient_unread = get_unread_counts(db, recipient_id)
    except Exception as exc:
        if file_written and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        app.logger.exception("Upload processing error: %s", exc)
        return jsonify(error="Failed to process file upload."), 500

    # Real-time notifications
    socketio.emit("message sent", payload, to=user_room(sender_id))
    socketio.emit("unread counts", recipient_unread, to=user_room(recipient_id))

    for sid in list(connected_users.get(recipient_id, set())):
        socketio.emit(
            "chat message",
            payload,
            to=sid,
            callback=lambda *args, mid=message_id, sender=sender_id:
                mark_message_delivered(mid, sender, *args),
        )

    return jsonify(payload), 201


@app.route("/attachments/<int:message_id>", methods=["GET"])
@login_required
def download_attachment(message_id):
    current_user_id = session.get("user_id")
    if not current_user_id:
        return jsonify(error="Unauthorized"), 401

    with get_db() as db:
        msg = fetch_message(db, message_id)

    if msg is None or not msg["attachment_path"]:
        return jsonify(error="Attachment not found."), 404

    if current_user_id != msg["sender_id"] and current_user_id != msg["recipient_id"]:
        return jsonify(error="Forbidden. You do not have permission to access this attachment."), 403

    storage_filename = os.path.basename(msg["attachment_path"])
    abs_upload_folder = os.path.abspath(UPLOAD_FOLDER)
    target_path = os.path.abspath(os.path.join(abs_upload_folder, storage_filename))

    # Path traversal protection
    if not target_path.startswith(abs_upload_folder) or not os.path.isfile(target_path):
        return jsonify(error="Attachment file not found on server."), 404

    download_name = msg["attachment_filename"] or f"attachment_{message_id}"
    is_inline = request.args.get("inline") == "1" and (msg["attachment_mime"] or "").startswith("image/")

    response = send_from_directory(
        abs_upload_folder,
        storage_filename,
        mimetype=msg["attachment_mime"],
        as_attachment=not is_inline,
        download_name=download_name,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'"
    return response


# ---------------------------------------------------------------------------
# Message & presence helpers
# ---------------------------------------------------------------------------
def user_room(user_id):
    return f"user_{user_id}"


def get_unread_counts(db, recipient_id):
    """Return unread message counts for recipient_id grouped by sender username."""
    rows = db.execute("""
        SELECT s.username, COUNT(m.id) AS count
        FROM messages m
        JOIN users s ON s.id = m.sender_id
        WHERE m.recipient_id = ? AND m.read_at IS NULL
        GROUP BY s.username
    """, (recipient_id,)).fetchall()
    counts = {row["username"]: row["count"] for row in rows}
    return {
        "counts": counts,
        "total": sum(counts.values()),
    }


def get_missed_messages_summary(db, recipient_id):
    """Return undelivered message counts for recipient_id grouped by sender username."""
    rows = db.execute("""
        SELECT s.username, COUNT(m.id) AS count
        FROM messages m
        JOIN users s ON s.id = m.sender_id
        WHERE m.recipient_id = ? AND m.delivered_at IS NULL
        GROUP BY s.username
    """, (recipient_id,)).fetchall()
    senders = {row["username"]: row["count"] for row in rows}
    return {
        "total": sum(senders.values()),
        "senders": senders,
    }


def message_payload(row):
    att_fn = None
    att_size = None
    att_mime = None
    try:
        att_fn = row["attachment_filename"]
        att_size = row["attachment_size"]
        att_mime = row["attachment_mime"]
    except (IndexError, KeyError):
        pass

    has_attachment = bool(att_fn)
    payload = {
        "id": row["id"],
        "sender_id": row["sender_id"],
        "recipient_id": row["recipient_id"],
        "username": row["sender_username"],
        "recipient_username": row["recipient_username"],
        "text": row["text"],
        "created_at": row["created_at"],
        "deleted_at": row["deleted_at"],
        "status": "read" if row["read_at"] else (
            "delivered" if row["delivered_at"] else "sent"
        ),
        "attachment_filename": att_fn if has_attachment else None,
        "attachment_size": att_size if has_attachment else None,
        "attachment_mime": att_mime if has_attachment else None,
        "attachment_url": f"/attachments/{row['id']}" if has_attachment else None,
    }
    if has_attachment:
        payload["attachment"] = {
            "filename": att_fn,
            "size": att_size,
            "mime": att_mime,
            "url": f"/attachments/{row['id']}",
        }
    return payload


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

    # CHAT-SR-003: a valid, unexpired token is required for the handshake,
    # and it must belong to the currently logged-in session.
    token = auth.get("token") if isinstance(auth, dict) else None
    token_uid = verify_ws_token(token)
    session_uid = session.get("user_id")

    if token_uid is None or session_uid is None or token_uid != session_uid:
        app.logger.warning("Rejected unauthenticated Socket.IO handshake")
        # Generic reason only; no detail about why it failed.
        raise ConnectionRefusedError("unauthorized")

    with get_db() as db:
        user = db.execute(
            "SELECT id, username, account_status FROM users WHERE id = ?",
            (token_uid,)
        ).fetchone()

    if user is None or user["account_status"] != "active":
        app.logger.warning("Rejected Socket.IO handshake for unavailable account")
        raise ConnectionRefusedError("unauthorized")
        raise ConnectionRefusedError("unauthorized")

    # Associate this connection with the verified user.
    user_id = token_uid
    sid = request.sid
    username = user["username"]

    is_first_connection = len(connected_users.get(user_id, set())) == 0

    sid_to_user[sid] = user_id
    join_room(user_room(user_id))
    connected_users.setdefault(user_id, set()).add(sid)
    print(f"Client connected: user_id={user_id}")

    # Presence: if this is the user's first active socket connection,
    # set last_seen to NULL and broadcast an online presence update.
    if is_first_connection:
        with get_db() as db:
            db.execute("UPDATE users SET last_seen = NULL WHERE id = ?", (user_id,))
        socketio.emit(
            "presence update",
            {"username": username, "is_online": True, "last_seen": None},
        )

    # Missed messages summary and initial unread counts for this connecting socket.
    with get_db() as db:
        missed_summary = get_missed_messages_summary(db, user_id)
        unread_summary = get_unread_counts(db, user_id)

    if missed_summary["total"] > 0:
        socketio.emit("missed messages", missed_summary, to=sid)

    socketio.emit("unread counts", unread_summary, to=sid)

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

        # Broadcast offline transition only when the user's last socket disconnects.
        if not connected_users[user_id]:
            connected_users.pop(user_id, None)
            now = utc_now()
            with get_db() as db:
                user = db.execute(
                    "SELECT username FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                db.execute(
                    "UPDATE users SET last_seen = ? WHERE id = ?", (now, user_id)
                )
            if user:
                socketio.emit(
                    "presence update",
                    {
                        "username": user["username"],
                        "is_online": False,
                        "last_seen": now,
                    },
                )

    print(f"Client disconnected: user_id={user_id}")


@socketio.on("get presence")
def handle_get_presence(data):
    requester_id = sid_to_user.get(request.sid)
    if not requester_id:
        emit("chat error", {"message": "Unauthorized."})
        return

    if not isinstance(data, dict):
        emit("chat error", {"message": "Invalid presence request."})
        return

    target_username = str(data.get("username", "")).strip()
    if not target_username:
        emit("chat error", {"message": "Username is required."})
        return

    with get_db() as db:
        target = db.execute(
            "SELECT id, username, last_seen FROM users WHERE username = ? COLLATE NOCASE",
            (target_username,),
        ).fetchone()

    if target is None:
        emit("chat error", {"message": "User not found."})
        return

    is_online = bool(connected_users.get(target["id"]))
    last_seen = None if is_online else target["last_seen"]

    emit(
        "presence update",
        {
            "username": target["username"],
            "is_online": is_online,
            "last_seen": last_seen,
        },
    )


@socketio.on("chat message")
def handle_chat_message(data):
    # Identity comes from the authenticated connection, never the payload.
    sender_id = sid_to_user.get(request.sid)
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
        recipient_unread = get_unread_counts(db, recipient_id)
    # The commit has happened here, so other connections can see the row.

    emit("message sent", payload)
    socketio.emit("unread counts", recipient_unread, to=user_room(recipient_id))

    # A message is persisted regardless of recipient presence.
    for sid in list(connected_users.get(recipient_id, set())):
        socketio.emit(
            "chat message",
            payload,
            to=sid,
            callback=lambda *args, mid=message_id, sender=sender_id:
                mark_message_delivered(mid, sender, *args),
        )


@socketio.on("load chat history")
def handle_load_chat_history(data):
    # Use the authenticated connection to identify the requester.
    requester_id = sid_to_user.get(request.sid)

    if not requester_id or not isinstance(data, dict):
        emit("chat error", {"message": "Unauthorized history request."})
        return

    recipient_username = str(data.get("recipient", "")).strip()

    if not recipient_username:
        emit("chat error", {"message": "Recipient is required."})
        return

    with get_db() as db:
        recipient = db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            (recipient_username,)
        ).fetchone()

        if recipient is None or recipient["id"] == requester_id:
            emit("chat error", {"message": "Invalid recipient."})
            return

        # Retrieve only messages exchanged between these two users.
        rows = db.execute("""
            SELECT m.*, s.username AS sender_username,
                   r.username AS recipient_username
            FROM messages m
            JOIN users s ON s.id = m.sender_id
            JOIN users r ON r.id = m.recipient_id
            WHERE
            (
                (m.sender_id = ? AND m.recipient_id = ?)
                OR
                (m.sender_id = ? AND m.recipient_id = ?)
            )
            AND m.deleted_at IS NULL
            ORDER BY m.id ASC
        """, (
            requester_id,
            recipient["id"],
            recipient["id"],
            requester_id
        )).fetchall()

        history = [message_payload(row) for row in rows]

    emit("chat history", {
        "recipient": recipient_username,
        "messages": history
    })

@socketio.on("create group")
def handle_create_group(data):
    creator_id = sid_to_user.get(request.sid)

    if not creator_id or not isinstance(data, dict):
        emit("chat error", {"message": "Unauthorized."})
        return

    group_name = str(data.get("name", "")).strip()
    member_usernames = data.get("members", [])

    if not group_name or len(group_name) > 50:
        emit("chat error", {"message": "Group name must be 1–50 characters."})
        return

    if not isinstance(member_usernames, list) or len(member_usernames) > 49:
        emit("chat error", {"message": "Invalid group members."})
        return

    member_usernames = list({
        str(name).strip()
        for name in member_usernames
        if isinstance(name, str) and name.strip()
    })
    print("DEBUG - Received group members:", member_usernames)

    with get_db() as db:
        creator = db.execute(
            "SELECT id, username FROM users WHERE id = ?",
            (creator_id,)
        ).fetchone()

        if creator is None:
            emit("chat error", {"message": "User not found."})
            return

        placeholders = ",".join("?" for _ in member_usernames)

        if member_usernames:
            members = db.execute(
                f"""
                SELECT id, username FROM users
                WHERE username COLLATE NOCASE IN ({placeholders})
                """,
                member_usernames
            ).fetchall()
            print("DEBUG - Matched database members:", [dict(m) for m in members])
        else:
            members = []

        if len(members) != len(member_usernames):
            emit("chat error", {"message": "One or more members were not found."})
            return

        all_members = {creator["id"]: creator["username"]}

        for member in members:
            all_members[member["id"]] = member["username"]

        cursor = db.execute(
            """
            INSERT INTO groups (name, created_by, created_at)
            VALUES (?, ?, ?)
            """,
            (group_name, creator_id, utc_now())
        )

        group_id = cursor.lastrowid

        for member_id in all_members:
            db.execute(
                """
                INSERT INTO group_members
                    (group_id, user_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (group_id, member_id, utc_now())
            )

    group_payload = {
        "group_id": group_id,
        "name": group_name,
        "created_by": creator_id,
        "members": list(all_members.values())
    }

    for member_id in all_members:
        socketio.emit(
            "group created",
            group_payload,
            to=user_room(member_id)
        )

@socketio.on("add group member")
def handle_add_group_member(data):
    requester_id = sid_to_user.get(request.sid)

    if not requester_id or not isinstance(data, dict):
        emit("chat error", {"message": "Unauthorized."})
        return

    try:
        group_id = int(data.get("group_id"))
    except (TypeError, ValueError):
        emit("chat error", {"message": "Invalid group."})
        return

    username = str(data.get("username", "")).strip()

    if not username:
        emit("chat error", {"message": "Username is required."})
        return

    with get_db() as db:
        group = db.execute(
            "SELECT id, name, created_by FROM groups WHERE id = ?",
            (group_id,)
        ).fetchone()

        if group is None:
            emit("chat error", {"message": "Group not found."})
            return

        if group["created_by"] != requester_id:
            emit("chat error", {"message": "Only the group admin can add members."})
            return

        user = db.execute(
            "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
            (username,)
        ).fetchone()

        if user is None:
            emit("chat error", {"message": "User not found."})
            return

        existing = db.execute(
            """
            SELECT 1 FROM group_members
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user["id"])
        ).fetchone()

        if existing:
            emit("chat error", {"message": "User is already a group member."})
            return

        db.execute(
            """
            INSERT INTO group_members (group_id, user_id, joined_at)
            VALUES (?, ?, ?)
            """,
            (group_id, user["id"], utc_now())
        )

        member_rows = db.execute(
            """
            SELECT u.id, u.username
            FROM group_members gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = ?
            """,
            (group_id,)
        ).fetchall()

    payload = {
        "group_id": group_id,
        "name": group["name"],
        "members": [row["username"] for row in member_rows],
        "added_member": user["username"]
    }

    for member in member_rows:
        socketio.emit("group members updated", payload, to=user_room(member["id"]))


@socketio.on("remove group member")
def handle_remove_group_member(data):
    requester_id = sid_to_user.get(request.sid)

    if not requester_id or not isinstance(data, dict):
        emit("chat error", {"message": "Unauthorized."})
        return

    try:
        group_id = int(data.get("group_id"))
    except (TypeError, ValueError):
        emit("chat error", {"message": "Invalid group."})
        return

    username = str(data.get("username", "")).strip()

    if not username:
        emit("chat error", {"message": "Username is required."})
        return

    with get_db() as db:
        group = db.execute(
            "SELECT id, name, created_by FROM groups WHERE id = ?",
            (group_id,)
        ).fetchone()

        if group is None:
            emit("chat error", {"message": "Group not found."})
            return

        if group["created_by"] != requester_id:
            emit("chat error", {"message": "Only the group admin can remove members."})
            return

        user = db.execute(
            "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
            (username,)
        ).fetchone()

        if user is None:
            emit("chat error", {"message": "User not found."})
            return

        if user["id"] == group["created_by"]:
            emit("chat error", {"message": "The group admin cannot remove themselves."})
            return

        cursor = db.execute(
            """
            DELETE FROM group_members
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user["id"])
        )

        if cursor.rowcount == 0:
            emit("chat error", {"message": "User is not a group member."})
            return

        member_rows = db.execute(
            """
            SELECT u.id, u.username
            FROM group_members gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = ?
            """,
            (group_id,)
        ).fetchall()

    payload = {
        "group_id": group_id,
        "name": group["name"],
        "members": [row["username"] for row in member_rows],
        "removed_member": user["username"]
    }

    socketio.emit("group member removed", payload, to=user_room(user["id"]))

    for member in member_rows:
        socketio.emit("group members updated", payload, to=user_room(member["id"]))

@socketio.on("leave group")
def handle_leave_group(data):
    user_id = sid_to_user.get(request.sid)

    if not user_id or not isinstance(data, dict):
        emit("chat error", {"message": "Unauthorized."})
        return

    try:
        group_id = int(data.get("group_id"))
    except (TypeError, ValueError):
        emit("chat error", {"message": "Invalid group."})
        return

    with get_db() as db:
        group = db.execute(
            "SELECT id, name, created_by FROM groups WHERE id = ?",
            (group_id,)
        ).fetchone()

        if group is None:
            emit("chat error", {"message": "Group not found."})
            return

        if group["created_by"] == user_id:
            emit("chat error", {
                "message": "The group admin cannot leave without transferring admin rights or deleting the group."
            })
            return

        cursor = db.execute(
            """
            DELETE FROM group_members
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user_id)
        )

        if cursor.rowcount == 0:
            emit("chat error", {"message": "You are not a member of this group."})
            return

        member_rows = db.execute(
            """
            SELECT u.id, u.username
            FROM group_members gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = ?
            """,
            (group_id,)
        ).fetchall()

    payload = {
        "group_id": group_id,
        "name": group["name"],
        "members": [row["username"] for row in member_rows],
        "left_user": user_id
    }

    socketio.emit("group member left", payload, to=user_room(user_id))

    for member in member_rows:
        socketio.emit("group members updated", payload, to=user_room(member["id"]))

@socketio.on("group message")
def handle_group_message(data):
    sender_id = sid_to_user.get(request.sid)

    if not sender_id or not isinstance(data, dict):
        emit("chat error", {"message": "Unauthorized."})
        return

    try:
        group_id = int(data.get("group_id"))
    except (TypeError, ValueError):
        emit("chat error", {"message": "Invalid group."})
        return

    message_text = str(data.get("text", "")).strip()

    if not message_text or len(message_text) > 1000:
        emit("chat error", {
            "message": "Message must be between 1 and 1000 characters."
        })
        return

    with get_db() as db:
        membership = db.execute(
            """
            SELECT 1 FROM group_members
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, sender_id)
        ).fetchone()

        if membership is None:
            emit("chat error", {
                "message": "You are not a member of this group."
            })
            return

        sender = db.execute(
            "SELECT username FROM users WHERE id = ?",
            (sender_id,)
        ).fetchone()

        group = db.execute(
            "SELECT name FROM groups WHERE id = ?",
            (group_id,)
        ).fetchone()

        if group is None or sender is None:
            emit("chat error", {"message": "Group or user not found."})
            return

        cursor = db.execute(
            """
            INSERT INTO group_messages
                (group_id, sender_id, text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (group_id, sender_id, message_text, utc_now())
        )

        message = {
            "id": cursor.lastrowid,
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_username": sender["username"],
            "text": message_text,
            "created_at": utc_now()
        }

        members = db.execute(
            """
            SELECT user_id FROM group_members
            WHERE group_id = ?
            """,
            (group_id,)
        ).fetchall()

    for member in members:
        socketio.emit(
            "group message",
            message,
            to=user_room(member["user_id"])
        )
@socketio.on("load groups")
def handle_load_groups():
    user_id = sid_to_user.get(request.sid)

    if not user_id:
        emit("chat error", {"message": "Unauthorized."})
        return

    with get_db() as db:
        groups = db.execute(
            """
            SELECT
                g.id,
                g.name,
                g.created_by,
                creator.username AS creator_username
            FROM groups g
            JOIN group_members gm ON gm.group_id = g.id
            JOIN users creator ON creator.id = g.created_by
            WHERE gm.user_id = ?
            ORDER BY g.id DESC
            """,
            (user_id,)
        ).fetchall()

        group_list = []

        for group in groups:
            members = db.execute(
                """
                SELECT u.username
                FROM group_members gm
                JOIN users u ON u.id = gm.user_id
                WHERE gm.group_id = ?
                ORDER BY u.username
                """,
                (group["id"],)
            ).fetchall()

            group_list.append({
                "creator_username": group["creator_username"],
                "group_id": group["id"],
                "name": group["name"],
                "created_by": group["created_by"],
                "members": [member["username"] for member in members]
            })

    emit("groups list", {"groups": group_list})

@socketio.on("load group history")
def handle_load_group_history(data):
    requester_id = sid_to_user.get(request.sid)

    if not requester_id or not isinstance(data, dict):
        emit("chat error", {"message": "Unauthorized history request."})
        return

    try:
        group_id = int(data.get("group_id"))
    except (TypeError, ValueError):
        emit("chat error", {"message": "Invalid group."})
        return

    with get_db() as db:
        membership = db.execute(
            """
            SELECT 1 FROM group_members
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, requester_id)
        ).fetchone()

        if membership is None:
            emit("chat error", {
                "message": "You are not a member of this group."
            })
            return

        group = db.execute(
            "SELECT name FROM groups WHERE id = ?",
            (group_id,)
        ).fetchone()

        if group is None:
            emit("chat error", {"message": "Group not found."})
            return

        rows = db.execute(
            """
            SELECT
                gm.id,
                gm.group_id,
                gm.sender_id,
                u.username AS sender_username,
                gm.text,
                gm.created_at,
                gm.deleted_at
            FROM group_messages gm
            JOIN users u ON u.id = gm.sender_id
            WHERE gm.group_id = ?
            ORDER BY gm.id ASC
            """,
            (group_id,)
        ).fetchall()

        history = [dict(row) for row in rows]

    emit("group history", {
        "group_id": group_id,
        "group_name": group["name"],
        "messages": history
    })

@socketio.on("delete message")
def handle_delete_message(data):
    user_id = sid_to_user.get(request.sid)

    if not user_id or not isinstance(data, dict):
        emit("chat error", {"message": "Unauthorized."})
        return

    try:
        message_id = int(data.get("message_id"))
    except (TypeError, ValueError):
        emit("chat error", {"message": "Invalid message."})
        return

    with get_db() as db:
        message = fetch_message(db, message_id)

        if message is None or message["sender_id"] != user_id:
            emit("chat error", {"message": "You can only delete your own messages."})
            return

        if message["deleted_at"] is not None:
            emit("chat error", {"message": "Message is already deleted."})
            return

        created_at = datetime.fromisoformat(message["created_at"])
        age_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()

        if age_seconds > MESSAGE_DELETE_WINDOW_MINUTES * 60:
            emit("chat error", {
                "message": "This message cannot be deleted because the 15-minute deletion time limit has expired."
            })
            return

        deleted_at = utc_now()

        db.execute(
            "UPDATE messages SET deleted_at = ? WHERE id = ?",
            (deleted_at, message_id)
        )

        recipient_id = message["recipient_id"]

    deletion_payload = {
        "message_id": message_id,
        "deleted_at": deleted_at
    }

    socketio.emit("message deleted", deletion_payload, to=user_room(user_id))
    socketio.emit("message deleted", deletion_payload, to=user_room(recipient_id))


@socketio.on("delete group message")
def handle_delete_group_message(data):
    user_id = sid_to_user.get(request.sid)

    if not user_id or not isinstance(data, dict):
        emit("chat error", {"message": "Unauthorized."})
        return

    try:
        message_id = int(data.get("message_id"))
    except (TypeError, ValueError):
        emit("chat error", {"message": "Invalid message."})
        return

    with get_db() as db:
        message = db.execute(
            """
            SELECT id, group_id, sender_id, created_at, deleted_at
            FROM group_messages
            WHERE id = ?
            """,
            (message_id,)
        ).fetchone()

        if message is None or message["sender_id"] != user_id:
            emit("chat error", {
                "message": "You can only delete your own group messages."
            })
            return

        if message["deleted_at"] is not None:
            emit("chat error", {"message": "Message is already deleted."})
            return

        created_at = datetime.fromisoformat(message["created_at"])
        age_seconds = (
            datetime.now(timezone.utc) - created_at
        ).total_seconds()

        if age_seconds > MESSAGE_DELETE_WINDOW_MINUTES * 60:
            emit("chat error", {
                "message": "The 15-minute deletion time limit has expired."
            })
            return

        deleted_at = utc_now()

        db.execute(
            """
            UPDATE group_messages
            SET deleted_at = ?
            WHERE id = ?
            """,
            (deleted_at, message_id)
        )

        members = db.execute(
            """
            SELECT user_id FROM group_members
            WHERE group_id = ?
            """,
            (message["group_id"],)
        ).fetchall()

    payload = {
        "message_id": message_id,
        "group_id": message["group_id"],
        "deleted_at": deleted_at
    }

    for member in members:
        socketio.emit(
            "group message deleted",
            payload,
            to=user_room(member["user_id"])
        )


@socketio.on("message read")
def handle_message_read(data):
    reader_id = sid_to_user.get(request.sid)
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
        reader_unread = get_unread_counts(db, reader_id)

    # Emitted after the commit.
    emit(
        "message status",
        {"message_id": message_id, "status": "read"},
        to=user_room(sender_id),
    )
    socketio.emit("unread counts", reader_unread, to=user_room(reader_id))


@socketio.on("typing")
def handle_typing(data):
    sender_id = sid_to_user.get(request.sid)

    if not sender_id or not isinstance(data, dict):
        return

    recipient_username = str(data.get("recipient", "")).strip()
    is_typing = bool(data.get("is_typing", False))

    if not recipient_username:
        return

    with get_db() as db:
        recipient = db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            (recipient_username,),
        ).fetchone()

        sender = db.execute(
            "SELECT username FROM users WHERE id = ?",
            (sender_id,),
        ).fetchone()

        if not recipient or not sender or recipient["id"] == sender_id:
            return

        conversation = db.execute(
            """
            SELECT 1 FROM messages
            WHERE
                (sender_id = ? AND recipient_id = ?)
                OR
                (sender_id = ? AND recipient_id = ?)
            LIMIT 1
            """,
            (
                sender_id,
                recipient["id"],
                recipient["id"],
                sender_id,
            ),
        ).fetchone()

    if conversation:
        emit(
            "typing",
            {"username": sender["username"], "is_typing": is_typing},
            to=user_room(recipient["id"]),
        )

if __name__ == "__main__":
    init_db()
    print("Starting Chat Application...")
    socketio.run(
        app,
        host=HTTPS_HOST,
        port=HTTPS_PORT,
        debug=True,
        ssl_context=(CERT_FILE, KEY_FILE),
        allow_unsafe_werkzeug=True
    )

