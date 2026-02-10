"""SQLite storage for cost snapshots, anomalies, and hosted device data."""

import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .parser import MessageUsage
from .anomaly import Anomaly
from .pricing import compute_cost, MODEL_PRICING, DEFAULT_PRICING

DEFAULT_DB_PATH = "~/.clawtrace/clawtrace.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_snapshots (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    model TEXT,
    provider TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    session_file TEXT
);

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY,
    detected_at TEXT NOT NULL,
    project TEXT,
    metric TEXT,
    expected_value REAL,
    actual_value REAL,
    severity TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT,
    tier TEXT NOT NULL DEFAULT 'free',
    nickname TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL,
    session_id TEXT,
    event_type TEXT NOT NULL,
    tool_name TEXT,
    model TEXT,
    project TEXT,
    provider TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER,
    cost_usd REAL DEFAULT 0,
    success INTEGER DEFAULT 1,
    timestamp TEXT NOT NULL,
    tools TEXT,
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON cost_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_snapshots_project ON cost_snapshots(project);
CREATE INDEX IF NOT EXISTS idx_snapshots_model ON cost_snapshots(model);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON cost_snapshots(session_id);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    acknowledged INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS alert_config (
    device_id TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    threshold REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (device_id, alert_type)
);

CREATE TABLE IF NOT EXISTS pricing_config (
    device_id TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    input_price REAL NOT NULL,
    output_price REAL NOT NULL,
    cache_read_price REAL NOT NULL,
    cache_write_price REAL NOT NULL,
    PRIMARY KEY (device_id, model, provider)
);

