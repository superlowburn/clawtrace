"""Flask HTTP API for ClawTrace — local + hosted modes."""

import json
import os
import re
import threading
import time
from functools import wraps

from flask import Flask, jsonify, request, send_file, render_template_string, make_response
from flask_cors import CORS

from .parser import find_session_files, parse_session_file
from .aggregator import get_summary, get_cost_timeseries, get_model_breakdown, get_project_breakdown, get_top_sessions
from .anomaly import detect_anomalies
from . import db


def _load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            return json.load(f)
    return {
        "data_paths": ["~/.openclaw/agents", "~/.claude/projects"],
        "server_port": 19898,
        "refresh_interval_seconds": 60,
        "anomaly_threshold": 0.25,
    }


_DEVICE_ID_RE = re.compile(r'^[a-f0-9]{8,64}$')


def _valid_device_id(device_id: str) -> bool:
    return bool(_DEVICE_ID_RE.match(device_id))


# Free tier limits
_FREE_MAX_PROJECTS = 1
_FREE_MAX_HISTORY_DAYS = 7


class DataCache:
    """Thread-safe cached data store that refreshes periodically."""

    def __init__(self, config: dict):
        self.config = config
        self._messages = []
        self._lock = threading.Lock()
        self._last_refresh = 0

    def _needs_refresh(self) -> bool:
        ttl = self.config.get("cache_ttl_seconds", 60)
        return time.time() - self._last_refresh > ttl

    def get_messages(self) -> list:
        if self._needs_refresh():
            self.refresh()
        with self._lock:
            return self._messages

    def refresh(self):
        data_paths = self.config.get("data_paths", [])
        files = find_session_files(data_paths)
        messages = []
        for f in files:
            messages.extend(parse_session_file(f))
        with self._lock:
            self._messages = messages
            self._last_refresh = time.time()


