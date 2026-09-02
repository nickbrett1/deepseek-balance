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

# A "spend interval" is flagged as unusually high when it exceeds the robust
# baseline (median + SPIKE_MULT * MAD) — and is always at least SPIKE_MIN_RATIO
# times the median, so near-median jitter is never called a spike. The summary
# needs at least MIN_INTERVALS_FOR_BASELINE spent intervals to trust that
# baseline; below that it reports "not enough data" instead of guessing.
SPIKE_MULT = 3.0
SPIKE_MIN_RATIO = 2.0
MIN_INTERVALS_FOR_BASELINE = 10


def _start_of_day_local(dt: datetime) -> datetime:
    """Local midnight for `dt`, expressed as an aware datetime in its tz."""
    tz = dt.tzinfo or UTC
    local = dt.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _mad(values: list[float], center: float) -> float:
    """Median absolute deviation, a robust measure of spread."""
    return _median([abs(v - center) for v in values]) or 0.0


def _minutes_between(a: str, b: str) -> float | None:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60.0
    except ValueError:
        return None


def _collect_drops(rows: list[dict], max_gap_minutes: int) -> list[tuple[datetime, float]]:
    """Per-interval balance declines from a chronological row list.

    Each decline is attributed to the timestamp of the snapshot it ended in
    and only counts when the gap between snapshots is sane (<= max_gap_minutes)
    so that downtime gaps don't masquerade as a single big spend.
    """
    drops: list[tuple[datetime, float]] = []
    for i in range(1, len(rows)):
        prev = rows[i - 1]
        cur = rows[i]
        if prev["total_balance"] is None or cur["total_balance"] is None:
            continue
        gap_min = _minutes_between(prev["ts"], cur["ts"])
        if gap_min is None or gap_min > max_gap_minutes:
            continue
        drop = prev["total_balance"] - cur["total_balance"]
        if drop > 0:
            drops.append((datetime.fromisoformat(cur["ts"]), drop))
    return drops


def _pool_interval_spends(
    rows: list[dict],
    slice_seconds: int,
    max_gap_minutes: int,
    epoch: datetime,
) -> list[float]:
    """Pool positive per-slice spend amounts from a chronological row list.

    Drops are bucketed into `slice_seconds`-wide slices relative to `epoch`,
    summed within each slice (same semantics as the live window), and every
    slice with spend > 0 contributes one pooled "interval" value.
    """
    drops = _collect_drops(rows, max_gap_minutes)
    spend_by_slice: dict[int, float] = {}
    for to_ts, drop in drops:
        idx = math.floor((to_ts - epoch).total_seconds() / slice_seconds)
        spend_by_slice[idx] = spend_by_slice.get(idx, 0.0) + drop
    return [spend for spend in spend_by_slice.values() if spend > 0]


def _day_bucket_stats(
    rows: list[tuple[datetime, float | None]],
    day_start_utc: datetime,
    slice_seconds: int,
    max_gap_minutes: int,
) -> tuple[float, dict[int, float]]:
    """Bucket one complete day's snapshot rows into spend slices.

    Returns ``(total_spend, spend_by_slice)`` for the day. ``rows`` must be the
    snapshots (parsed to aware datetimes) whose timestamps fall inside
    ``[day_start_utc, day_start_utc + 1d)``. Consecutive declines become
    per-interval spend attributed to the slice the decline ended in, so usage
    time (number of spent slices) and cost come from the *same* pool and the
    resulting cost-per-minute is meaningful.
    """
    spend_by_slice: dict[int, float] = {}
    for i in range(1, len(rows)):
        prev_ts, prev_bal = rows[i - 1]
        cur_ts, cur_bal = rows[i]
        if prev_bal is None or cur_bal is None:
            continue
        gap_min = (cur_ts - prev_ts).total_seconds() / 60.0
        if gap_min > max_gap_minutes:
            continue
        drop = prev_bal - cur_bal
        if drop > 0:
            idx = math.floor((cur_ts - day_start_utc).total_seconds() / slice_seconds)
            spend_by_slice[idx] = spend_by_slice.get(idx, 0.0) + drop
    return sum(spend_by_slice.values()), spend_by_slice


