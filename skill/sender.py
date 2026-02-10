#!/usr/bin/env python3
"""ClawTrace Sender - Sync OpenClaw usage data to ClawTrace.

Standalone script that:
- Generates and persists a device_id
- Finds JSONL session files from Claude Code and OpenClaw
- Parses usage data from assistant messages
- POSTs events to ClawTrace API
- Tracks sent events to avoid duplicates
"""

import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

# ClawTrace API endpoints
API_BASE = "https://clawtrace.vybng.co"
API_URL = f"{API_BASE}/api/ingest"
API_REGISTER = f"{API_BASE}/api/register"
API_CLAIM = f"{API_BASE}/api/claim"

# Local copy of pricing — server recomputes authoritatively via engine/pricing.py
# Pricing per million tokens (as of 2026-02)
MODEL_PRICING = {
    "claude-opus-4-6": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.5,
        "cache_write": 18.75,
    },
    "claude-opus-4-5-20251101": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.5,
        "cache_write": 18.75,
    },
    "claude-sonnet-4-5-20250929": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-haiku-4-5-20251001": {
        "input": 0.80,
        "output": 4.0,
        "cache_read": 0.08,
        "cache_write": 1.0,
    },
}

# Fallback pricing for unknown models — use Sonnet pricing
DEFAULT_PRICING = {
    "input": 3.0,
    "output": 15.0,
    "cache_read": 0.30,
    "cache_write": 3.75,
}


@dataclass
class UsageEvent:
    """Single usage event to send to ClawTrace."""
    timestamp: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_total: float
    project_name: str
    session_id: str
    tools: str = ""  # comma-separated normalized tool names


def _normalize_tool_name(tool_name: str) -> str:
    """Normalize tool names, grouping MCP tools by prefix."""
    if not tool_name.startswith("mcp__"):
        return tool_name
    if tool_name.startswith("mcp__claude-in-chrome__"):
        return "chrome-browser"
    if tool_name.startswith("mcp__plugin_"):
        remainder = tool_name[len("mcp__plugin_"):]
        parts = remainder.split("_")
        if parts:
            return parts[0]
    parts = tool_name.split("__")
    if len(parts) >= 2:
        return parts[1]
    return tool_name


def _extract_tools(message: dict) -> str:
    """Extract comma-separated normalized tool names from message content."""
    content = message.get("content", [])
    if not isinstance(content, list):
        return ""
    tool_names = set()
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            name = item.get("name")
            if name:
                tool_names.add(_normalize_tool_name(name))
    return ",".join(sorted(tool_names))


def get_or_register_device() -> tuple[str, str]:
    """Get or register device, returning (device_id, device_secret).

    On first run: registers with the API and stores both values.
    On existing device.json missing device_secret: claims a secret via /api/claim.
    """
    config_dir = Path.home() / ".clawtrace"
    config_file = config_dir / "device.json"

    if config_file.exists():
        try:
            with open(config_file, "r") as f:
                data = json.load(f)
                device_id = data["device_id"]
                device_secret = data.get("device_secret")
                if device_secret:
                    return device_id, device_secret
                # Migration: existing device without secret — claim one
                device_secret = _claim_device_secret(device_id)
                if device_secret:
                    data["device_secret"] = device_secret
                    with open(config_file, "w") as fw:
                        json.dump(data, fw, indent=2)
                    return device_id, device_secret
                # Claim failed (device not on server yet) — re-register
        except (OSError, KeyError, json.JSONDecodeError):
            pass

    # Register new device with the API
    device_id, device_secret = _register_device()
    config_dir.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w") as f:
        json.dump({"device_id": device_id, "device_secret": device_secret}, f, indent=2)

    return device_id, device_secret


def _register_device() -> tuple[str, str]:
    """Register a new device with the ClawTrace API."""
    try:
        req = urllib.request.Request(
            API_REGISTER,
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read())
            return data["device_id"], data["device_secret"]
    except Exception as err:
        # Fallback: generate locally (will need to claim later)
        print(f"Warning: could not register with API ({err}), generating local ID", file=sys.stderr)
        return uuid4().hex, ""


