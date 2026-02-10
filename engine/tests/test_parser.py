"""Tests for the JSONL parser."""

import json
from pathlib import Path

from engine.parser import (
    parse_session_file,
    find_session_files,
    _extract_project_name,
    _compute_cost,
)


class TestParseOpenClawFormat:
    def test_extracts_assistant_messages(self, openclaw_jsonl):
        messages = parse_session_file(openclaw_jsonl)
        assert len(messages) == 2  # 2 assistant messages, user message skipped

    def test_correct_token_counts(self, openclaw_jsonl):
        messages = parse_session_file(openclaw_jsonl)
        m = messages[0]
        assert m.input_tokens == 100
        assert m.output_tokens == 200
        assert m.cache_read_tokens == 5000
        assert m.cache_write_tokens == 10000

    def test_uses_precomputed_cost(self, openclaw_jsonl):
        messages = parse_session_file(openclaw_jsonl)
        m = messages[0]
        assert abs(m.cost_total - 0.2115) < 0.0001

    def test_model_and_provider(self, openclaw_jsonl):
        messages = parse_session_file(openclaw_jsonl)
        assert messages[0].model == "claude-opus-4-6"
        assert messages[0].provider == "anthropic"

    def test_session_id_from_filename(self, openclaw_jsonl):
        messages = parse_session_file(openclaw_jsonl)
        assert messages[0].session_id == "session-abc-123"


class TestParseClaudeCodeFormat:
    def test_extracts_assistant_messages(self, claude_code_jsonl):
        messages = parse_session_file(claude_code_jsonl)
        assert len(messages) == 2  # file-history-snapshot skipped

    def test_correct_token_counts(self, claude_code_jsonl):
        messages = parse_session_file(claude_code_jsonl)
        m = messages[0]
        assert m.input_tokens == 200
        assert m.output_tokens == 500
        assert m.cache_read_tokens == 0
        assert m.cache_write_tokens == 30000

    def test_computes_cost_from_tokens(self, claude_code_jsonl):
        messages = parse_session_file(claude_code_jsonl)
        m = messages[0]
        # Sonnet pricing: input=3/M, output=15/M, cache_write=3.75/M
        expected_input = 200 * 3.0 / 1_000_000
        expected_output = 500 * 15.0 / 1_000_000
        expected_cache_write = 30000 * 3.75 / 1_000_000
        expected_total = expected_input + expected_output + expected_cache_write
        assert abs(m.cost_total - expected_total) < 0.0001


class TestMalformedInput:
    def test_handles_bad_json_gracefully(self, malformed_jsonl):
        # Should not raise, just skip bad lines
        messages = parse_session_file(malformed_jsonl)
        assert len(messages) == 0  # none of the lines have valid usage data

    def test_handles_missing_file(self, tmp_path):
        messages = parse_session_file(tmp_path / "nonexistent.jsonl")
        assert messages == []


class TestProjectNameExtraction:
    def test_claude_project_path(self):
        path = "/Users/steve/.claude/projects/-Users-steve-claude-threadjack/abc.jsonl"
        assert _extract_project_name(path) == "threadjack"

    def test_claude_project_subagent(self):
        path = "/Users/steve/.claude/projects/-Users-steve-claude-vt2/session/subagents/agent-abc.jsonl"
        assert _extract_project_name(path) == "vt2"

    def test_openclaw_agent(self):
        path = "/Users/steve/.openclaw/agents/main/sessions/abc.jsonl"
        assert _extract_project_name(path) == "openclaw-main"

    def test_unknown_path(self):
        path = "/tmp/random/file.jsonl"
        assert _extract_project_name(path) == "unknown"


class TestFindSessionFiles:
    def test_finds_files_in_directory(self, tmp_path):
        d = tmp_path / "data"
        d.mkdir()
        (d / "a.jsonl").touch()
        (d / "b.jsonl").touch()
        (d / "c.txt").touch()  # not JSONL
        files = find_session_files([str(d)])
        assert len(files) == 2

    def test_finds_nested_files(self, tmp_path):
        d = tmp_path / "data" / "sub" / "deep"
        d.mkdir(parents=True)
        (d / "session.jsonl").touch()
        files = find_session_files([str(tmp_path / "data")])
        assert len(files) == 1

    def test_skips_nonexistent_path(self):
        files = find_session_files(["/nonexistent/path"])
        assert files == []


class TestComputeCost:
    def test_opus_pricing(self):
        total, breakdown = _compute_cost("claude-opus-4-6", 1000, 1000, 1000, 1000)
        assert abs(breakdown["input"] - 0.015) < 0.0001
        assert abs(breakdown["output"] - 0.075) < 0.0001
        assert abs(breakdown["cache_read"] - 0.0015) < 0.0001
        assert abs(breakdown["cache_write"] - 0.01875) < 0.0001

    def test_unknown_model_uses_default(self):
        total, breakdown = _compute_cost("some-new-model", 1000, 1000, 0, 0)
        # Default uses Sonnet pricing
        assert abs(breakdown["input"] - 0.003) < 0.0001
        assert abs(breakdown["output"] - 0.015) < 0.0001
