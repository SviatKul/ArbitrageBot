"""Unit tests for ArbitrageDetector — generic multi-venue."""

from __future__ import annotations

import pytest

from core.arbitrage_detector import ArbitrageDetector, _economics
from config.settings import get_settings
from models.types import ArbitrageOpportunity, Market, Venue


def _market(venue: Venue, mid: str, title: str, **quotes) -> Market:
    return Market(venue=venue, market_id=mid, title=title, extra=dict(quotes))


def _poly(mid: str, title: str, **q) -> Market:
    return _market(Venue.POLYMARKET, mid, title, **q)


def _betfair(mid: str, title: str, **q) -> Market:
    return _market(Venue.BETFAIR, mid, title, **q)


def _smarkets(mid: str, title: str, **q) -> Market:
    return _market(Venue.SMARKETS, mid, title, **q)


@pytest.fixture
def detector():
    s = get_settings()
    s.__dict__["min_profit_percent"] = 0.0   # Порог 0 чтобы ловить любой профит
    s.__dict__["min_leg_liquidity"] = 0.0
    return ArbitrageDetector(s)


# ------------------------------------------------------------------ #
# Тест математики _economics
# ------------------------------------------------------------------ #

def test_economics_no_fee_clear_arb():
    """YES=0.45 + NO=0.45 = 0.90 → прибыль 0.10 / 11.1%"""
    total, profit, pct = _economics(
        ask_yes=0.45, ask_yes_size=100, fee_yes_venue=0.0,
        ask_no=0.45,  ask_no_size=100, fee_no_venue=0.0,
    )
    assert abs(total - 0.90) < 1e-9
    assert abs(profit - 0.10) < 1e-9
    assert abs(pct - (0.10 / 0.90 * 100)) < 1e-6


def test_economics_fee_eats_profit():
    """YES=0.49 + NO=0.49 = 0.98, gross=0.02, но 5% Betfair fee съедает всё."""
    total, profit, pct = _economics(
        ask_yes=0.49, ask_yes_size=100, fee_yes_venue=0.05,
        ask_no=0.49,  ask_no_size=100, fee_no_venue=0.0,
    )
    assert total == pytest.approx(0.98)
    # fee_if_yes_wins = 0.05 * (1-0.49) = 0.0255; gross = 0.02
    assert profit < 0  # fee > gross → убыток


def test_economics_no_arb_when_total_above_1():
    _, profit, _ = _economics(
        ask_yes=0.55, ask_yes_size=100, fee_yes_venue=0.0,
        ask_no=0.55,  ask_no_size=100, fee_no_venue=0.0,
    )
    assert profit < 0


# ------------------------------------------------------------------ #
# Тест детектора: находит возможность
# ------------------------------------------------------------------ #

def test_detector_finds_polymarket_betfair_arb(detector):
    """YES на Polymarket 0.44, NO на Betfair 0.44 → суммарно 0.88 → арбитраж."""
    poly = _poly("P1", "Trump wins 2028", yes_best_ask=0.44, yes_best_ask_size=500,
                 no_best_ask=0.58, no_best_ask_size=500)
    bf = _betfair("B1", "Trump wins 2028", yes_best_ask=0.58, yes_best_ask_size=200,
                  no_best_ask=0.44, no_best_ask_size=200)

    opps = detector.find_opportunities([poly], [bf], [(poly, bf)])
    assert len(opps) > 0
    best = opps[0]
    assert best.profit_percent > 0
    assert best.yes_venue in (Venue.POLYMARKET, Venue.BETFAIR)
    assert best.no_venue in (Venue.POLYMARKET, Venue.BETFAIR)
    assert best.yes_venue != best.no_venue


def test_detector_finds_best_direction(detector):
    """Детектор выбирает более выгодное из двух направлений."""
    poly = _poly("P1", "Fed cuts rates", yes_best_ask=0.40, yes_best_ask_size=1000,
                 no_best_ask=0.62, no_best_ask_size=1000)
    bf = _betfair("B1", "Fed cuts rates", yes_best_ask=0.64, yes_best_ask_size=300,
                  no_best_ask=0.38, no_best_ask_size=300)

    opps = detector.find_opportunities([poly], [bf], [(poly, bf)])
    assert opps[0].profit_percent >= opps[-1].profit_percent  # отсортированы по убыванию


def test_detector_three_venues(detector):
    """N×N: три рынка с трёх бирж — детектор проверяет все пары."""
    poly = _poly("P1", "Bitcoin above 100k", yes_best_ask=0.42, yes_best_ask_size=100,
                 no_best_ask=0.60, no_best_ask_size=100)
    bf   = _betfair("B1", "Bitcoin above 100k", yes_best_ask=0.60, yes_best_ask_size=100,
                    no_best_ask=0.42, no_best_ask_size=100)
    sm   = _smarkets("S1", "Bitcoin above 100k", yes_best_ask=0.60, yes_best_ask_size=100,
                     no_best_ask=0.41, no_best_ask_size=100)

    # Пара poly+bf и poly+sm
    all_markets = [poly, bf, sm]
    pairs = [(poly, bf), (poly, sm)]

    opps = detector.find_opportunities(all_markets, all_markets, pairs)
    venues_used = {(o.yes_venue, o.no_venue) for o in opps}
    assert len(venues_used) >= 2


def test_detector_no_arb_when_total_above_1(detector):
    """Нет арбитража когда YES+NO > 1."""
    poly = _poly("P1", "Event", yes_best_ask=0.55, yes_best_ask_size=100,
                 no_best_ask=0.55, no_best_ask_size=100)
    bf   = _betfair("B1", "Event", yes_best_ask=0.55, yes_best_ask_size=100,
                    no_best_ask=0.55, no_best_ask_size=100)

    opps = detector.find_opportunities([poly], [bf], [(poly, bf)])
    assert opps == []


def test_detector_respects_min_profit(detector):
    """Возможности ниже порога MIN_PROFIT_PERCENT отбрасываются."""
    s = get_settings()
    s.__dict__["min_profit_percent"] = 5.0  # требуем 5%
    s.__dict__["min_leg_liquidity"] = 0.0
    det = ArbitrageDetector(s)

    # Спред всего 1% — не пройдёт
    poly = _poly("P1", "X", yes_best_ask=0.495, yes_best_ask_size=100,
                 no_best_ask=0.50, no_best_ask_size=100)
    bf   = _betfair("B1", "X", yes_best_ask=0.50, yes_best_ask_size=100,
                    no_best_ask=0.495, no_best_ask_size=100)

    opps = det.find_opportunities([poly], [bf], [(poly, bf)])
    assert opps == []


# ------------------------------------------------------------------ #
# Тест ArbitrageOpportunity legacy aliases
# ------------------------------------------------------------------ #

def test_opportunity_legacy_aliases():
    poly = _poly("PM1", "Test", yes_best_ask=0.45, yes_best_ask_size=100,
                 no_best_ask=0.45, no_best_ask_size=100)
    kal  = _market(Venue.KALSHI, "K1", "Test", yes_best_ask=0.45, yes_best_ask_size=100,
                   no_best_ask=0.45, no_best_ask_size=100)

    opp = ArbitrageOpportunity(
        yes_market=poly, no_market=kal,
        yes_venue=Venue.POLYMARKET, no_venue=Venue.KALSHI,
        match_score=90.0, match_method="fuzzy",
    )
    assert opp.polymarket is poly
    assert opp.kalshi is kal
