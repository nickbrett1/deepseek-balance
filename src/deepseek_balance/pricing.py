"""DeepSeek's published pricing, modelled with its time-of-day bands.

DeepSeek bills two bands. Peak hours are **01:00-04:00 and 06:00-10:00 UTC,
Monday to Friday**; every other hour is off-peak and costs *half* the peak
rate (https://api-docs.deepseek.com/quick_start/pricing/).

Why this module exists: the traced cost this app reconciles against
(``litellm.cost.total``, recorded by LiteLLM and read off the Phoenix span) is a
*single flat price per model*. For the DeepSeek models in play that flat price
equals the published **peak** column, unconditionally — so an off-peak window is
charged the peak rate and traced cost is ~2x the truth. Confirmed against
LiteLLM's own ``model_prices_and_context_window.json``:

======================  ==========================  ====================
model                   litellm (per 1M tokens)     published band
======================  ==========================  ====================
``deepseek-flash``      $0.30 in / $1.20 out         peak
``deepseek-v4-pro``     $1.32 in / $3.96 out         peak
======================  ==========================  ====================

So the tracer over-charges in off-peak windows and is correct in peak windows.
This module re-derives what a window *should* have cost from its token counts
and the band each request ran in, and quantifies the peak premium.

Everything here is pure: no I/O, no config, no clocks. Timestamps in, numbers
out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

# Band names used throughout (DB columns, payloads, reason keys).
PEAK = "peak"
OFF_PEAK = "off_peak"
MIXED = "mixed"

# Published peak windows, UTC, Monday-Friday, as half-open [start_hour, end_hour).
PEAK_BANDS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


class Rates(NamedTuple):
    """USD per 1M tokens for one model in one band."""

    cache_hit: float
    cache_miss: float
    output: float


# USD per 1M tokens, per model, per band, straight from the pricing page.
_TABLE: dict[str, dict[str, Rates]] = {
    "deepseek-flash": {
        OFF_PEAK: Rates(0.003, 0.15, 0.60),
        PEAK: Rates(0.006, 0.30, 1.20),
    },
    "deepseek-v4-pro": {
        OFF_PEAK: Rates(0.022, 0.66, 1.98),
        PEAK: Rates(0.044, 1.32, 3.96),
    },
}

# The flash model is the one this deployment bills against (deepseek-v4-flash is
# a retired alias of deepseek-v4.1-flash and is served/billed at the Flash
# price). Anything unrecognised is priced as Flash and reported as unmapped.
DEFAULT_MODEL = "deepseek-flash"

# Span model names (provider prefixes and the legacy aliases) → table key.
_MODEL_ALIASES: dict[str, str] = {
    "deepseek-flash": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v4.1-flash": "deepseek-flash",
    "deepseek-v4p1-flash": "deepseek-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-v4-pro-0813": "deepseek-v4-pro",
    "deepseek-v4.1-pro": "deepseek-v4-pro",
}


def _as_datetime(value: datetime | str) -> datetime:
    """Coerce a datetime or ISO-8601 string to an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise TypeError(f"unsupported timestamp: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def is_peak(ts: datetime | str) -> bool:
    """True when ``ts`` falls in a published peak window (UTC, Mon-Fri)."""
    dt = _as_datetime(ts)
    if dt.weekday() >= 5:  # Saturday / Sunday
        return False
    return any(start <= dt.hour < end for start, end in PEAK_BANDS_UTC)


def pricing_band(ts: datetime | str) -> str:
    """The pricing band a single instant falls in: ``"peak"`` or ``"off_peak"``."""
    return PEAK if is_peak(ts) else OFF_PEAK


def peak_overlap_minutes(start: datetime | str, end: datetime | str) -> float:
    """Minutes of the half-open window ``[start, end)`` that are peak-priced.

    Peak bands are whole-hour aligned, so this walks the hour boundaries the
    window touches — exact, and cheap even for a day-wide window.
    """
    start_dt = _as_datetime(start)
    end_dt = _as_datetime(end)
    if end_dt <= start_dt:
        return 0.0
    seconds = 0.0
    cur = start_dt
    while cur < end_dt:
        top_of_hour = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        nxt = min(end_dt, top_of_hour)
        if is_peak(cur):
            seconds += (nxt - cur).total_seconds()
        cur = nxt
    return seconds / 60.0


def window_band(start: datetime | str, end: datetime | str) -> str:
    """The band of a window: ``"peak"``, ``"off_peak"`` or ``"mixed"``.

    ``"mixed"`` means the window straddles a band boundary — some of it is
    peak-priced and some is not (see :func:`peak_overlap_minutes`).
    """
    start_dt = _as_datetime(start)
    end_dt = _as_datetime(end)
    total = (end_dt - start_dt).total_seconds()
    if total <= 0:
        return OFF_PEAK
    peak = peak_overlap_minutes(start_dt, end_dt) * 60.0
    if peak <= 0:
        return OFF_PEAK
    if peak >= total:
        return PEAK
    return MIXED


def fallback_band(start: datetime | str, end: datetime | str) -> str:
    """A single band to assume for a span that carries no timestamp.

    When a window straddles a boundary we take the band covering the majority
    of it, so an untimed request is priced the way most of its window was.
    """
    start_dt = _as_datetime(start)
    end_dt = _as_datetime(end)
    total = (end_dt - start_dt).total_seconds()
    if total <= 0:
        return OFF_PEAK
    peak = peak_overlap_minutes(start_dt, end_dt) * 60.0
    return PEAK if peak * 2 >= total else OFF_PEAK


def normalize_model(model: str | None) -> str:
    """Map a span's model name onto a table key (defaulting to Flash)."""
    if not model:
        return DEFAULT_MODEL
    key = str(model).strip().lower().split("/")[-1]
    return _MODEL_ALIASES.get(key, DEFAULT_MODEL)


def is_known_model(model: str | None) -> bool:
    """True when ``model`` maps to an explicit table entry (not the default)."""
    if not model:
        return False
    return str(model).strip().lower().split("/")[-1] in _MODEL_ALIASES


def rates_for(model: str | None, band: str) -> Rates:
    """Published per-1M-token rates for ``model`` in ``band``."""
    table = _TABLE.get(normalize_model(model), _TABLE[DEFAULT_MODEL])
    return table.get(band, table[OFF_PEAK])


def cost_of(
    model: str | None,
    *,
    cache_hit_tokens: float,
    cache_miss_tokens: float,
    output_tokens: float,
    band: str,
) -> float:
    """USD that these tokens cost for ``model`` in ``band``."""
    rates = rates_for(model, band)
    return (
        cache_hit_tokens * rates.cache_hit
        + cache_miss_tokens * rates.cache_miss
        + output_tokens * rates.output
    ) / 1_000_000.0


def models() -> dict[str, dict[str, Rates]]:
    """A copy of the published table (for introspection / tests)."""
    return {name: dict(bands) for name, bands in _TABLE.items()}
