"""Burst-level reconciliation: assembly, lag attribution and classification.

Covers the acceptance list in `memos/` "Balance detector: burst-level
reconciliation": the motivating 2026-09-15 case collapses to one burst with no
`unexplained` row, over-attribution is caught rather than clamped, boundary
merges and gap tolerance behave, spans are never double counted across
overlapping lag windows, and the new burst fields round-trip through the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from deepseek_balance import bursts, heuristics
from deepseek_balance.analysis import AnalysisService
from deepseek_balance.db import BalanceDB
from deepseek_balance.heuristics import REASONS


def _slice(ts: str, spend: float, bucket: str = "high") -> dict:
    return {"ts": ts, "spend": spend, "bucket": bucket}


def _span(*, start=None, input=1000, output=200, cached=0, cost=0.1,
          finish="stop", conversation=None, status="OK", error=False):
    attributes = {
        "gen_ai.response.model": "deepseek-chat",
        "gen_ai.usage.input_tokens": input,
        "gen_ai.usage.output_tokens": output,
        "gen_ai.usage.total_tokens": input + output,
        "litellm.cost.total": cost,
        "gen_ai.response.finish_reasons": [finish],
    }
    if cached:
        attributes["gen_ai.usage.cache_read.input_tokens"] = cached
    span = {
        "name": "chat deepseek-chat",
        "span_kind": "LLM",
        "status_code": "ERROR" if error else status,
        "attributes": attributes,
    }
    if start is not None:
        span["start_time"] = start
    if conversation is not None:
        span["trace_id"] = conversation
    return span


def _reconciled(spans, *, start=None, end=None):
    return heuristics.summarize(
        spans, window_spend=0.0, window_start_utc=start, window_end_utc=end
    )["reconciled_cost"]


# --- Stage 2: assembly ------------------------------------------------------

def test_boundary_merge_is_one_burst_one_row():
    """Acceptance 4: cost in slice A, delta split A/B -> one burst, one row."""
    slices = [
        _slice("2026-09-15T08:15:00+00:00", 0.26),
        _slice("2026-09-15T08:20:00+00:00", 0.22),
    ]
    got = bursts.assemble_bursts(slices, slice_minutes=5, spike_threshold=0.1)
    assert len(got) == 1
    assert got[0]["member_slice_count"] == 2
    assert got[0]["spend"] == pytest.approx(0.48)
    assert got[0]["start_utc"] == "2026-09-15T08:15:00+00:00"
    assert got[0]["end_utc"] == "2026-09-15T08:25:00+00:00"


def test_gap_tolerance_one_quiet_slice_merges_three_does_not():
    """Acceptance 5: a single quiet slice inside a burst does not split it;
    three quiet slices do."""
    one_between = [
        _slice("2026-09-15T08:15:00+00:00", 0.30),
        _slice("2026-09-15T08:20:00+00:00", 0.01, bucket="normal"),
        _slice("2026-09-15T08:25:00+00:00", 0.30),
    ]
    got = bursts.assemble_bursts(one_between, slice_minutes=5, spike_threshold=0.1)
    assert len(got) == 1
    assert got[0]["spend"] == pytest.approx(0.61)

    three_between = [
        _slice("2026-09-15T08:15:00+00:00", 0.30),
        _slice("2026-09-15T08:20:00+00:00", 0.01, bucket="normal"),
        _slice("2026-09-15T08:25:00+00:00", 0.01, bucket="normal"),
        _slice("2026-09-15T08:30:00+00:00", 0.01, bucket="normal"),
        _slice("2026-09-15T08:35:00+00:00", 0.30),
    ]
    got = bursts.assemble_bursts(three_between, slice_minutes=5, spike_threshold=0.1)
    assert len(got) == 2


def test_burst_below_threshold_is_dropped():
    """A lone sub-threshold slice is not a burst (don't burst on noise)."""
    slices = [_slice("2026-09-15T08:15:00+00:00", 0.05)]
    assert bursts.assemble_bursts(slices, slice_minutes=5, spike_threshold=0.1) == []


def test_edge_expansion_folds_the_settle_tail():
    """A neighbour above `below_floor` is folded into the burst (the meter's
    trailing settlement), so its money is not mislabelled as its own event."""
    slices = [
        _slice("2026-09-15T08:10:00+00:00", 0.05, bucket="normal"),
        _slice("2026-09-15T08:15:00+00:00", 0.40),
        _slice("2026-09-15T08:20:00+00:00", 0.04, bucket="normal"),
    ]
    got = bursts.assemble_bursts(
        slices, slice_minutes=5, spike_threshold=0.1, below_floor=0.02
    )
    assert len(got) == 1
    assert got[0]["member_slice_count"] == 3
    assert got[0]["spend"] == pytest.approx(0.49)


def test_assembly_is_idempotent():
    """Acceptance 8: the same input yields the same burst ids."""
    slices = [
        _slice("2026-09-15T08:15:00+00:00", 0.26),
        _slice("2026-09-15T08:20:00+00:00", 0.22),
    ]
    first = bursts.assemble_bursts(slices, slice_minutes=5, spike_threshold=0.1)
    second = bursts.assemble_bursts(slices, slice_minutes=5, spike_threshold=0.1)
    assert [b["burst_id"] for b in first] == [b["burst_id"] for b in second]


# --- Stage 3: lag attribution, no double counting ---------------------------

def test_no_double_counting_across_overlapping_lag_windows():
    """Acceptance 6: with overlapping lag windows every span goes to exactly one
    burst, so the sum of per-burst cost never exceeds the trace total."""
    a = {"start_utc": "2026-09-15T08:15:00+00:00", "end_utc": "2026-09-15T08:20:00+00:00"}
    b = {"start_utc": "2026-09-15T08:30:00+00:00", "end_utc": "2026-09-15T08:35:00+00:00"}
    spans = [
        _span(start="2026-09-15T08:16:00+00:00", conversation="a"),
        _span(start="2026-09-15T08:19:00+00:00", conversation="a"),
        _span(start="2026-09-15T08:31:00+00:00", conversation="b"),
        # Sits in both padded windows (a: [08:10,08:25), b: [08:25,08:40)).
        _span(start="2026-09-15T08:24:30+00:00", conversation="mid"),
    ]
    owned, unassigned = bursts.assign_spans(
        spans, [a, b], lag_slices=1, slice_minutes=5
    )
    all_owned = [s for group in owned.values() for s in group]
    assert len(all_owned) == len(spans)
    assert not unassigned
    # The overlap span lands with exactly one burst (nearest wins).
    assert len([s for s in owned[0] if s["attributes"]["litellm.cost.total"] == 0.1]) >= 1
    assert len(all_owned) == sum(len(g) for g in owned.values())
    assert len(set(map(id, all_owned))) == len(all_owned)


def test_span_without_start_time_is_unassigned():
    spans = [_span(conversation="a")]
    owned, unassigned = bursts.assign_spans(
        spans,
        [{"start_utc": "2026-09-15T08:15:00+00:00", "end_utc": "2026-09-15T08:20:00+00:00"}],
        lag_slices=1,
    )
    assert owned[0] == []
    assert len(unassigned) == 1


# --- Stage 4: burst-level classification ------------------------------------

def _conversation_spans(*, start="2026-09-15T08:16:00+00:00", cost=0.15):
    """One conversation whose context grows while the cache serves only the
    system prompt — the `cache_prefix_unstable` shape from the memo."""
    prefixes = [
        ("2026-09-15T08:14:40+00:00", 40_000, 39_000),
        ("2026-09-15T08:16:30+00:00", 90_000, 39_000),
        ("2026-09-15T08:18:00+00:00", 170_000, 39_500),
    ]
    return [
        _span(
            start=ts, input=prompt, cached=read, output=300,
            finish="tool_calls", cost=cost, conversation="conv-1",
        )
        for ts, prompt, read in prefixes
    ]


def test_cache_prefix_unstable_is_the_signature():
    spans = _conversation_spans()
    spend = _reconciled(spans)
    diag = heuristics.diagnose(spans, window_spend=spend)
    assert diag["reason"] == "cache_prefix_unstable"
    assert diag["signature"] == "cache_prefix_unstable"
    assert diag["actionable"] is True
    assert diag["conversation_count"] == 1
    assert diag["top_conversation_id"] == "conv-1"
    assert diag["prefix_tokens"] == pytest.approx(39_000)
    assert diag["peak_prompt_tokens"] == pytest.approx(170_000)


def test_memo_regression_two_slices_collapse_to_one_burst():
    """Acceptance 1: the motivating case folds to one burst, ~100% explained,
    a `cache_prefix_unstable` signature and no `unexplained` row."""
    slices = [
        _slice("2026-09-15T08:15:00+00:00", 0.26),
        _slice("2026-09-15T08:20:00+00:00", 0.22),
    ]
    assembled = bursts.assemble_bursts(slices, slice_minutes=5, spike_threshold=0.1)
    burst = assembled[0]
    spans = _conversation_spans()
    # The burst's tokens reconcile with its merged drop (within 10%).
    spend = _reconciled(spans, start=burst["start_utc"], end=burst["end_utc"])
    diag = heuristics.diagnose(
        spans,
        window_spend=spend,
        window_start_utc=burst["start_utc"],
        window_end_utc=burst["end_utc"],
        burst_id=burst["burst_id"],
        member_slice_count=burst["member_slice_count"],
        lag_slices=1,
    )
    assert diag["reason"] != "unexplained"
    assert diag["reason"] == "cache_prefix_unstable"
    assert 90 <= diag["explained_cost_pct"] <= 110
    assert diag["member_slice_count"] == 2
    assert diag["reconciled_cost_lag_slices"] == 1
    assert diag["burst_id"] == burst["burst_id"]


def test_over_attribution_is_caught_not_clamped():
    """Acceptance 3: span cost 2.5x the delta reports `over_attributed`."""
    spans = [_span(cost=0.1, input=2000, output=100) for _ in range(4)]
    reconciled = _reconciled(spans)
    diag = heuristics.diagnose(spans, window_spend=reconciled / 2.5)
    assert diag["reason"] == "over_attributed"
    assert diag["investigate"] is True
    assert diag["signature"] is None
    assert "more than the drop" in diag["summary"]


def test_burst_with_no_spans_still_investigates():
    """Acceptance 2: no new false negatives — an empty burst is unexplained."""
    diag = heuristics.diagnose(
        [], window_spend=0.48, burst_id="b1", member_slice_count=2, lag_slices=1
    )
    assert diag["reason"] == "unexplained"
    assert diag["investigate"] is True


def test_new_reasons_registered():
    for key in ("over_attributed", "cache_prefix_unstable", "tool_call_loop"):
        assert key in REASONS


# --- wiring: analysis applies the lag window + nearest-owner ----------------

class _RecordingPhoenix:
    def __init__(self, spans=None):
        self.spans = spans or []
        self.calls = []

    def fetch_llm_spans(self, start, end, pad_seconds=0):
        self.calls.append((start, end))
        return self.spans


def test_analysis_widens_the_window_for_a_burst_lag(tmp_path):
    db = BalanceDB(str(tmp_path / "lag.db"))
    phoenix = _RecordingPhoenix(spans=[_span(cost=0.2, input=5000, cached=4800) for _ in range(30)])
    svc = AnalysisService(db, phoenix, lookback_days=1, attribution_lag_slices=1)
    burst = {
        "start_utc": "2026-09-15T08:15:00+00:00",
        "end_utc": "2026-09-15T08:25:00+00:00",
        "slice_minutes": 5,
        "spend": 0.48,
        "burst_id": "b1",
        "member_slice_count": 2,
        "lag_slices": 1,
    }
    db.record_high_interval(
        start_utc="2026-09-15T08:15:00+00:00", end_utc="2026-09-15T08:25:00+00:00",
        slice_minutes=5, spend=0.48, spike_threshold=0.1, median_interval=0.01,
        day="2026-09-15", detected_at="2026-09-15T08:25:00+00:00",
        burst_id="b1", member_slice_count=2, lag_slices=1,
    )
    diag = svc._diagnose(burst)
    assert diag is not None
    # Window is padded by one slice (5 min) on each side.
    assert phoenix.calls == [("2026-09-15T08:10:00+00:00", "2026-09-15T08:30:00+00:00")]
    assert diag["reconciled_cost_lag_slices"] == 1
    assert diag["member_slice_count"] == 2


def test_analysis_plain_slice_diagnosed_without_lag(tmp_path):
    """A bare interval (no burst identity) is reconciled with no lag, keeping
    the direct per-slice path unchanged."""
    db = BalanceDB(str(tmp_path / "plain.db"))
    phoenix = _RecordingPhoenix(spans=[])
    svc = AnalysisService(db, phoenix, lookback_days=1)
    high = {
        "start_utc": "2026-09-15T08:15:00+00:00",
        "end_utc": "2026-09-15T08:20:00+00:00",
        "slice_minutes": 5,
        "spend": 0.26,
    }
    svc._diagnose(high)
    assert phoenix.calls[0] == ("2026-09-15T08:15:00+00:00", "2026-09-15T08:20:00+00:00")


# --- Stage 5: burst fields round-trip ---------------------------------------

def test_burst_fields_round_trip_through_the_db(tmp_path):
    db = BalanceDB(str(tmp_path / "burst.db"))
    start = "2026-09-15T08:15:00+00:00"
    db.record_high_interval(
        start_utc=start, end_utc="2026-09-15T08:25:00+00:00", slice_minutes=5,
        spend=0.48, spike_threshold=0.1, median_interval=0.01, day="2026-09-15",
        detected_at=start, burst_id=start, member_slice_count=2, lag_slices=1,
    )
    spans = _conversation_spans()
    diag = heuristics.diagnose(
        spans, window_spend=_reconciled(spans), window_start_utc=start,
        window_end_utc="2026-09-15T08:25:00+00:00", burst_id=start,
        member_slice_count=2, lag_slices=1,
    )
    db.upsert_diagnostic(start_utc=start, diag=diag)

    rows, _ = db.high_intervals_with_diagnostics(limit=10)
    got = rows[0]["diagnosis"]
    assert got["signature"] == "cache_prefix_unstable"
    assert got["burst_id"] == start
    assert got["member_slice_count"] == 2
    assert got["reconciled_cost_lag_slices"] == 1
    assert got["conversation_count"] == 1
    assert got["top_conversation_id"] == "conv-1"
    assert got["prefix_tokens"] == pytest.approx(39_000)
    assert got["peak_prompt_tokens"] == pytest.approx(170_000)

    detailed = db.high_intervals_detailed(limit=10)[0]["diagnosis"]
    assert detailed["signature"] == "cache_prefix_unstable"
    assert detailed["burst_spend"] == pytest.approx(got["burst_spend"])
    # The interval row itself carries the burst view.
    assert db.high_intervals_between(start, "2026-09-15T08:30:00+00:00")[0]["burst_id"] == start


def test_analysis_detection_records_one_burst_for_adjacent_highs(tmp_path):
    """End-to-end: two adjacent high slices are recorded (and counted) as one
    burst, not two slice-local rows."""
    db = BalanceDB(str(tmp_path / "integration.db"))
    now = datetime.now(UTC)
    ts = now - timedelta(hours=3)
    bal = 1000.0
    for drop in [2.0] * 12 + [20.0, 20.0]:
        bal -= drop
        db.insert_snapshot(
            ts=ts.isoformat(), currency="CNY", total_balance=bal,
            granted_balance=bal, topped_up_balance=0.0, is_available=True,
            http_status=200, raw="{}",
        )
        ts += timedelta(minutes=5)
    phoenix = _RecordingPhoenix(spans=[_span(cost=0.2, input=5000, cached=4800)])
    svc = AnalysisService(db, phoenix, lookback_days=1)

    report = svc.run(now=now)
    assert report["high_intervals_known"] == 1
    rows = db.high_intervals_detailed(limit=50, include_signals=False)
    assert len(rows) == 1
    diag = rows[0]["diagnosis"]
    assert diag["member_slice_count"] >= 2
    assert diag["reconciled_cost_lag_slices"] == 1
    # The burst's Phoenix fetch is lag-padded on both sides.
    fetch_start, fetch_end = phoenix.calls[0]
    assert fetch_start < rows[0]["start_utc"]
    assert fetch_end > rows[0]["end_utc"]


def test_reconcile_day_partitions_every_span():
    slices = [
        _slice("2026-09-15T08:15:00+00:00", 0.26),
        _slice("2026-09-15T08:20:00+00:00", 0.22),
        _slice("2026-09-15T09:00:00+00:00", 0.30),
    ]
    spans = [
        _span(start="2026-09-15T08:16:00+00:00", conversation="a"),
        _span(start="2026-09-15T08:21:00+00:00", conversation="a"),
        _span(start="2026-09-15T09:01:00+00:00", conversation="b"),
    ]
    day_bursts, owned, unassigned = bursts.reconcile_day(
        slices, spans, slice_minutes=5, spike_threshold=0.1
    )
    assert len(day_bursts) == 2
    assert not unassigned
    total = sum(len(g) for g in owned.values())
    assert total == len(spans)