CREATE INDEX IF NOT EXISTS idx_events_device ON events(device_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_device_ts ON events(device_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_device ON alerts(device_id);
CREATE INDEX IF NOT EXISTS idx_alerts_device_type ON alerts(device_id, alert_type);
"""


def _get_db_path(db_path: str | None = None) -> str:
    path = os.path.expanduser(db_path or DEFAULT_DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def init_db(db_path: str | None = None) -> str:
    """Initialize the database and return the path."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)

    # Migration: add tools column if missing
    try:
        conn.execute("ALTER TABLE events ADD COLUMN tools TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migration: add device_secret_hash column
    try:
        conn.execute("ALTER TABLE devices ADD COLUMN device_secret_hash TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    conn.commit()
    conn.close()
    return path


def store_messages(messages: list[MessageUsage], db_path: str | None = None) -> int:
    """Store parsed messages into the database. Returns count of new rows inserted."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Get existing session+timestamp combos to avoid duplicates
    cursor = conn.execute("SELECT session_id, timestamp FROM cost_snapshots")
    existing = set((row[0], row[1]) for row in cursor.fetchall())

    inserted = 0
    for m in messages:
        key = (m.session_id, m.timestamp)
        if key in existing:
            continue

        conn.execute(
            """INSERT INTO cost_snapshots
               (timestamp, session_id, project, model, provider,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                cost_usd, session_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (m.timestamp, m.session_id, m.project_name, m.model, m.provider,
             m.input_tokens, m.output_tokens, m.cache_read_tokens, m.cache_write_tokens,
             m.cost_total, m.session_file),
        )
        existing.add(key)
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


def store_anomalies(anomalies: list[Anomaly], db_path: str | None = None) -> int:
    """Store detected anomalies. Returns count inserted."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)

    # Dedup by date+project
    cursor = conn.execute("SELECT detected_at, project FROM anomalies")
    existing = set((row[0], row[1]) for row in cursor.fetchall())

    inserted = 0
    for a in anomalies:
        key = (a.date, a.project)
        if key in existing:
            continue
        conn.execute(
            """INSERT INTO anomalies (detected_at, project, metric, expected_value, actual_value, severity)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (a.date, a.project, "daily_cost", a.expected_cost, a.actual_cost, a.severity),
        )
        existing.add(key)
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


def get_recent_snapshots(days: int = 7, db_path: str | None = None) -> list[dict]:
    """Get cost snapshots from the last N days."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """SELECT * FROM cost_snapshots
           WHERE timestamp >= datetime('now', ?)
           ORDER BY timestamp DESC""",
        (f"-{days} days",),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_recent_anomalies(days: int = 30, db_path: str | None = None) -> list[dict]:
    """Get anomalies from the last N days."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """SELECT * FROM anomalies
           WHERE detected_at >= date('now', ?)
           ORDER BY detected_at DESC""",
        (f"-{days} days",),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


# --- Hosted multi-tenant functions ---

def ensure_device(device_id: str, db_path: str | None = None) -> None:
    """Create device record if it doesn't exist, update last_seen."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO devices (device_id, created_at, last_seen)
           VALUES (?, ?, ?)
           ON CONFLICT(device_id) DO UPDATE SET last_seen = ?""",
        (device_id, now, now, now),
    )
    conn.commit()
    conn.close()


def clear_device_events(device_id: str, db_path: str | None = None) -> int:
    """Delete all events for a device (for resync). Returns count deleted."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    cursor = conn.execute("DELETE FROM events WHERE device_id = ?", (device_id,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def ingest_events(device_id: str, events: list[dict], db_path: str | None = None,
                   pricing_overrides: dict | None = None) -> int:
    """Store events from a remote device. Returns count inserted.

    If pricing_overrides is provided, recomputes cost_usd from tokens + pricing
    instead of using the client-provided value.
    """
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")

    inserted = 0
    for e in events:
        cost_usd = e.get("cost_usd", 0)
        if pricing_overrides:
            # Build per-event overrides: resolve provider wildcards
            model = e.get("model", "unknown")
            provider = e.get("provider", "")
            event_overrides = {}
            # Check for exact model override
            if model in pricing_overrides:
                event_overrides[model] = pricing_overrides[model]
            else:
                # Check for provider wildcard (from get_device_pricing_overrides)
                provider_key = f"__provider__{provider}"
                if provider_key in pricing_overrides:
                    p = pricing_overrides[provider_key]
                    event_overrides["*"] = {k: v for k, v in p.items() if not k.startswith("_")}
                elif "*" in pricing_overrides:
                    # Bare wildcard (e.g. passed directly)
                    event_overrides["*"] = pricing_overrides["*"]
            total, _ = compute_cost(
                model,
                e.get("input_tokens", 0),
                e.get("output_tokens", 0),
                e.get("cache_read_tokens", 0),
                e.get("cache_write_tokens", 0),
                pricing_overrides=event_overrides or None,
            )
            cost_usd = total

        conn.execute(
            """INSERT INTO events
               (device_id, session_id, event_type, tool_name, model, project, provider,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                latency_ms, cost_usd, success, timestamp, tools)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                device_id,
                e.get("session_id"),
                e.get("event_type", "llm.usage"),
                e.get("tool_name"),
                e.get("model"),
                e.get("project"),
                e.get("provider"),
                e.get("input_tokens", 0),
                e.get("output_tokens", 0),
                e.get("cache_read_tokens", 0),
                e.get("cache_write_tokens", 0),
                e.get("latency_ms"),
                cost_usd,
                1 if e.get("success", True) else 0,
                e.get("timestamp", datetime.now(timezone.utc).isoformat()),
                e.get("tools"),
            ),
        )
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


def get_device_stats(device_id: str, days: int = 7, db_path: str | None = None) -> dict:
    """Get aggregated stats for a device's dashboard."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    # Summary stats
    row = conn.execute(
        """SELECT
             COUNT(*) as total_requests,
             COALESCE(SUM(cost_usd), 0) as total_cost,
             COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0) as total_tokens
           FROM events
           WHERE device_id = ? AND timestamp >= datetime('now', ?)""",
        (device_id, f"-{days} days"),
    ).fetchone()

    total_requests = row["total_requests"]
    total_cost = row["total_cost"]
    total_tokens = row["total_tokens"]
    avg_cost = total_cost / total_requests if total_requests > 0 else 0

    # Top model
    top_model_row = conn.execute(
        """SELECT model, COUNT(*) as cnt FROM events
           WHERE device_id = ? AND model IS NOT NULL AND timestamp >= datetime('now', ?)
           GROUP BY model ORDER BY cnt DESC LIMIT 1""",
        (device_id, f"-{days} days"),
    ).fetchone()
    top_model = top_model_row["model"] if top_model_row else None

    # Daily cost timeseries
    daily_rows = conn.execute(
        """SELECT date(timestamp) as day, SUM(cost_usd) as cost
           FROM events WHERE device_id = ? AND timestamp >= datetime('now', ?)
           GROUP BY date(timestamp) ORDER BY day""",
        (device_id, f"-{days} days"),
    ).fetchall()
    timeseries = [{"date": r["day"], "cost_usd": round(r["cost"], 4)} for r in daily_rows]

    # Model breakdown
    model_rows = conn.execute(
        """SELECT model, COUNT(*) as count, SUM(cost_usd) as cost,
                  SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) as tokens
           FROM events WHERE device_id = ? AND model IS NOT NULL AND timestamp >= datetime('now', ?)
           GROUP BY model ORDER BY cost DESC""",
        (device_id, f"-{days} days"),
    ).fetchall()
    models = [{"model": r["model"], "count": r["count"], "cost_usd": round(r["cost"], 4),
               "tokens": r["tokens"]} for r in model_rows]

    # Project breakdown
    project_rows = conn.execute(
        """SELECT project, COUNT(*) as count, SUM(cost_usd) as cost,
                  COUNT(DISTINCT session_id) as sessions
           FROM events WHERE device_id = ? AND project IS NOT NULL AND timestamp >= datetime('now', ?)
           GROUP BY project ORDER BY cost DESC""",
        (device_id, f"-{days} days"),
    ).fetchall()
    projects = [{"project": r["project"], "count": r["count"], "cost_usd": round(r["cost"], 4),
                 "sessions": r["sessions"]} for r in project_rows]

    # Recent sessions
    session_rows = conn.execute(
        """SELECT session_id, project, COUNT(*) as requests, SUM(cost_usd) as cost,
                  MAX(timestamp) as last_active
           FROM events WHERE device_id = ? AND session_id IS NOT NULL AND timestamp >= datetime('now', ?)
           GROUP BY session_id ORDER BY last_active DESC LIMIT 10""",
        (device_id, f"-{days} days"),
    ).fetchall()
    sessions = [{"session_id": r["session_id"], "project": r["project"],
                 "requests": r["requests"], "cost_usd": round(r["cost"], 4),
                 "last_active": r["last_active"]} for r in session_rows]

    # Tool breakdown
    tool_rows = conn.execute(
        """SELECT tools, cost_usd FROM events
           WHERE device_id = ? AND tools IS NOT NULL AND tools != '' AND timestamp >= datetime('now', ?)""",
        (device_id, f"-{days} days"),
    ).fetchall()

    tool_costs = {}
    for row in tool_rows:
        cost = row[1] or 0
        for tool in row[0].split(","):
            tool = tool.strip()
            if tool:
                if tool not in tool_costs:
                    tool_costs[tool] = {"count": 0, "cost_usd": 0}
                tool_costs[tool]["count"] += 1
                tool_costs[tool]["cost_usd"] += cost

    tools_list = [{"tool": t, "count": d["count"], "cost_usd": round(d["cost_usd"], 4)}
                  for t, d in sorted(tool_costs.items(), key=lambda x: x[1]["cost_usd"], reverse=True)]

    conn.close()

    return {
        "device_id": device_id,
        "days": days,
        "total_requests": total_requests,
        "total_cost_usd": round(total_cost, 4),
        "avg_cost_per_request": round(avg_cost, 6),
        "total_tokens": total_tokens,
        "top_model": top_model,
        "timeseries": timeseries,
        "models": models,
        "projects": projects,
        "sessions": sessions,
        "tools": tools_list,
    }


def get_optimization_suggestions(device_id: str, days: int = 7, db_path: str | None = None) -> list[dict]:
    """Analyze usage patterns and return cost optimization suggestions."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    suggestions = []

    # 1. Find projects using expensive models where cheaper ones might work
    # Look for projects where >50% of requests use Opus but avg tokens are low
    project_model_rows = conn.execute(
        """SELECT project, model, COUNT(*) as cnt, SUM(cost_usd) as cost,
                  AVG(input_tokens + output_tokens) as avg_tokens
           FROM events
           WHERE device_id = ? AND timestamp >= datetime('now', ?)
                 AND model IS NOT NULL AND project IS NOT NULL
           GROUP BY project, model
           ORDER BY cost DESC""",
        (device_id, f"-{days} days"),
    ).fetchall()

    # Group by project
    project_models = {}
    for row in project_model_rows:
        proj = row["project"]
        if proj not in project_models:
            project_models[proj] = []
        project_models[proj].append({
            "model": row["model"],
            "count": row["cnt"],
            "cost": row["cost"],
            "avg_tokens": row["avg_tokens"],
        })

    for proj, models in project_models.items():
        opus_usage = [m for m in models if "opus" in m["model"]]
        if not opus_usage:
            continue
        opus_cost = sum(m["cost"] for m in opus_usage)
        opus_count = sum(m["count"] for m in opus_usage)
        total_cost = sum(m["cost"] for m in models)

        if opus_cost > 1.0:  # Only suggest if meaningful savings
            # Estimate savings: Opus is ~5x Sonnet price
            estimated_savings = opus_cost * 0.8  # ~80% savings switching to Sonnet
            suggestions.append({
                "type": "model_downgrade",
                "severity": "high" if estimated_savings > 10 else "medium",
                "project": proj,
                "current_model": "opus",
                "suggested_model": "sonnet",
                "current_cost": round(opus_cost, 2),
                "estimated_savings": round(estimated_savings, 2),
                "affected_requests": opus_count,
                "message": f"Project '{proj}' spent ${opus_cost:.2f} on Opus ({opus_count} requests). "
                           f"Switching to Sonnet could save ~${estimated_savings:.2f}/week.",
            })

    # 2. Find tools with high cost per invocation (potential automation inefficiency)
    tool_rows = conn.execute(
        """SELECT tools, COUNT(*) as cnt, SUM(cost_usd) as cost
           FROM events
           WHERE device_id = ? AND tools IS NOT NULL AND tools != ''
                 AND timestamp >= datetime('now', ?)
           GROUP BY tools
           ORDER BY cost DESC LIMIT 20""",
        (device_id, f"-{days} days"),
    ).fetchall()

    # Aggregate by individual tool
    tool_agg = {}
    for row in tool_rows:
        cost_per_event = row["cost"] / row["cnt"] if row["cnt"] > 0 else 0
        for tool in row["tools"].split(","):
            tool = tool.strip()
            if not tool:
                continue
            if tool not in tool_agg:
                tool_agg[tool] = {"count": 0, "cost": 0}
            tool_agg[tool]["count"] += row["cnt"]
            tool_agg[tool]["cost"] += row["cost"]

    for tool, data in sorted(tool_agg.items(), key=lambda x: x[1]["cost"], reverse=True):
        avg_cost = data["cost"] / data["count"] if data["count"] > 0 else 0
        if avg_cost > 0.10 and data["cost"] > 5.0:  # Expensive tool pattern
            suggestions.append({
                "type": "expensive_tool",
                "severity": "medium",
                "tool": tool,
                "total_cost": round(data["cost"], 2),
                "avg_cost_per_use": round(avg_cost, 4),
                "count": data["count"],
                "message": f"Tool '{tool}' costs ${avg_cost:.4f}/use avg (${data['cost']:.2f} total, "
                           f"{data['count']} uses). Consider if all uses need the current model tier.",
            })

    # 3. Session cost outliers — sessions that cost >5x the average
    avg_row = conn.execute(
        """SELECT AVG(session_cost) as avg_cost FROM (
             SELECT session_id, SUM(cost_usd) as session_cost
             FROM events
             WHERE device_id = ? AND session_id IS NOT NULL
                   AND timestamp >= datetime('now', ?)
             GROUP BY session_id
           )""",
        (device_id, f"-{days} days"),
    ).fetchone()

    avg_session_cost = avg_row["avg_cost"] or 0
    if avg_session_cost > 0:
        outlier_rows = conn.execute(
            """SELECT session_id, project, SUM(cost_usd) as cost, COUNT(*) as requests
               FROM events
               WHERE device_id = ? AND session_id IS NOT NULL
                     AND timestamp >= datetime('now', ?)
               GROUP BY session_id
               HAVING SUM(cost_usd) > ? * 5
               ORDER BY cost DESC LIMIT 5""",
            (device_id, f"-{days} days", avg_session_cost),
        ).fetchall()

        for row in outlier_rows:
            if row["cost"] > 2.0:  # Only flag if meaningful
                suggestions.append({
                    "type": "session_outlier",
                    "severity": "low",
                    "session_id": row["session_id"],
                    "project": row["project"],
                    "cost": round(row["cost"], 2),
                    "requests": row["requests"],
                    "avg_session_cost": round(avg_session_cost, 2),
                    "message": f"Session {row['session_id'][:12]}... cost ${row['cost']:.2f} "
                               f"({row['requests']} requests) — {row['cost']/avg_session_cost:.1f}x the average session cost.",
                })

    conn.close()

    # Sort by estimated impact
    def sort_key(s):
        if s["type"] == "model_downgrade":
            return s.get("estimated_savings", 0)
        return s.get("total_cost", s.get("cost", 0))

    suggestions.sort(key=sort_key, reverse=True)
    return suggestions


def get_community_stats(db_path: str | None = None) -> dict:
    """Get aggregate stats across all devices (for Pro tier benchmarks)."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)

    row = conn.execute(
        """SELECT COUNT(DISTINCT device_id) as devices,
                  COUNT(*) as total_events,
                  COALESCE(AVG(cost_usd), 0) as avg_cost_per_request
           FROM events WHERE timestamp >= datetime('now', '-7 days')"""
    ).fetchone()

    conn.close()
    return {
        "active_devices": row[0],
        "total_events_7d": row[1],
        "community_avg_cost_per_request": round(row[2], 6),
    }


# --- Alert functions ---

# Default thresholds (overridden by config.json and per-device alert_config)
_DEFAULT_ALERT_THRESHOLDS = {
    "daily_budget": 10.00,
    "session_spike": 5.00,
    "hourly_burn_rate": 3.00,
    "hourly_request_volume": 200,
    "hourly_token_volume": 2000000,
    "session_duration": 180,
}
_DEDUP_WINDOW_MINUTES = 60


def _has_recent_alert(conn, device_id: str, alert_type: str,
                      dedup_minutes: int, session_id: str | None = None) -> bool:
    """Check if an unacknowledged alert of this type exists within the dedup window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=dedup_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    if session_id and alert_type == "session_spike":
        row = conn.execute(
            """SELECT COUNT(*) FROM alerts
               WHERE device_id = ? AND alert_type = ? AND acknowledged = 0
                     AND created_at >= ?
                     AND json_extract(details, '$.session_id') = ?""",
            (device_id, alert_type, cutoff, session_id),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT COUNT(*) FROM alerts
               WHERE device_id = ? AND alert_type = ? AND acknowledged = 0
                     AND created_at >= ?""",
            (device_id, alert_type, cutoff),
        ).fetchone()
    return row[0] > 0


def create_alert(device_id: str, alert_type: str, severity: str,
                 message: str, details: dict | None = None,
                 db_path: str | None = None) -> int | None:
    """Insert an alert. Returns alert id, or None if deduped."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)

    session_id = details.get("session_id") if details else None
    if _has_recent_alert(conn, device_id, alert_type, _DEDUP_WINDOW_MINUTES, session_id):
        conn.close()
        return None

    details_json = json.dumps(details) if details else None
    cursor = conn.execute(
        """INSERT INTO alerts (device_id, alert_type, severity, message, details)
           VALUES (?, ?, ?, ?, ?)""",
        (device_id, alert_type, severity, message, details_json),
    )
    alert_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return alert_id


def get_alerts(device_id: str, acknowledged: bool | None = None,
               db_path: str | None = None) -> list[dict]:
    """List alerts for a device. Optionally filter by acknowledged status."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    if acknowledged is None:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE device_id = ? ORDER BY created_at DESC, id DESC",
            (device_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE device_id = ? AND acknowledged = ? ORDER BY created_at DESC, id DESC",
            (device_id, 1 if acknowledged else 0),
        ).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        if d.get("details"):
            try:
                d["details"] = json.loads(d["details"])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(d)

    conn.close()
    return result


def acknowledge_alert(alert_id: int, device_id: str,
                      db_path: str | None = None) -> bool:
    """Mark an alert as acknowledged. Validates device_id ownership. Returns success."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    cursor = conn.execute(
        "UPDATE alerts SET acknowledged = 1 WHERE id = ? AND device_id = ?",
        (alert_id, device_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_active_alert_count(device_id: str, db_path: str | None = None) -> int:
    """Count unacknowledged alerts for badge display."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE device_id = ? AND acknowledged = 0",
        (device_id,),
    ).fetchone()
    conn.close()
    return row[0]


def get_alert_config(device_id: str, db_path: str | None = None,
                     defaults: dict | None = None) -> dict:
    """Get per-device alert thresholds, merged over defaults from config."""
    thresholds = dict(_DEFAULT_ALERT_THRESHOLDS)
    if defaults:
        _CONFIG_KEY_MAP = {
            "daily_budget": "daily_budget_usd",
            "session_spike": "session_spike_usd",
            "hourly_burn_rate": "hourly_burn_rate_usd",
            "hourly_request_volume": "hourly_request_volume",
            "hourly_token_volume": "hourly_token_volume",
            "session_duration": "session_duration_minutes",
        }
        for alert_type, cfg_key in _CONFIG_KEY_MAP.items():
            if cfg_key in defaults:
                thresholds[alert_type] = defaults[cfg_key]

    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT alert_type, threshold, enabled FROM alert_config WHERE device_id = ?",
        (device_id,),
    ).fetchall()
    conn.close()

    result = {}
    for alert_type, default_val in thresholds.items():
        entry = {"threshold": default_val, "enabled": True}
        for r in rows:
            if r["alert_type"] == alert_type:
                entry["threshold"] = r["threshold"]
                entry["enabled"] = bool(r["enabled"])
                break
        result[alert_type] = entry
    return result


