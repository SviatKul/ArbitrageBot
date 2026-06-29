"""Bot process lifecycle and log management."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]  # src/web/bot_manager.py → project root
LOG_FILE = ROOT / "logs" / "bot.log"
BOT_SCRIPT = ROOT / "run.py"

# Find python: project .venv → common locations → sys.executable → system python3
def _find_python() -> Path:
    import sys as _sys
    candidates = [
        ROOT / ".venv" / "bin" / "python",
        ROOT / ".venv" / "bin" / "python3",
        Path.home() / "PycharmProjects" / "spred" / ".venv" / "bin" / "python",
        Path(_sys.executable),  # same python that runs this web server
    ]
    for p in candidates:
        if p.exists():
            return p
    return Path("python3")

PYTHON = _find_python()


class BotManager:
    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._process and self._process.poll() is None:
                return False, "Уже запущен"
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(LOG_FILE, "a")
            py = str(PYTHON)
            import os as _os
            env = _os.environ.copy()
            env["TG_POLLING_DISABLED"] = "1"  # web dashboard owns the polling
            self._process = subprocess.Popen(
                [py, str(BOT_SCRIPT)],
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
        if not LOG_FILE.is_file():
            return []
        try:
            text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
            return text.splitlines()[-lines:]
        except OSError:
            return []

    def stream_log(self):
        """Generator: yield new log lines as they appear (SSE)."""
        if not LOG_FILE.is_file():
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOG_FILE.touch()
        import time
        with open(LOG_FILE, encoding="utf-8", errors="replace") as fh:
            fh.seek(0, 2)  # seek to end
            while True:
                line = fh.readline()
                if line:
                    yield line.rstrip()
                else:
                    time.sleep(0.3)
