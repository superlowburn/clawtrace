"""Tests for cost alert system — CRUD, checks, dedup, config, API endpoints."""

import json
import pytest
from datetime import datetime, timezone

from engine.server import create_app
from engine import db


DEVICE_ID = "a3fe065a4e5e425daf5991286c6976ac"


@pytest.fixture
def db_path(tmp_path):
    """Initialize a fresh DB and return path."""
    path = str(tmp_path / "test_alerts.db")
    db.init_db(path)
    db.ensure_device(DEVICE_ID, path)
    return path


@pytest.fixture
def hosted_setup(tmp_path):
    """Create test Flask app + client + registered pro device for alert endpoints."""
    db_path = str(tmp_path / "test.db")
    config = {
        "data_paths": [],
        "cache_ttl_seconds": 9999,
        "server_port": 19898,
        "db_path": db_path,
        "alerts": {
            "daily_budget_usd": 1.00,
            "session_spike_usd": 0.50,
            "hourly_burn_rate_usd": 0.80,
            "hourly_request_volume": 200,
            "hourly_token_volume": 2000000,
            "session_duration_minutes": 180,
            "dedup_window_minutes": 60,
        },
    }
    app = create_app(config)
    app.config["TESTING"] = True
    with app.test_client() as client:
        # Register a device and upgrade to pro (so alerts work)
        resp = client.post("/api/register")
        data = resp.get_json()
        device_id = data["device_id"]
        device_secret = data["device_secret"]
        headers = {"Authorization": f"Bearer {device_secret}"}
        # Upgrade to pro so alert endpoints return real data
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE devices SET tier = 'pro' WHERE device_id = ?", (device_id,))
        conn.commit()
        conn.close()
        yield client, device_id, headers


@pytest.fixture
def hosted_client(hosted_setup):
    client, _, _ = hosted_setup
    return client


def _ingest_events(db_path, device_id, events):
    """Helper to ingest events directly."""
    db.ensure_device(device_id, db_path)
    return db.ingest_events(device_id, events, db_path)


