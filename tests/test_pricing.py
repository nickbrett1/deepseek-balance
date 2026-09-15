"""Tests for the time-aware DeepSeek pricing model (``pricing.py``).

The published table is fixed data, so these assert the *behaviour* that matters:
the peak schedule (01:00-04:00 and 06:00-10:00 UTC, Mon-Fri), the 2x peak
multiplier, and the window helpers the heuristics lean on.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from deepseek_balance import pricing
from deepseek_balance.pricing import OFF_PEAK, PEAK

# 2026-09-15 is a Tuesday; 2026-09-19 is a Saturday (from the memo's timeline).


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


@pytest.mark.parametrize(
    "ts,expected",
    [
        ("2026-09-15T01:00:00+00:00", PEAK),      # Tue, in 01:00-04:00
        ("2026-09-15T03:59:00+00:00", PEAK),
        ("2026-09-15T04:00:00+00:00", OFF_PEAK),  # band end is half-open
        ("2026-09-15T06:00:00+00:00", PEAK),      # Tue, in 06:00-10:00
        ("2026-09-15T09:59:00+00:00", PEAK),
        ("2026-09-15T10:00:00+00:00", OFF_PEAK),
        ("2026-09-15T00:59:00+00:00", OFF_PEAK),
        ("2026-09-15T19:35:00+00:00", OFF_PEAK),
        ("2026-09-19T02:00:00+00:00", OFF_PEAK),  # Saturday: never peak
        ("2026-09-20T07:00:00+00:00", OFF_PEAK),  # Sunday
    ],
)
def test_pricing_band(ts, expected):
    assert pricing.pricing_band(ts) == expected


def test_pricing_band_accepts_datetime_and_iso():
    assert pricing.pricing_band(_dt("2026-09-15T02:00:00+00:00")) == PEAK
    assert pricing.pricing_band("2026-09-15T02:00:00Z") == PEAK
    assert pricing.pricing_band("2026-09-15T02:00:00") == PEAK  # naive => UTC


def test_peak_rates_are_double_off_peak():
    for model, bands in pricing.models().items():
        off, peak = bands[OFF_PEAK], bands[PEAK]
        assert peak.cache_hit == pytest.approx(2 * off.cache_hit), model
        assert peak.cache_miss == pytest.approx(2 * off.cache_miss), model
        assert peak.output == pytest.approx(2 * off.output), model


def test_cost_of_matches_published_table():
    # 1M of each bucket: off-peak 0.003 + 0.15 + 0.60; peak is double.
    kwargs = {
        "cache_hit_tokens": 1_000_000,
        "cache_miss_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }
    assert pricing.cost_of("deepseek-flash", band=OFF_PEAK, **kwargs) == pytest.approx(0.753)
    assert pricing.cost_of("deepseek-flash", band=PEAK, **kwargs) == pytest.approx(1.506)
    # v4-pro: off-peak 0.022 + 0.66 + 1.98.
    assert pricing.cost_of("deepseek-v4-pro", band=OFF_PEAK, **kwargs) == pytest.approx(2.662)


def test_litellm_flat_price_is_the_peak_column():
    """LiteLLM's flat DeepSeek price equals the published peak rate.

    This is the premise of the whole change: traced cost is peak-priced even in
    off-peak windows, so the tracer is 2x high off-peak, never low.
    """
    kwargs = {
        "cache_hit_tokens": 1_000_000,
        "cache_miss_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }
    # model_prices_and_context_window.json: deepseek-flash $0.30 in / $1.20 out,
    # cache read $0.006 per 1M -> exactly the peak column.
    litellm_flat = 0.30 + 1.20 + 0.006
    peak_cost = pricing.cost_of("deepseek-flash", band=PEAK, **kwargs)
    off_peak_cost = pricing.cost_of("deepseek-flash", band=OFF_PEAK, **kwargs)
    assert litellm_flat == pytest.approx(1.506)
    assert peak_cost == pytest.approx(litellm_flat)
    assert 2 * off_peak_cost == pytest.approx(litellm_flat)


def test_model_aliases_and_unknown_default():
    assert pricing.normalize_model("deepseek/deepseek-v4-flash") == "deepseek-flash"
    assert pricing.normalize_model("deepseek-v4-flash-vision-exp") == "deepseek-flash"
    assert pricing.normalize_model("DeepSeek-V4-Pro") == "deepseek-v4-pro"
    assert pricing.normalize_model("deepseek-chat") == pricing.DEFAULT_MODEL
    assert pricing.normalize_model(None) == pricing.DEFAULT_MODEL
    assert pricing.is_known_model("deepseek-v4-pro")
    assert not pricing.is_known_model("deepseek-chat")


def test_peak_overlap_minutes_across_the_boundary():
    # 00:57 -> 01:02 on a Tuesday: only 01:00-01:02 is peak.
    assert pricing.peak_overlap_minutes(
        "2026-09-15T00:57:00+00:00", "2026-09-15T01:02:00+00:00"
    ) == pytest.approx(2.0)
    # A whole off-peak window.
    assert pricing.peak_overlap_minutes(
        "2026-09-15T20:00:00+00:00", "2026-09-15T20:05:00+00:00"
    ) == 0.0
    # ...and a whole peak one.
    assert pricing.peak_overlap_minutes(
        "2026-09-15T01:00:00+00:00", "2026-09-15T01:05:00+00:00"
    ) == pytest.approx(5.0)


def test_window_band_and_fallback():
    assert pricing.window_band(
        "2026-09-15T01:00:00+00:00", "2026-09-15T01:05:00+00:00"
    ) == PEAK
    assert pricing.window_band(
        "2026-09-15T20:00:00+00:00", "2026-09-15T20:05:00+00:00"
    ) == OFF_PEAK
    # Straddling the boundary is reported as mixed...
    assert pricing.window_band(
        "2026-09-15T00:57:00+00:00", "2026-09-15T01:02:00+00:00"
    ) == pricing.MIXED
    # ...while the single-band fallback takes the majority of the window.
    assert pricing.fallback_band(
        "2026-09-15T00:57:00+00:00", "2026-09-15T01:02:00+00:00"
    ) == OFF_PEAK
    assert pricing.fallback_band(
        "2026-09-15T00:59:00+00:00", "2026-09-15T01:04:00+00:00"
    ) == PEAK
