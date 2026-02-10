"""Tests for ClawTrace sender — covers both Claude Code and OpenClaw JSONL formats."""

import json
import tempfile
from pathlib import Path

import pytest

from sender import (
    UsageEvent,
    _extract_tools,
    _extract_usage,
    _normalize_tool_name,
    compute_cost,
    extract_project_name,
    parse_session_file,
)


# ---------------------------------------------------------------------------
# Fixtures: realistic JSONL records
# ---------------------------------------------------------------------------

def _claude_code_record(model="claude-sonnet-4-5-20250929", input_t=1000, output_t=500,
                        cache_read=200, cache_write=100, provider="anthropic",
                        timestamp="2026-02-09T10:00:00Z", tools=None):
    """Build a Claude Code format record (type=assistant, input_tokens style)."""
    content = []
    if tools:
        for t in tools:
            content.append({"type": "tool_use", "name": t, "id": "x", "input": {}})
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "provider": provider,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
            "content": content,
        },
    }


def _openclaw_record(model="kimi-k2.5", input_t=800, output_t=300,
                     cache_read=0, cache_write=0, provider="moonshot",
                     timestamp="2026-02-09T11:00:00Z", cost_total=0.0):
    """Build an OpenClaw format record (type=message, role=assistant, short usage keys)."""
    return {
        "type": "message",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "model": model,
            "provider": provider,
            "usage": {
                "input": input_t,
                "output": output_t,
                "cacheRead": cache_read,
                "cacheWrite": cache_write,
                "cost": {"total": cost_total},
            },
            "content": [],
        },
    }