def _make_event(cost, session_id="sess-001", timestamp=None,
                input_tokens=100, output_tokens=50):
    """Helper to create a minimal event dict."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "session_id": session_id,
        "event_type": "llm.usage",
        "model": "claude-opus-4-6",
        "project": "test-project",
        "provider": "anthropic",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
        "success": True,
        "timestamp": timestamp,
    }


# --- Alert CRUD ---

class TestAlertCRUD:
    def test_create_alert(self, db_path):
        alert_id = db.create_alert(
            DEVICE_ID, "daily_budget", "warning",
            "Daily spend $12.00 exceeds $10.00 budget",
            details={"today_cost": 12.0, "threshold": 10.0},
            db_path=db_path,
        )
        assert alert_id is not None
        assert alert_id > 0

    def test_get_alerts(self, db_path):
        db.create_alert(DEVICE_ID, "daily_budget", "warning", "Alert 1", db_path=db_path)
        db.create_alert(DEVICE_ID, "session_spike", "critical", "Alert 2", db_path=db_path)

        alerts = db.get_alerts(DEVICE_ID, db_path=db_path)
        assert len(alerts) == 2
        messages = {a["message"] for a in alerts}
        assert messages == {"Alert 1", "Alert 2"}

    def test_get_alerts_filter_acknowledged(self, db_path):
        a1 = db.create_alert(DEVICE_ID, "daily_budget", "warning", "Alert 1", db_path=db_path)
        db.create_alert(DEVICE_ID, "session_spike", "critical", "Alert 2", db_path=db_path)
        db.acknowledge_alert(a1, DEVICE_ID, db_path=db_path)

        unacked = db.get_alerts(DEVICE_ID, acknowledged=False, db_path=db_path)
        assert len(unacked) == 1
        assert unacked[0]["message"] == "Alert 2"

        acked = db.get_alerts(DEVICE_ID, acknowledged=True, db_path=db_path)
        assert len(acked) == 1
        assert acked[0]["message"] == "Alert 1"

    def test_acknowledge_alert(self, db_path):
        alert_id = db.create_alert(DEVICE_ID, "daily_budget", "warning", "Test", db_path=db_path)
        assert db.acknowledge_alert(alert_id, DEVICE_ID, db_path=db_path) is True

        alerts = db.get_alerts(DEVICE_ID, acknowledged=False, db_path=db_path)
        assert len(alerts) == 0

    def test_acknowledge_wrong_device(self, db_path):
        other_device = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        db.ensure_device(other_device, db_path)
        alert_id = db.create_alert(DEVICE_ID, "daily_budget", "warning", "Test", db_path=db_path)
        assert db.acknowledge_alert(alert_id, other_device, db_path=db_path) is False

    def test_acknowledge_nonexistent(self, db_path):
        assert db.acknowledge_alert(9999, DEVICE_ID, db_path=db_path) is False

    def test_active_alert_count(self, db_path):
        assert db.get_active_alert_count(DEVICE_ID, db_path=db_path) == 0

        db.create_alert(DEVICE_ID, "daily_budget", "warning", "A1", db_path=db_path)
        db.create_alert(DEVICE_ID, "session_spike", "critical", "A2", db_path=db_path)
        assert db.get_active_alert_count(DEVICE_ID, db_path=db_path) == 2

        a3 = db.create_alert(DEVICE_ID, "hourly_burn_rate", "warning", "A3", db_path=db_path)
        db.acknowledge_alert(a3, DEVICE_ID, db_path=db_path)
        assert db.get_active_alert_count(DEVICE_ID, db_path=db_path) == 2

    def test_alert_details_json(self, db_path):
        details = {"today_cost": 12.5, "threshold": 10.0, "ratio": 1.25}
        db.create_alert(DEVICE_ID, "daily_budget", "warning", "Test",
                        details=details, db_path=db_path)
        alerts = db.get_alerts(DEVICE_ID, db_path=db_path)
        assert alerts[0]["details"] == details


# --- Dedup ---

class TestAlertDedup:
    def test_dedup_same_type(self, db_path):
        """Second alert of same type within window is skipped."""
        a1 = db.create_alert(DEVICE_ID, "daily_budget", "warning", "First", db_path=db_path)
        a2 = db.create_alert(DEVICE_ID, "daily_budget", "warning", "Second", db_path=db_path)
        assert a1 is not None
        assert a2 is None  # Deduped

    def test_dedup_different_types(self, db_path):
        """Different alert types are not deduped."""
        a1 = db.create_alert(DEVICE_ID, "daily_budget", "warning", "Budget", db_path=db_path)
        a2 = db.create_alert(DEVICE_ID, "session_spike", "warning", "Spike", db_path=db_path)
        assert a1 is not None
        assert a2 is not None

    def test_dedup_after_acknowledge(self, db_path):
        """Acknowledged alerts don't block new ones."""
        a1 = db.create_alert(DEVICE_ID, "daily_budget", "warning", "First", db_path=db_path)
        db.acknowledge_alert(a1, DEVICE_ID, db_path=db_path)
        a2 = db.create_alert(DEVICE_ID, "daily_budget", "warning", "Second", db_path=db_path)
        assert a2 is not None

    def test_session_spike_dedup_per_session(self, db_path):
        """Session spikes dedup per session_id."""
        a1 = db.create_alert(
            DEVICE_ID, "session_spike", "warning", "Session A spike",
            details={"session_id": "sess-aaa"}, db_path=db_path,
        )
        a2 = db.create_alert(
            DEVICE_ID, "session_spike", "warning", "Session B spike",
            details={"session_id": "sess-bbb"}, db_path=db_path,
        )
        assert a1 is not None
        assert a2 is not None  # Different session, not deduped

    def test_session_spike_dedup_same_session(self, db_path):
        """Same session spike within window is deduped."""
        a1 = db.create_alert(
            DEVICE_ID, "session_spike", "warning", "Spike 1",
            details={"session_id": "sess-aaa"}, db_path=db_path,
        )
        a2 = db.create_alert(
            DEVICE_ID, "session_spike", "warning", "Spike 2",
            details={"session_id": "sess-aaa"}, db_path=db_path,
        )
        assert a1 is not None
        assert a2 is None


# --- Alert Checks ---

