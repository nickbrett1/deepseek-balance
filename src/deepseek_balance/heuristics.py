"""Heuristics that explain an unusually-high spend interval from Phoenix spans.

An "unusually high" interval is a balance drop in one spend slice that is a
robust outlier vs the recent distribution. To understand *why*, we pull the
LLM spans Phoenix recorded over that window and classify them.

The classifier is deliberately a small, ordered, deterministic rule set so
first-version output is stable and easy to iterate on. It produces:

- aggregate ``signals`` (request count, token/cache breakdown, errors, tool
  calls, per-model cost) — the raw facts, stored so later tuning is cheap;
- a ``pricing`` block: the DeepSeek band the window fell in, how many minutes of
  it were peak-priced, and the peak premium in dollars;
- a primary ``reason`` (machine key) + ``reason_label`` chosen by precedence;
- an ``actionable`` flag and an ``investigate`` flag.

The reconciliation between the balance drop (``window_spend``) and the traced
LLM cost is done first: if the traces can't account for the spend, the honest
answer is "unexplained — investigate", not a made-up cause.

The reconciliation compares the drop against **``reconciled_cost_expected``** —
what the window's token counts cost at the DeepSeek rate for the band each
request actually ran in (see ``pricing.py``) — not against LiteLLM's flat
``litellm.cost.total``, which *always* charges the peak rate. That LiteLLM sum
is kept as ``reconciled_cost_litellm`` purely as a **flat-peak reference** (it
is consistently ~2x the truth off-peak); it is never the "traced" figure the
drop is reconciled against, and must not be presented as one. The gap between
the two is surfaced as ``flat_peak_overstatement_usd``.

The denominator — ``window_spend`` — is the balance movement over the *traced*
window, not one member slice: the caller (``analysis``) credits the drop from
the snapshot opening the burst to the snapshot closing it **plus the settling
lag**, so a burst whose flush is spread over several snapshots is not reported
as over-attributed. A window with no measurable (zero/negative) drop cannot be
divided into and falls back to ``unexplained``.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise

from . import pricing

# Reason keys and their human labels.
REASONS: dict[str, str] = {
    "tool_call_loop": "Agentic tool-call loop",
    "bloated_context": "Large cache-miss context (prompt bloat)",
    "expensive_single_request": "One request dominated the window",
    "errors_retries": "Errors / retries inflating spend",
    "high_concurrency_cache_miss": "High concurrency with cache misses",
    "large_output": "Unusually large output generation",
    "peak_pricing": "Peak-hour rate (2x off-peak)",
    "cache_prefix_unstable": "Unstable prompt-cache prefix (per-turn cache miss)",
    "high_activity_cached": "High activity — mostly cache hits / normal context",
    "cent_quantized": "Cent-quantized balance drop (measurement floor)",
    "over_attributed": "Over-attributed (traced cost exceeds the drop)",
    "settled_from_prior_burst": "Settled from prior burst (balance-snapshot lag)",
    "unexplained": "Could not attribute — investigate",
}

# Reasons that are an actionable *signature* of the traffic (as opposed to an
# attribution-health verdict). ``diagnose`` copies the winning one into the
# row's ``signature`` field so the finding is separable from the health label.
SIGNATURES: frozenset[str] = frozenset(
    {
        "cache_prefix_unstable",
        "tool_call_loop",
        "bloated_context",
        "expensive_single_request",
        "errors_retries",
        "high_concurrency_cache_miss",
        "large_output",
        "peak_pricing",
        "high_activity_cached",
        "cent_quantized",
    }
)

# Explainability bar: if traced cost is well under half the balance drop the
# money is not coming from the calls Phoenix saw, so mark it unexplained.
# Both figures are USD here (the balance is topped up in USD and litellm costs
# are reported in USD), so the explained percentage is directly comparable.
MIN_EXPLAINED_RATIO = 0.5
# Attribution-health band: inside [under, over] the traces reconcile with the
# drop; below it the money is genuinely unattributed; above it the traces cost
# *more* than the drop (double counting or lag beyond the burst window) — which
# is evidence, not a benign clamp.
UNDER_FACTOR = 0.5
OVER_FACTOR = 2.0
# `over_attributed` must not fire on a mere denominator mismatch. With the drop
# now credited over the full traced window, a genuine double count is the only
# thing that should push the traced cost past the drop, and it leaves a second
# fingerprint: the raw tracer sum (Σ litellm.cost.total) sits far above what the
# window's tokens can cost at *any* band — beyond the flat-peak factor that
# already overstates off-peak by ~2x. Below this multiple, an over-100% ratio is
# treated as measurement noise rather than actionable double counting.
DOUBLE_COUNT_TRACER_FACTOR = 2.5
# `cache_prefix_unstable`: a single conversation whose context grows turn over
# turn while the prompt cache keeps serving only the (small) system prefix. Needs
# a few turns to be meaningful, a non-trivial prefix, context that actually grows,
# and a peak prompt well above what the cache serves.
CACHE_PREFIX_MIN_TURNS = 3
CACHE_PREFIX_MIN_TOKENS = 1_000
CACHE_PREFIX_GROWTH = 1.5
CACHE_PREFIX_PROMPT_OVER_PREFIX = 2.0
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
# The peak premium is worth naming once it is at least this many dollars and
# this share of the window's drop. Below both, it is rounding noise.
PEAK_MIN_PREMIUM_USD = 0.01
PEAK_MIN_PREMIUM_SHARE = 0.10


def _explained_pct(reconciled_cost: float | None, window_spend: float) -> float | None:
    if reconciled_cost is None or not window_spend or window_spend <= 0:
        return None
    return reconciled_cost / window_spend * 100.0


def _mode(values: list[float]) -> float:
    """The most common value, bucketed to the nearest 1k tokens.

    A conversation's prompt cache serves the same prefix each turn, so the
    per-turn cache-read count clusters on the prefix size. Bucketing keeps tiny
    variance from disguising the mode.
    """
    vals = [v for v in values if v]
    if not vals:
        return 0.0
    rounded = [round(v / 1000.0) * 1000.0 for v in vals]
    return Counter(rounded).most_common(1)[0][0]


def _conversation_signals(
    turns_by_conversation: dict[str, list[tuple[str, float, float]]],
) -> dict:
    """Detect a growing conversation whose prompt cache stays pinned.

    This is the ``cache_prefix_unstable`` signature: one conversation, context
    (prompt tokens) increasing turn over turn, while ``cache_read`` stays near
    the system-prompt size and the peak prompt is far larger than what the cache
    ever serves. Reports ``prefix_tokens`` / ``peak_prompt_tokens`` so the row is
    self-explanatory.
    """
    out = {
        "conversation_count": len(turns_by_conversation),
        "top_conversation_id": None,
        "prefix_tokens": None,
        "peak_prompt_tokens": None,
        "cache_prefix_unstable": False,
    }
    if not turns_by_conversation:
        return out
    top_id, turns = max(
        turns_by_conversation.items(), key=lambda kv: len(kv[1])
    )
    out["top_conversation_id"] = top_id
    ordered = sorted(turns, key=lambda t: t[0])
    prompts = [p for _, p, _ in ordered]
    reads = [r for _, _, r in ordered]
    prefix = _mode(reads)
    peak = max(prompts) if prompts else 0.0
    out["prefix_tokens"] = prefix
    out["peak_prompt_tokens"] = peak
    if (
        len(ordered) < CACHE_PREFIX_MIN_TURNS
        or prefix < CACHE_PREFIX_MIN_TOKENS
        or not prompts
    ):
        return out
    growing = all(b >= a for a, b in pairwise(prompts)) and (
        prompts[-1] >= CACHE_PREFIX_GROWTH * prompts[0]
    )
    if growing and peak >= CACHE_PREFIX_PROMPT_OVER_PREFIX * prefix:
        out["cache_prefix_unstable"] = True
    return out


def summarize(
    spans: list[dict],
    *,
    window_spend: float,
    window_start_utc: str | None = None,
    window_end_utc: str | None = None,
) -> dict:
    """Aggregate one window's span metrics into signals + per-model cost.

    Cost is reported twice, deliberately:

    - ``reconciled_cost_expected`` — the window's tokens priced at DeepSeek's
      published rate for the band each request ran in (the number the balance
      drop is reconciled against);
    - ``reconciled_cost_litellm`` — Σ ``litellm.cost.total``, the tracer's flat
      (peak-rate) figure, kept for reference and for the delta between them.

    Per-request bands come from each span's start time when the trace carries
    one; otherwise the whole window is assumed to sit in the band covering most
    of it (``window_start_utc`` / ``window_end_utc``). When a span has no token
    counts at all there is nothing to re-derive from, so the litellm figure is
    used as-is and ``pricing.source`` says so.
    """
    n = len(spans)
    cost = None
    input_tokens = uncached = cache_read = output_tokens = total_tokens = 0.0
    cost_count = 0
    errors = 0
    tool_calls = 0
    model_cost: Counter[str] = Counter()
    model_reqs: Counter[str] = Counter()
    top1_cost = 0.0
    fallback_band = _fallback_band(window_start_utc, window_end_utc)
    # band -> pricing model -> [cache_hit, cache_miss, output] tokens.
    band_tokens: dict[str, dict[str, list[float]]] = {}
    models_seen: set[str] = set()
    # conversation id -> [(start_time, prompt_tokens, cache_read_tokens)] — the
    # turn sequence that the `cache_prefix_unstable` signature is read from.
    conversation_turns: dict[str, list[tuple[str, float, float]]] = {}

    for span in spans:
        m = _span_metrics(span)
        conv_key = m.get("conversation_id")
        if conv_key:
            conversation_turns.setdefault(conv_key, []).append(
                (m.get("start_time") or "", m["input_tokens"], m["cache_read_tokens"])
            )
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

        model_key = pricing.normalize_model(m["model"])
        models_seen.add(model_key)
        band = _span_band(m, fallback_band)
        bucket = band_tokens.setdefault(band, {}).setdefault(model_key, [0.0, 0.0, 0.0])
        bucket[0] += m["cache_read_tokens"]
        bucket[1] += m["uncached_input_tokens"]
        bucket[2] += m["output_tokens"]

    covered_cost = cost  # may be None if no span had a cost attribute

    # What these tokens cost at the band they ran in, and at off-peak rates.
    expected = 0.0
    offpeak_equivalent = 0.0
    peak_premium = 0.0
    for band, models in band_tokens.items():
        for model, (hit, miss, out) in models.items():
            at_band = pricing.cost_of(
                model, cache_hit_tokens=hit, cache_miss_tokens=miss,
                output_tokens=out, band=band,
            )
            at_offpeak = pricing.cost_of(
                model, cache_hit_tokens=hit, cache_miss_tokens=miss,
                output_tokens=out, band=pricing.OFF_PEAK,
            )
            expected += at_band
            offpeak_equivalent += at_offpeak
            peak_premium += at_band - at_offpeak

    has_tokens = (input_tokens + output_tokens) > 0
    if has_tokens:
        reconciled = expected
        pricing_source = "tokens"
    else:
        # Nothing to re-derive from — trust the tracer rather than claim zero.
        reconciled = covered_cost
        expected = covered_cost
        offpeak_equivalent = covered_cost
        peak_premium = 0.0
        pricing_source = "litellm"

    overstatement = (
        covered_cost - expected
        if covered_cost is not None and expected is not None
        else None
    )
    window_pricing = {
        "band": _window_band(window_start_utc, window_end_utc),
        "peak_overlap_minutes": _peak_overlap_minutes(window_start_utc, window_end_utc),
        "peak_premium_usd": max(0.0, peak_premium) if expected is not None else None,
        "expected_cost": expected,
        "offpeak_equivalent_cost": offpeak_equivalent,
        "litellm_cost": covered_cost,
        "flat_peak_overstatement_usd": overstatement,
        "source": pricing_source,
        "models": sorted(models_seen),
    }

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
    conversations = _conversation_signals(conversation_turns)

    return {
        "conversation_count": conversations["conversation_count"],
        "top_conversation_id": conversations["top_conversation_id"],
        "prefix_tokens": conversations["prefix_tokens"],
        "peak_prompt_tokens": conversations["peak_prompt_tokens"],
        "cache_prefix_unstable": conversations["cache_prefix_unstable"],
        "request_count": n,
        "reconciled_cost": reconciled,
        "reconciled_cost_expected": expected,
        "reconciled_cost_litellm": covered_cost,
        "pricing": window_pricing,
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
        "explained_cost_pct": _explained_pct(reconciled, window_spend),
        "window_spend": window_spend,
    }


def _fallback_band(window_start_utc: str | None, window_end_utc: str | None) -> str:
    """Band assumed for spans with no timestamp of their own."""
    if window_start_utc and window_end_utc:
        try:
            return pricing.fallback_band(window_start_utc, window_end_utc)
        except ValueError:
            pass
    return pricing.OFF_PEAK


def _window_band(window_start_utc: str | None, window_end_utc: str | None) -> str:
    if window_start_utc and window_end_utc:
        try:
            return pricing.window_band(window_start_utc, window_end_utc)
        except ValueError:
            pass
    return pricing.OFF_PEAK


def _peak_overlap_minutes(window_start_utc: str | None, window_end_utc: str | None) -> float:
    if window_start_utc and window_end_utc:
        try:
            return pricing.peak_overlap_minutes(window_start_utc, window_end_utc)
        except ValueError:
            pass
    return 0.0


def _span_band(metrics: dict, fallback: str) -> str:
    """The band a single span ran in: its own start time, else the window's."""
    ts = metrics.get("start_time")
    if ts:
        try:
            return pricing.pricing_band(ts)
        except (ValueError, TypeError):
            pass
    return fallback


