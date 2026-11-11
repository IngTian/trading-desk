"""Command-line interface: trading-desk check and trading-desk integrity.

The CLI is where strings enter the program. Every argument that can vary per trade
becomes a field on Plan or part of Context.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from desk import checks, config, integrity, model, rules, store


def _generate_trade_id(ticker: str, local_date: str, existing_ids: list[str]) -> str:
    """Generate a trade ID: ticker-YYYYMMDD, with -2/-3/... suffixes for collisions.

    The date is the trader's LOCAL calendar date (not UTC), because "today" in any day-scoped
    rule means the trader's day. A trade checked on the evening of 2026-11-10 local time gets
    an id dated 20261110, even if UTC has already rolled over to the 11th. Plan 3 fills in
    closed_today_r and the session clock; if those reach for UTC, a trade lands in the wrong
    day's count.
    """
    base = f"{ticker.lower()}-{local_date}"
    if base not in existing_ids:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing_ids:
        suffix += 1
    return f"{base}-{suffix}"




def cmd_check(args: argparse.Namespace) -> int:
    """Run checks on a planned trade and record the event."""
    # Two clocks: local date for the trade id (because "today" means the trader's day),
    # UTC timestamp for events.at (for storage and consistent comparisons).
    now_utc = datetime.now(UTC)
    local_date = datetime.now().date().strftime("%Y%m%d")

    # Create the home directory if it doesn't exist, so the error message's suggested
    # 'cp <example> <rules_path>' works without failing on 'No such file or directory'.
    config.home().mkdir(parents=True, exist_ok=True)

    # Normalize run_id early
    if args.run:
        args.run = rules.normalize_run_id(args.run)

    # Normalize ticker to upper-case at the boundary, so case variation in arguments doesn't
    # turn a rule off. Two of three string comparisons in checks.py already normalize; the
    # group-membership check does not, which is why --ticker acme silently passes a group rule
    # that should apply. Trade IDs stay lower-case (they're filename slugs). Currency is
    # normalized and validated by argparse's type function.
    args.ticker = args.ticker.upper()

    # Load rules
    rules_path = config.rules_path()
    try:
        rules_file = rules.load(rules_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    except ValueError as e:
        # Malformed rule line (no colon, etc.)
        print(str(e), file=sys.stderr)
        return 1

    # Check for --run with --account real contradiction
    if args.run and args.account == "real":
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

    # Reject --point-value 0 (the schema CHECK requires point_value > 0)
    if args.point_value is not None and args.point_value <= 0:
        print(
            "--point-value must be positive: a zero or negative point value would make every "
            "risk calculation wrong. For a stock or ETF, omit --point-value (the default is 1.0).",
            file=sys.stderr,
        )
        return 1

    # Apply defaults
    instrument = args.instrument or "stock"
    point_value = args.point_value if args.point_value is not None else 1.0

    # Scope the rules
    try:
        scoped_rules = rules_file.for_scope(args.account, args.run, checks.CHECK_NAMES)
    except ValueError as e:
        # Run section does not exist
        print(str(e), file=sys.stderr)
        return 1

    # Check for empty rules (no sections matched)
    if not scoped_rules.numbers and not scoped_rules.unchecked:
        print(f"The rules file at {rules_path} has no rules for account={args.account} "
              f"run_id={args.run}. Sections wanted: {scoped_rules.sections_wanted}. "
              f"Sections found: {sorted(rules_file._sections.keys())}",
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
        store.create(db_path)
        # Route to stderr so --json stdout stays pure
        print(f"Created book at {db_path}", file=sys.stderr)

    con = store.open()

    # Create run if needed
    if args.run:
        # Derive kind from account: real is not a run, so this is only called for paper/replay
        kind = args.account  # "paper" or "replay"
        # Build section name using the same function for_scope uses
        run_section = rules.build_run_section_name(args.account, args.run)
        run_created = store.ensure_run(
            con,
            run_id=args.run,
            kind=kind,
            started=now_utc.isoformat(),
            rules_section=run_section,
        )
        if run_created:
            # Route to stderr so --json stdout stays pure
            print(f"Created run {args.run}", file=sys.stderr)

    # Build context (bars-dependent fields None until plan 3)
    ctx = checks.Context(
        now=now_utc,
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

    # Print results (prose or JSON)
    if args.json:
        # JSON output: plan, results, ask items, sections_used, and rules hash
        # allow_nan=False is belt-and-braces: checks already withhold rather than dividing by
        # zero, but this ensures an Infinity can never escape as invalid JSON.
        output = {
            "plan": asdict(plan),
            "results": [asdict(r) for r in results],
            "ask": list(scoped_rules.ask),
            "sections_used": list(scoped_rules.sections_used),
            "rules_hash": rules_file.hash,
        }
        print(json.dumps(output, allow_nan=False))
    else:
        # Prose output: first, which rules applied and from which sections
        rule_names = sorted({r.name for r in results if r.ok is not None})
        section_names = ", ".join(scoped_rules.sections_used)
        print(f"checked {', '.join(rule_names)} — from {section_names}")

        # Report any headings that were parsed but not used (typos, duplicates, wrong case)
        unmatched = sorted(set(rules_file._sections.keys()) - set(scoped_rules.sections_used) -
                          {"Groups", "Changes"})
        if unmatched:
            print(f"unmatched sections (not used for this scope): {', '.join(unmatched)}")

        # Then one line per result
        for result in results:
            print(result.says)
        # The owner's ### Ask me questions, verbatim
        for question in scoped_rules.ask:
            print(f"Ask me: {question}")

    # Generate trade ID with collision handling
    existing_ids = store.trade_ids(con)
    trade_id = _generate_trade_id(args.ticker, local_date, existing_ids)

    # Build the payload with asdict to prevent divergence
    payload = {
        "plan": asdict(plan),
        "results": [asdict(r) for r in results],
        "ask": list(scoped_rules.ask),
        "sections_used": list(scoped_rules.sections_used),
        "rules_hash": rules_file.hash,
    }

    # Write trade row and checked event atomically
    # source="cli" because the command originates from the CLI. The product's loop is chat-driven:
    # the owner chats with an AI about a trade, the AI suggests a check, and the owner runs it via
    # the CLI. But the event's provenance is the CLI invocation, not the chat itself.
    store.add_trade_with_event(
        con,
        trade_id=trade_id,
        account=args.account,
        run_id=args.run,
        ticker=args.ticker,
        instrument=instrument,
        point_value=point_value,
        currency=args.currency,
        side=args.side,
        kind="checked",
        actor="ing",
        source="cli",
        payload=payload,
        rules_hash=rules_file.hash,
        at=now_utc.isoformat(),
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
        help="Point value: dollar value per point of price movement (default: 1.0 for stock/etf; "
             "required for futures, e.g. 100 for ES)",
    )
    def _currency_type(value: str) -> str:
        """Normalize currency to upper-case, then validate against CURRENCIES."""
        normalized = value.upper()
        if normalized not in config.CURRENCIES:
            raise argparse.ArgumentTypeError(
                f"invalid choice: {value!r} (choose from {', '.join(config.CURRENCIES)})"
            )
        return normalized

    check_parser.add_argument(
        "--currency",
        required=True,
        type=_currency_type,
        help="Currency (e.g., USD, CAD)",
    )
    check_parser.add_argument(
        "--entry",
        type=float,
        required=True,
        help="Entry price: the price you plan to enter at",
    )
    check_parser.add_argument(
        "--stop",
        type=float,
        help="Stop price: where you exit if the trade moves against you (optional but recommended)",
    )
    check_parser.add_argument(
        "--target",
        type=float,
        help="Target price: where you exit if the trade works (optional)",
    )
    check_parser.add_argument(
        "--size",
        type=int,
        help="Position size in shares or contracts (optional)",
    )
    check_parser.add_argument(
        "--horizon",
        help="Time horizon: how long you plan to hold (e.g., 1d, 2w)",
    )
    check_parser.add_argument(
        "--setup",
        help="Setup: the chart pattern or technical condition (e.g., 'breakout', 'pullback')",
    )
    check_parser.add_argument(
        "--thesis",
        help="Trade thesis: why you believe this trade will work",
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
    check_parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of prose",
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
