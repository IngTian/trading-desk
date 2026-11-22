"""Command-line interface: `trading-desk mcp`, the desk's one interface, and `trading-desk
integrity`. Recording goes through desk/api.py."""
from __future__ import annotations

import argparse
import sys

from desk import config, integrity, store
from desk.errors import Refused


def cmd_integrity(args: argparse.Namespace) -> int:
    """Run the integrity checks: exit 1 on any violation, never the raw count (exit codes
    wrap at 256, so 256 violations would read as success)."""
    try:
        config.refuse_legacy_db()
        con = store.open(read_only=True)
    except FileNotFoundError:   # store.open's text names store.create(), which a user cannot call
        path = config.db_path()
        try:
            config.refuse_if_in_icloud(path)
        except Refused as e:
            print(e, file=sys.stderr)
            return 1
        print(f"There is no desk.db at {path}; the desk creates it when its MCP server first "
              "starts (trading-desk mcp).", file=sys.stderr)
        return 1
    except (Refused, store.NotADeskDatabase) as e:
        print(e, file=sys.stderr)
        return 1
    with con:
        return 1 if integrity.run(con) else 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Serve the desk's 16 tools over stdio until the client closes stdin."""
    from desk import server  # the MCP SDK is imported only by the command that serves it
    try:
        return server.serve()
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trading-desk",
        description="A trading record in SQLite: strategies, runs and trades, verified by "
                    "its integrity checks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp", help="Serve the desk's tools to an MCP client over stdio")
    sub.add_parser("integrity", help="Run integrity checks on the event log")
    args = parser.parse_args(argv)
    return {"mcp": cmd_mcp, "integrity": cmd_integrity}[args.command](args)
