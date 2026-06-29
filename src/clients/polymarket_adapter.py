"""
Polymarket CLOB execution adapter using the official py-clob-client library.

Required env vars (add to .env):
    POLYMARKET_PRIVATE_KEY   — Ethereum private key (hex, with or without 0x prefix)
    POLYMARKET_API_KEY       — CLOB API key (from Polymarket dashboard or derived via client.create_api_key())
    POLYMARKET_API_SECRET    — CLOB API secret
    POLYMARKET_API_PASSPHRASE — CLOB API passphrase
    POLYMARKET_CHAIN_ID      — 137 for Polygon mainnet (default)

Install: pip install py-clob-client

Note: Polymarket is geo-restricted. Use a non-US IP.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from loguru import logger

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
    from py_clob_client.constants import POLYGON

    _CLOB_AVAILABLE = True
except ImportError:
    _CLOB_AVAILABLE = False
    ClobClient = None  # type: ignore[assignment,misc]


class PolymarketAdapter:
    """
    Concrete ``PolymarketExecutionAdapter`` backed by py-clob-client.

    Instantiate once and pass as ``polymarket_adapter`` to ``TradeExecutor``.
    """

    HOST = "https://clob.polymarket.com"

    def __init__(
        self,
        *,
        private_key: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
        chain_id: int = 137,
    ) -> None:
        if not _CLOB_AVAILABLE:
            raise ImportError(
                "py-clob-client is not installed. Run: pip install py-clob-client"
            )

        pk = private_key or os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        ak = api_key or os.environ.get("POLYMARKET_API_KEY", "")
        sk = api_secret or os.environ.get("POLYMARKET_API_SECRET", "")
        ps = api_passphrase or os.environ.get("POLYMARKET_API_PASSPHRASE", "")
        cid = chain_id or int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))

        if not pk:
            raise ValueError("POLYMARKET_PRIVATE_KEY is required")
        if not (ak and sk and ps):
            raise ValueError(
                "POLYMARKET_API_KEY / API_SECRET / API_PASSPHRASE are all required. "
                "Generate them at https://polymarket.com or via ClobClient.create_api_key()."
            )

        creds = ApiCreds(api_key=ak, api_secret=sk, api_passphrase=ps)
        self._client: ClobClient = ClobClient(
            host=self.HOST,
            chain_id=cid,
            key=pk,
            creds=creds,
        )
        logger.info("Polymarket adapter initialized (chain_id={})", cid)

    def place_order(self, market: "Any", side: str, quantity: float, price: float) -> str:
        """ExchangeClient protocol: delegates to place_market_order."""
        from models.types import Market as _Market
        token_id = ""
        if isinstance(market, _Market):
            tokens = dict(market.extra).get("tokens") or []
            target = side.upper()
            for t in tokens:
                label = str(t.get("outcome") or t.get("name") or "").strip().upper()
                if label == target:
                    token_id = str(t.get("token_id") or t.get("tokenId") or "")
                    break
            if not token_id:
                token_id = str(market.market_id)
        else:
            token_id = str(market)
        return self._place_market_order(token_id, side, quantity, price)

    def _place_market_order(self, token_id: str, side: str, quantity: float, price: float) -> str:
        """
        Place an aggressive market-style BUY order on the CLOB.

        ``side`` should be ``"BUY"`` for both YES and NO legs (we always buy the token we want).
        ``price`` is the maximum price (implied prob, 0–1) we're willing to pay — set slightly
        above the best ask to ensure fill.

        Returns the CLOB order_id string.
        """
        args = MarketOrderArgs(
            token_id=token_id,
            amount=quantity,
        )
        signed_order = self._client.create_market_order(args)
        resp = self._client.post_order(signed_order, OrderType.FOK)

        order_id: Optional[str] = None
        if isinstance(resp, Mapping):
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        if not order_id:
            raise RuntimeError(f"Polymarket order response missing orderID: {resp!r}")

        logger.info(
            "Polymarket order placed: token={} qty={} price={:.4f} order_id={}",
            token_id,
            quantity,
            price,
            order_id,
        )
        return str(order_id)

    def get_order_status(self, order_id: str) -> dict:
        """ExchangeClient protocol alias for get_order."""
        return dict(self.get_order(order_id))

    def get_order(self, order_id: str) -> Mapping[str, Any]:
        """Return raw order payload from the CLOB."""
        resp = self._client.get_order(order_id)
        if isinstance(resp, Mapping):
            return resp
        return {"raw": resp}

    def cancel_order(self, order_id: str) -> None:
        """Best-effort cancel of resting quantity."""
        try:
            self._client.cancel(order_id)
            logger.debug("Polymarket cancel sent for {}", order_id)
        except Exception as e:
            logger.warning("Polymarket cancel failed for {}: {}", order_id, e)

    def get_collateral_balance_usdc(self) -> Optional[float]:
        """Return USDC collateral balance for balance checks (used by TradeExecutor)."""
        try:
            bal = self._client.get_balance_allowance(asset_type=None)
            if isinstance(bal, Mapping):
                v = bal.get("balance") or bal.get("collateral_balance")
                if v is not None:
                    return float(v)
        except Exception as e:
            logger.warning("Polymarket balance fetch failed: {}", e)
        return None
