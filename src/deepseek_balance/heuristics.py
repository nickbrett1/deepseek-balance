"""Heuristics that explain an unusually-high spend interval from Phoenix spans.

An "unusually high" interval is a balance drop in one spend slice that is a
robust outlier vs the recent distribution. To understand *why*, we pull the
LLM spans Phoenix recorded over that window and classify them.

The classifier is deliberately a small, ordered, deterministic rule set so
first-version output is stable and easy to iterate on. It produces:

- aggregate ``signals`` (request count, token/cache breakdown, errors, tool
  calls, per-model cost) — the raw facts, stored so later tuning is cheap;
- a primary ``reason`` (machine key) + ``reason_label`` chosen by precedence;
- an ``actionable`` flag and an ``investigate`` flag.

The reconciliation between the balance drop (``window_spend``) and the traced
LLM cost (``litellm.cost.total`` summed over the window) is done first: if the
traces can't account for the spend, the honest answer is "unexplained —
investigate", not a made-up cause.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

# Reason keys and their human labels.
REASONS: dict[str, str] = {
    "tool_call_loop": "Agentic tool-call loop",
    "bloated_context": "Large cache-miss context (prompt bloat)",
    "expensive_single_request": "One request dominated the window",
    "errors_retries": "Errors / retries inflating spend",
    "high_concurrency_cache_miss": "High concurrency with cache misses",
    "large_output": "Unusually large output generation",
    "high_activity_cached": "High activity — mostly cache hits / normal context",
    "cent_quantized": "Cent-quantized balance drop on cache-heavy traffic (measurement floor)",
    "settled_from_prior_burst": "Settled from prior burst (balance-snapshot lag)",
    "unexplained": "Could not attribute — investigate",
}

# Explainability bar: if traced cost is well under half the balance drop the
# money is not coming from the calls Phoenix saw, so mark it unexplained.
# Both figures are USD here (the balance is topped up in USD and litellm costs
# are reported in USD), so the explained percentage is directly comparable.
MIN_EXPLAINED_RATIO = 0.5
# A single span is "dominant" when it is this share of the window's cost.
DOMINANT_SPAN_SHARE = 0.7
# A run is a "tool-call loop" once this many spans turned into tool calls.
TOOL_LOOP_MIN_SPANS = 3
TOOL_LOOP_MIN_SHARE = 0.5
# Errors are meaningful when at least this many spans errored at this share.
ERRORS_MIN_COUNT = 2
ERRORS_MIN_SHARE = 0.3
# "Large output": average output tokens per request above this.
LARGE_OUTPUT_AVG_TOKENS = 4_000
# "Bloated context": total uncached (cache-miss) input above this, and it must
# dominate total tokens enough to be the cost driver rather than output.
BLOATED_UNCACHED_TOKENS = 100_000
BLOATED_MIN_INPUT_SHARE = 0.6
# Benign "high activity": lots of requests, cache serving most input.
HIGH_ACTIVITY_MIN_REQUESTS = 20
HIGH_ACTIVITY_MIN_CACHE_HIT = 0.8
# Cent-quantization is treated as benign only when the *absolute* shortfall
# between the balance drop and the traced cost is at the cent scale (the
# balance source moves in whole cents) AND traffic is cache-heavy. A larger
# shortfall is real spend that's missing and stays "investigate".
CENT_QUANTIZATION_MAX_GAP = 0.10


def _explained_pct(reconciled_cost: float | None, window_spend: float) -> float | None:
    if reconciled_cost is None or not window_spend or window_spend <= 0:
        return None
    return reconciled_cost / window_spend * 100.0


def summarize(spans: list[dict], *, window_spend: float) -> dict:
    """Aggregate one window's span metrics into signals + per-model cost."""
    n = len(spans)
    cost = None
    input_tokens = uncached = cache_read = output_tokens = total_tokens = 0.0
    cost_count = 0
    errors = 0
    tool_calls = 0
    model_cost: Counter[str] = Counter()
    model_reqs: Counter[str] = Counter()
    top1_cost = 0.0

    for span in spans:
        m = _span_metrics(span)
        input_tokens += m["input_tokens"]
        uncached += m["uncached_input_tokens"]
        cache_read += m["cache_read_tokens"]
        output_tokens += m["output_tokens"]
        total_tokens += m["total_tokens"]
        if m["cost"] is not None:
            cost = (cost or 0.0) + m["cost"]
            cost_count += 1
            top1_cost = max(top1_cost, m["cost"])
            if m["model"]:
                model_cost[m["model"]] += m["cost"]
        if m["model"]:
            model_reqs[m["model"]] += 1
        if m["is_error"]:
            errors += 1
        if m["is_tool_call"]:
            tool_calls += 1

    covered_cost = cost  # may be None if no span had a cost attribute

    top_models = []
    for model, c in model_cost.most_common(4):
        top_models.append(
            {
                "model": model,
                "cost": c,
                "requests": model_reqs.get(model, 0),
                "cost_share": (c / covered_cost) if covered_cost else None,
            }
        )

    cache_hit_ratio = (cache_read / input_tokens) if input_tokens else None

    return {
        "request_count": n,
        "reconciled_cost": covered_cost,
        "cost_attr_spans": cost_count,
        "input_tokens": input_tokens,
        "uncached_input_tokens": uncached,
        "cache_read_tokens": cache_read,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_hit_ratio": cache_hit_ratio,
        "error_count": errors,
        "tool_call_count": tool_calls,
        "dominant_span_cost": top1_cost,
        "dominant_span_share": (top1_cost / covered_cost) if covered_cost else None,
        "top_models": top_models,
        "explained_cost_pct": _explained_pct(covered_cost, window_spend),
        "window_spend": window_spend,
    }