def create_app(config: dict | None = None) -> Flask:
    if config is None:
        config = _load_config()

    app = Flask(__name__)
    CORS(app, origins="*")

    cache = DataCache(config)
    db_path = config.get("db_path")

    # Initialize DB on startup
    db.init_db(db_path)

    def _require_device_auth(device_id: str):
        """Check Authorization: Bearer <secret> header. Returns error response or None."""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Authorization required", "hint": "Add header: Authorization: Bearer <device_secret>"}), 401
        token = auth_header[7:]
        if not db.verify_device_secret(device_id, token, db_path):
            return jsonify({"error": "Invalid device secret"}), 403
        return None

    def _get_device_tier(device_id: str) -> str:
        """Get tier for a device. Returns 'free' if device not found."""
        device = db.get_device(device_id, db_path)
        return device["tier"] if device else "free"

    # --- Registration ---
    @app.route("/api/register", methods=["POST"])
    def api_register():
        device_id, device_secret = db.register_device(db_path)
        return jsonify({
            "device_id": device_id,
            "device_secret": device_secret,
            "dashboard_url": f"/d/{device_id}#{device_secret}",
        })

    @app.route("/api/claim", methods=["POST"])
    def api_claim():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400
        device_id = data.get("device_id")
        if not device_id or not _valid_device_id(device_id):
            return jsonify({"error": "Valid device_id required"}), 400
        device_secret = db.claim_device(device_id, db_path)
        if device_secret is None:
            return jsonify({"error": "Device not found or already claimed"}), 404
        return jsonify({
            "device_id": device_id,
            "device_secret": device_secret,
            "dashboard_url": f"/d/{device_id}#{device_secret}",
        })

    # --- Landing page ---
    @app.route("/")
    def landing():
        html_path = os.path.join(os.path.dirname(__file__), "landing.html")
        if os.path.exists(html_path):
            return send_file(html_path)
        return "<h1>ClawTrace</h1><p>Analytics for OpenClaw</p>", 200

    # --- Per-device dashboard ---
    @app.route("/d/<device_id>")
    def device_dashboard(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        resp = make_response(send_file(html_path))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    # --- Hosted API: Ingest events from remote devices ---
    @app.route("/api/ingest", methods=["POST"])
    def api_ingest():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        device_id = data.get("device_id")
        if not device_id or not _valid_device_id(device_id):
            return jsonify({"error": "Valid device_id required (hex, 8-64 chars)"}), 400

        # Auth check
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err

        events = data.get("events", [])
        if not isinstance(events, list) or len(events) == 0:
            return jsonify({"error": "Non-empty events array required"}), 400

        if len(events) > 500:
            return jsonify({"error": "Max 500 events per batch"}), 400

        # Tier enforcement: free tier = 1 project only
        tier = _get_device_tier(device_id)
        if tier == "free":
            first_project = db.get_device_first_project(device_id, db_path)
            if first_project:
                events = [e for e in events if e.get("project") == first_project]
                if not events:
                    return jsonify({
                        "error": "Free tier limited to 1 project",
                        "allowed_project": first_project,
                        "upgrade_url": "https://clawtrace.vybng.co/#pricing",
                    }), 403

        db.ensure_device(device_id, db_path)
        pricing_defaults = config.get("pricing", {}).get("provider_defaults")
        overrides = db.get_device_pricing_overrides(device_id, db_path, provider_defaults=pricing_defaults)
        count = db.ingest_events(device_id, events, db_path, pricing_overrides=overrides or None)

        # Check alert thresholds after ingest
        alert_defaults = config.get("alerts", {})
        new_alerts = db.check_alerts(device_id, db_path=db_path, config_defaults=alert_defaults)

        return jsonify({"status": "ok", "ingested": count, "new_alerts": len(new_alerts)})

    # --- Hosted API: Clear device events for resync ---
    @app.route("/api/resync/<device_id>", methods=["POST"])
    def api_resync(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err
        deleted = db.clear_device_events(device_id, db_path)
        return jsonify({"status": "ok", "deleted": deleted})

    # --- Hosted API: Device stats for dashboard ---
    @app.route("/api/stats/<device_id>")
    def api_stats(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err

        days = request.args.get("days", 7, type=int)
        days = min(max(days, 1), 365)

        # Free tier: clamp to 7 days max
        tier = _get_device_tier(device_id)
        if tier == "free":
            days = min(days, _FREE_MAX_HISTORY_DAYS)

        stats = db.get_device_stats(device_id, days=days, db_path=db_path)
        return jsonify(stats)

    # --- Hosted API: Optimization suggestions ---
    @app.route("/api/optimize/<device_id>")
    def api_optimize(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err
        days = request.args.get("days", 7, type=int)
        days = min(max(days, 1), 365)
        suggestions = db.get_optimization_suggestions(device_id, days=days, db_path=db_path)
        return jsonify({"suggestions": suggestions})

    # --- Hosted API: Community benchmarks ---
    @app.route("/api/community")
    def api_community():
        stats = db.get_community_stats(db_path)
        return jsonify(stats)

    # --- Hosted API: Alerts ---
    @app.route("/api/alerts/<device_id>")
    def api_alerts(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err

        # Free tier: alerts are a Pro feature
        tier = _get_device_tier(device_id)
        if tier == "free":
            return jsonify({"alerts": [], "count": 0, "tier_limited": True,
                           "upgrade_url": "https://clawtrace.vybng.co/#pricing"})

        ack_param = request.args.get("acknowledged")
        acknowledged = None
        if ack_param is not None:
            acknowledged = ack_param.lower() in ("true", "1", "yes")
        alerts = db.get_alerts(device_id, acknowledged=acknowledged, db_path=db_path)
        return jsonify({"alerts": alerts, "count": len(alerts)})

    @app.route("/api/alerts/<device_id>/acknowledge/<int:alert_id>", methods=["POST"])
    def api_acknowledge_alert(device_id, alert_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err
        success = db.acknowledge_alert(alert_id, device_id, db_path=db_path)
        if not success:
            return jsonify({"error": "Alert not found or not owned by device"}), 404
        return jsonify({"status": "ok"})

    @app.route("/api/alerts/<device_id>/config")
    def api_alert_config_get(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err
        alert_defaults = config.get("alerts", {})
        cfg = db.get_alert_config(device_id, db_path=db_path, defaults=alert_defaults)
        return jsonify(cfg)

    @app.route("/api/alerts/<device_id>/config", methods=["POST"])
    def api_alert_config_set(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        alert_type = data.get("alert_type")
        threshold = data.get("threshold")
        enabled = data.get("enabled", True)

        valid_types = ("daily_budget", "session_spike", "hourly_burn_rate",
                       "hourly_request_volume", "hourly_token_volume", "session_duration")
        if alert_type not in valid_types:
            return jsonify({"error": f"alert_type must be one of {valid_types}"}), 400
        if not isinstance(threshold, (int, float)) or threshold < 0:
            return jsonify({"error": "threshold must be a non-negative number"}), 400

        db.set_alert_config(device_id, alert_type, float(threshold), enabled=enabled, db_path=db_path)
        return jsonify({"status": "ok"})

    # --- Hosted API: Pricing config ---
    @app.route("/api/pricing/<device_id>/config")
    def api_pricing_config_get(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err
        overrides = db.get_pricing_config(device_id, db_path)
        return jsonify({"overrides": overrides})

    @app.route("/api/pricing/<device_id>/config", methods=["POST"])
    def api_pricing_config_set(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        model = data.get("model")
        if not model:
            return jsonify({"error": "model is required"}), 400

        provider = data.get("provider", "")
        price_fields = ["input_price", "output_price", "cache_read_price", "cache_write_price"]
        for field in price_fields:
            val = data.get(field)
            if val is None or not isinstance(val, (int, float)) or val < 0:
                return jsonify({"error": f"{field} must be a non-negative number"}), 400

        db.set_pricing_config(
            device_id, model,
            float(data["input_price"]), float(data["output_price"]),
            float(data["cache_read_price"]), float(data["cache_write_price"]),
            provider=provider, db_path=db_path,
        )
        return jsonify({"status": "ok"})

    @app.route("/api/pricing/<device_id>/config", methods=["DELETE"])
    def api_pricing_config_delete(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        model = data.get("model")
        if not model:
            return jsonify({"error": "model is required"}), 400
        provider = data.get("provider", "")

        deleted = db.delete_pricing_config(device_id, model, provider=provider, db_path=db_path)
        if not deleted:
            return jsonify({"error": "Override not found"}), 404
        return jsonify({"status": "ok"})

    @app.route("/api/pricing/<device_id>/recalculate", methods=["POST"])
    def api_pricing_recalculate(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err
        pricing_defaults = config.get("pricing", {}).get("provider_defaults")
        count = db.recalculate_device_costs(device_id, db_path=db_path,
                                            provider_defaults=pricing_defaults)
        return jsonify({"status": "ok", "events_updated": count})

    @app.route("/api/pricing/<device_id>/models")
    def api_pricing_models(device_id):
        if not _valid_device_id(device_id):
            return jsonify({"error": "Invalid device ID"}), 400
        auth_err = _require_device_auth(device_id)
        if auth_err:
            return auth_err
        pricing_defaults = config.get("pricing", {}).get("provider_defaults")
        models = db.get_device_models(device_id, db_path)
        # Enrich with effective pricing
        for m in models:
            effective = db.resolve_pricing(
                device_id, m["model"], m.get("provider", ""),
                db_path=db_path, provider_defaults=pricing_defaults,
            )
            m["effective_pricing"] = effective
        return jsonify({"models": models})

    # --- Local API (existing, for backward compat) ---
    @app.route("/local/dashboard")
    def local_dashboard():
        html_path = os.path.join(os.path.dirname(__file__), "local_dashboard.html")
        if os.path.exists(html_path):
            return send_file(html_path)
        return "<h1>Local Dashboard</h1>", 200

    @app.route("/api/summary")
    def api_summary():
        messages = cache.get_messages()
        return jsonify(get_summary(messages))

    @app.route("/api/costs")
    def api_costs():
        messages = cache.get_messages()
        days = request.args.get("range", "7d")
        try:
            days_int = int(days.rstrip("d"))
        except ValueError:
            days_int = 7
        return jsonify(get_cost_timeseries(messages, days=days_int))

    @app.route("/api/models")
    def api_models():
        messages = cache.get_messages()
        return jsonify(get_model_breakdown(messages))

    @app.route("/api/projects")
    def api_projects():
        messages = cache.get_messages()
        return jsonify(get_project_breakdown(messages))

    @app.route("/api/sessions")
    def api_sessions():
        messages = cache.get_messages()
        n = request.args.get("n", 5, type=int)
        return jsonify(get_top_sessions(messages, n=n))

    @app.route("/api/anomalies")
    def api_anomalies():
        messages = cache.get_messages()
        threshold = config.get("anomaly_threshold", 0.25)
        return jsonify([{
            "date": a.date,
            "expected_cost": a.expected_cost,
            "actual_cost": a.actual_cost,
            "severity": a.severity,
            "pct_over": a.pct_over,
        } for a in detect_anomalies(messages, threshold=threshold)])

    @app.route("/api/health")
    def api_health():
        return jsonify({"status": "ok"})

    return app


def run_server(config: dict | None = None):
    if config is None:
        config = _load_config()
    port = config.get("server_port", 19898)
    host = config.get("server_host", "0.0.0.0")
    app = create_app(config)
    print(f"ClawTrace API running on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
