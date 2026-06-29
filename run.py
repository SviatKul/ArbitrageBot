#!/usr/bin/env python3
"""
Arbitrage bot main loop — multi-exchange: Polymarket, Betfair, Smarkets (+ Kalshi if configured).

Flow per iteration:
  1. Fetch markets from all configured exchanges
  2. N×N fuzzy match to find same events across venues
  3. Enrich matched pairs with live orderbook quotes
  4. Detect arbitrage opportunities (best YES+NO spread across venues)
  5. Execute best opportunity (dry-run or live)
"""

from __future__ import annotations

import json
import queue
import signal
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from clients.betdaq_client import BetdaqClient
from clients.betfair_client import BetfairClient
from clients.kalshi_ws import KalshiWebSocketFeed
from clients.matchbook_client import MatchbookClient
from clients.polymarket_client import PolymarketClobClient
from clients.polymarket_ws import PolymarketWebSocketFeed
from clients.smarkets_client import SmarketsClient
from clients.kalshi_client import KalshiClient
from config.settings import Settings, get_settings
from core.arbitrage_detector import ArbitrageDetector
from core.circuit_breaker import CircuitBreaker
from core.market_matcher import MarketMatcher
from core.position_manager import PositionManager
from core.price_feed import QuoteDeltaFilter, enrich_matched_pairs
from core.quote_store import QuoteStore
from core.pnl_tracker import PnLTracker
from core.rate_limiter import VenueRateLimiter
from core.trade_log import TradeLogger
from utils.telegram import TelegramNotifier
from core.trade_executor import TradeExecutor
from strategies.auto_close import AutoCloseStrategy
from loguru import logger
from models.types import Venue
from utils.logger import setup_logging_rotating

sys.path.insert(0, str(ROOT / "src"))
try:
    from web.opportunity_store import init_opportunity_store, log_opportunity as _log_opp, mark_executed as _mark_exec
    _OPP_STORE_OK = True
except Exception:
    _OPP_STORE_OK = False

_shutdown = threading.Event()
_ws_alert_ts: dict[str, float] = {}  # venue → last Telegram alert time
_LIVE_OPP_FILE = ROOT / "data" / "live_opportunities.json"
_live_iter = 0


def _write_live_opportunities(opportunities: list, checked_pairs: int) -> None:
    global _live_iter
    _live_iter += 1
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "iteration": _live_iter,
        "checked_pairs": checked_pairs,
        "opportunities": [
            {
                "title": o.yes_market.title,
                "yes_venue": o.yes_venue.value,
                "no_venue": o.no_venue.value,
                "yes_price": round(o.yes_prices.get(o.yes_venue.value, 0.0), 4),
                "no_price": round(o.no_prices.get(o.no_venue.value, 0.0), 4),
                "gross_pct": round(float(o.total_cost or 0.0) and (1 - float(o.total_cost)) * 100, 3),
                "profit_pct": round(float(o.profit_percent or 0.0), 3),
                "max_size": round(float(o.max_executable_size or 0.0), 2),
            }
            for o in (opportunities or [])
        ],
    }
    try:
        _LIVE_OPP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LIVE_OPP_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


class MarketCache:
    """
    TTL-кэш списков рынков по venue.

    Рынки меняются редко (новые открываются ~раз в минуты, не раз в секунды).
    Кэшируем список на MARKET_CACHE_TTL_SECONDS секунд и в каждой итерации
    пропускаем тяжёлый GET /markets — запрашиваем только котировки.

    Если запрос провалился — возвращаем устаревшие данные вместо пустого списка.
    """

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = float(ttl_seconds)
        self._data: dict[Venue, list] = {}
        self._ts: dict[Venue, float] = {}
        self._lock = threading.Lock()

    def get_fresh(self, venue: Venue) -> list | None:
        """Вернуть данные если они свежее TTL, иначе None."""
        with self._lock:
            if time.monotonic() - self._ts.get(venue, 0.0) <= self._ttl:
                return self._data.get(venue)
            return None

    def get_stale(self, venue: Venue) -> list | None:
        """Вернуть данные любого возраста (fallback при ошибке запроса)."""
        with self._lock:
            return self._data.get(venue)

    def set(self, venue: Venue, markets: list) -> None:
        with self._lock:
            self._data[venue] = markets
            self._ts[venue] = time.monotonic()

    def age(self, venue: Venue) -> float:
        with self._lock:
            return time.monotonic() - self._ts.get(venue, 0.0)


