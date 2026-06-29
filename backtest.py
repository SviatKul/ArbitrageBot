#!/usr/bin/env python3
"""
Arbitrage backtest CLI.

Usage:
  # Live snapshot — scan current markets:
  python backtest.py --live

  # CSV replay — replay historical snapshots:
  python backtest.py --csv data/quotes_history.csv

  # Adjust simulated stake per trade:
  python backtest.py --live --stake 50

Options:
  --live          Fetch live quotes and run one scan
  --csv FILE      Replay quotes from CSV file
  --stake FLOAT   Simulated stake per trade in USD (default: 10)
  --output FILE   Write full results to JSON file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from config.settings import get_settings
from utils.logger import setup_logging_rotating


def main() -> None:
    parser = argparse.ArgumentParser(description="Arbitrage backtest")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--live", action="store_true", help="Fetch live quotes from all exchanges")
    group.add_argument("--csv", metavar="FILE", help="Replay CSV quote history")
    parser.add_argument("--stake", type=float, default=10.0, help="Simulated USD per trade")
    parser.add_argument("--output", metavar="FILE", help="Write JSON results to file")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging_rotating(settings.log_level, log_directory=ROOT / settings.log_directory)

    from backtest.runner import run_csv_replay, run_live_snapshot

    if args.live:
        from run import _build_clients
        clients = _build_clients(settings)
        if len(clients) < 2:
            print(f"Need ≥2 exchanges. Configured: {list(clients.keys())}", file=sys.stderr)
            sys.exit(1)
        result = run_live_snapshot(settings, clients, stake_usd=args.stake)
    else:
        result = run_csv_replay(Path(args.csv), settings, stake_usd=args.stake)

    result.print_report()

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"summary": result.summary(), "opportunities": result.opportunities}, indent=2),
            encoding="utf-8",
        )
        print(f"Results written to {out}")


if __name__ == "__main__":
    main()
