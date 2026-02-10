"""Tests for per-device model pricing overrides."""

import json
import pytest
from datetime import datetime, timezone

from engine.pricing import compute_cost, MODEL_PRICING, DEFAULT_PRICING, FREE_PRICING
from engine.server import create_app
from engine import db


DEVICE_ID = "a3fe065a4e5e425daf5991286c6976ac"


@pytest.fixture
def db_path(tmp_path):
    """Initialize a fresh DB and return path."""
    path = str(tmp_path / "test_pricing.db")
    db.init_db(path)
    db.ensure_device(DEVICE_ID, path)
    return path


@pytest.fixture
def hosted_setup(tmp_path):
    """Create test Flask client with registered pro-tier device.

    Returns (client, device_id, auth_headers).
    """
    db_path_str = str(tmp_path / "test.db")
    config = {
        "data_paths": [],
        "cache_ttl_seconds": 9999,
        "server_port": 19898,
        "db_path": db_path_str,
        "pricing": {
            "provider_defaults": {
                "nvidia": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
            }
        },
        "alerts": {
            "daily_budget_usd": 100.0,
            "session_spike_usd": 100.0,
            "hourly_burn_rate_usd": 100.0,
            "hourly_request_volume": 10000,
            "hourly_token_volume": 100000000,
            "session_duration_minutes": 9999,
            "dedup_window_minutes": 60,
        },
    }
    app = create_app(config)
    app.config["TESTING"] = True
    with app.test_client() as client:
        # Register device and upgrade to pro for full access
        resp = client.post("/api/register")
        data = resp.get_json()
        device_id = data["device_id"]
        device_secret = data["device_secret"]
        headers = {"Authorization": f"Bearer {device_secret}"}

        import sqlite3
        conn = sqlite3.connect(db_path_str)
        conn.execute("UPDATE devices SET tier = 'pro' WHERE device_id = ?", (device_id,))
        conn.commit()
        conn.close()

        yield client, device_id, headers


def _make_event(model="claude-opus-4-6", provider="anthropic", cost=0.05,
                input_tokens=1000, output_tokens=500,
                cache_read_tokens=0, cache_write_tokens=0,
                session_id="sess-001", timestamp=None):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "session_id": session_id,
        "event_type": "llm.usage",
        "model": model,
        "project": "test-project",
        "provider": provider,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cost_usd": cost,
        "success": True,
        "timestamp": timestamp,
    }


# --- compute_cost() unit tests ---

class TestComputeCost:
    def test_known_model(self):
        """Known models use their defined pricing."""
        total, breakdown = compute_cost(
            "claude-opus-4-6", 1_000_000, 1_000_000, 0, 0)
        assert total == pytest.approx(15.0 + 75.0)
        assert breakdown["input"] == pytest.approx(15.0)
        assert breakdown["output"] == pytest.approx(75.0)

    def test_unknown_model_uses_default(self):
        """Unknown models fall back to Sonnet (DEFAULT_PRICING)."""
        total, _ = compute_cost(
            "gpt-4o", 1_000_000, 1_000_000, 0, 0)
        expected = DEFAULT_PRICING["input"] + DEFAULT_PRICING["output"]
        assert total == pytest.approx(expected)

    def test_override_exact_match(self):
        """Exact model override takes precedence."""
        overrides = {
            "my-model": {"input": 1.0, "output": 2.0, "cache_read": 0, "cache_write": 0},
        }
        total, breakdown = compute_cost(
            "my-model", 1_000_000, 1_000_000, 0, 0, pricing_overrides=overrides)
        assert total == pytest.approx(3.0)
        assert breakdown["input"] == pytest.approx(1.0)

    def test_override_wildcard(self):
        """Wildcard '*' override applies when no exact match."""
        overrides = {
            "*": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        }
        total, _ = compute_cost(
            "any-model", 1_000_000, 1_000_000, 100_000, 100_000,
            pricing_overrides=overrides)
        assert total == 0

    def test_override_priority_over_wildcard(self):
        """Exact model match beats wildcard."""
        overrides = {
            "my-model": {"input": 5.0, "output": 10.0, "cache_read": 0, "cache_write": 0},
            "*": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        }
        total, _ = compute_cost(
            "my-model", 1_000_000, 1_000_000, 0, 0, pricing_overrides=overrides)
        assert total == pytest.approx(15.0)

    def test_free_pricing_zero_cost(self):
        """FREE_PRICING constant produces zero cost."""
        total, breakdown = compute_cost(
            "any-model", 500_000, 500_000, 100_000, 100_000,
            pricing_overrides={"any-model": FREE_PRICING})
        assert total == 0
        assert all(v == 0 for v in breakdown.values())

    def test_no_overrides_passes_none(self):
        """None overrides fall through to global pricing."""
        total, _ = compute_cost(
            "claude-opus-4-6", 1_000_000, 0, 0, 0, pricing_overrides=None)
        assert total == pytest.approx(15.0)

    def test_cache_pricing(self):
        """Cache tokens are priced correctly."""
        total, breakdown = compute_cost(
            "claude-opus-4-6", 0, 0, 1_000_000, 1_000_000)
        assert breakdown["cache_read"] == pytest.approx(1.5)
        assert breakdown["cache_write"] == pytest.approx(18.75)


