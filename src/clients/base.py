"""Base protocol that every exchange client must implement."""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from models.types import Market, Venue


@runtime_checkable
class ExchangeClient(Protocol):
    """Uniform interface for any prediction market / betting exchange."""

    @property
    def venue(self) -> Venue: ...

    def get_markets(self, **kwargs) -> list[Market]: ...

    def best_quotes(self, market: Market) -> Optional[dict[str, float]]:
        """Return yes/no best-ask quotes in [0,1] probability space or None."""
        ...

    def place_order(self, market: Market, side: str, quantity: float, price: float) -> str:
        """Place an aggressive taker order. Returns venue order_id."""
        ...

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Return raw order status payload."""
        ...

    def cancel_order(self, order_id: str) -> None: ...

    def close(self) -> None: ...
