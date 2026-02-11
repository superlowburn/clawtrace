"""Tests for hosted multi-tenant API endpoints including auth and tier enforcement."""

import json
import pytest
from engine.server import create_app
from engine import db


@pytest.fixture
def hosted_app(tmp_path):
    """Create test Flask app with temp DB for hosted endpoints."""
    db_path = str(tmp_path / "test.db")
    config = {
        "data_paths": [],
        "cache_ttl_seconds": 9999,
        "server_port": 19898,
        "db_path": db_path,
        "hosted": True,
    }
    app = create_app(config)
    app.config["TESTING"] = True
    return app, db_path


@pytest.fixture
def hosted_client(hosted_app):
    """Create test Flask client."""
    app, _ = hosted_app
    with app.test_client() as client:
        yield client


@pytest.fixture
def registered_device(hosted_client):
    """Register a device and return (device_id, device_secret, auth_headers)."""
    resp = hosted_client.post("/api/register")
    assert resp.status_code == 200
    data = resp.get_json()
    device_id = data["device_id"]
    device_secret = data["device_secret"]
    headers = {"Authorization": f"Bearer {device_secret}"}
    return device_id, device_secret, headers


SAMPLE_EVENTS = [
    {
        "session_id": "sess-001",
        "event_type": "llm.usage",
        "model": "claude-opus-4-6",
        "project": "threadjack",
        "provider": "anthropic",
        "input_tokens": 1200,
        "output_tokens": 450,
        "cache_read_tokens": 500,
        "cache_write_tokens": 100,
        "cost_usd": 0.0567,
        "success": True,
        "timestamp": "2026-02-09T14:00:00Z",
    },
    {
        "session_id": "sess-001",
        "event_type": "llm.usage",
        "model": "claude-sonnet-4-5-20250929",
        "project": "threadjack",
        "provider": "anthropic",
        "input_tokens": 800,
        "output_tokens": 200,
        "cost_usd": 0.0054,
        "success": True,
        "timestamp": "2026-02-09T14:05:00Z",
    },
    {
        "session_id": "sess-002",
        "event_type": "llm.usage",
        "model": "claude-opus-4-6",
        "project": "clawtrace",
        "provider": "anthropic",
        "input_tokens": 2000,
        "output_tokens": 800,
        "cost_usd": 0.09,
        "success": True,
        "timestamp": "2026-02-09T15:00:00Z",
    },
]


# --- Registration ---

class TestRegistration:
    def test_register_returns_credentials(self, hosted_client):
        """POST /api/register returns device_id, device_secret, dashboard_url."""
        resp = hosted_client.post("/api/register")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "device_id" in data
        assert "device_secret" in data
        assert "dashboard_url" in data
        assert len(data["device_id"]) == 32
        assert len(data["device_secret"]) == 64
        assert data["device_id"] in data["dashboard_url"]

    def test_register_creates_unique_devices(self, hosted_client):
        """Each registration creates a unique device."""
        resp1 = hosted_client.post("/api/register")
        resp2 = hosted_client.post("/api/register")
        assert resp1.get_json()["device_id"] != resp2.get_json()["device_id"]

    def test_claim_existing_device(self, hosted_app):
        """POST /api/claim sets secret on existing device without one."""
        app, db_path = hosted_app
        # Manually create device without secret (simulates pre-auth device)
        device_id = "a3fe065a4e5e425daf5991286c6976ac"
        db.ensure_device(device_id, db_path)

        with app.test_client() as client:
            resp = client.post("/api/claim", json={"device_id": device_id})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["device_id"] == device_id
            assert "device_secret" in data

    def test_claim_already_claimed(self, hosted_app):
        """Second claim on same device fails."""
        app, db_path = hosted_app
        device_id = "a3fe065a4e5e425daf5991286c6976ac"
        db.ensure_device(device_id, db_path)

        with app.test_client() as client:
            resp1 = client.post("/api/claim", json={"device_id": device_id})
            assert resp1.status_code == 200
            resp2 = client.post("/api/claim", json={"device_id": device_id})
            assert resp2.status_code == 404

    def test_claim_nonexistent_device(self, hosted_client):
        resp = hosted_client.post("/api/claim", json={"device_id": "deadbeef12345678deadbeef12345678"})
        assert resp.status_code == 404


# --- Auth ---

