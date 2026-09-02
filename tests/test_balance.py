"""Tests for deepseek-balance: poller, storage, endpoints and interval parsing."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from deepseek_balance import mcp_server
from deepseek_balance.analytics import daily_heartbeat
from deepseek_balance.app import app
from deepseek_balance.db import BalanceDB
from deepseek_balance.poller import BalancePoller, CircuitBreaker, parse_interval

SAMPLE_OK = {
    "is_available": True,
    "balance_infos": [
        {
            "currency": "CNY",
            "total_balance": "110.00",
            "granted_balance": "10.00",
            "topped_up_balance": "100.00",
        }
    ],
}


# --- interval parsing ------------------------------------------------------

def test_parse_interval_units():
    assert parse_interval("30s") == 30
    assert parse_interval("15m") == 900
    assert parse_interval("2h") == 7200
    assert parse_interval("1d") == 86400
    assert parse_interval(" 5 m ") == 300


def test_parse_interval_invalid():
    with pytest.raises(ValueError):
        parse_interval("bogus")


# --- circuit breaker -------------------------------------------------------

def test_circuit_breaker_trips_and_recovers():
    cb = CircuitBreaker(threshold=3, recovery_timeout=0.01)
    assert not cb.is_open
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open
    cb.record_failure()
    assert cb.is_open
    # after recovery window elapses it becomes try-able again
    import time

    time.sleep(0.02)
    assert not cb.is_open
    cb.record_success()
    assert not cb.is_open


# --- poller ----------------------------------------------------------------

def _client_that_returns(body: dict, status: int = 200):
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=body)
        )
    )


def test_poller_success_inserts_row(tmp_path):
    db = BalanceDB(str(tmp_path / "test.db"))
    poller = BalancePoller(db=db, api_key="k", client=_client_that_returns(SAMPLE_OK))
    snap = poller.poll_once()
    assert snap["is_available"] is True
    assert snap["http_status"] == 200
    assert snap["total_balance"] == 110.0
    assert snap["granted_balance"] == 10.0
    assert snap["topped_up_balance"] == 100.0
    latest = db.latest()
    assert latest["total_balance"] == 110.0
    assert latest["currency"] == "CNY"
    db.close()


def test_poller_non200_inserts_gap_row(tmp_path):
    db = BalanceDB(str(tmp_path / "test.db"))
    poller = BalancePoller(db=db, api_key="k", client=_client_that_returns({"error": "nope"}, status=500))
    snap = poller.poll_once()
    assert snap["is_available"] is False
    assert snap["http_status"] == 500
    assert snap["total_balance"] is None
    # gap row recorded, latest still empty
    assert db.latest() is None
    import sqlite3

    raw = sqlite3.connect(str(tmp_path / "test.db"))
    count = raw.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0]
    status = raw.execute("SELECT http_status FROM balance_snapshots").fetchone()[0]
    raw.close()
    assert count == 1
    assert status == 500
    db.close()


def test_poller_no_key_inserts_gap_row(tmp_path):
    db = BalanceDB(str(tmp_path / "test.db"))
    poller = BalancePoller(db=db, api_key=None)
    snap = poller.poll_once()
    assert snap["is_available"] is False
    assert snap["http_status"] is None
    db.close()


def test_poller_is_available_false_inserts_gap(tmp_path):
    body = {"is_available": False, "balance_infos": []}
    db = BalanceDB(str(tmp_path / "test.db"))
    poller = BalancePoller(db=db, api_key="k", client=_client_that_returns(body))
    snap = poller.poll_once()
    assert snap["is_available"] is False
    assert snap["http_status"] == 200
    assert snap["total_balance"] is None
    db.close()


# --- app endpoints ---------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("POLL_INTERVAL", "1h")
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_latest_empty(client):
    r = client.get("/balance/latest")
    assert r.status_code == 200
    assert "error" in r.json()


def test_history_empty(client):
    r = client.get("/balance/history")
    assert r.status_code == 200
    assert r.json() == {"points": []}


def test_history_with_data(client):
    # seed rows relative to "now" so the test never depends on the wall clock
    db = client.app.state.db
    now = datetime.now(UTC)
    for i, bal in enumerate([10.0, 20.0, 30.0]):
        db.insert_snapshot(
            ts=(now - timedelta(minutes=2 - i)).isoformat(),
            currency="CNY",
            total_balance=bal,
            granted_balance=1.0,
            topped_up_balance=0.0,
            is_available=True,
            http_status=200,
            raw=json.dumps(SAMPLE_OK),
        )
    r = client.get("/balance/history?hours=1&step=1h")
    assert r.status_code == 200
    pts = r.json()["points"]
    assert pts and pts[-1]["total_balance"] == 30.0


def test_index_serves_widget(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "DeepSeek Balance" in r.text
    assert "Today’s heartbeat" in r.text
    assert "spend_summary" in r.text


# --- daily heartbeat analytics ----------------------------------------------

def _insert(db, ts, bal, granted=1.0, topped=0.0):
    db.insert_snapshot(
        ts=ts,
        currency="CNY",
        total_balance=bal,
        granted_balance=granted,
        topped_up_balance=topped,
        is_available=True,
        http_status=200,
        raw="{}",
    )


def test_daily_heartbeat_baseline(tmp_path):
    db = BalanceDB(str(tmp_path / "h.db"))
    # Yesterday: 100 -> 90 (a ¥10 day).
    _insert(db, "2026-01-14T08:00:00+00:00", 100.0)
    _insert(db, "2026-01-14T20:00:00+00:00", 90.0)
    # Today (so far): 90 -> 82 (¥8 spent at noon).
    _insert(db, "2026-01-15T02:00:00+00:00", 88.0)
    _insert(db, "2026-01-15T10:00:00+00:00", 82.0)

    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)  # noon local
    d = daily_heartbeat(db, now)

    assert d["current_balance"] == 82.0
    assert d["prev_balance"] == 90.0
    assert d["spent_today"] == 8.0
    assert d["normal_spend"] == 10.0
    # At noon, half the day elapsed -> projected spend 16, end-of-day 74.
    assert round(d["projected_spend"], 6) == 16.0
    assert d["projected_end_balance"] == 74.0
    assert round(d["spend_vs_normal"], 6) == 1.6
    # Nothing in the last 60 minutes -> no rapid drops.
    assert d["rapid_count"] == 0
    db.close()


def test_daily_heartbeat_spend_summary_needs_data(tmp_path):
    db = BalanceDB(str(tmp_path / "s.db"))
    # Only a couple of spent intervals in the 24h window -> not enough data yet.
    _insert(db, "2026-01-15T10:00:00+00:00", 80.0)
    _insert(db, "2026-01-15T10:06:00+00:00", 79.9)
    _insert(db, "2026-01-15T10:12:00+00:00", 78.0)

    now = datetime(2026, 1, 15, 11, 0, tzinfo=UTC)
    s = daily_heartbeat(db, now, spend_slice_minutes=5)["spend_summary"]

    assert s["intervals_with_spend"] == 2
    assert s["enough_data"] is False
    assert s["median_interval_spend"] == pytest.approx(1.0)
    assert s["spike_threshold"] is None
    assert s["unusually_high_count"] == 0
    db.close()


def test_daily_heartbeat_spend_summary_warmup_from_prior_days(tmp_path):
    """Early in the day, prior days' intervals warm the baseline so a fresh
    spike is still flagged even when today alone has too few spent intervals."""
    db = BalanceDB(str(tmp_path / "w.db"))
    # Prior day: a steady stream of ¥1 intervals (12 spent intervals, median 1).
    t = datetime(2026, 1, 14, 1, 0, tzinfo=UTC)
    bal = 100.0
    _insert(db, t.isoformat(), bal)
    for _ in range(12):
        t += timedelta(minutes=5)
        bal -= 1.0
        _insert(db, t.isoformat(), bal)
    # This morning: just two spent intervals, one a clear spike.
    now = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
    _insert(db, "2026-01-15T06:00:00+00:00", 50.0)
    _insert(db, "2026-01-15T06:05:00+00:00", 49.0)   # normal ¥1
    _insert(db, "2026-01-15T06:10:00+00:00", 41.0)   # ¥8 spike

    s = daily_heartbeat(db, now, spend_slice_minutes=5)["spend_summary"]

    # Today is still sparse, but the prior-day baseline lets us classify.
    assert s["intervals_with_spend"] == 2
    assert s["enough_data"] is True
    assert s["baseline_source"] == "history"
    assert s["median_interval_spend"] == pytest.approx(1.0)
    assert s["spike_threshold"] == pytest.approx(2.0)
    assert s["unusually_high_count"] == 1
    assert s["normal_count"] == 1
    db.close()


def test_daily_heartbeat_history_baseline_preferred_over_today(tmp_path):
    """Even once today has enough intervals, the larger history baseline wins."""
    db = BalanceDB(str(tmp_path / "hp.db"))
    # Prior day: steady ¥1 intervals.
    t = datetime(2026, 1, 14, 1, 0, tzinfo=UTC)
    bal = 100.0
    _insert(db, t.isoformat(), bal)
    for _ in range(12):
        t += timedelta(minutes=5)
        bal -= 1.0
        _insert(db, t.isoformat(), bal)
    # Today: a full day of ¥0.1 intervals plus a ¥0.5 "spike" relative to today.
    # Today's own median would be ~0.1, but history (median 1.0) must win, so
    # nothing today is high.
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    t2 = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
    bal2 = 100.0
    _insert(db, t2.isoformat(), bal2)
    for _ in range(12):
        t2 += timedelta(minutes=5)
        bal2 -= 0.1
        _insert(db, t2.isoformat(), bal2)
    t2 += timedelta(minutes=5)
    bal2 -= 0.5
    _insert(db, t2.isoformat(), bal2)

    s = daily_heartbeat(db, now, spend_slice_minutes=5)["spend_summary"]

    assert s["intervals_with_spend"] == 13
    assert s["enough_data"] is True
    assert s["baseline_source"] == "history"
    assert s["median_interval_spend"] == pytest.approx(1.0)
    assert s["unusually_high_count"] == 0
    db.close()


def test_daily_heartbeat_spend_summary_flags_spike(tmp_path):
    db = BalanceDB(str(tmp_path / "sp.db"))
    # 6 ¥1 intervals (normal), 3 ¥0.2 intervals (below) and one ¥7 spike.
    drops = [1.0] * 6 + [0.2] * 3 + [7.0]
    t0 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
    bal = 100.0
    _insert(db, t0.isoformat(), bal)
    for i, d in enumerate(drops, start=1):
        bal -= d
        _insert(db, (t0 + timedelta(minutes=5 * i)).isoformat(), bal)

    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    s = daily_heartbeat(db, now, spend_slice_minutes=5)["spend_summary"]

    assert s["intervals_with_spend"] == 10
    assert s["enough_data"] is True
    assert s["median_interval_spend"] == pytest.approx(1.0)
    assert s["spike_threshold"] == pytest.approx(2.0)
    assert s["unusually_high_count"] == 1
    assert s["normal_count"] == 6
    assert s["below_count"] == 3
    assert s["unusually_high_pct"] == pytest.approx(10.0)
    assert s["normal_pct"] == pytest.approx(60.0)
    assert s["below_pct"] == pytest.approx(30.0)
    db.close()


def test_daily_heartbeat_detects_rapid_drop(tmp_path):
    db = BalanceDB(str(tmp_path / "r.db"))
    # Small, normal minute-level drops...
    _insert(db, "2026-01-15T10:00:00+00:00", 100.0)
    _insert(db, "2026-01-15T10:10:00+00:00", 99.8)
    _insert(db, "2026-01-15T10:20:00+00:00", 99.6)
    _insert(db, "2026-01-15T10:30:00+00:00", 99.5)
    # ...followed by a single large drop.
    _insert(db, "2026-01-15T10:40:00+00:00", 70.0)

    now = datetime(2026, 1, 15, 11, 0, tzinfo=UTC)
    d = daily_heartbeat(db, now)

    assert d["rapid_count"] == 1
    largest = d["largest_rapid"]
    assert largest["drop"] == pytest.approx(29.5)
    assert largest["pct"] == pytest.approx(29.5 / 99.5)
    assert d["current_balance"] == 70.0
    db.close()


def test_spend_intervals_endpoint(client):
    db = client.app.state.db
    t0 = datetime.now(UTC) - timedelta(hours=1)
    bal = 100.0
    db.insert_snapshot(ts=t0.isoformat(), currency="USD", total_balance=bal,
                       granted_balance=0.0, topped_up_balance=0.0,
                       is_available=True, http_status=200, raw="{}")
    for d in [1.0] * 6 + [0.2] * 3 + [7.0]:
        bal -= d
        t0 += timedelta(minutes=5)
        db.insert_snapshot(ts=t0.isoformat(), currency="USD", total_balance=bal,
                           granted_balance=0.0, topped_up_balance=0.0,
                           is_available=True, http_status=200, raw="{}")

    r = client.get("/spend/intervals?hours=24&slice_minutes=5")
    assert r.status_code == 200
    body = r.json()
    assert body["enough_data"] is True
    assert body["summary"]["intervals_with_spend"] == 10
    assert body["summary"]["unusually_high_count"] == 1
    assert len(body["intervals"]) == 10
    assert any(i["bucket"] == "high" for i in body["intervals"])

    r2 = client.get("/spend/intervals?hours=24&slice_minutes=5&bucket=high")
    assert r2.status_code == 200
    high = r2.json()["intervals"]
    assert len(high) == 1
    assert all(i["bucket"] == "high" for i in high)


def _seed_dense_day(db, day: datetime, *, start_bal: float, drops: list[float]):
    """Seed a day with 5-min-apart snapshots so declines register as spent slices."""
    t = day
    bal = start_bal
    _insert(db, t.isoformat(), bal)
    for d in drops:
        t += timedelta(minutes=5)
        bal -= d
        _insert(db, t.isoformat(), bal)


def test_daily_history_series_usage_and_cost_per_minute(tmp_path):
    from deepseek_balance.analytics import daily_history_series

    db = BalanceDB(str(tmp_path / "h.db"))
    # Two prior complete days; each day 3 spent 5-min slices of ¥2 → spend 6,
    # usage 15m → cost/min 0.4. Balance reset each day for a clean drop series.
    _seed_dense_day(db, datetime(2026, 1, 13, 1, 0, tzinfo=UTC), start_bal=100.0, drops=[2.0] * 3)
    _seed_dense_day(db, datetime(2026, 1, 14, 1, 0, tzinfo=UTC), start_bal=94.0, drops=[2.0] * 3)

    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)  # today excluded
    series = daily_history_series(db, now, days=2, spend_slice_minutes=5)

    assert series["end_date"] == "2026-01-14"
    assert len(series["days"]) == 2
    d13, d14 = series["days"]
    for day in (d13, d14):
        assert day["spend"] == pytest.approx(6.0)
        assert day["usage_minutes"] == 15
        assert day["intervals_with_spend"] == 3
        assert day["cost_per_minute"] == pytest.approx(0.4)
    db.close()


def test_daily_history_series_all_time_from_earliest(tmp_path):
    from deepseek_balance.analytics import daily_history_series

    db = BalanceDB(str(tmp_path / "a.db"))
    _seed_dense_day(db, datetime(2026, 1, 10, 1, 0, tzinfo=UTC), start_bal=100.0, drops=[1.0])
    _seed_dense_day(db, datetime(2026, 1, 14, 1, 0, tzinfo=UTC), start_bal=99.0, drops=[2.0, 2.0])
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    series = daily_history_series(db, now, days=None, spend_slice_minutes=5)
    # Covers everything from the earliest snapshot's day up to yesterday.
    assert series["start_date"] == "2026-01-10"
    assert series["end_date"] == "2026-01-14"
    assert len(series["days"]) == 5
    assert series["days"][0]["spend"] == pytest.approx(1.0)
    assert series["days"][-1]["spend"] == pytest.approx(4.0)
    db.close()


def test_daily_heartbeat_usage_minutes(tmp_path):
    db = BalanceDB(str(tmp_path / "u.db"))
    _seed_dense_day(db, datetime(2026, 1, 15, 0, 30, tzinfo=UTC), start_bal=100.0, drops=[1.0] * 3)
    now = datetime(2026, 1, 15, 1, 0, tzinfo=UTC)
    s = daily_heartbeat(db, now, spend_slice_minutes=5)["spend_summary"]
    assert s["intervals_with_spend"] == 3
    assert s["usage_minutes"] == 15  # 3 slices × 5 min
    db.close()


def test_days_endpoint(client):
    db = client.app.state.db
    # Seed two prior complete days, dense enough to register slices.
    today_local = datetime.now(UTC)
    _seed_dense_day(db, today_local - timedelta(days=2), start_bal=100.0, drops=[2.0] * 3)
    _seed_dense_day(db, today_local - timedelta(days=1), start_bal=94.0, drops=[2.0] * 3)
    r = client.get("/balance/days?days=3")
    assert r.status_code == 200
    body = r.json()
    assert "currency" in body
    # Range is clipped to where data actually exists (no leading blank bars):
    # two seeded days means two complete days regardless of the requested 3.
    assert len(body["days"]) == 2
    assert all("usage_minutes" in d and "cost_per_minute" in d for d in body["days"])
    assert body["days"][-1]["usage_minutes"] == 15
    # Today's date must never appear in the series (partial day excluded).
    assert body["days"][-1]["date"] != datetime.now(UTC).date().isoformat()
    # The series starts on the first day that has data, not 3 days back.
    assert body["days"][0]["usage_minutes"] == 15


def test_daily_endpoint(client):
    db = client.app.state.db
    now = datetime.now(UTC)
    for i, bal in enumerate([100.0, 90.0]):
        db.insert_snapshot(
            ts=(now - timedelta(hours=1 - i)).isoformat(),
            currency="CNY",
            total_balance=bal,
            granted_balance=1.0,
            topped_up_balance=0.0,
            is_available=True,
            http_status=200,
            raw="{}",
        )
    r = client.get("/balance/daily")
    assert r.status_code == 200
    body = r.json()
    assert body["current_balance"] == 90.0
    assert "spent_today" in body
    assert "projected_end_balance" in body
    assert "rapid_count" in body
    assert "today_points" in body
    assert "spend_summary" in body


# --- MCP server timezone awareness ----------------------------------------

def _mcp_server_with_db(tmp_path) -> BalanceDB:
    """Point the MCP server at a throwaway DB and seed a couple of snapshots."""
    db = BalanceDB(str(tmp_path / "mcp.db"))
    _insert(db, "2026-01-15T02:00:00+00:00", 88.0)
    _insert(db, "2026-01-15T10:00:00+00:00", 82.0)
    mcp_server._db = db
    return db


@pytest.fixture
def new_york_tz(monkeypatch):
    """Force the container's local zone to America/New_York for the test."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()