def _is_cent_quantization_limited(signals: dict) -> bool:
    """True when a low explained ratio is a cent-quantization artifact rather
    than genuinely lost spend.

    The DeepSeek balance source only moves in whole cents at the poll boundary,
    so a *small absolute* shortfall between the balance drop and the traced
    cost on cheap, cache-heavy traffic is a measurement floor — the classifier
    should not cry "investigate". A shortfall beyond the cent scale is real
    missing spend and stays ``investigate``.

    A window that overlaps a peak band gets the same treatment: at a band
    boundary the 2x premium and the grid-settlement lag routinely move a drop
    by a cent or two, so a cent-scale shortfall there is noise, not lost spend.
    """
    cost = signals["reconciled_cost"]
    window_spend = signals["window_spend"]
    if not cost or cost <= 0 or not window_spend or window_spend <= 0:
        return False
    if window_spend - cost > CENT_QUANTIZATION_MAX_GAP:
        return False
    ratio = signals["cache_hit_ratio"]
    if ratio is not None and ratio >= HIGH_ACTIVITY_MIN_CACHE_HIT:
        return True
    # Not cache-heavy — but a peak-boundary window's residual is explained by
    # the premium + settlement lag straddling the boundary.
    pricing = signals.get("pricing") or {}
    return (pricing.get("peak_overlap_minutes") or 0.0) > 0