def _write_jsonl(records: list[dict]) -> Path:
    """Write records to a temp JSONL file and return its Path."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    for r in records:
        tmp.write(json.dumps(r) + "\n")
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# extract_project_name
# ---------------------------------------------------------------------------

class TestExtractProjectName:
    def test_claude_code_mac_path(self):
        path = "/Users/stevemallett/.claude/projects/-Users-stevemallett-claude-clawtrace/abc.jsonl"
        assert extract_project_name(path) == "clawtrace"

    def test_claude_code_vps_root(self):
        path = "/root/.claude/projects/-root-claude/session.jsonl"
        assert extract_project_name(path) == "claude-default"

    def test_claude_code_vps_project(self):
        path = "/root/.claude/projects/-root-claude-whiteboard-generator/session.jsonl"
        assert extract_project_name(path) == "generator"

    def test_openclaw_agent(self):
        path = "/root/.openclaw/agents/main/sessions/abc.jsonl"
        assert extract_project_name(path) == "openclaw-main"

    def test_openclaw_custom_agent(self):
        path = "/root/.openclaw/agents/twitter-campaign/sessions/abc.jsonl"
        assert extract_project_name(path) == "openclaw-twitter-campaign"

    def test_unknown_path(self):
        assert extract_project_name("/tmp/random.jsonl") == "unknown"


# ---------------------------------------------------------------------------
# _extract_usage — dual format
# ---------------------------------------------------------------------------

class TestExtractUsage:
    def test_claude_code_format(self):
        record = _claude_code_record(input_t=1000, output_t=500, cache_read=200, cache_write=100)
        result = _extract_usage(record)
        assert result is not None
        model, provider, ts, inp, out, cr, cw, cost, tools = result
        assert model == "claude-sonnet-4-5-20250929"
        assert provider == "anthropic"
        assert inp == 1000
        assert out == 500
        assert cr == 200
        assert cw == 100
        assert cost > 0

    def test_openclaw_format(self):
        record = _openclaw_record(model="kimi-k2.5", input_t=800, output_t=300, provider="moonshot")
        result = _extract_usage(record)
        assert result is not None
        model, provider, ts, inp, out, cr, cw, cost, tools = result
        assert model == "kimi-k2.5"
        assert provider == "moonshot"
        assert inp == 800
        assert out == 300

    def test_openclaw_precomputed_cost(self):
        record = _openclaw_record(model="claude-opus-4-6", cost_total=0.042, provider="anthropic")
        result = _extract_usage(record)
        assert result is not None
        *_, cost, _ = result
        assert cost == 0.042

    def test_openclaw_zero_cost_falls_back(self):
        record = _openclaw_record(model="kimi-k2.5", input_t=1000, output_t=500, cost_total=0.0)
        result = _extract_usage(record)
        assert result is not None
        *_, cost, _ = result
        # Should use compute_cost with default pricing, not 0
        assert cost > 0

    def test_ignores_user_messages(self):
        record = {"type": "human", "message": {"usage": {"input_tokens": 100}}}
        assert _extract_usage(record) is None

    def test_ignores_system_records(self):
        record = {"type": "system", "message": {}}
        assert _extract_usage(record) is None

    def test_ignores_message_without_usage(self):
        record = {"type": "assistant", "message": {"model": "test"}}
        assert _extract_usage(record) is None

    def test_openclaw_non_assistant_role(self):
        record = {
            "type": "message",
            "timestamp": "2026-02-09T11:00:00Z",
            "message": {
                "role": "user",
                "model": "kimi-k2.5",
                "provider": "moonshot",
                "usage": {"input": 100, "output": 50},
            },
        }
        assert _extract_usage(record) is None


# ---------------------------------------------------------------------------
# parse_session_file — integration
# ---------------------------------------------------------------------------

class TestParseSessionFile:
    def test_claude_code_file(self):
        records = [
            {"type": "human", "message": {"content": "hello"}},
            _claude_code_record(input_t=500, output_t=200),
            _claude_code_record(input_t=300, output_t=100, timestamp="2026-02-09T10:01:00Z"),
        ]
        path = _write_jsonl(records)
        try:
            events = parse_session_file(path)
            assert len(events) == 2
            assert events[0].input_tokens == 500
            assert events[1].input_tokens == 300
        finally:
            path.unlink()

    def test_openclaw_file(self):
        records = [
            _openclaw_record(model="kimi-k2.5", input_t=800, output_t=300),
            _openclaw_record(model="z-ai/glm4.7", input_t=600, output_t=200, timestamp="2026-02-09T11:01:00Z"),
        ]
        path = _write_jsonl(records)
        try:
            events = parse_session_file(path)
            assert len(events) == 2
            assert events[0].model == "kimi-k2.5"
            assert events[0].provider == "moonshot"
            assert events[1].model == "z-ai/glm4.7"
        finally:
            path.unlink()

    def test_mixed_format_file(self):
        records = [
            _claude_code_record(model="claude-opus-4-6", input_t=1000, output_t=500),
            _openclaw_record(model="kimi-k2.5", input_t=800, output_t=300),
            {"type": "human", "message": {"content": "ignored"}},
            _openclaw_record(model="z-ai/glm4.7", input_t=600, output_t=200, timestamp="2026-02-09T12:00:00Z"),
        ]
        path = _write_jsonl(records)
        try:
            events = parse_session_file(path)
            assert len(events) == 3
            models = [e.model for e in events]
            assert "claude-opus-4-6" in models
            assert "kimi-k2.5" in models
            assert "z-ai/glm4.7" in models
        finally:
            path.unlink()

    def test_empty_file(self):
        path = _write_jsonl([])
        try:
            assert parse_session_file(path) == []
        finally:
            path.unlink()

    def test_malformed_json_lines_skipped(self):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        tmp.write("not json\n")
        tmp.write(json.dumps(_claude_code_record(input_t=100, output_t=50)) + "\n")
        tmp.write("{bad json\n")
        tmp.flush()
        tmp.close()
        path = Path(tmp.name)
        try:
            events = parse_session_file(path)
            assert len(events) == 1
            assert events[0].input_tokens == 100
        finally:
            path.unlink()


# ---------------------------------------------------------------------------
# _extract_tools / _normalize_tool_name
# ---------------------------------------------------------------------------

class TestExtractTools:
    def test_standard_tools(self):
        message = {
            "content": [
                {"type": "tool_use", "name": "Read", "id": "1", "input": {}},
                {"type": "tool_use", "name": "Write", "id": "2", "input": {}},
            ]
        }
        assert _extract_tools(message) == "Read,Write"

    def test_mcp_chrome_grouped(self):
        message = {
            "content": [
                {"type": "tool_use", "name": "mcp__claude-in-chrome__navigate", "id": "1", "input": {}},
                {"type": "tool_use", "name": "mcp__claude-in-chrome__click", "id": "2", "input": {}},
            ]
        }
        assert _extract_tools(message) == "chrome-browser"

    def test_mcp_plugin_grouped(self):
        message = {
            "content": [
                {"type": "tool_use", "name": "mcp__plugin_pinecone_pinecone__search", "id": "1", "input": {}},
            ]
        }
        assert _extract_tools(message) == "pinecone"

    def test_no_tools(self):
        assert _extract_tools({"content": [{"type": "text", "text": "hello"}]}) == ""

    def test_empty_content(self):
        assert _extract_tools({"content": []}) == ""
        assert _extract_tools({}) == ""


class TestNormalizeToolName:
    def test_standard(self):
        assert _normalize_tool_name("Read") == "Read"

    def test_chrome(self):
        assert _normalize_tool_name("mcp__claude-in-chrome__snapshot") == "chrome-browser"

    def test_plugin(self):
        assert _normalize_tool_name("mcp__plugin_greptile_greptile__search") == "greptile"

    def test_generic_mcp(self):
        assert _normalize_tool_name("mcp__myserver__tool") == "myserver"


# ---------------------------------------------------------------------------
# compute_cost
# ---------------------------------------------------------------------------

class TestComputeCost:
    def test_known_model(self):
        # Sonnet: input=3.0, output=15.0 per million
        cost = compute_cost("claude-sonnet-4-5-20250929", 1_000_000, 1_000_000, 0, 0)
        assert cost == pytest.approx(18.0, abs=0.01)

    def test_unknown_model_uses_default(self):
        # Default = Sonnet pricing
        cost = compute_cost("kimi-k2.5", 1_000_000, 1_000_000, 0, 0)
        assert cost == pytest.approx(18.0, abs=0.01)

    def test_zero_tokens(self):
        assert compute_cost("claude-opus-4-6", 0, 0, 0, 0) == 0.0

    def test_cache_tokens(self):
        # Opus: cache_read=1.5, cache_write=18.75 per million
        cost = compute_cost("claude-opus-4-6", 0, 0, 1_000_000, 1_000_000)
        assert cost == pytest.approx(20.25, abs=0.01)