def set_alert_config(device_id: str, alert_type: str, threshold: float,
                     enabled: bool = True, db_path: str | None = None) -> None:
    """Set a per-device alert threshold."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO alert_config (device_id, alert_type, threshold, enabled)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(device_id, alert_type) DO UPDATE SET threshold = ?, enabled = ?""",
        (device_id, alert_type, threshold, 1 if enabled else 0, threshold, 1 if enabled else 0),
    )
    conn.commit()
    conn.close()


def _check_daily_budget(conn, device_id: str, threshold: float,
                        dedup_minutes: int) -> list[dict]:
    """Check if today's total cost exceeds the daily budget threshold."""
    alerts = []
    row = conn.execute(
        """SELECT COALESCE(SUM(cost_usd), 0) as today_cost
           FROM events
           WHERE device_id = ? AND date(timestamp) = date('now')""",
        (device_id,),
    ).fetchone()
    today_cost = row[0]

    if today_cost > threshold:
        ratio = today_cost / threshold
        severity = "critical" if ratio >= 1.5 else "warning"
        if not _has_recent_alert(conn, device_id, "daily_budget", dedup_minutes):
            alerts.append({
                "alert_type": "daily_budget",
                "severity": severity,
                "message": f"Daily spend ${today_cost:.2f} exceeds ${threshold:.2f} budget ({ratio:.1f}x)",
                "details": {"today_cost": round(today_cost, 4), "threshold": threshold, "ratio": round(ratio, 2)},
            })
    return alerts


