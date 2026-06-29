"""
Cross-venue binary-market arbitrage detector — works with any pair of exchanges.

Quotes are read from Market.extra using stable keys (filled by price_feed):
  yes_best_ask / yes_best_ask_size / no_best_ask / no_best_ask_size
"""

from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from loguru import logger

from config.settings import Settings
from models.types import ArbitrageOpportunity, Market, Venue

YES_BEST_ASK = "yes_best_ask"
YES_BEST_ASK_SIZE = "yes_best_ask_size"
NO_BEST_ASK = "no_best_ask"
NO_BEST_ASK_SIZE = "no_best_ask_size"

MatchPair = Tuple[Market, Market]


def _fee_for_venue(settings: Settings, venue: Venue) -> float:
    rates: dict[Venue, float] = {
        Venue.KALSHI: float(settings.kalshi_taker_fee_rate),
        Venue.POLYMARKET: float(settings.polymarket_taker_fee_rate),
        Venue.BETFAIR: float(settings.betfair_commission),
        Venue.SMARKETS: float(settings.smarkets_commission),
        Venue.BETDAQ: float(settings.betdaq_commission),
        Venue.MATCHBOOK: float(settings.matchbook_commission),
    }
    return rates.get(venue, 0.0)


def _float_extra(m: Market, key: str) -> Optional[float]:
    raw = dict(m.extra).get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def read_best_quotes(m: Market) -> Optional[Tuple[float, float, float, float]]:
    ya = _float_extra(m, YES_BEST_ASK)
    ysz = _float_extra(m, YES_BEST_ASK_SIZE)
    na = _float_extra(m, NO_BEST_ASK)
    nsz = _float_extra(m, NO_BEST_ASK_SIZE)
    if ya is None or ysz is None or na is None or nsz is None:
        return None
    if ya < 0 or na < 0 or ysz <= 0 or nsz <= 0:
        return None
    return ya, ysz, na, nsz


def _in_universe(m: Market, universe: Sequence[Market]) -> bool:
    return any(x.venue == m.venue and x.market_id == m.market_id for x in universe)


def _prices_consistent(market_a: Market, market_b: Market, tolerance: float = 0.18) -> bool:
    """
    Для одного и того же события: YES_A_prob + YES_B_prob ≈ 1.0

    Если рынок A считает вероятность YES = 0.44,
    то рынок B (об том же событии) должен давать YES ≈ 0.56 (= 1 - 0.44).
    Расхождение больше tolerance → скорее всего разные события.
    """
    ya = _float_extra(market_a, YES_BEST_ASK)
    yb = _float_extra(market_b, YES_BEST_ASK)
    if ya is None or yb is None:
        return True  # нет данных — не блокируем
    deviation = abs((ya + yb) - 1.0)
    return deviation <= tolerance


def _quote_is_fresh(m: Market, max_age: float) -> bool:
    ts = dict(m.extra).get("_quote_ts")
    if ts is None:
        return True  # нет метки — не блокируем (тесты без метки)
    return (time.monotonic() - float(ts)) <= max_age


def _economics(
    *,
    ask_yes: float,
    ask_yes_size: float,
    fee_yes_venue: float,
    ask_no: float,
    ask_no_size: float,
    fee_no_venue: float,
) -> Tuple[float, float, float]:
    """
    Compute post-fee arbitrage economics.

    total_cost   = ask_yes + ask_no
    gross_profit = 1 − total_cost
    worst_fee    = max(fee_yes × (1 − ask_yes), fee_no × (1 − ask_no))
    net_profit   = gross_profit − worst_fee
    profit_pct   = net_profit / total_cost × 100
    """
    total = ask_yes + ask_no
    gross = 1.0 - total
    fee_if_yes_wins = fee_yes_venue * (1.0 - ask_yes)
    fee_if_no_wins = fee_no_venue * (1.0 - ask_no)
    worst_fee = max(fee_if_yes_wins, fee_if_no_wins)
    profit = gross - worst_fee
    pct = (profit / total * 100.0) if total > 0 else 0.0
    return total, profit, pct


