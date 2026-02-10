"""Spike detection for cost anomalies."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from .parser import MessageUsage


@dataclass
class Anomaly:
    date: str
    expected_cost: float
    actual_cost: float
    severity: str  # "warning" or "critical"
    pct_over: float
    project: str = ""


def detect_anomalies(
    messages: list[MessageUsage],
    threshold: float = 0.25,
    window_days: int = 7,
) -> list[Anomaly]:
    """Compare each day to its rolling average and flag spikes.

    threshold: fraction above average to trigger (0.25 = 25% over)
    """
    # Build daily cost totals
    daily = defaultdict(float)
    for m in messages:
        ts = m.timestamp.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        day = dt.strftime("%Y-%m-%d")
        daily[day] += m.cost_total

    if not daily:
        return []

    # Sort days
    sorted_days = sorted(daily.keys())
    anomalies = []

    for i, day in enumerate(sorted_days):
        # Need at least 3 days of history for a meaningful average
        if i < 3:
            continue

        # Rolling average of previous `window_days` days
        start_idx = max(0, i - window_days)
        window = sorted_days[start_idx:i]
        if not window:
            continue

        avg = sum(daily[d] for d in window) / len(window)
        actual = daily[day]

        if avg == 0:
            # If average is 0 and we have any cost, that's anomalous
            if actual > 0:
                anomalies.append(Anomaly(
                    date=day,
                    expected_cost=0,
                    actual_cost=round(actual, 4),
                    severity="warning",
                    pct_over=100.0,
                ))
            continue

        pct_over = (actual - avg) / avg
        if pct_over > threshold:
            severity = "critical" if pct_over > threshold * 3 else "warning"
            anomalies.append(Anomaly(
                date=day,
                expected_cost=round(avg, 4),
                actual_cost=round(actual, 4),
                severity=severity,
                pct_over=round(pct_over * 100, 1),
            ))

    return anomalies


def detect_project_anomalies(
    messages: list[MessageUsage],
    threshold: float = 0.25,
) -> list[Anomaly]:
    """Detect per-project daily anomalies."""
    # Group by project then day
    by_project = defaultdict(lambda: defaultdict(float))
    for m in messages:
        ts = m.timestamp.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        day = dt.strftime("%Y-%m-%d")
        by_project[m.project_name][day] += m.cost_total

    all_anomalies = []
    for project, daily in by_project.items():
        sorted_days = sorted(daily.keys())
        for i, day in enumerate(sorted_days):
            if i < 3:
                continue
            start_idx = max(0, i - 7)
            window = sorted_days[start_idx:i]
            if not window:
                continue
            avg = sum(daily[d] for d in window) / len(window)
            actual = daily[day]
            if avg == 0:
                continue
            pct_over = (actual - avg) / avg
            if pct_over > threshold:
                severity = "critical" if pct_over > threshold * 3 else "warning"
                all_anomalies.append(Anomaly(
                    date=day,
                    expected_cost=round(avg, 4),
                    actual_cost=round(actual, 4),
                    severity=severity,
                    pct_over=round(pct_over * 100, 1),
                    project=project,
                ))

    return sorted(all_anomalies, key=lambda a: a.date, reverse=True)
