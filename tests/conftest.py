
"""Shared pytest setup and process-state isolation."""

import os
import sys

import pytest

os.environ.setdefault("CHAT_MSG_RATE_LIMIT", "100000")


@pytest.fixture(autouse=True)
def _reset_app_process_state():

    def reset_state():
        mod = sys.modules.get("app")

        if mod is None:
            return

        # Reset the process-wide message rate limiter.
        limiter = getattr(mod, "message_rate_limiter", None)
        if limiter is not None:
            limiter.reset()

        # Clear active connection tracking.
        for name in ("connected_users", "sid_to_user"):
            state = getattr(mod, name, None)
            if state is not None:
                state.clear()

        # Clear username caches safely.
        cache_lock = getattr(mod, "user_cache_lock", None)

        if cache_lock is not None:
            with cache_lock:
                for name in ("users_by_username", "usernames_by_id"):
                    cache = getattr(mod, name, None)
                    if cache is not None:
                        cache.clear()

        else:
            for name in ("users_by_username", "usernames_by_id"):
                cache = getattr(mod, name, None)
                if cache is not None:
                    cache.clear()

    reset_state()

    yield

    reset_state()
