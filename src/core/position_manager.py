"""In-memory open position tracking with exposure and unrealized PnL."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from loguru import logger

from models.types import Venue


@dataclass
class OpenPosition:
    """One open leg (YES/NO) on a venue."""

    position_id: str
    venue: Venue
    market_id: str
    side: str
    contracts: float
    entry_price: float
    title: str = ""
    token_id: Optional[str] = None
    mark_price: Optional[float] = None
    opened_at_utc: str = ""

    def copy(self) -> "OpenPosition":
        return OpenPosition(
            position_id=self.position_id,
            venue=self.venue,
            market_id=self.market_id,
            side=self.side.upper(),
            contracts=self.contracts,
            entry_price=self.entry_price,
            title=self.title,
            token_id=self.token_id,
            mark_price=self.mark_price,
            opened_at_utc=self.opened_at_utc,
        )


class PositionManager:
    """
    Tracks open positions, exposure by venue/market, and unrealized PnL vs last mark prices.

    Prices are implied probabilities in ``[0, 1]`` per contract side (YES or NO token).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._positions: Dict[str, OpenPosition] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._positions)

    def add_position(
        self,
        *,
        venue: Venue,
        market_id: str,
        side: str,
        contracts: float,
        entry_price: float,
        title: str = "",
        token_id: Optional[str] = None,
        position_id: Optional[str] = None,
        opened_at_utc: Optional[str] = None,
    ) -> str:
        """Open a new position; returns ``position_id``."""
        if contracts <= 0:
            raise ValueError("contracts must be positive")
        if not (0.0 <= entry_price <= 1.0):
            raise ValueError("entry_price must be in [0, 1] for implied-prob markets")
        side_u = side.upper()
        if side_u not in {"YES", "NO"}:
            raise ValueError("side must be YES or NO")

        pid = position_id or uuid.uuid4().hex
        ts = opened_at_utc or datetime.now(timezone.utc).isoformat()
        row = OpenPosition(
            position_id=pid,
            venue=venue,
            market_id=str(market_id),
            side=side_u,
            contracts=float(contracts),
            entry_price=float(entry_price),
            title=title,
            token_id=token_id,
            mark_price=None,
            opened_at_utc=ts,
        )
        with self._lock:
            if pid in self._positions:
                raise ValueError(f"position_id already exists: {pid}")
            self._positions[pid] = row
        return pid

    def close_position(
        self,
        position_id: str,
        *,
        contracts: Optional[float] = None,
    ) -> float:
        """
        Close fully (default) or partially.

        Returns the number of contracts removed from the book.
        """
        with self._lock:
            pos = self._positions.get(position_id)
            if pos is None:
                raise KeyError(f"unknown position_id: {position_id}")
            close_sz = float(contracts) if contracts is not None else pos.contracts
            if close_sz <= 0 or close_sz > pos.contracts:
                raise ValueError("invalid close size")
            remaining = pos.contracts - close_sz
            if remaining <= 1e-12:
                del self._positions[position_id]
            else:
                pos.contracts = remaining
            return close_sz

    def update_mark(self, position_id: str, mark_price: float) -> None:
        """Set mark-to-market price for one position (same units as entry, 0–1)."""
        if not (0.0 <= mark_price <= 1.0):
            raise ValueError("mark_price must be in [0, 1]")
        with self._lock:
            pos = self._positions.get(position_id)
            if pos is None:
                raise KeyError(f"unknown position_id: {position_id}")
            pos.mark_price = float(mark_price)

    def update_marks(self, marks: Mapping[str, float]) -> None:
        """Bulk ``{position_id: mark_price}``."""
        for pid, px in marks.items():
            self.update_mark(str(pid), float(px))

    def get_position(self, position_id: str) -> Optional[OpenPosition]:
        with self._lock:
            p = self._positions.get(position_id)
            return p.copy() if p else None

    def list_open_positions(self) -> List[OpenPosition]:
        with self._lock:
            return [p.copy() for p in self._positions.values()]

    def get_exposure(self) -> Dict[str, Any]:
        """
        Aggregate exposure.

        Returns keys:
        - ``by_venue``: contracts and entry notional per ``Venue``
        - ``by_market``: nested ``venue -> market_id -> {side: contracts, entry_notional}``
        - ``positions``: snapshot list of dicts
        """
        with self._lock:
            by_venue: Dict[str, Dict[str, float]] = {}
            by_market: Dict[str, Dict[str, Dict[str, Any]]] = {}
            snaps: List[Dict[str, Any]] = []

            for p in self._positions.values():
                vk = p.venue.value
                by_venue.setdefault(vk, {"contracts": 0.0, "entry_notional": 0.0})
                by_venue[vk]["contracts"] += p.contracts
                by_venue[vk]["entry_notional"] += p.contracts * p.entry_price

                by_market.setdefault(vk, {}).setdefault(
                    p.market_id,
                    {"YES": {"contracts": 0.0, "entry_notional": 0.0}, "NO": {"contracts": 0.0, "entry_notional": 0.0}},
                )
                bucket = by_market[vk][p.market_id][p.side]
                bucket["contracts"] += p.contracts
                bucket["entry_notional"] += p.contracts * p.entry_price

                snaps.append(
                    {
                        "position_id": p.position_id,
                        "venue": p.venue.value,
                        "market_id": p.market_id,
                        "side": p.side,
                        "contracts": p.contracts,
                        "entry_price": p.entry_price,
                        "mark_price": p.mark_price,
                        "title": p.title,
                        "token_id": p.token_id,
                        "opened_at_utc": p.opened_at_utc,
                    }
                )

            return {
                "by_venue": by_venue,
                "by_market": by_market,
                "positions": snaps,
                "open_count": len(self._positions),
            }

    def unrealized_pnl(self) -> float:
        """
        Sum of ``(mark - entry) * contracts`` for positions with a mark; positions without mark contribute ``0``.
        """
        total = 0.0
        with self._lock:
            for p in self._positions.values():
                if p.mark_price is None:
                    continue
                total += (p.mark_price - p.entry_price) * p.contracts
        return total

    def update_pnl(self) -> Dict[str, Any]:
        """
        Refresh accounting snapshot (marks unchanged unless you call ``update_mark`` elsewhere).

        Logs unrealized PnL and open count for monitoring.
        """
        exp = self.get_exposure()
        u = self.unrealized_pnl()
        logger.info(
            "PositionManager update_pnl: open_count={} unrealized_pnl={:.6f}",
            exp["open_count"],
            u,
        )
        return {"unrealized_pnl": u, "open_count": exp["open_count"], "exposure": exp}

    def save_snapshot(self, path: Path) -> None:
        """Persist ``get_exposure()`` payload plus timestamp for graceful shutdown."""
        path.parent.mkdir(parents=True, exist_ok=True)
        exp = self.get_exposure()
        payload = {"saved_at_utc": datetime.now(timezone.utc).isoformat(), **exp}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved positions snapshot to {}", path)

    def find_convergent(
        self,
        current_quotes: "dict[tuple[str,str], dict]",
        convergence_threshold: float = 0.985,
    ) -> list[OpenPosition]:
        """
        Возвращает позиции у которых рынок сошёлся к разрешению.

        Рынок считается разрешённым когда YES_ask >= threshold (≈$1).
        current_quotes: {(venue_value, market_id): {"yes_best_ask": ..., "no_best_ask": ...}}
        """
        result = []
        with self._lock:
            for pos in self._positions.values():
                key = (pos.venue.value, pos.market_id)
                q = current_quotes.get(key)
                if q is None:
                    continue
                yes_ask = q.get("yes_best_ask", 0.0)
                no_ask  = q.get("no_best_ask",  0.0)
                # If YES resolved to 1 or NO resolved to 1, the other side collapses
                if yes_ask >= convergence_threshold or no_ask >= convergence_threshold:
                    result.append(pos.copy())
        return result

    def load_snapshot(self, path: Path) -> list[dict]:
        """
        Restore positions from the last snapshot written by ``save_snapshot()``.

        Returns the list of position dicts that were loaded (for alerting).
        Silently skips malformed records. Existing in-memory positions are not cleared.
        """
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to read positions snapshot {}: {}", path, e)
            return []

        positions = payload.get("positions") or []
        loaded = []
        for p in positions:
            try:
                self.add_position(
                    venue=Venue(p["venue"]),
                    market_id=str(p["market_id"]),
                    side=str(p["side"]),
                    contracts=float(p["contracts"]),
                    entry_price=float(p["entry_price"]),
                    title=str(p.get("title", "")),
                    token_id=p.get("token_id"),
                    position_id=str(p["position_id"]),
                    opened_at_utc=p.get("opened_at_utc"),
                )
                loaded.append(p)
            except Exception as e:
                logger.warning("Skipping invalid position record {}: {}", p.get("position_id"), e)

        if loaded:
            saved_at = payload.get("saved_at_utc", "unknown")
            logger.warning(
                "Crash recovery: loaded {} open position(s) from snapshot (saved {}). "
                "Review and close manually if orders are not filled on exchanges.",
                len(loaded), saved_at,
            )
        return loaded
