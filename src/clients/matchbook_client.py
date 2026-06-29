"""
Matchbook Exchange client — P2P betting exchange.

Auth flow:
  POST https://www.matchbook.com/bpapi/rest/security/session
  Body: {"username": "...", "password": "..."}
  → session-token in response header and body.
  Token placed in Cookie or X-Session-Token header.
  Sessions expire; renewed automatically on 401.

Required env vars:
  MATCHBOOK_USERNAME
  MATCHBOOK_PASSWORD

Commission: 1.5% on net winnings (lowest of all major exchanges).

API reference: https://matchbook.com/api
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from config.settings import Settings
from models.types import Market, Venue


_API_BASE = "https://www.matchbook.com/bpapi/rest"
_AUTH_URL = f"{_API_BASE}/security/session"

# Matchbook sport IDs for binary prediction markets
# 14 = Politics, 15 = Specials / Current Affairs
_PREDICTION_SPORT_IDS = [14, 15]

_YES_LABELS = {"yes", "y", "for", "to happen", "will happen", "true", "backs"}
_NO_LABELS  = {"no",  "n", "against", "will not happen", "wont happen", "false", "lays"}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class MatchbookClient:
    """
    Read + execute client for Matchbook Exchange.

    Binary market detection: markets with exactly 2 runners whose names
    normalise to YES / NO variants. Commission 1.5% on winnings.
    """

    def __init__(self, settings: Settings, *, client: Optional[httpx.Client] = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=settings.http_timeout_seconds)
        self._username = settings.matchbook_username or ""
        self._password = settings.matchbook_password or ""
        self._session_token: Optional[str] = None
        self._session_ts: float = 0.0
        logger.info("Matchbook client initialized (user={})", self._username)

    # ------------------------------------------------------------------ #
    # Protocol
    # ------------------------------------------------------------------ #

    @property
    def venue(self) -> Venue:
        return Venue.MATCHBOOK

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "MatchbookClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    def _login(self) -> str:
        resp = self._client.post(
            _AUTH_URL,
            json={"username": self._username, "password": self._password},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        resp.raise_for_status()
        body = resp.json()
        token = (
            body.get("session-token")
            or body.get("sessionToken")
            or body.get("session_token")
            or resp.headers.get("X-Session-Token")
            or resp.headers.get("session-token")
        )
        if not token:
            raise RuntimeError(f"Matchbook login: no session-token in response: {body}")
        logger.info("Matchbook session established")
        return str(token)

    def _ensure_session(self) -> str:
        if not self._session_token or (time.time() - self._session_ts) > 1800:
            self._session_token = self._login()
            self._session_ts = time.time()
        return self._session_token

    def _headers(self) -> dict[str, str]:
        return {
            "session-token": self._ensure_session(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------ #
    # Low-level requests
    # ------------------------------------------------------------------ #

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{_API_BASE}{path}"

        @retry(
            reraise=True,
            stop=stop_after_attempt(self._settings.retry_max_attempts),
            wait=wait_exponential(min=self._settings.retry_wait_min_seconds,
                                  max=self._settings.retry_wait_max_seconds),
            retry=retry_if_exception(_is_retryable),
        )
        def _do() -> Any:
            resp = self._client.get(url, params=params or {}, headers=self._headers())
            if resp.status_code == 401:
                self._session_token = None
            resp.raise_for_status()
            return resp.json()

        return _do()

    def _post(self, path: str, body: Any) -> Any:
        url = f"{_API_BASE}{path}"

        @retry(
            reraise=True,
            stop=stop_after_attempt(self._settings.retry_max_attempts),
            wait=wait_exponential(min=self._settings.retry_wait_min_seconds,
                                  max=self._settings.retry_wait_max_seconds),
            retry=retry_if_exception(_is_retryable),
        )
        def _do() -> Any:
            resp = self._client.post(url, json=body, headers=self._headers())
            if resp.status_code == 401:
                self._session_token = None
            resp.raise_for_status()
            return resp.json()

        return _do()

    # ------------------------------------------------------------------ #
    # Market discovery
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_binary(runners: list[dict]) -> bool:
        if len(runners) != 2:
            return False
        labels = {r.get("name", "").strip().lower() for r in runners}
        return bool(labels & _YES_LABELS) and bool(labels & _NO_LABELS)

    def get_markets(self, *, max_results: int = 200, **_) -> list[Market]:
        """Fetch binary YES/NO markets from Matchbook politics/specials events."""
        if not self._username:
            logger.warning("Matchbook credentials not configured — skipping")
            return []

        markets: list[Market] = []
        for sport_id in _PREDICTION_SPORT_IDS:
            try:
                data = self._get("/events", params={
                    "sport-ids": sport_id,
                    "status": "open",
                    "per-page": min(max_results, 500),
                    "offset": 0,
                    "include-event-participants": "true",
                })
                events = data.get("events") or []
                for event in events:
                    event_name = str(event.get("name") or "")
                    for mkt in (event.get("markets") or []):
                        runners = mkt.get("runners") or []
                        if not self._is_binary(runners):
                            continue
                        title = str(mkt.get("name") or event_name)
                        markets.append(Market(
                            venue=Venue.MATCHBOOK,
                            market_id=str(mkt.get("id") or ""),
                            title=title,
                            extra={
                                **mkt,
                                "event_id": str(event.get("id") or ""),
                                "event_name": event_name,
                                "_runners": runners,
                            },
                        ))
            except Exception as e:
                logger.debug("Matchbook get_markets(sport={}): {}", sport_id, e)

        logger.info("Matchbook: fetched {} binary markets", len(markets))
        return markets

    # ------------------------------------------------------------------ #
    # Quotes
    # ------------------------------------------------------------------ #

    def best_quotes(self, market: Market) -> Optional[dict[str, float]]:
        """
        Return yes/no best-ask in [0,1] probability space.

        Matchbook uses decimal odds → implied prob = 1/odds.
        The API returns offers (equivalent to order book levels).
        """
        if not self._username:
            return None
        try:
            event_id = market.extra.get("event_id") or ""
            data = self._get(
                f"/events/{event_id}/markets/{market.market_id}/runners",
                params={"include-prices": "true", "price-depth": 1},
            )
            runners = data.get("runners") or []

            yes_ask: Optional[float] = None
            yes_size: float = 0.0
            no_ask: Optional[float] = None
            no_size: float = 0.0

            for runner in runners:
                name = runner.get("name", "").strip().lower()
                prices = runner.get("prices") or []
                if not prices:
                    continue
                # Best available back price (side = "back")
                back_prices = [p for p in prices if p.get("side") == "back"]
                if not back_prices:
                    continue
                best = back_prices[0]
                decimal_odds = float(best.get("decimal-odds") or best.get("odds") or 0)
                size = float(best.get("available-amount") or best.get("size") or 0)
                if decimal_odds <= 1.0:
                    continue
                implied = 1.0 / decimal_odds

                if name in _YES_LABELS:
                    yes_ask, yes_size = implied, size
                elif name in _NO_LABELS:
                    no_ask,  no_size  = implied, size

            if yes_ask is None or no_ask is None:
                return None

            return {
                "yes_best_ask":      yes_ask,
                "yes_best_ask_size": yes_size,
                "no_best_ask":       no_ask,
                "no_best_ask_size":  no_size,
            }
        except Exception as e:
            logger.debug("Matchbook best_quotes failed for {}: {}", market.market_id, e)
            return None

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def place_order(self, market: Market, side: str, quantity: float, price: float) -> str:
        """
        Place a BACK offer on Matchbook.

        side:     "yes" or "no"
        price:    implied probability [0,1] → decimal odds = 1/price
        quantity: stake in account currency
        """
        if self._settings.dry_run:
            oid = f"dry-matchbook-{uuid.uuid4().hex[:12]}"
            logger.debug("Dry-run Matchbook order {} side={} qty={} price={:.4f}", oid, side, quantity, price)
            return oid

        runners = market.extra.get("_runners") or []
        target_labels = _YES_LABELS if side.lower() == "yes" else _NO_LABELS
        runner_id: Optional[str] = None
        for r in runners:
            if r.get("name", "").strip().lower() in target_labels:
                runner_id = str(r.get("id") or "")
                break

        if not runner_id:
            raise ValueError(f"Cannot find {side} runner in Matchbook market {market.market_id}")

        decimal_odds = round(1.0 / price, 3) if price > 0 else 2.0
        decimal_odds = max(1.001, min(1000.0, decimal_odds))
        event_id = market.extra.get("event_id") or ""

        body = {
            "offers": [{
                "event-id": event_id,
                "market-id": market.market_id,
                "runner-id": runner_id,
                "side": "back",
                "decimal-odds": decimal_odds,
                "stake": round(quantity, 2),
                "keep-in-play": False,
            }]
        }
        resp = self._post("/offers", body)
        offers = resp.get("offers") or [{}]
        offer_id = offers[0].get("id") or resp.get("id")
        if not offer_id:
            raise RuntimeError(f"Matchbook place_order: no offer id in response: {resp}")
        logger.info("Matchbook offer placed: id={} side={} odds={} stake={}", offer_id, side, decimal_odds, quantity)
        return str(offer_id)

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        try:
            data = self._get(f"/offers/{order_id}")
            return data.get("offer") or data or {}
        except Exception as e:
            logger.debug("Matchbook get_order_status {}: {}", order_id, e)
            return {}

    def cancel_order(self, order_id: str) -> None:
        try:
            resp = self._client.delete(
                f"{_API_BASE}/offers/{order_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            logger.debug("Matchbook cancel sent for {}", order_id)
        except Exception as e:
            logger.warning("Matchbook cancel failed for {}: {}", order_id, e)