# --- Pricing config CRUD ---

class TestPricingConfigCRUD:
    def test_set_and_get(self, db_path):
        db.set_pricing_config(DEVICE_ID, "my-model", 1.0, 2.0, 0.1, 0.2, db_path=db_path)
        configs = db.get_pricing_config(DEVICE_ID, db_path)
        assert len(configs) == 1
        assert configs[0]["model"] == "my-model"
        assert configs[0]["input_price"] == 1.0
        assert configs[0]["output_price"] == 2.0

    def test_upsert_updates_existing(self, db_path):
        db.set_pricing_config(DEVICE_ID, "my-model", 1.0, 2.0, 0.1, 0.2, db_path=db_path)
        db.set_pricing_config(DEVICE_ID, "my-model", 5.0, 10.0, 0.5, 1.0, db_path=db_path)
        configs = db.get_pricing_config(DEVICE_ID, db_path)
        assert len(configs) == 1
        assert configs[0]["input_price"] == 5.0

    def test_delete(self, db_path):
        db.set_pricing_config(DEVICE_ID, "my-model", 1.0, 2.0, 0.1, 0.2, db_path=db_path)
        assert db.delete_pricing_config(DEVICE_ID, "my-model", db_path=db_path) is True
        assert len(db.get_pricing_config(DEVICE_ID, db_path)) == 0

    def test_delete_nonexistent_returns_false(self, db_path):
        assert db.delete_pricing_config(DEVICE_ID, "no-such-model", db_path=db_path) is False

    def test_get_empty_config(self, db_path):
        configs = db.get_pricing_config(DEVICE_ID, db_path)
        assert configs == []

    def test_provider_wildcard_entry(self, db_path):
        db.set_pricing_config(DEVICE_ID, "*", 0, 0, 0, 0, provider="nvidia", db_path=db_path)
        configs = db.get_pricing_config(DEVICE_ID, db_path)
        assert len(configs) == 1
        assert configs[0]["model"] == "*"
        assert configs[0]["provider"] == "nvidia"

    def test_multiple_overrides(self, db_path):
        db.set_pricing_config(DEVICE_ID, "model-a", 1, 2, 0, 0, db_path=db_path)
        db.set_pricing_config(DEVICE_ID, "model-b", 3, 4, 0, 0, db_path=db_path)
        db.set_pricing_config(DEVICE_ID, "*", 0, 0, 0, 0, provider="nvidia", db_path=db_path)
        configs = db.get_pricing_config(DEVICE_ID, db_path)
        assert len(configs) == 3


# --- Pricing resolution ---