def _peak_premium_material(premium: float | None, window_spend: float | None) -> bool:
    """True when the peak premium is worth naming (absolute and share floors)."""
    premium = premium or 0.0
    if premium < PEAK_MIN_PREMIUM_USD:
        return False
    return not (
        window_spend and window_spend > 0 and premium < PEAK_MIN_PREMIUM_SHARE * window_spend
    )


def _peak_note(signals: dict) -> str | None:
    """A one-clause note naming the peak premium, when there is one to name."""
    pricing = signals.get("pricing") or {}
    premium = pricing.get("peak_premium_usd") or 0.0
    overlap = pricing.get("peak_overlap_minutes") or 0.0
    if overlap <= 0 or not _peak_premium_material(premium, signals.get("window_spend")):
        return None
    return (
        f"~${premium:.2f} of this is the 2x peak-hour premium "
        f"({overlap:.0f} min of peak pricing, {pricing.get('band')} window)."
    )


def _double_count_suspected(signals: dict) -> bool:
    """True when the raw tracer sum betrays duplicated spans.

    The tracer (Σ ``litellm.cost.total``) is a flat *peak* price, so off-peak it
    already sits ~2x above the band-correct token-derived cost — that gap is
    expected, not evidence. A genuine double count pushes it past even that
    factor (roughly 2x *again*), or leaves tracer cost with no token basis at
    all. Only then should an over-100% ratio be read as double counting rather
    than a mis-sized denominator.
    """
    litellm = signals.get("reconciled_cost_litellm")
    if not litellm or litellm <= 0:
        return False
    expected = signals.get("reconciled_cost_expected")
    if not expected or expected <= 0:
        # Tracer cost with no tokens behind it: nothing to price it against.
        return True
    band = (signals.get("pricing") or {}).get("band")
    # Off-peak / mixed windows carry the flat-peak overstatement already.
    factor = DOUBLE_COUNT_TRACER_FACTOR if band in ("off_peak", "mixed") else 1.5
    return litellm > factor * expected