class TestDailyBudgetCheck:
    def test_triggers_warning(self, db_path):
        """Daily budget triggers warning when exceeded but < 1.5x."""
        _ingest_events(db_path, DEVICE_ID, [_make_event(6.0), _make_event(5.5)])
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"daily_budget_usd": 10.0})
        budget_alerts = [a for a in alerts if a["alert_type"] == "daily_budget"]
        assert len(budget_alerts) == 1
        assert budget_alerts[0]["severity"] == "warning"

    def test_triggers_critical(self, db_path):
        """Daily budget triggers critical at >= 1.5x threshold."""
        _ingest_events(db_path, DEVICE_ID, [_make_event(8.0), _make_event(8.0)])
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"daily_budget_usd": 10.0})
        budget_alerts = [a for a in alerts if a["alert_type"] == "daily_budget"]
        assert len(budget_alerts) == 1
        assert budget_alerts[0]["severity"] == "critical"

    def test_no_trigger_under_budget(self, db_path):
        """No alert when under budget."""
        _ingest_events(db_path, DEVICE_ID, [_make_event(3.0)])
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"daily_budget_usd": 10.0})
        budget_alerts = [a for a in alerts if a["alert_type"] == "daily_budget"]
        assert len(budget_alerts) == 0


class TestSessionSpikeCheck:
    def test_triggers_warning(self, db_path):
        """Session spike triggers warning when session cost > threshold but < 2x."""
        _ingest_events(db_path, DEVICE_ID, [
            _make_event(3.5, session_id="expensive-sess"),
            _make_event(2.5, session_id="expensive-sess"),
        ])
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"session_spike_usd": 5.0})
        spike_alerts = [a for a in alerts if a["alert_type"] == "session_spike"]
        assert len(spike_alerts) == 1
        assert spike_alerts[0]["severity"] == "warning"

    def test_triggers_critical(self, db_path):
        """Session spike triggers critical at >= 2x threshold."""
        _ingest_events(db_path, DEVICE_ID, [
            _make_event(6.0, session_id="very-expensive"),
            _make_event(5.0, session_id="very-expensive"),
        ])
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"session_spike_usd": 5.0})
        spike_alerts = [a for a in alerts if a["alert_type"] == "session_spike"]
        assert len(spike_alerts) == 1
        assert spike_alerts[0]["severity"] == "critical"

    def test_no_trigger_under_threshold(self, db_path):
        """No session spike when all sessions under threshold."""
        _ingest_events(db_path, DEVICE_ID, [_make_event(2.0, session_id="normal")])
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"session_spike_usd": 5.0})
        spike_alerts = [a for a in alerts if a["alert_type"] == "session_spike"]
        assert len(spike_alerts) == 0


class TestHourlyBurnRateCheck:
    def test_triggers_warning(self, db_path):
        """Hourly burn rate triggers warning when exceeded but < 2x."""
        _ingest_events(db_path, DEVICE_ID, [
            _make_event(2.0), _make_event(1.5),
        ])
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"hourly_burn_rate_usd": 3.0})
        burn_alerts = [a for a in alerts if a["alert_type"] == "hourly_burn_rate"]
        assert len(burn_alerts) == 1
        assert burn_alerts[0]["severity"] == "warning"

    def test_triggers_critical(self, db_path):
        """Hourly burn rate triggers critical at >= 2x threshold."""
        _ingest_events(db_path, DEVICE_ID, [
            _make_event(4.0), _make_event(3.0),
        ])
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"hourly_burn_rate_usd": 3.0})
        burn_alerts = [a for a in alerts if a["alert_type"] == "hourly_burn_rate"]
        assert len(burn_alerts) == 1
        assert burn_alerts[0]["severity"] == "critical"

    def test_no_trigger_under_threshold(self, db_path):
        """No alert when hourly spend is under threshold."""
        _ingest_events(db_path, DEVICE_ID, [_make_event(1.0)])
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"hourly_burn_rate_usd": 3.0})
        burn_alerts = [a for a in alerts if a["alert_type"] == "hourly_burn_rate"]
        assert len(burn_alerts) == 0


# --- Alert Config ---

