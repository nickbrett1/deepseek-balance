"""Daily "heartbeat" analytics for the DeepSeek balance widget.

Turns raw balance snapshots into a compact summary that answers the three
questions the widget cares about instead of a raw trend chart:

1. **Did I spend more than normal today?**  Compares today's spend (annualized
   to a full day) against the median spend of recent complete days.
2. **Where will I end up today?**  Projects today's end-of-day balance by
   extending today's spend pace to the end of the day.
3. **Any abnormal rapid drops?**  Scans the recent window for single-interval
   declines that are both a meaningful percentage and a clear outlier relative
   to the recent median per-interval drop.

All day-boundary logic runs in the caller-supplied local timezone; every DB
query is converted to UTC because the stored snapshot timestamps are UTC.
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta

DAY_SECONDS = 86400.0

# Thresholds (as multiples of the per-slice expected spend) used to bucket a
# bar as under / at / above expectations. Mirrors the widget's pacing pill.
_UNDER = 0.7
_AT = 1.3


def _start_of_day_local(dt: datetime) -> datetime:
    """Local midnight for `dt`, expressed as an aware datetime in its tz."""
    tz = dt.tzinfo or UTC
    local = dt.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _minutes_between(a: str, b: str) -> float | None:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60.0
    except ValueError:
        return None


def daily_heartbeat(
    db,
    now: datetime,
    *,
    rapid_window_minutes: int = 60,
    rapid_min_pct: float = 0.02,
    rapid_mult: float = 2.0,
    max_gap_minutes: int = 30,
    normal_days: int = 14,
    spend_slice_minutes: int = 5,
) -> dict:
    """Compute the daily heartbeat summary relative to the moment `now`.

    `now` must be an aware datetime. Queries against `db` are run in UTC.
    """
    sod_local = _start_of_day_local(now)
    sod_utc = sod_local.astimezone(UTC)
    tomorrow_utc = (sod_local + timedelta(days=1)).astimezone(UTC)
    now_utc = now.astimezone(UTC)

    # --- current + carry-over balance -------------------------------------
    latest = db.latest()
    current_balance = latest["total_balance"] if latest else None
    currency = latest["currency"] if latest else None
    current_ts = latest["ts"] if latest else None

    prev_rows = db.history((sod_utc - timedelta(days=normal_days)).isoformat(), before_iso=sod_utc.isoformat())
    prev_balance = prev_rows[-1]["total_balance"] if prev_rows else None

    # --- today's snapshots (spend + sparkline) -----------------------------
    today_rows = db.history(sod_utc.isoformat(), before_iso=tomorrow_utc.isoformat())
    today_points = [{"ts": r["ts"], "total_balance": r["total_balance"]} for r in today_rows]

    spent_today = None
    if prev_balance is not None and current_balance is not None:
        spent_today = max(0.0, prev_balance - current_balance)
    elif current_balance is not None and today_rows:
        spent_today = max(0.0, today_rows[0]["total_balance"] - current_balance)

    # --- normal daily spend over recent complete days ----------------------
    day_spends: list[float] = []
    for d in range(1, normal_days + 1):
        day_start = sod_utc - timedelta(days=d)
        day_stop = day_start + timedelta(days=1)
        rows = db.history(day_start.isoformat(), before_iso=day_stop.isoformat())
        if not rows:
            continue
        before_rows = db.history((day_start - timedelta(days=1)).isoformat(), before_iso=day_start.isoformat())
        start_bal = before_rows[-1]["total_balance"] if before_rows else rows[0]["total_balance"]
        end_bal = rows[-1]["total_balance"]
        day_spends.append(max(0.0, start_bal - end_bal))
    normal_spend = _median(day_spends)

    # --- projection --------------------------------------------------------
    fraction = max(0.01, min(1.0, (now - sod_local).total_seconds() / DAY_SECONDS))
    projected_spend = (spent_today / fraction) if spent_today is not None else None
    projected_end_balance = None
    if prev_balance is not None and projected_spend is not None:
        projected_end_balance = max(0.0, prev_balance - projected_spend)
    spend_vs_normal = None
    if projected_spend is not None and normal_spend and normal_spend > 0:
        spend_vs_normal = projected_spend / normal_spend

    # --- rapid drops in the recent window ----------------------------------
    window_start_utc = now_utc - timedelta(minutes=rapid_window_minutes)
    recent = db.history(window_start_utc.isoformat(), before_iso=tomorrow_utc.isoformat())
    drops: list[dict] = []
    for i in range(1, len(recent)):
        prev = recent[i - 1]
        cur = recent[i]
        if prev["total_balance"] is None or cur["total_balance"] is None:
            continue
        gap_min = _minutes_between(prev["ts"], cur["ts"])
        if gap_min is None or gap_min > max_gap_minutes:
            continue
        drop = prev["total_balance"] - cur["total_balance"]
        if drop <= 0:
            continue
        drops.append(
            {
                "from_ts": prev["ts"],
                "to_ts": cur["ts"],
                "drop": drop,
                "pct": drop / prev["total_balance"],
                "gap_minutes": gap_min,
            }
        )
    baseline_drop = _median([d["drop"] for d in drops]) or 0.0
    rapid = [
        d
        for d in drops
        if d["pct"] >= rapid_min_pct and d["drop"] >= rapid_mult * baseline_drop
    ]
    largest_rapid = max(rapid, key=lambda d: d["drop"]) if rapid else None

    # --- spend per time slice in the recent window (bar chart data) ---------
    slice_seconds = spend_slice_minutes * 60
    window_duration = max((now_utc - window_start_utc).total_seconds(), slice_seconds)
    n_slices = math.ceil(window_duration / slice_seconds)
    spend_by_slice = [0.0] * n_slices
    for d in drops:
        to_ts = datetime.fromisoformat(d["to_ts"])
        idx = math.floor((to_ts - window_start_utc).total_seconds() / slice_seconds)
        if 0 <= idx < n_slices:
            spend_by_slice[idx] += d["drop"]

    expected_per_slice = (
        (normal_spend * slice_seconds / DAY_SECONDS) if normal_spend else None
    )
    recent_spend: list[dict] = []
    for i in range(n_slices):
        slice_start = window_start_utc + timedelta(seconds=i * slice_seconds)
        spend = spend_by_slice[i]
        expected = expected_per_slice
        # The final slice is usually partial; scale its expectation to the
        # fraction of that slice that has actually elapsed.
        if expected_per_slice is not None:
            elapsed = min((now_utc - slice_start).total_seconds(), slice_seconds)
            expected = expected_per_slice * (elapsed / slice_seconds)
        status = None
        if expected:
            ratio = spend / expected
            status = "at" if ratio <= _AT else "above"
            if ratio < _UNDER:
                status = "under"
        recent_spend.append(
            {
                "ts": slice_start.isoformat(),
                "spend": spend,
                "expected": expected,
                "status": status,
            }
        )

    return {
        "currency": currency,
        "current_balance": current_balance,
        "current_ts": current_ts,
        "start_of_day": sod_local.isoformat(),
        "prev_balance": prev_balance,
        "spent_today": spent_today,
        "normal_spend": normal_spend,
        "spend_vs_normal": spend_vs_normal,
        "projected_spend": projected_spend,
        "projected_end_balance": projected_end_balance,
        "fraction_elapsed": fraction,
        "rapid_window_minutes": rapid_window_minutes,
        "rapid_drops": rapid,
        "rapid_count": len(rapid),
        "largest_rapid": largest_rapid,
        "today_points": today_points,
        "recent_spend": recent_spend,
        "spend_slice_minutes": spend_slice_minutes,
    }
