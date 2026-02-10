"""Shared test fixtures with sample JSONL data matching real formats."""

import json
import os
import tempfile
from pathlib import Path

import pytest


# Sample data: OpenClaw agent format
OPENCLAW_RECORDS = [
    {
        "type": "session",
        "version": 3,
        "id": "session-abc-123",
        "timestamp": "2026-02-09T10:00:00.000Z",
        "cwd": "/tmp/test",
    },
    {
        "type": "model_change",
        "id": "mc-1",
        "timestamp": "2026-02-09T10:00:00.001Z",
        "provider": "anthropic",
        "modelId": "claude-opus-4-6",
    },
    {
        "type": "message",
        "id": "msg-1",
        "timestamp": "2026-02-09T10:00:05.000Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "provider": "anthropic",
            "usage": {
                "input": 100,
                "output": 200,
                "cacheRead": 5000,
                "cacheWrite": 10000,
                "totalTokens": 15300,
                "cost": {
                    "input": 0.0015,
                    "output": 0.015,
                    "cacheRead": 0.0075,
                    "cacheWrite": 0.1875,
                    "total": 0.2115,
                },
            },
            "stopReason": "endTurn",
        },
    },
    {
        "type": "message",
        "id": "msg-2",
        "timestamp": "2026-02-09T10:01:00.000Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "provider": "anthropic",
            "usage": {
                "input": 50,
                "output": 300,
                "cacheRead": 10000,
                "cacheWrite": 0,
                "totalTokens": 10350,
                "cost": {
                    "input": 0.00075,
                    "output": 0.0225,
                    "cacheRead": 0.015,
                    "cacheWrite": 0,
                    "total": 0.03825,
                },
            },
            "stopReason": "endTurn",
        },
    },
    # A user message — should be ignored
    {
        "type": "message",
        "id": "msg-user",
        "timestamp": "2026-02-09T10:00:30.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "Hello"}],
        },
    },
]

# Sample data: Claude Code format
CLAUDE_CODE_RECORDS = [
    {
        "type": "assistant",
        "uuid": "uuid-1",
        "sessionId": "session-def-456",
        "timestamp": "2026-02-08T15:00:00.000Z",
        "message": {
            "model": "claude-sonnet-4-5-20250929",
            "role": "assistant",
            "usage": {
                "input_tokens": 200,
                "cache_creation_input_tokens": 30000,
                "cache_read_input_tokens": 0,
                "output_tokens": 500,
                "service_tier": "standard",
            },
        },
    },
    {
        "type": "assistant",
        "uuid": "uuid-2",
        "sessionId": "session-def-456",
        "timestamp": "2026-02-08T15:05:00.000Z",
        "message": {
            "model": "claude-sonnet-4-5-20250929",
            "role": "assistant",
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 30000,
                "output_tokens": 1000,
                "service_tier": "standard",
            },
        },
    },
    # Non-assistant type — should be ignored
    {
        "type": "file-history-snapshot",
        "messageId": "abc",
        "snapshot": {},
    },
]


@pytest.fixture
def openclaw_jsonl(tmp_path):
    """Create a fake OpenClaw agent session file."""
    agent_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    agent_dir.mkdir(parents=True)
    f = agent_dir / "session-abc-123.jsonl"
    with open(f, "w") as fh:
        for record in OPENCLAW_RECORDS:
            fh.write(json.dumps(record) + "\n")
    return f


@pytest.fixture
def claude_code_jsonl(tmp_path):
    """Create a fake Claude Code session file."""
    project_dir = tmp_path / ".claude" / "projects" / "-Users-test-claude-threadjack"
    project_dir.mkdir(parents=True)
    f = project_dir / "session-def-456.jsonl"
    with open(f, "w") as fh:
        for record in CLAUDE_CODE_RECORDS:
            fh.write(json.dumps(record) + "\n")
    return f


@pytest.fixture
def malformed_jsonl(tmp_path):
    """Create a JSONL file with some bad lines."""
    f = tmp_path / "bad.jsonl"
    with open(f, "w") as fh:
        fh.write('{"type": "message", "id": "ok"}\n')
        fh.write("this is not json\n")
        fh.write('{"type": "message"}\n')
        fh.write("\n")  # empty line
    return f


@pytest.fixture
def multi_day_messages():
    """Create messages spanning multiple days for aggregation tests."""
    from engine.parser import MessageUsage

    messages = []
    # 7 days of data with varying costs
    base_costs = [1.0, 1.2, 0.9, 1.1, 1.0, 5.0, 1.0]  # Day 6 is a spike
    for day_offset, cost in enumerate(base_costs):
        messages.append(MessageUsage(
            timestamp=f"2026-02-{3 + day_offset:02d}T12:00:00.000Z",
            model="claude-opus-4-6",
            provider="anthropic",
            input_tokens=1000,
            output_tokens=2000,
            cache_read_tokens=5000,
            cache_write_tokens=3000,
            cost_total=cost,
            cost_breakdown={"input": cost * 0.1, "output": cost * 0.5, "cache_read": cost * 0.2, "cache_write": cost * 0.2},
            session_id=f"session-{day_offset}",
            session_file=f"/tmp/session-{day_offset}.jsonl",
            project_name="threadjack",
        ))
    return messages
