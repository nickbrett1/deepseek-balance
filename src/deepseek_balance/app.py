"""deepseek-balance FastAPI application.

Serves the frontend at `/` (the daily-heartbeat homepage widget) and `/history`
(the drill-in), the view-models it reads at `/ui/*`, the machine endpoints under
`/balance/*` and `/analysis/*`, and `/health`. Starts the APScheduler poller on
startup (lifespan) and stops it on shutdown.

The screens are static files built by Vite (`web/`) and installed from the
`@nb` registry; this module keeps the store, the analytics and the API.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import analysis as analysis_mod
from . import analytics
from .db import BalanceDB
from .phoenix import client_from_env, is_configured
from .poller import BalancePoller, parse_interval

logger = logging.getLogger("deepseek_balance.app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_PORT = 3000
# Finer polling so the "daily heartbeat" can spot rapid single-interval drops.
# Every slice (SPEND_SLICE_MINUTES) needs a poll boundary so a drop can be
# attributed to the slice it ended in; 5m matches the default 5m slice.
DEFAULT_INTERVAL = "5m"

# The built frontend: `web/dist` in the repo, copied next to this module by the
# image. Overridable so a test (or a non-image deployment) can point elsewhere.
WEB_DIST_DIR = Path(
    os.environ.get("DEEPSEEK_BALANCE_WEB_DIR") or Path(__file__).with_name("web")
)
NO_STORE = {"Cache-Control": "no-store"}


def _poll_trigger(interval_seconds: int):
    """Wall-clock-anchored trigger so polls land exactly on the slice grid.

    Polls should fire on round clock boundaries (:00/:05/:10…) rather than on
    a free-running ``interval since the process started``. APScheduler's
    ``CronTrigger`` *waits for the next boundary* and then recomputes the next
    fire from the schedule (not from completion time), so a late or coalesced
    tick never drifts the phase or opens an oversized gap. Any interval that
    divides an hour evenly maps to a minute cron (1m→'*', 5m→'*/5', 15m→'*/15').
    Periods that can't be expressed that way fall back to an IntervalTrigger.
    """
    if interval_seconds >= 60 and interval_seconds % 60 == 0:
        minutes = interval_seconds // 60
        if minutes <= 59 and 60 % minutes == 0:
            minute = "*" if minutes == 1 else f"*/{minutes}"
            return CronTrigger(minute=minute)
    return IntervalTrigger(seconds=interval_seconds)


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _parse_now(now: str | None) -> datetime:
    """Parse the client's local `now` (ISO with offset); defaults to UTC now."""
    if now:
        try:
            tz_now = datetime.fromisoformat(now)
        except ValueError:
            tz_now = datetime.now(UTC)
        if tz_now.tzinfo is None:
            tz_now = tz_now.replace(tzinfo=UTC)
        return tz_now
    return datetime.now(UTC)


def _summary_kwargs() -> dict:
    """Shared spend-interval tuning pulled from the environment."""
    return {
        "spend_slice_minutes": _int_env("SPEND_SLICE_MINUTES", 5),
        "summary_hours": _int_env("SPEND_SUMMARY_HOURS", 24),
        "spike_mult": _float_env("SPIKE_MULT", 3.0),
        "spike_min_ratio": _float_env("SPIKE_MIN_RATIO", 2.0),
        "min_intervals_for_baseline": _int_env("MIN_INTERVALS_FOR_BASELINE", 10),
        "normal_band": _float_env("NORMAL_BAND", 2.0),
        "max_gap_minutes": _int_env("MAX_GAP_MINUTES", 30),
        "baseline_days": _int_env("BASELINE_DAYS", 14),
    }


