"""
Smarkets Exchange client — political & sports binary markets.

REST API v3: https://api.smarkets.com/v3/

Required env vars:
  SMARKETS_API_TOKEN  — from https://smarkets.com/account/settings/api

Binary market detection: Smarkets contracts are inherently YES/NO binary.
Each "market" has exactly two "contracts": YES and NO.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from config.settings import Settings
from models.types import Market, Venue

_API_BASE = "https://api.smarkets.com/v3"

# Smarkets event categories that overlap with Polymarket
_CATEGORIES = ["politics", "economics", "finance", "current-affairs", "entertainment"]


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class SmarketsClient:
    """Read + execute client for Smarkets Exchange."""

    def __init__(self, settings: Settings, *, client: Optional[httpx.Client] = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=settings.http_timeout_seconds)
        self._token = settings.smarkets_api_token or ""
        logger.info("Smarkets client initialized (token={})", "***" if self._token else "MISSING")

    @property
    def venue(self) -> Venue:
        return Venue.SMARKETS

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SmarketsClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

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
            resp.raise_for_status()
            return resp.json()

        return _do()

    # ------------------------------------------------------------------ #
    # Market discovery
    # ------------------------------------------------------------------ #

    def get_markets(self, *, limit: int = 200, **_) -> list[Market]:
        """Fetch binary markets from Smarkets political/finance categories."""
        if not self._token:
            logger.warning("Smarkets API token not configured — skipping market fetch")
            return []

        markets: list[Market] = []
        for category in _CATEGORIES:
            try:
                data = self._get(f"/events/", params={
                    "state": "upcoming,live",
                    "type": "binary",
                    "sort": "popular",
                    "limit": limit,
                    "category": category,
                })
                events = data.get("events") or []
                for event in events:
                    event_markets = event.get("markets") or []
                    for m in event_markets:
                        markets.append(Market.from_smarkets({
                            **m,
                            "_event_name": event.get("name", ""),
                            "_event_id": event.get("id", ""),
                        }))
            except Exception as e:
                logger.debug("Smarkets get_markets({}): {}", category, e)

        logger.info("Smarkets: fetched {} markets", len(markets))
        return markets

    # ------------------------------------------------------------------ #
    # Quotes
    # ------------------------------------------------------------------ #

    def best_quotes(self, market: Market) -> Optional[dict[str, float]]:
        """
        Fetch best yes/no ask prices from Smarkets.
        Smarkets quotes are in percentage (0–100); divide by 100 for [0,1].
        """
        if not self._token:
            return None
        try:
            data = self._get(f"/markets/{market.market_id}/quotes/")
            contracts = data.get("contracts") or []

            yes_ask: Optional[float] = None
            yes_size: float = 0.0
            no_ask: Optional[float] = None
            no_size: float = 0.0

            for contract in contracts:
                name = str(contract.get("name") or "").strip().lower()
                quotes = contract.get("quotes") or {}
                best_buy = quotes.get("best_buy") or {}
                price_raw = best_buy.get("price")
                quantity_raw = best_buy.get("quantity")

                if price_raw is None:
                    continue

                price_01 = float(price_raw) / 100.0
                size = float(quantity_raw or 0)

                if name in {"yes", "y", "for", "backs"}:
                    yes_ask = price_01
                    yes_size = size
                elif name in {"no", "n", "against", "lays"}:
                    no_ask = price_01
                    no_size = size

            if yes_ask is None or no_ask is None:
                return None

            return {
                "yes_best_ask": yes_ask,
                "yes_best_ask_size": yes_size,
                "no_best_ask": no_ask,
                "no_best_ask_size": no_size,
            }
        except Exception as e:
            logger.debug("Smarkets best_quotes failed for {}: {}", market.market_id, e)
            return None

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def place_order(self, market: Market, side: str, quantity: float, price: float) -> str:
        """
        Place a BUY order on Smarkets.

        side: "yes" or "no"
        price: [0,1] probability → Smarkets percentage = price * 100
        quantity: amount in pennies (GBP)
        """
        if self._settings.dry_run:
            oid = f"dry-smarkets-{uuid.uuid4().hex[:12]}"
            logger.debug("Dry-run Smarkets order {} side={} qty={} price={:.4f}", oid, side, quantity, price)
            return oid

        contracts = market.extra.get("contracts") or []
        target_name = "yes" if side.lower() == "yes" else "no"
        contract_id: Optional[str] = None
        for c in contracts:
            if str(c.get("name", "")).strip().lower() == target_name:
                contract_id = str(c.get("id", ""))
                break

        if not contract_id:
            raise ValueError(f"Cannot find {side} contract in Smarkets market {market.market_id}")

        smarkets_price = int(round(price * 100))
        body = {
            "contract_id": contract_id,
            "side": "buy",
            "price": smarkets_price,
            "quantity": int(quantity * 100),  # pennies
        }
        url = f"{_API_BASE}/orders/"
        resp = self._client.post(url, json=body, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        order_id = data.get("order", {}).get("id") or data.get("id")
        if not order_id:
            raise RuntimeError(f"Smarkets place_order: no id in response: {data}")
        logger.info("Smarkets order placed: id={} side={} price={}% qty={}", order_id, side, smarkets_price, quantity)
        return str(order_id)

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        try:
            data = self._get(f"/orders/{order_id}/")
            return data.get("order") or {}
        except Exception as e:
            logger.debug("Smarkets get_order_status {}: {}", order_id, e)
            return {}

    def cancel_order(self, order_id: str) -> None:
        try:
            url = f"{_API_BASE}/orders/{order_id}/"
            resp = self._client.delete(url, headers=self._headers())
            resp.raise_for_status()
            logger.debug("Smarkets cancel sent for {}", order_id)
        except Exception as e:
            logger.warning("Smarkets cancel failed for {}: {}", order_id, e)
