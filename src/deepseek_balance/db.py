"""SQLite storage for balance snapshots (WAL mode).

The poller writes one row per poll (successes and explicit gap rows alike);
the FastAPI read endpoints query this same database.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS balance_snapshots (
  ts TEXT PRIMARY KEY,            -- ISO8601 UTC
  currency TEXT,
  total_balance REAL,
  granted_balance REAL,
  topped_up_balance REAL,
  is_available INTEGER,
  http_status INTEGER,
  raw TEXT                        -- full JSON response
);
"""


class BalanceDB:
    """Thin thread-safe wrapper around a WAL-mode SQLite connection."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def insert_snapshot(
        self,
        *,
        ts: str,
        currency: str | None,
        total_balance: float | None,
        granted_balance: float | None,
        topped_up_balance: float | None,
        is_available: bool,
        http_status: int | None,
        raw: str,
    ) -> None:
        """Insert one snapshot row. ts is the primary key (upsert-safe)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO balance_snapshots (
                  ts, currency, total_balance, granted_balance,
                  topped_up_balance, is_available, http_status, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ts) DO UPDATE SET
                  currency=excluded.currency,
                  total_balance=excluded.total_balance,
                  granted_balance=excluded.granted_balance,
                  topped_up_balance=excluded.topped_up_balance,
                  is_available=excluded.is_available,
                  http_status=excluded.http_status,
                  raw=excluded.raw
                """,
                (
                    ts,
                    currency,
                    total_balance,
                    granted_balance,
                    topped_up_balance,
                    1 if is_available else 0,
                    http_status,
                    raw,
                ),
            )
            self._conn.commit()

    def latest(self) -> dict | None:
        """Latest successful snapshot (available and HTTP 200)."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT ts, currency, total_balance, granted_balance,
                       topped_up_balance
                FROM balance_snapshots
                WHERE is_available = 1 AND http_status = 200
                ORDER BY ts DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def history(self, since_iso: str) -> list[dict]:
        """All rows at-or-after `since_iso`, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ts, total_balance, is_available, http_status
                FROM balance_snapshots
                WHERE ts >= ? AND total_balance IS NOT NULL
                ORDER BY ts ASC
                """,
                (since_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
