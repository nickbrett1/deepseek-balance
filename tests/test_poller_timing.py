"""Poller cadence: wall-clock anchoring + scheduled_ts recording.

Regression guards for the "poller timing is not robust" fix — the poller must
fire on round clock boundaries (not on a free-running interval since process
start) and must record the grid slot it was meant to cover separately from the
completion timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def test_drop_slice_index_attributes_to_preceding_window():
    """A drop seen at a :00 boundary must reconcile to the slice that *ends*
    there (the preceding window [t-5m, t]), not the one that starts there."""
    from deepseek_balance.analytics import _drop_slice_index

    origin = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    # Drop becomes visible at 19:00; the preceding 5-min slice starts 18:55.
    idx = _drop_slice_index(datetime(2026, 9, 7, 19, 0, 0, tzinfo=UTC), origin, 300)
    assert idx == 227
    assert (origin + timedelta(seconds=idx * 300)).strftime("%H:%M") == "18:55"


def test_spend_intervals_buckets_drop_one_slice_earlier(tmp_path):
    """The memo case: a cent drop that appears at 19:00 is bucketed to the
    18:55–19:00 window, so Phoenix is later queried over where the spend really
    happened rather than the (empty) following window."""
    from deepseek_balance.analytics import spend_intervals
    from deepseek_balance.db import BalanceDB

    db = BalanceDB(str(tmp_path / "w.db"))
    # Whole-cent drop: 100.00 -> 99.96, observed at the 19:00 scheduled slot.
    db.insert_snapshot(
        ts="2026-09-07T18:54:58+00:00", scheduled_ts="2026-09-07T18:55:00+00:00",
        currency="CNY", total_balance=100.0, granted_balance=100.0,
        topped_up_balance=0.0, is_available=True, http_status=200, raw="{}",
    )
    db.insert_snapshot(
        ts="2026-09-07T19:00:02+00:00", scheduled_ts="2026-09-07T19:00:00+00:00",
        currency="CNY", total_balance=99.96, granted_balance=99.96,
        topped_up_balance=0.0, is_available=True, http_status=200, raw="{}",
    )
    now = datetime(2026, 9, 7, 19, 30, tzinfo=UTC)
    si = spend_intervals(
        db, now, spend_slice_minutes=5,
        summary_start_utc=datetime(2026, 9, 7, 18, 30, tzinfo=UTC),
    )
    intervals = [i for i in si["intervals"] if i["spend"] > 0]
    assert len(intervals) == 1
    # Preceding window [18:55, 19:00], not the following [19:00, 19:05].
    assert intervals[0]["ts"] == "2026-09-07T18:55:00+00:00"
    assert intervals[0]["spend"] == pytest.approx(0.04)

