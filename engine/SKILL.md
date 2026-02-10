---
name: clawtrace
description: "Local-first observability for OpenClaw agents. Check costs, track token usage, detect anomalies."
emoji: "\U0001F50D"
requires:
  bins: [python3]
user-invocable: true
---

# ClawTrace

Local-first cost and usage tracking for Claude Code and OpenClaw sessions.

## Usage

```bash
# Quick status
python -m clawtrace.engine status

# Detailed cost report
python -m clawtrace.engine cost-report --days 7

# Check for anomalies
python -m clawtrace.engine anomalies

# Start API server
python -m clawtrace.engine serve --port 19898
```

## API Endpoints

- `GET /api/summary` — today's cost, tokens, session count
- `GET /api/costs?range=7d` — cost timeseries
- `GET /api/models` — model breakdown
- `GET /api/projects` — per-project costs
- `GET /api/sessions?n=5` — most expensive sessions
- `GET /api/anomalies` — recent cost spikes
- `GET /api/health` — health check
