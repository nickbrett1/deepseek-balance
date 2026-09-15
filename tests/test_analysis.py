"""Tests for the "why was it high?" analysis: Phoenix client, heuristics and
DB round-trip. No live Phoenix is required — the client is exercised against a
stubbed httpx transport and the classifier against fixture spans."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from deepseek_balance import heuristics, mcp_server
from deepseek_balance.analysis import AnalysisService
from deepseek_balance.db import BalanceDB
from deepseek_balance.heuristics import REASONS
from deepseek_balance.phoenix import PhoenixClient, attr, span_metrics

# --- span helpers -----------------------------------------------------------

def _span(model="deepseek-chat", *, input=1000, output=200, cached=0, cost=0.1,
          finish="stop", status="OK", error=False, start=None):
    attributes = {
        "gen_ai.response.model": model,
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
    return span


# --- attribute helpers ------------------------------------------------------

def test_attr_nested_and_dotted():
    span = {"attributes": {"gen_ai.usage.input_tokens": 10, "nested": {"a": {"b": 5}}}}
    assert attr(span, "gen_ai.usage.input_tokens") == 10
    assert attr(span, "nested.a.b") == 5
    assert attr(span, "missing.deep") is None


def test_span_metrics_cache_uncached():
    m = span_metrics(_span(input=1000, cached=400, output=200, cost=0.5))
    assert m["input_tokens"] == 1000
    assert m["cache_read_tokens"] == 400
    assert m["uncached_input_tokens"] == 600
    assert m["output_tokens"] == 200
    assert m["cache_hit_ratio"] == pytest.approx(0.4)
    assert m["is_error"] is False


# --- heuristics: reconciliation first ---------------------------------------

def _reconciled(spans, *, start=None, end=None):
    """The band-correct cost of these spans — i.e. the drop they explain.

    The taxonomy tests below use this so the balance drop is exactly what the
    window's tokens cost at DeepSeek's published band rates, which is what the
    classifier reconciles against (a hand-picked drop would otherwise be judged
    against a different pricing model than the fixtures assume).
    """
    return heuristics.summarize(
        spans, window_spend=0.0, window_start_utc=start, window_end_utc=end
    )["reconciled_cost"]


def test_diagnose_no_spans_is_unexplained_investigate():
    diag = heuristics.diagnose([], window_spend=5.0)
    assert diag["reason"] == "unexplained"
    assert diag["investigate"] is True
    assert diag["request_count"] == 0


def test_diagnose_cost_mismatch_is_unexplained():
    # Lots of cheap spans but the balance dropped far more than they cost.
    spans = [_span(cost=1.0, input=5000) for _ in range(5)]
    diag = heuristics.diagnose(spans, window_spend=500.0)
    assert diag["reason"] == "unexplained"
    assert diag["investigate"] is True
    assert diag["explained_cost_pct"] < 50


# --- heuristics: reason taxonomy --------------------------------------------

def test_benign_high_activity_cached():
    spans = [_span(cost=0.2, input=5000, cached=4800, output=100) for _ in range(30)]
    diag = heuristics.diagnose(spans, window_spend=_reconciled(spans))
    assert diag["reason"] == "high_activity_cached"
    assert diag["actionable"] is False
    assert diag["investigate"] is False


def test_cent_quantization_low_explained_is_benign_not_investigate():
    # The memo case: a ~0.04 drop that traces explain only ~20% (≈0.008) but on
    # cache-heavy traffic → the shortfall is a cent-quantization floor, benign.
    spans = [_span(cost=0.002, input=4000, cached=3900, output=50) for _ in range(4)]
    diag = heuristics.diagnose(spans, window_spend=0.04)
    assert diag["explained_cost_pct"] is not None
    assert diag["explained_cost_pct"] < 50
    assert diag["reason"] == "cent_quantized"
    assert diag["investigate"] is False
    assert diag["actionable"] is False


def test_cent_quantization_still_investigates_when_gap_is_real():
    # Same cache-heavy profile but the shortfall is far beyond cent scale → the
    # money is genuinely missing; must stay "investigate".
    spans = [_span(cost=1.0, input=4000, cached=3900, output=50) for _ in range(4)]
    diag = heuristics.diagnose(spans, window_spend=20.0)  # explained ~20%<50
    assert diag["explained_cost_pct"] < 50
    assert diag["reason"] == "unexplained"
    assert diag["investigate"] is True


def test_cent_quantization_not_benign_without_cache_heavy_traffic():
    # A cent-scale shortfall but NOT cache-heavy → not safe to call it a
    # measurement floor; keep it as investigate.
    spans = [_span(cost=0.008, input=4000, cached=0, output=50) for _ in range(4)]
    diag = heuristics.diagnose(spans, window_spend=0.10)  # explained ~32%<50
    assert diag["explained_cost_pct"] < 50
    assert diag["reason"] == "unexplained"
    assert diag["investigate"] is True


def test_tool_call_loop():
    spans = [_span(finish="tool_calls", cost=0.5, output=300) for _ in range(6)]
    diag = heuristics.diagnose(spans, window_spend=_reconciled(spans))
    assert diag["reason"] == "tool_call_loop"
    assert diag["actionable"] is True


def test_bloated_context():
    # Many requests each with a huge uncached prompt and small output.
    spans = [_span(input=300_000, cached=0, output=200, cost=1.0) for _ in range(4)]
    diag = heuristics.diagnose(spans, window_spend=_reconciled(spans))
    assert diag["reason"] == "bloated_context"
    assert diag["actionable"] is True


def test_expensive_single_request():
    spans = [_span(cost=0.01, input=100, output=50) for _ in range(20)]
    spans.append(_span(cost=8.0, input=2000, output=4000))
    diag = heuristics.diagnose(spans, window_spend=_reconciled(spans))
    assert diag["reason"] == "expensive_single_request"
    assert diag["actionable"] is True


def test_errors_retries():
    spans = [_span(error=True, cost=0.3) for _ in range(3)]
    spans += [_span(cost=0.3) for _ in range(2)]
    diag = heuristics.diagnose(spans, window_spend=_reconciled(spans))
    assert diag["reason"] == "errors_retries"
    assert diag["actionable"] is True


def test_all_reasons_have_labels():
    for key in (
        "tool_call_loop", "bloated_context", "expensive_single_request",
        "errors_retries", "high_concurrency_cache_miss", "large_output",
        "peak_pricing",
        "high_activity_cached", "cent_quantized", "unexplained",
    ):
        assert key in REASONS


# --- pricing: band-correct reconciliation + peak annotation -----------------
#
# The fixtures below are the memo's reconstruction of two windows: the same
# token mix (447,616 cache-hit / 409,435 cache-miss / 11,002 output) in an
# off-peak window and in a peak window. DeepSeek's published rates make those
# tokens cost $0.0693593 off-peak and $0.1387186 at peak, and LiteLLM's flat
# price records the *peak* figure in both cases.

_MEMO_TOKENS = {"input": 857_051, "cached": 447_616, "output": 11_002, "cost": 0.1387186}
_MEMO_OFFPEAK = 0.0693593      # published off-peak cost of that token mix
_MEMO_PEAK = 0.1387186         # published peak cost == litellm.cost.total

# Sunday 19:35 UTC (off-peak) and Tuesday 01:05 UTC (inside a peak band).
_OFFPEAK_WINDOW = ("2026-09-13T19:35:00+00:00", "2026-09-13T19:40:00+00:00")
_PEAK_WINDOW = ("2026-09-15T01:05:00+00:00", "2026-09-15T01:10:00+00:00")


def test_off_peak_window_prices_at_off_peak_rates():
    """Acceptance: off-peak traces reconcile to the drop at off-peak rates.

    Previously they were 2x high (litellm's flat peak price), which is what
    inflated `explained_cost_pct` across the board.
    """
    start, end = _OFFPEAK_WINDOW
    diag = heuristics.diagnose(
        [_span(**_MEMO_TOKENS)], window_spend=_MEMO_OFFPEAK,
        window_start_utc=start, window_end_utc=end,
    )
    assert diag["reconciled_cost_expected"] == pytest.approx(_MEMO_OFFPEAK, abs=1e-6)
    assert diag["reconciled_cost_litellm"] == pytest.approx(_MEMO_PEAK, abs=1e-6)
    assert diag["explained_cost_pct"] == pytest.approx(100.0)
    assert diag["pricing_band"] == "off_peak"
    assert diag["peak_overlap_minutes"] == 0.0
    assert diag["peak_premium_usd"] == 0.0
    # The flat-peak bug: the tracer charged the peak figure off-peak.
    assert diag["reconciled_cost_litellm"] == pytest.approx(2 * diag["reconciled_cost"])
    assert diag["reason"] != "unexplained"


def test_peak_window_prices_at_peak_rates():
    start, end = _PEAK_WINDOW
    diag = heuristics.diagnose(
        [_span(**_MEMO_TOKENS)], window_spend=_MEMO_PEAK,
        window_start_utc=start, window_end_utc=end,
    )
    assert diag["reconciled_cost_expected"] == pytest.approx(_MEMO_PEAK, abs=1e-6)
    assert diag["reconciled_cost_litellm"] == pytest.approx(_MEMO_PEAK, abs=1e-6)
    assert diag["pricing_band"] == "peak"
    assert diag["peak_overlap_minutes"] == pytest.approx(5.0)
    # The whole window is peak, so the premium is exactly the 2x uplift.
    assert diag["peak_premium_usd"] == pytest.approx(_MEMO_OFFPEAK, abs=1e-6)


def test_boundary_straddling_window_splits_by_span_time():
    """A window that straddles 01:00 UTC is priced per span, not wholesale."""
    start, end = "2026-09-15T00:57:00+00:00", "2026-09-15T01:02:00+00:00"
    tokens = {"input": 1_000_000, "cached": 1_000_000, "output": 0}
    spans = [
        _span(start="2026-09-15T00:58:00+00:00", **tokens),  # off-peak
        _span(start="2026-09-15T01:01:00+00:00", **tokens),  # peak
    ]
    spend = heuristics.summarize(
        spans, window_spend=0.0, window_start_utc=start, window_end_utc=end
    )["reconciled_cost"]
    diag = heuristics.diagnose(
        spans, window_spend=spend, window_start_utc=start, window_end_utc=end
    )
    off_peak_cost = 1_000_000 * 0.003 / 1e6      # cache-hit, off-peak
    peak_cost = 1_000_000 * 0.006 / 1e6          # cache-hit, peak
    assert diag["pricing_band"] == "mixed"
    assert diag["peak_overlap_minutes"] == pytest.approx(2.0)
    assert diag["reconciled_cost_expected"] == pytest.approx(off_peak_cost + peak_cost)
    assert diag["peak_premium_usd"] == pytest.approx(peak_cost - off_peak_cost)
    # The window's cost is bracketed by all-off-peak and all-peak pricing.
    assert off_peak_cost * 2 < diag["reconciled_cost_expected"] < peak_cost * 2


def test_peak_pricing_reason_when_peak_is_the_only_story():
    """A window whose only story is the 2x band is labelled `peak_pricing`."""
    start, end = _PEAK_WINDOW
    spans = [_span(input=80_000, cached=72_000, output=0) for _ in range(10)]
    spend = heuristics.summarize(
        spans, window_spend=0.0, window_start_utc=start, window_end_utc=end
    )["reconciled_cost"]
    diag = heuristics.diagnose(
        spans, window_spend=spend, window_start_utc=start, window_end_utc=end
    )
    assert diag["reason"] == "peak_pricing"
    assert diag["reason_label"] == REASONS["peak_pricing"]
    assert diag["investigate"] is False
    assert diag["pricing_band"] == "peak"
    # cache-hit 720k @ $0.006 + cache-miss 80k @ $0.30 per 1M, peak; the
    # premium is the same tokens priced off-peak.
    assert diag["peak_premium_usd"] == pytest.approx(0.01416, abs=1e-6)
    assert "peak" in diag["summary"].lower()


def test_peak_premium_annotated_when_another_reason_wins():
    """A real burst still gets its shape, with the peak premium named alongside."""
    start, end = _PEAK_WINDOW
    spans = [_span(input=5000, cached=2400, output=0) for _ in range(30)]
    spend = heuristics.summarize(
        spans, window_spend=0.0, window_start_utc=start, window_end_utc=end
    )["reconciled_cost"]
    diag = heuristics.diagnose(
        spans, window_spend=spend, window_start_utc=start, window_end_utc=end
    )
    # 30 requests at 48% cache -> the concurrency signature wins...
    assert diag["reason"] == "high_concurrency_cache_miss"
    # ...but the 2x premium is not hidden.
    assert diag["peak_premium_usd"] == pytest.approx(0.011916, abs=1e-6)
    assert "2x peak-hour premium" in diag["summary"]


def test_peak_boundary_cent_scale_shortfall_is_a_measurement_floor():
    """The 21:05-style slice: a cache-light slice whose drop overshoots traced
    cost by cents *at a peak boundary* is noise, not lost spend."""
    start, end = "2026-09-15T01:05:00+00:00", "2026-09-15T01:10:00+00:00"
    spans = [_span(input=8500, cached=3900, output=890, cost=0.028183)
             for _ in range(12)]
    diag = heuristics.diagnose(
        spans, window_spend=0.06, window_start_utc=start, window_end_utc=end
    )
    assert diag["reason"] == "cent_quantized"
    assert diag["investigate"] is False
    assert diag["pricing_band"] == "peak"


def test_same_shortfall_off_peak_still_investigates():
    """The floor above is scoped to the peak boundary — elsewhere a cache-light
    cent-scale shortfall stays "investigate"."""
    start, end = _OFFPEAK_WINDOW
    spans = [_span(input=8500, cached=3900, output=890, cost=0.028183)
             for _ in range(12)]
    diag = heuristics.diagnose(
        spans, window_spend=0.06, window_start_utc=start, window_end_utc=end
    )
    assert diag["reason"] == "unexplained"
    assert diag["investigate"] is True


def test_peak_pricing_round_trips_through_the_db(tmp_path):
    """The pricing block survives storage and the read paths."""
    db = BalanceDB(str(tmp_path / "peak.db"))
    start, end = _OFFPEAK_WINDOW
    diag = heuristics.diagnose(
        [_span(**_MEMO_TOKENS)], window_spend=_MEMO_OFFPEAK,
        window_start_utc=start, window_end_utc=end,
    )
    db.record_high_interval(
        start_utc=start, end_utc=end, slice_minutes=5, spend=_MEMO_OFFPEAK,
        spike_threshold=0.02, median_interval=0.01, day="2026-09-13",
        detected_at=start,
    )
    db.upsert_diagnostic(start_utc=start, diag=diag)
    rows, _ = db.high_intervals_with_diagnostics(limit=10)
    got = rows[0]["diagnosis"]
    assert got["pricing_band"] == "off_peak"
    assert got["reconciled_cost_litellm"] == pytest.approx(_MEMO_PEAK, abs=1e-6)
    assert got["peak_premium_usd"] == 0.0


# --- Phoenix client (paginated fetch over a stubbed transport) --------------

class _FakeTransport(httpx.BaseTransport):
    def __init__(self, pages):
        self.pages = pages  # list of {"data": [...], "next_cursor": str|None}
        self.hits = 0

    def handle_request(self, request):
        self.hits += 1
        page = self.pages[min(self.hits - 1, len(self.pages) - 1)]
        return httpx.Response(200, json=page, request=request)


def _client(pages):
    return PhoenixClient(
        base_url="http://phoenix.test:6006",
        client=httpx.Client(transport=_FakeTransport(pages)),
    )


def test_fetch_llm_spans_paginates():
    spans = [_span() for _ in range(3)]
    pages = [
        {"data": spans[:2], "next_cursor": "page2"},
        {"data": spans[2:], "next_cursor": None},
    ]
    got = _client(pages).fetch_llm_spans("2026-09-07T00:00:00Z", "2026-09-07T00:05:00Z")
    assert len(got) == 3
    # pad widens the window but the transport ignores params; just confirm no crash
    got = _client(pages).fetch_llm_spans("2026-09-07T00:00:00Z", "2026-09-07T00:05:00Z", pad_seconds=60)
    assert len(got) == 3


# --- analysis service + DB round trip ---------------------------------------

def _seed_balance(db, now, spend_total):
    """Write balance snapshots for 'today' that sum to `spend_total` of spend
    in a handful of 5-min slices, old enough that today has enough intervals."""
    start = datetime.now(UTC).replace(tzinfo=UTC) - timedelta(hours=3)
    bal = 1000.0
    # several equal drops (normal) then one big drop (the spike)
    n_small = 12
    small = spend_total * 0.05
    big = spend_total - small * n_small
    drops = [small] * n_small + [big]
    ts = start
    for drop in drops:
        bal -= drop
        db.insert_snapshot(
            ts=ts.isoformat(), currency="CNY", total_balance=bal,
            granted_balance=bal, topped_up_balance=0.0,
            is_available=True, http_status=200, raw="{}",
        )
        ts += timedelta(minutes=5)
    # ensure the spike slice stands out vs today's own small intervals
    return ts


class _FakePhoenix:
    def __init__(self, spans=None):
        self.spans = spans or []
        self.calls = []

    def fetch_llm_spans(self, start, end, pad_seconds=0):
        self.calls.append((start, end, pad_seconds))
        return self.spans


def test_analysis_records_and_diagnoses(tmp_path):
    db = BalanceDB(str(tmp_path / "t.db"))
    phoenix = _FakePhoenix(spans=[_span(cost=5.0, input=5000, cached=0)])
    svc = AnalysisService(db, phoenix, lookback_days=1)

    now = datetime.now(UTC).replace(tzinfo=UTC)
    _seed_balance(db, now, spend_total=100.0)

    report = svc.run(now=now)
    assert report["high_intervals_known"] >= 1
    assert report["newly_diagnosed"] >= 1
    assert phoenix.calls, "Phoenix should have been queried for a high interval"

    rows, _has_more = db.high_intervals_with_diagnostics(limit=25)
    assert rows
    # newest-first
    starts = [r["start_utc"] for r in rows]
    assert starts == sorted(starts, reverse=True)
    assert any(r["diagnosis"] is not None for r in rows)

    # idempotent: a second pass finds nothing new to diagnose
    report2 = svc.run(now=now)
    assert report2["newly_diagnosed"] == 0


def _diag(actionable: bool = False, investigate: bool = False, reason: str = "x") -> dict:
    return {
        "reason": reason,
        "reason_label": reason,
        "actionable": actionable,
        "investigate": investigate,
        "window_spend": 100.0,
        "reconciled_cost": 100.0,
        "explained_cost_pct": 100.0,
        "request_count": 1,
        "summary": "summary",
        "analyzed_at": datetime.now(UTC).isoformat(),
    }


def test_high_intervals_with_diagnostics_status_filter(tmp_path):
    """The status filter narrows the table to one diagnosis state each."""
    db = BalanceDB(str(tmp_path / "filter.db"))
    base = datetime(2025, 1, 2, tzinfo=UTC)
    key = "2025-01-02"

    def iso(mins: int) -> str:
        return (base + timedelta(minutes=mins)).isoformat()

    def rec(mins: int) -> str:
        s = iso(mins)
        db.record_high_interval(
            start_utc=s, end_utc=iso(mins + 5), slice_minutes=5, spend=100.0,
            spike_threshold=20.0, median_interval=10.0, day=key,
            detected_at=iso(mins),
        )
        return s

    inv = rec(0)     # investigated (Phoenix couldn't explain)
    act = rec(10)    # actionable candidate
    fine = rec(20)   # benign / fine
    pend = rec(30)   # recorded but not yet diagnosed
    db.upsert_diagnostic(start_utc=inv, diag=_diag(investigate=True))
    db.upsert_diagnostic(start_utc=act, diag=_diag(actionable=True))
    db.upsert_diagnostic(start_utc=fine, diag=_diag())

    def starts(status: str | None) -> set[str]:
        rows, _ = db.high_intervals_with_diagnostics(limit=50, status=status)
        return {r["start_utc"] for r in rows}

    all_rows, _ = db.high_intervals_with_diagnostics(limit=50)
    assert {r["start_utc"] for r in all_rows} == {inv, act, fine, pend}  # default = all
    assert starts("all") == {inv, act, fine, pend}
    assert starts("investigate") == {inv}
    assert starts("actionable") == {act}
    assert starts("fine") == {fine}
    assert starts("pending") == {pend}
    # paging honours the filter (one pending row -> no extra page)
    rows_pg, has_more = db.high_intervals_with_diagnostics(limit=1, status="pending")
    assert [r["start_utc"] for r in rows_pg] == [pend]
    assert has_more is False


def test_clear_analyses_keeps_balances(tmp_path):
    db = BalanceDB(str(tmp_path / "clear.db"))
    phoenix = _FakePhoenix(spans=[_span(cost=5.0, input=5000, cached=0)])
    svc = AnalysisService(db, phoenix, lookback_days=1)
    now = datetime.now(UTC).replace(tzinfo=UTC)
    _seed_balance(db, now, spend_total=100.0)
    svc.run(now=now)

    assert db.high_intervals_detailed(limit=50)
    cleared = db.clear_analyses()
    assert cleared["intervals_deleted"] >= 1
    assert cleared["diagnostics_deleted"] >= 1
    # The balance history is untouched — analysis is derived, not source data.
    assert db.high_intervals_detailed(limit=50) == []
    assert db.history((now - timedelta(days=2)).isoformat())


def test_analysis_redo_wipes_and_recomputes(tmp_path):
    db = BalanceDB(str(tmp_path / "redo.db"))
    phoenix = _FakePhoenix(spans=[_span(cost=5.0, input=5000, cached=0)])
    svc = AnalysisService(db, phoenix, lookback_days=1)
    now = datetime.now(UTC).replace(tzinfo=UTC)
    _seed_balance(db, now, spend_total=100.0)

    first = svc.run(now=now)
    assert first["high_intervals_known"] >= 1

    # Re-running run() alone is idempotent (nothing new to diagnose)...
    assert svc.run(now=now)["newly_diagnosed"] == 0

    # ...but redo() wipes and recomputes, so rows are re-detected + re-diagnosed.
    report = svc.redo(now=now)
    assert report["cleared"]["intervals_deleted"] >= 1
    assert report["high_intervals_known"] >= 1
    assert report["newly_diagnosed"] >= 1
    assert db.high_intervals_detailed(limit=50), "analysis repopulated after redo"


def test_high_intervals_detailed_includes_signals(tmp_path):
    db = BalanceDB(str(tmp_path / "t2.db"))
    phoenix = _FakePhoenix(spans=[_span(cost=5.0, input=5000, cached=0)])
    svc = AnalysisService(db, phoenix, lookback_days=1)
    now = datetime.now(UTC).replace(tzinfo=UTC)
    _seed_balance(db, now, spend_total=100.0)
    svc.run(now=now)

    rows = db.high_intervals_detailed(limit=50, include_signals=True)
    assert rows
    diag = rows[0]["diagnosis"]
    assert diag["signals"] is not None
    assert diag["signals"]["request_count"] >= 0


def test_mcp_high_interval_diagnoses(tmp_path):
    db = BalanceDB(str(tmp_path / "mcp.db"))
    phoenix = _FakePhoenix(spans=[_span(cost=5.0, input=5000, cached=0)])
    svc = AnalysisService(db, phoenix, lookback_days=1)
    now = datetime.now(UTC).replace(tzinfo=UTC)
    _seed_balance(db, now, spend_total=100.0)
    svc.run(now=now)
    mcp_server._db = db

    out = mcp_server.high_interval_diagnoses(status="all", include_signals=True)
    assert out["count"] >= 1
    assert out["timezone"]
    first = out["intervals"][0]
    assert first["start_utc"] and first["start"]  # both UTC and local forms present
    assert "reason" in first and "summary" in first
    assert first["spend"] > 0

    # the single fake span with no cache => classified as investigate/actionable,
    # never 'benign'; just confirm the status filter runs and returns a list.
    benign = mcp_server.high_interval_diagnoses(status="benign")
    assert "intervals" in benign


# --- T1: one-interval settlement lookback -----------------------------------

def test_lookback_attributes_empty_window_to_prior_burst():
    # A burst in the preceding interval reconciles the (empty) window's drop.
    prior = [_span(cost=0.03, input=5000, cached=1000, output=200) for _ in range(3)]
    diag = heuristics.diagnose_lookback(
        prior,
        window_spend=_reconciled(prior),
        prior_start_utc="2026-09-11T13:10:00+00:00",
        prior_end_utc="2026-09-11T13:15:00+00:00",
    )
    assert diag is not None
    assert diag["reason"] == "settled_from_prior_burst"
    assert diag["reason_label"] == REASONS["settled_from_prior_burst"]
    assert diag["investigate"] is False
    assert diag["actionable"] is False
    # The referenced burst window is recorded (auditable from the row).
    assert diag["prior_burst_start_utc"] == "2026-09-11T13:10:00+00:00"
    assert diag["prior_burst_end_utc"] == "2026-09-11T13:15:00+00:00"
    assert diag["prior_burst_reason"]
    assert diag["request_count"] == 3


def test_lookback_none_without_prior_spans():
    # T3b: a genuinely idle window has no lookback burst and stays "investigate".
    assert heuristics.diagnose_lookback(
        [], window_spend=0.06,
        prior_start_utc="2026-09-11T13:10:00+00:00",
        prior_end_utc="2026-09-11T13:15:00+00:00",
    ) is None


def test_lookback_none_when_burst_cost_far_below_drop():
    # Escalation trigger: burst cost ≪ window spend means the charge is
    # genuinely elsewhere — never force the settlement attribution.
    prior = [_span(cost=0.001, input=100, cached=0, output=10) for _ in range(2)]
    assert heuristics.diagnose_lookback(
        prior, window_spend=5.0,
        prior_start_utc="2026-09-11T13:10:00+00:00",
        prior_end_utc="2026-09-11T13:15:00+00:00",
    ) is None


def test_settled_reason_registered():
    assert "settled_from_prior_burst" in REASONS


class _WindowPhoenix:
    """Phoenix stub that only returns spans for windows in ``bursts``.

    Keys are the window's start ISO prefix; everything else is idle. This is
    what lets the lookback be exercised deterministically: the diagnosed window
    is empty, the preceding one carries the burst.
    """

    def __init__(self, bursts: dict[str, list[dict]]):
        self.bursts = bursts
        self.calls: list[tuple[str, str]] = []

    def fetch_llm_spans(self, start, end, pad_seconds=0):
        self.calls.append((start, end))
        for prefix, spans in self.bursts.items():
            if start.startswith(prefix):
                return spans
        return []


def _pair_snapshots(db, start_slot, end_slot, start_bal, end_bal):
    """Two consecutive grid snapshots with an exact drop between them."""
    db.insert_snapshot(
        ts=start_slot.replace(":00+00:00", ":05+00:00"), scheduled_ts=start_slot,
        currency="USD", total_balance=start_bal, granted_balance=0.0,
        topped_up_balance=start_bal, is_available=True, http_status=200, raw="{}",
    )
    db.insert_snapshot(
        ts=end_slot.replace(":00+00:00", ":05+00:00"), scheduled_ts=end_slot,
        currency="USD", total_balance=end_bal, granted_balance=0.0,
        topped_up_balance=end_bal, is_available=True, http_status=200, raw="{}",
    )


def test_analysis_empty_window_settles_from_prior_burst(tmp_path):
    """T1 + T2 integration: the idle window reclassifies to the lookback reason,
    points at the prior burst, and records the snapshot pair."""
    db = BalanceDB(str(tmp_path / "settle.db"))
    # The diagnosed window is [13:15, 13:20); its drop is the 13:15 -> 13:20
    # snapshot decline, and the prior (lookback) interval is [13:10, 13:15).
    _pair_snapshots(
        db,
        "2026-09-11T13:15:00+00:00", "2026-09-11T13:20:00+00:00", 4.67, 4.61,
    )
    phoenix = _WindowPhoenix(
        {"2026-09-11T13:10:00": [_span(cost=0.04, input=5000, cached=1000, output=20_000)
                                 for _ in range(3)]}
    )
    svc = AnalysisService(db, phoenix, lookback_days=1)
    high = {
        "start_utc": "2026-09-11T13:15:00+00:00",
        "end_utc": "2026-09-11T13:20:00+00:00",
        "slice_minutes": 5,
        "spend": 0.06,
    }
    diag = svc._diagnose(high)
    assert diag is not None
    # Before: empty window => unexplained/investigate. After: lookback reason.
    assert diag["reason"] == "settled_from_prior_burst"
    assert diag["investigate"] is False
    assert diag["prior_burst_start_utc"] == "2026-09-11T13:10:00+00:00"
    assert diag["prior_burst_end_utc"] == "2026-09-11T13:15:00+00:00"
    # Only the current window and the one-interval lookback were queried.
    assert [s for s, _ in phoenix.calls] == [
        "2026-09-11T13:15:00+00:00",
        "2026-09-11T13:10:00+00:00",
    ]
    # T2: the snapshot pair accounts for window_spend exactly.
    assert diag["balance_start"] - diag["balance_end"] == pytest.approx(0.06)
    assert diag["balance_start_ts"] == "2026-09-11T13:15:05+00:00"
    assert diag["balance_end_ts"] == "2026-09-11T13:20:05+00:00"


def test_analysis_empty_window_no_burst_stays_investigate(tmp_path):
    """T3b integration: no prior burst => the genuine "investigate" is kept."""
    db = BalanceDB(str(tmp_path / "idle.db"))
    _pair_snapshots(
        db,
        "2026-09-11T13:15:00+00:00", "2026-09-11T13:20:00+00:00", 4.67, 4.61,
    )
    svc = AnalysisService(db, _WindowPhoenix({}), lookback_days=1)
    high = {
        "start_utc": "2026-09-11T13:15:00+00:00",
        "end_utc": "2026-09-11T13:20:00+00:00",
        "slice_minutes": 5,
        "spend": 0.06,
    }
    diag = svc._diagnose(high)
    assert diag["reason"] == "unexplained"
    assert diag["investigate"] is True
    assert diag["prior_burst_start_utc"] is None


def test_analysis_populated_cent_floor_still_cent_quantized(tmp_path):
    """T3c: the lookback must not swallow the populated small-drop case."""
    db = BalanceDB(str(tmp_path / "cent.db"))
    _pair_snapshots(
        db,
        "2026-09-08T22:45:00+00:00", "2026-09-08T22:50:00+00:00", 5.00, 4.96,
    )
    # The window itself is populated (cache-heavy, tiny traced cost).
    phoenix = _WindowPhoenix(
        {"2026-09-08T22:45:00": [_span(cost=0.002, input=4000, cached=3900, output=50)
                                 for _ in range(4)]}
    )
    svc = AnalysisService(db, phoenix, lookback_days=1)
    high = {
        "start_utc": "2026-09-08T22:45:00+00:00",
        "end_utc": "2026-09-08T22:50:00+00:00",
        "slice_minutes": 5,
        "spend": 0.04,
    }
    diag = svc._diagnose(high)
    assert diag["reason"] == "cent_quantized"
    assert diag["investigate"] is False


def test_analysis_populated_window_unchanged(tmp_path):
    """T3d: a normal populated window is untouched by the lookback."""
    db = BalanceDB(str(tmp_path / "pop.db"))
    start, end = "2026-09-07T10:00:00+00:00", "2026-09-07T10:05:00+00:00"
    spans = [_span(cost=0.2, input=5000, cached=4800, output=100) for _ in range(30)]
    spend = _reconciled(spans, start=start, end=end)
    _pair_snapshots(db, start, end, 5.00, 5.00 - spend)
    phoenix = _WindowPhoenix({"2026-09-07T10:00:00": spans})
    svc = AnalysisService(db, phoenix, lookback_days=1)
    high = {
        "start_utc": start,
        "end_utc": end,
        "slice_minutes": 5,
        "spend": spend,
    }
    diag = svc._diagnose(high)
    assert diag["reason"] == "high_activity_cached"
    # No lookback query was made for a populated window.
    assert [s for s, _ in phoenix.calls] == ["2026-09-07T10:00:00+00:00"]


def test_snapshot_pair_round_trips_through_db(tmp_path):
    """T2: the recorded pair is readable from the diagnosis row alone."""
    db = BalanceDB(str(tmp_path / "pair.db"))
    _pair_snapshots(
        db,
        "2026-09-11T13:10:00+00:00", "2026-09-11T13:15:00+00:00", 4.67, 4.61,
    )
    start_slot = "2026-09-11T13:15:00+00:00"
    db.record_high_interval(
        start_utc=start_slot, end_utc="2026-09-11T13:20:00+00:00", slice_minutes=5,
        spend=0.06, spike_threshold=0.02, median_interval=0.01,
        day="2026-09-11", detected_at="2026-09-11T13:20:00+00:00",
    )
    diag = _diag(reason="settled_from_prior_burst")
    diag.update({
        "window_spend": 0.06,
        "balance_start_ts": "2026-09-11T13:10:05+00:00",
        "balance_start": 4.67,
        "balance_end_ts": "2026-09-11T13:15:05+00:00",
        "balance_end": 4.61,
        "prior_burst_start_utc": "2026-09-11T13:10:00+00:00",
        "prior_burst_end_utc": "2026-09-11T13:15:00+00:00",
        "prior_burst_reason": "high_activity_cached",
    })
    db.upsert_diagnostic(start_utc=start_slot, diag=diag)
    rows, _ = db.high_intervals_with_diagnostics(limit=10)
    row = next(r for r in rows if r["start_utc"] == start_slot)
    got = row["diagnosis"]
    assert got["balance_start"] - got["balance_end"] == pytest.approx(got["window_spend"])
    assert got["prior_burst_start_utc"] == "2026-09-11T13:10:00+00:00"
    assert got["prior_burst_reason"] == "high_activity_cached"
