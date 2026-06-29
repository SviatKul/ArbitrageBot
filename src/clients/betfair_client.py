"""
Betfair Exchange client — binary prediction markets (politics, global events).

Auth flow:
  1. POST https://identitysso.betfair.com/api/login  (username + password)
  2. Use returned sessionToken in X-Authentication header for all API calls.
  3. Session expires after ~20 min of inactivity; auto-renewed on 401.

Required env vars:
  BETFAIR_USERNAME
  BETFAIR_PASSWORD
  BETFAIR_APP_KEY      — from Betfair API developer dashboard

Market filter: only binary (2-runner) markets with YES/NO runners from event types
that overlap with Polymarket (politics, global events, finance).
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from config.settings import Settings
from models.types import Market, Venue


_LOGIN_URL = "https://identitysso.betfair.com/api/login"
_API_BASE = "https://api.betfair.com/exchange/betting/rest/v1.0"

# Betfair event type IDs that correspond to binary political/macro markets
_BINARY_EVENT_TYPES = [
    "6423",    # Politics
    "468328",  # Global Politics
    "2378961", # US Politics
    "6231",    # Financial Indices
    "27979456",# World Economy
]

_YES_LABELS = {"yes", "y", "for", "true", "backs"}
_NO_LABELS = {"no", "n", "against", "false", "lays"}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class BetfairClient:
    """
    Read + execute client for Betfair Exchange.

    Binary market detection: markets with exactly 2 runners whose names
    normalise to YES / NO variants are treated as binary prediction markets.
    """

    def __init__(self, settings: Settings, *, client: Optional[httpx.Client] = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=settings.http_timeout_seconds)
        self._session_token: Optional[str] = None
        self._session_ts: float = 0.0
        self._app_key = settings.betfair_app_key or ""
        self._username = settings.betfair_username or ""
        self._password = settings.betfair_password or ""
        logger.info("Betfair client initialized (user={})", self._username)

    # ------------------------------------------------------------------ #
    # Protocol properties
    # ------------------------------------------------------------------ #

    @property
    def venue(self) -> Venue:
        return Venue.BETFAIR

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "BetfairClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    def _login(self) -> str:
        resp = self._client.post(
            _LOGIN_URL,
            data={"username": self._username, "password": self._password},
            headers={"X-Application": self._app_key, "Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        body = resp.json()
        status = body.get("status") or body.get("loginStatus")
        if str(status).upper() != "SUCCESS":
            raise RuntimeError(f"Betfair login failed: {body}")
        token = body.get("token") or body.get("sessionToken")
        if not token:
            raise RuntimeError(f"Betfair login: no token in response: {body}")
        logger.info("Betfair session established")
        return str(token)

    def _ensure_session(self) -> str:
        if not self._session_token or (time.time() - self._session_ts) > 1000:
            self._session_token = self._login()
            self._session_ts = time.time()
        return self._session_token

    def _headers(self) -> dict[str, str]:
        return {
            "X-Application": self._app_key,
            "X-Authentication": self._ensure_session(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------ #
    # Low-level request
    # ------------------------------------------------------------------ #

    def _post(self, endpoint: str, body: Any) -> Any:
        url = f"{_API_BASE}/{endpoint}/"

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
                self._session_token = None  # Force re-login on next attempt
            resp.raise_for_status()
            return resp.json()

        return _do()

    # ------------------------------------------------------------------ #
    # Market discovery
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_binary(runners: list[dict]) -> bool:
        """True when the market has exactly 2 runners mapping to YES / NO."""
        if len(runners) != 2:
            return False
        labels = {r.get("runnerName", "").strip().lower() for r in runners}
        has_yes = bool(labels & _YES_LABELS)
        has_no = bool(labels & _NO_LABELS)
        return has_yes and has_no

    def _catalogue_to_market(self, row: dict) -> Optional[Market]:
        runners = row.get("runners") or []
        if not self._is_binary(runners):
            return None
        return Market.from_betfair({**row, "_runners": runners})

    def get_markets(self, *, max_results: int = 200, **_) -> list[Market]:
        """
        Fetch binary YES/NO markets from Betfair politics / global events.
        Returns normalised Market list.
        """
        if not self._username or not self._app_key:
            logger.warning("Betfair credentials not configured — skipping market fetch")
            return []

        try:
            body = {
                "filter": {
                    "eventTypeIds": _BINARY_EVENT_TYPES,
                    "marketBettingTypes": ["ODDS"],
                    "inPlayOnly": False,
                    "marketCountries": [],
                },
                "marketProjection": ["EVENT", "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
                "maxResults": max_results,
                "sort": "MAXIMUM_AVAILABLE",
            }
            raw = self._post("listMarketCatalogue", body)
            markets = []
            for row in raw:
                m = self._catalogue_to_market(row)
                if m:
                    markets.append(m)
            logger.info("Betfair: fetched {} binary markets", len(markets))
            return markets
        except Exception as e:
            logger.error("Betfair get_markets failed: {}", e)
            return []

    # ------------------------------------------------------------------ #
    # Quotes
    # ------------------------------------------------------------------ #

    def best_quotes(self, market: Market) -> Optional[dict[str, float]]:
        """
        Return yes/no best-ask prices in [0,1] probability space.

        Betfair "back" price X → implied prob = 1/X.
        Size is in the exchange's base currency (GBP/EUR).
        """
        if not self._username or not self._app_key:
            return None
        try:
            body = {
                "marketIds": [market.market_id],
                "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
                "orderProjection": "EXECUTABLE",
                "matchProjection": "NO_ROLLUP",
            }
            raw = self._post("listMarketBook", body)
            if not raw:
                return None

            book = raw[0]
            runners = book.get("runners") or []
            if len(runners) < 2:
                return None

            catalogue_runners = market.extra.get("runners") or market.extra.get("_runners") or []
            runner_names: dict[int, str] = {
                r["selectionId"]: r.get("runnerName", "").strip().lower()
                for r in catalogue_runners
                if "selectionId" in r
            }

            yes_ask: Optional[float] = None
            yes_ask_size: float = 0.0
            no_ask: Optional[float] = None
            no_ask_size: float = 0.0

            for runner in runners:
                sel_id = runner.get("selectionId")
                name = runner_names.get(sel_id, "")
                ex = runner.get("ex") or {}
                backs = ex.get("availableToBack") or []
                if not backs:
                    continue
                best_back_price = float(backs[0]["price"])
                best_back_size = float(backs[0].get("size", 0))
                if best_back_price <= 1.0:
                    continue
                implied = 1.0 / best_back_price
                if name in _YES_LABELS:
                    yes_ask = implied
                    yes_ask_size = best_back_size
                elif name in _NO_LABELS:
                    no_ask = implied
                    no_ask_size = best_back_size

            if yes_ask is None or no_ask is None:
                return None

            return {
                "yes_best_ask": yes_ask,
                "yes_best_ask_size": yes_ask_size,
                "no_best_ask": no_ask,
                "no_best_ask_size": no_ask_size,
            }
        except Exception as e:
            logger.debug("Betfair best_quotes failed for {}: {}", market.market_id, e)
            return None

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def place_order(self, market: Market, side: str, quantity: float, price: float) -> str:
        """
        Place a BACK order on Betfair.

        side: "yes" or "no"
        price: implied probability in [0,1]; converted to decimal odds = 1/price
        quantity: stake in account currency (GBP/EUR)
        """
        if self._settings.dry_run:
            import uuid
            oid = f"dry-betfair-{uuid.uuid4().hex[:12]}"
            logger.debug("Dry-run Betfair order {} side={} qty={} price={:.4f}", oid, side, quantity, price)
            return oid

        catalogue_runners = market.extra.get("runners") or market.extra.get("_runners") or []
        target_label = "yes" if side.lower() == "yes" else "no"
        selection_id: Optional[int] = None
        for r in catalogue_runners:
            if r.get("runnerName", "").strip().lower() in (
                _YES_LABELS if target_label == "yes" else _NO_LABELS
            ):
                selection_id = r.get("selectionId")
                break

        if selection_id is None:
            raise ValueError(f"Cannot find {side} runner in market {market.market_id}")

        decimal_odds = round(1.0 / price, 2) if price > 0 else 2.0
        decimal_odds = max(1.01, min(1000.0, decimal_odds))

        body = {
            "marketId": market.market_id,
            "instructions": [{
                "orderType": "LIMIT",
                "selectionId": selection_id,
                "side": "BACK",
                "limitOrder": {
                    "size": round(quantity, 2),
                    "price": decimal_odds,
                    "persistenceType": "LAPSE",
                },
            }],
        }
        resp = self._post("placeOrders", body)
        result = (resp.get("instructionReports") or [{}])[0]
        bet_id = result.get("betId") or result.get("instruction", {}).get("betId")
        if not bet_id:
            raise RuntimeError(f"Betfair placeOrders: no betId in response: {resp}")
        logger.info("Betfair order placed: betId={} side={} odds={} stake={}", bet_id, side, decimal_odds, quantity)
        return str(bet_id)

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        try:
            resp = self._post("listCurrentOrders", {"betIds": [order_id]})
            orders = (resp or {}).get("currentOrders") or []
            return orders[0] if orders else {}
        except Exception as e:
            logger.debug("Betfair get_order_status {}: {}", order_id, e)
            return {}

    def cancel_order(self, order_id: str) -> None:
        try:
            self._post("cancelOrders", {"instructions": [{"betId": order_id}]})
            logger.debug("Betfair cancel sent for {}", order_id)
        except Exception as e:
            logger.warning("Betfair cancel failed for {}: {}", order_id, e)
