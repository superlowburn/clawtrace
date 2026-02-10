"""Tests for the anomaly detection module."""

from engine.anomaly import detect_anomalies, Anomaly


class TestDetectAnomalies:
    def test_detects_spike(self, multi_day_messages):
        # Day 6 (Feb 8) has cost=5.0 vs average ~1.0
        anomalies = detect_anomalies(multi_day_messages, threshold=0.25)
        assert len(anomalies) > 0
        # The spike day should be detected
        spike_dates = [a.date for a in anomalies]
        assert "2026-02-08" in spike_dates

    def test_spike_severity(self, multi_day_messages):
        anomalies = detect_anomalies(multi_day_messages, threshold=0.25)
        spike = next(a for a in anomalies if a.date == "2026-02-08")
        # 5.0 vs ~1.0 average = ~400% over = critical
        assert spike.severity == "critical"
        assert spike.pct_over > 100

    def test_no_anomalies_in_stable_data(self):
        from engine.parser import MessageUsage

        messages = []
        for i in range(10):
            messages.append(MessageUsage(
                timestamp=f"2026-02-{i + 1:02d}T12:00:00Z",
                model="claude-opus-4-6",
                provider="anthropic",
                input_tokens=1000,
                output_tokens=1000,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_total=1.0,
                cost_breakdown={},
                session_id=f"s-{i}",
                session_file=f"/tmp/s-{i}.jsonl",
                project_name="test",
            ))
        anomalies = detect_anomalies(messages, threshold=0.25)
        assert len(anomalies) == 0

    def test_empty_messages(self):
        assert detect_anomalies([], threshold=0.25) == []

    def test_too_few_days(self):
        from engine.parser import MessageUsage

        # Only 2 days — not enough for rolling average
        messages = [
            MessageUsage(
                timestamp=f"2026-02-0{i + 1}T12:00:00Z",
                model="m", provider="p",
                input_tokens=100, output_tokens=100,
                cache_read_tokens=0, cache_write_tokens=0,
                cost_total=1.0, cost_breakdown={},
                session_id=f"s{i}", session_file="f",
                project_name="test",
            )
            for i in range(2)
        ]
        assert detect_anomalies(messages) == []
