"""Minimal read-only client for Arize Phoenix traces.

Lets the analysis layer pull LLM spans over a time window so it can explain
why a spend interval was unusually high. Talks to the unauthenticated REST API
documented in the "phoenix-query-interface" memo (Phoenix container on the
``ai_proxy`` network at ``http://phoenix:6006``, data in project ``default``).

Only what the heuristics need is exposed here: fetch LLM spans in a window
(fully paginated) and small helpers to read span attributes by dotted path.
Everything is deliberately dumb so tests can run against fixture JSON without
a live Phoenix.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("deepseek_balance.phoenix")

DEFAULT_BASE_URL = "http://localhost:6006"
DEFAULT_PROJECT = "default"
SPANS_PATH = "/v1/projects/{project}/spans"
PAGE_SIZE = 200


def attr(span: dict, path: str) -> Any:
    """Read a span attribute by dotted path.

    Phoenix spans carry ``attributes`` as a map whose keys may be dotted (e.g.
    ``"gen_ai.usage.input_tokens"``) or nested. This resolves ``path`` against
    the attributes map either way, returning None when absent.
    """
    attributes = span.get("attributes") or {}
    parts = path.split(".")

    # Exact-key first (Phoenix often stores flattened dotted keys).
    if path in attributes:
        return attributes[path]

    # Fall back to walking nested dicts.
    node: Any = attributes
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def span_metrics(span: dict) -> dict:
    """Extract the metrics the heuristics care about from one LLM span.

    Returns a flat dict with defaulted zeros so aggregation is simple. Values
    that are genuinely absent come back as ``None`` cost and ``0`` counts.
    """
    input_tokens = _number(attr(span, "gen_ai.usage.input_tokens"))
    output_tokens = _number(
        attr(span, "gen_ai.usage.completion_tokens")
    )
    if output_tokens is None:
        output_tokens = _number(attr(span, "gen_ai.usage.output_tokens"))
    cache_read = _number(attr(span, "gen_ai.usage.cache_read.input_tokens"))
    total_tokens = _number(attr(span, "gen_ai.usage.total_tokens"))
    cost = _number(attr(span, "litellm.cost.total"))

    if cache_read is None:
        # Some providers report cache reads flat under usage.
        cache_read = _number(attr(span, "gen_ai.usage.prompt_tokens_details.cached_tokens"))
    if cache_read is None:
        cache_read = 0.0
    if input_tokens is None:
        input_tokens = 0.0
    if total_tokens is None:
        total_tokens = input_tokens + (output_tokens or 0.0)

    uncached_input = max(0.0, input_tokens - cache_read)

    finish = attr(span, "gen_ai.response.finish_reasons")
    if isinstance(finish, str):
        finish = [finish]
    finish = finish or []
    is_tool_call = any(str(f).lower() in ("tool_calls", "tool_call") for f in finish)

    model = (
        attr(span, "gen_ai.response.model")
        or attr(span, "litellm.provider.model")
        or attr(span, "gen_ai.request.model")
    )
    status = (span.get("status_code") or "").upper()

    return {
        "cost": cost,
        "input_tokens": input_tokens,
        "uncached_input_tokens": uncached_input,
        "cache_read_tokens": cache_read,
        "output_tokens": output_tokens or 0.0,
        "total_tokens": total_tokens or 0.0,
        "cache_hit_ratio": (cache_read / input_tokens) if input_tokens else None,
        "is_tool_call": bool(is_tool_call),
        "model": model,
        "status_code": status,
        "is_error": status == "ERROR",
    }


class PhoenixClient:
    """Read-only client for Phoenix trace spans over a window."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        project: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.project = project or DEFAULT_PROJECT
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def fetch_llm_spans(self, start_iso: str, end_iso: str, *, pad_seconds: int = 0) -> list[dict]:
        """Fully paginate LLM spans with ``start_iso <= start < end_iso``.

        ``pad_seconds`` widens the window on both sides so a trace whose start
        barely straddles the interval boundary is still caught (Phoenix bounds
        on trace/span *start* time). Returns the raw span dicts in arrival order.
        """
        spans: list[dict] = []
        if pad_seconds:
            try:
                start = _shift_iso(start_iso, -pad_seconds)
                end = _shift_iso(end_iso, pad_seconds)
            except ValueError:
                start, end = start_iso, end_iso
        else:
            start, end = start_iso, end_iso

        params: dict[str, Any] = {
            "start_time": start,
            "end_time": end,
            "span_kind": "LLM",
            "limit": PAGE_SIZE,
        }
        cursor: str | None = None
        while True:
            if cursor:
                params["cursor"] = cursor
            else:
                params.pop("cursor", None)
            url = self.base_url + SPANS_PATH.format(project=self.project)
            try:
                resp = self._client.get(url, params=params)
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPError as exc:
                logger.warning("Phoenix spans fetch failed: %s", exc)
                raise

            data = body.get("data") or body.get("spans") or []
            spans.extend(data)
            cursor = body.get("next_cursor")
            if not cursor:
                break
            if len(spans) > 20_000:  # safety valve
                logger.warning("Phoenix span fetch exceeded 20k cap; stopping")
                break
        return spans

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _shift_iso(iso: str, seconds: int) -> str:
    from datetime import UTC, datetime, timedelta

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt + timedelta(seconds=seconds)).isoformat()


def client_from_env() -> PhoenixClient:
    """Build a PhoenixClient from environment configuration (never raises on
    missing config — callers decide whether Phoenix is configured)."""
    import os

    return PhoenixClient(
        base_url=os.environ.get("PHOENIX_BASE_URL"),
        project=os.environ.get("PHOENIX_PROJECT", DEFAULT_PROJECT),
    )


def is_configured() -> bool:
    """True when Phoenix is configured (an explicit base URL was supplied)."""
    import os

    return bool(os.environ.get("PHOENIX_BASE_URL"))