class ExecutionWorker:
    """
    Асинхронный воркер исполнения — главный цикл не блокируется на fill-wait.

    Возможности кладутся в очередь через submit().
    Воркер берёт их по одной и исполняет в фоновом потоке.
    """

    def __init__(
        self,
        trade_executor: "TradeExecutor",
        circuit_breaker: "CircuitBreaker",
        max_queue: int = 20,
        on_success=None,
    ) -> None:
        self._executor = trade_executor
        self._cb = circuit_breaker
        self._on_success = on_success
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._thread = threading.Thread(target=self._run, daemon=True, name="ExecWorker")
        self._thread.start()

    def submit(self, opportunity) -> bool:
        try:
            self._queue.put_nowait(opportunity)
            return True
        except queue.Full:
            logger.warning("ExecutionWorker queue full — opportunity dropped")
            return False

    def _run(self) -> None:
        while True:
            opp = self._queue.get()
            if opp is None:
                break
            try:
                ok = self._executor.execute_arbitrage(opp)
                if ok:
                    self._cb.record_success()
                    if self._on_success:
                        self._on_success(opp)
                else:
                    self._cb.record_error()
            except Exception:
                logger.exception("ExecutionWorker: unhandled error")
                self._cb.record_error()
            finally:
                self._queue.task_done()

    def shutdown(self, timeout: float = 60.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout=timeout)


def _handle_signal(signum: int, _frame: object | None) -> None:
    logger.warning("Received signal {}, initiating graceful shutdown…", signum)
    _shutdown.set()


def _build_clients(settings: Settings) -> dict:
    """
    Build all exchange clients that have credentials configured.
    Returns {Venue: client} dict with only enabled exchanges.
    """
    clients = {}

    # Polymarket — always enabled (public data, no auth needed for price discovery)
    poly = PolymarketClobClient(settings)
    clients[Venue.POLYMARKET] = poly
    logger.info("Exchange enabled: Polymarket")

    # Kalshi — enabled only if credentials are configured
    if settings.kalshi_signing_configured:
        kalshi = KalshiClient(settings)
        clients[Venue.KALSHI] = kalshi
        logger.info("Exchange enabled: Kalshi")
    else:
        logger.info("Kalshi skipped (no API key configured)")

    # Betfair — enabled if username + app key are configured
    if settings.betfair_username and settings.betfair_app_key:
        betfair = BetfairClient(settings)
        clients[Venue.BETFAIR] = betfair
        logger.info("Exchange enabled: Betfair")
    else:
        logger.info("Betfair skipped (set BETFAIR_USERNAME + BETFAIR_APP_KEY + BETFAIR_PASSWORD)")

    # Smarkets — enabled if API token is configured
    if settings.smarkets_api_token:
        smarkets = SmarketsClient(settings)
        clients[Venue.SMARKETS] = smarkets
        logger.info("Exchange enabled: Smarkets")
    else:
        logger.info("Smarkets skipped (set SMARKETS_API_TOKEN)")

    # Betdaq — enabled if username + api_key are configured
    if settings.betdaq_username and settings.betdaq_api_key:
        betdaq = BetdaqClient(settings)
        clients[Venue.BETDAQ] = betdaq
        logger.info("Exchange enabled: Betdaq")
    else:
        logger.info("Betdaq skipped (set BETDAQ_USERNAME + BETDAQ_PASSWORD + BETDAQ_API_KEY)")

    # Matchbook — enabled if username + password are configured
    if settings.matchbook_username and settings.matchbook_password:
        matchbook = MatchbookClient(settings)
        clients[Venue.MATCHBOOK] = matchbook
        logger.info("Exchange enabled: Matchbook")
    else:
        logger.info("Matchbook skipped (set MATCHBOOK_USERNAME + MATCHBOOK_PASSWORD)")

    return clients


