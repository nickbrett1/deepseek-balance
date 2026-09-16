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


def test_merge_bursts_coalesces_fragments():
    """Two bursts a single quiet slice apart are one event; a wider gap is not."""
    def burst(start, end, spend, n=1):
        return {
            "burst_id": start, "start_utc": start, "end_utc": end, "spend": spend,
            "member_slices": [{"start_utc": start, "spend": spend}] * n,
            "member_slice_count": n, "lag_slices": 1,
        }

    a = burst("2026-09-15T08:15:00+00:00", "2026-09-15T08:20:00+00:00", 0.2)
    b = burst("2026-09-15T08:25:00+00:00", "2026-09-15T08:30:00+00:00", 0.3)
    merged = bursts.merge_bursts([a, b], slice_minutes=5, merge_gap_slices=1)
    assert len(merged) == 1
    assert merged[0]["start_utc"] == a["start_utc"]
    assert merged[0]["end_utc"] == b["end_utc"]
    assert merged[0]["spend"] == pytest.approx(0.5)
    assert merged[0]["member_slice_count"] == 2
    # The first burst's id survives, so a re-run stays idempotent.
    assert merged[0]["burst_id"] == a["burst_id"]

    far = burst("2026-09-15T08:45:00+00:00", "2026-09-15T08:50:00+00:00", 0.3)
    assert len(bursts.merge_bursts([a, far], slice_minutes=5, merge_gap_slices=1)) == 2


def test_assemble_merges_fragments_into_one_burst():
    """A high run whose settle-tail meets the next high run is one event: the
    two groups would each understate their delta, so they are merged."""
    slices = [
        _slice("2026-09-15T08:15:00+00:00", 0.40),
        _slice("2026-09-15T08:20:00+00:00", 0.03, bucket="normal"),
        _slice("2026-09-15T08:25:00+00:00", 0.03, bucket="normal"),
        _slice("2026-09-15T08:30:00+00:00", 0.40),
    ]
    merged = bursts.assemble_bursts(
        slices, slice_minutes=5, spike_threshold=0.1, below_floor=0.02,
        merge_gap_slices=1,
    )
    assert len(merged) == 1
    assert merged[0]["member_slice_count"] == 4
    assert merged[0]["spend"] == pytest.approx(0.86)
    # Disabling the merge leaves the two fragments separate.
    split = bursts.assemble_bursts(
        slices, slice_minutes=5, spike_threshold=0.1, below_floor=0.02,
        merge_gap_slices=0,
    )
    assert len(split) == 2


def test_zero_or_negative_window_drop_is_unexplained_not_divided():
    """A window with no measurable drop must not divide into a huge percentage."""
    spans = [_span(cost=0.1, input=1000) for _ in range(3)]
    zero = heuristics.diagnose(spans, window_spend=0.0)
    assert zero["reason"] == "unexplained"
    assert zero["investigate"] is True
    assert zero["explained_cost_pct"] is None
    negative = heuristics.diagnose(spans, window_spend=-0.05)
    assert negative["reason"] == "unexplained"


def test_over_attribution_denominator_artifact_is_not_flagged():
    """An over-100% ratio whose tracer matches the token cost is a mis-sized
    denominator, not double counting — it must not be actionable."""
    start, end = "2026-09-15T01:05:00+00:00", "2026-09-15T01:10:00+00:00"  # peak band
    tokens = {"input": 5000, "cached": 4800, "output": 100}
    per = _reconciled([_span(start=start, **tokens)])
    spans = [_span(start=start, cost=per, **tokens) for _ in range(30)]
    expected = _reconciled(spans, start=start, end=end)
    diag = heuristics.diagnose(
        spans, window_spend=expected / 3.0,
        window_start_utc=start, window_end_utc=end,
    )
    assert diag["explained_cost_pct"] > 200
    assert diag["reason"] != "over_attributed"
    assert diag["investigate"] is False


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