def daily_history_series(
    db,
    now: datetime,
    *,
    days: int | None = None,
    spend_slice_minutes: int = 5,
    max_gap_minutes: int = 30,
) -> dict:
    """Per-complete-day spend & usage-time series ending **yesterday** local.

    Each returned day carries the total spend (sum of decline slices within the
    day), the usage time (number of spent slices × slice width — a rough proxy
    for how long the API was actually worked), and the derived
    ``cost_per_minute`` (spend ÷ usage minutes).

    ``days`` is the number of most-recent complete days to return (default: all
    data from the earliest snapshot). Today is deliberately excluded — it is
    partial — and should be shown separately alongside the series. Day
    boundaries and labels are resolved in the caller's local timezone.
    """
    tz = now.tzinfo or UTC
    now_aware = now if now.tzinfo else now.replace(tzinfo=UTC)
    sod_local = _start_of_day_local(now_aware)
    slice_seconds = spend_slice_minutes * 60

    if days is not None and days > 0:
        first_local = sod_local - timedelta(days=days)
    else:
        earliest_ts = db.earliest_ts()
        if earliest_ts is None:
            return {
                "slice_minutes": spend_slice_minutes,
                "start_date": None,
                "end_date": None,
                "days": [],
            }
        earliest = datetime.fromisoformat(earliest_ts).astimezone(tz)
        first_local = _start_of_day_local(earliest)

    last_local = sod_local - timedelta(days=1)  # yesterday = most recent complete day
    if last_local < first_local:
        return {
            "slice_minutes": spend_slice_minutes,
            "start_date": first_local.date().isoformat(),
            "end_date": last_local.date().isoformat(),
            "days": [],
        }

    rows = db.history(
        first_local.astimezone(UTC).isoformat(),
        before_iso=sod_local.astimezone(UTC).isoformat(),
    )
    parsed: list[tuple[datetime, float | None]] = []
    for r in rows:
        try:
            parsed.append((datetime.fromisoformat(r["ts"]), r["total_balance"]))
        except ValueError:
            continue

    out: list[dict] = []
    day = first_local
    while day <= last_local:
        day_utc = day.astimezone(UTC)
        next_utc = day_utc + timedelta(days=1)
        day_rows = [
            (ts, bal) for ts, bal in parsed if day_utc <= ts < next_utc
        ]
        spend, spend_by_slice = _day_bucket_stats(
            day_rows, day_utc, slice_seconds, max_gap_minutes
        )
        usage_minutes = len(spend_by_slice) * spend_slice_minutes
        out.append(
            {
                "date": day.date().isoformat(),
                "ts": day.isoformat(),
                "spend": spend,
                "usage_minutes": usage_minutes,
                "intervals_with_spend": len(spend_by_slice),
                "cost_per_minute": (spend / usage_minutes) if usage_minutes > 0 else None,
            }
        )
        day += timedelta(days=1)

    return {
        "slice_minutes": spend_slice_minutes,
        "start_date": first_local.date().isoformat(),
        "end_date": last_local.date().isoformat(),
        "days": out,
    }


def _history_baseline_values(
    db,
    window_start_utc: datetime,
    baseline_days: int,
    slice_seconds: int,
    max_gap_minutes: int,
) -> list[float]:
    """Pool spent-interval amounts from `baseline_days` complete days before
    `window_start_utc`, to serve as a warm-up baseline when today has too few
    spent intervals to be trusted on its own."""
    if baseline_days <= 0:
        return []
    hist_start = window_start_utc - timedelta(days=baseline_days)
    rows = db.history(hist_start.isoformat(), before_iso=window_start_utc.isoformat())
    return _pool_interval_spends(rows, slice_seconds, max_gap_minutes, hist_start)


