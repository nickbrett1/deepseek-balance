"""The view-models the two screens read (`/ui/*`).

`/balance/*` and `/analysis/*` are the machine endpoints: the MCP server and the
poller read them, so a view-model must never become the API. These tests pin the
other direction -- that the screens' endpoints carry a *verdict* (is today normal,
did Phoenix explain this interval) and no more data than the screen draws.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from deepseek_balance.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "views.db"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("POLL_INTERVAL", "1h")
    with TestClient(app) as c:
        yield c


def _insert(db, when: datetime, balance: float) -> None:
    db.insert_snapshot(
        ts=when.isoformat(),
        currency="CNY",
        total_balance=balance,
        granted_balance=1.0,
        topped_up_balance=0.0,
        is_available=True,
        http_status=200,
        raw="{}",
    )


def _seed_three_days(client) -> None:
    """Four days of snapshots, so every day has spend and there is a baseline."""
    db = app.state.db
    now = datetime.now(UTC)
    # Day -2 .. today, 12 CNY a day in 5-minute drops, so every day has spend.
    for day in range(3, -1, -1):
        base = now - timedelta(days=day)
        for step in range(24):
            _insert(db, base - timedelta(hours=step), 200.0 - day * 12 - step * 0.5)


def test_widget_says_unavailable_before_the_first_snapshot(client):
    body = client.get("/ui/widget.json").json()
    assert body == {"available": False, "reason": "no successful snapshot yet"}


def test_widget_carries_a_pacing_verdict(client):
    _seed_three_days(client)
    body = client.get("/ui/widget.json").json()
    assert body["available"] is True
    assert body["currency"] == "CNY"
    # A verdict, not a number the browser has to judge: the 0.7/1.3 bands are a
    # domain rule.
    assert body["pacing"]["label"] in {"below avg", "on track", "above normal", "no baseline"}
    assert set(body["today"]) == {
        "spent_today",
        "spent_yesterday",
        "normal_spend",
        "projected_spend",
        "current_balance",
        "prev_balance",
        "current_ts",
    }


def test_history_carries_today_and_the_complete_day_series(client):
    _seed_three_days(client)
    body = client.get("/ui/history.json?days=14").json()
    assert body["available"] is True
    assert body["slice_minutes"] == 5
    # Today is *not* in the series: it is partial, and the page draws it as a
    # projection bar rather than a measurement.
    assert body["series"], "the series is empty"
    assert all(day["date"] < body["now"][:10] for day in body["series"])
    assert body["available_days"] >= 3
    assert body["today"]["start_of_day"]


def test_history_range_is_a_request_not_a_filter(client):
    _seed_three_days(client)
    two = client.get("/ui/history.json?days=2").json()
    fourteen = client.get("/ui/history.json?days=14").json()
    assert len(two["series"]) <= len(fourteen["series"])
    assert fourteen["days"] == 14


def test_analysis_is_honest_when_phoenix_is_not_configured(client):
    body = client.get("/ui/analysis.json").json()
    assert body["configured"] is False
    assert body["rows"] == []
    assert body["has_more"] is False


def test_analysis_rejects_an_unknown_status(client):
    assert client.get("/ui/analysis.json?status=bogus").status_code == 422


def test_the_screens_are_served_from_the_build_and_the_api_is_not(client, tmp_path, monkeypatch):
    """The split this port rests on: markup from `web/dist`, data from Python."""
    from fastapi.testclient import TestClient as _TestClient  # noqa: F401

    from deepseek_balance import app as app_module

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>built</title>")
    monkeypatch.setattr(app_module, "WEB_DIST_DIR", dist)

    assert "built" in client.get("/").text
    assert "built" in client.get("/history").text
    # The machine endpoints still answer, unchanged, from the same app.
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/balance/daily").status_code == 200
    assert json.loads(client.get("/ui/history.json").text)["days"] == 14
