"""SQLite storage for balance snapshots (WAL mode).

The poller writes one row per poll (successes and explicit gap rows alike);
the FastAPI read endpoints query this same database.

Since the "why was this high?" analysis materializes a registry of unusually
high spend intervals and the diagnosis the Phoenix dive produced for each, two
more tables live here: ``high_intervals`` (a stable record of each spike once,
keyed by its UTC interval start) and ``interval_diagnostics`` (the heuristic
explanation / investigation flag). All writes are idempotent upserts so the
analysis job and backfill can run repeatedly without duplicating rows.
"""

from __future__ import annotations

import json
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

-- One row per detected "unusually high" spend interval (UTC start = PK), so a
-- spike is recorded once regardless of how many times detection runs. The
-- thresholds used to call it a spike are kept for audit / re-tuning.
CREATE TABLE IF NOT EXISTS high_intervals (
  start_utc TEXT PRIMARY KEY,     -- ISO8601 UTC, slice start
  end_utc TEXT,
  slice_minutes INTEGER,
  spend REAL,                     -- balance drop attributed to the interval
  spike_threshold REAL,           -- median + SPIKE_MULT*MAD used (audit)
  median_interval REAL,
  day TEXT,                       -- local day (server TZ) the interval fell in
  detected_at TEXT                -- when we first recorded it (UTC)
);

