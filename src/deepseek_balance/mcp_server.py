"""MCP server exposing DeepSeek balance spend analytics to agents.

Agents can query which spend intervals over the last N hours were unusually
high, around normal, or below normal (with the thresholds used), so they can
correlate those periods with whatever was running and dig into why they were
cheap or expensive.

Run over stdio (the default for local agents):

    python -m deepseek_balance.mcp_server

or as a streamable-HTTP service (reachable by remote agents):

    python -m deepseek_balance.mcp_server --http [--host 0.0.0.0 --port 3100]

When imported by the FastAPI app (`mcp_server.app`), it also exposes the same
server over HTTP at `/mcp` on the app's port.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import analytics
from .db import BalanceDB

# Lazily-created shared DB; reads the same DB_PATH the app writes to (WAL mode
# allows concurrent readers alongside the poller).
_db: BalanceDB | None = None


def get_db() -> BalanceDB:
    global _db
    if _db is None:
        _db = BalanceDB(os.environ.get("DB_PATH", "/data/deepseek.db"))
    return _db


def _tuning() -> dict:
    def _int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, str(default)))
        except (TypeError, ValueError):
            return default

    def _float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, str(default)))
        except (TypeError, ValueError):
            return default

    return {
        "spend_slice_minutes": _int("SPEND_SLICE_MINUTES", 5),
        "summary_hours": _int("SPEND_SUMMARY_HOURS", 24),
        "spike_mult": _float("SPIKE_MULT", 3.0),
        "spike_min_ratio": _float("SPIKE_MIN_RATIO", 2.0),
        "min_intervals_for_baseline": _int("MIN_INTERVALS_FOR_BASELINE", 10),
        "normal_band": _float("NORMAL_BAND", 2.0),
        "max_gap_minutes": _int("MAX_GAP_MINUTES", 30),
        "baseline_days": _int("BASELINE_DAYS", 14),
    }


def _local_now() -> datetime:
    """Aware 'now' in the container's local timezone (the TZ env var, e.g.
    America/New_York). All day-boundary math flows from this, so 'today',
    midnight, and day-elapsed are in the user's local day, not UTC."""
    return datetime.now().astimezone()


def _tz_name() -> str:
    try:
        return _local_now().tzname() or "UTC"
    except (ValueError, TypeError):
        return "UTC"


def _to_local(iso: str) -> str:
    """Convert an ISO timestamp to the local timezone (ISO with offset)."""
    try:
        return datetime.fromisoformat(iso).astimezone().isoformat()
    except (ValueError, TypeError):
        return iso


def _intervals(hours: int, slice_minutes: int) -> dict:
    tuning = _tuning()
    tuning["spend_slice_minutes"] = slice_minutes
    tuning["summary_hours"] = hours
    return analytics.spend_intervals(get_db(), _local_now(), **tuning)


server = MCPServer(
    "deepseek-balance",
    instructions=(
        "Read-only access to DeepSeek balance spend analytics. All 'today' / "
        "day-boundary values are in the server's LOCAL timezone "
        "(TZ=America/New_York) and every tool returns a `timezone` field and "
        "local timestamps — do NOT reinterpret them as UTC. For a ready-made "
        "daily summary of today's spend, use `today_summary` first. Otherwise "
        "use `spend_summary` for aggregate counts over the last N hours and "
        "`list_spend_intervals` for individual periods, optionally filtered by "
        "`bucket` ('high' | 'normal' | 'below'). When following up on a heavy "
        "period, use `high_interval_diagnoses` to see why unusually-high "
        "intervals were flagged (Phoenix-trace reasons: actionable candidates, "
        "benign well-cached activity, or 'investigate' where the spend wasn't "
        "explained)."
    ),
)


@server.tool()
def get_balance_latest() -> dict:
    """Return the most recent successful balance snapshot (timestamp is in the
    server's local timezone)."""
    row = get_db().latest()
    if row is None:
        return {"error": "no successful snapshot yet"}
    if "ts" in row:
        row["ts"] = _to_local(row["ts"])
    return row