def _check_session_spike(conn, device_id: str, threshold: float,
                         dedup_minutes: int) -> list[dict]:
    """Check if any session in the last hour exceeds the spike threshold."""
    alerts = []
    rows = conn.execute(
        """SELECT session_id, SUM(cost_usd) as session_cost
           FROM events
           WHERE device_id = ? AND session_id IS NOT NULL
                 AND timestamp >= datetime('now', '-1 hour')
           GROUP BY session_id
           HAVING SUM(cost_usd) > ?""",
        (device_id, threshold),
    ).fetchall()

    for row in rows:
        session_id = row[0]
        session_cost = row[1]
        ratio = session_cost / threshold
        severity = "critical" if ratio >= 2.0 else "warning"
        if not _has_recent_alert(conn, device_id, "session_spike", dedup_minutes, session_id):
            alerts.append({
                "alert_type": "session_spike",
                "severity": severity,
                "message": f"Session {session_id[:12]}... cost ${session_cost:.2f} in last hour (threshold ${threshold:.2f})",
                "details": {"session_id": session_id, "session_cost": round(session_cost, 4),
                            "threshold": threshold, "ratio": round(ratio, 2)},
            })
    return alerts


def _check_hourly_burn_rate(conn, device_id: str, threshold: float,
                            dedup_minutes: int) -> list[dict]:
    """Check if the last hour's total spend exceeds the burn rate threshold."""
    alerts = []
    row = conn.execute(
        """SELECT COALESCE(SUM(cost_usd), 0) as hourly_cost
           FROM events
           WHERE device_id = ? AND timestamp >= datetime('now', '-1 hour')""",
        (device_id,),
    ).fetchone()
    hourly_cost = row[0]

    if hourly_cost > threshold:
        ratio = hourly_cost / threshold
        severity = "critical" if ratio >= 2.0 else "warning"
        if not _has_recent_alert(conn, device_id, "hourly_burn_rate", dedup_minutes):
            alerts.append({
                "alert_type": "hourly_burn_rate",
                "severity": severity,
                "message": f"Last hour spend ${hourly_cost:.2f} exceeds ${threshold:.2f}/hr limit ({ratio:.1f}x)",
                "details": {"hourly_cost": round(hourly_cost, 4), "threshold": threshold, "ratio": round(ratio, 2)},
            })
    return alerts


