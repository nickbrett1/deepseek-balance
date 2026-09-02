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
    tz_now = _parse_now(now)
    kwargs = _summary_kwargs()
    return analytics.daily_heartbeat(
        app.state.db,
        tz_now,
        rapid_window_minutes=_int_env("RAPID_WINDOW_MINUTES", 60),
        rapid_min_pct=_float_env("RAPID_MIN_PCT", 0.02),
        rapid_mult=_float_env("RAPID_MULT", 2.0),
        normal_days=_int_env("NORMAL_DAYS", 14),
        **kwargs,
    )


@app.get("/balance/days")
def balance_days(now: str | None = None, days: int = 14) -> dict:
    """Per-complete-day spend & usage-time series (bars for the history view).

    `days` is the number of most-recent complete days to return; pass `0` for
    all data from the earliest snapshot. Today is excluded (it's partial) and
    `now` carries the client's local time so day boundaries match the viewer.
    """
    tz_now = _parse_now(now)
    kwargs = {
        "spend_slice_minutes": _int_env("SPEND_SLICE_MINUTES", 5),
        "max_gap_minutes": _int_env("MAX_GAP_MINUTES", 30),
    }
    series = analytics.daily_history_series(
        app.state.db, tz_now, days=(days if days > 0 else None), **kwargs
    )
    # Currency for display; also carry the daily heartbeat so a single drill-in
    # page can render both today's card and the historical bars from one fetch.
    latest = app.state.db.latest()
    series["currency"] = latest["currency"] if latest else None
    return series


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


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return WIDGET_HTML


@app.get("/history", response_class=HTMLResponse)
def history_page() -> str:
    """Drill-in view: today's data plus the daily spend/usage/cost charts.

    The compact homepage widget (served at `/`, iframed by the Homepage
    dashboard) links here so the full screen only appears on drill-in."""
    return HISTORY_HTML


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
  .muted-line { opacity: .55; }
</style>
</head>
<body>
  <h1>DeepSeek Balance</h1>
  <div class="meta" id="meta">Loading…</div>
  <div class="balance" id="balance">—</div>
  <div class="sub" id="sub"></div>
  <div class="heart-title">Today’s heartbeat</div>
  <div id="heart">Loading…</div>
  <div class="summary-title" id="summaryTitle">Spend — today</div>
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

