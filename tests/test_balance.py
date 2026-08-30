"""Tests for deepseek-balance: poller, storage, endpoints and interval parsing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

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