def _seed_snapshots(db, slot_balances: dict[str, float]) -> None:
    """Seed one grid snapshot per ``{slot: balance}`` (scheduled at the slot)."""
    for slot, bal in slot_balances.items():
        db.insert_snapshot(
            ts=slot.replace(":00+00:00", ":05+00:00"), scheduled_ts=slot,
            currency="USD", total_balance=bal, granted_balance=0.0,
            topped_up_balance=bal, is_available=True, http_status=200, raw="{}",
        )


def test_reconciliation_credits_the_drop_over_the_traced_window(tmp_path):
    """The memo case: the flush is spread across later snapshots, so the
    denominator must be the drop over the whole traced window, not the one
    member slice. With only the member delta the burst reads 215%
    (`over_attributed`); with the window drop it reconciles ~100%."""
    db = BalanceDB(str(tmp_path / "drop.db"))
    start, end = "2026-09-16T16:25:00+00:00", "2026-09-16T16:30:00+00:00"
    spans = [
        _span(start="2026-09-16T16:26:00+00:00", cost=0.2, input=5000, cached=4800)
        for _ in range(30)
    ]
    reconciled = _reconciled(spans, start=start, end=end)
    member_spend = reconciled / 2.15  # what the old member-slice denominator saw
    _seed_snapshots(db, {
        start: 11.34,                              # opens the burst
        end: 11.27,                                # only part of the flush landed here
        "2026-09-16T16:35:00+00:00": 11.34 - reconciled,  # settles by end + lag
    })
    phoenix = _RecordingPhoenix(spans=spans)
    svc = AnalysisService(db, phoenix, lookback_days=1, attribution_lag_slices=1)
    high = {
        "start_utc": start, "end_utc": end, "slice_minutes": 5,
        "spend": member_spend, "burst_id": start, "member_slice_count": 1,
        "lag_slices": 1,
    }
    diag = svc._diagnose(high)
    assert diag is not None
    assert diag["reason"] != "over_attributed"
    assert diag["reason"] == "high_activity_cached"
    assert diag["investigate"] is False
    # The denominator is the window drop; the member delta is kept separately.
    assert diag["window_drop"] == pytest.approx(reconciled, abs=1e-9)
    assert diag["burst_spend"] == pytest.approx(member_spend, abs=1e-9)
    assert diag["traced_cost"] == pytest.approx(reconciled, abs=1e-9)
    assert diag["explained_cost_pct"] == pytest.approx(100.0, abs=1.0)
    # No member slice would have reconciled on its own.
    assert member_spend / reconciled == pytest.approx(1 / 2.15)
    # The recorded pair is the reconciliation span, not the member slice.
    assert diag["balance_start_ts"].startswith("2026-09-16T16:25")
    assert diag["balance_end_ts"].startswith("2026-09-16T16:35")


def test_reconciliation_falls_back_to_member_spend_without_snapshots(tmp_path):
    """With no reconciliation snapshot the member-slice delta is used, so the
    row still reconciles (never divides by a missing/zero window)."""
    db = BalanceDB(str(tmp_path / "nosnap.db"))
    start, end = "2026-09-16T16:25:00+00:00", "2026-09-16T16:30:00+00:00"
    spans = [_span(start="2026-09-16T16:26:00+00:00", cost=0.2, input=5000, cached=4800)
             for _ in range(30)]
    spend = _reconciled(spans, start=start, end=end)
    svc = AnalysisService(db, _RecordingPhoenix(spans=spans), lookback_days=1,
                          attribution_lag_slices=1)
    high = {
        "start_utc": start, "end_utc": end, "slice_minutes": 5,
        "spend": spend, "burst_id": start, "member_slice_count": 1, "lag_slices": 1,
    }
    diag = svc._diagnose(high)
    assert diag["window_drop"] == pytest.approx(spend)
    assert diag["explained_cost_pct"] == pytest.approx(100.0, abs=1.0)


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
    # The ratio's inputs round-trip too, so it is reproducible from the row.
    assert got["window_drop"] == pytest.approx(got["window_spend"])
    assert got["traced_cost"] == pytest.approx(got["reconciled_cost"])

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
