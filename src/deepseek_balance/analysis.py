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

from . import analytics, heuristics
from .db import BalanceDB
from .phoenix import PhoenixClient

logger = logging.getLogger("deepseek_balance.analysis")

# Tuneable knobs (all env-overridable).
DEFAULT_LOOKBACK_DAYS = 3          # how many complete days back to detect highs
DEFAULT_PHOENIX_PAD_SECONDS = 60   # widen each interval before hitting Phoenix
DEFAULT_MAX_DIAGNOSE_PER_RUN = 25  # cap on Phoenix dives per backfill pass
DEFAULT_MAX_HIGH_TO_RECORD = 200   # cap on intervals recorded per pass


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
    }


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _local_now() -> datetime:
    """Aware 'now' in the container's local timezone (TZ env)."""
    return datetime.now().astimezone()


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
    ) -> None:
        self.db = db
        self.phoenix = phoenix
        self.lookback_days = lookback_days
        self.pad_seconds = pad_seconds
        self.max_diagnose_per_run = max_diagnose_per_run

    # --- detection ---------------------------------------------------------

    def _detect_for_window(self, window_start_utc: datetime, window_end_utc: datetime, day: str) -> list[dict]:
        """Return high intervals in [start, end) as [{start_utc, end_utc, spend, threshold, median}]."""
        kwargs = _summary_kwargs()
        kwargs["spend_slice_minutes"] = _int_env("SPEND_SLICE_MINUTES", 5)
        si = analytics.spend_intervals(
            self.db,
            window_end_utc,
            summary_start_utc=window_start_utc,
            **kwargs,
        )
        highs: list[dict] = []
        slice_sec = si["slice_minutes"] * 60
        threshold = si["thresholds"].get("spike_threshold")
        median = si["thresholds"].get("median")
        for interval in si.get("intervals", []):
            if interval.get("bucket") != "high":
                continue
            start_utc = interval["ts"]
            try:
                end_utc = (
                    datetime.fromisoformat(start_utc) + timedelta(seconds=slice_sec)
                ).isoformat()
            except ValueError:
                end_utc = start_utc
            highs.append(
                {
                    "start_utc": start_utc,
                    "end_utc": end_utc,
                    "spend": interval["spend"],
                    "threshold": threshold,
                    "median": median,
                    "day": day,
                }
            )
        return highs

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
                self._detect_for_window(day_start, day_end, day_label)
            )

        # Today's already-closed intervals (anything whose slice has ended).
        candidates.extend(self._detect_for_window(today_utc, now_utc, today_local.date().isoformat()))

        recorded: list[dict] = []
        for c in candidates[: DEFAULT_MAX_HIGH_TO_RECORD]:
            self.db.record_high_interval(
                start_utc=c["start_utc"],
                end_utc=c["end_utc"],
                slice_minutes=_int_env("SPEND_SLICE_MINUTES", 5),
                spend=c["spend"],
                spike_threshold=c["threshold"],
                median_interval=c["median"],
                day=c["day"],
                detected_at=detected_at,
            )
            recorded.append(self.db._high_row(c["start_utc"]))
        return [r for r in recorded if r]

    # --- diagnosis ---------------------------------------------------------

    def _diagnose(self, high: dict) -> dict | None:
        """Query Phoenix over the (widened) interval and classify it."""
        if self.phoenix is None:
            return None
        try:
            spans = self.phoenix.fetch_llm_spans(
                high["start_utc"], high["end_utc"], pad_seconds=self.pad_seconds
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:  # leave unanalyzed
            logger.warning("Phoenix fetch failed for %s: %s", high["start_utc"], exc)
            return None
        diag = heuristics.diagnose(spans, window_spend=high["spend"] or 0.0)
        return diag

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
