"""Tests for the "why was it high?" analysis: Phoenix client, heuristics and
DB round-trip. No live Phoenix is required — the client is exercised against a
stubbed httpx transport and the classifier against fixture spans."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from deepseek_balance import heuristics
from deepseek_balance.analysis import AnalysisService
from deepseek_balance.db import BalanceDB
from deepseek_balance.heuristics import REASONS
from deepseek_balance.phoenix import PhoenixClient, attr, span_metrics

# --- span helpers -----------------------------------------------------------

def _span(model="deepseek-chat", *, input=1000, output=200, cached=0, cost=0.1,
          finish="stop", status="OK", error=False):
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
    return {
        "name": "chat deepseek-chat",
        "span_kind": "LLM",
        "status_code": "ERROR" if error else status,
        "attributes": attributes,
    }


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
    diag = heuristics.diagnose(spans, window_spend=6.0)
    assert diag["reason"] == "high_activity_cached"
    assert diag["actionable"] is False
    assert diag["investigate"] is False


def test_tool_call_loop():
    spans = [_span(finish="tool_calls", cost=0.5, output=300) for _ in range(6)]
    diag = heuristics.diagnose(spans, window_spend=3.0)
    assert diag["reason"] == "tool_call_loop"
    assert diag["actionable"] is True


def test_bloated_context():
    # Many requests each with a huge uncached prompt and small output.
    spans = [_span(input=300_000, cached=0, output=200, cost=1.0) for _ in range(4)]
    diag = heuristics.diagnose(spans, window_spend=4.0)
    assert diag["reason"] == "bloated_context"
    assert diag["actionable"] is True


def test_expensive_single_request():
    spans = [_span(cost=0.01, input=100, output=50) for _ in range(20)]
    spans.append(_span(cost=8.0, input=2000, output=4000))
    diag = heuristics.diagnose(spans, window_spend=8.5)
    assert diag["reason"] == "expensive_single_request"
    assert diag["actionable"] is True


def test_errors_retries():
    spans = [_span(error=True, cost=0.3) for _ in range(3)]
    spans += [_span(cost=0.3) for _ in range(2)]
    diag = heuristics.diagnose(spans, window_spend=1.5)
    assert diag["reason"] == "errors_retries"
    assert diag["actionable"] is True


def test_all_reasons_have_labels():
    for key in (
        "tool_call_loop", "bloated_context", "expensive_single_request",
        "errors_retries", "high_concurrency_cache_miss", "large_output",
        "high_activity_cached", "unexplained",
    ):
        assert key in REASONS


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
