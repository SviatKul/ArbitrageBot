"""
Per-venue token-bucket rate limiter.

Prevents API bans by enforcing each exchange's documented request rate.
All clients share the same limiter instance via VenueRateLimiter passed through
price_feed and market-fetch calls.

Rate limits (conservative estimates):
  Polymarket CLOB  50 req/s, burst 60
  Kalshi           10 req/s, burst 15
  Betfair          20 req/s, burst 25
  Smarkets         10 req/s, burst 15
  Betdaq           10 req/s, burst 15
  Matchbook         8 req/s, burst 12
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from loguru import logger
from models.types import Venue


class _TokenBucket:
    """Thread-safe token bucket for one venue."""

    def __init__(self, rate: float, burst: float) -> None:
        self._rate = float(rate)    # tokens added per second
        self._burst = float(burst)  # max bucket size
        self._tokens = float(burst) # start full
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        """Block until a token is available or timeout expires. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._burst,
                    self._tokens + (now - self._last) * self._rate,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            sleep_for = min(0.02, deadline - time.monotonic())
            if sleep_for <= 0:
                return False
            time.sleep(sleep_for)

    @property
    def available(self) -> float:
        with self._lock:
            now = time.monotonic()
            return min(self._burst, self._tokens + (now - self._last) * self._rate)


# Default rates per venue
_DEFAULTS: dict[str, tuple[float, float]] = {
    Venue.POLYMARKET.value: (50.0, 60.0),
    Venue.KALSHI.value:     (10.0, 15.0),
    Venue.BETFAIR.value:    (20.0, 25.0),
    Venue.SMARKETS.value:   (10.0, 15.0),
    Venue.BETDAQ.value:     (10.0, 15.0),
    Venue.MATCHBOOK.value:  (8.0,  12.0),
}


class VenueRateLimiter:
    """
    Rate limiter for all configured exchanges.

    Usage:
        limiter = VenueRateLimiter()
        limiter.acquire(Venue.KALSHI)  # blocks if bucket empty
    """

    def __init__(self, overrides: Optional[dict[str, tuple[float, float]]] = None) -> None:
        cfg = {**_DEFAULTS, **(overrides or {})}
        self._buckets: dict[str, _TokenBucket] = {
            venue: _TokenBucket(rate, burst)
            for venue, (rate, burst) in cfg.items()
        }

    def acquire(self, venue: Venue | str, timeout: float = 30.0) -> bool:
        """
        Acquire one request token for venue.
        Returns True immediately if under rate limit, blocks otherwise.
        Returns False if timed out (caller should skip the request).
        """
        key = venue.value if isinstance(venue, Venue) else str(venue)
        bucket = self._buckets.get(key)
        if bucket is None:
            return True  # unknown venue — no limit
        ok = bucket.acquire(timeout=timeout)
        if not ok:
            logger.warning("Rate limiter timeout for {} — skipping request", key)
        return ok

    def available(self, venue: Venue | str) -> float:
        """Current available tokens (informational)."""
        key = venue.value if isinstance(venue, Venue) else str(venue)
        b = self._buckets.get(key)
        return b.available if b else float("inf")