class ArbitrageDetector:
    """
    Finds synthetic-box opportunities across any pair of matched markets.

    Each (market_a, market_b) pair is checked in both directions:
      Direction 1: YES on market_a + NO on market_b
      Direction 2: YES on market_b + NO on market_a
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def find_opportunities(
        self,
        all_markets_a: Sequence[Market],
        all_markets_b: Sequence[Market],
        matches: Sequence[MatchPair],
    ) -> List[ArbitrageOpportunity]:
        out: list[ArbitrageOpportunity] = []

        for market_a, market_b in matches:
            if not _in_universe(market_a, all_markets_a) or not _in_universe(market_b, all_markets_b):
                logger.debug(
                    "Skipping stale match: {}/{}", market_a.market_id, market_b.market_id
                )
                continue

            if not _prices_consistent(market_a, market_b):
                logger.debug(
                    "Price inconsistency: YES_A={:.2f} YES_B={:.2f} sum={:.2f} — skipping",
                    dict(market_a.extra).get(YES_BEST_ASK, 0),
                    dict(market_b.extra).get(YES_BEST_ASK, 0),
                    (dict(market_a.extra).get(YES_BEST_ASK, 0) or 0)
                    + (dict(market_b.extra).get(YES_BEST_ASK, 0) or 0),
                )
                continue

            max_age = float(self._settings.quote_max_age_seconds)
            if not _quote_is_fresh(market_a, max_age):
                logger.debug("Stale quote for {} — skipping", market_a.market_id)
                continue
            if not _quote_is_fresh(market_b, max_age):
                logger.debug("Stale quote for {} — skipping", market_b.market_id)
                continue

            q_a = read_best_quotes(market_a)
            q_b = read_best_quotes(market_b)
            if q_a is None or q_b is None:
                continue

            a_yes, a_yes_sz, a_no, a_no_sz = q_a
            b_yes, b_yes_sz, b_no, b_no_sz = q_b

            # Direction 1: YES on market_a + NO on market_b
            opp1 = self._try_direction(
                yes_market=market_a, no_market=market_b,
                ask_yes=a_yes, ask_yes_size=a_yes_sz,
                ask_no=b_no, ask_no_size=b_no_sz,
                tag=f"YES@{market_a.venue.value} + NO@{market_b.venue.value}",
            )
            if opp1 is not None:
                out.append(opp1)

            # Direction 2: YES on market_b + NO on market_a
            opp2 = self._try_direction(
                yes_market=market_b, no_market=market_a,
                ask_yes=b_yes, ask_yes_size=b_yes_sz,
                ask_no=a_no, ask_no_size=a_no_sz,
                tag=f"YES@{market_b.venue.value} + NO@{market_a.venue.value}",
            )
            if opp2 is not None:
                out.append(opp2)

        out.sort(key=lambda o: o.profit or 0.0, reverse=True)
        return out

    def _try_direction(
        self,
        *,
        yes_market: Market,
        no_market: Market,
        ask_yes: float,
        ask_yes_size: float,
        ask_no: float,
        ask_no_size: float,
        tag: str,
    ) -> Optional[ArbitrageOpportunity]:
        min_liq = float(self._settings.min_leg_liquidity)
        if ask_yes_size <= min_liq or ask_no_size <= min_liq:
            return None

        fee_yes = _fee_for_venue(self._settings, yes_market.venue)
        fee_no = _fee_for_venue(self._settings, no_market.venue)

        # Добавляем буфер на AMM-проскальзывание Polymarket
        buf = float(self._settings.amm_slippage_buffer)
        eff_yes = ask_yes + (buf if yes_market.venue == Venue.POLYMARKET else 0.0)
        eff_no  = ask_no  + (buf if no_market.venue  == Venue.POLYMARKET else 0.0)

        total, profit, profit_pct = _economics(
            ask_yes=eff_yes, ask_yes_size=ask_yes_size, fee_yes_venue=fee_yes,
            ask_no=eff_no,   ask_no_size=ask_no_size,   fee_no_venue=fee_no,
        )

        if total >= 1.0 or profit <= 0:
            return None
        if profit_pct < float(self._settings.min_profit_percent):
            return None

        max_sz = min(ask_yes_size, ask_no_size)

        return ArbitrageOpportunity(
            yes_market=yes_market,
            no_market=no_market,
            yes_venue=yes_market.venue,
            no_venue=no_market.venue,
            match_score=0.0,
            match_method="detector",
            expected_edge=profit,
            yes_prices={yes_market.venue.value: ask_yes},
            no_prices={no_market.venue.value: ask_no},
            notes=tag,
            total_cost=total,
            profit=profit,
            profit_percent=profit_pct,
            max_executable_size=max_sz,
        )