def _analysis_service(db: BalanceDB) -> analysis_mod.AnalysisService | None:
    """Build the analysis service when Phoenix is configured, else None."""
    if not is_configured():
        return None
    try:
        phoenix = client_from_env()
    except Exception as exc:  # noqa: BLE001 — fail soft so the app still runs
        logger.warning("Phoenix client init failed: %s — analysis disabled", exc)
        return None
    return analysis_mod.AnalysisService(
        db,
        phoenix,
        lookback_days=_int_env("ANALYSIS_LOOKBACK_DAYS", analysis_mod.DEFAULT_LOOKBACK_DAYS),
        pad_seconds=_int_env("PHOENIX_WINDOW_PAD_SECONDS", analysis_mod.DEFAULT_PHOENIX_PAD_SECONDS),
        max_diagnose_per_run=_int_env(
            "MAX_DIAGNOSE_PER_RUN", analysis_mod.DEFAULT_MAX_DIAGNOSE_PER_RUN
        ),
        max_attempts=_int_env(
            "ANALYSIS_MAX_ATTEMPTS", analysis_mod.DEFAULT_MAX_ANALYSIS_ATTEMPTS
        ),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = _env("DB_PATH", "/data/deepseek.db")
    interval = _env("POLL_INTERVAL", DEFAULT_INTERVAL)
    api_key = _env("DEEPSEEK_API_KEY")
    interval_seconds = parse_interval(interval)

    db = BalanceDB(db_path)
    poller = BalancePoller(db=db, api_key=api_key, interval_seconds=interval_seconds)
    trigger = _poll_trigger(interval_seconds)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        poller.poll_once,
        trigger=trigger,
        id="deepseek-balance-poll",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("poller started: every %s (%.0fs), db=%s", interval, interval_seconds, db_path)

    # Optional "why was it high?" analysis: only when Phoenix is configured.
    analysis_service = _analysis_service(db)
    if analysis_service is not None:
        analysis_seconds = parse_interval(_env("ANALYSIS_INTERVAL", "10m"))
        scheduler.add_job(
            analysis_service.run,
            trigger=IntervalTrigger(seconds=analysis_seconds),
            id="deepseek-balance-analysis",
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "analysis job started: every %ss (phoenix=%s, lookback=%dd)",
            analysis_seconds,
            analysis_service.phoenix.base_url,
            analysis_service.lookback_days,
        )
        # First pass on startup so the table has history to show right away.
        try:
            report = analysis_service.run()
            logger.info("analysis backfill: %s", report)
        except Exception as exc:  # noqa: BLE001 — never let a backfill failure kill startup
            logger.warning("initial analysis backfill failed: %s", exc)

    app.state.db = db
    app.state.poller = poller
    app.state.analysis = analysis_service

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        db.close()


app = FastAPI(title="deepseek-balance", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/balance/latest")
def balance_latest() -> dict:
    row = app.state.db.latest()
    if row is None:
        return {"error": "no successful snapshot yet"}
    return {
        "currency": row["currency"],
        "total_balance": row["total_balance"],
        "granted_balance": row["granted_balance"],
        "topped_up_balance": row["topped_up_balance"],
        "ts": row["ts"],
    }


@app.get("/balance/history")
def balance_history(hours: int = 24, step: str = "15m") -> dict:
    step_seconds = parse_interval(step)
    if hours <= 0:
        hours = 24
    now = datetime.now(UTC)
    start = now - timedelta(hours=hours)
    rows = app.state.db.history(start.isoformat())

    # Bucket rows into `step` windows, keeping the last value per bucket.
    buckets: dict[int, dict] = {}
    for row in rows:
        ts = datetime.fromisoformat(row["ts"])
        idx = int((ts - start).total_seconds() // step_seconds)
        buckets[idx] = {
            "ts": (start + timedelta(seconds=idx * step_seconds)).isoformat(),
            "total_balance": row["total_balance"],
        }
    points = [buckets[k] for k in sorted(buckets)]
    return {"points": points}


def _daily_heartbeat(tz_now: datetime) -> dict:
    """The day's summary, relative to the viewer's local `now`.

    One implementation, two callers: `/balance/daily` (the machine endpoint the
    widget's previous inline script used) and `/ui/widget.json` (the
    view-model). They must not drift, so they cannot be two expressions of the
    same calculation.
    """
    return analytics.daily_heartbeat(
        app.state.db,
        tz_now,
        rapid_window_minutes=_int_env("RAPID_WINDOW_MINUTES", 60),
        rapid_min_pct=_float_env("RAPID_MIN_PCT", 0.02),
        rapid_mult=_float_env("RAPID_MULT", 2.0),
        normal_days=_int_env("NORMAL_DAYS", 14),
        **_summary_kwargs(),
    )


@app.get("/balance/daily")
def balance_daily(now: str | None = None) -> dict:
    """Daily "heartbeat" summary. `now` is the client's local time (with
    offset) so "today" matches the user's local day; defaults to UTC."""
    return _daily_heartbeat(_parse_now(now))


@app.get("/balance/days")
def balance_days(now: str | None = None, days: int = 14) -> dict:
    """Per-complete-day spend & usage-time series (bars for the history view).

    `days` is the number of most-recent complete days to return; pass `0` for
    all data from the earliest snapshot. Today is excluded (it's partial) and
    `now` carries the client's local time so day boundaries match the viewer.
    """
    return _daily_series(_parse_now(now), days)


@app.get("/spend/intervals")
def spend_intervals_endpoint(
    now: str | None = None,
    hours: int = 24,
    slice_minutes: int = 5,
    bucket: str | None = None,
) -> dict:
    """Per-spend-interval breakdown for deeper analysis.

    Returns every spent interval (with its start time and a `high`/`normal`/
    `below` classification) plus the thresholds used. Pass `bucket` to narrow
    to just one classification. `now` is the client's local time (with offset).
    """
    tz_now = _parse_now(now)
    kwargs = _summary_kwargs()
    kwargs["spend_slice_minutes"] = slice_minutes
    kwargs["summary_hours"] = hours
    data = analytics.spend_intervals(app.state.db, tz_now, **kwargs)
    if bucket in {"high", "normal", "below"}:
        data["intervals"] = [i for i in data["intervals"] if i["bucket"] == bucket]
    return data


def _analysis_rows(
    service, limit: int, before: str | None, status: str | None
) -> tuple[list[dict], bool, str | None]:
    """Query diagnosed/known high intervals, converted to server-local times.

    Returns ``(rows, has_more, next_before)`` for the table paginator.
    ``status`` narrows rows to one diagnosis state (see the endpoint docstring).
    """
    if service is None:
        return [], False, None
    rows, has_more = service.db.high_intervals_with_diagnostics(
        limit=limit, before_utc=before, status=status
    )
    tz = datetime.now().astimezone().tzinfo
    out: list[dict] = []
    for r in rows:
        def _to_local(iso: str) -> str:
            try:
                return datetime.fromisoformat(iso).astimezone(tz).isoformat()
            except (TypeError, ValueError):
                return iso
        out.append(
            {
                "start_utc": r["start_utc"],
                "end_utc": r["end_utc"],
                "start_local": _to_local(r["start_utc"]),
                "end_local": _to_local(r["end_utc"]),
                "day": r["day"],
                "slice_minutes": r["slice_minutes"],
                "spend": r["spend"],
                "diagnosis": r["diagnosis"],
                # Phoenix-dive health for a not-yet-diagnosed row: lets the UI
                # say "Phoenix failed" instead of "Awaiting the next pass".
                "analysis_attempts": r.get("analysis_attempts") or 0,
                "analysis_error": r.get("analysis_error"),
            }
        )
    next_before = out[-1]["start_utc"] if (has_more and out) else None
    return out, has_more, next_before


_ANALYSIS_STATUSES = {"all", "investigate", "actionable", "fine", "pending"}


@app.get("/analysis/high-intervals")
def analysis_high_intervals(
    limit: int = 15, before: str | None = None, status: str = "all"
) -> dict:
    """Table of unusually-high spend intervals with their Phoenix diagnosis.

    Newest first. Pass ``before`` (a UTC interval-start from the previous page's
    ``next_before``) to page back through older intervals. ``limit`` caps page
    size (default 15). Times are given in both UTC and the server's local time.

    ``status`` filters the table by diagnosis state so the "why was it high?"
    view can surface exactly the rows that need attention: ``investigate``
    (spend Phoenix couldn't explain - needs a human), ``actionable`` (a real
    optimisation candidate), ``fine`` (benign / well-cached high activity), or
    ``pending`` (recorded but not yet analysed). Default ``all`` returns every
    status.
    """
    if status not in _ANALYSIS_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="status must be one of: all, investigate, actionable, fine, pending",
        )
    limit = max(1, min(100, limit))
    service: analysis_mod.AnalysisService | None = getattr(app.state, "analysis", None)
    if service is None:
        return {"configured": False, "timezone": None, "rows": [], "has_more": False}
    rows, has_more, next_before = _analysis_rows(service, limit, before, status)
    return {
        "configured": True,
        "timezone": datetime.now().astimezone().tzname(),
        "status": status,
        "max_attempts": getattr(service, "max_attempts", None),
        "rows": rows,
        "has_more": has_more,
        "next_before": next_before,
    }


@app.post("/analysis/backfill")
def analysis_backfill() -> dict:
    """Run one analysis pass now (detect highs + diagnose the new ones)."""
    service: analysis_mod.AnalysisService | None = getattr(app.state, "analysis", None)
    if service is None:
        return {"error": "analysis not configured (set PHOENIX_BASE_URL)"}
    try:
        return service.run()
    except Exception as exc:  # noqa: BLE001
        logger.warning("manual analysis backfill failed: %s", exc)
        return {"error": str(exc)}


@app.post("/analysis/redo")
def analysis_redo() -> dict:
    """Wipe the recorded high-interval analysis and recompute it from scratch.

    Deletes every recorded high interval + diagnosis (balance snapshots are
    kept) and runs a fresh detection + Phoenix-diagnosis pass with the current
    logic. Use after a reconciliation change to regenerate the table against
    the unchanged balance history.
    """
    service: analysis_mod.AnalysisService | None = getattr(app.state, "analysis", None)
    if service is None:
        return {"error": "analysis not configured (set PHOENIX_BASE_URL)"}
    try:
        return service.redo()
    except Exception as exc:  # noqa: BLE001
        logger.warning("analysis redo failed: %s", exc)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """The homepage widget: an iframe-embedded card, still."""
    return _index()


@app.get("/history", include_in_schema=False)
def history_page() -> FileResponse:
    """Drill-in view: today's data plus the daily spend/usage/cost charts.

    The compact homepage widget (served at `/`, iframed by the Homepage
    dashboard) links here so the full screen only appears on drill-in.
    """
    return _index()


def _index() -> FileResponse:
    """One bundle, two views: the page picks its screen from the pathname."""
    index = WEB_DIST_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"the frontend is not built: {index} is missing (run `npm run build` in web/)",
        )
    return FileResponse(index, media_type="text/html", headers=NO_STORE)


if (WEB_DIST_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIST_DIR / "assets")), name="assets")

