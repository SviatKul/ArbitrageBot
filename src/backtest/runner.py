"""
Backtesting runner — replays market data through the full arbitrage pipeline.

Two modes:
  1. Live snapshot (--live): fetches current quotes from all configured exchanges
     and runs one pass through the pipeline to report what would be traded NOW.
  2. CSV replay  (--csv FILE): reads a CSV of historical quote snapshots and
     replays them in time order, accumulating simulated P&L.

CSV format (one row = one quote snapshot):
  timestamp_utc, venue, market_id, title, yes_best_ask, no_best_ask

Output:
  - Opportunities found (venue A × venue B, spread %)
  - Simulated P&L assuming Kelly-sized position or fixed $10 per trade
  - Summary: win rate, avg spread, total theoretical P&L
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


@dataclass
class BacktestResult:
    opportunities: list[dict] = field(default_factory=list)
    total_trades: int = 0
    total_pnl_usd: float = 0.0
    avg_spread_pct: float = 0.0
    max_spread_pct: float = 0.0
    start_ts: Optional[str] = None
    end_ts: Optional[str] = None

    def summary(self) -> dict:
        spreads = [o["profit_pct"] for o in self.opportunities]
        return {
            "total_opportunities": len(self.opportunities),
            "total_trades_simulated": self.total_trades,
            "total_pnl_usd": round(self.total_pnl_usd, 4),
            "avg_spread_pct": round(sum(spreads) / len(spreads), 4) if spreads else 0.0,
            "max_spread_pct": round(max(spreads), 4) if spreads else 0.0,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
        }

    def print_report(self) -> None:
        s = self.summary()
        print(f"\n{'='*60}")
        print("  BACKTEST REPORT")
        print(f"{'='*60}")
        print(f"  Period:          {s['start_ts']} → {s['end_ts']}")
        print(f"  Opportunities:   {s['total_opportunities']}")
        print(f"  Simulated trades:{s['total_trades_simulated']}")
        print(f"  Avg spread:      {s['avg_spread_pct']:.2f}%")
        print(f"  Max spread:      {s['max_spread_pct']:.2f}%")
        sign = "+" if s["total_pnl_usd"] >= 0 else ""
        print(f"  Theoretical P&L: {sign}${s['total_pnl_usd']:.2f}")
        print(f"{'='*60}\n")

        if self.opportunities:
            print(f"  Top 10 opportunities:")
            for opp in sorted(self.opportunities, key=lambda x: x["profit_pct"], reverse=True)[:10]:
                print(
                    f"    {opp.get('ts','')[:10]}  "
                    f"{opp['yes_venue']:12} × {opp['no_venue']:12}  "
                    f"{opp['profit_pct']:+.2f}%  "
                    f"{opp.get('title','')[:40]}"
                )


def run_live_snapshot(settings, clients: dict, stake_usd: float = 10.0) -> BacktestResult:
    """
    Fetch current quotes from all exchanges and run one arbitrage scan.
    Reports what opportunities exist right now.
    """
    from concurrent.futures import ThreadPoolExecutor

    from core.arbitrage_detector import ArbitrageDetector
    from core.market_matcher import MarketMatcher
    from core.price_feed import enrich_matched_pairs

    result = BacktestResult(start_ts=datetime.now(timezone.utc).isoformat())

    logger.info("Backtest: fetching live markets from {} exchange(s)…", len(clients))

    markets_by_venue: dict = {}
    def fetch(pair):
        venue, client = pair
        try:
            if hasattr(client, "get_markets"):
                mkts = client.get_markets()
                logger.info("  {} → {} markets", venue.value, len(mkts))
                return venue, mkts
        except Exception as e:
            logger.warning("  {} fetch failed: {}", venue.value, e)
        return venue, []

    with ThreadPoolExecutor(max_workers=max(1, len(clients))) as pool:
        for venue, mkts in pool.map(fetch, clients.items()):
            if mkts:
                markets_by_venue[venue] = mkts

    if len(markets_by_venue) < 2:
        logger.warning("Need ≥2 exchanges with data for matching")
        result.end_ts = datetime.now(timezone.utc).isoformat()
        return result

    matcher = MarketMatcher(min_fuzzy_score=float(settings.market_match_min_fuzzy_score))
    pairs = matcher.find_all_matches_multi(markets_by_venue)
    logger.info("Matched {} cross-venue pair(s)", len(pairs))

    if not pairs:
        result.end_ts = datetime.now(timezone.utc).isoformat()
        return result

    enriched, _, _ = enrich_matched_pairs(pairs, clients=clients)

    detector = ArbitrageDetector(settings)
    a_list = [a for a, _ in enriched]
    b_list = [b for _, b in enriched]
    opps = detector.find_opportunities(a_list, b_list, enriched)

    ts = datetime.now(timezone.utc).isoformat()
    for opp in opps:
        pct = opp.profit_percent or 0.0
        pnl = stake_usd * pct / 100.0
        result.opportunities.append({
            "ts": ts,
            "title": opp.yes_market.title,
            "yes_venue": opp.yes_venue.value,
            "no_venue": opp.no_venue.value,
            "yes_price": opp.yes_prices.get(opp.yes_venue.value, 0.0),
            "no_price": opp.no_prices.get(opp.no_venue.value, 0.0),
            "profit_pct": pct,
            "simulated_pnl": pnl,
        })
        result.total_trades += 1
        result.total_pnl_usd += pnl

    result.end_ts = datetime.now(timezone.utc).isoformat()
    return result


def run_csv_replay(csv_path: Path, settings, stake_usd: float = 10.0) -> BacktestResult:
    """
    Replay quote snapshots from CSV and simulate arbitrage decisions.

    CSV columns: timestamp_utc, venue, market_id, title, yes_best_ask, no_best_ask
    """
    from collections import defaultdict

    from core.arbitrage_detector import ArbitrageDetector
    from core.market_matcher import MarketMatcher
    from models.types import Market, Venue

    result = BacktestResult()

    if not csv_path.is_file():
        logger.error("CSV file not found: {}", csv_path)
        return result

    rows = []
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                rows.append({
                    "ts": row["timestamp_utc"],
                    "venue": Venue(row["venue"]),
                    "market_id": row["market_id"],
                    "title": row.get("title", row["market_id"]),
                    "yes_ask": float(row["yes_best_ask"]),
                    "no_ask": float(row["no_best_ask"]),
                })
            except Exception:
                continue

    if not rows:
        logger.warning("No valid rows in CSV: {}", csv_path)
        return result

    result.start_ts = rows[0]["ts"]
    result.end_ts = rows[-1]["ts"]
    logger.info("CSV replay: {} rows, {} → {}", len(rows), result.start_ts[:10], result.end_ts[:10])

    # Group by timestamp
    by_ts: dict[str, list] = defaultdict(list)
    for row in rows:
        by_ts[row["ts"]].append(row)

    detector = ArbitrageDetector(settings)
    matcher = MarketMatcher(min_fuzzy_score=float(settings.market_match_min_fuzzy_score))

    for ts, snapshot in sorted(by_ts.items()):
        markets_by_venue: dict = defaultdict(list)
        quote_map: dict = {}
        for r in snapshot:
            m = Market(r["venue"], r["market_id"], r["title"], {
                "yes_best_ask": r["yes_ask"],
                "no_best_ask": r["no_ask"],
            })
            markets_by_venue[r["venue"]].append(m)
            quote_map[(r["venue"].value, r["market_id"])] = {
                "yes_best_ask": r["yes_ask"],
                "no_best_ask": r["no_ask"],
            }

        if len(markets_by_venue) < 2:
            continue

        pairs = matcher.find_all_matches_multi(dict(markets_by_venue))
        if not pairs:
            continue

        # Inject saved quotes directly (no HTTP calls in backtest)
        enriched = []
        for a, b in pairs:
            qa = quote_map.get((a.venue.value, a.market_id))
            qb = quote_map.get((b.venue.value, b.market_id))
            if qa:
                a.extra.update(qa)
            if qb:
                b.extra.update(qb)
            if qa and qb:
                enriched.append((a, b))

        a_list = [a for a, _ in enriched]
        b_list = [b for _, b in enriched]
        opps = detector.find_opportunities(a_list, b_list, enriched)

        for opp in opps:
            pct = opp.profit_percent or 0.0
            pnl = stake_usd * pct / 100.0
            result.opportunities.append({
                "ts": ts,
                "title": opp.yes_market.title,
                "yes_venue": opp.yes_venue.value,
                "no_venue": opp.no_venue.value,
                "yes_price": opp.yes_prices.get(opp.yes_venue.value, 0.0),
                "no_price": opp.no_prices.get(opp.no_venue.value, 0.0),
                "profit_pct": pct,
                "simulated_pnl": pnl,
            })
            result.total_trades += 1
            result.total_pnl_usd += pnl

    return result
