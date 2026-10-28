
import os
import re
import sqlite3
from functools import wraps
from contextlib import contextmanager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
)
from flask_socketio import SocketIO, emit
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
    "CHAT_SECRET_KEY", "development-only-change-this-key"
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

DATABASE = os.path.join(app.root_path, "users.db")
socketio = SocketIO(app)


@contextmanager
def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row

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
                    """
                    INSERT INTO users (username, email, password_hash)
                    VALUES (?, ?, ?)
                    """,
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
        username_or_email = request.form.get(
            "username_or_email", ""
        ).strip()
        password = request.form.get("password", "")

        with get_db() as db:
            user = db.execute(
                """
                SELECT id, username, password_hash
                FROM users
                WHERE username = ? COLLATE NOCASE
                   OR email = ? COLLATE NOCASE
                """,
                (username_or_email, username_or_email),
            ).fetchone()

        if user is None or not check_password_hash(
            user["password_hash"], password
        ):
            flash("Invalid username/email or password.", "error")
            return render_template("login.html")

        # Clear any previous session before creating the new login session.
        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]

        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# Existing Socket.IO connection events.
@socketio.on("connect")
def handle_connect(auth=None):
    print("Client connected")


@socketio.on("disconnect")
def handle_disconnect(reason=None):
    print("Client disconnected")


@socketio.on("chat message")
def handle_chat_message(data):
    # Basic protection while HTTP login is being integrated.
    # The separate authenticated WebSocket handshake task (#11)
    # must still be completed and tested.
    if "user_id" not in session:
        return

    if not isinstance(data, dict):
        return

    message_text = str(data.get("text", "")).strip()[:1000]
    if not message_text:
        return

    message = {
        "username": session["username"],
        "text": message_text,
    }

    emit("chat message", message, broadcast=True)


if __name__ == "__main__":
    init_db()
    socketio.run(
        app,
        host="127.0.0.1",
        port=5000,
        debug=True,
    )