class TestAlertConfig:
    def test_get_defaults(self, db_path):
        """Returns default thresholds when no per-device config set."""
        cfg = db.get_alert_config(DEVICE_ID, db_path=db_path,
                                  defaults={"daily_budget_usd": 15.0})
        assert cfg["daily_budget"]["threshold"] == 15.0
        assert cfg["daily_budget"]["enabled"] is True

    def test_set_and_get(self, db_path):
        """Per-device config overrides defaults."""
        db.set_alert_config(DEVICE_ID, "daily_budget", 25.0, db_path=db_path)
        cfg = db.get_alert_config(DEVICE_ID, db_path=db_path,
                                  defaults={"daily_budget_usd": 10.0})
        assert cfg["daily_budget"]["threshold"] == 25.0

    def test_disable_type(self, db_path):
        """Can disable an alert type per device."""
        db.set_alert_config(DEVICE_ID, "session_spike", 5.0, enabled=False, db_path=db_path)
        cfg = db.get_alert_config(DEVICE_ID, db_path=db_path)
        assert cfg["session_spike"]["enabled"] is False

    def test_disabled_type_skips_check(self, db_path):
        """Disabled alert type is not checked."""
        db.set_alert_config(DEVICE_ID, "daily_budget", 1.0, enabled=False, db_path=db_path)
        _ingest_events(db_path, DEVICE_ID, [_make_event(5.0)])
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path)
        budget_alerts = [a for a in alerts if a["alert_type"] == "daily_budget"]
        assert len(budget_alerts) == 0

    def test_upsert_config(self, db_path):
        """Setting config twice updates rather than duplicates."""
        db.set_alert_config(DEVICE_ID, "daily_budget", 10.0, db_path=db_path)
        db.set_alert_config(DEVICE_ID, "daily_budget", 20.0, db_path=db_path)
        cfg = db.get_alert_config(DEVICE_ID, db_path=db_path)
        assert cfg["daily_budget"]["threshold"] == 20.0


# --- API Endpoints ---

