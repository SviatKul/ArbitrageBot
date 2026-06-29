"""
CSV-лог всех найденных возможностей и исполненных сделок.

Пишет в data/trades.csv — открывается в Excel/Numbers.
Каждая строка = одна возможность (статус: found / executed / failed / skipped).
"""

from __future__ import annotations

import csv
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_COLUMNS = [
    "timestamp_utc",
    "market",
    "yes_venue",
    "no_venue",
    "yes_price",
    "no_price",
    "spread_pct",
    "cost_usd",
    "status",       # found | executed | failed | hedged
    "notes",
]


class TradeLogger:
    """Thread-safe CSV trade log."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            with open(self._path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(_COLUMNS)

    def _write(self, row: dict) -> None:
        with self._lock:
            with open(self._path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=_COLUMNS, extrasaction="ignore")
                w.writerow(row)

    def log_opportunity(
        self,
        *,
        title: str,
        yes_venue: str,
        no_venue: str,
        yes_price: float,
        no_price: float,
        profit_pct: float,
        status: str = "found",
        notes: str = "",
        cost_usd: float = 0.0,
    ) -> None:
        self._write({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "market":    title[:80],
            "yes_venue": yes_venue,
            "no_venue":  no_venue,
            "yes_price": f"{yes_price:.4f}",
            "no_price":  f"{no_price:.4f}",
            "spread_pct": f"{profit_pct:.2f}",
            "cost_usd":  f"{cost_usd:.2f}",
            "status":    status,
            "notes":     notes,
        })

    def log_execution(
        self,
        *,
        title: str,
        yes_venue: str,
        no_venue: str,
        yes_price: float,
        no_price: float,
        profit_pct: float,
        cost_usd: float,
        notes: str = "",
    ) -> None:
        self.log_opportunity(
            title=title,
            yes_venue=yes_venue,
            no_venue=no_venue,
            yes_price=yes_price,
            no_price=no_price,
            profit_pct=profit_pct,
            cost_usd=cost_usd,
            status="executed",
            notes=notes,
        )

    def recent(self, n: int = 50) -> list[dict]:
        """Return last n rows as list of dicts (for dashboard)."""
        try:
            with open(self._path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            return rows[-n:]
        except Exception:
            return []

    def summary(self) -> dict:
        """Aggregate stats: total, executed, avg profit."""
        try:
            with open(self._path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            return {"total": 0, "executed": 0, "avg_spread_pct": 0.0}

        total = len(rows)
        executed = [r for r in rows if r.get("status") == "executed"]
        spreads = []
        for r in rows:
            try:
                spreads.append(float(r["spread_pct"]))
            except (KeyError, ValueError):
                pass
        return {
            "total": total,
            "executed": len(executed),
            "avg_spread_pct": sum(spreads) / len(spreads) if spreads else 0.0,
            "max_spread_pct": max(spreads) if spreads else 0.0,
        }
