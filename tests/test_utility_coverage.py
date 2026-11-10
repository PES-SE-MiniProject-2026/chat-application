
import pytest
import app as chat


# ---------------------------------------------------------
# Performance utility tests
# ---------------------------------------------------------

def test_perf_percentiles_empty():
    assert chat._perf_percentiles([]) is None


def test_perf_percentiles_single_value():
    result = chat._perf_percentiles([25.567])

    assert result == {
        "count": 1,
        "p50": 25.567,
        "p95": 25.567,
        "p99": 25.567,
        "max": 25.567,
    }


def test_perf_percentiles_multiple_values():
    result = chat._perf_percentiles([50, 10, 30, 20, 40])

    assert result["count"] == 5
    assert result["p50"] == 30
    assert result["p95"] == 50
    assert result["p99"] == 50
    assert result["max"] == 50


def test_perf_stats_disabled(monkeypatch):
    monkeypatch.setattr(chat, "PERFORMANCE_TEST_MODE", False)

    response = chat.app.test_client().get(
        "/perf-stats", base_url="https://localhost"
    )

    assert response.status_code == 404


def test_perf_stats_returns_percentiles(monkeypatch):
    monkeypatch.setattr(chat, "PERFORMANCE_TEST_MODE", True)
    monkeypatch.setitem(chat.perf_samples, "inbound_ms", [10, 20, 30])

    response = chat.app.test_client().get(
        "/perf-stats", base_url="https://localhost"
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["inbound_ms"]["count"] == 3
    assert data["inbound_ms"]["max"] == 30


def test_perf_stats_reset(monkeypatch):
    monkeypatch.setattr(chat, "PERFORMANCE_TEST_MODE", True)
    monkeypatch.setitem(chat.perf_samples, "inbound_ms", [10, 20])
    monkeypatch.setitem(chat.perf_samples, "handler_ms", [30])

    response = chat.app.test_client().get(
        "/perf-stats?reset=1", base_url="https://localhost"
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "reset"}
    assert all(not values for values in chat.perf_samples.values())


# ---------------------------------------------------------
# Sliding window rate limiter tests
# ---------------------------------------------------------

def test_rate_limiter_reset_specific_key():
    limiter = chat.SlidingWindowRateLimiter(2, 10)

    limiter.check("alice")
    limiter.check("bob")
    limiter.reset("alice")

    assert "alice" not in limiter._events
    assert "bob" in limiter._events


def test_rate_limiter_reset_all_keys():
    limiter = chat.SlidingWindowRateLimiter(2, 10)

    limiter.check("alice")
    limiter.check("bob")
    limiter.reset()

    assert limiter._events == {}


def test_rate_limiter_sweep_removes_expired_keys():
    limiter = chat.SlidingWindowRateLimiter(2, 10)

    limiter._events = {
        "expired": [],
        "old": [5],
        "active": [95],
    }

    limiter._sweep(90)

    assert "expired" not in limiter._events
    assert "old" not in limiter._events
    assert "active" in limiter._events


# ---------------------------------------------------------
# Security and HTTP error handler tests
# ---------------------------------------------------------

def test_security_headers_are_added():
    response = chat.app.test_client().get(
        "/health", base_url="https://localhost"
    )

    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_security_headers_add_hsts_when_secure_and_not_debug(monkeypatch):
    monkeypatch.setattr(chat, "DEBUG", False)

    response = chat.app.test_client().get(
        "/health", base_url="https://localhost"
    )

    assert "max-age=31536000" in response.headers[
        "Strict-Transport-Security"
    ]


def test_internal_error_returns_generic_message():
    with chat.app.test_request_context("/"):
        response, status = chat.internal_error(Exception("secret database error"))

    assert status == 500
    assert response == "Something went wrong. Please try again later."


def test_request_entity_too_large_returns_json():
    with chat.app.test_request_context(
        "/upload", headers={"Accept": "application/json"}
    ):
        response, status = chat.request_entity_too_large(Exception())

    assert status == 413
    assert response.get_json()["error"] == (
        "File exceeds maximum allowed size (5 MiB)."
    )


def test_request_entity_too_large_returns_plain_text():
    with chat.app.test_request_context("/"):
        response, status = chat.request_entity_too_large(Exception())

    assert status == 413
    assert "5 MiB" in response


# ---------------------------------------------------------
# Filename sanitization tests
# ---------------------------------------------------------

def test_sanitize_normal_filename():
    result = chat.sanitize_display_filename("report.pdf", ".pdf")

    assert result == "report.pdf"


def test_sanitize_path_traversal():
    result = chat.sanitize_display_filename("../../secret.pdf", ".pdf")

    assert result == "secret.pdf"
    assert "/" not in result
    assert "\\" not in result


def test_sanitize_empty_filename():
    result = chat.sanitize_display_filename("", ".pdf")

    assert result.startswith("attachment_")
    assert result.endswith(".pdf")


def test_sanitize_filename_with_control_characters():
    result = chat.sanitize_display_filename("report\n\t.pdf", ".pdf")

    assert "\n" not in result
    assert "\t" not in result


def test_sanitize_filename_length_limit():
    result = chat.sanitize_display_filename("a" * 250 + ".pdf", ".pdf")

    assert len(result) <= 200
    assert result.endswith(".pdf")


# ---------------------------------------------------------
# Health endpoint tests
# ---------------------------------------------------------

def test_health_endpoint_success():
    response = chat.app.test_client().get(
        "/health", base_url="https://localhost"
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert response.headers["Cache-Control"] == "no-store"


def test_health_endpoint_database_failure(monkeypatch):
    def failing_database():
        raise RuntimeError("Database unavailable")

    monkeypatch.setattr(chat, "get_db", failing_database)

    response = chat.app.test_client().get(
        "/health", base_url="https://localhost"
    )

    assert response.status_code == 503
    assert response.get_json() == {"status": "unavailable"}
    assert response.headers["Cache-Control"] == "no-store"
