"""
Telegram Bot notifications for the arbitrage bot.

Sends alerts to a single chat for:
  - Arbitrage opportunity found
  - Trade executed (both legs)
  - Trade failed / partial fill hedged
  - Circuit breaker triggered
  - Daily P&L digest
  - Open positions on startup (crash recovery alert)

Uses the Bot API over HTTPS (httpx) — no extra library needed.
Messages are sent in a background daemon thread so alerts never block
the main trading loop.

Setup:
  1. Create bot: BotFather → /newbot → copy token to TELEGRAM_BOT_TOKEN
  2. Get your chat_id: send any message to the bot, then run:
       curl https://api.telegram.org/bot<TOKEN>/getUpdates
     Look for "chat": {"id": 123456789}
  3. Add to .env:
       TELEGRAM_BOT_TOKEN=...
       TELEGRAM_CHAT_ID=123456789
"""

from __future__ import annotations

import queue
import threading
from typing import Optional

import httpx
from loguru import logger


class TelegramNotifier:
    """
    Non-blocking Telegram alert sender.

    Messages are queued and sent by a background thread so network
    latency never touches the main loop.
    """

    _API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self,
        *,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: float = 10.0,
        max_queue: int = 100,
    ) -> None:
        self._token = (bot_token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._timeout = timeout
        self._enabled = bool(self._token and self._chat_id)
        self._queue: queue.Queue[Optional[str]] = queue.Queue(maxsize=max_queue)

        if self._enabled:
            t = threading.Thread(target=self._worker, name="TelegramSender", daemon=True)
            t.start()
            logger.info("Telegram notifications enabled (chat_id={})", self._chat_id)
        else:
            logger.info("Telegram notifications disabled (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def send(self, text: str) -> None:
        """Enqueue a plain-text message (non-blocking)."""
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            logger.debug("Telegram queue full — message dropped")

    def opportunity(self, title: str, yes_venue: str, no_venue: str, profit_pct: float) -> None:
        self.send(
            f"🎯 *Арбитраж найден*\n"
            f"`{title[:60]}`\n"
            f"YES @ {yes_venue}  +  NO @ {no_venue}\n"
            f"Прибыль: *{profit_pct:.2f}%*"
        )

    def trade_executed(
        self,
        title: str,
        yes_venue: str,
        no_venue: str,
        cost: float,
        profit_pct: float,
    ) -> None:
        self.send(
            f"✅ *Сделка исполнена*\n"
            f"`{title[:60]}`\n"
            f"YES @ {yes_venue}  +  NO @ {no_venue}\n"
            f"Затраты: ${cost:.2f}  |  Прибыль: {profit_pct:.2f}%"
        )

    def trade_failed(self, title: str, reason: str) -> None:
        self.send(
            f"❌ *Ошибка исполнения*\n"
            f"`{title[:60]}`\n"
            f"Причина: {reason}"
        )

    def hedge_placed(self, title: str, side: str, venue: str, qty: float) -> None:
        self.send(
            f"🛡 *Хедж размещён*\n"
            f"`{title[:60]}`\n"
            f"Обратный ордер: {side} × {qty:.2f} на {venue}"
        )

    def circuit_breaker(self, reason: str, daily_pnl: float) -> None:
        self.send(
            f"🔴 *Circuit Breaker сработал*\n"
            f"Причина: {reason}\n"
            f"Дневной P&L: ${daily_pnl:.2f}"
        )

    def daily_report(self, daily_pnl: float, trades: int, open_count: int) -> None:
        sign = "+" if daily_pnl >= 0 else ""
        self.send(
            f"📊 *Дневной отчёт*\n"
            f"P&L: {sign}${daily_pnl:.2f}\n"
            f"Сделок за день: {trades}\n"
            f"Открытых позиций: {open_count}"
        )

    def startup_positions(self, positions: list[dict]) -> None:
        """Alert about positions found in snapshot on bot restart."""
        if not positions:
            return
        lines = [f"⚠️ *Позиции найдены после перезапуска* ({len(positions)} шт.)"]
        for p in positions[:5]:
            lines.append(
                f"  • {p.get('side')} {p.get('contracts'):.1f} @ "
                f"{p.get('venue')} | {str(p.get('market_id'))[:20]}"
            )
        if len(positions) > 5:
            lines.append(f"  ... и ещё {len(positions) - 5}")
        lines.append("Проверь и закрой вручную если нужно.")
        self.send("\n".join(lines))

    def info(self, text: str) -> None:
        self.send(f"ℹ️ {text}")

    def start_command_polling(self, callbacks: "dict[str, callable]") -> None:
        """
        Запускает фоновый поллинг команд от пользователя.

        callbacks = {
            "/status":    lambda: "строка статуса",
            "/stop":      callable,
            "/positions": callable,
            "/pnl":       callable,
        }
        """
        if not self._enabled:
            return
        t = threading.Thread(
            target=self._poll_commands,
            args=(callbacks,),
            name="TelegramCmdPoll",
            daemon=True,
        )
        t.start()
        logger.info("Telegram command polling started")

    def shutdown(self, timeout: float = 5.0) -> None:
        """Flush remaining messages before process exit."""
        if not self._enabled:
            return
        self._queue.put(None)
        import time
        time.sleep(min(timeout, 2.0))

    # ------------------------------------------------------------------ #
    # Workers
    # ------------------------------------------------------------------ #

    def _worker(self) -> None:
        url = self._API.format(token=self._token)
        with httpx.Client(timeout=self._timeout) as client:
            while True:
                msg = self._queue.get()
                if msg is None:
                    break
                try:
                    resp = client.post(url, json={
                        "chat_id": self._chat_id,
                        "text": msg,
                        "parse_mode": "Markdown",
                    })
                    if resp.status_code != 200:
                        logger.debug("Telegram error {}: {}", resp.status_code, resp.text[:200])
                except Exception as e:
                    logger.debug("Telegram send failed: {}", e)
                finally:
                    self._queue.task_done()

    def _poll_commands(self, callbacks: "dict[str, callable]") -> None:
        """Long-poll /getUpdates and dispatch commands to callbacks."""
        import time
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        offset = 0
        help_text = (
            "*Команды бота:*\n"
            "/status — статус и биржи\n"
            "/positions — открытые позиции\n"
            "/pnl — дневной P&L\n"
            "/stop — остановить бот\n"
            "/help — эта справка"
        )

        with httpx.Client(timeout=35.0) as client:
            while True:
                try:
                    resp = client.get(url, params={"offset": offset, "timeout": 30,
                                                    "allowed_updates": ["message"]})
                    updates = resp.json().get("result", [])
                except Exception:
                    time.sleep(5)
                    continue

                for upd in updates:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message", {})
                    chat_id = str((msg.get("chat") or {}).get("id", ""))
                    text = (msg.get("text") or "").strip().lower()

                    if chat_id != self._chat_id:
                        continue

                    if text in ("/help", "/start"):
                        self.send(help_text)
                    elif text in callbacks:
                        try:
                            result = callbacks[text]()
                            if result:
                                self.send(str(result))
                        except Exception as e:
                            self.send(f"❌ Ошибка: {e}")
                    elif text.startswith("/"):
                        self.send(f"Неизвестная команда: `{text}`\n/help — список команд")
