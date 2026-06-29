"""Tests for PnLTracker, TradeLogger, and circuit_breaker.record_pnl integration."""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

import pytest

from core.pnl_tracker import PnLTracker
from core.trade_log import TradeLogger
from core.circuit_breaker import CircuitBreaker


# ─────────────────────────── PnLTracker ────────────────────────────────── #

def _tracker(tmp_path: Path) -> PnLTracker:
    return PnLTracker(tmp_path / "pnl.json")


def test_pnl_tracker_starts_at_zero(tmp_path):
    t = _tracker(tmp_path)
    assert t.today() == 0.0


def test_pnl_tracker_record_accumulates(tmp_path):
    t = _tracker(tmp_path)
    t.record(5.0)
    t.record(3.25)
    assert t.today() == pytest.approx(8.25)


def test_pnl_tracker_persists_across_instances(tmp_path):
    path = tmp_path / "pnl.json"
    t1 = PnLTracker(path)
    t1.record(12.5)
    t2 = PnLTracker(path)
    assert t2.today() == pytest.approx(12.5)


def test_pnl_tracker_history_sorted(tmp_path):
    t = _tracker(tmp_path)
    t.record(1.0)
    hist = t.history(30)
    assert len(hist) >= 1
    dates = [h["date"] for h in hist]
    assert dates == sorted(dates)


def test_pnl_tracker_history_capped(tmp_path):
    t = _tracker(tmp_path)
    t.record(1.0)
    hist = t.history(days=5)
    assert len(hist) <= 5


def test_pnl_tracker_negative_delta(tmp_path):
    t = _tracker(tmp_path)
    t.record(10.0)
    t.record(-3.0)
    assert t.today() == pytest.approx(7.0)


# ─────────────────────────── TradeLogger ───────────────────────────────── #

def _logger(tmp_path: Path) -> TradeLogger:
    return TradeLogger(tmp_path / "trades.csv")


def test_trade_logger_creates_csv_with_header(tmp_path):
    tl = _logger(tmp_path)
    path = tmp_path / "trades.csv"
    assert path.exists()
    with open(path) as f:
        header = f.readline().strip()
    assert "timestamp_utc" in header
    assert "spread_pct" in header


def test_trade_logger_log_opportunity(tmp_path):
    tl = _logger(tmp_path)
    tl.log_opportunity(
        title="Will Trump win?",
        yes_venue="polymarket",
        no_venue="kalshi",
        yes_price=0.44,
        no_price=0.44,
        profit_pct=12.0,
    )
    rows = tl.recent(10)
    assert len(rows) == 1
    assert rows[0]["status"] == "found"
    assert rows[0]["yes_venue"] == "polymarket"
    assert float(rows[0]["spread_pct"]) == pytest.approx(12.0)


def test_trade_logger_log_execution(tmp_path):
    tl = _logger(tmp_path)
    tl.log_execution(
        title="Test market",
        yes_venue="polymarket",
        no_venue="kalshi",
        yes_price=0.44,
        no_price=0.44,
        profit_pct=12.0,
        cost_usd=88.0,
        notes="test run",
    )
    rows = tl.recent(10)
    assert rows[0]["status"] == "executed"
    assert float(rows[0]["cost_usd"]) == pytest.approx(88.0)


def test_trade_logger_summary(tmp_path):
    tl = _logger(tmp_path)
    tl.log_opportunity(title="M1", yes_venue="a", no_venue="b",
                       yes_price=0.45, no_price=0.45, profit_pct=10.0)
    tl.log_execution(title="M2", yes_venue="a", no_venue="b",
                     yes_price=0.45, no_price=0.45, profit_pct=8.0, cost_usd=90.0)
    s = tl.summary()
    assert s["total"] == 2
    assert s["executed"] == 1
    assert s["avg_spread_pct"] == pytest.approx(9.0)


def test_trade_logger_recent_limit(tmp_path):
    tl = _logger(tmp_path)
    for i in range(20):
        tl.log_opportunity(title=f"M{i}", yes_venue="a", no_venue="b",
                           yes_price=0.4, no_price=0.5, profit_pct=float(i))
    assert len(tl.recent(5)) == 5
    assert len(tl.recent(100)) == 20


def test_trade_logger_title_truncated(tmp_path):
    tl = _logger(tmp_path)
    long_title = "x" * 200
    tl.log_opportunity(title=long_title, yes_venue="a", no_venue="b",
                       yes_price=0.4, no_price=0.4, profit_pct=5.0)
    rows = tl.recent(1)
    assert len(rows[0]["market"]) <= 80


# ─────────────────────── CircuitBreaker.record_pnl ─────────────────────── #

def test_circuit_breaker_record_pnl_trips_on_loss():
    cb = CircuitBreaker(max_daily_loss=50.0, bankroll=1000.0)
    cb.record_pnl(-60.0)
    assert not cb.can_trade()


def test_circuit_breaker_record_pnl_ok_within_limit():
    cb = CircuitBreaker(max_daily_loss=100.0, bankroll=1000.0)
    cb.record_pnl(-50.0)
    assert cb.can_trade()


def test_circuit_breaker_record_pnl_tracks_cumulative():
    cb = CircuitBreaker(max_daily_loss=30.0, bankroll=1000.0)
    cb.record_pnl(-20.0)
    assert cb.can_trade()
    cb.record_pnl(-15.0)  # total -35, exceeds -30
    assert not cb.can_trade()


def test_circuit_breaker_positive_pnl_does_not_trip():
    cb = CircuitBreaker(max_daily_loss=50.0, bankroll=1000.0)
    cb.record_pnl(100.0)
    assert cb.can_trade()