def _check_hourly_request_volume(conn, device_id: str, threshold: float,
                                  dedup_minutes: int) -> list[dict]:
    """Check if requests in the last hour exceed the volume threshold."""
    alerts = []
    row = conn.execute(
        """SELECT COUNT(*) as request_count
           FROM events
           WHERE device_id = ? AND timestamp >= datetime('now', '-1 hour')""",
        (device_id,),
    ).fetchone()
    request_count = row[0]

    if request_count > threshold:
        ratio = request_count / threshold
        severity = "critical" if ratio >= 2.0 else "warning"
        if not _has_recent_alert(conn, device_id, "hourly_request_volume", dedup_minutes):
            alerts.append({
                "alert_type": "hourly_request_volume",
                "severity": severity,
                "message": f"Last hour: {request_count} requests exceeds {int(threshold)} limit ({ratio:.1f}x)",
                "details": {"request_count": request_count, "threshold": threshold, "ratio": round(ratio, 2)},
            })
    return alerts


def _check_hourly_token_volume(conn, device_id: str, threshold: float,
                                dedup_minutes: int) -> list[dict]:
    """Check if total tokens in the last hour exceed the volume threshold."""
    alerts = []
    row = conn.execute(
        """SELECT COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0) as total_tokens
           FROM events
           WHERE device_id = ? AND timestamp >= datetime('now', '-1 hour')""",
        (device_id,),
    ).fetchone()
    total_tokens = row[0]

    if total_tokens > threshold:
        ratio = total_tokens / threshold
        severity = "critical" if ratio >= 2.0 else "warning"
        if not _has_recent_alert(conn, device_id, "hourly_token_volume", dedup_minutes):
            alerts.append({
                "alert_type": "hourly_token_volume",
                "severity": severity,
                "message": f"Last hour: {total_tokens:,} tokens exceeds {int(threshold):,} limit ({ratio:.1f}x)",
                "details": {"total_tokens": total_tokens, "threshold": threshold, "ratio": round(ratio, 2)},
            })
    return alerts


