# ClawTrace — Local-First Observability for OpenClaw Agents

## What This Is

A SaaS product that gives OpenClaw users visibility into their agent costs, token usage, and health. Born from ThreadJack signal discovery — OpenClaw's creator (@steipete) explicitly rejected telemetry, but enterprise users want analytics. ClawTrace fills that gap with a local-first approach: your data never leaves your machine unless you opt into the hosted dashboard.

## The Opportunity

- OpenClaw has 179K+ GitHub stars, 5,700+ community skills
- Creator explicitly rejected built-in telemetry/analytics
- Enterprise users (FileWave, etc.) want cost tracking and agent monitoring
- No existing solution — this is greenfield
- Validated via ThreadJack signals: multiple users asking for cost visibility, dashboards, per-skill breakdowns

## Architecture (3 components)

### 1. Engine (`engine/`) — Python backend
- **Parser** (`parser.py`): Reads JSONL session logs from `~/.claude/projects` and `~/.openclaw/agents`
- **Aggregator** (`aggregator.py`): Computes cost summaries, timeseries, model breakdowns, project breakdowns, top sessions
- **Anomaly** (`anomaly.py`): Detects cost spikes using configurable thresholds
- **Pricing** (`pricing.py`): Model pricing lookup with per-device overrides
- **DB** (`db.py`): SQLite storage at `~/.clawtrace/clawtrace.db`
- **Server** (`server.py`): Flask API on port 19898 with endpoints:
  - `GET /api/summary` — today's cost, tokens, session count
  - `GET /api/costs?range=7d` — cost timeseries
  - `GET /api/models` — model breakdown
  - `GET /api/projects` — per-project costs
  - `GET /api/sessions?n=5` — most expensive sessions
  - `GET /api/anomalies` — recent cost spikes
  - `GET /api/health` — health check
- **CLI** (`cli.py`): `python -m clawtrace.engine status|cost-report|anomalies|serve`
- **Config** (`config.json`): Data paths, thresholds, pricing overrides, alert config
- **Alerts**: Daily budget ($10), session spike ($5), hourly burn rate ($3), request/token volume thresholds
- **Tests** (`tests/`): pytest suite covering aggregator, anomaly, parser, server, alerts, pricing, hosted mode

### 2. macOS Menu Bar App (`app/ClawTrace/`) — Swift/SwiftUI
- Native macOS menu bar app showing real-time cost dashboard
- Talks to the local engine API (port 19898)
- Views: MenuBarView, DashboardWindow, CostChartView, ModelBreakdownView, ProjectListView, AnomalyBadge
- Swift Package Manager (`Package.swift`)

### 3. Landing Page (`web/`) — Astro + Cloudflare Pages
- Marketing site at `clawtrace.vybng.co`
- Built with Astro, deployed to Cloudflare Pages
- Signup form → Cloudflare KV (namespace `SIGNUPS`, id `8e48aa1958344cb294a942b31bdb3380`)
- Also has Google Sheets integration option (`GOOGLE_SHEETS_SETUP.md`)
- Pricing: Free ($0, 1 project, 7 days), Pro ($79 one-time), Team ($199 one-time, 3 licenses)
- `wrangler.toml` configured for Cloudflare Pages

### 4. OpenClaw Skill (`skill/`) — Data sender
- `sender.py`: Syncs anonymized usage data to hosted ClawTrace API
- Reads JSONL logs, extracts model usage, computes costs
- Sends only: timestamps, model names, token counts, costs (no conversation content)
- Device ID is random UUID stored locally
- Deduplicates sent events

## What's Built vs What's Next

### Built (working)
- Full engine: parser, aggregator, anomaly detection, pricing, alerts, Flask API, CLI
- Test suite passing
- Swift menu bar app (compiles, connects to local API)
- Landing page with signup form (deployed to Cloudflare)
- OpenClaw skill sender

### Not Yet Built
- **Hosted backend**: The API that receives data from `sender.py` — needs auth, multi-tenant storage, dashboard serving
- **Payment processing**: Stripe/Lemon Squeezy for Pro/Team tiers
- **Distribution**: ClawHub skill listing, Homebrew formula for menu bar app
- **User onboarding**: First-run experience, device registration flow
- **Dashboard frontend**: Hosted web dashboard for users who opt into cloud sync (the local engine has a basic `dashboard.html` but no hosted equivalent)

## Relationship to ThreadJack

ThreadJack is the intelligence tool that found this opportunity. ClawTrace is the product born from it:
1. ThreadJack monitored OpenClaw ecosystem on Twitter + GitHub
2. Found signals: steipete rejected telemetry, enterprise users wanted analytics
3. Steve posted strategic replies positioning "Datadog for OpenClaw"
4. Warm leads responded (jlevitsk from FileWave gave detailed feature spec)
5. ClawTrace is the product that delivers on that positioning

ThreadJack continues to monitor for new signals that inform ClawTrace's roadmap.

## Project Setup

```bash
cd ~/claude/clawtrace

# Engine
pip install -e .
python -m clawtrace.engine status
python -m pytest engine/tests/ -v

# Landing page
cd web && npm install && npm run dev

# Swift app
cd app/ClawTrace && swift build
```

## Key Files

| File | Purpose |
|------|---------|
| `engine/server.py` | Flask API server |
| `engine/parser.py` | JSONL session log parser |
| `engine/aggregator.py` | Cost/usage computation |
| `engine/anomaly.py` | Spike detection |
| `engine/pricing.py` | Model pricing + overrides |
| `engine/db.py` | SQLite storage |
| `engine/cli.py` | CLI interface |
| `engine/config.json` | All config |
| `app/ClawTrace/` | Swift menu bar app |
| `web/src/pages/index.astro` | Landing page |
| `web/wrangler.toml` | Cloudflare deployment |
| `skill/sender.py` | OpenClaw skill data sender |

## Steve's Context

Steve is a solo developer building in the OpenClaw ecosystem. He's not a big company — he's one person using AI agents to build and ship products. ClawTrace should be simple, focused, and shippable fast. Don't over-engineer. The local-first angle is the moat — users keep their data, no cloud required for core functionality. The hosted tier is for people who want dashboards across devices or team visibility.