def test_mcp_today_summary_is_local(new_york_tz, tmp_path):
    db = _mcp_server_with_db(tmp_path)
    tool = mcp_server.server._tool_manager._tools["today_summary"]
    hb = tool.fn(normal_days=14)

    # The timezone must be a real local zone (EDT/EST), never UTC.
    assert hb["timezone"] in ("EDT", "EST")
    # local_now must carry a non-UTC offset (America/New_York is UTC-4/-5).
    assert not hb["local_now"].endswith("+00:00")
    assert not hb["local_now"].endswith("Z")
    # The daily view exposes a start_of_day / fraction_elapsed pair.
    assert "start_of_day" in hb
    assert "fraction_elapsed" in hb
    # start_of_day and nested today_points timestamps must be local, not UTC.
    assert not hb["start_of_day"].endswith("+00:00")
    assert not hb["start_of_day"].endswith("Z")
    for p in hb.get("today_points", []):
        if "ts" in p:
            assert not p["ts"].endswith("+00:00")
    if hb.get("current_ts") is not None:
        assert not hb["current_ts"].endswith("+00:00")
    db.close()


def test_mcp_spend_tools_are_local(new_york_tz, tmp_path):
    db = _mcp_server_with_db(tmp_path)

    summary = mcp_server.server._tool_manager._tools["spend_summary"].fn()
    assert summary["timezone"] in ("EDT", "EST")
    assert "local_now" in summary
    assert not summary["local_now"].endswith("+00:00")

    intervals = mcp_server.server._tool_manager._tools["list_spend_intervals"].fn()
    assert intervals["timezone"] in ("EDT", "EST")
    assert intervals["local_now"]
    for i in intervals["intervals"]:
        assert "ts" in i
        assert not i["ts"].endswith("+00:00")
    db.close()


def test_mcp_balance_history_is_local(new_york_tz, tmp_path):
    db = BalanceDB(str(tmp_path / "mcp_history.db"))
    now = datetime.now().astimezone()
    _insert(db, (now - timedelta(hours=2)).isoformat(), 95.0)
    _insert(db, (now - timedelta(hours=1)).isoformat(), 90.0)
    mcp_server._db = db
    rows = mcp_server.server._tool_manager._tools["balance_history"].fn(hours=24)
    assert len(rows) >= 1
    for r in rows:
        assert not r["ts"].endswith("+00:00")
    db.close()