class TestAuth:
    def test_ingest_requires_auth(self, hosted_client):
        """Ingest without auth returns 401."""
        resp = hosted_client.post("/api/ingest", json={
            "device_id": "a3fe065a4e5e425daf5991286c6976ac",
            "events": SAMPLE_EVENTS,
        })
        assert resp.status_code == 401

    def test_ingest_wrong_secret(self, hosted_client, registered_device):
        """Ingest with wrong secret returns 403."""
        device_id, _, _ = registered_device
        resp = hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": SAMPLE_EVENTS},
            headers={"Authorization": "Bearer wrongsecret"})
        assert resp.status_code == 403

    def test_ingest_with_valid_auth(self, hosted_client, registered_device):
        """Ingest with valid auth succeeds."""
        device_id, _, headers = registered_device
        resp = hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": SAMPLE_EVENTS[:1]},
            headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_stats_requires_auth(self, hosted_client, registered_device):
        """Stats without auth returns 401."""
        device_id, _, _ = registered_device
        resp = hosted_client.get(f"/api/stats/{device_id}")
        assert resp.status_code == 401

    def test_stats_with_valid_auth(self, hosted_client, registered_device):
        """Stats with valid auth succeeds."""
        device_id, _, headers = registered_device
        resp = hosted_client.get(f"/api/stats/{device_id}", headers=headers)
        assert resp.status_code == 200

    def test_resync_requires_auth(self, hosted_client, registered_device):
        device_id, _, _ = registered_device
        resp = hosted_client.post(f"/api/resync/{device_id}")
        assert resp.status_code == 401

    def test_optimize_requires_auth(self, hosted_client, registered_device):
        device_id, _, _ = registered_device
        resp = hosted_client.get(f"/api/optimize/{device_id}")
        assert resp.status_code == 401

    def test_alerts_requires_auth(self, hosted_client, registered_device):
        device_id, _, _ = registered_device
        resp = hosted_client.get(f"/api/alerts/{device_id}")
        assert resp.status_code == 401

    def test_pricing_requires_auth(self, hosted_client, registered_device):
        device_id, _, _ = registered_device
        resp = hosted_client.get(f"/api/pricing/{device_id}/config")
        assert resp.status_code == 401


# --- No-auth endpoints ---

class TestNoAuthEndpoints:
    def test_health_no_auth(self, hosted_client):
        resp = hosted_client.get("/api/health")
        assert resp.status_code == 200

    def test_community_no_auth(self, hosted_client):
        resp = hosted_client.get("/api/community")
        assert resp.status_code == 200

    def test_landing_no_auth(self, hosted_client):
        resp = hosted_client.get("/")
        assert resp.status_code == 200

    def test_dashboard_page_no_auth(self, hosted_client, registered_device):
        """Dashboard HTML page doesn't need auth (it's just static HTML)."""
        device_id, _, _ = registered_device
        resp = hosted_client.get(f"/d/{device_id}")
        assert resp.status_code == 200

    def test_register_no_auth(self, hosted_client):
        resp = hosted_client.post("/api/register")
        assert resp.status_code == 200


# --- Ingest (with auth) ---

class TestIngestEndpoint:
    def test_ingest_success(self, hosted_client, registered_device):
        device_id, _, headers = registered_device
        resp = hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": SAMPLE_EVENTS},
            headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["ingested"] == 3

    def test_ingest_missing_body(self, hosted_client):
        resp = hosted_client.post("/api/ingest", content_type="application/json")
        assert resp.status_code == 400

    def test_ingest_missing_device_id(self, hosted_client):
        resp = hosted_client.post("/api/ingest", json={"events": SAMPLE_EVENTS})
        assert resp.status_code == 400

    def test_ingest_invalid_device_id(self, hosted_client):
        resp = hosted_client.post("/api/ingest", json={
            "device_id": "not-a-hex-id",
            "events": SAMPLE_EVENTS,
        })
        assert resp.status_code == 400

    def test_ingest_empty_events(self, hosted_client, registered_device):
        device_id, _, headers = registered_device
        resp = hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": []},
            headers=headers)
        assert resp.status_code == 400

    def test_ingest_too_many_events(self, hosted_client, registered_device):
        device_id, _, headers = registered_device
        resp = hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": [{"event_type": "llm.usage", "timestamp": "2026-02-09T00:00:00Z"}] * 501},
            headers=headers)
        assert resp.status_code == 400
        assert "500" in resp.get_json()["error"]


# --- Stats (with auth) ---

