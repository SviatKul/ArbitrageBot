"""Circuit breaker for halting trading after errors, drawdown, or cooldown recovery."""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from loguru import logger


class CircuitState(str, Enum):
    """Breaker health states."""

    NORMAL = "normal"
    WARNING = "warning"
    BROKEN = "broken"


class CircuitBreaker:
    """
    Tracks consecutive failures, daily PnL, and open position count for risk gating.

    - ``NORMAL``: below warning error threshold.
    - ``WARNING``: at/above warning threshold but below broken threshold (still allows trade by default).
    - ``BROKEN``: tripped by errors, daily loss cap, or post-error lock; ``can_trade()`` is False until cooldown elapses.

    ``total_positions`` is a plain counter — update it from your ``PositionManager`` (e.g. ``breaker.total_positions = len(manager)``).
    """

    def __init__(
        self,
        *,
        warning_error_threshold: int = 3,
        broken_error_threshold: int = 5,
        cooldown_seconds: float = 300.0,
        max_daily_loss: float = 0.0,
        max_daily_loss_pct: float = 0.0,
        bankroll: float = 0.0,
        auto_recover_after_cooldown: bool = True,
    ) -> None:
        if warning_error_threshold < 1 or broken_error_threshold < warning_error_threshold:
            raise ValueError("broken_error_threshold must be >= warning_error_threshold >= 1")
        self._warning_th = int(warning_error_threshold)
        self._broken_th = int(broken_error_threshold)
        self._cooldown_s = float(cooldown_seconds)
        self._max_daily_loss = float(max_daily_loss)
        self._max_daily_loss_pct = float(max_daily_loss_pct)
        self._bankroll = float(bankroll)
        self._auto_recover = bool(auto_recover_after_cooldown)

        self._lock = threading.RLock()
        self.state: CircuitState = CircuitState.NORMAL
        self.consecutive_errors: int = 0
        self.daily_pnl: float = 0.0
        self.total_positions: int = 0
        self._pnl_day: date = datetime.now(timezone.utc).date()
        self._cooldown_until_monotonic: Optional[float] = None

    def _roll_daily_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._pnl_day:
            logger.info(
                "CircuitBreaker: rolling daily PnL ({} -> {}), was {:.4f}",
                self._pnl_day,
                today,
                self.daily_pnl,
            )
            self._pnl_day = today
            self.daily_pnl = 0.0

    def _recompute_state_from_errors(self) -> None:
        if self.state == CircuitState.BROKEN:
            return
        if self.consecutive_errors >= self._broken_th:
            self.state = CircuitState.BROKEN
        elif self.consecutive_errors >= self._warning_th:
            self.state = CircuitState.WARNING
        else:
            self.state = CircuitState.NORMAL

    def _trip_broken(self, reason: str) -> None:
        self.state = CircuitState.BROKEN
        self._cooldown_until_monotonic = time.monotonic() + self._cooldown_s
        logger.error("CircuitBreaker: BROKEN ({}) cooldown={}s", reason, self._cooldown_s)

    def _maybe_expire_cooldown(self) -> None:
        if self.state != CircuitState.BROKEN or self._cooldown_until_monotonic is None:
            return
        if time.monotonic() < self._cooldown_until_monotonic:
            return
        if not self._auto_recover:
            return
        logger.warning(
            "CircuitBreaker: cooldown elapsed -> NORMAL (was BROKEN, consecutive_errors reset)"
        )
        self.state = CircuitState.NORMAL
        self.consecutive_errors = 0
        self._cooldown_until_monotonic = None
        self._recompute_state_from_errors()

    def record_error(self) -> None:
        """Increment consecutive errors; may move to WARNING or BROKEN and start cooldown."""
        with self._lock:
            self._roll_daily_if_needed()
            self.consecutive_errors += 1
            if self.consecutive_errors >= self._broken_th:
                if self.state != CircuitState.BROKEN:
                    self._trip_broken(f"errors>={self._broken_th}")
                return
            self._recompute_state_from_errors()

    def update_bankroll(self, bankroll: float) -> None:
        """Обновить текущий банкролл для расчёта процентного лимита убытков."""
        with self._lock:
            self._bankroll = max(0.0, float(bankroll))

    def record_pnl(self, delta: float) -> None:
        """Добавляет реализованный PnL; срабатывает стоп при превышении дневного лимита."""
        with self._lock:
            self._roll_daily_if_needed()
            self.daily_pnl += float(delta)

            # Лимит в USD
            if (
                self._max_daily_loss > 0.0
                and self.daily_pnl <= -self._max_daily_loss
                and self.state != CircuitState.BROKEN
            ):
                self._trip_broken(
                    f"daily_pnl {self.daily_pnl:.2f} <= -{self._max_daily_loss:.2f} USD"
                )
                return

            # Лимит в % от банкролла
            if (
                self._max_daily_loss_pct > 0.0
                and self._bankroll > 0.0
                and self.state != CircuitState.BROKEN
            ):
                loss_pct = (-self.daily_pnl / self._bankroll) * 100.0
                if loss_pct >= self._max_daily_loss_pct:
                    self._trip_broken(
                        f"daily loss {loss_pct:.1f}% >= limit {self._max_daily_loss_pct:.0f}%"
                        f" (bankroll={self._bankroll:.2f})"
                    )

    def record_success(self) -> None:
        """Clear consecutive error streak after a successful execution or healthy trade."""
        with self._lock:
            self._roll_daily_if_needed()
            if self.state == CircuitState.BROKEN:
                return
            self.consecutive_errors = 0
            self._recompute_state_from_errors()

    def can_trade(self) -> bool:
        """
        Return whether new trades are allowed.

        Lazily clears BROKEN after the monotonic cooldown when ``auto_recover_after_cooldown`` is True.
        WARNING still allows trading; only BROKEN blocks.
        """
        with self._lock:
            self._roll_daily_if_needed()
            self._maybe_expire_cooldown()
            return self.state != CircuitState.BROKEN

    def reset(self) -> None:
        """Manual reset: NORMAL state, zero errors, zero daily PnL, zero positions counter, clear cooldown."""
        with self._lock:
            self.state = CircuitState.NORMAL
            self.consecutive_errors = 0
            self.daily_pnl = 0.0
            self.total_positions = 0
            self._cooldown_until_monotonic = None
            self._pnl_day = datetime.now(timezone.utc).date()
            logger.info("CircuitBreaker: manual reset()")
