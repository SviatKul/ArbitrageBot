"""
Дневная история P&L — хранится в data/pnl_history.json.
Используется для графика на дашборде.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class PnLTracker:
    """Thread-safe daily P&L tracker.  {date_str: cumulative_delta_usd}"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._history: dict[str, float] = self._load()

    def _load(self) -> dict[str, float]:
        if self._path.is_file():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._history, indent=2), encoding="utf-8")

    def record(self, delta: float) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            self._history[today] = round(self._history.get(today, 0.0) + delta, 4)
            self._save()

    def today(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            return self._history.get(today, 0.0)

    def history(self, days: int = 30) -> list[dict]:
        """Last N days as [{date, pnl}] sorted ascending."""
        with self._lock:
            items = sorted(self._history.items())[-days:]
        return [{"date": d, "pnl": v} for d, v in items]
