"""Analysis: explain unusually-high spend intervals via Phoenix traces.

Ties the three pieces together:

1. **Detection** — reuse ``analytics.spend_intervals`` (the same robust-MAD
   spike rule the widget uses) over each recent *complete day* plus the closed
   intervals of today, and record every flagged interval into the DB's
   ``high_intervals`` registry. Detection runs in the **server's local time**,
   the canonical timezone (matching the MCP server), and stores intervals with
   UTC bounds.
2. **Diagnosis** — for each recorded high interval that does not yet have a
   diagnosis, widen the window slightly and pull Phoenix LLM spans, then run
   the heuristics to classify the likely cause (or flag it for investigation).
3. **Backfill** — a single pass catches up on anything within the lookback
   window, so the table has history to show from day one.

The analysis is deliberately *not* in the poller's hot path: it runs on its own
low-frequency schedule and/or on demand (see the ``/analysis/backfill``
endpoint), and each run caps how many intervals it diagnoses to bound the
volume of Phoenix queries.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import httpx

from . import analytics, bursts, heuristics
from .db import BalanceDB
from .phoenix import PhoenixClient

logger = logging.getLogger("deepseek_balance.analysis")

# Tuneable knobs (all env-overridable).
DEFAULT_LOOKBACK_DAYS = 3          # how many complete days back to detect highs
DEFAULT_PHOENIX_PAD_SECONDS = 60   # widen each interval before hitting Phoenix
DEFAULT_MAX_DIAGNOSE_PER_RUN = 25  # cap on Phoenix dives per backfill pass
DEFAULT_MAX_HIGH_TO_RECORD = 200   # cap on intervals recorded per pass
# Burst reconciliation: one quiet slice inside a burst does not split it, and
# the meter's observed settling lag is allowed for when attributing spans.
DEFAULT_BURST_GAP_SLICES = 1
DEFAULT_ATTRIBUTION_LAG_SLICES = 1
# Fragments of one event (a flush split over snapshots) closer than this join.
DEFAULT_BURST_MERGE_GAP_SLICES = bursts.DEFAULT_MERGE_GAP_SLICES


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _summary_kwargs() -> dict:
    """The spend-interval tuning used for detection, from the environment."""
    return {
        "spend_slice_minutes": _int_env("SPEND_SLICE_MINUTES", 5),
        "spike_mult": _float_env("SPIKE_MULT", 3.0),
        "spike_min_ratio": _float_env("SPIKE_MIN_RATIO", 2.0),
        "min_intervals_for_baseline": _int_env("MIN_INTERVALS_FOR_BASELINE", 10),
        "normal_band": _float_env("NORMAL_BAND", 2.0),
        "max_gap_minutes": _int_env("MAX_GAP_MINUTES", 30),
        "baseline_days": _int_env("BASELINE_DAYS", 14),
        "burst_gap_slices": _int_env("BURST_GAP_SLICES", bursts.DEFAULT_GAP_SLICES),
    }


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _local_now() -> datetime:
    """Aware 'now' in the container's local timezone (TZ env)."""
    return datetime.now().astimezone()


def _shift(iso: str, seconds: float) -> str:
    """Shift an ISO timestamp by ``seconds`` (tz-aware), returning ISO."""
    dt = datetime.fromisoformat(iso)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _start_of_day(dt: datetime) -> datetime:
    tz = dt.tzinfo or UTC
    local = dt.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


