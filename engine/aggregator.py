"""Aggregate cost and token data from parsed session messages."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .parser import MessageUsage


def _parse_ts(ts: str) -> datetime:
    """Parse ISO timestamp to datetime."""
    # Handle both Z suffix and +00:00
    ts = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _date_key(ts: str) -> str:
    """Extract YYYY-MM-DD from timestamp."""
    return _parse_ts(ts).strftime("%Y-%m-%d")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_summary(messages: list[MessageUsage]) -> dict:
    """Today's total cost, token count, session count."""
    today = _today()
    today_msgs = [m for m in messages if _date_key(m.timestamp) == today]

    total_cost = sum(m.cost_total for m in today_msgs)
    total_tokens = sum(m.input_tokens + m.output_tokens + m.cache_read_tokens + m.cache_write_tokens for m in today_msgs)
    sessions = len(set(m.session_id for m in today_msgs))
    message_count = len(today_msgs)

    return {
        "date": today,
        "total_cost_usd": round(total_cost, 4),
        "total_tokens": total_tokens,
        "session_count": sessions,
        "message_count": message_count,
    }


def get_cost_timeseries(messages: list[MessageUsage], days: int = 7) -> list[dict]:
    """Daily costs for the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    daily = defaultdict(float)

    for m in messages:
        dt = _parse_ts(m.timestamp)
        if dt >= cutoff:
            day = dt.strftime("%Y-%m-%d")
            daily[day] += m.cost_total

    # Fill in missing days with 0
    result = []
    for i in range(days):
        day = (datetime.now(timezone.utc) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        result.append({"date": day, "cost_usd": round(daily.get(day, 0), 4)})

    return result


def get_model_breakdown(messages: list[MessageUsage]) -> list[dict]:
    """Cost per model."""
    by_model = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "count": 0})

    for m in messages:
        entry = by_model[m.model]
        entry["cost"] += m.cost_total
        entry["tokens"] += m.input_tokens + m.output_tokens + m.cache_read_tokens + m.cache_write_tokens
        entry["count"] += 1

    result = []
    for model, data in sorted(by_model.items(), key=lambda x: x[1]["cost"], reverse=True):
        result.append({
            "model": model,
            "cost_usd": round(data["cost"], 4),
            "total_tokens": data["tokens"],
            "message_count": data["count"],
        })

    return result


def get_project_breakdown(messages: list[MessageUsage]) -> list[dict]:
    """Cost per project (derived from path)."""
    by_project = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "sessions": set(), "count": 0})

    for m in messages:
        entry = by_project[m.project_name]
        entry["cost"] += m.cost_total
        entry["tokens"] += m.input_tokens + m.output_tokens + m.cache_read_tokens + m.cache_write_tokens
        entry["sessions"].add(m.session_id)
        entry["count"] += 1

    result = []
    for project, data in sorted(by_project.items(), key=lambda x: x[1]["cost"], reverse=True):
        result.append({
            "project": project,
            "cost_usd": round(data["cost"], 4),
            "total_tokens": data["tokens"],
            "session_count": len(data["sessions"]),
            "message_count": data["count"],
        })

    return result


def get_top_sessions(messages: list[MessageUsage], n: int = 5) -> list[dict]:
    """Most expensive sessions."""
    by_session = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "count": 0, "project": "", "file": ""})

    for m in messages:
        entry = by_session[m.session_id]
        entry["cost"] += m.cost_total
        entry["tokens"] += m.input_tokens + m.output_tokens + m.cache_read_tokens + m.cache_write_tokens
        entry["count"] += 1
        entry["project"] = m.project_name
        entry["file"] = m.session_file

    result = []
    for session_id, data in sorted(by_session.items(), key=lambda x: x[1]["cost"], reverse=True)[:n]:
        result.append({
            "session_id": session_id,
            "project": data["project"],
            "cost_usd": round(data["cost"], 4),
            "total_tokens": data["tokens"],
            "message_count": data["count"],
        })

    return result


def aggregate_costs(messages: list[MessageUsage], group_by: str = "day") -> dict:
    """Group costs by day, model, or project."""
    if group_by == "day":
        return {"timeseries": get_cost_timeseries(messages, days=30)}
    elif group_by == "model":
        return {"models": get_model_breakdown(messages)}
    elif group_by == "project":
        return {"projects": get_project_breakdown(messages)}
    else:
        return {"error": f"Unknown group_by: {group_by}"}
