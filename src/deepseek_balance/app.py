"""deepseek-balance FastAPI application.

Serves `/` (the daily-heartbeat homepage widget), `/balance/daily` (its data
endpoint), `/balance/latest`, `/balance/history` and `/health`. Starts the
APScheduler poller on startup (lifespan) and stops it on shutdown.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import analytics
from .db import BalanceDB
from .poller import BalancePoller, parse_interval

logger = logging.getLogger("deepseek_balance.app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_PORT = 3000
# Finer polling so the "daily heartbeat" can spot rapid single-interval drops.
DEFAULT_INTERVAL = "1m"


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = _env("DB_PATH", "/data/deepseek.db")
    interval = _env("POLL_INTERVAL", DEFAULT_INTERVAL)
    api_key = _env("DEEPSEEK_API_KEY")

    db = BalanceDB(db_path)
    poller = BalancePoller(db=db, api_key=api_key)
    interval_seconds = parse_interval(interval)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        poller.poll_once,
        trigger=IntervalTrigger(seconds=interval_seconds),
        id="deepseek-balance-poll",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("poller started: every %s (%.0fs), db=%s", interval, interval_seconds, db_path)

    app.state.db = db
    app.state.poller = poller

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


@app.get("/balance/daily")
def balance_daily(now: str | None = None) -> dict:
    """Daily "heartbeat" summary. `now` is the client's local time (with
    offset) so "today" matches the user's local day; defaults to UTC."""
    if now:
        try:
            tz_now = datetime.fromisoformat(now)
        except ValueError:
            tz_now = datetime.now(UTC)
        if tz_now.tzinfo is None:
            tz_now = tz_now.replace(tzinfo=UTC)
    else:
        tz_now = datetime.now(UTC)
    return analytics.daily_heartbeat(
        app.state.db,
        tz_now,
        rapid_window_minutes=_int_env("RAPID_WINDOW_MINUTES", 60),
        rapid_min_pct=_float_env("RAPID_MIN_PCT", 0.02),
        rapid_mult=_float_env("RAPID_MULT", 2.0),
        max_gap_minutes=_int_env("MAX_GAP_MINUTES", 30),
        normal_days=_int_env("NORMAL_DAYS", 14),
        spend_slice_minutes=_int_env("SPEND_SLICE_MINUTES", 5),
        summary_hours=_int_env("SPEND_SUMMARY_HOURS", 24),
        spike_mult=_float_env("SPIKE_MULT", 3.0),
        spike_min_ratio=_float_env("SPIKE_MIN_RATIO", 2.0),
        min_intervals_for_baseline=_int_env("MIN_INTERVALS_FOR_BASELINE", 10),
        normal_band=_float_env("NORMAL_BAND", 2.0),
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return WIDGET_HTML


WIDGET_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepSeek Balance</title>
<style>
  html, body { background: #0f172a; }
  body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 12px 14px 8px; font-size: 12.5px; line-height: 1.35; color: #e2e8f0; }
  h1 { font-size: 13px; margin: 0 0 2px; font-weight: 600; }
  .meta { font-size: 10px; opacity: .55; margin-bottom: 4px; }
  .balance { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .balance small { font-size: 11px; font-weight: 500; opacity: .6; }
  .sub { font-size: 10px; opacity: .65; margin-top: 2px; }
  .heart-title { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; opacity: .6; margin: 8px 0 2px; }
  .row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 4px 0; border-bottom: 1px solid rgba(148,163,184,.12); }
  .row:last-child { border-bottom: 0; }
  .row .k { opacity: .65; }
  .row .v { font-variant-numeric: tabular-nums; font-weight: 600; }
  .pill { display: inline-block; font-size: 10px; font-weight: 600; padding: 1px 7px; border-radius: 999px; margin-left: 4px; vertical-align: 1px; }
  .pill.ok { background: rgba(52,211,153,.15); color: #6ee7b7; }
  .pill.warn { background: rgba(251,146,60,.15); color: #fdba74; }
  .pill.bad { background: rgba(248,113,113,.18); color: #fca5a5; }
  .pill.muted { background: rgba(148,163,184,.15); color: #94a3b8; }
  .summary-title { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; opacity: .6; margin: 8px 0 2px; }
  .summary .row .v { font-weight: 500; }
  .up { color: #fb923c; }
  .at { color: #60a5fa; }
  .dn { color: #34d399; }
  .error { color: #f87171; padding: 8px; }
</style>
</head>
<body>
  <h1>DeepSeek Balance</h1>
  <div class="meta" id="meta">Loading…</div>
  <div class="balance" id="balance">—</div>
  <div class="sub" id="sub"></div>
  <div class="heart-title">Today’s heartbeat</div>
  <div id="heart">Loading…</div>
  <div class="summary-title" id="summaryTitle">Spend — last 24h</div>
  <div class="summary" id="summary">Loading…</div>
<script>
const DAILY = "/balance/daily";

function pad(n) { return String(n).padStart(2, "0"); }

// Local ISO-8601 timestamp with offset so the server's "today" matches ours.
function localIso() {
  const d = new Date();
  const off = -d.getTimezoneOffset();            // minutes east of UTC
  const sign = off >= 0 ? "+" : "-";
  const abs = Math.abs(off);
  const tz = sign + pad(Math.floor(abs / 60)) + ":" + pad(abs % 60);
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
    "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()) +
    "." + String(d.getMilliseconds()).padStart(3, "0") + tz;
}

function fmt(v, cur) {
  if (v == null) return "—";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }) + (cur ? " " + cur : "");
}

async function load() {
  try {
    const r = await fetch(DAILY + "?now=" + encodeURIComponent(localIso()), { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    if (d.error || d.current_balance == null) {
      document.getElementById("balance").textContent = "No data yet";
      document.getElementById("heart").innerHTML = '<div class="meta">Waiting for the first snapshot…</div>';
      return;
    }
    renderHeader(d);
    renderHeart(d);
    renderSummary(d);
  } catch (e) {
    document.getElementById("heart").innerHTML = '<div class="error">Failed to load: ' + e.message + "</div>";
  }
}

function renderHeader(d) {
  document.getElementById("balance").innerHTML =
    fmt(d.current_balance) + " <small>" + (d.currency || "") + "</small>";
  document.getElementById("sub").textContent = "started the day at " + fmt(d.prev_balance, d.currency);
  const t = new Date(d.current_ts);
  document.getElementById("meta").textContent =
    "Updated " + t.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) +
    " · " + t.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function pacing(svn) {
  if (svn == null) return ["muted", "no baseline"];
  if (svn < 0.7) return ["ok", "below avg"];
  if (svn <= 1.3) return ["ok", "on track"];
  return ["warn", "above normal"];
}

function renderHeart(d) {
  const [cls, label] = pacing(d.spend_vs_normal);
  const pill = '<span class="pill ' + cls + '">' + label + "</span>";
  const rows = [
    { k: "Spent today", v: fmt(d.spent_today, d.currency) + pill },
    { k: "Typical day", v: fmt(d.normal_spend, d.currency) },
    { k: "Projected spend today", v: fmt(d.projected_spend, d.currency) },
  ];
  document.getElementById("heart").innerHTML = rows.map((r) =>
    '<div class="row"><span class="k">' + r.k + '</span><span class="v">' + r.v + "</span></div>"
  ).join("");
}

function renderSummary(d) {
  const s = d.spend_summary || {};
  const el = document.getElementById("summary");
  document.getElementById("summaryTitle").textContent =
    "Spend — " + s.slice_minutes + " min intervals · last " + s.window_hours + "h";
  const total = s.intervals_with_spend || 0;
  if (total === 0) {
    el.innerHTML = '<div class="meta">No spend in the last ' + s.window_hours + "h.</div>";
    return;
  }
  const pct = (n) => Math.round((n / total) * 100) + "%";
  let rows = '<div class="row"><span class="k">Intervals with spend</span><span class="v">' + total + "</span></div>";
  rows += '<div class="row"><span class="k">Median interval</span><span class="v">' + fmt(s.median_interval_spend, d.currency) + "</span></div>";
  if (!s.enough_data) {
    const need = Math.max(0, s.min_intervals_for_baseline - total);
    el.innerHTML = rows + '<div class="meta">Need ' + s.min_intervals_for_baseline +
      " spent intervals to judge unusual spend — " + total + " so far" + (need ? " (" + need + " more)" : "") + ".</div>";
    return;
  }
  const pctN = (n) => pct(n || 0);
  rows += '<div class="row"><span class="k">Unusually high</span><span class="v up">' + (s.unusually_high_count || 0) +
    " (" + pctN(s.unusually_high_count) + ") · over " + fmt(s.spike_threshold, d.currency) + "</span></div>";
  rows += '<div class="row"><span class="k">Around normal</span><span class="v at">' + (s.normal_count || 0) +
    " (" + pctN(s.normal_count) + ") · " + fmt(s.below_floor, d.currency) + "–" + fmt(s.spike_threshold, d.currency) + "</span></div>";
  rows += '<div class="row"><span class="k">Below normal</span><span class="v dn">' + (s.below_count || 0) +
    " (" + pctN(s.below_count) + ") · under " + fmt(s.below_floor, d.currency) + "</span></div>";
  el.innerHTML = rows;
}

load();
</script>
</body>
</html>
"""
