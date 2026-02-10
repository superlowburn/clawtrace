"""CLI entry point for ClawTrace."""

import argparse
import json
import os
import sys

from .parser import find_session_files, parse_session_file
from .aggregator import get_summary, get_cost_timeseries, get_model_breakdown, get_project_breakdown, get_top_sessions
from .anomaly import detect_anomalies


def _load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            return json.load(f)
    return {
        "data_paths": ["~/.openclaw/agents", "~/.claude/projects"],
        "anomaly_threshold": 0.25,
        "server_port": 19898,
    }


def _load_messages(config: dict) -> list:
    data_paths = config.get("data_paths", [])
    files = find_session_files(data_paths)
    messages = []
    for f in files:
        messages.extend(parse_session_file(f))
    return messages


def _format_cost(cost: float) -> str:
    if cost >= 1.0:
        return f"${cost:.2f}"
    return f"${cost:.4f}"


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def cmd_status(args, config):
    messages = _load_messages(config)
    summary = get_summary(messages)
    file_count = len(find_session_files(config.get("data_paths", [])))

    if args.json:
        summary["file_count"] = file_count
        summary["total_messages"] = len(messages)
        print(json.dumps(summary, indent=2))
        return

    print(f"ClawTrace Status — {summary['date']}")
    print(f"{'=' * 40}")
    print(f"Session files scanned:  {file_count}")
    print(f"Total messages parsed:  {len(messages)}")
    print()
    print(f"Today's cost:           {_format_cost(summary['total_cost_usd'])}")
    print(f"Today's tokens:         {_format_tokens(summary['total_tokens'])}")
    print(f"Today's sessions:       {summary['session_count']}")
    print(f"Today's API calls:      {summary['message_count']}")


def cmd_cost_report(args, config):
    messages = _load_messages(config)
    days = args.days

    if args.json:
        result = {
            "timeseries": get_cost_timeseries(messages, days=days),
            "models": get_model_breakdown(messages),
            "projects": get_project_breakdown(messages),
            "top_sessions": get_top_sessions(messages, n=5),
        }
        print(json.dumps(result, indent=2))
        return

    print(f"Cost Report — Last {days} days")
    print(f"{'=' * 50}")

    # Daily costs
    ts = get_cost_timeseries(messages, days=days)
    total = sum(d["cost_usd"] for d in ts)
    print(f"\nDaily Costs (total: {_format_cost(total)}):")
    for entry in ts:
        bar = "#" * int(entry["cost_usd"] * 10) if entry["cost_usd"] > 0 else ""
        print(f"  {entry['date']}  {_format_cost(entry['cost_usd']):>10}  {bar}")

    # Model breakdown
    models = get_model_breakdown(messages)
    if models:
        print(f"\nBy Model:")
        for m in models:
            print(f"  {m['model']:<35} {_format_cost(m['cost_usd']):>10}  ({_format_tokens(m['total_tokens'])} tokens, {m['message_count']} calls)")

    # Project breakdown
    projects = get_project_breakdown(messages)
    if projects:
        print(f"\nBy Project:")
        for p in projects:
            print(f"  {p['project']:<35} {_format_cost(p['cost_usd']):>10}  ({p['session_count']} sessions)")

    # Top sessions
    sessions = get_top_sessions(messages, n=5)
    if sessions:
        print(f"\nTop 5 Most Expensive Sessions:")
        for s in sessions:
            print(f"  {s['session_id'][:12]}...  {_format_cost(s['cost_usd']):>10}  {s['project']:<20} ({s['message_count']} calls)")


def cmd_anomalies(args, config):
    messages = _load_messages(config)
    threshold = config.get("anomaly_threshold", 0.25)
    anomalies = detect_anomalies(messages, threshold=threshold)

    if args.json:
        print(json.dumps([{
            "date": a.date,
            "expected_cost": a.expected_cost,
            "actual_cost": a.actual_cost,
            "severity": a.severity,
            "pct_over": a.pct_over,
        } for a in anomalies], indent=2))
        return

    if not anomalies:
        print("No anomalies detected.")
        return

    print(f"Cost Anomalies (threshold: {threshold * 100:.0f}% over rolling average)")
    print(f"{'=' * 60}")
    for a in anomalies:
        marker = "!!" if a.severity == "critical" else " >"
        print(f"  {marker} {a.date}  actual: {_format_cost(a.actual_cost)}  expected: {_format_cost(a.expected_cost)}  (+{a.pct_over}%)")


def cmd_serve(args, config):
    from .server import run_server
    config["server_port"] = args.port or config.get("server_port", 19898)
    run_server(config)


def main():
    parser = argparse.ArgumentParser(prog="clawtrace", description="Local-first observability for Claude/OpenClaw sessions")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    subparsers = parser.add_subparsers(dest="command")

    # status
    sub = subparsers.add_parser("status", help="Quick summary of today's usage")
    sub.add_argument("--json", action="store_true", help="Output as JSON")

    # cost-report
    sub = subparsers.add_parser("cost-report", help="Detailed cost breakdown")
    sub.add_argument("--days", type=int, default=7, help="Number of days (default: 7)")
    sub.add_argument("--json", action="store_true", help="Output as JSON")

    # anomalies
    sub = subparsers.add_parser("anomalies", help="List cost spikes")
    sub.add_argument("--json", action="store_true", help="Output as JSON")

    # serve
    sub = subparsers.add_parser("serve", help="Start HTTP API server")
    sub.add_argument("--port", type=int, help="Port number (default: 19898)")

    args = parser.parse_args()
    config = _load_config()

    if args.command == "status":
        cmd_status(args, config)
    elif args.command == "cost-report":
        cmd_cost_report(args, config)
    elif args.command == "anomalies":
        cmd_anomalies(args, config)
    elif args.command == "serve":
        cmd_serve(args, config)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