class AnalysisService:
    """Discovers + explains high spend intervals from balance + Phoenix data."""

    def __init__(
        self,
        db: BalanceDB,
        phoenix: PhoenixClient | None,
        *,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        pad_seconds: int = DEFAULT_PHOENIX_PAD_SECONDS,
        max_diagnose_per_run: int = DEFAULT_MAX_DIAGNOSE_PER_RUN,
        burst_gap_slices: int | None = None,
        attribution_lag_slices: int | None = None,
        burst_merge_gap_slices: int | None = None,
    ) -> None:
        self.db = db
        self.phoenix = phoenix
        self.lookback_days = lookback_days
        self.pad_seconds = pad_seconds
        self.max_diagnose_per_run = max_diagnose_per_run
        self.burst_gap_slices = (
            burst_gap_slices
            if burst_gap_slices is not None
            else _int_env("BURST_GAP_SLICES", DEFAULT_BURST_GAP_SLICES)
        )
        self.attribution_lag_slices = (
            attribution_lag_slices
            if attribution_lag_slices is not None
            else _int_env("ATTRIBUTION_LAG_SLICES", DEFAULT_ATTRIBUTION_LAG_SLICES)
        )
        self.burst_merge_gap_slices = (
            burst_merge_gap_slices
            if burst_merge_gap_slices is not None
            else _int_env("BURST_MERGE_GAP_SLICES", DEFAULT_BURST_MERGE_GAP_SLICES)
        )

    # --- detection ---------------------------------------------------------

    def _detect_bursts_for_window(
        self, window_start_utc: datetime, window_end_utc: datetime, day: str
    ) -> list[dict]:
        """Detect bursts in [start, end).

        Keeps the cheap slice scan (``analytics.spend_intervals``) and folds the
        flagged slices into bursts (a run of high slices up to ``gap_slices``
        apart, edge-expanded into a settle-tail slice, dropped below the spike
        threshold). Returns one dict per burst with the bounds, merged spend and
        the member-slice count the reconciler/classifier need.
        """
        kwargs = _summary_kwargs()
        kwargs["spend_slice_minutes"] = _int_env("SPEND_SLICE_MINUTES", 5)
        si = analytics.spend_intervals(
            self.db,
            window_end_utc,
            summary_start_utc=window_start_utc,
            **kwargs,
        )
        slice_minutes = si["slice_minutes"]
        threshold = si["thresholds"].get("spike_threshold")
        below_floor = si["thresholds"].get("below_floor")
        median = si["thresholds"].get("median")
        detected = bursts.assemble_bursts(
            si.get("intervals", []),
            slice_minutes=slice_minutes,
            gap_slices=self.burst_gap_slices,
            spike_threshold=threshold,
            below_floor=below_floor,
            lag_slices=self.attribution_lag_slices,
            merge_gap_slices=self.burst_merge_gap_slices,
        )
        for b in detected:
            b["slice_minutes"] = slice_minutes
            b["threshold"] = threshold
            b["median"] = median
            b["day"] = day
        return detected

    def _recorded(self, now: datetime) -> list[dict]:
        """Find and persist high intervals over the lookback window.

        Returns every recorded high interval (newly recorded + already known)
        as a flat list of high_interval rows, capped for safety.
        """
        tz = now.tzinfo or UTC
        today_local = _start_of_day(now)
        today_utc = today_local.astimezone(UTC)
        now_utc = now.astimezone(UTC)
        detected_at = now_utc.isoformat()

        candidates: list[dict] = []
        # Complete days: yesterday back `lookback_days` days.
        for offset in range(self.lookback_days, 0, -1):
            day_start = today_utc - timedelta(days=offset)
            day_end = day_start + timedelta(days=1)
            local_day = day_start.astimezone(tz)
            day_label = local_day.date().isoformat()
            candidates.extend(
                self._detect_bursts_for_window(day_start, day_end, day_label)
            )

        # Today's already-closed intervals (anything whose slice has ended).
        candidates.extend(
            self._detect_bursts_for_window(today_utc, now_utc, today_local.date().isoformat())
        )

        recorded: list[dict] = []
        for c in candidates[: DEFAULT_MAX_HIGH_TO_RECORD]:
            self.db.record_high_interval(
                start_utc=c["start_utc"],
                end_utc=c["end_utc"],
                slice_minutes=c.get("slice_minutes") or _int_env("SPEND_SLICE_MINUTES", 5),
                spend=c["spend"],
                spike_threshold=c["threshold"],
                median_interval=c["median"],
                day=c["day"],
                detected_at=detected_at,
                burst_id=c.get("burst_id") or c["start_utc"],
                member_slice_count=c.get("member_slice_count"),
                lag_slices=c.get("lag_slices", self.attribution_lag_slices),
            )
            recorded.append(self.db._high_row(c["start_utc"]))
        return [r for r in recorded if r]

    # --- diagnosis ---------------------------------------------------------

    def _prior_window(self, high: dict) -> tuple[str, str] | None:
        """The one-snapshot-interval window immediately before this one.

        Balances are polled on a grid and cent-quantized, so a burst's charges
        can settle in the following, otherwise-idle window. The lookback is
        deliberately exactly one ``slice_minutes`` wide (never wider — a longer
        lookback absorbs genuinely idle periods and manufactures attributions).
        """
        slice_minutes = high.get("slice_minutes") or _int_env("SPEND_SLICE_MINUTES", 5)
        try:
            start = datetime.fromisoformat(high["start_utc"])
        except (TypeError, ValueError):
            return None
        prior_start = start - timedelta(minutes=slice_minutes)
        return prior_start.isoformat(), start.isoformat()

    def _reconciliation_pair(
        self, high: dict, lag_seconds: float
    ) -> tuple[float, dict, dict] | None:
        """The snapshot pair whose movement this burst's traced cost explains.

        The traced cost covers the lag-padded window, so the balance movement
        it is reconciled against must too: from the snapshot opening the burst
        to the snapshot closing it **plus the settling lag**
        (``[burst_start, burst_end + lag]``). Using only the member slices'
        own delta — the old denominator — understated the movement whenever a
        flush was spread over several snapshots and reported a spurious
        ``over_attributed`` (see the reconciliation memo).

        Returns ``(drop, start_row, end_row)`` or ``None`` when either grid slot
        has no snapshot (the caller then falls back to the member-slice delta).
        """
        start = self.db.snapshot_at(high["start_utc"])
        end_slot = _shift(high["end_utc"], lag_seconds)
        end = self.db.snapshot_at(end_slot)
        if not start or not end:
            return None
        if start["total_balance"] is None or end["total_balance"] is None:
            return None
        return start["total_balance"] - end["total_balance"], start, end

    def _attach_balance_pair(self, diag: dict, high: dict, lag_seconds: float) -> None:
        """Record the snapshot pair whose drop this window explains (T2).

        The pair is the *reconciliation* span — the burst's opening snapshot and
        the snapshot one settling-lag after its close — so its delta is exactly
        the ``window_drop`` the ratio divides into and the settlement lag is
        visible in the row rather than inferable from a separate history call.
        """
        pair = self._reconciliation_pair(high, lag_seconds)
        if pair is not None:
            _, start, end = pair
            diag["balance_start_ts"] = start["ts"]
            diag["balance_start"] = start["total_balance"]
            diag["balance_end_ts"] = end["ts"]
            diag["balance_end"] = end["total_balance"]
        else:
            # No reconciliation pair: fall back to the member-slice bracketing
            # snapshots so the row still carries a pair where one exists.
            start = self.db.snapshot_at(high["start_utc"])
            end = self.db.snapshot_at(high["end_utc"])
            diag["balance_start_ts"] = start["ts"] if start else None
            diag["balance_start"] = start["total_balance"] if start else None
            diag["balance_end_ts"] = end["ts"] if end else None
            diag["balance_end"] = end["total_balance"] if end else None
        # Keep the referenced-burst keys present on every row, so a plain
        # "unexplained" window and a lookback-attributed one have the same shape.
        diag.setdefault("prior_burst_start_utc", None)
        diag.setdefault("prior_burst_end_utc", None)
        diag.setdefault("prior_burst_reason", None)

    def _lag_slices(self, high: dict) -> int:
        """Attribution lag for a burst row.

        A recorded burst carries its own ``lag_slices`` (stamped at detection).
        A bare interval dict with no burst identity — e.g. a slice diagnosed on
        its own — is reconciled without a lag allowance.
        """
        if high.get("lag_slices") is not None:
            return int(high["lag_slices"])
        if high.get("burst_id"):
            return self.attribution_lag_slices
        return 0

    def _owned_spans(self, spans: list[dict], high: dict, lag_slices: int) -> list[dict]:
        """Keep only the spans this burst owns when lag windows overlap.

        With a lag allowance two neighbouring bursts' padded windows can
        overlap. Every span is attributed to the nearest burst (by start time)
        so ``Σ burst_span_cost`` over a day never exceeds the trace total.
        """
        if lag_slices <= 0 or not spans:
            return spans
        slice_minutes = high.get("slice_minutes") or _int_env("SPEND_SLICE_MINUTES", 5)
        lag_seconds = lag_slices * slice_minutes * 60
        start = _shift(high["start_utc"], -lag_seconds)
        end = _shift(high["end_utc"], lag_seconds)
        neighbours = self.db.high_intervals_between(start, end)
        if len(neighbours) <= 1:
            return spans
        windows = [
            {"start_utc": r["start_utc"], "end_utc": r["end_utc"]} for r in neighbours
        ]
        owned, _ = bursts.assign_spans(
            spans, windows, lag_slices=lag_slices, slice_minutes=slice_minutes
        )
        for i, w in enumerate(windows):
            if w["start_utc"] == high["start_utc"]:
                return owned.get(i, [])
        return spans

    def _diagnose(self, high: dict) -> dict | None:
        """Query Phoenix over the (lag-widened) window and classify it."""
        if self.phoenix is None:
            return None
        lag_slices = self._lag_slices(high)
        slice_minutes = high.get("slice_minutes") or _int_env("SPEND_SLICE_MINUTES", 5)
        lag_seconds = lag_slices * slice_minutes * 60
        fetch_start = _shift(high["start_utc"], -lag_seconds)
        fetch_end = _shift(high["end_utc"], lag_seconds)
        try:
            spans = self.phoenix.fetch_llm_spans(
                fetch_start, fetch_end, pad_seconds=self.pad_seconds
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:  # leave unanalyzed
            logger.warning("Phoenix fetch failed for %s: %s", high["start_utc"], exc)
            return None
        spans = self._owned_spans(spans, high, lag_slices)
        member_spend = high["spend"] or 0.0
        # Denominator = the drop over the traced (lag-padded) window, not the
        # member slices' own delta. Only fall back to the member delta when the
        # grid has no snapshot at one end of the reconciliation span.
        pair = self._reconciliation_pair(high, lag_seconds)
        window_spend = pair[0] if pair is not None else member_spend
        diag = heuristics.diagnose(
            spans,
            window_spend=window_spend,
            window_start_utc=high["start_utc"],
            window_end_utc=high["end_utc"],
            burst_id=high.get("burst_id"),
            burst_start_utc=high["start_utc"],
            burst_end_utc=high["end_utc"],
            member_slice_count=high.get("member_slice_count") or 1,
            lag_slices=lag_slices,
            burst_spend=member_spend,
        )

        # One-interval lookback: an empty window may be a lagged settlement of
        # the preceding interval's burst (T1). Only attempted when the window
        # truly has no LLM traffic, so the populated paths — including the
        # small-drop `cent_quantized` case, which needs spans to exist — are
        # never reclassified as lag.
        if diag["reason"] == "unexplained" and diag["request_count"] == 0:
            prior = self._prior_window(high)
            if prior is not None:
                prior_start, prior_end = prior
                try:
                    prior_spans = self.phoenix.fetch_llm_spans(
                        prior_start, prior_end, pad_seconds=self.pad_seconds
                    )
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    logger.warning(
                        "Phoenix lookback fetch failed for %s: %s", high["start_utc"], exc
                    )
                    prior_spans = []
                look = heuristics.diagnose_lookback(
                    prior_spans,
                    window_spend=window_spend,
                    prior_start_utc=prior_start,
                    prior_end_utc=prior_end,
                )
                if look is not None:
                    diag = look

        # Keep the reconciliation fields coherent however the reason was reached
        # (including the lookback path, which builds its own diagnosis dict).
        diag["window_drop"] = window_spend
        diag["traced_cost"] = diag.get("reconciled_cost")
        diag["burst_spend"] = member_spend
        self._attach_balance_pair(diag, high, lag_seconds)
        return diag

    def redo(self, *, now: datetime | None = None) -> dict:
        """Wipe the recorded high-interval analysis and recompute it from scratch.

        Deletes every recorded high interval and its diagnosis (balance
        snapshots are left intact), then runs a full detection + diagnosis pass.
        Use this after a reconciliation change (e.g. the window-attribution
        fix) to regenerate the "investigate" table with the new logic against
        the same, still-good balance history. Returns the run report plus what
        was cleared.
        """
        now = now or _local_now()
        cleared = self.db.clear_analyses()
        report = self.run(now=now)
        report["cleared"] = cleared
        return report

    def run(self, *, now: datetime | None = None) -> dict:
        """One full pass: record highs over the lookback, then diagnose the new
        ones. Returns a small report dict for logging / the on-demand endpoint."""
        now = now or _local_now()
        recorded = self._recorded(now)
        now_utc = now.astimezone(UTC)

        # Only diagnose intervals within the Phoenix-reachable lookback window
        # and that don't already have a diagnosis (idempotent across runs).
        reach_start = (now_utc - timedelta(days=self.lookback_days)).isoformat()
        unanalyzed = self.db.high_intervals_unanalyzed(
            reach_start, limit=self.max_diagnose_per_run
        )
        diagnosed = 0
        for high in unanalyzed:
            diag = self._diagnose(high)
            if diag is None:
                continue  # transient phoenix failure — retried next pass
            self.db.upsert_diagnostic(start_utc=high["start_utc"], diag=diag)
            diagnosed += 1

        return {
            "lookback_days": self.lookback_days,
            "high_intervals_known": len(recorded),
            "newly_diagnosed": diagnosed,
            "remaining_unanalyzed": len(self.db.high_intervals_unanalyzed(reach_start)),
            "phoenix_configured": self.phoenix is not None,
        }