// Render minutes as a compact "2h 15m" (or "45m").
function fmtDur(min) {
  if (min == null) return "—";
  const m = Math.round(Number(min));
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return (h ? h + "h " : "") + mm + "m";
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
    { k: "Spend yesterday", v: fmt(d.spent_yesterday, d.currency) },
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
    "Spend — " + s.slice_minutes + " min intervals · today";
  const total = s.intervals_with_spend || 0;
  if (total === 0) {
    el.innerHTML = '<div class="meta">No spend today.</div>';
    return;
  }
  const pct = (n) => Math.round((n / total) * 100) + "%";
  let rows = '<div class="row"><span class="k">Intervals with spend</span><span class="v">' + total + "</span></div>";
  rows += '<div class="row"><span class="k">Median interval</span><span class="v">' + fmt(s.median_interval_spend, d.currency) + "</span></div>";
  rows += '<div class="row"><span class="k">Usage time</span><span class="v">' + fmtDur(s.usage_minutes) + "</span></div>";
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


HISTORY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepSeek Balance</title>
<style>
  :root { color-scheme: dark; }
  html, body { background: #0f172a; }
  body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 14px 16px 30px; font-size: 13px; line-height: 1.4; color: #e2e8f0; }
  a { color: #7dd3fc; }
  h1 { font-size: 15px; margin: 0 0 2px; font-weight: 600; }
  .meta { font-size: 11px; opacity: .6; margin-bottom: 8px; }
  .topcards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
  .card { background: rgba(30,41,59,.55); border: 1px solid rgba(148,163,184,.14); border-radius: 8px; padding: 8px 10px; }
  .card .t { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; opacity: .6; }
  .card .v { font-size: 16px; font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 2px; }
  .card .s { font-size: 11px; opacity: .7; margin-top: 2px; font-variant-numeric: tabular-nums; }
  .section-title { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; opacity: .6; margin: 16px 0 6px; font-weight: 600; }
  .row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 4px 0; border-bottom: 1px solid rgba(148,163,184,.12); }
  .row:last-child { border-bottom: 0; }
  .row .k { opacity: .65; }
  .row .v { font-variant-numeric: tabular-nums; font-weight: 600; }
  .pill { display: inline-block; font-size: 10px; font-weight: 600; padding: 1px 7px; border-radius: 999px; margin-left: 4px; vertical-align: 1px; }
  .pill.ok { background: rgba(52,211,153,.15); color: #6ee7b7; }
  .pill.warn { background: rgba(251,146,60,.15); color: #fdba74; }
  .pill.bad { background: rgba(248,113,113,.18); color: #fca5a5; }
  .pill.muted { background: rgba(148,163,184,.15); color: #94a3b8; }
  .up { color: #fb923c; } .at { color: #60a5fa; } .dn { color: #34d399; }
  .seg { display: inline-flex; flex-wrap: wrap; border: 1px solid rgba(148,163,184,.25); border-radius: 6px; overflow: hidden; margin: 4px 0 8px; }
  .seg button { background: transparent; color: #cbd5e1; border: 0; padding: 5px 12px; font-size: 12px; cursor: pointer; }
  .seg button.on { background: #334155; color: #f1f5f9; }
  .avgline { font-size: 11px; opacity: .85; margin: 2px 0 10px; }
  .avgline b { font-variant-numeric: tabular-nums; }
  .chart { background: rgba(15,23,42,.35); border: 1px solid rgba(148,163,184,.13); border-radius: 8px; padding: 8px 10px 4px; margin-bottom: 12px; }
  .chart h3 { font-size: 12px; font-weight: 600; margin: 0 0 2px; }
  .chart .note { font-size: 10px; opacity: .6; margin-bottom: 4px; }
  .chart svg { width: 100%; height: auto; display: block; }
  .err { color: #f87171; }
  .muted { opacity: .55; }
  svg text { font-family: ui-sans-serif, system-ui, sans-serif; }
  .todaycard { display:grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); gap:8px; }
</style>
</head>
<body>
  <h1>DeepSeek Balance</h1>
  <div class="meta" id="meta">Loading…</div>

  <div class="section-title">Today’s heartbeat</div>
  <div class="card" id="heart"><div class="muted">Loading…</div></div>

  <div class="section-title">Spend — today <span id="sliceNote"></span></div>
  <div class="card" id="summary"><div class="muted">Loading…</div></div>

  <div class="section-title">History by day</div>
  <div class="seg" id="range">
    <button data-days="14" class="on">14 days</button>
    <button data-days="30">Last month</button>
    <button data-days="0">All time</button>
  </div>
  <div class="avgline" id="avgline"></div>

  <div class="chart">
    <h3>Spend per day <span style="font-weight:400;opacity:.7">(<span id="legCur">currency</span>)</span></h3>
    <div class="note">Today’s bar is dashed = projected full-day spend. Hover for detail.</div>
    <div id="cSpend"></div>
  </div>
  <div class="chart">
    <h3>Usage time per day</h3>
    <div class="note">Minutes with spend × 5-min slices — a rough proxy for active time.</div>
    <div id="cUsage"></div>
  </div>
  <div class="chart">
    <h3>Cost per hour <span style="font-weight:400;opacity:.7">(in <span id="legMinor">¢</span>/hour)</span></h3>
    <div class="note">Spend ÷ active hours. Lower = cheaper use. Blank days had no usage.</div>
    <div id="cCost"></div>
  </div>

<script>
const DAILY = "/balance/daily";
const DAYS = "/balance/days";
const RANGES = { "14": 14, "30": 30, "0": 0 };

function pad(n) { return String(n).padStart(2, "0"); }
function localIso() {
  const d = new Date();
  const off = -d.getTimezoneOffset();
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
function fmtMoney(v, cur) {
  if (v == null) return "—";
  const s = v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
  return s + (cur ? " " + cur : "");
}
function fmtDur(min) {
  if (min == null) return "—";
  const m = Math.round(Number(min));
  const h = Math.floor(m / 60), mm = m % 60;
  return (h ? h + "h " : "") + mm + "m";
}
function minorUnit(cur) {
  const c = (cur || "").toUpperCase();
  if (c.indexOf("CNY") === 0 || c.indexOf("RMB") === 0) return "分";
  return "¢";
}
function fmtMinorInt(v) { // minor-unit hourly number, integer-ish
  return Math.round(v).toLocaleString();
}

const state = { days: 14 };
let cur = "";
let today = null;   // full /balance/daily doc

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

// ---- today panel ---------------------------------------------------------

function pacing(svn) {
  if (svn == null) return ["muted", "no baseline"];
  if (svn < 0.7) return ["ok", "below avg"];
  if (svn <= 1.3) return ["ok", "on track"];
  return ["warn", "above normal"];
}

function renderToday(d) {
  const [cls, label] = pacing(d.spend_vs_normal);
  const pill = '<span class="pill ' + cls + '">' + label + "</span>";
  const rows = [
    { k: "Spent today", v: fmtMoney(d.spent_today, d.currency) + pill },
    { k: "Spend yesterday", v: fmtMoney(d.spent_yesterday, d.currency) },
    { k: "Typical day", v: fmtMoney(d.normal_spend, d.currency) },
    { k: "Projected spend today", v: fmtMoney(d.projected_spend, d.currency) },
    { k: "Balance now", v: fmtMoney(d.current_balance, d.currency) },
    { k: "Started day at", v: fmtMoney(d.prev_balance, d.currency) },
  ];
  document.getElementById("heart").innerHTML = rows.map(r =>
    '<div class="row"><span class="k">' + r.k + '</span><span class="v">' + r.v + "</span></div>").join("");
}

function renderSummary(d) {
  const s = d.spend_summary || {};
  const el = document.getElementById("summary");
  document.getElementById("sliceNote").textContent = "(" + s.slice_minutes + " min intervals)";
  const total = s.intervals_with_spend || 0;
  if (total === 0) { el.innerHTML = '<div class="muted">No spend today.</div>'; return; }
  const pct = (n) => Math.round((n || 0) / total * 100) + "%";
  const rows = [];
  rows.push('<div class="row"><span class="k">Intervals with spend</span><span class="v">' + total + "</span></div>");
  rows.push('<div class="row"><span class="k">Usage time</span><span class="v">' + fmtDur(s.usage_minutes) + "</span></div>");
  rows.push('<div class="row"><span class="k">Median interval</span><span class="v">' + fmtMoney(s.median_interval_spend, d.currency) + "</span></div>");
  if (!s.enough_data) {
    el.innerHTML = rows.join("") + '<div class="muted">Need ' + s.min_intervals_for_baseline +
      " spent intervals to judge unusual spend — " + total + " so far.</div>";
    return;
  }
  rows.push('<div class="row"><span class="k">Unusually high</span><span class="v up">' + (s.unusually_high_count || 0) +
    " (" + pct(s.unusually_high_count) + ")</span></div>");
  rows.push('<div class="row"><span class="k">Around normal</span><span class="v at">' + (s.normal_count || 0) +
    " (" + pct(s.normal_count) + ")</span></div>");
  rows.push('<div class="row"><span class="k">Below normal</span><span class="v dn">' + (s.below_count || 0) +
    " (" + pct(s.below_count) + ")</span></div>");
  el.innerHTML = rows.join("");
}

async function loadToday() {
  const d = await getJSON(DAILY + "?now=" + encodeURIComponent(localIso()));
  if (d.error || d.current_balance == null) {
    today = null;
    document.getElementById("heart").innerHTML = '<div class="muted">Waiting for the first snapshot…</div>';
    return;
  }
  today = d;
  cur = d.currency || "";
  document.getElementById("legCur").textContent = cur || "currency";
  document.getElementById("legMinor").textContent = minorUnit(cur);
  renderToday(d);
  renderSummary(d);
  const t = new Date(d.current_ts);
  document.getElementById("meta").textContent =
    "Updated " + t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) +
    " · " + t.toLocaleDateString([], { month: "short", day: "numeric" });
}

// ---- charts --------------------------------------------------------------

function axisNum(v) {
  if (v >= 100) return Math.round(v).toLocaleString();
  if (v >= 10) return String(Math.round(v * 10) / 10);
  return String(Math.round(v * 100) / 100);
}
function axisDur(v) {
  const m = Math.round(v);
  if (m < 60) return m + "m";
  const h = m / 60;
  return (Math.round(h * 10) / 10) + "h";
}

function niceStep(raw) {
  if (!(raw > 0)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  let s;
  if (norm <= 1) s = 1; else if (norm <= 2) s = 2; else if (norm <= 2.5) s = 2.5;
  else if (norm <= 5) s = 5; else s = 10;
  return s * mag;
}

// items: [{date, v (nullable), isToday}] — bar slot kept even when v==null so
// all three charts share the same x-axis spacing.
function renderBar(containerId, items, cfg) {
  const host = document.getElementById(containerId);
  const vals = items.map(it => it.v).filter(v => v != null);
  if (!vals.length) { host.innerHTML = '<div class="muted">No data in this range.</div>'; return; }
  const maxV = Math.max.apply(null, vals) || 1;
  const step = niceStep(maxV / 4);
  const top = Math.ceil(maxV / step) * step;

  const n = items.length;
  const padL = 44, padR = 6, padT = 8, padB = 22;
  const plotW = Math.max(240, n * 30);        // logical width; scales to container, no scroll
  const plotH = 150, height = plotH + padT + padB, width = plotW + padL + padR;
  const slot = plotW / n;
  const barW = Math.max(2, Math.min(slot * 0.6, 26));
  const y = (v) => padT + plotH * (1 - v / top);

  let s = '<svg viewBox="0 0 ' + width + ' ' + height + '" xmlns="http://www.w3.org/2000/svg">';
  for (let k = 0; k <= 4; k++) {
    const vv = top * k / 4;
    const gy = y(vv);
    s += '<line x1="' + padL + '" y1="' + gy + '" x2="' + (width - padR) + '" y2="' + gy +
      '" stroke="rgba(148,163,184,.12)"' + (k === 0 ? ' stroke-width="1.3"' : "") + "/>";
    s += '<text x="' + (padL - 5) + '" y="' + (gy + 3) + '" text-anchor="end" fill="#94a3b8" font-size="9">' +
      cfg.yFmt(top * k / 4) + "</text>";
  }
  const labelEvery = Math.max(1, Math.ceil(n / 9));
  for (let i = 0; i < n; i++) {
    const it = items[i];
    const cx = padL + i * slot + slot / 2;
    const x = cx - barW / 2;
    if (it.v == null) { continue; }
    const h = Math.max(it.v > 0 ? 1 : 0, (it.v / top) * plotH);
    const yy = y(it.v);
    let fill = cfg.color;
    let extra = "";
    if (it.isToday) {
      fill = "rgba(0,0,0,0)";
      extra = ' stroke="' + cfg.color + '" stroke-dasharray="4 3" stroke-width="1.4"';
    }
    s += '<rect x="' + x.toFixed(1) + '" y="' + yy.toFixed(1) + '" width="' + barW.toFixed(1) +
      '" height="' + h.toFixed(1) + '" fill="' + fill + '"' + extra + ' rx="1"><title>' +
      cfg.tip(it) + "</title></rect>";
    if (it.isToday) {
      s += '<text x="' + cx.toFixed(1) + '" y="' + (padT + plotH + 12) + '" text-anchor="middle" fill="' +
        cfg.color + '" font-size="9">today' + (it.proj ? " (~proj)" : "") + "</text>";
    } else if (i % labelEvery === 0) {
      s += '<text x="' + cx.toFixed(1) + '" y="' + (padT + plotH + 12) + '" text-anchor="middle" fill="#94a3b8" font-size="9">' +
        it.date.slice(5) + "</text>";
    }
  }
  s += "</svg>";
  host.innerHTML = s;
}

// Build per-day series with today's projected bar appended. Returns {items, label}
function buildSpend(hist, tday) {
  const items = hist.map(d => ({ date: d.date, v: d.spend, isToday: false }));
  if (tday) items.push({ date: tday.date, v: tday.spend, isToday: true, proj: tday.proj });
  return items;
}
function buildUsage(hist, tday) {
  const items = hist.map(d => ({ date: d.date, v: d.usage_minutes, isToday: false }));
  if (tday) items.push({ date: tday.date, v: tday.usage_minutes, isToday: true });
  return items;
}
function buildCost(hist, tday, scale) {
  // scale = minor units per base unit (100). cost/hr * scale, integer.
  const items = hist.map(d => {
    const v = d.usage_minutes > 0 ? (d.spend * 60 / d.usage_minutes) * scale : null;
    return { date: d.date, v: v, isToday: false };
  });
  if (tday && tday.usage_minutes > 0 && tday.spent != null) {
    items.push({ date: tday.date, v: (tday.spent * 60 / tday.usage_minutes) * scale, isToday: true, proj: true });
  }
  return items;
}

function renderCharts(series) {
  const hist = (series && series.days) || [];
  const tday = !today ? null : {
    date: (today.start_of_day || "").slice(0, 10),
    spend: today.projected_spend != null ? today.projected_spend : (today.spent_today || 0),
    proj: today.projected_spend != null,
    usage_minutes: (today.spend_summary || {}).usage_minutes || 0,
    spent: today.spent_today != null ? today.spent_today : 0,
  };
  const SCALE = 100; // cents / minor units per base unit

  renderBar("cSpend", buildSpend(hist, tday), {
    color: "#38bdf8",
    yFmt: axisNum,
    tip: it => (it.isToday ? "today (projected) · " : it.date + " · ") + "spend " + fmtMoney(it.v, cur),
  });
  renderBar("cUsage", buildUsage(hist, tday), {
    color: "#a78bfa",
    yFmt: axisDur,
    tip: it => (it.isToday ? "today (so far) · " : it.date + " · ") + "usage " + fmtDur(it.v),
  });
  renderBar("cCost", buildCost(hist, tday, SCALE), {
    color: "#f472b6",
    yFmt: v => fmtMinorInt(v),
    tip: it => (it.isToday ? "today (so far) · " : it.date + " · ") + (it.v != null ? fmtMinorInt(it.v) + " " + minorUnit(cur) + "/h" : "—"),
  });

  // average chips over complete days only (never the today estimate)
  const done = hist.filter(d => d.spend > 0 || d.usage_minutes > 0);
  if (!done.length) {
    document.getElementById("avgline").innerHTML = '<span class="muted">No complete days of history yet — the bar is today’s projection.</span>';
    return;
  }
  const n = done.length;
  const avgSpend = done.reduce((a, d) => a + d.spend, 0) / n;
  const avgUse = done.reduce((a, d) => a + d.usage_minutes, 0) / n;
  const spendTot = done.reduce((a, d) => a + d.spend, 0);
  const useTot = done.reduce((a, d) => a + d.usage_minutes, 0);
  const cph = useTot > 0 ? (spendTot * 60 / useTot) * SCALE : null;
  const label = state.days === 0 ? "all time since " + hist[0].date : "last " + n + " complete day" + (n === 1 ? "" : "s");
  document.getElementById("avgline").innerHTML =
    "<b>" + label + "</b> · avg spend/day <b>" + fmtMoney(avgSpend, cur) + "</b> · avg usage/day <b>" + fmtDur(avgUse) +
    "</b> · avg cost " + fmtMinorInt(cph) + " " + minorUnit(cur) + "/h";
}

async function loadCharts() {
  const series = await getJSON(DAYS + "?days=" + state.days + "&now=" + encodeURIComponent(localIso()));
  renderCharts(series);
}

document.getElementById("range").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  state.days = RANGES[btn.dataset.days];
  document.querySelectorAll("#range button").forEach(b => b.classList.remove("on"));
  btn.classList.add("on");
  loadCharts().catch(e => { document.getElementById("avgline").innerHTML = '<span class="err">' + e.message + "</span>"; });
});

async function initAll() {
  try { await loadToday(); }
  catch (e) { document.getElementById("heart").innerHTML = '<div class="err">Failed to load: ' + e.message + "</div>"; }
  loadCharts().catch(e => { document.getElementById("avgline").innerHTML = '<span class="err">' + e.message + "</span>"; });
}
initAll();
</script>
</body>
</html>
"""