@server.tool()
def spend_summary(hours: int = 24, slice_minutes: int = 5) -> dict:
    """Aggregate spend-interval summary over the last `hours`.

    Counts spent intervals and how many were unusually high / around normal /
    below normal (with percentages and the thresholds used). Returns the
    `summary` and `thresholds` blocks. Timestamps and 'today' boundaries are
    in the server's local timezone (`timezone`), not UTC.
    """
    si = _intervals(hours, slice_minutes)
    return {
        "timezone": _tz_name(),
        "local_now": _local_now().isoformat(),
        "window_hours": si["window_hours"],
        "slice_minutes": si["slice_minutes"],
        "total_slices": si["total_slices"],
        "enough_data": si["enough_data"],
        "min_intervals_for_baseline": si["min_intervals_for_baseline"],
        "thresholds": si["thresholds"],
        "summary": si["summary"],
    }


@server.tool()
def list_spend_intervals(
    bucket: str | None = None,
    hours: int = 24,
    slice_minutes: int = 5,
) -> dict:
    """List individual spend intervals over the last `hours`.

    Each entry has its start time (`ts`, ISO **local** timezone — see the
    `timezone` field), `spend`, and a `bucket` classification of 'high',
    'normal' or 'below' (None before enough data). Pass `bucket` to return
    only intervals in that classification.
    """
    if bucket not in (None, "high", "normal", "below"):
        raise ValueError("bucket must be one of: high, normal, below (or omitted)")
    si = _intervals(hours, slice_minutes)
    intervals = si["intervals"]
    if bucket is not None:
        intervals = [i for i in intervals if i["bucket"] == bucket]
    for i in intervals:
        i["ts"] = _to_local(i["ts"])
    return {
        "timezone": _tz_name(),
        "local_now": _local_now().isoformat(),
        "window_hours": si["window_hours"],
        "slice_minutes": si["slice_minutes"],
        "total_slices": si["total_slices"],
        "enough_data": si["enough_data"],
        "thresholds": si["thresholds"],
        "summary": si["summary"],
        "intervals": intervals,
    }


@server.tool()
def high_interval_diagnoses(
    limit: int = 15,
    status: str = "all",
    reason: str | None = None,
    include_signals: bool = False,
) -> dict:
    """List unusually-high spend intervals with their Phoenix-trace diagnosis.

    This is the "why was it high?" view an agent should read when it wants to
    follow up on a heavy period: each entry is one high spend interval plus the
    heuristic reason the Phoenix dive produced. `status` narrows to actionable
    (real optimisation candidates), investigate (spend Phoenix couldn't
    explain), benign (well-cached high activity - not candidates), or pending
    (recorded but not yet analysed). Pass `reason` to filter by the exact
    reason key (e.g. 'bloated_context', 'tool_call_loop'). Set
    `include_signals` to add the raw per-window token/cost signals for deep
    investigation. Timestamps are given in both UTC and the server's local
    timezone (`timezone`).
    """
    if status not in ("all", "actionable", "investigate", "benign", "pending"):
        raise ValueError("status must be one of: all, actionable, investigate, benign, pending")
    limit = max(1, min(100, limit))
    rows = get_db().high_intervals_detailed(limit=500, include_signals=include_signals)

    filtered = []
    for r in rows:
        diag = r["diagnosis"]
        if status == "actionable" and not (diag and diag["actionable"]):
            continue
        if status == "investigate" and not (diag and diag["investigate"]):
            continue
        if status == "benign" and not (diag and not diag["actionable"] and not diag["investigate"]):
            continue
        if status == "pending" and diag is not None:
            continue
        if reason and not (diag and diag["reason"] == reason):
            continue
        filtered.append(r)

    entries = []
    for r in filtered[:limit]:
        diag = r["diagnosis"]
        entry = {
            "start_utc": r["start_utc"],
            "start": _to_local(r["start_utc"]),
            "end_utc": r["end_utc"],
            "end": _to_local(r["end_utc"]),
            "day": r["day"],
            "slice_minutes": r["slice_minutes"],
            "spend": r["spend"],
        }
        if diag is not None:
            entry.update(
                {
                    "reason": diag["reason"],
                    "reason_label": diag["reason_label"],
                    "actionable": diag["actionable"],
                    "investigate": diag["investigate"],
                    "summary": diag["summary"],
                    "reconciled_cost": diag["reconciled_cost"],
                    "explained_cost_pct": diag["explained_cost_pct"],
                    "request_count": diag["request_count"],
                    "cache_read_tokens": diag["cache_read_tokens"],
                    "uncached_input_tokens": diag["uncached_input_tokens"],
                    "output_tokens": diag["output_tokens"],
                    "cache_hit_ratio": diag["cache_hit_ratio"],
                    "error_count": diag["error_count"],
                    "tool_call_count": diag["tool_call_count"],
                    "top_models": diag["top_models"],
                    "analyzed_at": diag["analyzed_at"],
                }
            )
            if include_signals and diag.get("signals") is not None:
                entry["signals"] = diag["signals"]
        entries.append(entry)

    return {
        "timezone": _tz_name(),
        "local_now": _local_now().isoformat(),
        "status": status,
        "count": len(entries),
        "intervals": entries,
    }


