#!/usr/bin/env python3
"""An MCP server over the book, READ-ONLY, so an LLM can be handed a statement and reconcile.

    desk-mcp                 # speaks MCP over stdio; point an MCP client at it

The author, 2026-10-06, on the one input this tool has never been able to accept -- a broker
statement total: "i will give you account statements for you to figure it out. so you should
have MCP tools to add this reconcillation via llm."

WHY READ-ONLY, AND WHY THAT IS NOT A HALF-MEASURE. The author chose it over write access after
being told it does not, by itself, get a balance into the book. That is the right trade here.
Everything this server exposes is DERIVED -- positions, bets, the attribution residual, the
invariants -- so being wrong about it costs a confusing answer. A write is different in kind:
`add_fill` from a misread statement is a fabricated trade, and unlike a wrong balance nothing
downstream contradicts it. The book would simply believe it, which is precisely the
confident-wrong-figure failure every rule in this project exists to prevent.

So the division of labour is: the LLM READS the book and the statement, says what disagrees,
and a human makes the write. The reconciliation gets automated; the recording does not.

READ-ONLY BY CONSTRUCTION, NOT BY CONVENTION. The connection is opened `mode=ro` through a
URI, so SQLite itself refuses a write -- a bug in a tool here raises rather than corrupts. The
tools also carry `read_only_hint`, which is the declaration to the client; the URI is the
guarantee. One of those can be forgotten in review and the other cannot.

NOTHING HERE IMPORTS TEXTUAL. The TUI is the other half of the product and has no business in
a server, and keeping them apart is what lets `mcp` be an optional extra: a plain
`pip install trading-desk` is still one dependency.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import book, config


def _ro() -> sqlite3.Connection:
    """A read-only connection to the resolved book.

    `mode=ro` IS THE WHOLE SECURITY MODEL. book.connect() opens for writing, which is correct
    for the desk and wrong here, so this does not reuse it -- it re-applies only the settings
    a reader needs. sqlite3 will raise on any INSERT, UPDATE or DELETE, so "read-only" is a
    property of the connection rather than a promise about the code above it.
    """
    path = config.book_path()
    if not path.exists():
        raise FileNotFoundError(
            f"no book at {path}. Create one with `desk-migrate --new`, or set "
            f"$TRADING_DESK_BOOK to an existing one.")
    # `as_uri()`, NOT an f-string: a path containing `?` or `#` makes SQLite parse the
    # query early and SILENTLY DISCARD mode=ro, handing back a WRITABLE connection to a
    # different, newly created file. Measured, not theorised -- CREATE TABLE succeeded.
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # Foreign keys are irrelevant to a reader, but the row factory is not: desk/book.py reads
    # every column BY NAME, and a plain tuple factory makes it raise "tuple indices must be
    # integers" -- a defect this project has hit twice, in a test fixture and in check.py.
    return con


def _money(x: float | None) -> float | None:
    """Two decimals, or None. A float with fifteen digits reads as false precision to a model
    exactly as it does to a person, and every figure here is currency."""
    return None if x is None else round(x, 2)


def _pct(x: float | None) -> float | None:
    """Two decimals, or None. `filled_pct: 69.86287388431761` invites a model to quote four
    decimal places of a percentage computed from a hand-read statement total.

    NOT APPLIED TO STORED FIGURES. A mark comes back from the price source as
    261.4100036621094 -- a float32 artefact -- and it stays that way here, because rounding
    something the book RECORDED would make this server disagree with the book about what the
    book says. Only derived ratios are rounded.
    """
    return None if x is None else round(x, 2)


def build_server() -> Any:
    """The server, with its tools. Built in a function so importing this module is cheap and
    so the tests can construct one without running a transport."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    try:
        from importlib.metadata import version as _v
        ver = _v("trading-desk")
    except Exception:            # noqa: BLE001 - running from a checkout
        ver = "0+unknown"

    server = MCPServer(
        name="trading-desk",
        version=ver,
        instructions=(
            "Read-only access to a trading book kept by trading-desk: positions, bets, "
            "closed round trips, derived net worth, and a reconciliation panel.\n\n"
            "THE TASK THIS EXISTS FOR: the user hands you a broker statement; you compare it "
            "against this book and say what disagrees. `attribution` is the check that "
            "matters -- it asks whether the book can account for its own P&L against a "
            "recorded balance, and a large UNEXPLAINED means something is missing.\n\n"
            "TWO RULES WHEN REPORTING THESE FIGURES.\n"
            "1. `null` means NOT RECORDED. It never means zero and never means parity. If a "
            "figure is null, say it is unknown; do not substitute a default, and do not sum "
            "a list that contains one -- the total is unknown too.\n"
            "2. Rows are in their own currency; only totals are in the home currency. Each "
            "tool says which is which. Never add figures across currencies.\n\n"
            "You cannot write. To record a balance or a fill, tell the user what to enter."),
    )
    ro = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                         idempotent_hint=True, open_world_hint=False)

    @server.tool(
        name="book_info",
        description="Which book is open, its schema version, and row counts. Start here.",
        annotations=ro)
    def book_info() -> str:
        con = _ro()
        try:
            counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("trades", "fills", "flows", "bets", "accounts",
                                "instruments", "income", "account_snapshots")}
            return json.dumps({
                "path": str(config.book_path()),
                "why_this_book": config.book_source(),
                "schema_version": con.execute(
                    "SELECT MAX(version) FROM schema_version").fetchone()[0],
                "home_currency": book.base_currency(con),
                "counts": counts,
                "newest_balance_as_of": con.execute(
                    "SELECT MAX(as_of) FROM account_snapshots").fetchone()[0],
            }, indent=2)
        finally:
            con.close()

    @server.tool(
        name="positions",
        description=("Open legs. `*_native` figures are in the leg's own currency; "
                     "`*_base` are in the home currency. null means not recorded."),
        annotations=ro)
    def positions() -> str:
        con = _ro()
        try:
            return json.dumps([{
                "trade_id": p.trade_id, "ticker": p.ticker, "bet_id": p.bet_id,
                "account": p.account, "currency": p.currency,
                "qty": p.qty, "entry_per_share": p.entry, "mark": p.mark,
                "mark_date": p.mark_date, "opened": p.opened,
                "cost_native": _money(p.cost_native), "value_native": _money(p.value_native),
                "pnl_native": _money(p.pnl_native), "pnl_pct": _pct(p.pnl_pct_native),
                "cost_base": _money(p.cost_base), "value_base": _money(p.value_base),
                "unconfirmed_fills": p.unconfirmed_fills,
            } for p in book.positions(con)], indent=2)
        finally:
            con.close()

    @server.tool(
        name="bets",
        description=("Bets with their declared allocation, what has been spent (entry cost, "
                     "home currency), market value, and P&L. `filled_pct` is spent over "
                     "allocation."),
        annotations=ro)
    def bets() -> str:
        con = _ro()
        try:
            return json.dumps([{
                "id": b.id, "name": b.name, "status": b.status, "opened": b.opened,
                "allocation": _money(b.allocation), "allocation_ccy": b.allocation_ccy,
                "open_legs": b.legs, "spent_base": _money(b.cost),
                "value_base": _money(b.value), "filled_pct": _pct(b.filled_pct),
                "weight_pct_of_net": _pct(b.weight_pct),
                "of_allocated_pct": _pct(b.of_allocated_pct),
                "realised": _money(b.realised), "unrealised": _money(b.unrealised),
                "pnl": _money(b.pnl), "pnl_pct": _pct(b.pnl_pct),
                "closed_legs": b.closed_legs,
                "legs_that_cannot_be_priced": b.unpriced_legs,
                "note": b.note,
            } for b in book.bets(con)], indent=2)
        finally:
            con.close()

    @server.tool(
        name="closed_legs",
        description="Finished round trips: what each cost, what it returned, and when.",
        annotations=ro)
    def closed_legs() -> str:
        con = _ro()
        try:
            return json.dumps([{
                "trade_id": c.trade_id, "ticker": c.ticker, "bet_id": c.bet_id,
                "currency": c.currency, "opened": c.opened, "closed": c.closed,
                "cost": _money(c.cost), "realised": _money(c.realised),
            } for c in book.closed_legs(con)], indent=2)
        finally:
            con.close()

    @server.tool(
        name="net_worth",
        description=("Derived net worth and its parts, in the home currency. Withheld "
                     "entirely (null) rather than computed partially when a leg cannot be "
                     "valued."),
        annotations=ro)
    def net_worth() -> str:
        con = _ro()
        try:
            n = book.net(con)
            return json.dumps({
                "home_currency": n.home,
                "net": _money(n.net),
                "cash": _money(n.cash),
                "positions_at_mark": _money(n.positions),
                "contributed": _money(n.contributed),
                "parked_in_money_market": _money(book.parked(con)),
            }, indent=2)
        finally:
            con.close()

    @server.tool(
        name="attribution",
        description=("THE RECONCILIATION. Asks whether the book can account for its own "
                     "P&L against the newest recorded balance: realised + income + "
                     "unrealised + fx should equal balance minus contributions. A large "
                     "UNEXPLAINED means a fill, fee, dividend or conversion is unrecorded. "
                     "Reports STALE when the balance predates recent fills, in which case "
                     "the residual compares two different days and means nothing."),
        annotations=ro)
    def attribution() -> str:
        from . import check as bookcheck
        con = _ro()
        # A SINK, NOT redirect_stdout, and this file got that wrong first time round. The
        # comment here used to claim redirecting was safe "because this runs on the server's
        # own thread with nothing else printing". It does not: the SDK dispatches each call as
        # its own task and runs sync tool bodies on worker threads. Six concurrent calls
        # returned five different lengths, one of them the string "attribution printed
        # nothing" -- a false answer about the one thing this server exists to report -- and
        # sys.stdout was left replaced for the life of the process.
        #
        # A lock would only narrow that window. desk/prices.py made the same mistake and the
        # same fix; check.attribution takes `emit` now for exactly this caller.
        lines: list[str] = []
        try:
            bookcheck.attribution(con, emit=lines.append)
        finally:
            con.close()
        # NO "printed nothing" FALLBACK. An empty capture was a symptom of the bug above, and
        # paraphrasing it as a result is how a model was handed a confident non-answer.
        return "\n".join(lines)

    @server.tool(
        name="integrity_check",
        description=("Runs the book's invariants -- the things a CHECK constraint cannot "
                     "see, like a closed trade still holding shares. Returns the failures, "
                     "or says all of them hold."),
        annotations=ro)
    def integrity_check() -> str:
        from . import check as bookcheck
        con = _ro()
        try:
            failures = []
            for name, query, why in bookcheck.INVARIANTS:
                rows = query(con) if callable(query) else con.execute(query).fetchall()
                if rows:
                    failures.append({
                        "invariant": name, "why_it_matters": why,
                        "offending": [r if isinstance(r, str) else r[0] for r in rows][:20],
                    })
            return json.dumps({
                "invariants_checked": len(bookcheck.INVARIANTS),
                "failures": failures,
                "verdict": "all hold" if not failures else f"{len(failures)} failed",
            }, indent=2)
        finally:
            con.close()

    return server


def main() -> int:
    """The `desk-mcp` console script. Speaks MCP over stdio."""
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
