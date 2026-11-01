"""Shared pytest setup.

The message-send limiter (#13) is process-wide and keyed by user id. Every
test builds a fresh database, so user ids repeat (always 1, 2, ...) and one
test's messages would count against the next test's user. Keep the limit out
of the way for the suite, and clear any leftover state between tests.
Tests that exercise the limiter set their own limit (see
test_rate_limit_and_status.py).
"""
import os
import sys

import pytest

os.environ.setdefault("CHAT_MSG_RATE_LIMIT", "100000")


@pytest.fixture(autouse=True)
def _reset_message_rate_limiter():
    mod = sys.modules.get("app")
    if mod is not None and hasattr(mod, "message_rate_limiter"):
        mod.message_rate_limiter.reset()
    yield