def _is_cent_quantization_limited(signals: dict) -> bool:
    """True when a low explained ratio is a cent-quantization artifact rather
    than genuinely lost spend.

    The DeepSeek balance source only moves in whole cents at the poll boundary,
    so a *small absolute* shortfall between the balance drop and the traced
    cost on cheap, cache-heavy traffic is a measurement floor — the classifier
    should not cry "investigate". A shortfall beyond the cent scale is real
    missing spend and stays ``investigate``.
    """
    cost = signals["reconciled_cost"]
    window_spend = signals["window_spend"]
    if not cost or cost <= 0 or not window_spend or window_spend <= 0:
        return False
    if window_spend - cost > CENT_QUANTIZATION_MAX_GAP:
        return False
    ratio = signals["cache_hit_ratio"]
    return ratio is not None and ratio >= HIGH_ACTIVITY_MIN_CACHE_HIT


def classify(signals: dict) -> dict:
    """Pick the primary reason for a window from its aggregated signals."""
    n = signals["request_count"]
    cost = signals["reconciled_cost"]
    explained = signals["explained_cost_pct"]

    # No observable LLM traffic at all.
    if n == 0:
        return _mk("unexplained", investigate=True,
                   summary="No LLM spans were found in Phoenix for this window, yet spend "
                           "was recorded — trace coverage is missing or the cost is elsewhere.")

    # Traced cost can't account for the balance drop.
    if cost is None:
        return _mk("unexplained", investigate=True,
                   summary="Spans carry no cost attributes, so the spend can't be tied to LLM calls.")
    if explained is not None and explained < MIN_EXPLAINED_RATIO * 100:
        if _is_cent_quantization_limited(signals):
            return _mk(
                "cent_quantized",
                summary=(f"Balance drop ({_money(signals['window_spend'])}) is at the cent-"
                         f"quantization floor on cache-heavy traffic — traced cost "
                         f"({_money(cost)}) is a measurement artifact, not lost spend."),
            )
        return _mk("unexplained", investigate=True,
                   summary=(f"Traced LLM cost ({_money(cost)}) only explains ~{explained:.0f}% "
                            f"of the {_money(signals['window_spend'])} drop — the rest is not "
                            "accounted for; investigate."))

    share = signals["dominant_span_share"]
    error_share = signals["error_count"] / n if n else 0.0
    tool_share = signals["tool_call_count"] / n if n else 0.0
    avg_output = signals["output_tokens"] / n if n else 0.0

    # Actionable drivers, most-specific first.
    if signals["tool_call_count"] >= TOOL_LOOP_MIN_SPANS and tool_share >= TOOL_LOOP_MIN_SHARE:
        return _mk("tool_call_loop", actionable=True,
                   summary=(f"{signals['tool_call_count']}/{n} requests ended in tool calls "
                            "— an agentic loop driving repeated model calls."))

    uncached = signals["uncached_input_tokens"]
    total_tok = signals["total_tokens"]
    input_share = (uncached / total_tok) if total_tok else 0.0
    if uncached >= BLOATED_UNCACHED_TOKENS and input_share >= BLOATED_MIN_INPUT_SHARE:
        return _mk("bloated_context", actionable=True,
                   summary=(f"{_tokens(uncached)} of cache-miss input across {n} requests "
                            "— context bloat (uncached prompts) looks like the driver."))

    if share is not None and share >= DOMINANT_SPAN_SHARE:
        return _mk("expensive_single_request", actionable=True,
                   summary=(f"One request was ~{share * 100:.0f}% of the window's LLM cost "
                            f"({_money(cost)})."))

    if signals["error_count"] >= ERRORS_MIN_COUNT and error_share >= ERRORS_MIN_SHARE:
        return _mk("errors_retries", actionable=True,
                   summary=(f"{signals['error_count']}/{n} requests errored — failed + retried "
                            "calls inflate spend."))

    # High concurrency with cache misses (many full-price calls at once).
    if (n >= HIGH_ACTIVITY_MIN_REQUESTS
            and signals["cache_hit_ratio"] is not None
            and signals["cache_hit_ratio"] < 0.5):
        return _mk("high_concurrency_cache_miss", actionable=True,
                   summary=(f"{n} requests in the window with only "
                            f"{signals['cache_hit_ratio'] * 100:.0f}% input served from cache."))

    if avg_output >= LARGE_OUTPUT_AVG_TOKENS:
        return _mk("large_output", actionable=True,
                   summary=f"Requests averaged {_tokens(avg_output)} output tokens each.")

    # Benign: high activity but well-cached and normal-sized → not a candidate.
    if (n >= HIGH_ACTIVITY_MIN_REQUESTS
            and signals["cache_hit_ratio"] is not None
            and signals["cache_hit_ratio"] >= HIGH_ACTIVITY_MIN_CACHE_HIT):
        return _mk("high_activity_cached",
                   summary=(f"{n} requests, {signals['cache_hit_ratio'] * 100:.0f}% input from "
                            "cache, no dominant request — normal usage, nothing to optimise."))

    # Fallback: spend is explained but none of the signatures dominate.
    return _mk("high_activity_cached",
               summary=(f"{n} requests explaining ~{explained:.0f}% of the drop, but with no "
                        "single dominant signature."))


