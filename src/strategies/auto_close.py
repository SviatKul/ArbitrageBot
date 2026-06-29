"""
Auto-close rules for open legs: spread convergence, take-profit (fixed + trailing vs peak),
and per-contract stop-loss when price moves against the position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from loguru import logger

from config.settings import Settings
from core.position_manager import OpenPosition, PositionManager


def spread_converged(yes_mid: float, no_mid: float, *, epsilon: float) -> bool:
    """Return True when complementary mids sum to ~1 (tight box)."""
    return float(yes_mid) + float(no_mid) >= 1.0 - float(epsilon)


@dataclass
class AutoCloseResult:
    """Summary of a single ``evaluate`` pass."""

    closed_position_ids: list[str]
    reasons: dict[str, str]


class AutoCloseStrategy:
    """
    Stateful trailing peak tracker (in-memory) per ``position_id``.

    Rules (in order):
    1. **Paired spread** — if ``paired`` mids converge, close all ``paired_position_ids``.
    2. **Stop-loss** — per-contract adverse move ``entry - mark`` (long YES/NO token) exceeds threshold.
    3. **Take-profit (theoretical)** — unrealized ≥ ``tp_frac_theory`` × max move to 1 per bundle.
    4. **Take-profit (trailing vs peak)** — after peak unrealized ≥ ``min_peak_pnl``, close if
       unrealized < ``tp_frac_peak`` × peak (give-back exit).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._peak_unrealized: dict[str, float] = {}

    def evaluate(
        self,
        position_manager: PositionManager,
        *,
        mid_by_position_id: Mapping[str, float],
        paired: Optional[tuple[Sequence[str], float, float]] = None,
    ) -> AutoCloseResult:
        """
        ``mid_by_position_id`` maps ``OpenPosition.position_id`` → mark in ``[0,1]``.

        ``paired`` may be ``(position_ids, yes_mid, no_mid)`` to trigger coordinated spread exits.
        """
        closed: list[str] = []
        reasons: dict[str, str] = {}

        if paired is not None:
            ids, y_mid, n_mid = paired
            if spread_converged(y_mid, n_mid, epsilon=float(self._settings.auto_close_spread_convergence_epsilon)):
                for pid in ids:
                    if pid in reasons:
                        continue
                    self._close_one(position_manager, str(pid), closed, reasons, "spread_convergence")

        for pos in position_manager.list_open_positions():
            pid = pos.position_id
            if pid in reasons:
                continue
            mark = mid_by_position_id.get(pid)
            if mark is None:
                mark = pos.mark_price
            if mark is None:
                continue

            unreal = self._unrealized_total(pos, float(mark))
            self._peak_unrealized[pid] = max(self._peak_unrealized.get(pid, unreal), unreal)

            if self._stop_loss_hit(pos, float(mark)):
                self._close_one(position_manager, pid, closed, reasons, "stop_loss")
                continue

            if self._take_profit_theoretical(pos, unreal):
                self._close_one(position_manager, pid, closed, reasons, "take_profit_theoretical")
                continue

            peak = self._peak_unrealized.get(pid, unreal)
            if peak >= float(self._settings.auto_close_min_peak_pnl):
                frac = float(self._settings.auto_close_take_profit_fraction_of_peak)
                if unreal < frac * peak:
                    self._close_one(position_manager, pid, closed, reasons, "take_profit_trailing_peak")

        return AutoCloseResult(closed_position_ids=closed, reasons=reasons)

    @staticmethod
    def _unrealized_total(pos: OpenPosition, mark: float) -> float:
        return (mark - float(pos.entry_price)) * float(pos.contracts)

    def _stop_loss_hit(self, pos: OpenPosition, mark: float) -> bool:
        """Long token: lose when mark falls below entry."""
        adverse = max(0.0, float(pos.entry_price) - float(mark))
        return adverse >= float(self._settings.auto_close_stop_loss_per_contract)

    def _take_profit_theoretical(self, pos: OpenPosition, unreal: float) -> bool:
        """Capture a fraction of the remaining path to probability 1."""
        frac = float(self._settings.auto_close_take_profit_fraction_of_theoretical)
        max_bundle = float(pos.contracts) * max(0.0, 1.0 - float(pos.entry_price))
        if max_bundle <= 0:
            return False
        return unreal >= frac * max_bundle

    def _close_one(
        self,
        pm: PositionManager,
        pid: str,
        closed: list[str],
        reasons: dict[str, str],
        reason: str,
    ) -> None:
        try:
            pm.close_position(pid)
            closed.append(pid)
            reasons[pid] = reason
            self._peak_unrealized.pop(pid, None)
        except Exception as e:
            logger.warning("Auto-close failed for {}: {}", pid, e)