class TestStatsEndpoint:
    def test_stats_empty_device(self, hosted_client, registered_device):
        device_id, _, headers = registered_device
        resp = hosted_client.get(f"/api/stats/{device_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total_requests"] == 0
        assert data["total_cost_usd"] == 0

    def test_stats_after_ingest(self, hosted_client, registered_device):
        device_id, _, headers = registered_device
        # Early access: ingest all events (no project limit)
        hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": SAMPLE_EVENTS},
            headers=headers)
        resp = hosted_client.get(f"/api/stats/{device_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total_requests"] == 3
        assert data["total_cost_usd"] > 0

    def test_stats_invalid_device_id(self, hosted_client):
        resp = hosted_client.get("/api/stats/INVALID!")
        assert resp.status_code == 400

    def test_stats_days_param(self, hosted_client, registered_device):
        device_id, _, headers = registered_device
        # Free tier clamps to 7 days max
        resp = hosted_client.get(f"/api/stats/{device_id}?days=30", headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["days"] == 7  # Clamped by free tier


# --- Community ---

class TestCommunityEndpoint:
    def test_community_empty(self, hosted_client):
        resp = hosted_client.get("/api/community")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["active_devices"] == 0

    def test_community_after_ingest(self, hosted_client, registered_device):
        device_id, _, headers = registered_device
        hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": SAMPLE_EVENTS[:1]},
            headers=headers)
        resp = hosted_client.get("/api/community")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["active_devices"] == 1


# --- Pages ---

class TestLandingPage:
    def test_landing_returns_html(self, hosted_client):
        resp = hosted_client.get("/")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/html")


class TestDeviceDashboard:
    def test_dashboard_returns_html(self, hosted_client, registered_device):
        device_id, _, _ = registered_device
        resp = hosted_client.get(f"/d/{device_id}")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/html")

    def test_dashboard_invalid_id(self, hosted_client):
        resp = hosted_client.get("/d/INVALID!")
        assert resp.status_code == 400


# --- Tier Enforcement ---

class TestTierEnforcement:
    def test_free_tier_allows_all_projects(self, hosted_client, registered_device):
        """Early access: free tier allows all projects."""
        device_id, _, headers = registered_device
        # First ingest: establishes "threadjack" as the first project
        hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": SAMPLE_EVENTS[:1]},
            headers=headers)
        # Second ingest: mixed projects — all events should be ingested
        resp = hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": SAMPLE_EVENTS},
            headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        # All 3 events ingested (early access: no project limit)
        assert data["ingested"] == 3

    def test_free_tier_accepts_new_project(self, hosted_client, registered_device):
        """Early access: free tier accepts events from any project."""
        device_id, _, headers = registered_device
        # Establish first project
        hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": SAMPLE_EVENTS[:1]},
            headers=headers)
        # Send events for a different project — should succeed
        new_project_events = [{"event_type": "llm.usage", "timestamp": "2026-02-09T16:00:00Z",
                               "project": "new-project", "model": "claude-opus-4-6",
                               "input_tokens": 100, "output_tokens": 50}]
        resp = hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": new_project_events},
            headers=headers)
        assert resp.status_code == 200

    def test_free_tier_clamps_history(self, hosted_client, registered_device):
        """Free tier clamps stats to 7 days max."""
        device_id, _, headers = registered_device
        resp = hosted_client.get(f"/api/stats/{device_id}?days=30", headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()["days"] == 7

    def test_free_tier_gets_alerts(self, hosted_client, registered_device):
        """Early access: free tier gets real alerts response."""
        device_id, _, headers = registered_device
        resp = hosted_client.get(f"/api/alerts/{device_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert "alerts" in data
        assert "tier_limited" not in data

    def test_pro_tier_full_access(self, hosted_app, hosted_client):
        """Pro tier gets full access."""
        app, db_path = hosted_app
        # Register and upgrade to pro
        resp = hosted_client.post("/api/register")
        data = resp.get_json()
        device_id = data["device_id"]
        device_secret = data["device_secret"]
        headers = {"Authorization": f"Bearer {device_secret}"}

        # Manually upgrade tier
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE devices SET tier = 'pro' WHERE device_id = ?", (device_id,))
        conn.commit()
        conn.close()

        # Pro can ingest multi-project
        resp = hosted_client.post("/api/ingest",
            json={"device_id": device_id, "events": SAMPLE_EVENTS},
            headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()["ingested"] == 3

        # Pro can get 30 days of stats
        resp = hosted_client.get(f"/api/stats/{device_id}?days=30", headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()["days"] == 30

        # Pro gets real alerts (even if empty)
        resp = hosted_client.get(f"/api/alerts/{device_id}", headers=headers)
        assert resp.status_code == 200
        assert "tier_limited" not in resp.get_json()
