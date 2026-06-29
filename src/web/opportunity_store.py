"""SQLite-хранилище всех найденных арбитражных возможностей."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DB_PATH: Optional[Path] = None


def init_opportunity_store(data_dir: Path) -> None:
    global _DB_PATH
    _DB_PATH = data_dir / "opportunities.db"
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT    NOT NULL,
                date       TEXT    NOT NULL,
                title      TEXT    NOT NULL,
                yes_venue  TEXT    NOT NULL,
                no_venue   TEXT    NOT NULL,
                yes_price  REAL,
                no_price   REAL,
                profit_pct REAL,
                max_size   REAL,
                executed   INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_opp_ts   ON opportunities(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_opp_date ON opportunities(date)")
        conn.commit()


def _connect() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("opportunity_store not initialised")
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def log_opportunity(
    *,
    title: str,
    yes_venue: str,
    no_venue: str,
    yes_price: float,
    no_price: float,
    profit_pct: float,
    max_size: float = 0.0,
    executed: bool = False,
) -> None:
    if _DB_PATH is None:
        return
    now = datetime.now(timezone.utc)
    try:
        with _connect() as conn:
            conn.execute("""
                INSERT INTO opportunities
                    (ts, date, title, yes_venue, no_venue, yes_price, no_price, profit_pct, max_size, executed)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                now.isoformat(timespec="seconds"),
                now.strftime("%Y-%m-%d"),
                title, yes_venue, no_venue,
                yes_price, no_price, profit_pct, max_size,
                int(executed),
            ))
            conn.commit()
    except Exception:
        pass


def mark_executed(title: str, yes_venue: str, no_venue: str) -> None:
    """Помечает последнюю запись с данной парой как исполненную."""
    if _DB_PATH is None:
        return
    try:
        with _connect() as conn:
            conn.execute("""
                UPDATE opportunities SET executed=1
                WHERE id = (
                    SELECT id FROM opportunities
                    WHERE title=? AND yes_venue=? AND no_venue=? AND executed=0
                    ORDER BY id DESC LIMIT 1
                )
            """, (title, yes_venue, no_venue))
            conn.commit()
    except Exception:
        pass


def get_opportunities(
    limit: int = 200,
    min_pct: float = 0.0,
    date: Optional[str] = None,
) -> list[dict]:
    if _DB_PATH is None or not _DB_PATH.is_file():
        return []
    query = "SELECT * FROM opportunities WHERE profit_pct >= ?"
    params: list = [min_pct]
    if date:
        query += " AND date = ?"
        params.append(date)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    if _DB_PATH is None or not _DB_PATH.is_file():
        return {"total": 0, "executed": 0, "missed": 0, "avg_pct": 0.0, "today": 0, "best_pct": 0.0}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _connect() as conn:
        total    = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        executed = conn.execute("SELECT COUNT(*) FROM opportunities WHERE executed=1").fetchone()[0]
        avg_pct  = conn.execute("SELECT AVG(profit_pct) FROM opportunities WHERE profit_pct>0").fetchone()[0]
        best_pct = conn.execute("SELECT MAX(profit_pct) FROM opportunities").fetchone()[0]
        today_n  = conn.execute("SELECT COUNT(*) FROM opportunities WHERE date=?", (today,)).fetchone()[0]
    return {
        "total":    total,
        "executed": executed,
        "missed":   total - executed,
        "avg_pct":  round(avg_pct or 0.0, 3),
        "best_pct": round(best_pct or 0.0, 3),
        "today":    today_n,
    }


def get_daily_stats(days: int = 30) -> list[dict]:
    if _DB_PATH is None or not _DB_PATH.is_file():
        return []
    with _connect() as conn:
        rows = conn.execute("""
            SELECT date,
                   COUNT(*) AS total,
                   SUM(executed) AS executed,
                   AVG(profit_pct) AS avg_pct,
                   MAX(profit_pct) AS best_pct
            FROM opportunities
            GROUP BY date
            ORDER BY date DESC
            LIMIT ?
        """, (days,)).fetchall()
    return [dict(r) for r in rows]


def get_best_pairs(limit: int = 20) -> list[dict]:
    """Лучшие пары рынков по среднему спреду."""
    if _DB_PATH is None or not _DB_PATH.is_file():
        return []
    with _connect() as conn:
        rows = conn.execute("""
            SELECT yes_venue, no_venue, title,
                   COUNT(*)         AS count,
                   AVG(profit_pct)  AS avg_pct,
                   MAX(profit_pct)  AS max_pct,
                   SUM(executed)    AS executed
            FROM opportunities
            GROUP BY yes_venue, no_venue, title
            ORDER BY avg_pct DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_hourly_heatmap() -> list[dict]:
    """Активность по часу UTC и дню недели (0=пн … 6=вс)."""
    if _DB_PATH is None or not _DB_PATH.is_file():
        return []
    with _connect() as conn:
        rows = conn.execute("""
            SELECT CAST(substr(ts, 12, 2) AS INTEGER) AS hour,
                   CAST(strftime('%w', date) AS INTEGER) AS weekday,
                   COUNT(*)        AS count,
                   AVG(profit_pct) AS avg_pct
            FROM opportunities
            GROUP BY hour, weekday
        """).fetchall()
    return [dict(r) for r in rows]


def get_spread_distribution() -> list[dict]:
    """Распределение спредов по бакетам."""
    if _DB_PATH is None or not _DB_PATH.is_file():
        return []
    with _connect() as conn:
        rows = conn.execute("""
            SELECT CASE
                WHEN profit_pct < 1   THEN '0–1%'
                WHEN profit_pct < 2   THEN '1–2%'
                WHEN profit_pct < 3   THEN '2–3%'
                WHEN profit_pct < 5   THEN '3–5%'
                WHEN profit_pct < 10  THEN '5–10%'
                ELSE '10%+'
            END AS bucket,
            COUNT(*) AS count,
            AVG(profit_pct) AS avg_pct
            FROM opportunities
            GROUP BY bucket
        """).fetchall()
    return [dict(r) for r in rows]


def get_calendar_heatmap(days: int = 365) -> list[dict]:
    """Ежедневное количество возможностей за последние N дней."""
    if _DB_PATH is None or not _DB_PATH.is_file():
        return []
    with _connect() as conn:
        rows = conn.execute("""
            SELECT date, COUNT(*) AS count, AVG(profit_pct) AS avg_pct
            FROM opportunities
            GROUP BY date
            ORDER BY date DESC
            LIMIT ?
        """, (days,)).fetchall()
    return [dict(r) for r in rows]