def classify(signals: dict) -> dict:
    """Pick the primary reason for a window from its aggregated signals."""
    n = signals["request_count"]
    cost = signals["reconciled_cost"]
    explained = signals["explained_cost_pct"]
    window_spend = signals.get("window_spend") or 0.0

    # No observable LLM traffic at all.
    if n == 0:
        return _mk("unexplained", investigate=True,
                   summary="No LLM spans were found in Phoenix for this window, yet spend "
                           "was recorded — trace coverage is missing or the cost is elsewhere.")

    # Traced cost can't account for the balance drop.
    if cost is None:
        return _mk("unexplained", investigate=True,
                   summary="Spans carry no cost attributes, so the spend can't be tied to LLM calls.")

    # No measurable drop to reconcile against: a zero or negative (top-up)
    # movement must not divide into a meaningless percentage — say so plainly.
    if window_spend <= 0:
        return _mk("unexplained", investigate=True,
                   summary=(f"No measurable balance drop for this window "
                            f"({_money(window_spend)}); {_money(cost)} of traced LLM cost "
                            "cannot be reconciled against a zero or negative movement."))

    # Attribution health, in band order: over-attributed cost is evidence of a
    # double count, so it is surfaced — but only when the tracer itself betrays
    # duplicated spans. An over-100% ratio with a sane tracer is a denominator
    # artifact, not actionable, and falls through to the traffic-shape rules.
    if (
        explained is not None
        and explained > OVER_FACTOR * 100
        and _double_count_suspected(signals)
    ):
        return _mk("over_attributed", investigate=True,
                   summary=(f"Traced LLM cost ({_money(cost)}) is ~{explained:.0f}% of the "
                            f"{_money(window_spend)} drop — more than the drop itself, and "
                            "the raw tracer sum is far above what these tokens cost at any "
                            "rate; cost looks double counted."))

    if explained is not None and explained < UNDER_FACTOR * 100:
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

    # A growing conversation whose prompt cache stays pinned is the signature
    # the burst layer exists to name; it beats the generic "bloated context"
    # rule below (which would otherwise fire on the same uncached-input mass).
    if signals.get("cache_prefix_unstable"):
        return _mk("cache_prefix_unstable", actionable=True,
                   summary=(f"Context grew to {_tokens(signals['peak_prompt_tokens'])} while "
                            f"the prompt cache stayed pinned at ~{_tokens(signals['prefix_tokens'])} "
                            f"per turn — {_tokens(signals['uncached_input_tokens'])} uncached input "
                            "across the conversation; the cache prefix is unstable."))

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

    # Peak pricing: the window sat in (or straddled) a published peak band and
    # the 2x rate is a material part of what it cost. Only reached when no
    # traffic-shape rule above fired — otherwise the shape is the primary
    # reason and the peak premium is attached to the summary as a note.
    pricing = signals.get("pricing") or {}
    premium = pricing.get("peak_premium_usd") or 0.0
    overlap = pricing.get("peak_overlap_minutes") or 0.0
    if overlap > 0 and _peak_premium_material(premium, signals["window_spend"]):
        return _mk(
            "peak_pricing",
            summary=(
                f"{overlap:.0f} min of this window fell in DeepSeek's peak band, "
                f"billing at 2x the off-peak rate — ~${premium:.2f} more than the "
                "same tokens would cost off-peak."
            ),
        )

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


