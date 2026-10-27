
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "development-secret-key"

socketio = SocketIO(app)


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("connect")
def handle_connect(auth=None):
    print("Client connected")


@socketio.on("disconnect")
def handle_disconnect(reason=None):
    print("Client disconnected")


@socketio.on("chat message")
def handle_chat_message(data):
    if not isinstance(data, dict):
        return

    username = str(data.get("username", "Guest")).strip()[:30]
    message_text = str(data.get("text", "")).strip()[:1000]

    if not message_text:
        return

    message = {
        "username": username or "Guest",
        "text": message_text,
    }

    # Broadcast to every connected client for the initial test.
    emit("chat message", message, broadcast=True)


if __name__ == "__main__":
    socketio.run(
        app,
        host="127.0.0.1",
        port=5000,
        debug=True,
    )