def _daily_series(tz_now: datetime, days: int) -> dict:
    """Per-complete-day series, with the display currency attached."""
    series = analytics.daily_history_series(
        app.state.db,
        tz_now,
        days=(days if days > 0 else None),
        spend_slice_minutes=_int_env("SPEND_SLICE_MINUTES", 5),
        max_gap_minutes=_int_env("MAX_GAP_MINUTES", 30),
    )
    latest = app.state.db.latest()
    series["currency"] = latest["currency"] if latest else None
    return series


# --- view-models -------------------------------------------------------------
#
# What the two screens read. Deliberately not the machine endpoints: these
# carry a verdict (is today above normal? did Phoenix explain this interval?)
# that is a domain rule, and a rule belongs on the side that owns the data.
# `/balance/*` and `/analysis/*` stay exactly as they were -- the MCP server and
# the poller read them, and a view-model must not become the API.

PACING_BELOW = 0.7
PACING_ABOVE = 1.3

# The diagnosis verdicts, in the words the table shows. `pending` is "recorded,
# not analysed yet"; `failed` is "Phoenix was asked and could not answer" -- a
# different state, and the reason a window that will never be analysed must not
# read as merely waiting.
ANALYSIS_STATUS = {
    "investigate": {"label": "investigate", "tone": "bad"},
    "actionable": {"label": "actionable", "tone": "warn"},
    "fine": {"label": "fine", "tone": "ok"},
    "pending": {"label": "analysing", "tone": "neutral"},
    "failed": {"label": "failed", "tone": "bad"},
}