def _build_polymarket_adapter(settings: Settings):
    """Return live Polymarket execution adapter if credentials are configured."""
    import os
    if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
        return None
    try:
        from clients.polymarket_adapter import PolymarketAdapter
        return PolymarketAdapter()
    except Exception as e:
        logger.warning("Polymarket adapter init failed (live orders disabled): {}", e)
        return None


def run_iteration(
    *,
    settings: Settings,
    clients: dict,
    circuit_breaker: CircuitBreaker,
    market_matcher: MarketMatcher,
    arbitrage_detector: ArbitrageDetector,
    auto_close: "AutoCloseStrategy",
    trade_executor: TradeExecutor,
    exec_worker: "ExecutionWorker",
    position_manager: PositionManager,
    market_cache: "MarketCache",
    quote_delta: "QuoteDeltaFilter",
    quote_store: "QuoteStore",
    ws_feeds: "dict",
    rate_limiter: "VenueRateLimiter",
    tg: "TelegramNotifier",
    trade_log: "TradeLogger",
    pnl_tracker: "PnLTracker",
) -> None:
    if not circuit_breaker.can_trade():
        reason = f"state={circuit_breaker.state.name}"
        logger.warning("Circuit breaker blocks trading ({}); sleeping {:.1f}s", reason, settings.circuit_breaker_wait_seconds)
        tg.circuit_breaker(reason, circuit_breaker.daily_pnl)
        _shutdown.wait(timeout=float(settings.circuit_breaker_wait_seconds))
        return

    # 0. WS health check — alert at most once per 5 min per venue to avoid spam
    _WS_ALERT_COOLDOWN = 300.0
    for feed_venue in ws_feeds:
        if not quote_store.is_live(feed_venue):
            logger.warning("WS feed {} offline — using REST fallback", feed_venue.value)
            last = _ws_alert_ts.get(feed_venue.value, 0.0)
            if time.monotonic() - last >= _WS_ALERT_COOLDOWN:
                tg.info(f"⚠️ WebSocket {feed_venue.value} не отвечает, переключились на REST")
                _ws_alert_ts[feed_venue.value] = time.monotonic()
        else:
            _ws_alert_ts.pop(feed_venue.value, None)  # сбрасываем при восстановлении

    # 1. Fetch markets — served from cache if fresh, otherwise parallel HTTP fetch (rate-limited)
    def _fetch_one(venue_client_pair):
        venue, client = venue_client_pair
        cached = market_cache.get_fresh(venue)
        if cached is not None:
            logger.debug(
                "Cache hit for {} ({} markets, age={:.0f}s)",
                venue.value, len(cached), market_cache.age(venue),
            )
            return venue, cached
        try:
            rate_limiter.acquire(venue)  # enforce per-venue request rate
            if venue == Venue.POLYMARKET:
                mkts = client.get_markets(max_pages=settings.polymarket_markets_max_pages)
            elif venue == Venue.KALSHI:
                mkts = client.get_markets(limit=settings.kalshi_markets_limit, status="open")
            elif venue == Venue.BETFAIR:
                mkts = client.get_markets(max_results=settings.betfair_markets_limit)
            else:
                mkts = client.get_markets()
            market_cache.set(venue, mkts)
            logger.info("Fetched {} {} markets (cache refreshed)", len(mkts), venue.value)
            return venue, mkts
        except Exception as e:
            logger.warning("Failed to fetch markets from {}: {}", venue.value, e)
            # Fall back to stale cache data rather than empty list
            stale = market_cache.get_stale(venue)
            if stale:
                logger.info("Using stale cache for {} ({} markets)", venue.value, len(stale))
            return venue, stale or []

    markets_by_venue: dict = {}
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        for venue, mkts in pool.map(_fetch_one, clients.items()):
            markets_by_venue[venue] = mkts

    if sum(len(v) for v in markets_by_venue.values()) == 0:
        logger.warning("No markets fetched from any exchange — skipping iteration")
        return

    # 2. N×N market matching
    raw_matches = market_matcher.find_all_matches_multi(markets_by_venue)
    logger.debug("Market matcher produced {} pair(s)", len(raw_matches))

    if not raw_matches:
        logger.debug("No matched pairs this iteration")
        return

    # 3. Subscribe new markets to WebSocket feeds (no-op for already-subscribed)
    all_matched = [m for pair in raw_matches for m in pair]
    for feed_venue, feed in ws_feeds.items():
        venue_mkts = [m for m in all_matched if m.venue == feed_venue]
        if venue_mkts:
            feed.subscribe(venue_mkts)

    # 3b. Enrich with live quotes — WS store first, REST fallback (rate-limited)
    enriched_pairs, all_a, all_b = enrich_matched_pairs(
        raw_matches,
        clients=clients,
        quote_store=quote_store,
        rate_limiter=rate_limiter,
    )

    if not enriched_pairs:
        logger.debug("No pairs survived quote enrichment")
        return

    # 4. Delta filter — skip pairs where quotes haven't moved since last iteration
    active_pairs = [(a, b) for a, b in enriched_pairs if quote_delta.changed(a, b)]
    skipped = len(enriched_pairs) - len(active_pairs)
    if skipped:
        logger.debug("Delta filter: skipped {}/{} unchanged pairs", skipped, len(enriched_pairs))
    active_a = [a for a, _ in active_pairs]
    active_b = [b for _, b in active_pairs]

    # 5. Detect best arbitrage opportunity across changed pairs only
    opportunities = arbitrage_detector.find_opportunities(active_a, active_b, active_pairs)

    # Update delta snapshot with all enriched quotes (not just active)
    quote_delta.update(all_a + all_b)

    # 5b. Auto-close: evaluate open positions against live quotes
    mid_by_market: dict[str, tuple[float, float]] = {}  # market_id → (yes_ask, no_ask)
    for a, b in enriched_pairs:
        for mkt in (a, b):
            ya = mkt.extra.get("yes_best_ask")
            na = mkt.extra.get("no_best_ask")
            if ya is not None and na is not None:
                mid_by_market[mkt.market_id] = (float(ya), float(na))

    open_positions = position_manager.list_open_positions()
    pos_marks: dict[str, float] = {}
    for p in open_positions:
        prices = mid_by_market.get(p.market_id)
        if prices is not None:
            pos_marks[p.position_id] = prices[0] if p.side == "YES" else prices[1]
    if pos_marks:
        position_manager.update_marks(pos_marks)

    ac_result = auto_close.evaluate(position_manager, mid_by_position_id={
        p.position_id: pos_marks.get(p.position_id, p.mark_price or p.entry_price)
        for p in open_positions
    })
    for pid, reason in ac_result.reasons.items():
        logger.warning("Auto-closed position {} (reason={})", pid, reason)
        tg.info(f"🔒 Авто-закрытие позиции {pid[:20]} | причина: {reason}")

    if opportunities:
        n = min(len(opportunities), settings.max_concurrent_opportunities)
        top = opportunities[:n]
        logger.info(
            "Found {} opportunit{}; queuing top {} for async execution",
            len(opportunities),
            "y" if len(opportunities) == 1 else "ies",
            n,
        )
        for opp in top:
            pct = opp.profit_percent or 0.0
            logger.info(
                "  YES@{} + NO@{} | {:.2f}% | {}",
                opp.yes_venue.value, opp.no_venue.value,
                pct, opp.yes_market.title[:55],
            )
            tg.opportunity(
                title=opp.yes_market.title,
                yes_venue=opp.yes_venue.value,
                no_venue=opp.no_venue.value,
                profit_pct=pct,
            )
            trade_log.log_opportunity(
                title=opp.yes_market.title,
                yes_venue=opp.yes_venue.value,
                no_venue=opp.no_venue.value,
                yes_price=opp.yes_prices.get(opp.yes_venue.value, 0.0),
                no_price=opp.no_prices.get(opp.no_venue.value, 0.0),
                profit_pct=pct,
            )
            exec_worker.submit(opp)
            if _OPP_STORE_OK:
                try:
                    _log_opp(
                        title=opp.yes_market.title,
                        yes_venue=opp.yes_venue.value,
                        no_venue=opp.no_venue.value,
                        yes_price=opp.yes_prices.get(opp.yes_venue.value, 0.0),
                        no_price=opp.no_prices.get(opp.no_venue.value, 0.0),
                        profit_pct=pct,
                        max_size=float(opp.max_executable_size or 0.0),
                        executed=False,
                    )
                except Exception:
                    pass
    else:
        logger.debug("No arbitrage opportunities this iteration")

    position_manager.update_pnl()
    circuit_breaker.total_positions = len(position_manager)
    _write_live_opportunities(opportunities if opportunities else [], len(enriched_pairs))