class TestPricingResolution:
    def test_exact_model_match(self, db_path):
        db.set_pricing_config(DEVICE_ID, "custom-model", 7.0, 14.0, 0.7, 1.4, db_path=db_path)
        result = db.resolve_pricing(DEVICE_ID, "custom-model", "anthropic", db_path=db_path)
        assert result["input"] == 7.0
        assert result["output"] == 14.0

    def test_provider_wildcard_match(self, db_path):
        db.set_pricing_config(DEVICE_ID, "*", 0, 0, 0, 0, provider="nvidia", db_path=db_path)
        result = db.resolve_pricing(DEVICE_ID, "glm-4.7", "nvidia", db_path=db_path)
        assert result["input"] == 0
        assert result["output"] == 0

    def test_model_beats_provider_wildcard(self, db_path):
        db.set_pricing_config(DEVICE_ID, "*", 0, 0, 0, 0, provider="nvidia", db_path=db_path)
        db.set_pricing_config(DEVICE_ID, "special-nvidia-model", 5.0, 10.0, 0.5, 1.0, db_path=db_path)
        result = db.resolve_pricing(DEVICE_ID, "special-nvidia-model", "nvidia", db_path=db_path)
        assert result["input"] == 5.0

    def test_fallback_to_global_default(self, db_path):
        result = db.resolve_pricing(DEVICE_ID, "claude-opus-4-6", "anthropic", db_path=db_path)
        assert result == MODEL_PRICING["claude-opus-4-6"]

    def test_fallback_to_sonnet_unknown(self, db_path):
        result = db.resolve_pricing(DEVICE_ID, "totally-unknown-model", "unknown", db_path=db_path)
        assert result == DEFAULT_PRICING

    def test_config_provider_defaults(self, db_path):
        """Config-level provider defaults apply when no device override exists."""
        provider_defaults = {
            "nvidia": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        }
        result = db.resolve_pricing(
            DEVICE_ID, "glm-4.7", "nvidia", db_path=db_path,
            provider_defaults=provider_defaults)
        assert result["input"] == 0


# --- Cost recalculation ---

class TestCostRecalculation:
    def test_recalculate_updates_costs(self, db_path):
        # Ingest events with original cost
        db.ingest_events(DEVICE_ID, [
            _make_event(cost=99.99, input_tokens=1000, output_tokens=500),
        ], db_path)

        # Set free pricing and recalculate
        db.set_pricing_config(DEVICE_ID, "claude-opus-4-6", 0, 0, 0, 0, db_path=db_path)
        count = db.recalculate_device_costs(DEVICE_ID, db_path=db_path)
        assert count == 1

        # Verify cost was updated to 0
        stats = db.get_device_stats(DEVICE_ID, days=30, db_path=db_path)
        assert stats["total_cost_usd"] == 0

    def test_recalculate_with_provider_wildcard(self, db_path):
        db.ingest_events(DEVICE_ID, [
            _make_event(model="glm-4.7", provider="nvidia", cost=5.0,
                        input_tokens=100000, output_tokens=50000),
        ], db_path)

        db.set_pricing_config(DEVICE_ID, "*", 0, 0, 0, 0, provider="nvidia", db_path=db_path)
        count = db.recalculate_device_costs(DEVICE_ID, db_path=db_path)
        assert count == 1

        stats = db.get_device_stats(DEVICE_ID, days=30, db_path=db_path)
        assert stats["total_cost_usd"] == 0

    def test_recalculate_returns_count(self, db_path):
        db.ingest_events(DEVICE_ID, [
            _make_event(cost=1.0),
            _make_event(cost=2.0, session_id="sess-002"),
            _make_event(cost=3.0, session_id="sess-003"),
        ], db_path)
        count = db.recalculate_device_costs(DEVICE_ID, db_path=db_path)
        assert count == 3

    def test_recalculate_no_events_returns_zero(self, db_path):
        count = db.recalculate_device_costs(DEVICE_ID, db_path=db_path)
        assert count == 0

    def test_recalculate_with_config_provider_defaults(self, db_path):
        """Provider defaults from config.json are used in recalculation."""
        db.ingest_events(DEVICE_ID, [
            _make_event(model="glm-4.7", provider="nvidia", cost=10.0,
                        input_tokens=100000, output_tokens=50000),
        ], db_path)

        provider_defaults = {
            "nvidia": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        }
        count = db.recalculate_device_costs(
            DEVICE_ID, db_path=db_path, provider_defaults=provider_defaults)
        assert count == 1

        stats = db.get_device_stats(DEVICE_ID, days=30, db_path=db_path)
        assert stats["total_cost_usd"] == 0


# --- Ingest with pricing ---