def _pacing(spend_vs_normal: float | None) -> dict:
    """Today's projection against the typical day.

    The two thresholds are the rule "is today normal", not a colour choice, so
    they live here rather than in the browser.
    """
    if spend_vs_normal is None:
        return {"label": "no baseline", "tone": "neutral"}
    if spend_vs_normal < PACING_BELOW:
        return {"label": "below avg", "tone": "ok"}
    if spend_vs_normal <= PACING_ABOVE:
        return {"label": "on track", "tone": "ok"}
    return {"label": "above normal", "tone": "warn"}


def _today_panel(daily: dict, *, full: bool) -> dict:
    """The day's numbers as named fields, not as a list the page must trust.

    `full` is the drill-in's six numbers; the widget shows the four that fit in
    a dashboard card and puts the day's opening balance in its sub-line.
    """
    fields = {
        "spent_today": daily.get("spent_today"),
        "spent_yesterday": daily.get("spent_yesterday"),
        "normal_spend": daily.get("normal_spend"),
        "projected_spend": daily.get("projected_spend"),
        "current_balance": daily.get("current_balance"),
        "prev_balance": daily.get("prev_balance"),
        "start_of_day": daily.get("start_of_day"),
        "current_ts": daily.get("current_ts"),
    }
    if not full:
        fields.pop("start_of_day")
    return fields