def _span_metrics(span: dict) -> dict:
    from . import phoenix as _phx

    return _phx.span_metrics(span)


def diagnose(spans: list[dict], *, window_spend: float, analyzed_at: str | None = None) -> dict:
    """Full diagnosis for a window: signals + classified reason."""
    signals = summarize(spans, window_spend=window_spend)
    decision = classify(signals)
    decision["analyzed_at"] = analyzed_at or datetime.now(UTC).isoformat()
    # Merge the top-level fields the DB stores + keep the signals for tuning.
    diag = {
        "reason": decision["reason"],
        "reason_label": REASONS[decision["reason"]],
        "actionable": decision.get("actionable", False),
        "investigate": decision.get("investigate", False),
        "summary": decision["summary"],
        "window_spend": signals["window_spend"],
        "reconciled_cost": signals["reconciled_cost"],
        "explained_cost_pct": signals["explained_cost_pct"],
        "request_count": signals["request_count"],
        "cache_read_tokens": signals["cache_read_tokens"],
        "uncached_input_tokens": signals["uncached_input_tokens"],
        "output_tokens": signals["output_tokens"],
        "cache_hit_ratio": signals["cache_hit_ratio"],
        "error_count": signals["error_count"],
        "tool_call_count": signals["tool_call_count"],
        "top_models": signals["top_models"],
        "analyzed_at": decision["analyzed_at"],
        # Full signals block stored for later heuristic tuning / drill-in.
        "payload": {"signals": signals},
    }
    return diag


