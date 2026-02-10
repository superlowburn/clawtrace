---
name: clawtrace
description: "Analytics and cost tracking for OpenClaw. Syncs your usage data to ClawTrace for cost insights and community benchmarks."
---

# ClawTrace — Analytics for OpenClaw

Tracks your agent's model usage, costs, and performance. View your dashboard at https://clawtrace.vybng.co

## Commands

- `/clawtrace sync` — Sync latest usage data to ClawTrace
- `/clawtrace status` — Show device ID and dashboard URL
- `/clawtrace dashboard` — Open your dashboard URL

## Setup

Run once to sync: `python ~/.openclaw/skills/clawtrace/sender.py`

Your data is anonymized — only token counts, model names, and costs are sent. No conversation content leaves your machine.

## How It Works

1. Finds JSONL session logs from `~/.claude/projects` and `~/.openclaw/agents`
2. Extracts model usage from assistant messages
3. Computes costs using current Anthropic pricing
4. Sends aggregated events to ClawTrace API
5. Tracks sent events to avoid duplicates

## Privacy

- No message content is collected
- Device ID is a random UUID stored locally
- Only metrics sent: timestamps, model names, token counts, computed costs
- Project names are anonymized slugs (e.g., "threadjack", "vintage-tracker")

## Manual Usage

```bash
# Sync now
python ~/.openclaw/skills/clawtrace/sender.py

# Show device ID
python ~/.openclaw/skills/clawtrace/sender.py --device-id

# Show dashboard URL
python ~/.openclaw/skills/clawtrace/sender.py --dashboard
```
