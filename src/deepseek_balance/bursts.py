"""Burst assembly + burst-level reconciliation for the spike detector.

The detector's first pass scans fixed ``slice_minutes`` slices of balance spend
and flags each *slice* that is a robust statistical outlier. Classifying those
slices in isolation is the defect this layer fixes: a single burst of spend can
straddle a slice boundary and the balance meter settles with a lag, so the
traced cost lands mostly in one slice while the money is split across two — an
over-attributed slice next to an under-attributed one that the slice-local
classifier loudly reports as "unexplained".

This module keeps the cheap slice scan and adds two things on top of it:

- **assembly** — coalesce high slices into maximal *bursts*: a run of high
  slices no more than ``gap_slices`` apart (so one quiet slice inside a burst
  does not split it), edge-expanded by one slice on each side when that
  neighbouring slice still carries spend above ``below_floor`` (folding the
  residual settle-tail into the burst that caused it), and dropped when the
  run's combined delta does not reach ``spike_threshold``; and
- **attribution** — assign each LLM span to exactly one burst, by ``start_time``
  within a lag-padded window, resolving any overlap to the nearest burst so cost
  is never double counted between bursts.

Nothing here changes the slice cadence, the balance sampling or the thresholds;
a burst is a *view* over slices the existing scan already produced.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import phoenix

# One quiet slice inside a burst does not split it.
DEFAULT_GAP_SLICES = 1
# The meter's observed settling lag is ~1 slice (see the memo's 2026-09-15 case).
DEFAULT_LAG_SLICES = 1


def _start(slice_row: dict) -> str | None:
    return slice_row.get("start_utc") or slice_row.get("ts")


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def assemble_bursts(
    slices: list[dict],
    *,
    slice_minutes: int = 5,
    gap_slices: int = DEFAULT_GAP_SLICES,
    spike_threshold: float | None = None,
    below_floor: float | None = None,
    lag_slices: int = DEFAULT_LAG_SLICES,
) -> list[dict]:
    """Coalesce flagged slices into bursts.

    ``slices`` is the detector's spent-interval list (each ``{ts, spend,
    bucket}``); only ``bucket == "high"`` slices seed a burst, but a spent,
    non-high slice is used to bridge a run (up to ``gap_slices`` of them) and to
    expand a burst's edges when its spend is above ``below_floor``.

    Returns the bursts oldest-first, each ``{burst_id, start_utc, end_utc,
    spend, member_slices, member_slice_count, lag_slices}``. ``burst_id`` is the
    burst's start timestamp — stable for the same input, which is what makes a
    re-run idempotent.
    """
    spent: list[tuple[datetime, dict]] = []
    for s in slices:
        iso = _start(s)
        if not iso or s.get("spend") is None:
            continue
        try:
            spent.append((_parse(iso), s))
        except (TypeError, ValueError):
            continue
    spent.sort(key=lambda pair: pair[0])
    if not spent:
        return []

    slice_seconds = slice_minutes * 60
    origin = spent[0][0]
    index_of: dict[int, dict] = {}
    for ts, s in spent:
        idx = round((ts - origin).total_seconds() / slice_seconds)
        index_of[idx] = s

    high_idx = sorted(
        idx for idx, s in index_of.items() if s.get("bucket") == "high"
    )
    if not high_idx:
        return []

    # Stage 2a — coalesce high slices no more than `gap_slices` apart.
    groups: list[list[int]] = [[high_idx[0]]]
    for idx in high_idx[1:]:
        if idx - groups[-1][-1] <= gap_slices + 1:
            groups[-1].append(idx)
        else:
            groups.append([idx])

    bursts: list[dict] = []
    for group in groups:
        # Every slice in the run — including the quiet slices bridged across —
        # belongs to the burst, so its money is reconciled with the event.
        members = set(range(group[0], group[-1] + 1))
        # Stage 2b — absorb one settle-tail slice on each side when it still
        # carries spend above the "below normal" floor.
        if below_floor is not None:
            for edge in (group[0] - 1, group[-1] + 1):
                neighbour = index_of.get(edge)
                if (
                    neighbour is not None
                    and edge not in members
                    and (neighbour.get("spend") or 0.0) > below_floor
                ):
                    members.add(edge)
        ordered = [i for i in sorted(members) if i in index_of]
        member_slices = [
            {"start_utc": _start(index_of[i]), "spend": index_of[i].get("spend")}
            for i in ordered
        ]
        total = sum(m["spend"] or 0.0 for m in member_slices)
        if spike_threshold is not None and total < spike_threshold:
            continue
        end_dt = _parse(member_slices[-1]["start_utc"]) + timedelta(seconds=slice_seconds)
        bursts.append(
            {
                "burst_id": member_slices[0]["start_utc"],
                "start_utc": member_slices[0]["start_utc"],
                "end_utc": end_dt.isoformat(),
                "spend": total,
                "member_slices": member_slices,
                "member_slice_count": len(member_slices),
                "lag_slices": lag_slices,
            }
        )
    return bursts


def _window(burst: dict, lag_seconds: float) -> tuple[datetime, datetime, datetime, datetime]:
    """(padded_start, padded_end, core_start, core_end) for a burst."""
    core_start = _parse(burst["start_utc"])
    core_end = _parse(burst["end_utc"])
    return (
        core_start - timedelta(seconds=lag_seconds),
        core_end + timedelta(seconds=lag_seconds),
        core_start,
        core_end,
    )


def assign_spans(
    spans: list[dict],
    bursts: list[dict],
    *,
    lag_slices: int = DEFAULT_LAG_SLICES,
    slice_minutes: int = 5,
) -> tuple[dict[int, list[dict]], list[dict]]:
    """Partition spans among bursts by ``start_time`` — nearest burst wins.

    Each burst owns the spans whose ``start_time`` falls in its lag-padded
    window ``[start - lag, end + lag)``. Where two padded windows overlap, the
    span goes to the burst whose *core* interval is nearest, so a span belongs
    to exactly one burst and ``Σ burst_span_cost`` never exceeds the trace
    total. Spans with no timestamp are returned as unassigned (they cannot be
    placed on the timeline).

    Returns ``({burst_index: [spans]}, unassigned_spans)``.
    """
    lag_seconds = lag_slices * slice_minutes * 60
    windows = [_window(b, lag_seconds) for b in bursts]
    owned: dict[int, list[dict]] = {i: [] for i in range(len(bursts))}
    unassigned: list[dict] = []
    for span in spans:
        metrics = phoenix.span_metrics(span)
        ts = metrics.get("start_time")
        if not ts:
            unassigned.append(span)
            continue
        try:
            when = _parse(ts)
        except (TypeError, ValueError):
            unassigned.append(span)
            continue
        best_index: int | None = None
        best_distance: float | None = None
        for i, (padded_start, padded_end, core_start, core_end) in enumerate(windows):
            if not (padded_start <= when < padded_end):
                continue
            if core_start <= when < core_end:
                distance = 0.0
            else:
                distance = min(
                    abs((when - core_start).total_seconds()),
                    abs((when - core_end).total_seconds()),
                )
            if best_distance is None or distance < best_distance:
                best_index, best_distance = i, distance
        if best_index is None:
            unassigned.append(span)
        else:
            owned[best_index].append(span)
    return owned, unassigned


def reconcile_day(
    slices: list[dict],
    spans: list[dict],
    *,
    slice_minutes: int = 5,
    gap_slices: int = DEFAULT_GAP_SLICES,
    spike_threshold: float | None = None,
    below_floor: float | None = None,
    lag_slices: int = DEFAULT_LAG_SLICES,
) -> tuple[list[dict], dict[int, list[dict]], list[dict]]:
    """Assemble a day's bursts and attribute every span to exactly one of them.

    A convenience wrapper over :func:`assemble_bursts` + :func:`assign_spans`
    used by the offline replay and the day-level invariant test.
    """
    day_bursts = assemble_bursts(
        slices,
        slice_minutes=slice_minutes,
        gap_slices=gap_slices,
        spike_threshold=spike_threshold,
        below_floor=below_floor,
        lag_slices=lag_slices,
    )
    owned, unassigned = assign_spans(
        spans, day_bursts, lag_slices=lag_slices, slice_minutes=slice_minutes
    )
    return day_bursts, owned, unassigned
