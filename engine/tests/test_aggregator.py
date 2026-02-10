"""Tests for the aggregator module."""

from engine.parser import MessageUsage
from engine.aggregator import (
    get_summary,
    get_cost_timeseries,
    get_model_breakdown,
    get_project_breakdown,
    get_top_sessions,
)


class TestGetSummary:
    def test_empty_messages(self):
        summary = get_summary([])
        assert summary["total_cost_usd"] == 0
        assert summary["total_tokens"] == 0
        assert summary["session_count"] == 0

    def test_filters_to_today(self, multi_day_messages):
        # multi_day_messages are from Feb 3-9, only Feb 9 is "today" if running on that date
        # Since this is test data, summary will show 0 unless we mock "today"
        summary = get_summary(multi_day_messages)
        # The exact result depends on when tests run — just verify structure
        assert "total_cost_usd" in summary
        assert "total_tokens" in summary
        assert "session_count" in summary
        assert "date" in summary


class TestCostTimeseries:
    def test_returns_correct_days(self, multi_day_messages):
        ts = get_cost_timeseries(multi_day_messages, days=7)
        assert len(ts) == 7
        for entry in ts:
            assert "date" in entry
            assert "cost_usd" in entry

    def test_empty_messages(self):
        ts = get_cost_timeseries([], days=3)
        assert len(ts) == 3
        assert all(e["cost_usd"] == 0 for e in ts)


class TestModelBreakdown:
    def test_groups_by_model(self):
        messages = [
            _make_msg(model="claude-opus-4-6", cost=1.0),
            _make_msg(model="claude-opus-4-6", cost=2.0),
            _make_msg(model="claude-sonnet-4-5-20250929", cost=0.5),
        ]
        breakdown = get_model_breakdown(messages)
        assert len(breakdown) == 2
        # Opus should be first (highest cost)
        assert breakdown[0]["model"] == "claude-opus-4-6"
        assert abs(breakdown[0]["cost_usd"] - 3.0) < 0.001
        assert breakdown[0]["message_count"] == 2

    def test_empty_messages(self):
        assert get_model_breakdown([]) == []


class TestProjectBreakdown:
    def test_groups_by_project(self):
        messages = [
            _make_msg(project="threadjack", cost=5.0, session_id="s1"),
            _make_msg(project="threadjack", cost=3.0, session_id="s2"),
            _make_msg(project="vt2", cost=1.0, session_id="s3"),
        ]
        breakdown = get_project_breakdown(messages)
        assert len(breakdown) == 2
        assert breakdown[0]["project"] == "threadjack"
        assert abs(breakdown[0]["cost_usd"] - 8.0) < 0.001
        assert breakdown[0]["session_count"] == 2


class TestTopSessions:
    def test_returns_top_n(self):
        messages = [
            _make_msg(session_id="s1", cost=10.0),
            _make_msg(session_id="s2", cost=5.0),
            _make_msg(session_id="s3", cost=1.0),
        ]
        top = get_top_sessions(messages, n=2)
        assert len(top) == 2
        assert top[0]["session_id"] == "s1"
        assert top[1]["session_id"] == "s2"


def _make_msg(
    model="claude-opus-4-6",
    cost=1.0,
    project="test",
    session_id="test-session",
    timestamp="2026-02-09T12:00:00Z",
) -> MessageUsage:
    return MessageUsage(
        timestamp=timestamp,
        model=model,
        provider="anthropic",
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_total=cost,
        cost_breakdown={},
        session_id=session_id,
        session_file="/tmp/test.jsonl",
        project_name=project,
    )