class TestIngestWithPricing:
    def test_ingest_recomputes_with_overrides(self, db_path):
        """When overrides provided, cost is recomputed from tokens."""
        overrides = {
            "claude-opus-4-6": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        }
        db.ingest_events(DEVICE_ID, [
            _make_event(cost=99.99, input_tokens=1000, output_tokens=500),
        ], db_path, pricing_overrides=overrides)

        stats = db.get_device_stats(DEVICE_ID, days=30, db_path=db_path)
        assert stats["total_cost_usd"] == 0

    def test_ingest_without_overrides_uses_client_cost(self, db_path):
        """Without overrides, client-provided cost_usd is preserved."""
        db.ingest_events(DEVICE_ID, [
            _make_event(cost=42.0),
        ], db_path, pricing_overrides=None)

        stats = db.get_device_stats(DEVICE_ID, days=30, db_path=db_path)
        assert stats["total_cost_usd"] == pytest.approx(42.0)

    def test_ingest_nvidia_free_zero_cost(self, db_path):
        """NVIDIA events with free override get $0 cost."""
        overrides = {
            "__provider__nvidia": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                                   "_provider": "nvidia"},
        }
        # The ingest uses compute_cost which only checks model and "*" keys
        # So for provider-level, we need the "*" key approach
        overrides_simple = {
            "*": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        }
        db.ingest_events(DEVICE_ID, [
            _make_event(model="glm-4.7", provider="nvidia", cost=5.0,
                        input_tokens=100000, output_tokens=50000),
        ], db_path, pricing_overrides=overrides_simple)

        stats = db.get_device_stats(DEVICE_ID, days=30, db_path=db_path)
        assert stats["total_cost_usd"] == 0


# --- API endpoints ---

