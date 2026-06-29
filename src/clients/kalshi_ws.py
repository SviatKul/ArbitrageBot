"""
Kalshi WebSocket feed.

Subscribes to the 'ticker' channel for binary markets and pushes
real-time YES/NO prices into QuoteStore.

Protocol (wss://api.elections.kalshi.com/trade-api/ws/v2):
  Auth: first message must carry KALSHI_API_KEY_ID + signature
  SEND → {"id":1,"cmd":"subscribe","params":{"channels":["ticker"],
           "market_tickers":["KXELON-24DEC31",...]}}
  RECV ← {"type":"subscribed","msg":{"channel":"ticker",...}}
  RECV ← {"type":"ticker","msg":{"market_ticker":"KXELON-24DEC31",
           "yes_ask":0.46,"no_ask":0.54,"yes_bid":0.44,"no_bid":0.52,
           "volume":1234,"open_interest":5678}}
  RECV ← {"type":"heartbeat"}  # keep-alive, ignored

Reconnect: exponential backoff 1s → 30s.
Authentication requires the same RSA key used for REST requests.
If no credentials are configured, the feed runs without auth (read-only public).
"""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Optional

from loguru import logger

from core.quote_store import QuoteStore
from models.types import Market, Venue

_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
_RECONNECT_DELAYS = [1, 2, 4, 8, 16, 30]
_CMD_ID = 0


def _next_id() -> int:
    global _CMD_ID
    _CMD_ID += 1
    return _CMD_ID


class KalshiWebSocketFeed:
    """
    Background thread that subscribes to Kalshi ticker channel
    and pushes real-time quotes into QuoteStore.
    """

    def __init__(
        self,
        store: QuoteStore,
        *,
        api_key_id: Optional[str] = None,
        private_key_pem: Optional[str] = None,
    ) -> None:
        self._store = store
        self._api_key_id = api_key_id or ""
        self._private_key_pem = private_key_pem or ""
        self._lock = threading.Lock()
        self._tickers: set[str] = set()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._ws = None

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #

    def subscribe(self, markets: list[Market]) -> None:
        """Register Kalshi markets for real-time ticker updates."""
        with self._lock:
            for m in markets:
                if m.venue == Venue.KALSHI:
                    self._tickers.add(m.market_id)
        if self._ws is not None:
            self._send_subscribe()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="KalshiWS",
            daemon=True,
        )
        self._thread.start()
        logger.info("Kalshi WebSocket feed started")

    def stop(self) -> None:
        self._running = False
        self._store.mark_offline(Venue.KALSHI)
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        logger.info("Kalshi WebSocket feed stopped")

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    def _auth_headers(self) -> dict[str, str]:
        """Build RSA-signed Authorization header for the WebSocket upgrade."""
        if not self._api_key_id or not self._private_key_pem:
            return {}
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            timestamp_ms = str(int(time.time() * 1000))
            message = timestamp_ms + "GET" + "/trade-api/ws/v2"
            pem = self._private_key_pem.encode()
            private_key = serialization.load_pem_private_key(pem, password=None)
            signature = private_key.sign(
                message.encode("utf-8"),
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
            sig_b64 = base64.b64encode(signature).decode()
            return {
                "KALSHI-ACCESS-KEY": self._api_key_id,
                "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
                "KALSHI-ACCESS-SIGNATURE": sig_b64,
            }
        except Exception as e:
            logger.debug("Kalshi WS auth header failed: {}", e)
            return {}

    # ------------------------------------------------------------------ #
    # Messaging
    # ------------------------------------------------------------------ #

    def _send_subscribe(self) -> None:
        ws = self._ws
        if ws is None:
            return
        with self._lock:
            tickers = list(self._tickers)
        if not tickers:
            return
        msg = json.dumps({
            "id": _next_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["ticker"],
                "market_tickers": tickers,
            },
        })
        try:
            ws.send(msg)
            logger.debug("Kalshi WS: subscribed to {} tickers", len(tickers))
        except Exception as e:
            logger.debug("Kalshi WS send error: {}", e)

    def _on_message(self, _ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        mtype = msg.get("type") or ""
        if mtype == "ticker":
            self._handle_ticker(msg.get("msg") or {})
        # "subscribed", "heartbeat", "error" — ignore

    def _handle_ticker(self, data: dict) -> None:
        ticker = str(data.get("market_ticker") or "")
        if not ticker:
            return
        try:
            yes_ask  = float(data["yes_ask"])
            no_ask   = float(data["no_ask"])
            yes_size = float(data.get("yes_ask_size") or data.get("volume") or 0)
            no_size  = float(data.get("no_ask_size")  or data.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            return

        self._store.set(Venue.KALSHI, ticker, {
            "yes_best_ask":      yes_ask,
            "yes_best_ask_size": yes_size,
            "no_best_ask":       no_ask,
            "no_best_ask_size":  no_size,
        })

    def _on_open(self, _ws) -> None:
        self._store.mark_live(Venue.KALSHI)
        logger.info("Kalshi WebSocket connected")
        self._send_subscribe()

    def _on_close(self, _ws, code, reason) -> None:
        self._store.mark_offline(Venue.KALSHI)
        logger.warning("Kalshi WebSocket closed (code={} reason={})", code, reason)

    def _on_error(self, _ws, error) -> None:
        logger.warning("Kalshi WebSocket error: {}", error)

    def _run_loop(self) -> None:
        import websocket  # noqa: PLC0415

        attempt = 0
        while self._running:
            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            if attempt > 0:
                logger.info("Kalshi WS reconnect in {}s (attempt {})", delay, attempt)
                time.sleep(delay)

            try:
                headers = self._auth_headers()
                ws = websocket.WebSocketApp(
                    _WS_URL,
                    header=[f"{k}: {v}" for k, v in headers.items()],
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws = ws
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.warning("Kalshi WS run_forever error: {}", e)
            finally:
                self._ws = None
                self._store.mark_offline(Venue.KALSHI)

            attempt += 1
