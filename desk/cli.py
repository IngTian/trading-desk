"""Command-line interface: trading-desk check and trading-desk integrity.

The CLI is where strings enter the program. Every argument that can vary per trade
becomes a field on Plan or part of Context.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from desk import checks, config, fmt, integrity, model, rules, store


def _price(value: str) -> float:
    """A price argument: a finite number above zero, refused at the boundary otherwise.

    float() accepts "nan", "inf" and "-9". None of them is a price, and each one used to reach
    a check or the log: a NaN entry printed a full set of results and then crashed on writing
    the event, and a negative stop produced a risk figure for a trade that cannot exist.
    """
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a price: a price must be a positive number")
    return number


def _size(value: str) -> int:
    """A size argument: a whole number of shares or contracts above zero.

    An exit of 0 recorded nothing, and an exit of -3 GREW the open position by 3 while
    printing nothing unusual.
    """
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a size: a size must be a positive whole number")
    return number


# A continuous futures contract as TradingView writes it ("GC1!", "MNQ2!") and as Yahoo does
# ("GC=F"), optionally behind an exchange prefix ("COMEX:GC1!").
# A root may start with a digit (6E is a currency future), so the root is any run of letters
# and digits, matched lazily so the contract number is left for the suffix.
_CONTINUOUS = re.compile(r"^(?:[A-Z0-9_]+:)?([A-Z0-9]+?)(?:\d+!|=F)$")


def canonical_ticker(typed: str) -> tuple[str, str | None]:
    """(ticker, instrument it implies): "GC1!" -> ("GC", "future"), "acme" -> ("ACME", None).

    One trade, one ticker. Typing GC once and GC1! the next time made them two tickers, so a
    stop-out on one never cooled the other down, and an id built from "GC1!" carried a "!"
    that zsh expands. And the form itself says it is a future, which is the fact that a
    forgotten --instrument flag used to lose: GC1! checked as a stock worth 1 per point
    reported 100x too little risk and passed a futures ban. A bare root ("GC") says nothing,
    so it is taken as typed.
    """
    upper = typed.strip().upper()
    continuous = _CONTINUOUS.match(upper)
    if continuous:
        return continuous.group(1), "future"
    return upper, None


def _generate_trade_id(ticker: str, local_date: str, existing_ids: list[str]) -> str:
    """Generate a trade ID: ticker-YYYYMMDD, with -2/-3/... suffixes for collisions.

    The date is the trader's LOCAL calendar date (not UTC), because "today" in any day-scoped
    rule means the trader's day. A trade checked on the evening of 2026-11-10 local time gets
    an id dated 20261110, even if UTC has already rolled over to the 11th. Plan 3 fills in
    closed_today_r and the session clock; if those reach for UTC, a trade lands in the wrong
    day's count.

    Only letters and digits survive from the ticker, so an id can be typed unquoted in any
    shell: "!" is zsh's history expansion, and "." or "=" would need care too.
    """
    base = f"{re.sub(r'[^a-z0-9]', '', ticker.lower())}-{local_date}"
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

    # Normalize the ticker at the boundary, so the way it was typed cannot turn a rule off:
    # upper case (--ticker acme once passed a group rule it was in), and a continuous futures
    # contract reduced to its root and marked a future (see canonical_ticker). Trade IDs stay
    # lower-case. Currency is normalized and validated by argparse's type function.
    args.ticker, implied = canonical_ticker(args.ticker)
    if implied == "future":
        if args.instrument not in (None, "future"):
            print(
                f"--ticker is a futures contract, so it cannot be checked as --instrument "
                f"{args.instrument}: a future's risk is multiplied by its point value and a "
                f"{args.instrument}'s is not.",
                file=sys.stderr,
            )
            return 1
        args.instrument = "future"

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

    # Require --point-value for a future, whether --instrument said so or the ticker did
    if args.instrument == "future" and args.point_value is None:
        print(
            "--point-value is required for a future: risk is size × stop distance × "
            "point value, so a futures contract at 100 per point makes a 1.5-point stop a 150 risk "
            "rather than 1.5. Defaulting to 1.0 would under-report risk by 100×.",
            file=sys.stderr,
        )
        return 1

    # Reject --point-value 0 (the schema CHECK requires point_value > 0), and NaN, which
    # compares false to everything and so slipped past `<= 0` into a traceback.
    if args.point_value is not None and (
            not math.isfinite(args.point_value) or args.point_value <= 0):
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

    # Build context. Bars-dependent fields stay None until plan 3, and the session start until
    # sessions exist in plan 4: withheld, not defaulted.
    ctx = checks.Context(
        now=now_utc,
        open_trades=model.open_trades(con, account=args.account, run_id=args.run),
        session_started=None,
        closed_today_r=model.realised_r_on(
            con, now_utc.astimezone().date(), account=args.account, run_id=args.run),
        median_candle_range=None,
        volatility_ratio=None,
        stopped_closes=model.stopped_closes(con, account=args.account, run_id=args.run),
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

        # Report any sections that match no recognized shape (typos, wrong case)
        # Recognized: All, Real/Paper/Replay, "<Account> — run <anything>", Groups, Changes
        def _is_recognized(section_name: str) -> bool:
            """True if the section name matches a recognized shape."""
            if section_name in ("All", "Groups", "Changes"):
                return True
            if section_name in ("Real", "Paper", "Replay"):
                return True
            # Match "<Account> — run <anything>"
            if " — run " in section_name:
                account_part = section_name.split(" — run ")[0]
                if account_part in ("Real", "Paper", "Replay"):
                    return True
            return False

        unrecognised = sorted([
            s for s in rules_file._sections.keys()
            if not _is_recognized(s)
        ])
        for section_name in unrecognised:
            print(
                f"unrecognised section '{section_name}': it matches no account or run, "
                f"so none of its rules will ever apply"
            )

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
        actor="owner",
        source="cli",
        payload=payload,
        rules_hash=rules_file.hash,
        at=now_utc.isoformat(),
    )

    # Print the trade id so the user knows what to pass to entered/exited/stop-moved/closed
    print(f"recorded as {trade_id}", file=sys.stderr)

    return 0


def cmd_integrity(args: argparse.Namespace) -> int:
    """Run integrity checks on the event log."""
    db_path = config.db_path()
    if not db_path.exists():
        print(f"No database at {db_path}", file=sys.stderr)
        return 1

    con = store.open(read_only=True)
    return integrity.run(con)


def _unit_name(instrument: str) -> str:
    """Return the unit name for an instrument: shares for stock/etf, contracts for futures."""
    return "contracts" if instrument == "future" else "shares"


def cmd_entered(args: argparse.Namespace) -> int:
    """Record an entered event: the trade was actually taken at these terms."""
    db_path = config.db_path()
    if not db_path.exists():
        print(f"No database at {db_path}", file=sys.stderr)
        return 1

    con = store.open()
    trade_id = args.trade_id

    # Check if trade exists
    trade_row = store.trade(con, trade_id)
    if not trade_row:
        print(f"Unknown trade id: {trade_id}", file=sys.stderr)
        return 1

    # Check if trade already has an entered event
    events = store.events(con, trade_id=trade_id)
    entered_events = [e for e in events if e["kind"] == "entered"]
    if entered_events:
        entered_at = entered_events[0]["at"]
        entered_date = datetime.fromisoformat(entered_at).date().isoformat()
        print(
            f"Trade {trade_id} already entered on {entered_date}; "
            f"a revised entry needs a correction, which this version cannot record.",
            file=sys.stderr
        )
        return 1

    # A stop at the entry is no risk at all, and 1R is measured against the stop at entry for
    # the trade's whole life -- so every R this trade ever produced would be a division by zero.
    # (Moving the stop to the entry LATER is routine and fine: R keeps the stop at entry.)
    if args.stop == args.entry:
        print(
            f"Trade {trade_id}: a stop at the entry ({args.stop:g}) means no risk, so the trade "
            f"has no 1R to measure it in. Record the stop you actually had when you entered; "
            f"if you have since moved it to breakeven, record that with stop-moved.",
            file=sys.stderr,
        )
        return 1

    # Build the payload
    payload = {
        "entry": args.entry,
        "stop": args.stop,
        "target": args.target,
        "size": args.size,
        "horizon": args.horizon,
        "setup": args.setup,
        "thesis": args.thesis,
    }

    # Record the event
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="cli",
        trade_id=trade_id,
        payload=payload,
    )

    # Print the resulting state
    state = model.state_of(con, trade_id)
    unit = _unit_name(trade_row["instrument"])
    print(f"{trade_id}: {state.size_open} {unit} open")
    return 0


def cmd_exited(args: argparse.Namespace) -> int:
    """Record an exited event: a partial or full exit at this price and size."""
    db_path = config.db_path()
    if not db_path.exists():
        print(f"No database at {db_path}", file=sys.stderr)
        return 1

    con = store.open()
    trade_id = args.trade_id

    # Check if trade exists
    trade_row = store.trade(con, trade_id)
    if not trade_row:
        print(f"Unknown trade id: {trade_id}", file=sys.stderr)
        return 1

    # Check if trade has an entered event
    state = model.state_of(con, trade_id)
    if state.opened_at is None:
        print(
            f"Trade {trade_id} has no entered event; cannot exit a trade that was never entered.",
            file=sys.stderr
        )
        return 1

    # Check if exit size exceeds open size
    unit = _unit_name(trade_row["instrument"])
    if state.size_open is None or args.size > state.size_open:
        print(
            f"Trade {trade_id}: cannot exit {args.size} {unit} when only "
            f"{state.size_open} are open.",
            file=sys.stderr
        )
        return 1

    # Build the payload
    payload = {
        "price": args.price,
        "size": args.size,
    }

    # Record the event
    store.append(
        con,
        kind="exited",
        actor="owner",
        source="cli",
        trade_id=trade_id,
        payload=payload,
    )

    # Print the resulting state: what is still open, and what has been realised so far, in
    # money (points x size x point value, so a future reads in dollars, not points) and in R.
    state = model.state_of(con, trade_id)
    held = (f"{state.size_open} {unit} open" if state.size_open and state.size_open > 0
            else "flat")
    r = "R withheld: the trade had no risk at entry" if state.realised_r is None \
        else f"{fmt.signed(state.realised_r)} R"
    print(f"{trade_id}: {held}, realised {fmt.signed(state.realised_cash)} {state.currency} "
          f"({r})")
    return 0


def cmd_stop_moved(args: argparse.Namespace) -> int:
    """Record a stop_moved event: the stop was adjusted to this price."""
    db_path = config.db_path()
    if not db_path.exists():
        print(f"No database at {db_path}", file=sys.stderr)
        return 1

    con = store.open()
    trade_id = args.trade_id

    # Check if trade exists
    trade_row = store.trade(con, trade_id)
    if not trade_row:
        print(f"Unknown trade id: {trade_id}", file=sys.stderr)
        return 1

    # Check if trade has an entered event
    state = model.state_of(con, trade_id)
    if state.opened_at is None:
        print(
            f"Trade {trade_id} has no entered event; "
            f"cannot move the stop on a trade that was never entered.",
            file=sys.stderr
        )
        return 1
    if state.status == "closed" or (state.size_open is not None and state.size_open <= 0):
        # A flat trade has no stop to manage. Recording one anyway is not harmless: it reads
        # like a live order on a finished trade.
        print(
            f"Trade {trade_id} is flat; there is no open position whose stop could move.",
            file=sys.stderr,
        )
        return 1

    # Build the payload
    payload = {
        "stop": args.stop,
    }

    # Record the event
    store.append(
        con,
        kind="stop_moved",
        actor="owner",
        source="cli",
        trade_id=trade_id,
        payload=payload,
    )

    # Print confirmation
    state = model.state_of(con, trade_id)
    print(f"{trade_id}: stop moved to {state.stop}")
    return 0


def cmd_closed(args: argparse.Namespace) -> int:
    """Record a closed event: this trade is finished, no longer being tracked."""
    db_path = config.db_path()
    if not db_path.exists():
        print(f"No database at {db_path}", file=sys.stderr)
        return 1

    con = store.open()
    trade_id = args.trade_id

    # Check if trade exists
    trade_row = store.trade(con, trade_id)
    if not trade_row:
        print(f"Unknown trade id: {trade_id}", file=sys.stderr)
        return 1

    # Check if trade is still holding size
    state = model.state_of(con, trade_id)
    unit = _unit_name(trade_row["instrument"])
    if state.size_open is not None and state.size_open > 0:
        print(
            f"Trade {trade_id} still holding {state.size_open} {unit}; "
            f"exit them first before closing.",
            file=sys.stderr
        )
        return 1

    # Build the payload (empty for closed)
    payload = {}

    # Record the event
    store.append(
        con,
        kind="closed",
        actor="owner",
        source="cli",
        trade_id=trade_id,
        payload=payload,
    )

    # Print confirmation
    print(f"{trade_id}: closed")
    return 0


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
        type=_price,
        required=True,
        help="Entry price: the price you plan to enter at",
    )
    check_parser.add_argument(
        "--stop",
        type=_price,
        help="Stop price: where you exit if the trade moves against you (optional but recommended)",
    )
    check_parser.add_argument(
        "--target",
        type=_price,
        help="Target price: where you exit if the trade works (optional)",
    )
    check_parser.add_argument(
        "--size",
        type=_size,
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

    # entered subcommand
    entered_parser = subparsers.add_parser(
        "entered",
        help="Record that a trade was actually taken at these terms",
    )
    entered_parser.add_argument("trade_id", help="Trade ID (from check command)")
    entered_parser.add_argument(
        "--entry", type=_price, required=True,
        help="Entry price: the price the trade was actually entered at"
    )
    entered_parser.add_argument(
        "--stop", type=_price, required=True,
        help="Stop price: where you will exit if the trade moves against you"
    )
    entered_parser.add_argument(
        "--target", type=_price, required=True,
        help="Target price: where you will exit if the trade works"
    )
    entered_parser.add_argument(
        "--size", type=_size, required=True,
        help="Position size in shares or contracts"
    )
    entered_parser.add_argument(
        "--horizon",
        help="Time horizon: how long you plan to hold (e.g., 1d, 2w)"
    )
    entered_parser.add_argument(
        "--setup",
        help="Setup: the chart pattern or technical condition"
    )
    entered_parser.add_argument(
        "--thesis",
        help="Trade thesis: why you believe this trade will work"
    )

    # exited subcommand
    exited_parser = subparsers.add_parser(
        "exited",
        help="Record that shares were exited at this price",
    )
    exited_parser.add_argument("trade_id", help="Trade ID")
    exited_parser.add_argument("--price", type=_price, required=True,
                              help="Exit price: the price at which shares were sold")
    exited_parser.add_argument("--size", type=_size, required=True,
                              help="Number of shares exited")

    # stop-moved subcommand
    stop_moved_parser = subparsers.add_parser(
        "stop-moved",
        help="Record that the stop was adjusted to a new price",
    )
    stop_moved_parser.add_argument("trade_id", help="Trade ID")
    stop_moved_parser.add_argument("--stop", type=_price, required=True,
                                  help="New stop price")

    # closed subcommand
    closed_parser = subparsers.add_parser(
        "closed",
        help="Record that a trade is finished and no longer being tracked",
    )
    closed_parser.add_argument("trade_id", help="Trade ID")

    args = parser.parse_args(argv)
    # Ids are generated in lower case, so an id typed in any case names the same trade.
    if getattr(args, "trade_id", None):
        args.trade_id = args.trade_id.strip().lower()

    if args.command == "check":
        return cmd_check(args)
    elif args.command == "integrity":
        return cmd_integrity(args)
    elif args.command == "entered":
        return cmd_entered(args)
    elif args.command == "exited":
        return cmd_exited(args)
    elif args.command == "stop-moved":
        return cmd_stop_moved(args)
    elif args.command == "closed":
        return cmd_closed(args)
    else:
        parser.print_help()
        return 1
