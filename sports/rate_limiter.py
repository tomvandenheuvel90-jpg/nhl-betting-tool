"""Thread-safe rate limiter per API-bron."""

import time
import threading


class RateLimiter:
    """Blokkeert tot het minimum interval tussen calls verstreken is."""

    def __init__(self, calls_per_second: float = 1.0):
        self._interval = 1.0 / calls_per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            wait_time = self._interval - (now - self._last)
            if wait_time > 0:
                time.sleep(wait_time)
            self._last = time.monotonic()


# Geconfigureerde limiters per bron
nhl_limiter       = RateLimiter(calls_per_second=2.0)   # NHL API: royaal
moneypuck_limiter = RateLimiter(calls_per_second=0.3)   # MoneyPuck: conservatief
nba_limiter       = RateLimiter(calls_per_second=0.4)   # NBA.com via nba_api
mlb_limiter       = RateLimiter(calls_per_second=2.0)   # MLB Stats API
soccer_limiter    = RateLimiter(calls_per_second=0.17)  # football-data.org: 10/min
