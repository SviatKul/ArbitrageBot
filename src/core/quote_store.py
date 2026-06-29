"""
Thread-safe in-memory store for real-time quotes from WebSocket feeds.

WebSocket handlers write here; price_feed.py reads from here before
falling back to REST polling. Sub-100ms quote freshness vs 5s polling.

Usage:
    store = QuoteStore()
    store.set(Venue.KALSHI, "KXELON-24DEC31", {
        "yes_best_ask": 0.46, "yes_best_ask_size": 200,
        "no_best_ask":  0.54, "no_best_ask_size":  150,
    })
    quotes = store.get(Venue.KALSHI, "KXELON-24DEC31", max_age=3.0)
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from models.types import Venue


class QuoteStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # (venue_value, market_id) → quote dict
        self._quotes: dict[tuple[str, str], dict] = {}
        self._timestamps: dict[tuple[str, str], float] = {}
        # Venues with active WS connections
        self._live_venues: set[str] = set()

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    @staticmethod
    def _vkey(venue: Venue | str) -> str:
        return venue.value if isinstance(venue, Venue) else str(venue)

    def _key(self, venue: Venue | str, market_id: str) -> tuple[str, str]:
        return (self._vkey(venue), market_id)

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    def set(self, venue: Venue | str, market_id: str, quotes: dict) -> None:
        key = self._key(venue, market_id)
        with self._lock:
            self._quotes[key] = quotes
            self._timestamps[key] = time.monotonic()

    def mark_live(self, venue: Venue | str) -> None:
        with self._lock:
            self._live_venues.add(self._vkey(venue))

    def mark_offline(self, venue: Venue | str) -> None:
        with self._lock:
            self._live_venues.discard(self._vkey(venue))

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def get(
        self,
        venue: Venue | str,
        market_id: str,
        max_age: float = 5.0,
    ) -> Optional[dict]:
        """Return quotes if present and fresher than max_age seconds, else None."""
        key = self._key(venue, market_id)
        with self._lock:
            if key not in self._quotes:
                return None
            if (time.monotonic() - self._timestamps.get(key, 0.0)) > max_age:
                return None
            return dict(self._quotes[key])

    def age(self, venue: Venue | str, market_id: str) -> float:
        """Seconds since last update for this market (inf if never updated)."""
        key = self._key(venue, market_id)
        with self._lock:
            ts = self._timestamps.get(key)
            return (time.monotonic() - ts) if ts is not None else float("inf")

    def is_live(self, venue: Venue | str) -> bool:
        """True if the WS feed for this venue is connected."""
        with self._lock:
            return self._vkey(venue) in self._live_venues

    def stats(self) -> dict:
        with self._lock:
            return {
                "total_markets": len(self._quotes),
                "live_venues": sorted(self._live_venues),
            }
