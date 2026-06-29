"""Unit tests for TradeExecutor — dry-run mode, multi-venue routing."""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import pytest

from config.settings import get_settings
from core.trade_executor import TradeExecutor
from models.types import ArbitrageOpportunity, Market, Venue


def _mkt(venue: Venue, mid: str) -> Market:
    return Market(venue=venue, market_id=mid, title="Test Market", extra={
        "yes_best_ask": 0.44, "yes_best_ask_size": 200,
        "no_best_ask": 0.44,  "no_best_ask_size": 200,
    })


def _opp(yes_venue: Venue, no_venue: Venue) -> ArbitrageOpportunity:
    ym = _mkt(yes_venue, "YES_MKT")
    nm = _mkt(no_venue,  "NO_MKT")
    return ArbitrageOpportunity(
        yes_market=ym, no_market=nm,
        yes_venue=yes_venue, no_venue=no_venue,
        match_score=90.0, match_method="fuzzy",
        yes_prices={yes_venue.value: 0.44},
        no_prices={no_venue.value: 0.44},
        total_cost=0.88, profit=0.12, profit_percent=13.6,
        max_executable_size=100.0,
    )


@pytest.fixture
def dry_settings():
    s = get_settings()
    s.__dict__["dry_run"] = True
    s.__dict__["max_order_contracts"] = 50
    return s


def test_dry_run_returns_true_no_clients_called(dry_settings):
    """В dry-run режиме execute_arbitrage возвращает True без обращений к биржам."""
    mock_poly = MagicMock()
    mock_bf   = MagicMock()

    clients = {Venue.POLYMARKET: mock_poly, Venue.BETFAIR: mock_bf}
    executor = TradeExecutor(dry_settings, clients)

    opp = _opp(Venue.POLYMARKET, Venue.BETFAIR)
    result = executor.execute_arbitrage(opp)

    assert result is True
    mock_poly.place_order.assert_not_called()
    mock_bf.place_order.assert_not_called()


def test_dry_run_poly_smarkets(dry_settings):
    """Dry-run работает для любой пары бирж."""
    clients = {
        Venue.POLYMARKET: MagicMock(),
        Venue.SMARKETS:   MagicMock(),
    }
    executor = TradeExecutor(dry_settings, clients)
    opp = _opp(Venue.POLYMARKET, Venue.SMARKETS)
    assert executor.execute_arbitrage(opp) is True


def test_dry_run_respects_max_contracts(dry_settings):
    """Размер ордера ограничен max_order_contracts."""
    placed_calls = []

    class FakeClient:
        @property
        def venue(self): return Venue.BETFAIR
        def place_order(self, market, side, quantity, price):
            placed_calls.append(quantity)
            return "order-1"
        def get_order_status(self, oid): return {"filled": 9999}
        def cancel_order(self, oid): pass
        def close(self): pass

    # live режим
    s = get_settings()
    s.__dict__["dry_run"] = False
    s.__dict__["max_order_contracts"] = 10
    s.__dict__["order_fill_timeout_seconds"] = 0.1
    s.__dict__["execution_poll_interval_seconds"] = 0.05

    # Оппортунити с size=200, но cap=10
    opp = _opp(Venue.POLYMARKET, Venue.BETFAIR)
    opp.__dict__["max_executable_size"] = 200.0

    fake = FakeClient()
    clients = {Venue.POLYMARKET: fake, Venue.BETFAIR: fake}
    executor = TradeExecutor(s, clients)

    # Не проверяем результат (нет реальных fills), просто убеждаемся что qty <= cap
    executor.execute_arbitrage(opp)
    for qty in placed_calls:
        assert qty <= 10


def test_execute_missing_client_returns_false(dry_settings):
    """Нет клиента для NO venue → False."""
    dry_settings.__dict__["dry_run"] = False
    clients = {Venue.POLYMARKET: MagicMock()}  # Betfair отсутствует

    executor = TradeExecutor(dry_settings, clients)
    opp = _opp(Venue.POLYMARKET, Venue.BETFAIR)
    assert executor.execute_arbitrage(opp) is False


def test_kelly_sizing_increases_with_edge(dry_settings):
    """Kelly criterion: больший спред → больший размер позиции."""
    dry_settings.__dict__["kelly_enabled"] = True
    dry_settings.__dict__["kelly_fraction"] = 0.25
    dry_settings.__dict__["kelly_bankroll"] = 1000.0
    dry_settings.__dict__["max_order_contracts"] = 500

    executor = TradeExecutor(dry_settings, {})

    small_edge = ArbitrageOpportunity(
        yes_market=_mkt(Venue.POLYMARKET, "M1"),
        no_market=_mkt(Venue.BETFAIR, "M2"),
        yes_venue=Venue.POLYMARKET, no_venue=Venue.BETFAIR,
        match_score=90.0, match_method="fuzzy",
        yes_prices={}, no_prices={},
        total_cost=0.98, profit=0.02, profit_percent=2.0,
        max_executable_size=500.0,
    )
    large_edge = ArbitrageOpportunity(
        yes_market=_mkt(Venue.POLYMARKET, "M3"),
        no_market=_mkt(Venue.BETFAIR, "M4"),
        yes_venue=Venue.POLYMARKET, no_venue=Venue.BETFAIR,
        match_score=90.0, match_method="fuzzy",
        yes_prices={}, no_prices={},
        total_cost=0.80, profit=0.20, profit_percent=20.0,
        max_executable_size=500.0,
    )
    assert executor._kelly_qty(small_edge) < executor._kelly_qty(large_edge)


def test_kelly_disabled_uses_max_contracts(dry_settings):
    """Без Kelly: размер = max_order_contracts."""
    dry_settings.__dict__["kelly_enabled"] = False
    dry_settings.__dict__["max_order_contracts"] = 77
    executor = TradeExecutor(dry_settings, {})

    opp = _opp(Venue.POLYMARKET, Venue.BETFAIR)
    assert executor._kelly_qty(opp) == 77


def test_dry_run_records_positions_in_manager(dry_settings):
    """После dry-run исполнения PositionManager должен содержать обе ноги."""
    from core.position_manager import PositionManager
    pm = PositionManager()
    executor = TradeExecutor(dry_settings, {}, position_manager=pm)

    opp = _opp(Venue.POLYMARKET, Venue.KALSHI)
    result = executor.execute_arbitrage(opp)

    assert result is True
    positions = pm.list_open_positions()
    assert len(positions) == 2
    sides = {p.side for p in positions}
    assert sides == {"YES", "NO"}
    venues = {p.venue for p in positions}
    assert Venue.POLYMARKET in venues
    assert Venue.KALSHI in venues


def test_execute_without_position_manager_does_not_raise(dry_settings):
    """Если position_manager не передан — просто не пишем позиции, не падаем."""
    executor = TradeExecutor(dry_settings, {})  # no position_manager
    opp = _opp(Venue.POLYMARKET, Venue.KALSHI)
    assert executor.execute_arbitrage(opp) is True


def test_position_manager_not_updated_on_missing_client(dry_settings):
    """При отсутствии клиента для live-трейда позиции не записываются."""
    from core.position_manager import PositionManager
    dry_settings.__dict__["dry_run"] = False  # live mode
    pm = PositionManager()
    executor = TradeExecutor(dry_settings, {}, position_manager=pm)  # no clients

    opp = _opp(Venue.POLYMARKET, Venue.KALSHI)
    result = executor.execute_arbitrage(opp)

    assert result is False
    assert len(pm.list_open_positions()) == 0