class TestAlertsAPI:
    def test_list_alerts_empty(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.get(f"/api/alerts/{device_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["alerts"] == []
        assert data["count"] == 0

    def test_list_alerts_invalid_device(self, hosted_client):
        resp = hosted_client.get("/api/alerts/INVALID!")
        assert resp.status_code == 400

    def test_acknowledge_alert_via_api(self, hosted_setup):
        client, device_id, headers = hosted_setup
        # Ingest events that trigger an alert (config has daily_budget_usd=1.0)
        client.post("/api/ingest", json={
            "device_id": device_id,
            "events": [_make_event(2.0)],
        }, headers=headers)

        # Fetch alerts
        resp = client.get(f"/api/alerts/{device_id}?acknowledged=false", headers=headers)
        data = resp.get_json()
        assert len(data["alerts"]) > 0
        alert_id = data["alerts"][0]["id"]

        # Acknowledge
        resp = client.post(f"/api/alerts/{device_id}/acknowledge/{alert_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

        # Verify gone from unacknowledged list
        resp = client.get(f"/api/alerts/{device_id}?acknowledged=false", headers=headers)
        remaining = [a for a in resp.get_json()["alerts"] if a["id"] == alert_id]
        assert len(remaining) == 0

    def test_acknowledge_nonexistent(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.post(f"/api/alerts/{device_id}/acknowledge/9999", headers=headers)
        assert resp.status_code == 404

    def test_alert_config_get(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.get(f"/api/alerts/{device_id}/config", headers=headers)
        assert resp.status_code == 200
        cfg = resp.get_json()
        assert "daily_budget" in cfg
        assert cfg["daily_budget"]["threshold"] == 1.0  # from test config

    def test_alert_config_set(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.post(f"/api/alerts/{device_id}/config", json={
            "alert_type": "daily_budget",
            "threshold": 50.0,
        }, headers=headers)
        assert resp.status_code == 200

        resp = client.get(f"/api/alerts/{device_id}/config", headers=headers)
        cfg = resp.get_json()
        assert cfg["daily_budget"]["threshold"] == 50.0

    def test_alert_config_invalid_type(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.post(f"/api/alerts/{device_id}/config", json={
            "alert_type": "invalid_type",
            "threshold": 10.0,
        }, headers=headers)
        assert resp.status_code == 400

    def test_alert_config_negative_threshold(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.post(f"/api/alerts/{device_id}/config", json={
            "alert_type": "daily_budget",
            "threshold": -5.0,
        }, headers=headers)
        assert resp.status_code == 400

    def test_ingest_triggers_alerts(self, hosted_setup):
        """Ingest that exceeds thresholds returns new_alerts count."""
        client, device_id, headers = hosted_setup
        resp = client.post("/api/ingest", json={
            "device_id": device_id,
            "events": [_make_event(2.0)],  # Exceeds daily_budget_usd=1.0
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["new_alerts"] > 0

    def test_ingest_no_alerts_under_threshold(self, hosted_setup):
        """Ingest under thresholds returns 0 new_alerts."""
        client, device_id, headers = hosted_setup
        resp = client.post("/api/ingest", json={
            "device_id": device_id,
            "events": [_make_event(0.01)],
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["new_alerts"] == 0


# --- Activity-based Alert Checks ---

class TestHourlyRequestVolumeCheck:
    def test_triggers_warning(self, db_path):
        """Request volume triggers warning when > threshold but < 2x."""
        events = [_make_event(0, session_id=f"s-{i}") for i in range(25)]
        _ingest_events(db_path, DEVICE_ID, events)
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"hourly_request_volume": 20})
        vol_alerts = [a for a in alerts if a["alert_type"] == "hourly_request_volume"]
        assert len(vol_alerts) == 1
        assert vol_alerts[0]["severity"] == "warning"

    def test_triggers_critical(self, db_path):
        """Request volume triggers critical at >= 2x threshold."""
        events = [_make_event(0, session_id=f"s-{i}") for i in range(50)]
        _ingest_events(db_path, DEVICE_ID, events)
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"hourly_request_volume": 20})
        vol_alerts = [a for a in alerts if a["alert_type"] == "hourly_request_volume"]
        assert len(vol_alerts) == 1
        assert vol_alerts[0]["severity"] == "critical"

    def test_no_trigger_under_threshold(self, db_path):
        """No alert when request count is under threshold."""
        events = [_make_event(0) for _ in range(5)]
        _ingest_events(db_path, DEVICE_ID, events)
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"hourly_request_volume": 20})
        vol_alerts = [a for a in alerts if a["alert_type"] == "hourly_request_volume"]
        assert len(vol_alerts) == 0


class TestHourlyTokenVolumeCheck:
    def test_triggers_warning(self, db_path):
        """Token volume triggers warning when > threshold but < 2x."""
        events = [_make_event(0, input_tokens=60000, output_tokens=40000) for _ in range(5)]
        _ingest_events(db_path, DEVICE_ID, events)
        # 5 * (60000+40000) = 500,000 tokens total — threshold at 400,000
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"hourly_token_volume": 400000})
        tok_alerts = [a for a in alerts if a["alert_type"] == "hourly_token_volume"]
        assert len(tok_alerts) == 1
        assert tok_alerts[0]["severity"] == "warning"

    def test_triggers_critical(self, db_path):
        """Token volume triggers critical at >= 2x threshold."""
        events = [_make_event(0, input_tokens=100000, output_tokens=100000) for _ in range(5)]
        _ingest_events(db_path, DEVICE_ID, events)
        # 5 * 200,000 = 1,000,000 tokens — threshold at 400,000 → 2.5x
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"hourly_token_volume": 400000})
        tok_alerts = [a for a in alerts if a["alert_type"] == "hourly_token_volume"]
        assert len(tok_alerts) == 1
        assert tok_alerts[0]["severity"] == "critical"

    def test_no_trigger_under_threshold(self, db_path):
        """No alert when token count is under threshold."""
        events = [_make_event(0, input_tokens=100, output_tokens=50)]
        _ingest_events(db_path, DEVICE_ID, events)
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"hourly_token_volume": 400000})
        tok_alerts = [a for a in alerts if a["alert_type"] == "hourly_token_volume"]
        assert len(tok_alerts) == 0