def spend_intervals(
    db,
    now: datetime,
    *,
    spend_slice_minutes: int = 5,
    summary_hours: int = 24,
    summary_start_utc: datetime | None = None,
    spike_mult: float = SPIKE_MULT,
    spike_min_ratio: float = SPIKE_MIN_RATIO,
    min_intervals_for_baseline: int = MIN_INTERVALS_FOR_BASELINE,
    normal_band: float = 2.0,
    max_gap_minutes: int = 30,
    baseline_days: int = 14,
) -> dict:
    """Bucket spend into fixed intervals and classify each spent one.

    Returns thresholds plus one entry per spent interval with its start time
    and bucket (`high` / `normal` / `below`, or `None` before enough data).
    The bucket rule uses a robust baseline (median + SPIKE_MULT * MAD) so a
    single unusually large interval is distinguished from lots of ordinary use.

    The window runs for `summary_hours` ending at `now`, unless an explicit
    `summary_start_utc` is given (e.g. the start of today) in which case it
    spans from there to `now`.

    Classification always prefers the pooled spent intervals from the prior
    `baseline_days` complete days — a larger, more robust sample than today's
    handful of intervals. It falls back to today's own spent intervals only
    when there is no prior history to draw on yet, and reports `baseline_source`
    so callers can see which one was used.
    """
    now_utc = now.astimezone(UTC)
    slice_seconds = spend_slice_minutes * 60
    if summary_start_utc is None:
        summary_start_utc = now_utc - timedelta(seconds=summary_hours * 3600)
    else:
        summary_start_utc = summary_start_utc.astimezone(UTC)
    window_hours = (now_utc - summary_start_utc).total_seconds() / 3600.0
    sum_rows = db.history(
        summary_start_utc.isoformat(),
        before_iso=(now_utc + timedelta(seconds=1)).isoformat(),
    )

    # Per-interval drops, attributed to the slice the decline ended in.
    drops = _collect_drops(sum_rows, max_gap_minutes)

    n_slices = math.ceil(
        (now_utc - summary_start_utc).total_seconds() / slice_seconds
    )
    spend_by_slice: dict[int, float] = {}
    for to_ts, drop in drops:
        idx = math.floor((to_ts - summary_start_utc).total_seconds() / slice_seconds)
        if 0 <= idx < n_slices:
            spend_by_slice[idx] = spend_by_slice.get(idx, 0.0) + drop

    # Only slices with actual spend become intervals.
    spent: list[tuple[int, float]] = sorted(
        ((i, spend) for i, spend in spend_by_slice.items() if spend > 0),
        key=lambda pair: pair[0],
    )
    values = [spend for _, spend in spent]
    interval_count = len(values)
    today_median = _median(values)
    today_enough = interval_count >= min_intervals_for_baseline

    # Warm-up baseline from prior complete days, so classification can work
    # early in the day before today has enough spent intervals to be trusted.
    hist_values: list[float] = []
    hist_enough = False
    if baseline_days > 0:
        hist_values = _history_baseline_values(
            db,
            summary_start_utc.astimezone(UTC),
            baseline_days,
            slice_seconds,
            max_gap_minutes,
        )
        hist_enough = len(hist_values) >= min_intervals_for_baseline

    enough_data = today_enough or hist_enough

    # Always prefer the pooled history baseline: it's a larger, more robust
    # sample than today's handful of intervals. Fall back to today's own spent
    # intervals only when there's no history to draw on yet.
    baseline_source = "none"
    median_interval = today_median  # reported even before enough data
    spread_values: list[float] = []
    if hist_enough:
        baseline_source = "history"
        median_interval = _median(hist_values)
        spread_values = hist_values
    elif today_enough:
        baseline_source = "today"
        spread_values = values

    threshold = None
    below_floor = None
    buckets: dict[str, int] = {"high": 0, "normal": 0, "below": 0}
    if enough_data and median_interval is not None:
        spread = _mad(spread_values, median_interval)
        threshold = max(median_interval + spike_mult * spread, median_interval * spike_min_ratio)
        below_floor = median_interval / normal_band

    intervals: list[dict] = []
    for idx, spend in spent:
        bucket = None
        if threshold is not None and below_floor is not None:
            if spend > threshold:
                bucket = "high"
            elif spend < below_floor:
                bucket = "below"
            else:
                bucket = "normal"
            buckets[bucket] += 1
        intervals.append(
            {
                "ts": (summary_start_utc + timedelta(seconds=idx * slice_seconds)).isoformat(),
                "spend": spend,
                "bucket": bucket,
            }
        )

    high_count = buckets["high"]
    def _pct(n: int) -> float | None:
        return (n / interval_count * 100) if interval_count else None

    return {
        "window_hours": window_hours,
        "slice_minutes": spend_slice_minutes,
        "total_slices": n_slices,
        "enough_data": enough_data,
        "min_intervals_for_baseline": min_intervals_for_baseline,
        "baseline_source": baseline_source,
        "hist_interval_count": len(hist_values),
        "hist_median_interval": _median(hist_values),
        "thresholds": {
            "median": median_interval,
            "avg": statistics.mean(values) if values else None,
            "spike_threshold": threshold,
            "below_floor": below_floor,
        },
        "summary": {
            "intervals_with_spend": interval_count,
            "unusually_high_count": high_count,
            "unusually_high_pct": _pct(high_count),
            "normal_count": buckets["normal"],
            "normal_pct": _pct(buckets["normal"]),
            "below_count": buckets["below"],
            "below_pct": _pct(buckets["below"]),
        },
        "intervals": intervals,
    }


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
    summary_hours: int = 24,
    spike_mult: float = SPIKE_MULT,
    spike_min_ratio: float = SPIKE_MIN_RATIO,
    min_intervals_for_baseline: int = MIN_INTERVALS_FOR_BASELINE,
    normal_band: float = 2.0,
    baseline_days: int = 14,
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
    spent_yesterday = None
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
        spend = max(0.0, start_bal - end_bal)
        if d == 1:
            spent_yesterday = spend
        day_spends.append(spend)
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

    # --- spend-interval summary over the last `summary_hours` ---------------
    # Buckets spend into fixed slices, ignores zero-spend slices, then flags
    # any single interval whose spend is a robust statistical outlier (a
    # spike). This separates "one unusually large interval" from "lots of
    # ordinary usage" — the two signals that matter for a balance guard.
    si = spend_intervals(
        db,
        now,
        spend_slice_minutes=spend_slice_minutes,
        summary_start_utc=sod_utc,
        spike_mult=spike_mult,
        spike_min_ratio=spike_min_ratio,
        min_intervals_for_baseline=min_intervals_for_baseline,
        normal_band=normal_band,
        max_gap_minutes=max_gap_minutes,
        baseline_days=baseline_days,
    )
    spend_summary = {
        "window_hours": si["window_hours"],
        "slice_minutes": si["slice_minutes"],
        "total_slices": si["total_slices"],
        "intervals_with_spend": si["summary"]["intervals_with_spend"],
        "usage_minutes": si["summary"]["intervals_with_spend"] * si["slice_minutes"],
        "enough_data": si["enough_data"],
        "min_intervals_for_baseline": si["min_intervals_for_baseline"],
        "baseline_source": si["baseline_source"],
        "hist_interval_count": si["hist_interval_count"],
        "hist_median_interval": si["hist_median_interval"],
        "median_interval_spend": si["thresholds"]["median"],
        "avg_interval_spend": si["thresholds"]["avg"],
        "spike_threshold": si["thresholds"]["spike_threshold"],
        "below_floor": si["thresholds"]["below_floor"],
        "unusually_high_count": si["summary"]["unusually_high_count"],
        "unusually_high_pct": si["summary"]["unusually_high_pct"],
        "normal_count": si["summary"]["normal_count"],
        "normal_pct": si["summary"]["normal_pct"],
        "below_count": si["summary"]["below_count"],
        "below_pct": si["summary"]["below_pct"],
    }

    return {
        "currency": currency,
        "current_balance": current_balance,
        "current_ts": current_ts,
        "start_of_day": sod_local.isoformat(),
        "prev_balance": prev_balance,
        "spent_today": spent_today,
        "spent_yesterday": spent_yesterday,
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
        "spend_summary": spend_summary,
    }
