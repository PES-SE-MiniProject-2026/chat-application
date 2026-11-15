# Real-Time Chat Application

A secure, real-time chat application built for the Software Engineering Mini-Project (Team 5). Users can register, log in, message each other directly or in groups, share files, see who is online, and be moderated by an administrator. Messaging runs over WebSockets (Socket.IO) and all traffic is encrypted with HTTPS / WSS.

## Table of Contents

- [Features](#features)
- [Technology Stack](#technology-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [Secure Transport (CHAT-SR-001)](#secure-transport-chat-sr-001)
- [WebSocket Authentication (CHAT-SR-003)](#websocket-authentication-chat-sr-003)
- [Security Summary](#security-summary)
- [HTTP Endpoints](#http-endpoints)
- [Socket.IO Events](#socketio-events)
- [Database](#database)
- [Testing](#testing)
- [Documentation and Deliverables](#documentation-and-deliverables)
- [Project Management](#project-management)
- [Team](#team)
- [Project Status](#project-status)

## Features

### Accounts and authentication

- Registration with validation: username of 3–30 characters (letters, numbers, underscores), valid email, password of 12–128 characters, and a check against common passwords.
- Passwords are stored as salted hashes, never in plain text.
- Login and logout. Logging out disconnects the user's active sockets.
- Login attempts are rate limited.
- Accounts can be `active`, `suspended` or `banned`. Suspended and banned users cannot use the app.

### Direct messaging

- One-to-one messaging between registered users, up to 1000 characters per message.
- Messages are stored in the database, so history is kept and offline messages are delivered when the user reconnects.
- Chat history is loaded per conversation and only contains messages between the two participants.
- Message status: **sent**, **delivered** and **read**. A message is marked read only when the chat window is actually being viewed.
- Typing indicators.
- Message deletion: a sender can delete their own message within 15 minutes. Both participants see "This message was deleted."
- Unread badges per conversation, and a missed-messages banner that summarises what arrived while the user was away.

### Group conversations

- Create a group with a name (1–50 characters) and up to 49 other members.
- Only the group creator (the group admin) can add or remove members.
- Members can leave a group.
- Group messages (up to 1000 characters) are delivered to every member. Only members can send messages and load group history.
- Load your groups and per-group chat history.
- Delete your own group messages within the same 15-minute window.

### Presence and notifications

- Online/offline presence for users.
- Live notifications for new messages and for presence changes.

### File sharing

- Attach a file to a direct message, with an optional caption.
- Allowed types: PNG, JPEG, GIF, WebP, PDF and TXT. Maximum size 5 MiB.
- Uploads are validated by extension, size, and file signature (magic bytes), so a renamed file is rejected.
- Files are stored on the server under random names. The original filename is kept only for download.
- Only the sender and recipient can download an attachment. The download route is protected against path traversal and sends `nosniff` and a restrictive Content-Security-Policy.
- Images can be shown inline in the chat.

### Administration

- Admin dashboard at `/admin` (admins only) listing all users with their role and account status.
- Admins can set a user to `active`, `suspended` or `banned`. Suspending or banning disconnects the user immediately.
- Admins cannot change their own status or moderate other administrators.
- Admins can broadcast a system announcement (up to 1000 characters) to all connected users.
- The first admin is created by setting `CHAT_ADMIN_USERNAME` to an existing username.

### Reliability and availability

- Automatic reconnection after a dropped connection, with a fresh authentication token on every attempt.
- `GET /health` endpoint that returns `200` only when the database is reachable, for availability monitoring (CHAT-NF-003).
- Message delivery latency tuned and verified under load against the specified target (p95 under 200 ms in the 100-user test, Sprint 3).

### User interface

- Responsive layout for desktop, tablet and mobile (verified in issue #17 in Sprint 3, see `docs/issue-17-responsive-ui-verification.md`).
- A user can send their first message within the click/tap budget defined in the requirements.

### Rate limiting

- Messages: sliding-window limit per user (default 20 messages per 10 seconds), applied to chat messages and uploads.
- Login: 5 attempts per minute per IP.
- `/ws-token`: 300 requests per minute per IP (configurable) to allow reconnect storms.

## Technology Stack

- **Backend:** Python, Flask, Flask-SocketIO (WebSocket / Socket.IO), Flask-Limiter
- **Database:** SQLite
- **Frontend:** HTML, CSS, JavaScript (Socket.IO client)
- **Security:** HTTPS/WSS (TLS 1.2+), signed WebSocket tokens (itsdangerous), Werkzeug password hashing
- **Testing / CI:** pytest, Node's built-in test runner, GitHub Actions

## Project Structure

```
chat-application/
├── app.py                  # Flask app, HTTP routes, Socket.IO event handlers, database setup
├── security.py             # HTTPS enforcement, security headers, error handlers
├── websocket_auth.py       # Signed, expiring WebSocket token creation and verification
├── rate_limiting.py        # Thread-safe sliding-window rate limiter for messages
├── attachment_utils.py     # File type, size and signature validation for uploads
├── templates/              # index.html (chat), login.html, register.html, admin.html
├── static/css/style.css    # Styling and responsive layout
├── uploads/                # Stored attachments (git-ignored in deployment)
├── certs/                  # Local TLS certificate and key (git-ignored)
├── tests/                  # Unit, integration, performance and reliability tests
├── docs/                   # Test, validation and coverage reports
├── requirements.txt
├── pytest.ini
└── .github/workflows/ci.yml
```

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

### Create an administrator

Register a normal account first, then restart the app with that username set as admin:

```powershell
$env:CHAT_ADMIN_USERNAME="your_username"; python app.py
```

Log in and open **https://localhost:5000/admin**.

## Configuration

All settings are environment variables.

| Variable                                   | Default                                         | Purpose                                                        |
| ------------------------------------------ | ----------------------------------------------- | -------------------------------------------------------------- |
| `CHAT_DEBUG`                               | `1`                                             | Set to `0` in deployment                                       |
| `CHAT_SECRET_KEY`                          | dev key                                         | Required when `CHAT_DEBUG=0`                                   |
| `CHAT_HOST`                                | `localhost`                                     | Host the server binds to                                       |
| `CHAT_HTTPS_PORT`                          | `5000`                                          | HTTPS port                                                     |
| `CHAT_HTTP_PORT`                           | `5080`                                          | Dev HTTP-to-HTTPS redirect port                                |
| `CHAT_CERT_FILE` / `CHAT_KEY_FILE`         | `certs/cert.pem`, `certs/key.pem`               | TLS certificate and key                                        |
| `CHAT_ALLOWED_ORIGINS`                     | `https://localhost:5000,https://127.0.0.1:5000` | Comma-separated allowed origins                                |
| `CHAT_BEHIND_PROXY`                        | `0`                                             | Set to `1` behind a TLS-terminating proxy                      |
| `CHAT_DATABASE`                            | `users.db` in project folder                    | SQLite database path                                           |
| `CHAT_UPLOAD_FOLDER`                       | `uploads/` in project folder                    | Where attachments are stored                                   |
| `CHAT_ADMIN_USERNAME`                      | empty                                           | Username promoted to administrator at startup                  |
| `CHAT_MSG_RATE_LIMIT`                      | `20`                                            | Messages allowed per window, per user                          |
| `CHAT_MSG_RATE_WINDOW`                     | `10`                                            | Message rate-limit window in seconds                           |
| `CHAT_WS_TOKEN_LIMIT`                      | `300 per minute`                                | Rate limit for `/ws-token`                                     |
| `CHAT_RATE_LIMITS`                         | `1`                                             | Set to `0` to disable rate limits (debug mode only)            |
| `CHAT_PING_INTERVAL` / `CHAT_PING_TIMEOUT` | `5` / `5`                                       | Socket.IO keep-alive settings in seconds                       |
| `CHAT_SHARED_DB`                           | `1`                                             | Use a shared database connection for writes                    |
| `CHAT_PERFORMANCE_TEST`                    | `0`                                             | Performance-test mode (disables rate limits, adds timing data) |

The app refuses to start with `CHAT_DEBUG=0` if the default secret key is used or rate limits are disabled.

### Deployment

In deployment, TLS is terminated by a reverse proxy such as nginx with a CA-issued certificate (for example Let's Encrypt). The app runs behind it with `CHAT_BEHIND_PROXY=1`, `CHAT_DEBUG=0`, a strong `CHAT_SECRET_KEY`, and `CHAT_ALLOWED_ORIGINS` set to the public HTTPS URL. Use a single worker, because online presence and rate-limit state are currently kept in memory.

## Secure Transport (CHAT-SR-001)

- The app is served over HTTPS, and Socket.IO uses WSS. TLS 1.2 or newer is required.
- Plain HTTP requests are redirected to HTTPS (301).
- Socket.IO connections that are not secure are rejected, and allowed origins are restricted to the HTTPS URLs.
- The session cookie is `Secure`, `HttpOnly` and `SameSite=Lax`. HSTS is enabled outside debug mode.
- Responses include `X-Content-Type-Options: nosniff`.
- Connection failures show generic messages to the user. Details stay in the server log.

## WebSocket Authentication (CHAT-SR-003)

A valid authentication token is required to open a WebSocket session.

- After logging in, the client requests a short-lived signed token from `GET /ws-token`. This endpoint returns `401` to users who are not logged in.
- The token is sent in the Socket.IO handshake (`auth` payload) and expires after 60 seconds. The client fetches a fresh token on every connect and reconnect.
- The server rejects missing, malformed, tampered or expired tokens, and tokens that do not match the logged-in session. Rejected clients only see a generic `unauthorized` message, and details stay in the server log.
- Each connection is tied to the verified user, and message, read-receipt, typing, group and delete events use that identity, never a value sent by the client.
- Logging out, suspending or banning a user disconnects their active sockets.

## Security Summary

| Area             | Measure                                                                                                                                                 |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Transport        | HTTPS / WSS, HTTP redirect, HSTS, restricted origins                                                                                                    |
| Authentication   | Salted password hashes, password policy, login rate limit, signed 60-second WebSocket tokens                                                            |
| Authorization    | Users only read their own conversations and attachments. Only group members can post. Only the group admin manages members. Only admins reach `/admin`. |
| Input validation | Length limits on messages, names and announcements. File type, size and signature checks.                                                               |
| File safety      | Random storage names, path-traversal protection, `nosniff`, restrictive CSP on downloads                                                                |
| Abuse prevention | Per-user message rate limit, per-IP login and token limits, 5 MiB request cap                                                                           |
| Error handling   | Generic user-facing errors. Details only in the server log.                                                                                             |

## HTTP Endpoints

| Method    | Path                        | Purpose                                         | Access              |
| --------- | --------------------------- | ----------------------------------------------- | ------------------- |
| GET       | `/`                         | Chat interface                                  | Logged in           |
| GET, POST | `/register`                 | Create an account                               | Public              |
| GET, POST | `/login`                    | Log in (5 per minute)                           | Public              |
| POST      | `/logout`                   | Log out and disconnect sockets                  | Logged in           |
| GET       | `/ws-token`                 | Short-lived WebSocket token                     | Logged in           |
| POST      | `/upload`                   | Send a file attachment                          | Logged in           |
| GET       | `/attachments/<message_id>` | Download an attachment (`?inline=1` for images) | Sender or recipient |
| GET       | `/health`                   | Liveness check with database test               | Public              |
| GET       | `/admin`                    | Admin dashboard                                 | Admin               |
| POST      | `/admin/users/<id>/status`  | Set user to active, suspended or banned         | Admin               |
| POST      | `/admin/announcements`      | Broadcast a system announcement                 | Admin               |
| GET       | `/perf-stats`               | Timing statistics, performance-test mode only   | Test use            |

## Socket.IO Events

| Event                                      | Purpose                                               |
| ------------------------------------------ | ----------------------------------------------------- |
| `connect` / `disconnect`                   | Authenticated session start and end, presence updates |
| `get presence`                             | Request who is online                                 |
| `chat message`                             | Send a direct message                                 |
| `load chat history`                        | Load a direct conversation                            |
| `message read`                             | Mark messages as read                                 |
| `typing`                                   | Typing indicator                                      |
| `delete message`                           | Delete your own direct message (15-minute window)     |
| `create group`                             | Create a group                                        |
| `add group member` / `remove group member` | Group admin manages members                           |
| `leave group`                              | Leave a group                                         |
| `group message`                            | Send a group message                                  |
| `load groups` / `load group history`       | Load your groups and a group's history                |
| `delete group message`                     | Delete your own group message (15-minute window)      |
| `system announcement`                      | Server-to-client admin broadcast                      |

## Database

SQLite, created and migrated automatically at startup. Main tables:

- `users`: username, email, password hash, role (`user` or `admin`), account status, created time
- `messages`: direct messages with status, timestamps, deletion time and attachment details
- `groups` and `group_members`: groups, their creator, and membership
- `group_messages`: group messages with deletion time

`users.db` is git-ignored. Do not commit a real database.

## Testing

### Automated tests

```powershell
python -m pytest tests -v
```

The pytest suite lives in `tests/` and covers:

- HTTPS redirect, secure cookies and rejection of insecure Socket.IO connections
- Registration, login, logout and authentication security
- WebSocket token authentication (valid, missing, malformed, tampered, expired, wrong-user, no-session)
- Direct message handlers, status updates and access control
- Group creation, membership, messaging and history
- Presence and notifications
- File sharing: allowed types, size limit, signature checks, download permissions
- Message and group-message deletion
- Rate limiting and account status enforcement
- Admin dashboard, moderation and announcements
- Shared-write database mode and utility functions

The frontend structure test runs with Node: `node --test tests/app.test.js`.

Tests run automatically on every push and pull request through GitHub Actions (`.github/workflows/ci.yml`) on Python 3.12.

### Performance test (100 concurrent users)

Start the server in performance mode, then run the test from the project root:

```powershell
$env:CHAT_PERFORMANCE_TEST="1"; python app.py
python tests/performance_test.py
```

It uses real registration, login, `/ws-token` and Socket.IO authentication for 100 users, counts a message as delivered only when the recipient receives it, and checks the p95 latency against the 200 ms target. To remove the load generator from the measurement, run `python tests/run_split_perf.py`, which splits the users across several processes. Results are written to `tests/results/` as JSON and CSV.

### Reliability test

```powershell
python tests/reliability_test.py
```

Verifies automatic reconnection after a dropped connection. Results are written to `tests/results/`.

### Coverage

Code and branch coverage reports are in `docs/` (`coverage.json`, `coverage.xml`, and the PDF report).

## Documentation and Deliverables

| Document                               | Location                                             |
| -------------------------------------- | ---------------------------------------------------- |
| Software Requirements Specification    | `5_SRS_MiniProject.pdf`                              |
| Software Test Plan                     | `5_TestPlan_MiniProject.pdf`                         |
| Software Architecture Document         | `Team5_SAD.pdf`                                      |
| Unit Test Report                       | `docs/1.UNIT TEST REPORT TEAM 5.pdf`                 |
| System Validation Report               | `docs/2.SYSTEM VALIDATION REPORT TEAM 5.pdf`         |
| Code and Branch Coverage Report        | `docs/3. CODE AND BRANCH COVERAGE REPORT TEAM 5.pdf` |
| Responsive UI verification (issue #17) | `docs/issue-17-responsive-ui-verification.md`        |
| Sprint 1 demo video                    | `Sprint 1 Demo.mp4`                                  |
| Sprint 2 demo video                    | `Sprint 2 Demo.mp4`                                  |

## Project Management

Project tasks, assignees, story points, priorities, and sprint planning are tracked using GitHub Projects. Work was done on feature branches and merged through pull requests, with CI running on each.

## Team

- Amoghaa R: QA, Testing and Documentation
- Vaibhav R: Server and Socket Backend
- P S Harshavardhan: Authentication and Database
- Pramath Ganesh Hegde: Frontend and UI

## Project Status

Sprints 1, 2 and 3 are complete. Implemented and tested:

- **Sprint 1:** registration, login and logout, direct messaging, delivery and read receipts, typing indicators, secure transport (CHAT-SR-001), WebSocket authentication (CHAT-SR-003)
- **Sprint 2:** group conversations, presence and notifications, chat history, file sharing, message deletion, admin moderation and announcements, rate limiting, health check, automatic reconnection, performance and reliability testing
- **Sprint 3:**
  - Message delivery latency verified under load and reduced to meet the target (#14, PR #35)
  - Responsive UI and first-message usability verified (#17, PR #37)
  - Code modularisation and server-side unit test coverage work (#18, PR #36): `security.py`, `rate_limiting.py`, `attachment_utils.py` and `websocket_auth.py` split out of `app.py`
  - Unit test, system validation, and code and branch coverage reports

Known limitations: online presence and rate-limit state are kept in memory, so the app must run as a single worker. File attachments are supported in direct messages only.
