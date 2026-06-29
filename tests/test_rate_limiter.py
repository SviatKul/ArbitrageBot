"""Unit tests for VenueRateLimiter (token-bucket)."""

from __future__ import annotations

import time

import pytest

from core.rate_limiter import VenueRateLimiter, _TokenBucket
from models.types import Venue


def test_acquire_returns_true_when_tokens_available():
    rl = VenueRateLimiter()
    assert rl.acquire(Venue.KALSHI) is True


def test_five_fast_requests_under_burst():
    rl = VenueRateLimiter()
    t0 = time.monotonic()
    for _ in range(5):
        ok = rl.acquire(Venue.POLYMARKET)
        assert ok is True
    assert time.monotonic() - t0 < 0.1


def test_unknown_venue_always_true():
    rl = VenueRateLimiter()
    assert rl.acquire("some_unknown_exchange_xyz") is True


def test_timeout_returns_false_when_empty():
    # Rate 2/s burst 1: drain bucket then immediately try again with tiny timeout
    bucket = _TokenBucket(rate=2.0, burst=1.0)
    bucket.acquire(timeout=5.0)   # drains the 1 token
    ok = bucket.acquire(timeout=0.02)   # should timeout before refill
    assert ok is False


def test_bucket_refills_over_time():
    # Rate 10/s burst 1: drain, wait 0.15s, should have ~1.5 tokens → succeed
    bucket = _TokenBucket(rate=10.0, burst=1.0)
    bucket.acquire(timeout=1.0)   # drain
    time.sleep(0.12)
    ok = bucket.acquire(timeout=0.0)
    # After 0.12s at 10/s we have ~1.2 tokens, so next acquire should succeed
    assert ok is True


def test_available_property():
    bucket = _TokenBucket(rate=100.0, burst=50.0)
    assert bucket.available > 0
    assert bucket.available <= 50.0


def test_rate_limiter_custom_overrides():
    rl = VenueRateLimiter(overrides={"polymarket": (1000.0, 1000.0)})
    t0 = time.monotonic()
    for _ in range(20):
        rl.acquire(Venue.POLYMARKET)
    assert time.monotonic() - t0 < 0.05


def test_all_configured_venues_have_limiters():
    rl = VenueRateLimiter()
    for venue in Venue:
        # Should not raise and should return True (buckets start full)
        assert rl.acquire(venue) is True
