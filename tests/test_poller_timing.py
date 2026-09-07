"""Poller cadence: wall-clock anchoring + scheduled_ts recording.

Regression guards for the "poller timing is not robust" fix — the poller must
fire on round clock boundaries (not on a free-running interval since process
start) and must record the grid slot it was meant to cover separately from the
completion timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from deepseek_balance.app import _poll_trigger
from deepseek_balance.poller import BalancePoller, floor_interval, parse_interval


@pytest.mark.parametrize(
    "value,expected_minute_step",
    [("1m", None), ("5m", 5), ("15m", 15), ("30m", 30)],
)
def test_poll_trigger_is_cron_anchored(value, expected_minute_step):
    trigger = _poll_trigger(parse_interval(value))
    assert isinstance(trigger, CronTrigger)
    minute = trigger.fields[trigger.FIELD_NAMES.index("minute")]
    exprs = list(minute.expressions)
    # '1m' -> every minute ('*'); otherwise a step expression on the minute.
    assert len(exprs) == 1
    if expected_minute_step is None:
        assert exprs[0].step is None
    else:
        assert exprs[0].step == expected_minute_step


@pytest.mark.parametrize("value", ["7m", "60m", "1h", "30s"])
def test_poll_trigger_falls_back_to_interval(value):
    # Periods that can't be expressed as a whole-minute cron grid.
    assert isinstance(_poll_trigger(parse_interval(value)), IntervalTrigger)


@pytest.mark.parametrize("seconds", [60, 300, 900])
def test_floor_interval_lands_on_grid_boundary(seconds):
    now = datetime.now(UTC)
    floored = floor_interval(now, seconds)
    assert floored.tzinfo is not None
    assert floored.second == 0
    assert floored.microsecond == 0
    # Boundary must be <= now and within one period.
    assert floored <= now
    assert (now - floored).total_seconds() < seconds


def test_poller_records_scheduled_boundary_separate_from_completion():
    resp = httpx.Response(
        200,
        json={
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "5.0"}],
        },
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: resp))

    stored: list[dict] = []

    class FakeDB:
        def insert_snapshot(self, **kw):
            stored.append(kw)

    poller = BalancePoller(FakeDB(), "k", client=client, interval_seconds=300)
    snap = poller.poll_once()

    assert snap is not None
    # scheduled_ts is on a :00 boundary; ts is completion and may be later.
    scheduled = datetime.fromisoformat(snap["scheduled_ts"])
    assert (scheduled.minute % 5) == 0 and scheduled.second == 0
    assert snap["ts"] >= snap["scheduled_ts"]
    # Stored through to the DB layer.
    assert stored[0]["scheduled_ts"] == snap["scheduled_ts"]


def test_reconciliation_attributes_drop_to_scheduled_boundary_not_completion():
    """A drop must be keyed to the poll's scheduled grid slot, not its ts."""
    from deepseek_balance.analytics import _collect_drops

    rows = [
        {"ts": "2026-09-07T19:04:58.500+00:00", "scheduled_ts": "2026-09-07T19:05:00+00:00", "total_balance": 100.0},
        # completion is 19:08 (past the :05 boundary it was scheduled for).
        {"ts": "2026-09-07T19:08:02.100+00:00", "scheduled_ts": "2026-09-07T19:05:00+00:00", "total_balance": 90.0},
    ]
    drops = _collect_drops(rows, max_gap_minutes=30)
    assert len(drops) == 1
    to_ts, drop = drops[0]
    assert drop == pytest.approx(10.0)
    # Attributed to the scheduled boundary (19:05:00), not completion (19:08).
    assert to_ts == datetime(2026, 9, 7, 19, 5, 0, tzinfo=UTC)