def _check_session_duration(conn, device_id: str, threshold: float,
                             dedup_minutes: int) -> list[dict]:
    """Check if any active session exceeds the duration threshold (minutes)."""
    alerts = []
    rows = conn.execute(
        """SELECT session_id,
                  (julianday(MAX(timestamp)) - julianday(MIN(timestamp))) * 1440 as duration_minutes
           FROM events
           WHERE device_id = ? AND session_id IS NOT NULL
                 AND timestamp >= datetime('now', '-1 hour')
           GROUP BY session_id
           HAVING duration_minutes > ?""",
        (device_id, threshold),
    ).fetchall()

    for row in rows:
        session_id = row[0]
        duration = row[1]
        ratio = duration / threshold
        severity = "critical" if ratio >= 2.0 else "warning"
        if not _has_recent_alert(conn, device_id, "session_duration", dedup_minutes, session_id):
            alerts.append({
                "alert_type": "session_duration",
                "severity": severity,
                "message": f"Session {session_id[:12]}... running {duration:.0f}min exceeds {int(threshold)}min limit ({ratio:.1f}x)",
                "details": {"session_id": session_id, "duration_minutes": round(duration, 1),
                            "threshold": threshold, "ratio": round(ratio, 2)},
            })
    return alerts


def check_alerts(device_id: str, db_path: str | None = None,
                 config_defaults: dict | None = None) -> list[dict]:
    """Run all alert checks for a device. Called after ingest. Returns list of new alerts created."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)

    # Load per-device config merged with defaults
    conn.row_factory = sqlite3.Row
    config_rows = conn.execute(
        "SELECT alert_type, threshold, enabled FROM alert_config WHERE device_id = ?",
        (device_id,),
    ).fetchall()
    conn.row_factory = None

    thresholds = dict(_DEFAULT_ALERT_THRESHOLDS)
    if config_defaults:
        _CONFIG_KEY_MAP = {
            "daily_budget": "daily_budget_usd",
            "session_spike": "session_spike_usd",
            "hourly_burn_rate": "hourly_burn_rate_usd",
            "hourly_request_volume": "hourly_request_volume",
            "hourly_token_volume": "hourly_token_volume",
            "session_duration": "session_duration_minutes",
        }
        for alert_type, cfg_key in _CONFIG_KEY_MAP.items():
            if cfg_key in config_defaults:
                thresholds[alert_type] = config_defaults[cfg_key]

    enabled = {k: True for k in thresholds}
    for r in config_rows:
        at = r["alert_type"]
        if at in thresholds:
            thresholds[at] = r["threshold"]
            enabled[at] = bool(r["enabled"])

    dedup_minutes = _DEDUP_WINDOW_MINUTES
    if config_defaults and "dedup_window_minutes" in config_defaults:
        dedup_minutes = config_defaults["dedup_window_minutes"]

    pending = []
    if enabled.get("daily_budget", True):
        pending.extend(_check_daily_budget(conn, device_id, thresholds["daily_budget"], dedup_minutes))
    if enabled.get("session_spike", True):
        pending.extend(_check_session_spike(conn, device_id, thresholds["session_spike"], dedup_minutes))
    if enabled.get("hourly_burn_rate", True):
        pending.extend(_check_hourly_burn_rate(conn, device_id, thresholds["hourly_burn_rate"], dedup_minutes))
    if enabled.get("hourly_request_volume", True):
        pending.extend(_check_hourly_request_volume(conn, device_id, thresholds["hourly_request_volume"], dedup_minutes))
    if enabled.get("hourly_token_volume", True):
        pending.extend(_check_hourly_token_volume(conn, device_id, thresholds["hourly_token_volume"], dedup_minutes))
    if enabled.get("session_duration", True):
        pending.extend(_check_session_duration(conn, device_id, thresholds["session_duration"], dedup_minutes))

    conn.close()

    # Actually create alerts
    created = []
    for a in pending:
        alert_id = create_alert(
            device_id, a["alert_type"], a["severity"], a["message"],
            details=a.get("details"), db_path=db_path,
        )
        if alert_id is not None:
            a["id"] = alert_id
            created.append(a)

    return created


# --- Pricing config functions ---

def get_pricing_config(device_id: str, db_path: str | None = None) -> list[dict]:
    """Get all pricing overrides for a device."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT model, provider, input_price, output_price,
                  cache_read_price, cache_write_price
           FROM pricing_config WHERE device_id = ?""",
        (device_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_pricing_config(device_id: str, model: str,
                       input_price: float, output_price: float,
                       cache_read_price: float, cache_write_price: float,
                       provider: str = "", db_path: str | None = None) -> None:
    """Set (upsert) a pricing override for a device + model + provider."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO pricing_config
               (device_id, model, provider, input_price, output_price,
                cache_read_price, cache_write_price)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(device_id, model, provider) DO UPDATE SET
               input_price = ?, output_price = ?,
               cache_read_price = ?, cache_write_price = ?""",
        (device_id, model, provider, input_price, output_price,
         cache_read_price, cache_write_price,
         input_price, output_price, cache_read_price, cache_write_price),
    )
    conn.commit()
    conn.close()


def delete_pricing_config(device_id: str, model: str,
                          provider: str = "", db_path: str | None = None) -> bool:
    """Delete a pricing override. Returns True if a row was deleted."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    cursor = conn.execute(
        "DELETE FROM pricing_config WHERE device_id = ? AND model = ? AND provider = ?",
        (device_id, model, provider),
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_device_pricing_overrides(device_id: str, db_path: str | None = None,
                                 provider_defaults: dict | None = None) -> dict:
    """Build pricing overrides dict for compute_cost().

    Resolution: device-level overrides > config.json provider_defaults.
    The dict maps model name (or "*" for wildcard) to pricing dicts.
    """
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT model, provider, input_price, output_price,
                  cache_read_price, cache_write_price
           FROM pricing_config WHERE device_id = ?""",
        (device_id,),
    ).fetchall()
    conn.close()

    overrides = {}

    # Layer 1: config.json provider defaults as wildcards
    if provider_defaults:
        for provider_name, prices in provider_defaults.items():
            # These get lower priority — device overrides will overwrite
            overrides[f"__provider__{provider_name}"] = {
                "input": prices.get("input", 0),
                "output": prices.get("output", 0),
                "cache_read": prices.get("cache_read", 0),
                "cache_write": prices.get("cache_write", 0),
                "_provider": provider_name,
            }

    # Layer 2: device-level overrides (higher priority)
    for r in rows:
        pricing = {
            "input": r["input_price"],
            "output": r["output_price"],
            "cache_read": r["cache_read_price"],
            "cache_write": r["cache_write_price"],
        }
        if r["model"] == "*" and r["provider"]:
            # Provider wildcard — key as "*" only if it's the sole wildcard,
            # otherwise we need provider-aware resolution
            overrides[f"__provider__{r['provider']}"] = {**pricing, "_provider": r["provider"]}
        else:
            overrides[r["model"]] = pricing

    return overrides


def resolve_pricing(device_id: str, model: str, provider: str,
                    db_path: str | None = None,
                    provider_defaults: dict | None = None) -> dict:
    """Resolve effective pricing for a specific model+provider.

    Resolution order:
    1. Exact device model override
    2. Device provider wildcard (model="*", provider=X)
    3. Config.json provider defaults
    4. Global MODEL_PRICING
    5. DEFAULT_PRICING (Sonnet fallback)
    """
    overrides = get_device_pricing_overrides(device_id, db_path, provider_defaults)

    # Exact model match
    if model in overrides:
        p = overrides[model]
        return {k: v for k, v in p.items() if not k.startswith("_")}

    # Provider wildcard
    provider_key = f"__provider__{provider}"
    if provider_key in overrides:
        p = overrides[provider_key]
        return {k: v for k, v in p.items() if not k.startswith("_")}

    # Global defaults
    return MODEL_PRICING.get(model, DEFAULT_PRICING)


def get_device_models(device_id: str, db_path: str | None = None) -> list[dict]:
    """Get distinct model+provider combos for a device with event counts."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT model, provider, COUNT(*) as count
           FROM events
           WHERE device_id = ? AND model IS NOT NULL
           GROUP BY model, provider
           ORDER BY count DESC""",
        (device_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def recalculate_device_costs(device_id: str, db_path: str | None = None,
                             provider_defaults: dict | None = None) -> int:
    """Recompute cost_usd for all events using current pricing config.

    Returns count of events updated.
    """
    overrides_raw = get_device_pricing_overrides(device_id, db_path, provider_defaults)

    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")

    rows = conn.execute(
        """SELECT id, model, provider, input_tokens, output_tokens,
                  cache_read_tokens, cache_write_tokens
           FROM events WHERE device_id = ?""",
        (device_id,),
    ).fetchall()

    if not rows:
        conn.close()
        return 0

    updated = 0
    batch = []
    for row in rows:
        event_id, model, provider, in_t, out_t, cr_t, cw_t = row

        # Build per-event overrides: check exact model, then provider wildcard
        effective_overrides = {}
        if model in overrides_raw:
            effective_overrides[model] = overrides_raw[model]
        else:
            provider_key = f"__provider__{provider}"
            if provider_key in overrides_raw:
                p = overrides_raw[provider_key]
                effective_overrides["*"] = {k: v for k, v in p.items() if not k.startswith("_")}

        total, _ = compute_cost(
            model or "unknown", in_t or 0, out_t or 0, cr_t or 0, cw_t or 0,
            pricing_overrides=effective_overrides or None,
        )
        batch.append((total, event_id))
        updated += 1

        if len(batch) >= 1000:
            conn.executemany("UPDATE events SET cost_usd = ? WHERE id = ?", batch)
            batch = []

    if batch:
        conn.executemany("UPDATE events SET cost_usd = ? WHERE id = ?", batch)

    conn.commit()
    conn.close()
    return updated


# --- Auth functions ---

def _hash_secret(secret: str) -> str:
    """SHA-256 hash a device secret."""
    return hashlib.sha256(secret.encode()).hexdigest()


def register_device(db_path: str | None = None) -> tuple[str, str]:
    """Register a new device. Returns (device_id, device_secret)."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    device_id = secrets.token_hex(16)
    device_secret = secrets.token_hex(32)
    secret_hash = _hash_secret(device_secret)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO devices (device_id, created_at, last_seen, device_secret_hash)
           VALUES (?, ?, ?, ?)""",
        (device_id, now, now, secret_hash),
    )
    conn.commit()
    conn.close()
    return device_id, device_secret


def claim_device(device_id: str, db_path: str | None = None) -> str | None:
    """Set a secret on an existing device that has no secret (migration).

    Returns the new device_secret, or None if device doesn't exist or already has a secret.
    """
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT device_secret_hash FROM devices WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    if row[0] is not None:
        conn.close()
        return None  # Already has a secret

    device_secret = secrets.token_hex(32)
    secret_hash = _hash_secret(device_secret)
    conn.execute(
        "UPDATE devices SET device_secret_hash = ? WHERE device_id = ?",
        (secret_hash, device_id),
    )
    conn.commit()
    conn.close()
    return device_secret


def verify_device_secret(device_id: str, secret: str,
                         db_path: str | None = None) -> bool:
    """Verify a device secret against stored hash."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT device_secret_hash FROM devices WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    conn.close()
    if row is None or row[0] is None:
        return False
    return secrets.compare_digest(_hash_secret(secret), row[0])


def get_device(device_id: str, db_path: str | None = None) -> dict | None:
    """Get full device record."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM devices WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def get_device_project_count(device_id: str, db_path: str | None = None) -> int:
    """Count distinct projects for a device."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT COUNT(DISTINCT project) FROM events WHERE device_id = ? AND project IS NOT NULL",
        (device_id,),
    ).fetchone()
    conn.close()
    return row[0]


def get_device_first_project(device_id: str, db_path: str | None = None) -> str | None:
    """Get the first project seen for a device (by earliest timestamp)."""
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path)
    row = conn.execute(
        """SELECT project FROM events
           WHERE device_id = ? AND project IS NOT NULL
           ORDER BY timestamp ASC LIMIT 1""",
        (device_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None
