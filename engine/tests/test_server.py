"""Tests for Flask server routes."""

import pytest
from engine.server import create_app


@pytest.fixture
def client():
    """Create test Flask client with empty config."""
    config = {
        "data_paths": [],  # Empty paths for testing
        "cache_ttl_seconds": 9999,  # Don't auto-refresh during tests
        "server_port": 19898,
    }
    app = create_app(config)
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


class TestDashboardRoute:
    def test_returns_html(self, client):
        """GET / should return HTML dashboard."""
        response = client.get("/")
        assert response.status_code == 200
        assert response.content_type.startswith("text/html")
        # Check for basic HTML structure
        html = response.data.decode("utf-8")
        assert "<html" in html.lower()
        assert "<body" in html.lower()


class TestHealthRoute:
    def test_health_check(self, client):
        """GET /api/health should return ok."""
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.get_json()
        assert data == {"status": "ok"}


class TestAPIRoutes:
    def test_summary_endpoint(self, client):
        """GET /api/summary should return summary object."""
        response = client.get("/api/summary")
        assert response.status_code == 200
        data = response.get_json()
        # Empty cache = zeros
        assert "total_cost_usd" in data
        assert "message_count" in data
        assert "session_count" in data

    def test_costs_endpoint(self, client):
        """GET /api/costs should return timeseries array."""
        response = client.get("/api/costs")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)

    def test_models_endpoint(self, client):
        """GET /api/models should return model breakdown array."""
        response = client.get("/api/models")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)

    def test_projects_endpoint(self, client):
        """GET /api/projects should return project breakdown array."""
        response = client.get("/api/projects")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)

    def test_sessions_endpoint(self, client):
        """GET /api/sessions should return top sessions array."""
        response = client.get("/api/sessions")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)

    def test_anomalies_endpoint(self, client):
        """GET /api/anomalies should return anomalies array."""
        response = client.get("/api/anomalies")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)
