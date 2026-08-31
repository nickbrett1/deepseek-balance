"""Tests for deepseek-balance: poller, storage, endpoints and interval parsing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

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


def test_index_serves_chart(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "DeepSeek Balance" in r.text
    assert "<svg" in r.text


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


def test_daily_heartbeat_recent_spend_bars(tmp_path):
    db = BalanceDB(str(tmp_path / "s.db"))
    # A ¥20 "typical day" yesterday establishes the per-slice expectation.
    _insert(db, "2026-01-14T08:00:00+00:00", 100.0)
    _insert(db, "2026-01-14T20:00:00+00:00", 80.0)
    # Today: small drop then a bigger one, both inside the recent window.
    _insert(db, "2026-01-15T10:00:00+00:00", 80.0)
    _insert(db, "2026-01-15T10:06:00+00:00", 79.9)
    _insert(db, "2026-01-15T10:12:00+00:00", 78.0)

    now = datetime(2026, 1, 15, 11, 0, tzinfo=UTC)
    d = daily_heartbeat(db, now, rapid_window_minutes=60, spend_slice_minutes=5)

    bars = d["recent_spend"]
    assert len(bars) == 12
    # 10:06 drop lands in slice 1 (10:05), 10:12 drop in slice 2 (10:10).
    assert bars[1]["spend"] == pytest.approx(0.1)
    assert bars[2]["spend"] == pytest.approx(1.9)
    # Empty slices (spend 0) are "under" expectation; the bigger one is "above".
    assert bars[0]["status"] == "under"
    assert bars[2]["status"] == "above"
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