class TestPricingAPI:
    def test_get_config_empty(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.get(f"/api/pricing/{device_id}/config", headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["overrides"] == []

    def test_set_and_get_config(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.post(f"/api/pricing/{device_id}/config", json={
            "model": "claude-opus-4-6",
            "input_price": 10.0,
            "output_price": 50.0,
            "cache_read_price": 1.0,
            "cache_write_price": 12.5,
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

        resp = client.get(f"/api/pricing/{device_id}/config", headers=headers)
        data = resp.get_json()
        assert len(data["overrides"]) == 1
        assert data["overrides"][0]["model"] == "claude-opus-4-6"
        assert data["overrides"][0]["input_price"] == 10.0

    def test_set_invalid_model_400(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.post(f"/api/pricing/{device_id}/config", json={
            "input_price": 1.0,
            "output_price": 2.0,
            "cache_read_price": 0.1,
            "cache_write_price": 0.2,
        }, headers=headers)
        assert resp.status_code == 400
        assert "model" in resp.get_json()["error"]

    def test_set_negative_price_400(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.post(f"/api/pricing/{device_id}/config", json={
            "model": "test",
            "input_price": -1.0,
            "output_price": 2.0,
            "cache_read_price": 0.1,
            "cache_write_price": 0.2,
        }, headers=headers)
        assert resp.status_code == 400

    def test_set_missing_price_400(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.post(f"/api/pricing/{device_id}/config", json={
            "model": "test",
            "input_price": 1.0,
            # missing other prices
        }, headers=headers)
        assert resp.status_code == 400

    def test_delete_config(self, hosted_setup):
        client, device_id, headers = hosted_setup
        # Create then delete
        client.post(f"/api/pricing/{device_id}/config", json={
            "model": "test-model",
            "input_price": 1.0, "output_price": 2.0,
            "cache_read_price": 0.1, "cache_write_price": 0.2,
        }, headers=headers)
        resp = client.delete(f"/api/pricing/{device_id}/config", json={
            "model": "test-model",
        }, headers=headers)
        assert resp.status_code == 200

        configs = client.get(f"/api/pricing/{device_id}/config", headers=headers).get_json()
        assert len(configs["overrides"]) == 0

    def test_delete_nonexistent_404(self, hosted_setup):
        client, device_id, headers = hosted_setup
        resp = client.delete(f"/api/pricing/{device_id}/config", json={
            "model": "no-such-model",
        }, headers=headers)
        assert resp.status_code == 404

    def test_recalculate_endpoint(self, hosted_setup):
        client, device_id, headers = hosted_setup
        # Ingest some events
        client.post("/api/ingest", json={
            "device_id": device_id,
            "events": [_make_event(cost=5.0)],
        }, headers=headers)
        # Recalculate
        resp = client.post(f"/api/pricing/{device_id}/recalculate", headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["events_updated"] == 1

    def test_models_endpoint(self, hosted_setup):
        client, device_id, headers = hosted_setup
        # Ingest events with different models
        client.post("/api/ingest", json={
            "device_id": device_id,
            "events": [
                _make_event(model="claude-opus-4-6"),
                _make_event(model="claude-haiku-4-5-20251001", session_id="s2"),
            ],
        }, headers=headers)
        resp = client.get(f"/api/pricing/{device_id}/models", headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["models"]) == 2
        # Each model should have effective_pricing
        for m in data["models"]:
            assert "effective_pricing" in m
            assert "input" in m["effective_pricing"]

    def test_invalid_device_id_400(self, hosted_setup):
        client, device_id, headers = hosted_setup
        # GET endpoints
        for endpoint in ["/config", "/models"]:
            resp = client.get(f"/api/pricing/INVALID!{endpoint}")
            assert resp.status_code == 400, f"GET {endpoint} should return 400"
        # POST endpoints
        resp = client.post(f"/api/pricing/INVALID!/recalculate")
        assert resp.status_code == 400
        resp = client.post(f"/api/pricing/INVALID!/config", json={
            "model": "test", "input_price": 0, "output_price": 0,
            "cache_read_price": 0, "cache_write_price": 0,
        })
        assert resp.status_code == 400

    def test_ingest_applies_pricing_overrides(self, hosted_setup):
        """Ingest endpoint uses device pricing overrides."""
        client, device_id, headers = hosted_setup
        # Set free pricing for opus
        client.post(f"/api/pricing/{device_id}/config", json={
            "model": "claude-opus-4-6",
            "input_price": 0, "output_price": 0,
            "cache_read_price": 0, "cache_write_price": 0,
        }, headers=headers)
        # Ingest with client-reported cost of $99
        client.post("/api/ingest", json={
            "device_id": device_id,
            "events": [_make_event(cost=99.0, input_tokens=1000, output_tokens=500)],
        }, headers=headers)
        # Stats should show $0 (server recomputed)
        resp = client.get(f"/api/stats/{device_id}", headers=headers)
        stats = resp.get_json()
        assert stats["total_cost_usd"] == 0

    def test_full_flow_nvidia_free(self, hosted_setup):
        """End-to-end: set nvidia as free provider, ingest, verify zero cost."""
        client, device_id, headers = hosted_setup
        # Set nvidia as free via provider wildcard
        client.post(f"/api/pricing/{device_id}/config", json={
            "model": "*",
            "provider": "nvidia",
            "input_price": 0, "output_price": 0,
            "cache_read_price": 0, "cache_write_price": 0,
        }, headers=headers)

        # Ingest nvidia events with non-zero client cost
        client.post("/api/ingest", json={
            "device_id": device_id,
            "events": [
                _make_event(model="glm-4.7", provider="nvidia", cost=5.0,
                            input_tokens=100000, output_tokens=50000),
                _make_event(model="minimax-m2.1", provider="nvidia", cost=3.0,
                            input_tokens=80000, output_tokens=40000, session_id="s2"),
            ],
        }, headers=headers)

        # Verify costs are 0
        resp = client.get(f"/api/stats/{device_id}", headers=headers)
        stats = resp.get_json()
        assert stats["total_cost_usd"] == 0

    def test_recalculate_after_price_change(self, hosted_setup):
        """Change pricing, recalculate, verify costs updated."""
        client, device_id, headers = hosted_setup
        # Ingest with default pricing
        client.post("/api/ingest", json={
            "device_id": device_id,
            "events": [_make_event(cost=5.0, input_tokens=1000, output_tokens=500)],
        }, headers=headers)

        # Verify non-zero cost
        resp = client.get(f"/api/stats/{device_id}", headers=headers)
        before_cost = resp.get_json()["total_cost_usd"]
        assert before_cost > 0

        # Set free pricing
        client.post(f"/api/pricing/{device_id}/config", json={
            "model": "claude-opus-4-6",
            "input_price": 0, "output_price": 0,
            "cache_read_price": 0, "cache_write_price": 0,
        }, headers=headers)

        # Recalculate
        resp = client.post(f"/api/pricing/{device_id}/recalculate", headers=headers)
        assert resp.get_json()["events_updated"] == 1

        # Verify cost is now 0
        resp = client.get(f"/api/stats/{device_id}", headers=headers)
        assert resp.get_json()["total_cost_usd"] == 0
