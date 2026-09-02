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
  .topbar { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  .drill { font-size: 10px; color: #7dd3fc; text-decoration: none; white-space: nowrap; }
  .drill:hover { text-decoration: underline; }
  .muted-line { opacity: .55; }
</style>
</head>
<body>
  <div class="topbar">
    <h1>DeepSeek Balance</h1>
    <a class="drill" id="drill" href="/history" target="_blank" rel="noopener" title="Spend & usage by day">History ↗</a>
  </div>
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
<title>DeepSeek Balance · History</title>
<style>
  :root { color-scheme: dark; }
  html, body { background: #0f172a; }
  body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 18px 20px 30px; font-size: 13px; line-height: 1.4; color: #e2e8f0; }
  a { color: #7dd3fc; }
  h1 { font-size: 15px; margin: 0 0 2px; font-weight: 600; }
  .back { font-size: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; margin: 12px 0; }
  .card { background: rgba(30,41,59,.55); border: 1px solid rgba(148,163,184,.14); border-radius: 8px; padding: 10px 12px; }
  .card .t { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; opacity: .6; }
  .card .v { font-size: 16px; font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 2px; }
  .card .s { font-size: 11px; opacity: .7; margin-top: 2px; font-variant-numeric: tabular-nums; }
  .seg { display: inline-flex; border: 1px solid rgba(148,163,184,.25); border-radius: 6px; overflow: hidden; margin: 10px 0 4px; }
  .seg button { background: transparent; color: #cbd5e1; border: 0; padding: 5px 12px; font-size: 12px; cursor: pointer; }
  .seg button.on { background: #334155; color: #f1f5f9; }
  .legend { font-size: 11px; opacity: .75; margin-bottom: 4px; }
  .legend .sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin: 0 5px 0 12px; vertical-align: -1px; }
  .chartwrap { overflow-x: auto; border: 1px solid rgba(148,163,184,.14); border-radius: 8px; background: rgba(15,23,42,.4); padding: 10px; }
  .muted { opacity: .55; }
  .err { color: #f87171; }
  svg text { font-family: ui-sans-serif, system-ui, sans-serif; }
</style>
</head>
<body>
  <div class="back"><a href="/">&larr; back to today</a></div>
  <h1>DeepSeek Balance · by day</h1>
  <div class="muted" id="sub">Loading…</div>

  <div class="seg" id="range">
    <button data-days="14" class="on">14 days</button>
    <button data-days="30">Last month</button>
    <button data-days="0">All time</button>
  </div>

  <div class="grid" id="cards">
    <div class="card"><div class="t">Spent today</div><div class="v" id="cSpent">—</div><div class="s" id="sSpent">usage 0m · —/min</div></div>
    <div class="card"><div class="t">Balance now</div><div class="v" id="cBal">—</div><div class="s" id="sBal"></div></div>
    <div class="card"><div class="t">Range spend</div><div class="v" id="cTotal">—</div><div class="s" id="sTotal"></div></div>
    <div class="card"><div class="t">Range usage</div><div class="v" id="cUse">—</div><div class="s" id="sUse"></div></div>
    <div class="card"><div class="t">Overall cost / min</div><div class="v" id="cCpm">—</div><div class="s" id="sCpm">usage-weighted avg</div></div>
  </div>

  <div class="legend">
    <span class="sw" style="background:#38bdf8"></span>spend (<span id="legCur">currency</span>, left axis)
    <span class="sw" style="background:#a78bfa"></span>usage time (right axis)
  </div>
  <div class="chartwrap" id="chartwrap"><div class="muted" id="chartMsg">Loading…</div></div>

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
function fmtDur(min) {
  if (min == null) return "—";
  const m = Math.round(Number(min));
  const h = Math.floor(m / 60), mm = m % 60;
  return (h ? h + "h " : "") + mm + "m";
}
function fmtMoney(v, cur) {
  if (v == null) return "—";
  const s = v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
  return s + (cur ? " " + cur : "");
}
function fmtCpm(v, cur) {
  if (v == null) return "—";
  const per = v >= 1 ? v.toFixed(2) : v >= 0.01 ? v.toFixed(3) : v.toExponential(1);
  return per + (cur ? " " + cur : "") + "/min";
}
function fmtRight(min) {
  const m = Math.round(Number(min));
  if (m >= 60) { const h = m / 60; return (h % 1 === 0 ? h : h.toFixed(1)) + "h"; }
  return m + "m";
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

let cur = "";
const state = { days: 14 };

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

async function loadCards() {
  const d = await getJSON(DAILY + "?now=" + encodeURIComponent(localIso()));
  if (d.error || d.current_balance == null) { document.getElementById("sSpent").textContent = "waiting for first snapshot"; return; }
  cur = d.currency || "";
  document.getElementById("legCur").textContent = cur || "currency";
  const ss = d.spend_summary || {};
  const useMin = ss.usage_minutes || 0;
  const cpm = d.spent_today != null && useMin > 0 ? d.spent_today / useMin : null;
  document.getElementById("cSpent").textContent = fmt(d.spent_today, cur);
  document.getElementById("sSpent").textContent = "usage " + fmtDur(useMin) + " · " + fmtCpm(cpm, cur);
  document.getElementById("cBal").textContent = fmt(d.current_balance, cur);
  const t = new Date(d.current_ts);
  document.getElementById("sBal").textContent = "updated " + t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) +
    " · projected " + fmt(d.projected_spend, cur) + " today";
  document.getElementById("sub").textContent =
    "Today's numbers (left cards) refresh live; bars are complete days up to yesterday.";
}

function svgText(x, y, str, anchor, fill, size) {
  return '<text x="' + x + '" y="' + y + '" text-anchor="' + (anchor || "middle") + '" fill="' + (fill || "#94a3b8") + '" font-size="' + (size || 10) + '">' + str + "</text>";
}

function renderChart(data) {
  const box = document.getElementById("chartwrap");
  const days = (data && data.days) || [];
  if (!days.length) {
    box.innerHTML = '<div class="muted">No complete days of data yet — check back after your first full day.</div>';
    return;
  }
  const spendMax = Math.max.apply(null, days.map(d => d.spend)) || 1;
  const useMax = Math.max.apply(null, days.map(d => d.usage_minutes)) || 1;
  const stepL = niceStep(spendMax / 4), topL = Math.ceil(spendMax / stepL) * stepL;
  const stepR = niceStep(useMax / 4), topR = Math.ceil(useMax / stepR) * stepR;

  const n = days.length;
  const padL = 46, padR = 46, padT = 8, padB = 26;
  const slot = n <= 14 ? 42 : n <= 31 ? 30 : Math.max(14, Math.min(22, 2400 / n));
  const width = n * slot + padL + padR;
  const plotH = 220, height = plotH + padT + padB;
  const plotW = width - padL - padR;
  const barW = Math.min(slot * 0.36, 34);
  const y = (frac) => padT + plotH * (1 - frac);

  let s = '<svg viewBox="0 0 ' + width + ' ' + height + '" width="100%" style="min-width:' + width + 'px">';
  const steps = 4;
  for (let k = 0; k <= steps; k++) {
    const f = k / steps;
    const gy = y(f);
    s += '<line x1="' + padL + '" y1="' + gy + '" x2="' + (width - padR) + '" y2="' + gy + '" stroke="rgba(148,163,184,.12)"' + (k === 0 ? ' stroke-width="1.2"' : "") + "/>";
    const leftV = topL * f;
    s += svgText(padL - 6, gy + 3, leftV.toLocaleString(undefined, { maximumFractionDigits: 0 }), "end", "#94a3b8");
    const rightV = topR * f;
    s += svgText(width - padR + 6, gy + 3, fmtRight(rightV), "start", "#a78bfa");
  }

  const labelEvery = Math.max(1, Math.ceil(n / 14));
  for (let i = 0; i < n; i++) {
    const d = days[i];
    const cx = padL + i * slot + slot / 2;
    const sf = d.spend / topL, uf = d.usage_minutes / topR;
    const hS = sf * plotH, hU = uf * plotH;
    const xS = cx - barW - 1.5, xU = cx + 1.5;
    s += '<rect x="' + xS.toFixed(1) + '" y="' + y(sf).toFixed(1) + '" width="' + barW + '" height="' + hS.toFixed(1) + '" fill="#38bdf8" rx="1"><title>' +
      d.date + " spend " + fmtMoney(d.spend, cur) + (d.cost_per_minute != null ? " · " + fmtCpm(d.cost_per_minute, cur) : "") + "</title></rect>";
    s += '<rect x="' + xU.toFixed(1) + '" y="' + y(uf).toFixed(1) + '" width="' + barW + '" height="' + hU.toFixed(1) + '" fill="#a78bfa" rx="1" opacity="0.85"><title>' +
      d.date + " usage " + fmtDur(d.usage_minutes) + "</title></rect>";
    if (i % labelEvery === 0) {
      s += svgText(cx, height - 8, d.date.slice(5));
    }
  }
  s += "</svg>";
  box.innerHTML = s;

  const totSpend = days.reduce((a, d) => a + d.spend, 0);
  const totUse = days.reduce((a, d) => a + d.usage_minutes, 0);
  const daysWithUse = days.filter(d => d.usage_minutes > 0);
  const wCpm = totUse > 0 ? totSpend / totUse : null;
  const wCpmPerDay = wCpm != null && daysWithUse.length
    ? daysWithUse.reduce((a, d) => a + d.cost_per_minute, 0) / daysWithUse.length : null;
  const label = state.days === 0 ? "since " + days[0].date : "last " + days.length + " complete days";
  document.getElementById("cTotal").textContent = fmtMoney(totSpend, cur);
  document.getElementById("sTotal").textContent = label;
  document.getElementById("cUse").textContent = fmtDur(totUse);
  document.getElementById("sUse").textContent = "≈ " + (totUse / 60).toFixed(1) + "h active across " + days.length + " days";
  document.getElementById("cCpm").textContent = fmtCpm(wCpm, cur);
  document.getElementById("sCpm").textContent = "avg day " + fmtCpm(wCpmPerDay, cur) + " · hover bars for daily";
}

async function loadChart() {
  try {
    const data = await getJSON(DAYS + "?days=" + state.days + "&now=" + encodeURIComponent(localIso()));
    renderChart(data);
  } catch (e) {
    document.getElementById("chartwrap").innerHTML = '<div class="err">Failed to load history: ' + e.message + "</div>";
  }
}

document.getElementById("range").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  state.days = RANGES[btn.dataset.days];
  document.querySelectorAll("#range button").forEach(b => b.classList.remove("on"));
  btn.classList.add("on");
  loadChart();
});

loadCards().catch(e => { document.getElementById("sSpent").textContent = "failed to load: " + e.message; });
loadChart();
</script>
</body>
</html>
"""
