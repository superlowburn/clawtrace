"""Parse JSONL session logs from Claude Code and OpenClaw agents."""

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .pricing import compute_cost as _compute_cost_fn


@dataclass
class MessageUsage:
    timestamp: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_total: float
    cost_breakdown: dict
    session_id: str
    session_file: str
    project_name: str
    tools: list[str] = field(default_factory=list)


def _compute_cost(model: str, input_t: int, output_t: int, cache_read_t: int, cache_write_t: int) -> tuple[float, dict]:
    """Compute cost from token counts and model pricing."""
    return _compute_cost_fn(model, input_t, output_t, cache_read_t, cache_write_t)


def _normalize_tool_name(tool_name: str) -> str:
    """Normalize tool names, grouping MCP tools by prefix.

    Examples:
        mcp__claude-in-chrome__computer -> chrome-browser
        mcp__plugin_playwright_browser__snapshot -> playwright
        mcp__plugin_pinecone_pinecone__search-docs -> pinecone
        Bash -> Bash
        Read -> Read
    """
    if not tool_name.startswith("mcp__"):
        return tool_name

    # Special case: chrome browser
    if tool_name.startswith("mcp__claude-in-chrome__"):
        return "chrome-browser"

    # Plugin pattern: mcp__plugin_{name}_{module}__{method}
    # Example: mcp__plugin_playwright_browser__snapshot
    # Split on underscores and find the plugin name after "plugin"
    if tool_name.startswith("mcp__plugin_"):
        # Remove "mcp__plugin_" prefix
        remainder = tool_name[len("mcp__plugin_"):]
        # Split on underscore to get plugin name (first segment)
        parts = remainder.split("_")
        if parts:
            return parts[0]

    # Generic MCP pattern: mcp__{segment}__{...}
    # Extract second segment
    parts = tool_name.split("__")
    if len(parts) >= 2:
        return parts[1]

    return tool_name


def _extract_tools(message: dict) -> list[str]:
    """Extract tool names from message content array.

    Returns deduplicated list of normalized tool names.
    """
    content = message.get("content", [])
    if not isinstance(content, list):
        return []

    tool_names = set()
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            name = item.get("name")
            if name:
                tool_names.add(_normalize_tool_name(name))

    return sorted(tool_names)


def _extract_project_name(path: str) -> str:
    """Extract project name from session file path.

    Examples:
        ~/.claude/projects/-Users-stevemallett-claude-threadjack/abc.jsonl -> threadjack
        ~/.claude/projects/-Users-stevemallett-claude-vt2/sub/agent.jsonl -> vt2
        ~/.openclaw/agents/main/sessions/abc.jsonl -> openclaw-main
    """
    p = str(path)

    # Claude Code projects: extract last segment of the project dir name
    if "/.claude/projects/" in p:
        # Path like: .../.claude/projects/-Users-stevemallett-claude-threadjack/session.jsonl
        parts = p.split("/.claude/projects/")
        if len(parts) > 1:
            project_dir = parts[1].split("/")[0]
            # project_dir is like "-Users-stevemallett-claude-threadjack"
            # Take the last hyphen-separated segment
            segments = project_dir.split("-")
            # Find meaningful name — skip empty and path components
            # The pattern is -Users-username-path-to-project
            # We want the last meaningful segment
            if segments:
                return segments[-1] if segments[-1] else project_dir
        return "unknown"

    # OpenClaw agents
    if "/.openclaw/agents/" in p:
        parts = p.split("/.openclaw/agents/")
        if len(parts) > 1:
            agent_name = parts[1].split("/")[0]
            return f"openclaw-{agent_name}"
        return "openclaw"

    return "unknown"


