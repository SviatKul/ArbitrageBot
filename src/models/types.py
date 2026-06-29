"""Shared domain types for markets, books, and opportunities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


class Venue(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"
    BETFAIR = "betfair"
    SMARKETS = "smarkets"
    BETDAQ = "betdaq"
    MATCHBOOK = "matchbook"


@dataclass(frozen=True)
class Market:
    """Normalized market description from any venue."""

    venue: Venue
    market_id: str
    title: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_kalshi(payload: Mapping[str, Any]) -> "Market":
        return Market(
            venue=Venue.KALSHI,
            market_id=str(payload.get("ticker", "")),
            title=str(payload.get("title", "") or ""),
            extra=dict(payload),
        )

    @staticmethod
    def from_polymarket_clob(payload: Mapping[str, Any]) -> "Market":
        q = payload.get("question") or payload.get("description") or ""
        mid = str(payload.get("condition_id", "") or payload.get("conditionId", ""))
        return Market(
            venue=Venue.POLYMARKET,
            market_id=mid,
            title=str(q),
            extra=dict(payload),
        )

    @staticmethod
    def from_betfair(payload: Mapping[str, Any]) -> "Market":
        name = payload.get("marketName") or payload.get("event", {}).get("name") or ""
        event_name = payload.get("event", {}).get("name") or ""
        title = f"{event_name} — {name}".strip(" —") if event_name else str(name)
        return Market(
            venue=Venue.BETFAIR,
            market_id=str(payload.get("marketId", "")),
            title=title,
            extra=dict(payload),
        )

    @staticmethod
    def from_smarkets(payload: Mapping[str, Any]) -> "Market":
        name = payload.get("name") or payload.get("slug") or ""
        return Market(
            venue=Venue.SMARKETS,
            market_id=str(payload.get("id", "")),
            title=str(name),
            extra=dict(payload),
        )

    def expiry(self) -> Optional[datetime]:
        """Extract expiry/close datetime from raw extra data (venue-agnostic)."""
        extra = dict(self.extra)
        candidates = (
            extra.get("end_date_iso") or extra.get("endDateIso")
            or extra.get("close_time") or extra.get("closeTime")
            or extra.get("marketStartTime")
            or extra.get("scheduled_close_time")
            or extra.get("datetime")
            or extra.get("end_date")
        )
        if not candidates:
            return None
        try:
            raw = str(candidates).replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None


@dataclass(frozen=True)
class PriceLevel:
    price: str
    size: str


@dataclass(frozen=True)
class OrderBook:
    venue: Venue
    market_id: str
    token_id: Optional[str]
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArbitrageOpportunity:
    """
    Cross-venue opportunity: two matched markets plus economics.

    yes_market  — market where we buy YES (any venue)
    no_market   — market where we buy NO  (any venue)
    yes_venue / no_venue derived from the markets above.
    """

    yes_market: Market
    no_market: Market
    yes_venue: Venue
    no_venue: Venue
    match_score: float
    match_method: str
    expected_edge: Optional[float] = None
    yes_prices: Mapping[str, float] = field(default_factory=dict)
    no_prices: Mapping[str, float] = field(default_factory=dict)
    notes: str = ""
    total_cost: Optional[float] = None
    profit: Optional[float] = None
    profit_percent: Optional[float] = None
    max_executable_size: float = 0.0

    # Legacy aliases for backward compat (yes/no market identity depends on venue assignment)
    @property
    def polymarket(self) -> Market:
        if self.yes_venue == Venue.POLYMARKET:
            return self.yes_market
        if self.no_venue == Venue.POLYMARKET:
            return self.no_market
        raise AttributeError("No Polymarket leg in this opportunity")

    @property
    def kalshi(self) -> Market:
        if self.yes_venue == Venue.KALSHI:
            return self.yes_market
        if self.no_venue == Venue.KALSHI:
            return self.no_market
        raise AttributeError("No Kalshi leg in this opportunity")

    @staticmethod
    def from_match(
        market_a: Market,
        market_b: Market,
        *,
        score: float,
        method: str,
        notes: str = "",
    ) -> "ArbitrageOpportunity":
        return ArbitrageOpportunity(
            yes_market=market_a,
            no_market=market_b,
            yes_venue=market_a.venue,
            no_venue=market_b.venue,
            match_score=score,
            match_method=method,
            notes=notes,
        )


def markets_from_kalshi_api(rows: Sequence[Mapping[str, Any]]) -> list[Market]:
    return [Market.from_kalshi(r) for r in rows]


def markets_from_polymarket_api(rows: Sequence[Mapping[str, Any]]) -> list[Market]:
    return [Market.from_polymarket_clob(r) for r in rows]
