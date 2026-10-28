# Real-Time Chat Application

A real-time chat application developed as part of the Software Engineering Mini-Project.

## Project Overview

The project aims to develop a chat application that enables users to communicate in real time through WebSocket-based messaging.

## Objectives

- Provide user registration, login, and logout.
- Support direct messaging and group conversations.
- Display user presence and message notifications.
- Support file sharing and chat history.
- Implement secure communication and access control.
- Test performance, reliability, and usability.

## Technology Stack

- **Backend:** Python, Flask, Flask-SocketIO (WebSocket / Socket.IO)
- **Database:** SQLite
- **Frontend:** HTML, CSS, JavaScript (Socket.IO client)
- **Testing / CI:** pytest, GitHub Actions

## Current Features

- User registration, login, and logout
- Direct messaging between registered users
- Message persistence, with offline messages delivered on reconnect
- Message status: sent, delivered, read (read is sent only when the chat window is actually viewed)
- Typing indicators
- Encrypted transport (HTTPS / WSS), see [Secure Transport](#secure-transport-chat-sr-001)
- Token-based authentication for WebSocket connections, see [WebSocket Authentication](#websocket-authentication-chat-sr-003)

## Getting Started

### Prerequisites

- Python 3.12 or newer
- [mkcert](https://github.com/FiloSottile/mkcert) for a locally trusted development certificate

### Setup (Windows PowerShell)

```powershell
git clone https://github.com/PES-SE-MiniProject-2026/chat-application
cd chat-application

python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

On macOS/Linux, activate the environment with `source .venv/bin/activate`.

### Create a development TLS certificate (one time per machine)

```powershell
winget install FiloSottile.mkcert    # macOS: brew install mkcert
mkcert -install
mkdir certs
mkcert -cert-file certs/cert.pem -key-file certs/key.pem localhost 127.0.0.1 ::1
```

The `certs/` folder is git-ignored. Each developer generates their own certificate, and private keys are never committed.

### Run

```powershell
python app.py
```

Open **https://localhost:5000**. Plain HTTP on `http://localhost:5080` redirects to HTTPS.

To try the chat, register two users and open them in two different browsers (or a private window).

## Secure Transport (CHAT-SR-001)

- The app is served over HTTPS, and Socket.IO uses WSS. TLS 1.2 or newer is required.
- Plain HTTP requests are redirected to HTTPS (301).
- Socket.IO connections that are not secure are rejected, and allowed origins are restricted to the HTTPS URLs.
- The session cookie is `Secure` and `HttpOnly`. HSTS is enabled outside debug mode.
- Connection failures show generic messages to the user. Details stay in the server log.

### Configuration (environment variables)

| Variable                           | Default                                         | Purpose                                   |
| ---------------------------------- | ----------------------------------------------- | ----------------------------------------- |
| `CHAT_DEBUG`                       | `1`                                             | Set to `0` in deployment                  |
| `CHAT_SECRET_KEY`                  | dev key                                         | Required when `CHAT_DEBUG=0`              |
| `CHAT_HTTPS_PORT`                  | `5000`                                          | HTTPS port                                |
| `CHAT_HTTP_PORT`                   | `5080`                                          | Dev HTTP-to-HTTPS redirect port           |
| `CHAT_CERT_FILE` / `CHAT_KEY_FILE` | `certs/cert.pem`, `certs/key.pem`               | TLS certificate and key                   |
| `CHAT_ALLOWED_ORIGINS`             | `https://localhost:5000,https://127.0.0.1:5000` | Comma-separated allowed origins           |
| `CHAT_BEHIND_PROXY`                | `0`                                             | Set to `1` behind a TLS-terminating proxy |

### Deployment

In deployment, TLS is terminated by a reverse proxy such as nginx with a CA-issued certificate (for example Let's Encrypt). The app runs behind it with `CHAT_BEHIND_PROXY=1`, `CHAT_DEBUG=0`, a strong `CHAT_SECRET_KEY`, and `CHAT_ALLOWED_ORIGINS` set to the public HTTPS URL. Use a single worker, because online presence is currently kept in memory.

## WebSocket Authentication (CHAT-SR-003)

A valid authentication token is required to open a WebSocket session.

- After logging in, the client requests a short-lived signed token from `GET /ws-token`. This endpoint returns `401` to users who are not logged in.
- The token is sent in the Socket.IO handshake (`auth` payload) and expires after 60 seconds. The client fetches a fresh token on every connect and reconnect.
- The server rejects missing, malformed, tampered or expired tokens, and tokens that do not match the logged-in session. Rejected clients only see a generic `unauthorized` message, and details stay in the server log.
- Each connection is tied to the verified user, and message, read-receipt and typing events use that identity, never a value sent by the client.
- Logging out disconnects the user's active sockets.

## Testing

```powershell
pip install pytest
python -m pytest tests -v
```

The tests cover the HTTP-to-HTTPS redirect, secure cookie settings, rejection of insecure Socket.IO connections, authentication and logout, and WebSocket token authentication (valid, missing, malformed, tampered, expired, wrong-user and no-session cases). They run automatically on every push through GitHub Actions (`.github/workflows/ci.yml`).

Testing will also cover functionality, security, performance, reliability, and usability, in accordance with the submitted Software Test Plan.

## Project Management

Project tasks, assignees, story points, priorities, and sprint planning are tracked using GitHub Projects.

## Team

- Amoghaa R — QA, Testing and Documentation
- Vaibhav R — Server and Socket Backend
- P S Harshavardhan — Authentication and Database
- Pramath Ganesh Hegde — Frontend and UI

## Project Status

Sprint 1 is complete. Authentication, direct messaging, delivery/read receipts, typing indicators, secure transport (CHAT-SR-001) and WebSocket authentication (CHAT-SR-003) are implemented and tested. Group conversations, presence, file sharing, and chat history are planned for later sprints.
