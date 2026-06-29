"""
Polymarket CLOB WebSocket feed.

Subscribes to real-time orderbook updates for Polymarket binary markets.
Runs in a background daemon thread; pushes YES/NO best-ask into QuoteStore.

Protocol (wss://clob.polymarket.com/ws/market):
  SEND → {"type":"subscribe","channel":"market","markets":["<condition_id>",...]}
  RECV ← {"event_type":"book","market":"<cid>","asset_id":"<token_id>",
           "data":{"buys":[{"price":"0.44","size":"100"}],
                   "sells":[{"price":"0.46","size":"80"}]}}
  RECV ← {"event_type":"price_change","market":"<cid>","asset_id":"<token_id>",
           "changes":[{"side":"sell","price":"0.46","size":"80"}]}
  RECV ← {"event_type":"last_trade_price","market":"<cid>",...}  # ignored

Each binary market has two tokens: YES and NO.
Market.extra["clobTokenIds"] = [yes_token_id, no_token_id]
We build a token_id → (condition_id, side) mapping at subscription time.

Reconnect: exponential backoff 1s → 30s on any disconnect or error.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Optional

from loguru import logger

from core.quote_store import QuoteStore
from models.types import Market, Venue

_WS_URL = "wss://clob.polymarket.com/ws/market"
_RECONNECT_DELAYS = [1, 2, 4, 8, 16, 30]  # seconds


class PolymarketWebSocketFeed:
    """
    Background thread that maintains a WebSocket connection to Polymarket CLOB
    and pushes real-time best-ask quotes into a QuoteStore.
    """

    def __init__(self, store: QuoteStore) -> None:
        self._store = store
        self._lock = threading.Lock()
        # condition_id → Market (for building the token map)
        self._markets: dict[str, Market] = {}
        # token_id → (condition_id, "yes"/"no")
        self._token_map: dict[str, tuple[str, str]] = {}
        # In-flight best prices per token: token_id → (best_ask, best_ask_size)
        self._asks: dict[str, tuple[float, float]] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._ws = None

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #

    def subscribe(self, markets: list[Market]) -> None:
        """Register markets and subscribe to their price channels."""
        with self._lock:
            for m in markets:
                if m.venue != Venue.POLYMARKET:
                    continue
                self._markets[m.market_id] = m
                token_ids = self._extract_token_ids(m)
                if len(token_ids) >= 2:
                    self._token_map[token_ids[0]] = (m.market_id, "yes")
                    self._token_map[token_ids[1]] = (m.market_id, "no")
                elif len(token_ids) == 1:
                    # If only one token ID found, assume it's YES
                    self._token_map[token_ids[0]] = (m.market_id, "yes")

        if self._ws is not None:
            self._send_subscribe()

    def start(self) -> None:
        """Start the background WebSocket thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="PolyWS",
            daemon=True,
        )
        self._thread.start()
        logger.info("Polymarket WebSocket feed started")

    def stop(self) -> None:
        """Signal shutdown and close the WebSocket."""
        self._running = False
        self._store.mark_offline(Venue.POLYMARKET)
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        logger.info("Polymarket WebSocket feed stopped")

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_token_ids(market: Market) -> list[str]:
        extra = dict(market.extra)
        ids = (
            extra.get("clobTokenIds")
            or extra.get("clob_token_ids")
            or extra.get("tokenIds")
        )
        if isinstance(ids, list):
            return [str(x) for x in ids if x]
        return []

    def _send_subscribe(self) -> None:
        ws = self._ws
        if ws is None:
            return
        with self._lock:
            condition_ids = list(self._markets.keys())
        if not condition_ids:
            return
        msg = json.dumps({
            "type": "subscribe",
            "channel": "market",
            "markets": condition_ids,
        })
        try:
            ws.send(msg)
            logger.debug("Polymarket WS: subscribed to {} markets", len(condition_ids))
        except Exception as e:
            logger.debug("Polymarket WS send error: {}", e)

    def _on_message(self, _ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        event = msg.get("event_type") or msg.get("type") or ""

        if event == "book":
            self._handle_book(msg)
        elif event == "price_change":
            self._handle_price_change(msg)
        # last_trade_price and others are ignored

    def _handle_book(self, msg: dict) -> None:
        """Full orderbook snapshot for a token."""
        token_id = str(msg.get("asset_id") or msg.get("token_id") or "")
        data = msg.get("data") or {}
        sells = data.get("sells") or []
        if not sells:
            return
        try:
            best_ask = float(sells[0]["price"])
            best_size = float(sells[0].get("size", 0))
        except (KeyError, TypeError, ValueError):
            return
        self._update_token_ask(token_id, best_ask, best_size)

    def _handle_price_change(self, msg: dict) -> None:
        """Incremental price update for a token."""
        token_id = str(msg.get("asset_id") or msg.get("token_id") or "")
        changes = msg.get("changes") or []
        for change in changes:
            if change.get("side") != "sell":
                continue
            try:
                price = float(change["price"])
                size = float(change.get("size", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if size == 0:
                # Level removed — get current best from remaining; skip for now
                continue
            self._update_token_ask(token_id, price, size)

    def _update_token_ask(self, token_id: str, price: float, size: float) -> None:
        with self._lock:
            info = self._token_map.get(token_id)
            if info is None:
                return
            condition_id, side = info
            self._asks[f"{condition_id}:{side}"] = (price, size)
            # Build combined quote if we have both sides
            yes_key = f"{condition_id}:yes"
            no_key  = f"{condition_id}:no"
            yes_data = self._asks.get(yes_key)
            no_data  = self._asks.get(no_key)

        if yes_data and no_data:
            self._store.set(Venue.POLYMARKET, condition_id, {
                "yes_best_ask":      yes_data[0],
                "yes_best_ask_size": yes_data[1],
                "no_best_ask":       no_data[0],
                "no_best_ask_size":  no_data[1],
            })

    def _on_open(self, _ws) -> None:
        self._store.mark_live(Venue.POLYMARKET)
        logger.info("Polymarket WebSocket connected")
        self._send_subscribe()

    def _on_close(self, _ws, code, reason) -> None:
        self._store.mark_offline(Venue.POLYMARKET)
        logger.warning("Polymarket WebSocket closed (code={} reason={})", code, reason)

    def _on_error(self, _ws, error) -> None:
        logger.warning("Polymarket WebSocket error: {}", error)

    def _run_loop(self) -> None:
        import websocket  # import here so module loads even if not installed

        attempt = 0
        while self._running:
            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            if attempt > 0:
                logger.info("Polymarket WS reconnect in {}s (attempt {})", delay, attempt)
                time.sleep(delay)

            try:
                ws = websocket.WebSocketApp(
                    _WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws = ws
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logger.warning("Polymarket WS run_forever error: {}", e)
            finally:
                self._ws = None
                self._store.mark_offline(Venue.POLYMARKET)

            attempt += 1