def parse_session_file(path: str | Path) -> list[MessageUsage]:
    """Parse one JSONL file and extract all assistant messages with usage data."""
    path = Path(path)
    results = []
    session_id = path.stem  # filename without extension
    project_name = _extract_project_name(str(path))

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    print(f"Warning: malformed JSON at {path}:{line_num}", file=sys.stderr)
                    continue

                usage_data = _extract_usage(record)
                if usage_data is None:
                    continue

                model, provider, timestamp, input_t, output_t, cache_read_t, cache_write_t, cost_total, cost_breakdown = usage_data
                tools = _extract_tools(record.get("message", {}))

                results.append(MessageUsage(
                    timestamp=timestamp,
                    model=model,
                    provider=provider,
                    input_tokens=input_t,
                    output_tokens=output_t,
                    cache_read_tokens=cache_read_t,
                    cache_write_tokens=cache_write_t,
                    cost_total=cost_total,
                    cost_breakdown=cost_breakdown,
                    session_id=session_id,
                    session_file=str(path),
                    project_name=project_name,
                    tools=tools,
                ))
    except (OSError, IOError) as e:
        print(f"Warning: cannot read {path}: {e}", file=sys.stderr)

    return results


def _extract_usage(record: dict) -> Optional[tuple]:
    """Extract usage data from a JSONL record, handling both formats.

    Returns (model, provider, timestamp, input_t, output_t, cache_read_t, cache_write_t, cost_total, cost_breakdown)
    or None if no usage data found.

    Note: Tool extraction is handled separately via _extract_tools().
    """
    message = record.get("message", {})
    if not isinstance(message, dict):
        return None

    usage = message.get("usage")
    if not usage or not isinstance(usage, dict):
        return None

    timestamp = record.get("timestamp", "")
    record_type = record.get("type", "")

    # Format 1: OpenClaw agent format
    # type: "message", message.role: "assistant", message.usage with short names
    if record_type == "message" and message.get("role") == "assistant":
        model = message.get("model", "unknown")
        provider = message.get("provider", "unknown")
        input_t = usage.get("input", 0)
        output_t = usage.get("output", 0)
        cache_read_t = usage.get("cacheRead", 0)
        cache_write_t = usage.get("cacheWrite", 0)

        cost_obj = usage.get("cost")
        if cost_obj and isinstance(cost_obj, dict):
            cost_total = cost_obj.get("total", 0)
            cost_breakdown = {
                "input": cost_obj.get("input", 0),
                "output": cost_obj.get("output", 0),
                "cache_read": cost_obj.get("cacheRead", 0),
                "cache_write": cost_obj.get("cacheWrite", 0),
            }
        else:
            cost_total, cost_breakdown = _compute_cost(model, input_t, output_t, cache_read_t, cache_write_t)

        return (model, provider, timestamp, input_t, output_t, cache_read_t, cache_write_t, cost_total, cost_breakdown)

    # Format 2: Claude Code format
    # type: "assistant", message.usage with API names (input_tokens, etc.)
    if record_type == "assistant":
        model = message.get("model", "unknown")
        # Claude Code format doesn't have provider at message level — check record
        provider = record.get("provider", "anthropic")
        input_t = usage.get("input_tokens", 0)
        output_t = usage.get("output_tokens", 0)
        cache_read_t = usage.get("cache_read_input_tokens", 0)
        cache_write_t = usage.get("cache_creation_input_tokens", 0)

        # No pre-computed cost — compute from tokens
        cost_total, cost_breakdown = _compute_cost(model, input_t, output_t, cache_read_t, cache_write_t)

        return (model, provider, timestamp, input_t, output_t, cache_read_t, cache_write_t, cost_total, cost_breakdown)

    return None


def find_session_files(data_paths: list[str]) -> list[Path]:
    """Find all .jsonl session files in the given directories."""
    files = []
    for data_path in data_paths:
        expanded = Path(os.path.expanduser(data_path))
        if not expanded.exists():
            continue
        for jsonl_file in expanded.rglob("*.jsonl"):
            files.append(jsonl_file)
    return sorted(files)