@app.get("/ui/widget.json")
def ui_widget(now: str | None = None) -> dict:
    """Everything the homepage card draws, in one fetch.

    The card is iframed by the Homepage dashboard, so it gets no chrome of its
    own: one verdict (the pacing pill), four numbers, and today's interval
    summary.
    """
    daily = _daily_heartbeat(_parse_now(now))
    if daily.get("current_balance") is None:
        return {"available": False, "reason": "no successful snapshot yet"}
    return {
        "available": True,
        "currency": daily.get("currency"),
        "pacing": _pacing(daily.get("spend_vs_normal")),
        "today": _today_panel(daily, full=False),
        "summary": daily.get("spend_summary") or {},
    }


@app.get("/ui/history.json")
def ui_history(now: str | None = None, days: int = 14) -> dict:
    """The drill-in's data: today's panel and the complete-day series.

    One fetch for both, because the page draws them side by side and the
    projection bar is today's number drawn against yesterday's history: two
    requests could disagree about what "today" is.
    """
    tz_now = _parse_now(now)
    daily = _daily_heartbeat(tz_now)
    series = _daily_series(tz_now, days)
    return {
        "available": daily.get("current_balance") is not None,
        "currency": series.get("currency") or daily.get("currency"),
        "now": tz_now.isoformat(),
        "slice_minutes": series.get("slice_minutes"),
        "today": _today_panel(daily, full=True),
        "pacing": _pacing(daily.get("spend_vs_normal")),
        "summary": daily.get("spend_summary") or {},
        "series": series.get("days") or [],
        "available_days": _available_days(tz_now),
        "days": days,
    }


def _available_days(tz_now: datetime) -> int:
    """How many complete days we hold, for the range picker's counts."""
    earliest = app.state.db.earliest_ts()
    if earliest is None:
        return 0
    try:
        first = datetime.fromisoformat(earliest).astimezone(tz_now.tzinfo or UTC)
    except ValueError:
        return 0
    return max(0, (tz_now.date() - first.date()).days)


def _analyse_row(row: dict, max_attempts: int | None) -> dict:
    """One high interval, with the verdict the table shows.

    `diagnosis` absent has two meanings and they must not be merged: no attempts
    yet is *pending*, attempts that all failed is *failed* -- and a row that has
    exhausted its attempts will never be analysed, so it must not sit there
    looking like it is about to be.
    """
    diag = row.get("diagnosis")
    attempts = row.get("analysis_attempts") or 0
    if diag:
        status = (
            ANALYSIS_STATUS["investigate"]
            if diag.get("investigate")
            else ANALYSIS_STATUS["actionable"]
            if diag.get("actionable")
            else ANALYSIS_STATUS["fine"]
        )
        label = diag.get("reason_label") or diag.get("reason") or ""
        summary = diag.get("summary") or ""
        requests = diag.get("request_count")
        cache = diag.get("cache_hit_ratio")
        explained = diag.get("explained_cost_pct")
    else:
        failed = attempts > 0
        gave_up = bool(failed and max_attempts is not None and attempts >= max_attempts)
        status = ANALYSIS_STATUS["failed" if failed else "pending"]
        label = "analysis failed" if failed else "analysis pending"
        summary = (
            (
                "Phoenix could not be queried -- giving up: "
                if gave_up
                else "Phoenix failed: "
            )
            + (row.get("analysis_error") or "unknown error")
            + ("" if gave_up else " -- retrying.")
            if failed
            else "Awaiting the next analysis pass."
        )
        requests = cache = explained = None
    return {
        **row,
        "requests": requests,
        "cache_pct": round(cache * 100) if cache is not None else None,
        "explained_pct": round(explained) if explained is not None else None,
        "label": label,
        "summary": summary,
        "status": status,
        "attempts": attempts,
    }


@app.get("/ui/analysis.json")
def ui_analysis(
    limit: int = 15, before: str | None = None, status: str = "all"
) -> dict:
    """The "why was it unusually high?" table, paged the same way as the API."""
    if status not in _ANALYSIS_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="status must be one of: all, investigate, actionable, fine, pending",
        )
    limit = max(1, min(100, limit))
    service: analysis_mod.AnalysisService | None = getattr(app.state, "analysis", None)
    if service is None:
        return {
            "configured": False,
            "status": status,
            "max_attempts": None,
            "rows": [],
            "has_more": False,
            "next_before": None,
        }
    max_attempts = getattr(service, "max_attempts", None)
    rows, has_more, next_before = _analysis_rows(service, limit, before, status)
    return {
        "configured": True,
        "status": status,
        "max_attempts": max_attempts,
        "rows": [_analyse_row(row, max_attempts) for row in rows],
        "has_more": has_more,
        "next_before": next_before,
    }
