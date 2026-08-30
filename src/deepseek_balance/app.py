"""deepseek-balance FastAPI application.

Serves `/`, `/balance/latest`, `/balance/history` and `/health`. Starts the
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

from .db import BalanceDB
from .poller import BalancePoller, parse_interval

logger = logging.getLogger("deepseek_balance.app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_PORT = 3000
DEFAULT_INTERVAL = "15m"


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


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


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return CHART_HTML


CHART_HTML = """<!DOCTYPE html>
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
  .chart-title { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; opacity: .6; margin: 8px 0 2px; }
  .error { color: #f87171; padding: 8px; }
</style>
</head>
<body>
  <h1>DeepSeek Balance</h1>
  <div class="meta" id="meta">Loading…</div>
  <div class="balance" id="balance">—</div>
  <div class="sub" id="sub"></div>
  <div class="chart-title">Balance over time</div>
  <div id="chart">Loading…</div>
<script>
const LATEST = "/balance/latest";
const HISTORY = "/balance/history?hours=24&step=15m";

async function load() {
  try {
    const [lr, hr] = await Promise.all([
      fetch(LATEST, { cache: "no-store" }),
      fetch(HISTORY, { cache: "no-store" }),
    ]);
    if (!lr.ok) throw new Error("HTTP " + lr.status);
    const latest = await lr.json();
    const hist = hr.ok ? await hr.json() : { points: [] };
    renderHeader(latest);
    renderChart(hist.points || []);
  } catch (e) {
    document.getElementById("chart").innerHTML = '<div class="error">Failed to load: ' + e.message + "</div>";
  }
}

function fmt(v) {
  return (v == null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
}

function renderHeader(latest) {
  const el = document.getElementById("balance");
  if (latest.error || latest.total_balance == null) {
    el.textContent = "No data yet";
    return;
  }
  el.innerHTML = fmt(latest.total_balance) + " <small>" + (latest.currency || "") + "</small>";
  document.getElementById("sub").textContent =
    "granted " + fmt(latest.granted_balance) + " · topped up " + fmt(latest.topped_up_balance);
  const d = new Date(latest.ts);
  document.getElementById("meta").textContent =
    "Updated " + (d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
    d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }));
}

function renderChart(points) {
  if (points.length < 2) {
    document.getElementById("chart").innerHTML = '<div class="meta">Not enough data yet</div>';
    return;
  }
  const W = 320, H = 84, padL = 6, padR = 6, padT = 8, padB = 16;
  const vals = points.map((p) => p.total_balance);
  const maxV = Math.max(...vals), minV = Math.min(...vals);
  const range = Math.max(maxV - minV, 1);
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const x = (i) => padL + (points.length === 1 ? plotW / 2 : (i * plotW) / (points.length - 1));
  const y = (v) => padT + ((maxV - v) / range) * plotH;
  const pts = points.map((p, i) => x(i).toFixed(1) + "," + y(p.total_balance).toFixed(1));
  let area = "M" + pts[0];
  for (let i = 1; i < pts.length; i++) area += " L" + pts[i];
  area += " L" + x(points.length - 1).toFixed(1) + "," + (padT + plotH).toFixed(1) +
          " L" + x(0).toFixed(1) + "," + (padT + plotH).toFixed(1) + " Z";
  let circles = "";
  let labels = "";
  points.forEach((p, i) => {
    const cx = x(i).toFixed(1), cy = y(p.total_balance).toFixed(1);
    circles += '<circle cx="' + cx + '" cy="' + cy + '" r="2.2" fill="#60a5fa">'
      + "<title>" + new Date(p.ts).toLocaleString() + ": " + fmt(p.total_balance) + "</title></circle>";
    if (i === 0 || i === points.length - 1 || i % 4 === 0) {
      const t = new Date(p.ts);
      labels += '<text x="' + cx + '" y="' + (H - 4) + '" font-size="8" text-anchor="middle" opacity=".55">'
        + t.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) + "</text>";
    }
  });
  const svg = '<svg viewBox="0 0 ' + W + " " + H + '" style="width:100%;max-width:420px" preserveAspectRatio="xMidYMid meet">'
    + '<path d="' + area + '" fill="rgba(96,165,250,.12)" stroke="none"/>'
    + '<polyline points="' + pts.join(" ") + '" fill="none" stroke="#60a5fa" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>'
    + circles + labels + "</svg>";
  document.getElementById("chart").innerHTML = svg;
}

load();
</script>
</body>
</html>
"""