def _claim_device_secret(device_id: str) -> str | None:
    """Claim a secret for an existing device (migration from pre-auth)."""
    try:
        req = urllib.request.Request(
            API_CLAIM,
            data=json.dumps({"device_id": device_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read())
            return data.get("device_secret")
    except Exception:
        return None


def get_device_id() -> str:
    """Backward-compatible wrapper that returns just device_id."""
    device_id, _ = get_or_register_device()
    return device_id


def compute_cost(model: str, input_t: int, output_t: int, cache_read_t: int, cache_write_t: int) -> float:
    """Compute cost from token counts and model pricing."""
    pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
    total = (
        input_t * pricing["input"] / 1_000_000 +
        output_t * pricing["output"] / 1_000_000 +
        cache_read_t * pricing["cache_read"] / 1_000_000 +
        cache_write_t * pricing["cache_write"] / 1_000_000
    )
    return round(total, 6)


def extract_project_name(path: str) -> str:
    """Extract project name from session file path."""
    p = str(path)

    # Claude Code projects
    if "/.claude/projects/" in p:
        parts = p.split("/.claude/projects/")
        if len(parts) > 1:
            project_dir = parts[1].split("/")[0]
            # Handle edge cases: "-" or short paths
            if project_dir == "-" or len(project_dir) < 3:
                return "claude-default"
            segments = project_dir.split("-")
            # Filter out empty and path-like segments (Users, home, usernames)
            meaningful = [
                s for s in segments
                if s and s not in ["Users", "home", "root", "stevemallett", "claude"] and len(s) > 2
            ]
            if meaningful:
                return meaningful[-1]
            return "claude-default"
        return "unknown"

    # OpenClaw agents
    if "/.openclaw/agents/" in p:
        parts = p.split("/.openclaw/agents/")
        if len(parts) > 1:
            agent_name = parts[1].split("/")[0]
            return f"openclaw-{agent_name}"
        return "openclaw"

    return "unknown"


def _extract_usage(record: dict) -> tuple | None:
    """Extract usage data from a record, handling both JSONL formats.

    Format 1 (Claude Code): type="assistant", usage keys: input_tokens, output_tokens, etc.
    Format 2 (OpenClaw):    type="message", message.role="assistant", usage keys: input, output, cacheRead, cacheWrite

    Returns (model, provider, timestamp, input_t, output_t, cache_read_t, cache_write_t, cost_total, tools) or None.
    """
    message = record.get("message", {})
    if not isinstance(message, dict):
        return None

    usage = message.get("usage")
    if not usage or not isinstance(usage, dict):
        return None

    record_type = record.get("type", "")
    timestamp = record.get("timestamp", "")

    # Format 1: OpenClaw agent format
    # type: "message", message.role: "assistant", usage keys: input, output, cacheRead, cacheWrite
    if record_type == "message" and message.get("role") == "assistant":
        model = message.get("model", "unknown")
        provider = message.get("provider", "unknown")
        input_t = usage.get("input", 0)
        output_t = usage.get("output", 0)
        cache_read_t = usage.get("cacheRead", 0)
        cache_write_t = usage.get("cacheWrite", 0)

        # OpenClaw may have pre-computed cost
        cost_obj = usage.get("cost")
        if cost_obj and isinstance(cost_obj, dict) and cost_obj.get("total", 0) > 0:
            cost_total = cost_obj["total"]
        else:
            cost_total = compute_cost(model, input_t, output_t, cache_read_t, cache_write_t)

        tools = _extract_tools(message)
        return (model, provider, timestamp, input_t, output_t, cache_read_t, cache_write_t, cost_total, tools)

    # Format 2: Claude Code format
    # type: "assistant", usage keys: input_tokens, output_tokens, etc.
    if record_type == "assistant":
        model = message.get("model", "unknown")
        provider = record.get("provider", "anthropic")
        input_t = usage.get("input_tokens", 0)
        output_t = usage.get("output_tokens", 0)
        cache_read_t = usage.get("cache_read_input_tokens", 0)
        cache_write_t = usage.get("cache_creation_input_tokens", 0)

        cost_total = compute_cost(model, input_t, output_t, cache_read_t, cache_write_t)
        tools = _extract_tools(message)
        return (model, provider, timestamp, input_t, output_t, cache_read_t, cache_write_t, cost_total, tools)

    return None


def parse_session_file(path: Path) -> list[UsageEvent]:
    """Parse one JSONL file and extract assistant messages with usage."""
    events = []
    session_id = path.stem
    project_name = extract_project_name(str(path))

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                result = _extract_usage(record)
                if result is None:
                    continue

                model, provider, timestamp, input_t, output_t, cache_read_t, cache_write_t, cost_total, tools = result

                events.append(UsageEvent(
                    timestamp=timestamp,
                    model=model,
                    provider=provider,
                    input_tokens=input_t,
                    output_tokens=output_t,
                    cache_read_tokens=cache_read_t,
                    cache_write_tokens=cache_write_t,
                    cost_total=cost_total,
                    project_name=project_name,
                    session_id=session_id,
                    tools=tools,
                ))
    except (OSError, IOError):
        pass

    return events


def find_session_files() -> list[Path]:
    """Find all JSONL session files from Claude Code and OpenClaw."""
    files = []
    search_paths = [
        Path.home() / ".claude" / "projects",
        Path.home() / ".openclaw" / "agents",
    ]

    for search_path in search_paths:
        if not search_path.exists():
            continue
        for jsonl_file in search_path.rglob("*.jsonl"):
            files.append(jsonl_file)

    return sorted(files)


def load_cursor() -> dict:
    """Load sent cursor from ~/.clawtrace/sent_cursor.json."""
    cursor_file = Path.home() / ".clawtrace" / "sent_cursor.json"
    if cursor_file.exists():
        try:
            with open(cursor_file, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_cursor(cursor: dict):
    """Save sent cursor to ~/.clawtrace/sent_cursor.json."""
    cursor_file = Path.home() / ".clawtrace" / "sent_cursor.json"
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cursor_file, "w") as f:
        json.dump(cursor, f, indent=2)


def send_events(device_id: str, device_secret: str, events: list[UsageEvent]) -> bool:
    """POST events to ClawTrace API in batches of 500."""
    if not events:
        return True

    BATCH_SIZE = 500
    total_sent = 0

    for i in range(0, len(events), BATCH_SIZE):
        batch = events[i:i + BATCH_SIZE]
        payload = {
            "device_id": device_id,
            "events": [
                {
                    "timestamp": e.timestamp,
                    "model": e.model,
                    "provider": e.provider,
                    "event_type": "llm.usage",
                    "input_tokens": e.input_tokens,
                    "output_tokens": e.output_tokens,
                    "cache_read_tokens": e.cache_read_tokens,
                    "cache_write_tokens": e.cache_write_tokens,
                    "cost_usd": e.cost_total,
                    "project": e.project_name,
                    "session_id": e.session_id,
                    "tools": e.tools,
                }
                for e in batch
            ],
        }

        headers = {"Content-Type": "application/json"}
        if device_secret:
            headers["Authorization"] = f"Bearer {device_secret}"

        try:
            req = urllib.request.Request(
                API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status != 200:
                    print(f"Batch {i // BATCH_SIZE + 1} failed: HTTP {response.status}", file=sys.stderr)
                    return False
            total_sent += len(batch)
            if len(events) > BATCH_SIZE:
                print(f"  Batch {i // BATCH_SIZE + 1}: sent {len(batch)} events ({total_sent}/{len(events)})")
        except Exception as err:
            print(f"Error sending batch {i // BATCH_SIZE + 1}: {err}", file=sys.stderr)
            return False

    return True


def sync():
    """Main sync logic: find new events, send them, update cursor."""
    device_id, device_secret = get_or_register_device()
    cursor = load_cursor()
    session_files = find_session_files()

    new_events = []
    updated_cursor = cursor.copy()

    for session_file in session_files:
        file_key = str(session_file)
        last_sent_ts = cursor.get(file_key, "")

        events = parse_session_file(session_file)
        if not events:
            continue

        # Filter to new events only
        new = [e for e in events if e.timestamp > last_sent_ts]
        new_events.extend(new)

        # Update cursor with latest timestamp from this file
        if events:
            latest_ts = max(e.timestamp for e in events)
            updated_cursor[file_key] = latest_ts

    dashboard_url = f"https://clawtrace.vybng.co/d/{device_id}"
    if device_secret:
        dashboard_url += f"#{device_secret}"

    if new_events:
        success = send_events(device_id, device_secret, new_events)
        if success:
            save_cursor(updated_cursor)
            print(f"Synced {len(new_events)} events to ClawTrace.")
            print(f"Dashboard: {dashboard_url}")
        else:
            print("Failed to sync events.", file=sys.stderr)
            sys.exit(1)
    else:
        print("No new events to sync.")
        print(f"Dashboard: {dashboard_url}")


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "--device-id":
            device_id, _ = get_or_register_device()
            print(device_id)
            return
        elif sys.argv[1] == "--dashboard":
            device_id, device_secret = get_or_register_device()
            url = f"https://clawtrace.vybng.co/d/{device_id}"
            if device_secret:
                url += f"#{device_secret}"
            print(url)
            return
        elif sys.argv[1] == "--resync":
            # Clear cursor and VPS data, re-send everything with tools
            device_id, device_secret = get_or_register_device()
            print(f"Clearing VPS events for device {device_id}...")
            headers = {"Content-Type": "application/json"}
            if device_secret:
                headers["Authorization"] = f"Bearer {device_secret}"
            try:
                req = urllib.request.Request(
                    f"{API_BASE}/api/resync/{device_id}",
                    data=b"{}",
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
                    print(f"Deleted {result.get('deleted', 0)} old events.")
            except Exception as err:
                print(f"Warning: could not clear VPS data: {err}", file=sys.stderr)
            print("Clearing local cursor...")
            cursor_file = Path.home() / ".clawtrace" / "sent_cursor.json"
            if cursor_file.exists():
                cursor_file.unlink()
            sync()
            return

    sync()


if __name__ == "__main__":
    main()