def diagnose_lookback(
    prior_spans: list[dict],
    *,
    window_spend: float,
    prior_start_utc: str,
    prior_end_utc: str,
    analyzed_at: str | None = None,
) -> dict | None:
    """Explain an empty window as the lagged settlement of the prior interval's burst.

    The balance source is polled on a grid and cent-quantized, so charges from a
    burst post up to roughly one snapshot interval *after* the tokens were
    consumed. A trailing settlement can therefore land in a genuinely idle
    window: the window itself has no spans, but the **preceding** interval
    carries the burst that caused it. When that is the case, attribute the
    window to the prior burst instead of crying "could not attribute".

    This is deliberately conservative (see the T1 escalation trigger): the
    attribution only holds when the prior burst actually reconciles with the
    window's drop. If the burst's traced cost is well below the drop, the charge
    is genuinely elsewhere and the caller must keep the "unexplained" verdict —
    forcing a settlement label there would hide real missing spend.

    Returns a full diagnosis dict (same shape as :func:`diagnose`), or ``None``
    when there is no reconciling prior burst.
    """
    if not prior_spans:
        return None
    prior = summarize(prior_spans, window_spend=window_spend)
    cost = prior["reconciled_cost"]
    if not cost or cost <= 0:
        return None
    # Burst cost must plausibly cover the settlement: a burst whose traced cost
    # is far short of the drop is not the explanation, so do not force it.
    if window_spend and window_spend > 0 and cost < window_spend * MIN_EXPLAINED_RATIO:
        return None
    # The prior window's own primary reason is the burst signature we attribute
    # to. It cannot be the reconciliation-mismatch branch here (we just checked
    # cost >= MIN_EXPLAINED_RATIO of the drop).
    prior_reason = classify(prior)["reason"]

    decision = _mk(
        "settled_from_prior_burst",
        summary=(
            f"No LLM spans in this window, but the preceding interval "
            f"({prior_start_utc} → {prior_end_utc}) carries a burst of "
            f"{prior['request_count']} requests costing {_money(cost)}. The "
            f"{_money(window_spend)} drop is a lagged settlement of that burst "
            f"(balances are polled on a grid and cent-quantized); burst "
            f"signature: {prior_reason}."
        ),
    )
    decision["analyzed_at"] = analyzed_at or datetime.now(UTC).isoformat()
    diag = {
        **decision,
        "reason_label": REASONS["settled_from_prior_burst"],
        "window_spend": window_spend,
        "reconciled_cost": cost,
        "explained_cost_pct": _explained_pct(cost, window_spend),
        "request_count": prior["request_count"],
        "cache_read_tokens": prior["cache_read_tokens"],
        "uncached_input_tokens": prior["uncached_input_tokens"],
        "output_tokens": prior["output_tokens"],
        "cache_hit_ratio": prior["cache_hit_ratio"],
        "error_count": prior["error_count"],
        "tool_call_count": prior["tool_call_count"],
        "top_models": prior["top_models"],
        # The referenced burst window — this is what makes the lookback
        # auditable from the row itself rather than trusted.
        "prior_burst_start_utc": prior_start_utc,
        "prior_burst_end_utc": prior_end_utc,
        "prior_burst_reason": prior_reason,
        "payload": {
            "signals": prior,
            "lookback": {
                "prior_burst_start_utc": prior_start_utc,
                "prior_burst_end_utc": prior_end_utc,
                "prior_burst_reason": prior_reason,
                "prior_burst_cost": cost,
            },
        },
    }
    return diag


def _mk(reason: str, *, actionable: bool = False, investigate: bool = False, summary: str) -> dict:
    return {
        "reason": reason,
        "actionable": actionable,
        "investigate": investigate,
        "summary": summary,
    }


def _money(value: float) -> str:
    return f"{value:.2f}"


def _tokens(value: float) -> str:
    v = round(value)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}k"
    return str(v)
