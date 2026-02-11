"""Tests for install script serving routes."""

import pytest
from engine.server import create_app


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    config = {
        "data_paths": [],
        "cache_ttl_seconds": 9999,
        "server_port": 19898,
        "db_path": db_path,
    }
    app = create_app(config)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestInstallRoutes:
    def test_install_script_served(self, client):
        """GET /install returns install.sh as text/plain."""
        resp = client.get("/install")
        assert resp.status_code == 200
        assert "text/plain" in resp.content_type
        assert resp.data.startswith(b"#!/bin/bash")

    def test_sender_script_served(self, client):
        """GET /install/sender.py returns sender.py as text/plain."""
        resp = client.get("/install/sender.py")
        assert resp.status_code == 200
        assert "text/plain" in resp.content_type
        assert b"API_BASE" in resp.data

    def test_uninstall_script_served(self, client):
        """GET /uninstall returns uninstall.sh as text/plain."""
        resp = client.get("/uninstall")
        assert resp.status_code == 200
        assert "text/plain" in resp.content_type
        assert resp.data.startswith(b"#!/bin/bash")
