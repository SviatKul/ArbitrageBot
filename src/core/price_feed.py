"""
Enrich matched Market pairs with real-time best-ask quotes from any exchange.

Two-stage fetch strategy (minimises total latency):

  Stage 1 — deduplicate all unique markets across every pair and fetch their
             quotes in one parallel batch. A market appearing in 5 pairs is
             fetched exactly once.

  Stage 2 — reconstruct pairs from the in-memory quote map. No extra I/O.

Call ``enrich_matched_pairs()`` after ``MarketMatcher.find_all_matches_multi()``
and before ``ArbitrageDetector.find_opportunities()``.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Optional

from loguru import logger

from models.types import Market, Venue

if TYPE_CHECKING:
    from clients.base import ExchangeClient
    from core.quote_store import QuoteStore
    from core.rate_limiter import VenueRateLimiter

_QUOTE_KEYS = ("yes_best_ask", "yes_best_ask_size", "no_best_ask", "no_best_ask_size")

# Минимальное изменение цены для повторной проверки арбитража.
# Пары, где обе стороны изменились меньше этого — пропускаются детектором.
_DELTA_EPSILON = 0.003  # 0.3% — меньше нашего мин. порога прибыли


class QuoteDeltaFilter:
    """
    Фильтр дельт котировок — пропускает пары без значимого движения цен.

    Логика: если обе стороны пары показали yes_best_ask / no_best_ask с
    изменением < epsilon по сравнению с прошлой итерацией, арбитражная
    ситуация заведомо не изменилась → пропускаем их в find_opportunities().

    Снижает нагрузку на детектор и логи в спокойные периоды.
    """

    def __init__(self, epsilon: float = _DELTA_EPSILON) -> None:
        self._epsilon = epsilon
        # (venue_value, market_id) → (yes_ask, no_ask)
        self._prev: dict[tuple[str, str], tuple[float, float]] = {}

    def _key(self, m: Market) -> tuple[str, str]:
        return (m.venue.value, m.market_id)

    def _quote_snapshot(self, m: Market) -> Optional[tuple[float, float]]:
        extra = dict(m.extra)
        ya = extra.get("yes_best_ask")
        na = extra.get("no_best_ask")
        if ya is None or na is None:
            return None
        try:
            return float(ya), float(na)
        except (TypeError, ValueError):
            return None

    def changed(self, market_a: Market, market_b: Market) -> bool:
        """True если хотя бы одна сторона пары значимо сдвинулась с прошлой итерации."""
        snap_a = self._quote_snapshot(market_a)
        snap_b = self._quote_snapshot(market_b)
        if snap_a is None or snap_b is None:
            return True  # нет данных → считаем изменением

        prev_a = self._prev.get(self._key(market_a))
        prev_b = self._prev.get(self._key(market_b))
        if prev_a is None or prev_b is None:
            return True  # первая итерация

        def _moved(prev: tuple, curr: tuple) -> bool:
            return (
                abs(curr[0] - prev[0]) >= self._epsilon
                or abs(curr[1] - prev[1]) >= self._epsilon
            )

        return _moved(prev_a, snap_a) or _moved(prev_b, snap_b)

    def update(self, markets: list[Market]) -> None:
        """Обновить снимок после итерации детектора."""
        for m in markets:
            snap = self._quote_snapshot(m)
            if snap is not None:
                self._prev[self._key(m)] = snap


def _merge_extra(market: Market, quotes: dict) -> Market:
    new_extra = {**dict(market.extra), **quotes, "_quote_ts": time.monotonic()}
    return Market(
        venue=market.venue,
        market_id=market.market_id,
        title=market.title,
        extra=new_extra,
    )


def _market_key(m: Market) -> tuple[str, str]:
    return (m.venue.value, m.market_id)


def enrich_matched_pairs(
    pairs: list[tuple[Market, Market]],
    *,
    clients: dict[Venue, "ExchangeClient"],
    max_workers: int = 24,
    quote_store: Optional["QuoteStore"] = None,
    ws_max_age: float = 3.0,
    rate_limiter: Optional["VenueRateLimiter"] = None,
) -> tuple[list[tuple[Market, Market]], list[Market], list[Market]]:
    """
    Fetch orderbooks for all matched pairs with maximum parallelism.

    Deduplicates markets before fetching, so if the same market appears in
    multiple pairs it is only requested once. Both legs of every pair are
    fetched concurrently — latency per iteration ≈ max single-request latency.

    Returns:
        enriched_pairs — [(market_a, market_b), ...] with quotes in .extra
        all_a          — flat list of A-side enriched markets
        all_b          — flat list of B-side enriched markets
    """
    if not pairs:
        return [], [], []

    # Stage 1: collect unique markets
    unique: dict[tuple[str, str], Market] = {}
    for a, b in pairs:
        unique[_market_key(a)] = a
        unique[_market_key(b)] = b

    n_unique = len(unique)

    # Stage 2: parallel quote fetch — WebSocket store first, REST as fallback
    ws_hits = 0

    def _fetch(market: Market) -> tuple[tuple[str, str], Optional[dict]]:
        nonlocal ws_hits
        # Try WebSocket store first (sub-millisecond, no I/O)
        if quote_store is not None and quote_store.is_live(market.venue):
            cached = quote_store.get(market.venue, market.market_id, max_age=ws_max_age)
            if cached is not None:
                ws_hits += 1
                return _market_key(market), cached

        # Fall back to REST polling (rate-limited)
        client = clients.get(market.venue)
        if client is None:
            return _market_key(market), None
        if rate_limiter is not None:
            rate_limiter.acquire(market.venue)
        try:
            return _market_key(market), client.best_quotes(market)
        except Exception as exc:
            logger.warning("Quote error {}/{}: {}", market.venue.value, market.market_id, exc)
            return _market_key(market), None

    quote_map: dict[tuple[str, str], Optional[dict]] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, n_unique)) as pool:
        futures = {pool.submit(_fetch, m): k for k, m in unique.items()}
        for fut in as_completed(futures):
            key, quotes = fut.result()
            quote_map[key] = quotes

    # Stage 3: reconstruct enriched pairs from the quote map (no I/O)
    enriched: list[tuple[Market, Market]] = []
    for a, b in pairs:
        qa = quote_map.get(_market_key(a))
        qb = quote_map.get(_market_key(b))
        if qa is None:
            logger.debug("No quotes for {}/{}", a.venue.value, a.market_id)
            continue
        if qb is None:
            logger.debug("No quotes for {}/{}", b.venue.value, b.market_id)
            continue
        enriched.append((_merge_extra(a, qa), _merge_extra(b, qb)))

    all_a = [a for a, _ in enriched]
    all_b = [b for _, b in enriched]
    rest_calls = n_unique - ws_hits
    if ws_hits:
        logger.info(
            "Price feed: {}/{} pairs enriched | {} WS hits + {} REST calls",
            len(enriched), len(pairs), ws_hits, rest_calls,
        )
    else:
        logger.info(
            "Price feed: {}/{} pairs enriched | {} REST calls",
            len(enriched), len(pairs), rest_calls,
        )
    return enriched, all_a, all_b