@server.tool()
def today_summary(
    rapid_window_minutes: int = 60,
    normal_days: int = 14,
) -> dict:
    """Daily 'heartbeat' summary for TODAY in the server's local timezone
    (TZ=America/New_York). Use this instead of reconstructing the day from UTC
    or from `spend_summary` — it directly returns the local day's `start_of_day`,
    `fraction_elapsed`, `spent_today`, `normal_spend`, `spend_vs_normal`,
    `projected_end_balance`, rapid-drop flags, and the spend-interval summary.
    """
    hb = analytics.daily_heartbeat(
        get_db(),
        _local_now(),
        rapid_window_minutes=rapid_window_minutes,
        normal_days=normal_days,
        **_tuning(),
    )
    # The heartbeat's nested timestamps come from DB rows as UTC; convert them
    # to the local timezone so the agent sees the same day / hour the user does.
    if hb.get("current_ts") is not None:
        hb["current_ts"] = _to_local(hb["current_ts"])
    for p in hb.get("today_points", []):
        if "ts" in p:
            p["ts"] = _to_local(p["ts"])
    for d in hb.get("rapid_drops", []):
        if "from_ts" in d:
            d["from_ts"] = _to_local(d["from_ts"])
        if "to_ts" in d:
            d["to_ts"] = _to_local(d["to_ts"])
    hb["timezone"] = _tz_name()
    hb["local_now"] = _local_now().isoformat()
    return hb


@server.tool()
def balance_history(hours: int = 24) -> list[dict]:
    """Raw balance snapshots for the last `hours` (oldest first). Returned
    timestamps are in the server's local timezone."""
    since = (_local_now() - timedelta(hours=hours)).isoformat()
    rows = get_db().history(since)
    for r in rows:
        if "ts" in r:
            r["ts"] = _to_local(r["ts"])
    return rows


@server.tool()
def redo_high_interval_analysis(confirm: bool = False) -> dict:
    """[DESTRUCTIVE] Wipe the recorded unusually-high spend-interval analysis
    and recompute it from the unchanged balance history.

    Deletes every recorded high interval and its Phoenix diagnosis (balance
    snapshots are preserved) and re-runs detection + diagnosis with the
    current logic. Use after a reconciliation change (e.g. the window
    attribution fix) to regenerate the table. Pass ``confirm=True`` to run it;
    ``confirm=False`` (default) is a safe dry-run that only reports how many
    rows would be cleared. Requires Phoenix to be configured.
    """
    from . import analysis as analysis_mod
    from .phoenix import client_from_env, is_configured

    if not is_configured():
        return {"ok": False, "error": "analysis not configured (set PHOENIX_BASE_URL)"}
    db = get_db()
    existing = db.high_intervals_detailed(limit=100_000, include_signals=False)
    if not confirm:
        return {
            "ok": True,
            "dry_run": True,
            "would_clear_intervals": len(existing),
            "note": "Pass confirm=True to wipe the analysis and recompute it.",
        }
    svc = analysis_mod.AnalysisService(db, client_from_env())
    report = svc.redo()
    report["cleared"] = {
        "would_clear_intervals": len(existing),
        **report.get("cleared", {}),
    }
    report["ok"] = True
    return report


def run_stdio() -> None:
    asyncio.run(server.run_stdio_async())


def get_app(path: str = "/mcp") -> Any:
    """A Starlette app exposing the MCP server over streamable HTTP at `path`."""
    return server.streamable_http_app(streamable_http_path=path)


def run_http(host: str, port: int, path: str = "/mcp") -> None:
    asyncio.run(
        server.run_streamable_http_async(host=host, port=port, streamable_http_path=path)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="deepseek-balance MCP server")
    parser.add_argument("--http", action="store_true", help="serve over streamable HTTP")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3100)
    args = parser.parse_args()
    if args.http:
        run_http(args.host, args.port)
    else:
        run_stdio()


if __name__ == "__main__":
    main()
