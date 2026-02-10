# ClawTrace — Local-First Observability for OpenClaw Agents

## What This Is

A SaaS product that gives OpenClaw users visibility into their agent costs, token usage, and health. Born from ThreadJack signal discovery — OpenClaw's creator (@steipete) explicitly rejected telemetry, but enterprise users want analytics. ClawTrace fills that gap with a local-first approach: your data never leaves your machine unless you opt into the hosted dashboard.

## Origin Story

ClawTrace was born from ThreadJack competitive intelligence signals in the OpenClaw ecosystem:

1. **The rejection signal** (2026-02-06): @steipete (Peter Steinberger, OpenClaw creator, 179K+ GitHub stars) explicitly rejected built-in telemetry/analytics in a release thread with 614K views
2. **The enterprise demand** (2026-02-07): @jlevitsk from FileWave replied wanting cost tracking, per-skill breakdowns, agent monitoring — classic enterprise analytics need
3. **Steve's threadjack** (2026-02-08): Replied to @jlevitsk positioning "Datadog for OpenClaw" — local-first observability skill. Tweet ID: 2020601641049952417. jlevitsk replied with a detailed feature spec AND followed Steve — warm lead
4. **Dashboard thread** (2026-02-09): @GanimCorey's OpenClaw dashboard thread (15K views, 28 replies) — Steve posted 2 strategic replies about per-skill cost breakdowns and strategy tracking
5. **Product built** (2026-02-09–10): Engine, sender, hosted backend, landing page, menu bar app — all shipped in 48 hours

The positioning: steipete won't build telemetry into OpenClaw. Enterprise users want it. ClawTrace gives them analytics without compromising OpenClaw's privacy-first stance — your data stays local unless you opt into the hosted dashboard.

## Architecture (5 components)

### 1. Engine (`engine/`) — Python backend (local)
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
- **Tests** (`tests/`): pytest suite — 171 tests covering aggregator, anomaly, parser, server, alerts, pricing, hosted mode

### 2. Hosted Backend (DigitalOcean VPS) — LIVE
- **VPS**: `143.110.218.78` (DigitalOcean, root access)
- **Stack**: Gunicorn + nginx + SQLite, SSL via Let's Encrypt
- **Domain**: `clawtrace.vybng.co`
- **Auth**: Device registration (`/api/register`) returns `device_id` + `device_secret`; all endpoints require `Authorization: Bearer <secret>`
- **Tier enforcement**: Free (1 project, 7 days retention), Pro ($79, unlimited projects, 90 days), Team ($199, 3 devices)
- **Endpoints**: `/api/ingest`, `/api/register`, `/api/claim`, `/api/resync/<device_id>`, `/api/stats/<device_id>`, `/d/<device_id>` (dashboard)
- **Systemd**: `clawtrace.service` (Gunicorn), `clawtrace-sync.timer` (periodic sender sync on VPS)
- **Deploy**: `bash deploy.sh --sync-only` (syncs code to VPS)

### 3. macOS Menu Bar App (`app/ClawTrace/`) — Swift/SwiftUI
- Native macOS menu bar app showing real-time cost dashboard
- Talks to the local engine API (port 19898)
- Views: MenuBarView, DashboardWindow, CostChartView, ModelBreakdownView, ProjectListView, AnomalyBadge
- Swift Package Manager (`Package.swift`)

### 4. Landing Page (`web/`) — Astro + Cloudflare Pages
- Marketing site at `clawtrace.vybng.co`
- Built with Astro, deployed to Cloudflare Pages
- Signup form → Cloudflare KV (namespace `SIGNUPS`, id `8e48aa1958344cb294a942b31bdb3380`)
- Pricing: Free ($0, 1 project, 7 days), Pro ($79 one-time), Team ($199 one-time, 3 licenses)
- `wrangler.toml` configured for Cloudflare Pages

### 5. OpenClaw Skill (`skill/`) — Data sender
- `sender.py`: Syncs anonymized usage data to hosted ClawTrace API
- Handles both JSONL session formats (see below)
- Sends only: timestamps, model names, token counts, costs (no conversation content)
- Device ID + secret stored in `~/.clawtrace/device.json`
- Deduplicates sent events via `~/.clawtrace/sent_cursor.json`
- `test_sender.py`: 32 tests covering both formats, project extraction, cost computation

## Session JSONL Formats

Both `engine/parser.py` and `skill/sender.py` handle two distinct JSONL formats:

### Format 1: Claude Code (`~/.claude/projects/`)
```json
{"type": "assistant", "timestamp": "...", "provider": "anthropic",
 "message": {"model": "claude-sonnet-4-5-20250929",
  "usage": {"input_tokens": 1000, "output_tokens": 500,
   "cache_read_input_tokens": 200, "cache_creation_input_tokens": 100}}}
```

### Format 2: OpenClaw Agents (`~/.openclaw/agents/`)
```json
{"type": "message", "timestamp": "...",
 "message": {"role": "assistant", "model": "kimi-k2.5", "provider": "moonshot",
  "usage": {"input": 800, "output": 300, "cacheRead": 0, "cacheWrite": 0,
   "cost": {"total": 0.0}}}}
```

Key differences:
- `type`: `"assistant"` (Claude Code) vs `"message"` (OpenClaw)
- Role: implicit in Claude Code, explicit `message.role` in OpenClaw
- Usage keys: `input_tokens` vs `input`, `cache_read_input_tokens` vs `cacheRead`
- Provider: `record.provider` (Claude Code) vs `message.provider` (OpenClaw)
- Cost: computed from tokens (Claude Code) vs sometimes pre-computed in `usage.cost.total` (OpenClaw)

OpenClaw models seen: `kimi-k2.5`, `z-ai/glm4.7`, `claude-opus-4-6`, `claude-sonnet-4-5`, `claude-haiku-4-5`, `glm-4.7`, `delivery-mirror`, `minimaxai/minimax-m2.1`

## What's Built vs What's Next

### Built (working)
- Full engine: parser, aggregator, anomaly detection, pricing, alerts, Flask API, CLI (171 tests)
- Hosted backend: auth, multi-tenant storage, tier enforcement, dashboard serving (LIVE)
- Sender: dual-format parsing, auto-sync via systemd timer (32 tests)
- Swift menu bar app (compiles, connects to local API)
- Landing page with signup form (deployed to Cloudflare)

### Not Yet Built
- **Payment processing**: Stripe/Lemon Squeezy for Pro/Team tiers
- **Distribution**: ClawHub skill listing, Homebrew formula for menu bar app
- **User onboarding**: First-run experience, device registration flow
- **Team features**: Multi-device dashboard, shared team view

## VPS Deployment

| Action | Command |
|--------|---------|
| Deploy code | `bash deploy.sh --sync-only` |
| Full resync | `ssh root@143.110.218.78 '/root/clawtrace/.venv/bin/python /root/clawtrace/skill/sender.py --resync'` |
| Check stats | `curl -s -H "Authorization: Bearer <secret>" https://clawtrace.vybng.co/api/stats/<device_id>` |
| View dashboard | `https://clawtrace.vybng.co/d/<device_id>#<secret>` |
| Service logs | `ssh root@143.110.218.78 'journalctl -u clawtrace -n 50'` |

## Project Setup

```bash
cd ~/claude/clawtrace

# Engine
pip install -e .
python -m clawtrace.engine status
.venv/bin/python -m pytest engine/tests/ -v   # 171 tests
.venv/bin/python -m pytest skill/test_sender.py -v  # 32 tests

# Landing page
cd web && npm install && npm run dev

# Swift app
cd app/ClawTrace && swift build
```

## Key Files

| File | Purpose |
|------|---------|
| `engine/server.py` | Flask API server (local + hosted) |
| `engine/parser.py` | JSONL session log parser (dual-format) |
| `engine/aggregator.py` | Cost/usage computation |
| `engine/anomaly.py` | Spike detection |
| `engine/pricing.py` | Model pricing + per-device overrides |
| `engine/db.py` | SQLite storage |
| `engine/cli.py` | CLI interface |
| `engine/config.json` | All config |
| `skill/sender.py` | Data sender (dual-format, auth, batched) |
| `skill/test_sender.py` | Sender test suite (32 tests) |
| `app/ClawTrace/` | Swift menu bar app |
| `web/src/pages/index.astro` | Landing page |
| `web/wrangler.toml` | Cloudflare deployment |
| `deploy.sh` | VPS deployment script |

## Steve's Context

Steve is a solo developer building in the OpenClaw ecosystem. He's not a big company — he's one person using AI agents to build and ship products. ClawTrace should be simple, focused, and shippable fast. Don't over-engineer. The local-first angle is the moat — users keep their data, no cloud required for core functionality. The hosted tier is for people who want dashboards across devices or team visibility.
