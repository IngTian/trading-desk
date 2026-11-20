"""Command-line interface: `trading-desk integrity`. Recording goes through desk/api.py."""
from __future__ import annotations

import argparse
import sys

from desk import integrity, store


def cmd_integrity(args: argparse.Namespace) -> int:
    """Run the integrity checks: exit 1 on any violation, never the raw count (exit codes
    wrap at 256, so 256 violations would read as success)."""
    try:
        con = store.open(read_only=True)
    except (FileNotFoundError, store.NotADeskDatabase) as e:
        print(e, file=sys.stderr)
        return 1
    with con:
        return 1 if integrity.run(con) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trading-desk",
        description="A trading record in SQLite: strategies, runs and trades, verified by "
                    "its integrity checks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("integrity", help="Run integrity checks on the event log")
    args = parser.parse_args(argv)
    return {"integrity": cmd_integrity}[args.command](args)