-- The Phoenix-trace diagnosis for a high interval. Keyed by the same UTC
-- interval start so re-analysis overwrites rather than duplicates.
CREATE TABLE IF NOT EXISTS interval_diagnostics (
  start_utc TEXT PRIMARY KEY,
  reason TEXT,                    -- stable machine key, e.g. 'bloated_context'
  reason_label TEXT,              -- human sentence
  actionable INTEGER,
  investigate INTEGER,            -- 1 => could not explain, needs a human
  window_spend REAL,              -- interval spend (balance drop) we tried to explain
  reconciled_cost REAL,           -- sum of litellm.cost.total over the window
  explained_cost_pct REAL,        -- reconciled_cost / window_spend * 100
  request_count INTEGER,
  cache_read_tokens INTEGER,
  uncached_input_tokens INTEGER,
  output_tokens INTEGER,
  cache_hit_ratio REAL,
  error_count INTEGER,
  tool_call_count INTEGER,
  top_models TEXT,                -- JSON list of {model, cost, requests}
  summary TEXT,                   -- one-line human explanation
  payload TEXT,                   -- full JSON diagnosis (signals, for tuning)
  analyzed_at TEXT                -- when the dive last ran (UTC)
);
"""


def _json(value) -> str | None:
    return json.dumps(value) if value is not None else None


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
        self._conn.executescript(SCHEMA)

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

    # --- high-interval registry -------------------------------------------------

    def record_high_interval(
        self,
        *,
        start_utc: str,
        end_utc: str,
        slice_minutes: int,
        spend: float,
        spike_threshold: float | None,
        median_interval: float | None,
        day: str,
        detected_at: str,
    ) -> None:
        """Idempotently record one unusually-high interval (upsert on start)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO high_intervals (
                  start_utc, end_utc, slice_minutes, spend,
                  spike_threshold, median_interval, day, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(start_utc) DO UPDATE SET
                  end_utc=excluded.end_utc,
                  slice_minutes=excluded.slice_minutes,
                  spend=excluded.spend,
                  spike_threshold=excluded.spike_threshold,
                  median_interval=excluded.median_interval,
                  day=excluded.day
                """,
                (
                    start_utc, end_utc, slice_minutes, spend,
                    spike_threshold, median_interval, day, detected_at,
                ),
            )
            self._conn.commit()

    def _high_row(self, start_utc: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM high_intervals WHERE start_utc = ?", (start_utc,)
            ).fetchone()
        return dict(row) if row else None

    def high_intervals_unanalyzed(
        self, since_utc: str, limit: int = 200
    ) -> list[dict]:
        """Recorded high intervals since `since_utc` that lack a diagnosis yet."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT h.* FROM high_intervals h
                LEFT JOIN interval_diagnostics d ON d.start_utc = h.start_utc
                WHERE h.start_utc >= ? AND d.start_utc IS NULL
                ORDER BY h.start_utc DESC LIMIT ?
                """,
                (since_utc, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def high_intervals_with_diagnostics(
        self,
        *,
        limit: int = 25,
        before_utc: str | None = None,
    ) -> tuple[list[dict], bool]:
        """Diagnosed high intervals, newest first, joined with their diagnosis.

        Returns ``(rows, has_more)`` for paging. Each row carries the high
        interval fields plus ``diagnosis`` (the matching diagnostic dict, or
        None when it is still being analysed). ``before_utc`` pages to older
        rows than a cursor timestamp.
        """
        sql = """
            SELECT h.*, d.* FROM high_intervals h
            LEFT JOIN interval_diagnostics d ON d.start_utc = h.start_utc
        """
        params: list = []
        if before_utc is not None:
            sql += " WHERE h.start_utc < ?"
            params.append(before_utc)
        sql += " ORDER BY h.start_utc DESC LIMIT ?"
        params.append(limit + 1)  # fetch one extra to know if more pages exist
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]
        out: list[dict] = []
        for r in rows:
            r = dict(r)
            diag = None
            if r.get("analyzed_at") is not None:
                diag = {
                    "reason": r.pop("reason"),
                    "reason_label": r.pop("reason_label"),
                    "actionable": bool(r.pop("actionable")),
                    "investigate": bool(r.pop("investigate")),
                    "window_spend": r.pop("window_spend"),
                    "reconciled_cost": r.pop("reconciled_cost"),
                    "explained_cost_pct": r.pop("explained_cost_pct"),
                    "request_count": r.pop("request_count"),
                    "cache_read_tokens": r.pop("cache_read_tokens"),
                    "uncached_input_tokens": r.pop("uncached_input_tokens"),
                    "output_tokens": r.pop("output_tokens"),
                    "cache_hit_ratio": r.pop("cache_hit_ratio"),
                    "error_count": r.pop("error_count"),
                    "tool_call_count": r.pop("tool_call_count"),
                    "top_models": json.loads(r.pop("top_models") or "[]"),
                    "summary": r.pop("summary"),
                    "analyzed_at": r.pop("analyzed_at"),
                }
            out.append(
                {
                    "start_utc": r["start_utc"],
                    "end_utc": r["end_utc"],
                    "slice_minutes": r["slice_minutes"],
                    "spend": r["spend"],
                    "day": r["day"],
                    "diagnosis": diag,
                }
            )
        return out, has_more

    # --- diagnostics ------------------------------------------------------------

    def upsert_diagnostic(self, *, start_utc: str, diag: dict) -> None:
        """Store (overwrite) the diagnosis for one high interval."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO interval_diagnostics (
                  start_utc, reason, reason_label, actionable, investigate,
                  window_spend, reconciled_cost, explained_cost_pct,
                  request_count, cache_read_tokens, uncached_input_tokens,
                  output_tokens, cache_hit_ratio, error_count, tool_call_count,
                  top_models, summary, payload, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(start_utc) DO UPDATE SET
                  reason=excluded.reason,
                  reason_label=excluded.reason_label,
                  actionable=excluded.actionable,
                  investigate=excluded.investigate,
                  window_spend=excluded.window_spend,
                  reconciled_cost=excluded.reconciled_cost,
                  explained_cost_pct=excluded.explained_cost_pct,
                  request_count=excluded.request_count,
                  cache_read_tokens=excluded.cache_read_tokens,
                  uncached_input_tokens=excluded.uncached_input_tokens,
                  output_tokens=excluded.output_tokens,
                  cache_hit_ratio=excluded.cache_hit_ratio,
                  error_count=excluded.error_count,
                  tool_call_count=excluded.tool_call_count,
                  top_models=excluded.top_models,
                  summary=excluded.summary,
                  payload=excluded.payload,
                  analyzed_at=excluded.analyzed_at
                """,
                (
                    start_utc,
                    diag["reason"],
                    diag["reason_label"],
                    1 if diag.get("actionable") else 0,
                    1 if diag.get("investigate") else 0,
                    diag.get("window_spend"),
                    diag.get("reconciled_cost"),
                    diag.get("explained_cost_pct"),
                    diag.get("request_count", 0),
                    diag.get("cache_read_tokens", 0),
                    diag.get("uncached_input_tokens", 0),
                    diag.get("output_tokens", 0),
                    diag.get("cache_hit_ratio"),
                    diag.get("error_count", 0),
                    diag.get("tool_call_count", 0),
                    _json(diag.get("top_models")),
                    diag.get("summary"),
                    _json(diag.get("payload")),
                    diag.get("analyzed_at"),
                ),
            )
            self._conn.commit()

    # --- generic ----------------------------------------------------------------

    def earliest_ts(self) -> str | None:
        """Timestamp of the earliest snapshot that carries a balance."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT ts FROM balance_snapshots
                WHERE total_balance IS NOT NULL
                ORDER BY ts ASC LIMIT 1
                """
            ).fetchone()
        return row["ts"] if row else None

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

    def history(self, since_iso: str, before_iso: str | None = None) -> list[dict]:
        """All available rows with `since_iso <= ts < before_iso`, oldest first.

        `before_iso` is optional; when omitted rows are returned from
        `since_iso` onwards.
        """
        sql = """
            SELECT ts, total_balance, is_available, http_status
            FROM balance_snapshots
            WHERE ts >= ? AND total_balance IS NOT NULL
        """
        params: list[str] = [since_iso]
        if before_iso is not None:
            sql += " AND ts < ?"
            params.append(before_iso)
        sql += " ORDER BY ts ASC"
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
