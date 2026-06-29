"""Per-user bot process manager for SaaS mode."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
BOT_SCRIPT = ROOT / "run.py"


def _find_python() -> Path:
    import sys
    candidates = [
        ROOT / ".venv" / "bin" / "python",
        ROOT / ".venv" / "bin" / "python3",
        Path.home() / "PycharmProjects" / "spred" / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]
    for p in candidates:
        if p.exists():
            return p
    return Path("python3")


PYTHON = _find_python()


class _UserBot:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    @property
    def data_dir(self) -> Path:
        return ROOT / "data" / f"user_{self.user_id}"

    @property
    def log_dir(self) -> Path:
        return ROOT / "logs" / f"user_{self.user_id}"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "bot.log"

    def start(self, user_env: dict[str, str]) -> tuple[bool, str]:
        with self._lock:
            if self._process and self._process.poll() is None:
                return False, "Уже запущен"
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_fh = open(self.log_file, "a")
            env = os.environ.copy()
            env.update(user_env)
            env["ARBITRAGE_DATA_DIR"] = str(self.data_dir)
            env["ARBITRAGE_LOG_DIR"] = str(self.log_dir)
            env["ARBITRAGE_USER_ID"] = str(self.user_id)
            env["TG_POLLING_DISABLED"] = "1"
            self._process = subprocess.Popen(
                [str(PYTHON), str(BOT_SCRIPT)],
                cwd=str(ROOT),
                stdout=log_fh,
                stderr=log_fh,
                env=env,
            )
            return True, f"Запущен (PID {self._process.pid})"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if not self._process or self._process.poll() is not None:
                return False, "Не запущен"
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            return True, "Остановлен"

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        with self._lock:
            return self._process.pid if self._process and self._process.poll() is None else None

    def tail_log(self, lines: int = 200) -> list[str]:
        if not self.log_file.is_file():
            return []
        try:
            text = self.log_file.read_text(encoding="utf-8", errors="replace")
            return text.splitlines()[-lines:]
        except OSError:
            return []

    def stream_log(self):
        if not self.log_file.is_file():
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self.log_file.touch()
        import time
        with open(self.log_file, encoding="utf-8", errors="replace") as fh:
            fh.seek(0, 2)
            while True:
                line = fh.readline()
                if line:
                    yield line.rstrip()
                else:
                    time.sleep(0.3)


class MultiBotManager:
    """Manages one bot subprocess per user."""

    def __init__(self) -> None:
        self._bots: dict[int, _UserBot] = {}
        self._lock = threading.Lock()

    def _get(self, user_id: int) -> _UserBot:
        with self._lock:
            if user_id not in self._bots:
                self._bots[user_id] = _UserBot(user_id)
            return self._bots[user_id]

    def start(self, user_id: int, user_env: dict[str, str]) -> tuple[bool, str]:
        return self._get(user_id).start(user_env)

    def stop(self, user_id: int) -> tuple[bool, str]:
        return self._get(user_id).stop()

    def is_running(self, user_id: int) -> bool:
        return self._get(user_id).is_running

    def pid(self, user_id: int) -> Optional[int]:
        return self._get(user_id).pid

    def tail_log(self, user_id: int, lines: int = 200) -> list[str]:
        return self._get(user_id).tail_log(lines)

    def stream_log(self, user_id: int):
        return self._get(user_id).stream_log()

    def data_dir(self, user_id: int) -> Path:
        return self._get(user_id).data_dir

    def all_running(self) -> list[dict]:
        with self._lock:
            return [
                {"user_id": uid, "pid": b.pid, "running": b.is_running}
                for uid, b in self._bots.items()
                if b.is_running
            ]
