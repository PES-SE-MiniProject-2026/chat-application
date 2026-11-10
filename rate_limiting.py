
import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window limiter keyed by an arbitrary value.

    Rejected attempts are not recorded, so a user who keeps hammering the
    send button is allowed again as soon as their oldest accepted message
    leaves the window. State is in-process; for multiple workers use a
    shared store such as Redis.
    """

    SWEEP_THRESHOLD = 1024

    def __init__(self, limit, window_seconds, clock=time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._events = {}
        self._lock = threading.Lock()

    def check(self, key):
        """Record an attempt. Return (allowed, retry_after_seconds)."""
        now = self._clock()
        cutoff = now - self.window

        with self._lock:
            if len(self._events) > self.SWEEP_THRESHOLD:
                self._sweep(cutoff)

            events = self._events.setdefault(key, deque())

            while events and events[0] <= cutoff:
                events.popleft()

            if len(events) >= self.limit:
                return False, max(events[0] + self.window - now, 0.0)

            events.append(now)
            return True, 0.0

    def reset(self, key=None):
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)

    def _sweep(self, cutoff):
        for key in [
            k for k, ev in self._events.items()
            if not ev or ev[-1] <= cutoff
        ]:
            del self._events[key]
