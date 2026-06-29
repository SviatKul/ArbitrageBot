"""
Concurrent cross-venue execution — routes orders to the correct exchange client.

Each leg (YES / NO) is submitted in parallel via the client registry.
Dry-run mode simulates fills without network I/O.
"""

from __future__ import annotations

import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional, TYPE_CHECKING

from loguru import logger

from config.settings import Settings
from models.types import ArbitrageOpportunity, Market, Venue

if TYPE_CHECKING:
    from clients.base import ExchangeClient
    from core.position_manager import PositionManager


def _hint_price(opp: ArbitrageOpportunity, *, want_yes: bool) -> float:
    if want_yes:
        raw = opp.yes_prices.get(opp.yes_venue.value)
        return float(raw) if raw is not None else 0.99
    raw = opp.no_prices.get(opp.no_venue.value)
    return float(raw) if raw is not None else 0.99


def _order_filled_qty(payload: dict[str, Any]) -> float:
    for key in ("filled_count", "filled_qty", "filled", "fill_count", "size_matched",
                "filled_amount", "matched_amount", "executed_count"):
        v = payload.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _order_is_resting(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("order_status") or "").lower()
    return status not in {"filled", "executed", "cancelled", "canceled", "expired", "lapsed"}


class TradeExecutor:
    """
    Places both legs in parallel via the exchange client registry.

    clients: {Venue: ExchangeClient} — one entry per enabled exchange.
    """

    def __init__(
        self,
        settings: Settings,
        clients: dict[Venue, "ExchangeClient"],
        position_manager: Optional["PositionManager"] = None,
    ) -> None:
        self._settings = settings
        self._clients = clients
        self._position_manager = position_manager

    def _client(self, venue: Venue) -> Optional["ExchangeClient"]:
        return self._clients.get(venue)

    def _kelly_qty(self, opp: ArbitrageOpportunity) -> int:
        """Quarter-Kelly (or fractional) optimal position sizing.

        For a near-riskless synthetic box the Kelly formula simplifies to:
          f* = edge / (1 - edge)   where edge = profit_pct / 100
        We then scale by kelly_fraction and bankroll, divide by cost-per-pair.
        """
        cap = int(self._settings.max_order_contracts)
        if not self._settings.kelly_enabled:
            return cap
        profit_pct = (opp.profit_percent or 0.0) / 100.0
        if profit_pct <= 0:
            return 1
        full_kelly = profit_pct / (1.0 - profit_pct + 1e-9)
        cost = float(opp.total_cost or 1.0)
        sized = int(self._settings.kelly_fraction * full_kelly * self._settings.kelly_bankroll / (cost + 1e-9))
        return max(1, min(sized, cap))

    def execute_arbitrage(self, opportunity: ArbitrageOpportunity) -> bool:
        yes_venue = opportunity.yes_venue
        no_venue = opportunity.no_venue

        qty = min(float(opportunity.max_executable_size), float(self._kelly_qty(opportunity)))
        qty_int = max(1, int(math.floor(qty)))

        yes_px = _hint_price(opportunity, want_yes=True)
        no_px = _hint_price(opportunity, want_yes=False)

        if not self._settings.dry_run:
            client_yes = self._client(yes_venue)
            client_no = self._client(no_venue)
            if client_yes is None:
                logger.error("No client configured for YES venue {}", yes_venue)
                return False
            if client_no is None:
                logger.error("No client configured for NO venue {}", no_venue)
                return False

        oid_yes: Optional[str] = None
        oid_no: Optional[str] = None
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_yes = pool.submit(
                    self._place_leg,
                    opportunity.yes_market, "yes", float(qty_int), yes_px,
                )
                fut_no = pool.submit(
                    self._place_leg,
                    opportunity.no_market, "no", float(qty_int), no_px,
                )
                oid_yes = fut_yes.result()
                oid_no = fut_no.result()
        except Exception as e:
            logger.exception("Concurrent leg submission failed: {}", e)
            self._safe_cancel(yes_venue, oid_yes)
            self._safe_cancel(no_venue, oid_no)
            return False

        if self._settings.dry_run:
            logger.info(
                "Dry-run execution OK: yes_order={} ({}) no_order={} ({})",
                oid_yes, yes_venue.value, oid_no, no_venue.value,
            )
            self._record_positions(opportunity, qty_int, yes_px, no_px)
            return True

        ok = self._wait_fill_or_cancel(
            yes_venue=yes_venue, oid_yes=oid_yes,
            no_venue=no_venue, oid_no=oid_no,
            qty=float(qty_int),
            yes_market=opportunity.yes_market,
            no_market=opportunity.no_market,
        )
        if ok:
            self._record_positions(opportunity, qty_int, yes_px, no_px)
        else:
            logger.warning(
                "Execution incomplete after timeout — cancelled resting qty "
                "(yes_oid={} no_oid={})", oid_yes, oid_no,
            )
        return ok

    def _record_positions(
        self,
        opp: ArbitrageOpportunity,
        qty: int,
        yes_px: float,
        no_px: float,
    ) -> None:
        if self._position_manager is None:
            return
        try:
            self._position_manager.add_position(
                venue=opp.yes_venue,
                market_id=opp.yes_market.market_id,
                side="YES",
                contracts=float(qty),
                entry_price=yes_px,
                title=opp.yes_market.title,
            )
            self._position_manager.add_position(
                venue=opp.no_venue,
                market_id=opp.no_market.market_id,
                side="NO",
                contracts=float(qty),
                entry_price=no_px,
                title=opp.no_market.title,
            )
        except Exception as e:
            logger.warning("Failed to record positions: {}", e)

    def _place_leg(
        self, market: Market, side: str, quantity: float, price: float
    ) -> str:
        if self._settings.dry_run:
            oid = f"dry-{market.venue.value}-{uuid.uuid4().hex[:12]}"
            logger.debug("Dry-run {} order {}", market.venue.value, oid)
            return oid
        client = self._client(market.venue)
        if client is None:
            raise RuntimeError(f"No client configured for venue {market.venue}")
        return client.place_order(market, side, quantity, price)

    def _wait_fill_or_cancel(
        self,
        *,
        yes_venue: Venue,
        oid_yes: Optional[str],
        no_venue: Venue,
        oid_no: Optional[str],
        qty: float,
        yes_market: Optional["Market"] = None,
        no_market: Optional["Market"] = None,
    ) -> bool:
        deadline = time.monotonic() + float(self._settings.order_fill_timeout_seconds)
        poll = float(self._settings.execution_poll_interval_seconds)
        min_ratio = float(self._settings.min_fill_ratio)

        while time.monotonic() < deadline:
            y_filled = self._get_filled(yes_venue, oid_yes) if oid_yes else qty
            n_filled = self._get_filled(no_venue, oid_no) if oid_no else qty
            if y_filled + 1e-9 >= qty and n_filled + 1e-9 >= qty:
                return True
            time.sleep(poll)

        # Таймаут — отменяем незаполненные ордера
        self._safe_cancel(yes_venue, oid_yes)
        self._safe_cancel(no_venue, oid_no)

        # Проверяем частичные заполнения и хеджируем несимметрию
        y_filled = self._get_filled(yes_venue, oid_yes) if oid_yes else 0.0
        n_filled = self._get_filled(no_venue, oid_no) if oid_no else 0.0

        y_ratio = y_filled / qty if qty > 0 else 0.0
        n_ratio = n_filled / qty if qty > 0 else 0.0

        if y_ratio >= min_ratio and n_ratio < min_ratio and yes_market is not None:
            logger.warning(
                "Leg imbalance: YES filled {:.0%} but NO filled {:.0%} — hedging YES leg",
                y_ratio, n_ratio,
            )
            self._hedge_leg(yes_market, "no", y_filled)
        elif n_ratio >= min_ratio and y_ratio < min_ratio and no_market is not None:
            logger.warning(
                "Leg imbalance: NO filled {:.0%} but YES filled {:.0%} — hedging NO leg",
                n_ratio, y_ratio,
            )
            self._hedge_leg(no_market, "yes", n_filled)

        return False

    def _hedge_leg(self, market: "Market", reverse_side: str, qty: float) -> None:
        """Продаём обратно заполненную ногу чтобы закрыть риск."""
        if self._settings.dry_run:
            logger.info("Dry-run hedge: {} {} qty={:.2f}", market.venue.value, reverse_side, qty)
            return
        client = self._client(market.venue)
        if client is None:
            logger.error("Cannot hedge: no client for {}", market.venue)
            return
        try:
            oid = client.place_order(market, reverse_side, qty, 0.99)
            logger.info("Hedge order placed: {} oid={}", market.venue.value, oid)
        except Exception as e:
            logger.error("Hedge order failed for {}: {}", market.venue.value, e)

    def _get_filled(self, venue: Venue, order_id: Optional[str]) -> float:
        if not order_id:
            return 0.0
        client = self._client(venue)
        if client is None:
            return 0.0
        try:
            payload = client.get_order_status(order_id)
            return _order_filled_qty(payload)
        except Exception as e:
            logger.debug("get_order_status {} {}: {}", venue, order_id, e)
            return 0.0

    def _safe_cancel(self, venue: Venue, order_id: Optional[str]) -> None:
        if not order_id:
            return
        client = self._client(venue)
        if client is None:
            return
        try:
            client.cancel_order(order_id)
        except Exception as e:
            logger.debug("safe_cancel {} {}: {}", venue, order_id, e)
