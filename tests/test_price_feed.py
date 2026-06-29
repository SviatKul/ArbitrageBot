"""Unit tests for price_feed — mock ExchangeClient."""

from __future__ import annotations

from typing import Optional

from core.price_feed import enrich_matched_pairs
from models.types import Market, Venue


class MockClient:
    def __init__(self, venue: Venue, quotes: dict[str, dict]):
        self._venue = venue
        self._quotes = quotes  # market_id -> quote dict

    @property
    def venue(self) -> Venue:
        return self._venue

    def best_quotes(self, market: Market) -> Optional[dict]:
        return self._quotes.get(market.market_id)

    def get_markets(self, **_):
        return []

    def place_order(self, *a, **kw): raise NotImplementedError
    def get_order_status(self, *a, **kw): return {}
    def cancel_order(self, *a, **kw): pass
    def close(self): pass


def _mkt(venue, mid, title="X"):
    return Market(venue=venue, market_id=mid, title=title, extra={})


def test_enrich_fills_quotes_for_both_venues():
    poly = _mkt(Venue.POLYMARKET, "P1", "Bitcoin 100k")
    bf   = _mkt(Venue.BETFAIR,    "B1", "Bitcoin 100k")

    poly_q = {"yes_best_ask": 0.42, "yes_best_ask_size": 500,
               "no_best_ask": 0.60, "no_best_ask_size": 500}
    bf_q   = {"yes_best_ask": 0.60, "yes_best_ask_size": 200,
               "no_best_ask": 0.42, "no_best_ask_size": 200}

    clients = {
        Venue.POLYMARKET: MockClient(Venue.POLYMARKET, {"P1": poly_q}),
        Venue.BETFAIR:    MockClient(Venue.BETFAIR,    {"B1": bf_q}),
    }

    enriched, all_a, all_b = enrich_matched_pairs([(poly, bf)], clients=clients)

    assert len(enriched) == 1
    ep, eb = enriched[0]
    assert ep.extra["yes_best_ask"] == pytest.approx(0.42)
    assert eb.extra["no_best_ask"]  == pytest.approx(0.42)


def test_enrich_drops_pair_when_client_missing():
    poly = _mkt(Venue.POLYMARKET, "P1")
    sm   = _mkt(Venue.SMARKETS,   "S1")

    clients = {
        Venue.POLYMARKET: MockClient(Venue.POLYMARKET, {"P1": {
            "yes_best_ask": 0.5, "yes_best_ask_size": 100,
            "no_best_ask": 0.5,  "no_best_ask_size": 100,
        }}),
        # Smarkets client отсутствует
    }
    enriched, _, _ = enrich_matched_pairs([(poly, sm)], clients=clients)
    assert enriched == []


def test_enrich_drops_pair_when_quotes_none():
    poly = _mkt(Venue.POLYMARKET, "P1")
    bf   = _mkt(Venue.BETFAIR,    "B1")

    clients = {
        Venue.POLYMARKET: MockClient(Venue.POLYMARKET, {"P1": None}),  # type: ignore
        Venue.BETFAIR:    MockClient(Venue.BETFAIR,    {"B1": {
            "yes_best_ask": 0.5, "yes_best_ask_size": 100,
            "no_best_ask": 0.5,  "no_best_ask_size": 100,
        }}),
    }
    enriched, _, _ = enrich_matched_pairs([(poly, bf)], clients=clients)
    assert enriched == []


def test_enrich_empty_pairs():
    enriched, a, b = enrich_matched_pairs([], clients={})
    assert enriched == [] and a == [] and b == []


import pytest
