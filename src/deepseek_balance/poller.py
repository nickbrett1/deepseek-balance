"""DeepSeek balance poller.

Fetches `GET /user/balance` from the DeepSeek platform API and writes one
snapshot row per run. Failures insert explicit gap rows (never silently
skipped), with a simple circuit breaker that stops hammering the API after a
burst of consecutive failures and resumes once a recovery window elapses.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime

import httpx

from .db import BalanceDB

logger = logging.getLogger("deepseek_balance.poller")

API_URL = "https://api.deepseek.com/user/balance"
DEEPSEEK_KEY = "DEEPSEEK_API_KEY"

# Retry/backoff: 2 retries, 60s apart.
MAX_RETRIES = 2
RETRY_DELAY_S = 60
# Circuit breaker: trip after N consecutive failures, open for `recovery_timeout`.
BREAKER_THRESHOLD = 5
BREAKER_RECOVERY_S = 60

_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*(s|m|h|d)\s*$", re.IGNORECASE)
_UNIT_TO_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval(value: str) -> int:
    """Parse a `POLL_INTERVAL`-style value (`15m`, `30s`, `2h`) into seconds."""
    m = _INTERVAL_RE.match(value)
    if not m:
        raise ValueError(
            f"invalid interval {value!r}; expected e.g. 30s, 15m, 2h, 1d"
        )
    return int(m.group(1)) * _UNIT_TO_SECONDS[m.group(2).lower()]


class CircuitBreaker:
    """Tiny circuit breaker: open after N failures, auto-recovers after a window."""

    def __init__(self, threshold: int = BREAKER_THRESHOLD, recovery_timeout: float = BREAKER_RECOVERY_S) -> None:
        self.threshold = threshold
        self.recovery_timeout = recovery_timeout
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        # Allow a trial (half-open) once the recovery window has elapsed.
        return (time.monotonic() - self._opened_at) < self.recovery_timeout

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = time.monotonic()


class BalancePoller:
    def __init__(self, db: BalanceDB, api_key: str | None, client: httpx.Client | None = None) -> None:
        self.db = db
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=30.0)
        self.breaker = CircuitBreaker()

    def poll_once(self) -> dict | None:
        """Run one poll cycle. Returns the snapshot dict that was stored (or None if skipped)."""
        if self.breaker.is_open:
            logger.warning("circuit breaker open — skipping poll (resume after recovery window)")
            return None
        if not self.api_key:
            logger.warning("DEEPSEEK_API_KEY not set — recording gap row")
            snapshot = self._gap_snapshot("no api key", http_status=None, is_available=False)
            self._store(snapshot)
            return snapshot

        snapshot = self._fetch_with_retry()
        self._store(snapshot)
        if snapshot["is_available"]:
            self.breaker.record_success()
        else:
            self.breaker.record_failure()
        return snapshot

    def _fetch_with_retry(self) -> dict:
        raw_text = None
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self.client.get(
                    API_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                raw_text = resp.text
                if resp.status_code == 200:
                    return self._parse_success(raw_text)
                # Non-200 → explicit gap row carrying http_status.
                return self._gap_snapshot(raw_text, http_status=resp.status_code, is_available=False)
            except httpx.HTTPError as exc:  # network-level failure
                last_error = exc
                if attempt < MAX_RETRIES:
                    logger.warning("request failed (attempt %d): %s — retrying", attempt + 1, exc)
                    time.sleep(RETRY_DELAY_S)
        # All attempts failed at the network level.
        return self._gap_snapshot(f"error: {last_error}", http_status=None, is_available=False)

    def _parse_success(self, raw_text: str) -> dict:
        data = json.loads(raw_text)
        info = (data.get("balance_infos") or [{}])[0]
        is_available = bool(data.get("is_available"))
        if not is_available:
            return self._gap_snapshot(raw_text, http_status=200, is_available=False)
        return {
            "ts": datetime.now(UTC).isoformat(),
            "currency": info.get("currency"),
            "total_balance": _to_float(info.get("total_balance")),
            "granted_balance": _to_float(info.get("granted_balance")),
            "topped_up_balance": _to_float(info.get("topped_up_balance")),
            "is_available": True,
            "http_status": 200,
            "raw": raw_text,
        }

    def _gap_snapshot(self, raw_text: str, *, http_status: int | None, is_available: bool) -> dict:
        """An explicit 'gap' row: no balance, but records the failure."""
        return {
            "ts": datetime.now(UTC).isoformat(),
            "currency": None,
            "total_balance": None,
            "granted_balance": None,
            "topped_up_balance": None,
            "is_available": is_available,
            "http_status": http_status,
            "raw": raw_text,
        }

    def _store(self, snapshot: dict) -> None:
        self.db.insert_snapshot(
            ts=snapshot["ts"],
            currency=snapshot["currency"],
            total_balance=snapshot["total_balance"],
            granted_balance=snapshot["granted_balance"],
            topped_up_balance=snapshot["topped_up_balance"],
            is_available=snapshot["is_available"],
            http_status=snapshot["http_status"],
            raw=snapshot["raw"],
        )


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
