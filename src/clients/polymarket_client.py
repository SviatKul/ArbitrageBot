"""Read-only Polymarket CLOB HTTP client (public market endpoints)."""

from __future__ import annotations

from typing import Any, Mapping, Optional

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import Settings
from models.types import Market, markets_from_polymarket_api


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


class PolymarketClobClient:
    """
    Public CLOB API client (``https://clob.polymarket.com``).

    Stage 1 only implements paginated ``GET /markets`` used for discovery and sanity checks.
    """

    def __init__(self, settings: Settings, *, client: Optional[httpx.Client] = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=settings.http_timeout_seconds)
        logger.info("Polymarket CLOB client initialized (host={})", settings.polymarket_clob_host)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PolymarketClobClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _request(self, method: str, path: str, *, params: Optional[Mapping[str, Any]] = None) -> httpx.Response:
        if not path.startswith("/"):
            path = f"/{path}"
        host = self._settings.polymarket_clob_host.rstrip("/")
        url = f"{host}{path}"

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
            response = self._client.request(method.upper(), url, params=dict(params or {}))
            response.raise_for_status()
            return response

        return _do()

    def list_markets(self, *, next_cursor: Optional[str] = None) -> dict[str, Any]:
        """
        GET /markets — paginated payload with ``data``, ``next_cursor``, ``limit``, ``count``.
        """
        params: dict[str, Any] = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        response = self._request("GET", "/markets", params=params or None)
        return response.json()

    def get_book(self, token_id: str) -> dict[str, Any]:
        """
        GET /book?token_id={token_id}

        Returns raw CLOB L2 payload::

            {"bids": [{"price": "0.49", "size": "200"}, ...],
             "asks": [{"price": "0.51", "size": "150"}, ...]}

        Asks are sorted ascending (best ask first).
        """
        response = self._request("GET", "/book", params={"token_id": token_id})
        return response.json()

    def best_quotes_for_market(self, market_extra: dict[str, Any]) -> Optional[dict[str, float]]:
        """
        Given the raw ``Market.extra`` dict from a Polymarket market, fetch best ask quotes
        for both YES and NO tokens.

        Returns ``{yes_best_ask, yes_best_ask_size, no_best_ask, no_best_ask_size}`` or ``None``.
        """
        tokens = market_extra.get("tokens")
        if not isinstance(tokens, list):
            return None

        yes_token_id: Optional[str] = None
        no_token_id: Optional[str] = None
        for t in tokens:
            if not isinstance(t, dict):
                continue
            label = str(t.get("outcome") or t.get("name") or "").strip().upper()
            tid = t.get("token_id") or t.get("tokenId")
            if not tid:
                continue
            if label in {"YES", "Y"}:
                yes_token_id = str(tid)
            elif label in {"NO", "N"}:
                no_token_id = str(tid)

        if not yes_token_id or not no_token_id:
            return None

        result: dict[str, float] = {}
        try:
            yes_book = self.get_book(yes_token_id)
            yes_asks = yes_book.get("asks") or []
            if yes_asks:
                result["yes_best_ask"] = float(yes_asks[0]["price"])
                result["yes_best_ask_size"] = float(yes_asks[0]["size"])
        except Exception as e:
            logger.debug("Polymarket YES book fetch failed for {}: {}", yes_token_id, e)
            return None

        try:
            no_book = self.get_book(no_token_id)
            no_asks = no_book.get("asks") or []
            if no_asks:
                result["no_best_ask"] = float(no_asks[0]["price"])
                result["no_best_ask_size"] = float(no_asks[0]["size"])
        except Exception as e:
            logger.debug("Polymarket NO book fetch failed for {}: {}", no_token_id, e)
            return None

        if len(result) < 4:
            return None
        return result

    @property
    def venue(self) -> "Venue":
        from models.types import Venue
        return Venue.POLYMARKET

    def best_quotes(self, market: "Market") -> "Optional[dict[str, float]]":
        """Unified ExchangeClient protocol: delegates to best_quotes_for_market."""
        return self.best_quotes_for_market(dict(market.extra))

    def get_markets(self, *, max_pages: int = 1) -> list[Market]:
        """
        Concatenate one or more ``GET /markets`` pages into normalized ``Market`` rows.

        ``max_pages`` caps how many ``next_cursor`` hops to follow per iteration.
        """
        acc: list[Market] = []
        cursor: Optional[str] = None
        for _ in range(max(1, max_pages)):
            payload = self.list_markets(next_cursor=cursor)
            chunk = payload.get("data") or []
            acc.extend(markets_from_polymarket_api(chunk))
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return acc

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
        Route to _exec_adapter (py-clob-client) if configured, else raise.
        The adapter is attached by run.py: client._exec_adapter = PolymarketAdapter()
        """
        adapter = getattr(self, "_exec_adapter", None)
        if adapter is None:
            raise RuntimeError(
                "Polymarket live orders require POLYMARKET_PRIVATE_KEY and py-clob-client. "
                "Set the env var or run in DRY_RUN=true mode."
            )
        return adapter.place_order(market, side, quantity, price)

    def get_order_status(self, order_id: str) -> dict:
        adapter = getattr(self, "_exec_adapter", None)
        if adapter is None:
            return {"status": "unknown"}
        return adapter.get_order_status(order_id)

    def cancel_order(self, order_id: str) -> None:
        adapter = getattr(self, "_exec_adapter", None)
        if adapter is not None:
            adapter.cancel_order(order_id)
