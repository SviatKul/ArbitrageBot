"""Unit tests for QuoteStore."""

from __future__ import annotations

import threading
import time

import pytest

from core.quote_store import QuoteStore
from models.types import Venue

_Q = {"yes_best_ask": 0.46, "yes_best_ask_size": 100.0, "no_best_ask": 0.54, "no_best_ask_size": 80.0}


def test_set_get_fresh():
    store = QuoteStore()
    store.set(Venue.KALSHI, "KXTEST", _Q)
    result = store.get(Venue.KALSHI, "KXTEST", max_age=5.0)
    assert result == _Q


def test_get_returns_copy():
    store = QuoteStore()
    store.set(Venue.KALSHI, "KXTEST", _Q)
    result = store.get(Venue.KALSHI, "KXTEST", max_age=5.0)
    result["yes_best_ask"] = 0.99
    fresh = store.get(Venue.KALSHI, "KXTEST", max_age=5.0)
    assert fresh["yes_best_ask"] == 0.46  # original unchanged


def test_get_stale_returns_none():
    store = QuoteStore()
    store.set(Venue.POLYMARKET, "poly1", _Q)
    time.sleep(0.05)
    result = store.get(Venue.POLYMARKET, "poly1", max_age=0.01)
    assert result is None


def test_get_missing_returns_none():
    store = QuoteStore()
    assert store.get(Venue.KALSHI, "NONEXISTENT", max_age=10.0) is None


def test_mark_live_offline():
    store = QuoteStore()
    assert not store.is_live(Venue.KALSHI)
    store.mark_live(Venue.KALSHI)
    assert store.is_live(Venue.KALSHI)
    store.mark_offline(Venue.KALSHI)
    assert not store.is_live(Venue.KALSHI)


def test_mark_live_string_venue():
    store = QuoteStore()
    store.mark_live("kalshi")
    assert store.is_live(Venue.KALSHI)
    assert store.is_live("kalshi")


def test_age_grows():
    store = QuoteStore()
    store.set(Venue.KALSHI, "KXTEST", _Q)
    time.sleep(0.05)
    assert store.age(Venue.KALSHI, "KXTEST") >= 0.04


def test_age_unknown_is_inf():
    store = QuoteStore()
    assert store.age(Venue.KALSHI, "UNKNOWN") == float("inf")


def test_stats_total_markets():
    store = QuoteStore()
    store.set(Venue.KALSHI, "M1", _Q)
    store.set(Venue.KALSHI, "M2", _Q)
    store.set(Venue.POLYMARKET, "P1", _Q)
    store.mark_live(Venue.KALSHI)
    s = store.stats()
    assert s["total_markets"] == 3
    assert "kalshi" in s["live_venues"]


def test_overwrite_updates_timestamp():
    store = QuoteStore()
    store.set(Venue.KALSHI, "KXTEST", {"yes_best_ask": 0.40, "yes_best_ask_size": 50.0, "no_best_ask": 0.60, "no_best_ask_size": 50.0})
    time.sleep(0.06)
    # Now stale
    assert store.get(Venue.KALSHI, "KXTEST", max_age=0.01) is None
    # Overwrite refreshes timestamp
    store.set(Venue.KALSHI, "KXTEST", _Q)
    assert store.get(Venue.KALSHI, "KXTEST", max_age=1.0) is not None


def test_concurrent_writes_no_race():
    store = QuoteStore()
    errors = []

    def writer(i: int):
        for j in range(100):
            try:
                store.set(Venue.KALSHI, f"M{i}", {"yes_best_ask": i * 0.01 + j * 0.0001,
                                                    "yes_best_ask_size": float(j),
                                                    "no_best_ask": 0.5,
                                                    "no_best_ask_size": 100.0})
            except Exception as e:
                errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert store.stats()["total_markets"] == 10