def diagnose(
    spans: list[dict],
    *,
    window_spend: float,
    window_start_utc: str | None = None,
    window_end_utc: str | None = None,
    analyzed_at: str | None = None,
    burst_id: str | None = None,
    burst_start_utc: str | None = None,
    burst_end_utc: str | None = None,
    member_slice_count: int = 1,
    lag_slices: int = 0,
    burst_spend: float | None = None,
) -> dict:
    """Full diagnosis for a window: signals + classified reason.

    The window bounds are optional but recommended: they let pricing fall back
    to the right band for spans that carry no start time of their own.

    ``window_spend`` is the reconciliation **denominator** — the balance drop
    over the traced window (lag-padded), which is what ``explained_cost_pct``
    divides into. ``burst_spend`` is the separate, auditable sum of the burst's
    *member-slice* deltas (defaults to ``window_spend`` for a bare window); both
    are stored so the ratio can be re-derived from the row alone.

    ``burst_*`` / ``member_slice_count`` / ``lag_slices`` describe the burst the
    window belongs to (a bare slice is a one-member burst): the burst bounds,
    how many slices were folded in, and the lag (in slices) allowed when the
    spans were attributed. ``lag_slices`` defaults to 0 so a window diagnosed
    on its own is reconciled without a lag allowance; the analysis service
    passes the burst's configured lag.
    """
    signals = summarize(
        spans,
        window_spend=window_spend,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
    )
    decision = classify(signals)
    # When peak pricing is not the headline reason it is still a fact about the
    # window, so it rides along in the summary text.
    note = _peak_note(signals)
    if note and decision["reason"] != "peak_pricing":
        decision["summary"] = f"{decision['summary']} {note}"
    decision["analyzed_at"] = analyzed_at or datetime.now(UTC).isoformat()
    pricing = signals["pricing"]
    # Merge the top-level fields the DB stores + keep the signals for tuning.
    diag = {
        "reason": decision["reason"],
        "reason_label": REASONS[decision["reason"]],
        "signature": decision.get("signature"),
        "actionable": decision.get("actionable", False),
        "investigate": decision.get("investigate", False),
        "summary": decision["summary"],
        # `window_spend` is the drop over the traced window (the denominator);
        # `window_drop` mirrors it explicitly and `traced_cost` is the numerator
        # actually used, so the ratio is reproducible from the row alone.
        "window_spend": signals["window_spend"],
        "window_drop": signals["window_spend"],
        "traced_cost": signals["reconciled_cost"],
        "reconciled_cost": signals["reconciled_cost"],
        "reconciled_cost_expected": signals["reconciled_cost_expected"],
        "reconciled_cost_litellm": signals["reconciled_cost_litellm"],
        "pricing_band": pricing["band"],
        "peak_overlap_minutes": pricing["peak_overlap_minutes"],
        "peak_premium_usd": pricing["peak_premium_usd"],
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
        # Burst identity + granularity: which burst this row reconciles, how
        # many slices it folded, and the lag allowance used to attribute spans.
        "burst_id": burst_id or burst_start_utc or window_start_utc,
        "burst_start_utc": burst_start_utc or window_start_utc,
        "burst_end_utc": burst_end_utc or window_end_utc,
        "burst_spend": burst_spend if burst_spend is not None else signals["window_spend"],
        "member_slice_count": member_slice_count,
        "reconciled_cost_lag_slices": lag_slices,
        # Conversation context (drives / explains `cache_prefix_unstable`).
        "conversation_count": signals["conversation_count"],
        "top_conversation_id": signals["top_conversation_id"],
        "prefix_tokens": signals["prefix_tokens"],
        "peak_prompt_tokens": signals["peak_prompt_tokens"],
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
    prior = summarize(
        prior_spans,
        window_spend=window_spend,
        window_start_utc=prior_start_utc,
        window_end_utc=prior_end_utc,
    )
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
    note = _peak_note(prior)
    if note:
        decision["summary"] = f"{decision['summary']} {note}"
    decision["analyzed_at"] = analyzed_at or datetime.now(UTC).isoformat()
    pricing = prior["pricing"]
    diag = {
        **decision,
        "reason_label": REASONS["settled_from_prior_burst"],
        "window_spend": window_spend,
        "window_drop": window_spend,
        "traced_cost": cost,
        "reconciled_cost": cost,
        "reconciled_cost_expected": prior["reconciled_cost_expected"],
        "reconciled_cost_litellm": prior["reconciled_cost_litellm"],
        "pricing_band": pricing["band"],
        "peak_overlap_minutes": pricing["peak_overlap_minutes"],
        "peak_premium_usd": pricing["peak_premium_usd"],
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
        # Signature = the winning *traffic shape* (None for an attribution
        # verdict such as `unexplained` / `over_attributed`).
        "signature": reason if reason in SIGNATURES else None,
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
