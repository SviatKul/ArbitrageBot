"""User authentication — SQLite-backed, Flask-Login managed."""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet
from flask_login import LoginManager, UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

_DB_PATH: Optional[Path] = None


def init_auth(app, storage_dir: Path) -> LoginManager:
    """Call once from create_app(). Sets up LoginManager and DB path."""
    global _DB_PATH
    _DB_PATH = storage_dir / "users.db"
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _init_db()

    lm = LoginManager()
    lm.login_view = "login"
    lm.login_message = "Войдите чтобы продолжить."
    lm.init_app(app)

    @lm.user_loader
    def load_user(user_id: str) -> Optional[User]:
        return User.get_by_id(int(user_id))

    @lm.unauthorized_handler
    def unauthorized():
        from flask import jsonify, redirect, request, url_for
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "unauthorized", "redirect": "/login"}), 401
        return redirect(url_for("login", next=request.url))

    return lm


def _make_fernet(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def _init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL,
                last_login    TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_api_keys (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                key_name    TEXT    NOT NULL,
                enc_value   TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL,
                UNIQUE(user_id, key_name)
            )
        """)
        # Migrations for older schemas
        for col_sql in [
            "ALTER TABLE users ADD COLUMN last_login TEXT",
            "ALTER TABLE users ADD COLUMN totp_secret TEXT",
            "ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                conn.execute(col_sql)
                conn.commit()
            except Exception:
                pass


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def user_count() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) as n FROM users").fetchone()
        return row["n"] if row else 0


class User(UserMixin):
    def __init__(self, row: sqlite3.Row) -> None:
        self.id = row["id"]
        self.username = row["username"]
        self.password_hash = row["password_hash"]
        self.is_admin = bool(row["is_admin"])
        self.created_at = row["created_at"]
        self.last_login    = row["last_login"]    if "last_login"    in row.keys() else None
        self.totp_secret   = row["totp_secret"]   if "totp_secret"   in row.keys() else None
        self.totp_enabled  = bool(row["totp_enabled"]) if "totp_enabled" in row.keys() else False

    # ── Flask-Login ──────────────────────────────────────────────────── #

    def get_id(self) -> str:
        return str(self.id)

    # ── DB helpers ───────────────────────────────────────────────────── #

    @staticmethod
    def get_by_id(user_id: int) -> Optional["User"]:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return User(row) if row else None

    @staticmethod
    def get_by_username(username: str) -> Optional["User"]:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return User(row) if row else None

    @staticmethod
    def create(username: str, password: str, is_admin: bool = False) -> "User":
        ph = generate_password_hash(password)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
                (username.strip(), ph, int(is_admin), ts),
            )
            conn.commit()
        return User.get_by_username(username.strip())

    @staticmethod
    def all_users() -> list["User"]:
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [User(r) for r in rows]

    @staticmethod
    def delete(user_id: int) -> None:
        with _connect() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.commit()

    @staticmethod
    def set_password(user_id: int, new_password: str) -> None:
        ph = generate_password_hash(new_password)
        with _connect() as conn:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (ph, user_id))
            conn.commit()

    @staticmethod
    def record_login(user_id: int) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _connect() as conn:
            conn.execute("UPDATE users SET last_login=? WHERE id=?", (ts, user_id))
            conn.commit()

    @staticmethod
    def set_totp(user_id: int, secret: str, enabled: bool) -> None:
        with _connect() as conn:
            conn.execute(
                "UPDATE users SET totp_secret=?, totp_enabled=? WHERE id=?",
                (secret, int(enabled), user_id),
            )
            conn.commit()

    def verify_totp(self, code: str) -> bool:
        if not self.totp_enabled or not self.totp_secret:
            return True
        try:
            import pyotp
            return pyotp.TOTP(self.totp_secret).verify(str(code).strip(), valid_window=1)
        except Exception:
            return False

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class UserApiKey:
    """Encrypted per-user API key storage."""

    API_KEY_NAMES = [
        "POLYMARKET_API_KEY", "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE",
        "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PEM",
        "BETFAIR_USERNAME", "BETFAIR_PASSWORD", "BETFAIR_APP_KEY",
        "SMARKETS_API_TOKEN",
        "BETDAQ_USERNAME", "BETDAQ_PASSWORD", "BETDAQ_API_KEY",
        "MATCHBOOK_USERNAME", "MATCHBOOK_PASSWORD",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    ]

    @staticmethod
    def set(user_id: int, key_name: str, value: str, app_secret: str) -> None:
        f = _make_fernet(app_secret)
        enc = f.encrypt(value.encode()).decode()
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _connect() as conn:
            conn.execute(
                """INSERT INTO user_api_keys (user_id, key_name, enc_value, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(user_id, key_name) DO UPDATE
                   SET enc_value=excluded.enc_value, updated_at=excluded.updated_at""",
                (user_id, key_name, enc, ts),
            )
            conn.commit()

    @staticmethod
    def delete(user_id: int, key_name: str) -> None:
        with _connect() as conn:
            conn.execute(
                "DELETE FROM user_api_keys WHERE user_id=? AND key_name=?",
                (user_id, key_name),
            )
            conn.commit()

    @staticmethod
    def get_all(user_id: int, app_secret: str) -> dict[str, str]:
        f = _make_fernet(app_secret)
        with _connect() as conn:
            rows = conn.execute(
                "SELECT key_name, enc_value FROM user_api_keys WHERE user_id=?",
                (user_id,),
            ).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            try:
                result[row["key_name"]] = f.decrypt(row["enc_value"].encode()).decode()
            except Exception:
                pass
        return result

    @staticmethod
    def get_names(user_id: int) -> list[str]:
        """Return list of key names set for this user (without decrypting values)."""
        with _connect() as conn:
            rows = conn.execute(
                "SELECT key_name FROM user_api_keys WHERE user_id=?",
                (user_id,),
            ).fetchall()
        return [r["key_name"] for r in rows]
