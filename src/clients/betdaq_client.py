"""
Betdaq Exchange client — binary political/event markets.

Auth flow:
  POST https://api.betdaq.com/v2.0/user/session  (username + password)
  → sessionToken used in Authorization header for all subsequent calls.
  Session expires after ~30 min of inactivity; auto-renewed on 401.

Required env vars:
  BETDAQ_USERNAME
  BETDAQ_PASSWORD
  BETDAQ_API_KEY  — from https://developer.betdaq.com/

Commission: 2% on net winnings (vs Betfair's 5% — key advantage).

Betdaq event classification IDs for binary prediction markets:
  100305 — Politics & Current Affairs
  100338 — World Politics
  100301 — Financial / Economy
  100323 — Entertainment / Awards
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


_LOGIN_URL = "https://api.betdaq.com/v2.0/user/session"
_API_BASE  = "https://api.betdaq.com/v2.0"

# Betdaq top-level event classification IDs for binary markets
_BINARY_EVENT_CLASSES = [100305, 100338, 100301, 100323]

_YES_LABELS = {"yes", "y", "for", "true", "backs", "will", "to win"}
_NO_LABELS  = {"no",  "n", "against", "false", "lays",  "wont", "will not"}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class BetdaqClient:
    """
    Read + execute client for Betdaq Exchange.

    Binary market detection: markets with exactly 2 runners whose names
    normalise to YES / NO variants. Commission 2% on winnings.
    """

    def __init__(self, settings: Settings, *, client: Optional[httpx.Client] = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=settings.http_timeout_seconds)
        self._username  = settings.betdaq_username or ""
        self._password  = settings.betdaq_password or ""
        self._api_key   = settings.betdaq_api_key  or ""
        self._session_token: Optional[str] = None
        self._session_ts: float = 0.0
        logger.info("Betdaq client initialized (user={})", self._username)

    # ------------------------------------------------------------------ #
    # Protocol
    # ------------------------------------------------------------------ #

    @property
    def venue(self) -> Venue:
        return Venue.BETDAQ

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "BetdaqClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    def _login(self) -> str:
        resp = self._client.post(
            _LOGIN_URL,
            json={"username": self._username, "password": self._password},
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": self._api_key,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        token = (
            body.get("sessionToken")
            or body.get("session_token")
            or (body.get("data") or {}).get("sessionToken")
        )
        if not token:
            raise RuntimeError(f"Betdaq login: no token in response: {body}")
        logger.info("Betdaq session established")
        return str(token)

    def _ensure_session(self) -> str:
        if not self._session_token or (time.time() - self._session_ts) > 1500:
            self._session_token = self._login()
            self._session_ts = time.time()
        return self._session_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._ensure_session()}",
            "X-Api-Key": self._api_key,
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
        """Fetch binary YES/NO markets from Betdaq politics/global events."""
        if not self._username or not self._api_key:
            logger.warning("Betdaq credentials not configured — skipping")
            return []

        markets: list[Market] = []
        for class_id in _BINARY_EVENT_CLASSES:
            try:
                data = self._get("/markets", params={
                    "eventClassificationId": class_id,
                    "state": "active",
                    "type": "binary",
                    "perPage": min(max_results, 500),
                })
                rows = (
                    data.get("markets")
                    or data.get("data", {}).get("markets")
                    or []
                )
                for row in rows:
                    runners = row.get("runners") or row.get("selections") or []
                    if not self._is_binary(runners):
                        continue
                    markets.append(Market(
                        venue=Venue.BETDAQ,
                        market_id=str(row.get("id") or row.get("marketId", "")),
                        title=str(row.get("name") or row.get("marketName") or ""),
                        extra={**row, "_runners": runners},
                    ))
            except Exception as e:
                logger.debug("Betdaq get_markets(class={}): {}", class_id, e)

        logger.info("Betdaq: fetched {} binary markets", len(markets))
        return markets

    # ------------------------------------------------------------------ #
    # Quotes
    # ------------------------------------------------------------------ #

    def best_quotes(self, market: Market) -> Optional[dict[str, float]]:
        """
        Return yes/no best-ask in [0,1] probability space.

        Betdaq decimal odds X → implied prob = 1/X.
        """
        if not self._username or not self._api_key:
            return None
        try:
            data = self._get(f"/markets/{market.market_id}/prices")
            runners = (
                data.get("runners")
                or data.get("selections")
                or data.get("data", {}).get("runners")
                or []
            )
            if len(runners) < 2:
                return None

            catalogue = market.extra.get("_runners") or []
            id_to_name: dict[str, str] = {
                str(r.get("id") or r.get("selectionId", "")): r.get("name", "").strip().lower()
                for r in catalogue
            }

            yes_ask: Optional[float] = None
            yes_size: float = 0.0
            no_ask: Optional[float] = None
            no_size: float = 0.0

            for runner in runners:
                rid = str(runner.get("id") or runner.get("selectionId", ""))
                name = id_to_name.get(rid) or runner.get("name", "").strip().lower()
                # Best available back price
                backs = runner.get("availableToBack") or runner.get("backPrices") or []
                if not backs:
                    continue
                best = backs[0]
                price = float(best.get("price") or best.get("odds") or 0)
                size  = float(best.get("size")  or best.get("amount") or 0)
                if price <= 1.0:
                    continue
                implied = 1.0 / price

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
            logger.debug("Betdaq best_quotes failed for {}: {}", market.market_id, e)
            return None

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def place_order(self, market: Market, side: str, quantity: float, price: float) -> str:
        """
        Place a BACK order on Betdaq.

        side:     "yes" or "no"
        price:    implied probability [0,1] → decimal odds = 1/price
        quantity: stake in account currency
        """
        if self._settings.dry_run:
            oid = f"dry-betdaq-{uuid.uuid4().hex[:12]}"
            logger.debug("Dry-run Betdaq order {} side={} qty={} price={:.4f}", oid, side, quantity, price)
            return oid

        runners = market.extra.get("_runners") or []
        target_labels = _YES_LABELS if side.lower() == "yes" else _NO_LABELS
        selection_id: Optional[str] = None
        for r in runners:
            if r.get("name", "").strip().lower() in target_labels:
                selection_id = str(r.get("id") or r.get("selectionId", ""))
                break

        if not selection_id:
            raise ValueError(f"Cannot find {side} runner in Betdaq market {market.market_id}")

        decimal_odds = round(1.0 / price, 2) if price > 0 else 2.0
        decimal_odds = max(1.01, min(1000.0, decimal_odds))

        body = {
            "marketId": market.market_id,
            "orders": [{
                "selectionId": selection_id,
                "side": "back",
                "price": decimal_odds,
                "size": round(quantity, 2),
                "type": "limit",
                "persistenceType": "lapse",
            }],
        }
        resp = self._post("/order/orders", body)
        reports = resp.get("orderReports") or resp.get("data", {}).get("orderReports") or [{}]
        order_id = (
            reports[0].get("orderId")
            or reports[0].get("id")
            or resp.get("orderId")
        )
        if not order_id:
            raise RuntimeError(f"Betdaq place_order: no orderId in response: {resp}")
        logger.info("Betdaq order placed: id={} side={} odds={} stake={}", order_id, side, decimal_odds, quantity)
        return str(order_id)

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        try:
            data = self._get(f"/order/orders/{order_id}")
            return data.get("order") or data.get("data") or {}
        except Exception as e:
            logger.debug("Betdaq get_order_status {}: {}", order_id, e)
            return {}

    def cancel_order(self, order_id: str) -> None:
        try:
            resp = self._client.delete(
                f"{_API_BASE}/order/orders/{order_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            logger.debug("Betdaq cancel sent for {}", order_id)
        except Exception as e:
            logger.warning("Betdaq cancel failed for {}: {}", order_id, e)
