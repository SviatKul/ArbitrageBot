"""Audit log — записывает все значимые действия пользователей."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DB_PATH: Optional[Path] = None


def init_audit(db_path: Path) -> None:
    global _DB_PATH
    _DB_PATH = db_path
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT    NOT NULL,
                user_id  INTEGER,
                username TEXT,
                action   TEXT    NOT NULL,
                details  TEXT    NOT NULL DEFAULT '',
                ip       TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")
        conn.commit()


def _connect() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("audit not initialised")
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def log(
    action: str,
    *,
    user_id: Optional[int] = None,
    username: str = "system",
    details: str = "",
    ip: Optional[str] = None,
) -> None:
    if _DB_PATH is None:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, user_id, username, action, details, ip) VALUES (?,?,?,?,?,?)",
                (ts, user_id, username, action, details, ip),
            )
            conn.commit()
    except Exception:
        pass


def get_log(limit: int = 200) -> list[dict]:
    if _DB_PATH is None or not _DB_PATH.is_file():
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Action constants ─────────────────────────────────────────────────── #

LOGIN_OK       = "login_ok"
LOGIN_FAIL     = "login_fail"
LOGOUT         = "logout"
PW_CHANGE      = "password_change"
PW_RESET       = "password_reset"
USER_CREATE    = "user_create"
USER_DELETE    = "user_delete"
SETTINGS_SAVE  = "settings_save"
BOT_START      = "bot_start"
BOT_STOP       = "bot_stop"
TOTP_ENABLE    = "2fa_enable"
TOTP_DISABLE   = "2fa_disable"
TOTP_FAIL      = "2fa_fail"