def main() -> None:
    settings = get_settings()
    snapshot_path = ROOT / settings.positions_snapshot_path
    log_dir = ROOT / settings.log_directory
    if _OPP_STORE_OK:
        init_opportunity_store(ROOT / "data")

    setup_logging_rotating(
        settings.log_level,
        log_directory=log_dir,
        retention_days=settings.log_retention_days,
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    clients = _build_clients(settings)

    if len(clients) < 2:
        logger.error(
            "Need at least 2 exchanges for arbitrage. "
            "Currently enabled: {}. "
            "Configure BETFAIR_USERNAME+BETFAIR_APP_KEY or SMARKETS_API_TOKEN.",
            list(clients.keys()),
        )
        return

    circuit_breaker = CircuitBreaker(
        max_daily_loss=float(settings.max_daily_loss_usd),
        max_daily_loss_pct=float(settings.max_daily_loss_pct),
        bankroll=float(settings.kelly_bankroll),
    )
    market_matcher = MarketMatcher(
        min_fuzzy_score=float(settings.market_match_min_fuzzy_score),
        semantic_enabled=settings.semantic_matching_enabled,
        max_expiry_diff_days=int(settings.max_expiry_diff_days),
    )
    arbitrage_detector = ArbitrageDetector(settings)
    auto_close = AutoCloseStrategy(settings)
    position_manager = PositionManager()

    # Build execution client registry (includes live adapters where available)
    exec_clients = dict(clients)
    poly_adapter = _build_polymarket_adapter(settings)
    if poly_adapter is not None:
        # Wrap adapter so it implements ExchangeClient for Polymarket execution
        clients[Venue.POLYMARKET]._exec_adapter = poly_adapter

    trade_log = TradeLogger(ROOT / "data" / "trades.csv")
    pnl_tracker = PnLTracker(ROOT / "data" / "pnl_history.json")

    def _on_trade_success(opp) -> None:
        if _OPP_STORE_OK:
            try:
                _mark_exec(opp.yes_market.title, opp.yes_venue.value, opp.no_venue.value)
            except Exception:
                pass
        pct = opp.profit_percent or 0.0
        yes_px = opp.yes_prices.get(opp.yes_venue.value, 0.0)
        no_px = opp.no_prices.get(opp.no_venue.value, 0.0)
        cost = float(opp.total_cost or ((yes_px + no_px) * float(opp.max_executable_size or 1)))
        pnl_delta = cost * pct / 100.0
        pnl_tracker.record(pnl_delta)
        circuit_breaker.record_pnl(pnl_delta)
        trade_log.log_execution(
            title=opp.yes_market.title,
            yes_venue=opp.yes_venue.value,
            no_venue=opp.no_venue.value,
            yes_price=yes_px,
            no_price=no_px,
            profit_pct=pct,
            cost_usd=cost,
        )

    trade_executor = TradeExecutor(settings, exec_clients, position_manager=position_manager)
    exec_worker = ExecutionWorker(trade_executor, circuit_breaker, on_success=_on_trade_success)
    market_cache = MarketCache(ttl_seconds=float(settings.market_cache_ttl_seconds))
    quote_delta = QuoteDeltaFilter()
    quote_store = QuoteStore()
    rate_limiter = VenueRateLimiter()
    tg = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )

    # Crash recovery: restore positions from last snapshot and alert via Telegram
    recovered = position_manager.load_snapshot(snapshot_path)
    if recovered:
        tg.startup_positions(recovered)

    # Start WebSocket feeds for supported venues
    ws_feeds: dict = {}
    if Venue.POLYMARKET in clients:
        poly_ws = PolymarketWebSocketFeed(quote_store)
        poly_ws.start()
        ws_feeds[Venue.POLYMARKET] = poly_ws
    if Venue.KALSHI in clients:
        kalshi_ws = KalshiWebSocketFeed(
            quote_store,
            api_key_id=settings.kalshi_api_key_id,
            private_key_pem=settings.kalshi_private_key_pem,
        )
        kalshi_ws.start()
        ws_feeds[Venue.KALSHI] = kalshi_ws

    if ws_feeds:
        logger.info("WebSocket feeds started: {}", [v.value for v in ws_feeds])
        logger.info("Waiting 2s for initial WS connections…")
        _shutdown.wait(timeout=2.0)

    # Telegram command handlers
    def _cmd_status() -> str:
        running_exchanges = [v.value for v in clients]
        cb_state = circuit_breaker.state.name
        return (
            f"*Статус бота*\n"
            f"Биржи: {', '.join(running_exchanges)}\n"
            f"Circuit breaker: {cb_state}\n"
            f"Открытых позиций: {len(position_manager)}\n"
            f"Дневной P&L: ${pnl_tracker.today():.2f}\n"
            f"DRY_RUN: {settings.dry_run}"
        )

    def _cmd_positions() -> str:
        positions = position_manager.list_open_positions()
        if not positions:
            return "Открытых позиций нет."
        lines = [f"*Открытые позиции* ({len(positions)}):"]
        for p in positions[:10]:
            lines.append(f"• {p.side} {p.contracts:.1f} @ {p.venue.value} | {p.market_id[:25]}")
        return "\n".join(lines)

    def _cmd_pnl() -> str:
        hist = pnl_tracker.history(7)
        lines = ["*P&L за 7 дней:*"]
        for row in hist:
            sign = "+" if row["pnl"] >= 0 else ""
            lines.append(f"  {row['date']}: {sign}${row['pnl']:.2f}")
        lines.append(f"\nСегодня: ${pnl_tracker.today():.2f}")
        return "\n".join(lines)

    def _cmd_stop() -> str:
        _shutdown.set()
        return "Бот остановлен."

    # When launched from the web dashboard, polling is owned by the dashboard process
    import os as _os
    if not _os.environ.get("TG_POLLING_DISABLED"):
        tg.start_command_polling({
            "/status":    _cmd_status,
            "/positions": _cmd_positions,
            "/pnl":       _cmd_pnl,
            "/stop":      _cmd_stop,
        })

    startup_msg = (
        f"Бот запущен | биржи: {[v.value for v in clients]} | "
        f"DRY_RUN={settings.dry_run} | poll={settings.poll_interval_seconds}s"
    )
    logger.info(startup_msg)
    tg.info(startup_msg)

    try:
        while not _shutdown.is_set():
            try:
                run_iteration(
                    settings=settings,
                    clients=clients,
                    circuit_breaker=circuit_breaker,
                    market_matcher=market_matcher,
                    arbitrage_detector=arbitrage_detector,
                    auto_close=auto_close,
                    trade_executor=trade_executor,
                    exec_worker=exec_worker,
                    position_manager=position_manager,
                    market_cache=market_cache,
                    quote_delta=quote_delta,
                    quote_store=quote_store,
                    ws_feeds=ws_feeds,
                    rate_limiter=rate_limiter,
                    tg=tg,
                    trade_log=trade_log,
                    pnl_tracker=pnl_tracker,
                )
            except Exception:
                logger.exception("Iteration failed")
                circuit_breaker.record_error()

            _shutdown.wait(timeout=float(settings.poll_interval_seconds))
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt — shutting down")
        _shutdown.set()
    finally:
        # Drain execution queue first so any in-flight trades can still send Telegram alerts
        exec_worker.shutdown(timeout=60.0)

        for feed in ws_feeds.values():
            try:
                feed.stop()
            except Exception:
                pass

        open_pos = position_manager.list_open_positions()
        if open_pos:
            tg.startup_positions([
                {"side": p.side, "contracts": p.contracts, "venue": p.venue.value, "market_id": p.market_id}
                for p in open_pos
            ])
        tg.info(f"Бот остановлен. Открытых позиций: {len(open_pos)}")
        tg.shutdown()

        for client in clients.values():
            try:
                client.close()
            except Exception:
                pass
        try:
            position_manager.save_snapshot(snapshot_path)
        except Exception:
            logger.exception("Failed to save positions snapshot")
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
