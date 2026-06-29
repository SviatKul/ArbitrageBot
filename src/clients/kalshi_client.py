"""Read-only Kalshi Trade API client with optional RSA-PSS request signing."""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import Settings
from models.types import Market, markets_from_kalshi_api


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


class KalshiClient:
    """
    Minimal Trade API v2 HTTP client.

    Kalshi signs ``timestamp + METHOD + path`` (path without query string, from URL root),
    using RSA-PSS + SHA256 and base64-encoded signature. See Kalshi authenticated request docs.

    Public ``GET /markets`` works without credentials; when ``kalshi_signing_configured`` is true,
    auth headers are attached for all requests (recommended for production parity).
    """

    def __init__(self, settings: Settings, *, client: Optional[httpx.Client] = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=settings.http_timeout_seconds)
        self._private_key: Optional[rsa.RSAPrivateKey] = None
        if settings.kalshi_signing_configured:
            self._private_key = self._load_private_key(settings)
            logger.info("Kalshi client initialized with RSA signing enabled")
        else:
            logger.warning(
                "Kalshi RSA signing disabled (set KALSHI_API_KEY_ID and "
                "KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM). Public read-only calls still work."
            )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def _load_private_key(settings: Settings) -> rsa.RSAPrivateKey:
        pem_data: bytes
        if settings.kalshi_private_key_pem and settings.kalshi_private_key_pem.strip():
            pem_data = settings.kalshi_private_key_pem.strip().encode("utf-8")
        elif settings.kalshi_private_key_path and settings.kalshi_private_key_path.strip():
            path = Path(settings.kalshi_private_key_path).expanduser()
            pem_data = path.read_bytes()
        else:
            raise ValueError("Kalshi private key not provided (PEM string or file path required)")
        key = serialization.load_pem_private_key(pem_data, password=None, backend=default_backend())
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("Kalshi private key must be an RSA key in PEM format")
        return key

    def _sign(self, timestamp_ms: str, method: str, path_for_signing: str) -> str:
        if self._private_key is None:
            raise RuntimeError("Signing requested but private key is not loaded")
        path_without_query = path_for_signing.split("?", maxsplit=1)[0]
        message = f"{timestamp_ms}{method.upper()}{path_without_query}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, full_url: str) -> Mapping[str, str]:
        if not self._settings.kalshi_signing_configured or self._private_key is None:
            return {}
        parsed = urlparse(full_url)
        path_for_signing = parsed.path or "/"
        timestamp_ms = str(int(time.time() * 1000))
        signature = self._sign(timestamp_ms, method, path_for_signing)
        return {
            "KALSHI-ACCESS-KEY": self._settings.kalshi_api_key_id or "",
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_payload: Optional[Any] = None,
    ) -> httpx.Response:
        if not path.startswith("/"):
            path = f"/{path}"
        base = self._settings.kalshi_base_url.rstrip("/")
        url = f"{base}{path}"
        if params:
            query = urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                url = f"{url}?{query}"

        json_body = json_payload

        @retry(
            reraise=True,
            stop=stop_after_attempt(self._settings.retry_max_attempts),
            wait=wait_exponential(
                multiplier=1,
                min=self._settings.retry_wait_min_seconds,
                max=self._settings.retry_wait_max_seconds,
            ),
            retry=retry_if_exception(_is_retryable_exception),
        )
        def _do() -> httpx.Response:
            # Fresh timestamp + signature on each attempt (Kalshi rejects stale timestamps).
            headers = dict(self._auth_headers(method, url))
            if json_body is not None:
                headers.setdefault("Content-Type", "application/json")
            response = self._client.request(
                method.upper(),
                url,
                headers=headers,
                json=json_body,
            )
            response.raise_for_status()
            return response

        return _do()

    def api_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_payload: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Authenticated JSON request (RSA-signed when credentials are configured)."""
        return self._request(method, path, params=params, json_payload=json_payload).json()

    def list_markets(
        self,
        *,
        limit: int = 100,
        cursor: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        GET /markets — returns ``{"markets": [...], "cursor": ...}`` per Kalshi Trade API.

        ``status`` may be one of: ``unopened``, ``open``, ``closed``, ``settled`` (optional).
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        response = self._request("GET", "/markets", params=params)
        return response.json()

    @property
    def venue(self) -> "Venue":
        from models.types import Venue
        return Venue.KALSHI

    def get_markets(
        self,
        *,
        limit: int = 500,
        cursor: Optional[str] = None,
        status: Optional[str] = None,
        max_pages: int = 20,
    ) -> list[Market]:
        """Fetch all markets via cursor pagination and return normalized ``Market`` rows."""
        acc: list[Any] = []
        cur = cursor
        for _ in range(max(1, max_pages)):
            payload = self.list_markets(limit=limit, cursor=cur, status=status)
            rows = payload.get("markets") or []
            acc.extend(rows)
            cur = payload.get("cursor")
            if not cur or not rows:
                break
        return markets_from_kalshi_api(acc)

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        """
        GET /markets/{ticker}/orderbook

        Returns raw payload::

            {"orderbook": {"yes": [[price_cents, qty], ...], "no": [[price_cents, qty], ...]}}

        YES and NO arrays contain **bids** sorted by price descending.
        To buy YES as taker: best YES ask ≈ (100 − best_no_bid) / 100.
        """
        response = self._request("GET", f"/markets/{ticker}/orderbook")
        return response.json()

    def best_quotes(self, market_or_ticker) -> Optional[dict[str, float]]:
        """
        Unified ExchangeClient protocol + legacy ticker-string support.

        Accepts either a Market object or a ticker string.
        Returns yes/no best-ask quotes in [0,1] probability space or None.
        """
        from models.types import Market as _Market
        ticker = market_or_ticker.market_id if isinstance(market_or_ticker, _Market) else market_or_ticker
        try:
            raw = self.get_orderbook(ticker)
        except Exception as e:
            logger.debug("Kalshi orderbook fetch failed for {}: {}", ticker, e)
            return None

        ob = raw.get("orderbook") or {}
        yes_bids: list[list[Any]] = ob.get("yes") or []
        no_bids: list[list[Any]] = ob.get("no") or []

        if not yes_bids and not no_bids:
            return None

        # Best YES ask = price a taker pays to buy YES = 100 − best NO bid
        # Best NO ask  = price a taker pays to buy NO  = 100 − best YES bid
        result: dict[str, float] = {}
        if no_bids:
            best_no_bid_cents = float(no_bids[0][0])
            best_no_bid_size = float(no_bids[0][1]) if len(no_bids[0]) > 1 else 0.0
            result["yes_best_ask"] = (100.0 - best_no_bid_cents) / 100.0
            result["yes_best_ask_size"] = best_no_bid_size
        if yes_bids:
            best_yes_bid_cents = float(yes_bids[0][0])
            best_yes_bid_size = float(yes_bids[0][1]) if len(yes_bids[0]) > 1 else 0.0
            result["no_best_ask"] = (100.0 - best_yes_bid_cents) / 100.0
            result["no_best_ask_size"] = best_yes_bid_size

        if len(result) < 4:
            return None
        return result

    # ------------------------------------------------------------------ #
    # Order execution (ExchangeClient protocol)
    # ------------------------------------------------------------------ #

    def place_order(
        self,
        market: "Market",
        side: str,
        quantity: float,
        price: float,
    ) -> str:
        """
        POST /portfolio/orders — place a limit order on Kalshi.

        side: "yes" or "no"
        price: implied probability in [0, 1] → converted to cents internally
        Returns order_id string.
        """
        from models.types import Market as _Market
        ticker = market.market_id if isinstance(market, _Market) else str(market)
        side_upper = side.upper()
        if side_upper not in {"YES", "NO"}:
            raise ValueError(f"side must be YES or NO, got: {side!r}")

        price_cents = max(1, min(99, round(float(price) * 100)))
        payload = {
            "ticker":   ticker,
            "action":   "buy",
            "side":     side_upper,
            "type":     "limit",
            "count":    int(quantity),
            "yes_price": price_cents if side_upper == "YES" else (100 - price_cents),
            "no_price":  price_cents if side_upper == "NO"  else (100 - price_cents),
        }
        resp = self._request("POST", "/portfolio/orders", json_payload=payload)
        data = resp.json()
        order = data.get("order") or data
        order_id = str(order.get("order_id") or order.get("id") or "")
        if not order_id:
            raise RuntimeError(f"Kalshi place_order returned no order_id: {data}")
        logger.info("Kalshi order placed: {} {} qty={} price={}¢ oid={}", ticker, side_upper, int(quantity), price_cents, order_id)
        return order_id

    def get_order_status(self, order_id: str) -> dict:
        """GET /portfolio/orders/{order_id}"""
        resp = self._request("GET", f"/portfolio/orders/{order_id}")
        data = resp.json()
        return data.get("order") or data

    def cancel_order(self, order_id: str) -> None:
        """DELETE /portfolio/orders/{order_id}"""
        try:
            self._request("DELETE", f"/portfolio/orders/{order_id}")
            logger.info("Kalshi order cancelled: {}", order_id)
        except Exception as e:
            logger.warning("Kalshi cancel_order {} failed: {}", order_id, e)