class TestSessionDurationCheck:
    def test_triggers_on_long_session(self, db_path):
        """Session duration alert triggers when session spans > threshold minutes."""
        now = datetime.now(timezone.utc)
        # Create events spanning 200 minutes within the last hour window
        # The MIN timestamp is 200min ago, MAX is now
        from datetime import timedelta
        early = (now - timedelta(minutes=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
        late = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [
            _make_event(0, session_id="long-sess", timestamp=early),
            _make_event(0, session_id="long-sess", timestamp=late),
        ]
        _ingest_events(db_path, DEVICE_ID, events)
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"session_duration_minutes": 180})
        dur_alerts = [a for a in alerts if a["alert_type"] == "session_duration"]
        assert len(dur_alerts) == 1
        assert dur_alerts[0]["severity"] == "warning"

    def test_no_trigger_on_short_session(self, db_path):
        """No alert when session duration is under threshold."""
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        early = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        late = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [
            _make_event(0, session_id="short-sess", timestamp=early),
            _make_event(0, session_id="short-sess", timestamp=late),
        ]
        _ingest_events(db_path, DEVICE_ID, events)
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path,
                                 config_defaults={"session_duration_minutes": 180})
        dur_alerts = [a for a in alerts if a["alert_type"] == "session_duration"]
        assert len(dur_alerts) == 0


# --- Activity Alert Config ---

class TestActivityAlertConfig:
    def test_set_get_activity_types(self, db_path):
        """Can set and get config for activity-based alert types."""
        for alert_type, threshold in [("hourly_request_volume", 500),
                                       ("hourly_token_volume", 5000000),
                                       ("session_duration", 240)]:
            db.set_alert_config(DEVICE_ID, alert_type, threshold, db_path=db_path)

        cfg = db.get_alert_config(DEVICE_ID, db_path=db_path)
        assert cfg["hourly_request_volume"]["threshold"] == 500
        assert cfg["hourly_token_volume"]["threshold"] == 5000000
        assert cfg["session_duration"]["threshold"] == 240

    def test_disabled_skips_check(self, db_path):
        """Disabled activity alert type is not checked."""
        db.set_alert_config(DEVICE_ID, "hourly_request_volume", 5, enabled=False, db_path=db_path)
        events = [_make_event(0) for _ in range(20)]
        _ingest_events(db_path, DEVICE_ID, events)
        alerts = db.check_alerts(DEVICE_ID, db_path=db_path)
        vol_alerts = [a for a in alerts if a["alert_type"] == "hourly_request_volume"]
        assert len(vol_alerts) == 0

    def test_config_defaults_from_config_json(self, db_path):
        """Config defaults map correctly for activity alert types."""
        cfg = db.get_alert_config(DEVICE_ID, db_path=db_path, defaults={
            "hourly_request_volume": 300,
            "hourly_token_volume": 3000000,
            "session_duration_minutes": 120,
        })
        assert cfg["hourly_request_volume"]["threshold"] == 300
        assert cfg["hourly_token_volume"]["threshold"] == 3000000
        assert cfg["session_duration"]["threshold"] == 120


# --- Activity Alerts API ---

class TestActivityAlertsAPI:
    def test_ingest_triggers_activity_alerts(self, hosted_setup):
        """Ingest with high volume triggers activity alerts."""
        client, device_id, headers = hosted_setup
        events = [_make_event(0, session_id=f"s-{i}") for i in range(250)]
        resp = client.post("/api/ingest", json={
            "device_id": device_id,
            "events": events,
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["new_alerts"] > 0

        # Verify the activity alert exists
        resp = client.get(f"/api/alerts/{device_id}?acknowledged=false", headers=headers)
        alerts = resp.get_json()["alerts"]
        types = {a["alert_type"] for a in alerts}
        assert "hourly_request_volume" in types

    def test_config_endpoint_accepts_new_types(self, hosted_setup):
        """Config endpoint accepts activity-based alert types."""
        client, device_id, headers = hosted_setup
        for alert_type in ("hourly_request_volume", "hourly_token_volume", "session_duration"):
            resp = client.post(f"/api/alerts/{device_id}/config", json={
                "alert_type": alert_type,
                "threshold": 999,
            }, headers=headers)
            assert resp.status_code == 200

        resp = client.get(f"/api/alerts/{device_id}/config", headers=headers)
        cfg = resp.get_json()
        assert cfg["hourly_request_volume"]["threshold"] == 999
        assert cfg["hourly_token_volume"]["threshold"] == 999
        assert cfg["session_duration"]["threshold"] == 999
