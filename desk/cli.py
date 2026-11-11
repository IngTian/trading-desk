"""Command-line interface: trading-desk check and trading-desk integrity.

The CLI is where strings enter the program. Every argument that can vary per trade
becomes a field on Plan or part of Context.
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from desk import checks, config, integrity, model, rules, store


def _generate_trade_id(ticker: str, now: datetime, existing_ids: list[str]) -> str:
    """Generate a trade ID: ticker-YYYYMMDD, with -2/-3/... suffixes for collisions."""
    base = f"{ticker.lower()}-{now:%Y%m%d}"
    if base not in existing_ids:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing_ids:
        suffix += 1
    return f"{base}-{suffix}"


def _create_run_if_needed(con, run_id: str, now: datetime) -> bool:
    """Create a runs row if it doesn't exist. Returns True if created."""
    existing = con.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
    if existing:
        return False
    # Extract the run identifier from run-N format: "run-5" -> "run 5"
    run_section = run_id.replace("-", " ")  # "run-5" -> "run 5"
    con.execute(
        """
        INSERT INTO runs (id, kind, started, rules_section)
        VALUES (?, ?, ?, ?)
        """,
        (run_id, "paper", now.isoformat(), f"Paper — {run_section}"),
    )
    con.commit()
    return True


def cmd_check(args: argparse.Namespace) -> int:
    """Run checks on a planned trade and record the event."""
    now = datetime.now(UTC)

    # Load rules
    rules_path = config.rules_path()
    try:
        rules_file = rules.load(rules_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    # Check for --run with --account real contradiction
    if args.run is not None and args.account == "real":
        print("--run cannot be used with --account real (runs are paper or replay constructs)",
              file=sys.stderr)
        return 1

    # Require --point-value when --instrument future
    if args.instrument == "future" and args.point_value is None:
        print(
            "--point-value is required when --instrument future: risk is size × stop distance × "
            "point value, so a futures contract at 100 per point makes a 1.5-point stop a 150 risk "
            "rather than 1.5. Defaulting to 1.0 would under-report risk by 100×.",
            file=sys.stderr,
        )
        return 1

    # Apply defaults
    instrument = args.instrument or "stock"
    point_value = args.point_value if args.point_value is not None else 1.0

    # Scope the rules
    scoped_rules = rules_file.for_scope(args.account, args.run, checks.CHECK_NAMES)

    # Check for empty rules (no sections matched)
    if not scoped_rules.numbers and not scoped_rules.unchecked:
        print(f"The rules file at {rules_path} has no rules for account={args.account} "
              f"run_id={args.run}. Sections found: {list(rules_file._sections.keys())}",
              file=sys.stderr)
        return 1

    # Build the plan
    plan = model.Plan(
        ticker=args.ticker,
        side=args.side,
        instrument=instrument,
        point_value=point_value,
        currency=args.currency,
        entry=args.entry,
        stop=args.stop,
        target=args.target,
        size=args.size,
        horizon=args.horizon,
        setup=args.setup,
        thesis=args.thesis,
        account=args.account,
        run_id=args.run,
    )

    # Open or create the database
    db_path = config.db_path()
    if not db_path.exists():
        store.create()
        print(f"Created book at {db_path}")

    con = store.open()

    # Create run if needed
    if args.run:
        run_created = _create_run_if_needed(con, args.run, now)
        if run_created:
            print(f"Created run {args.run}")

    # Build context (bars-dependent fields None until plan 3)
    ctx = checks.Context(
        now=now,
        open_trades=model.open_trades(con),
        session_started=None,
        closed_today_r=None,
        median_candle_range=None,
        volatility_ratio=None,
        stopped_closes=model.stopped_closes(con),
        fx=checks.no_fx,
    )

    # Run the checks
    results = checks.run_checks(plan, scoped_rules, ctx)

    # Print results
    for result in results:
        print(result.says)

    # Generate trade ID with collision handling
    existing_ids = store.trade_ids(con)
    trade_id = _generate_trade_id(args.ticker, now, existing_ids)

    # Create the trade row
    store.add_trade(
        con,
        trade_id=trade_id,
        account=args.account,
        run_id=args.run,
        ticker=args.ticker,
        instrument=instrument,
        point_value=point_value,
        currency=args.currency,
        side=args.side,
    )

    # Build the payload with results serialized to dicts
    payload = {
        "plan": {
            "ticker": plan.ticker,
            "side": plan.side,
            "entry": plan.entry,
            "stop": plan.stop,
            "target": plan.target,
            "size": plan.size,
            "horizon": plan.horizon,
            "setup": plan.setup,
            "thesis": plan.thesis,
            "instrument": plan.instrument,
            "point_value": plan.point_value,
            "currency": plan.currency,
        },
        "results": [
            {
                "name": r.name,
                "ok": r.ok,
                "figure": r.figure,
                "limit": r.limit,
                "says": r.says,
                "unit": r.unit,
            }
            for r in results
        ],
    }

    # Append the checked event
    store.append(
        con,
        kind="checked",
        actor="ing",
        source="chat",
        trade_id=trade_id,
        payload=payload,
        rules_hash=rules_file.hash,
        at=now.isoformat(),
    )

    return 0


def cmd_integrity(args: argparse.Namespace) -> int:
    """Run integrity checks on the event log."""
    db_path = config.db_path()
    if not db_path.exists():
        print(f"No database at {db_path}", file=sys.stderr)
        return 1

    con = store.open(read_only=True)
    return integrity.run(con)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        prog="trading-desk",
        description="A trading book in the terminal: check trades against rules, "
                    "record the event log, and verify integrity.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # check subcommand
    check_parser = subparsers.add_parser(
        "check",
        help="Check a planned trade against rules and record the event",
    )
    check_parser.add_argument("--ticker", required=True, help="Ticker symbol")
    check_parser.add_argument(
        "--side",
        required=True,
        choices=("long", "short"),
        help="Trade side (long or short)",
    )
    check_parser.add_argument(
        "--instrument",
        choices=("stock", "etf", "future"),
        default=None,
        help="Instrument type (default: stock)",
    )
    check_parser.add_argument(
        "--point-value",
        type=float,
        default=None,
        help="Point value (default: 1.0 for stock/etf; required for future)",
    )
    check_parser.add_argument(
        "--currency",
        required=True,
        help="Currency (e.g., USD, CAD)",
    )
    check_parser.add_argument(
        "--entry",
        type=float,
        required=True,
        help="Entry price",
    )
    check_parser.add_argument(
        "--stop",
        type=float,
        help="Stop price (optional)",
    )
    check_parser.add_argument(
        "--target",
        type=float,
        help="Target price (optional)",
    )
    check_parser.add_argument(
        "--size",
        type=int,
        help="Position size in shares/contracts (optional)",
    )
    check_parser.add_argument(
        "--horizon",
        help="Time horizon (e.g., 1d, 2w)",
    )
    check_parser.add_argument(
        "--setup",
        help="Setup name/description",
    )
    check_parser.add_argument(
        "--thesis",
        help="Trade thesis",
    )
    check_parser.add_argument(
        "--account",
        required=True,
        choices=("real", "paper", "replay"),
        help="Account type",
    )
    check_parser.add_argument(
        "--run",
        help="Run ID (e.g., run-1) for paper/replay trades",
    )

    # integrity subcommand
    subparsers.add_parser(
        "integrity",
        help="Run integrity checks on the event log",
    )

    args = parser.parse_args(argv)

    if args.command == "check":
        return cmd_check(args)
    elif args.command == "integrity":
        return cmd_integrity(args)
    else:
        parser.print_help()
        return 1
