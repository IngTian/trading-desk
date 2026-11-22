"""`trading-desk mcp`: desk/api.py's 14 functions as MCP tools over stdio, with
desk/instructions.md as the server's `instructions`.

On the MCP SDK's low-level server, never MCPServer: MCPServer coerces arguments ("1" and true
reach an integer parameter as 1), and the API refuses a wrong type itself (design §9). The SDK
frames messages, speaks both protocol eras, answers ping, skips bad lines, and points fd 0 and
fd 1 away from the wire while serving. The desk adds what it does not: Python's buffered prints
kept off the wire, one call at a time in a worker thread, and replies in strict JSON.
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib.metadata
import json
import math
import sqlite3
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import anyio
import mcp.types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from desk import api, config, store
from desk.errors import Refused, shown


@dataclass(frozen=True)
class Tool:
    fn: Callable                # the desk.api function it calls
    description: str
    params: dict[str, dict]     # parameter -> its JSON Schema, each with a "description"
    required: tuple[str, ...] = ()


def _p(type_: str, description: str, **schema) -> dict:
    return {"type": type_, "description": description, **schema}


_TIME = ("a UTC ISO-8601 time with its offset, e.g. 2026-03-02T14:30:00Z; required in a replay "
         "run, now by default in a live one")


def _at(what: str) -> dict:
    return _p("string", f"{what}: {_TIME}.")


_TRADE = _p("string", "The trade's id, e.g. acme-20260302, as trade or list_trades gave it.")
_VERSION = _p("integer", "A version of the strategy, from 1; left out: the latest.", minimum=1)

TOOLS: dict[str, Tool] = {
    "save_strategy": Tool(api.save_strategy, (
        "Save a strategy's Markdown text: front matter with `name:`, prose, an optional "
        "`## Checklist` section whose top-level bullets are the items answered at each entry, and "
        "exactly one fenced `rules` block in TOML, the rules every trade is checked against. A "
        "new id makes version 1. An existing id needs `if_current`, the `current_hash` from "
        "get_strategy, so a save never overwrites a change made since you read it; any change to "
        "the text then makes a new version (spacing alone does not), and a saved version never "
        "changes. A rules block that does not parse is refused, naming the key or line and the "
        "fix. The reply is the strategy at its latest version, with `saved`: \"version N "
        "created\" or \"unchanged\"."), {
        "id": _p("string", "The strategy's id: 1 to 64 lowercase letters, digits and hyphens, "
                 "e.g. dip-buyer, fixed at its first save.", pattern="^[a-z0-9-]{1,64}$"),
        "markdown": _p("string", "The strategy's whole Markdown text."),
        "if_current": _p("string", "The current_hash get_strategy gave: required to save over "
                         "an existing id, left out for a new one."),
    }, ("id", "markdown")),
    "get_strategy": Tool(api.get_strategy, (
        "One version of a strategy (default: the latest): its text; its rules as parsed; its "
        "timeframes, the bar lengths its rules read, finest first (e.g. 15m, 1h); its checklist "
        "items; `versions`, how many it has; and `current_hash`, which save_strategy takes as "
        "`if_current` to save a change. `market_keys`: every key its rules read from `market`, "
        "in the rules' order with the facts last."), {
        "id": _p("string", "The strategy's id."),
        "version": _VERSION,
    }, ("id",)),
    "list_strategies": Tool(api.list_strategies, (
        "Every strategy, in id order, each at its latest version, with the fields get_strategy "
        "gives."), {}),
    "start_run": Tool(api.start_run, (
        "Deploy a strategy version as a run, `<strategy>-r<N>`: one version traded over a "
        "period, live (in real time) or replay (re-running past history). Every trade's outcome "
        "comes from your reports of it in either mode; the only difference is that `at` defaults "
        "to now on a live run and is required on a replay one. A run trades in one "
        "currency: every trade on it is in that currency, and the rules' amounts are read in it. "
        "Tags are free labels, kept verbatim and never interpreted; stats can narrow by them. "
        "Several runs may be open at once. The reply is the run, with its id and `started`."), {
        "strategy": _p("string", "The id of the strategy to deploy."),
        "mode": _p("string", "live: real time; replay: re-running past history.",
                   enum=["live", "replay"]),
        "currency": _p("string", "The one currency every trade on the run is in.",
                       enum=["CAD", "USD"]),
        "tags": _p("array", "Free labels for the run, kept verbatim and never interpreted, e.g. "
                   "[\"breakout\"].", items={"type": "string"}),
        "version": _VERSION,
        "at": _at("When the run starts"),
    }, ("strategy", "mode", "currency")),
    "stop_run": Tool(api.stop_run, (
        "Stop a run: no trade may be entered at or after `at`, its pending limit orders (orders "
        "to trade only at a set price or better, not yet filled) read cancelled with why \"run "
        "stopped\", and open trades stay open until their exit is reported. It is refused before "
        "the run's start, and at or before a trade's entry. The reply is the run, with `stopped`, "
        "and `cancelled`: the ids of the limit orders the stop cancelled, in recording order. It "
        "cannot be undone."), {
        "run": _p("string", "The run's id, e.g. dip-buyer-r1."),
        "at": _at("When the run stops"),
    }, ("run",)),
    "list_runs": Tool(api.list_runs, (
        "Every run, in the order created, or one strategy's: each with its strategy, version, "
        "mode, tags, currency, `started`, and `stopped` (null while it runs)."), {
        "strategy": _p("string", "A strategy's id, to list only its runs; left out: every run."),
    }),
    "trade": Tool(api.trade, (
        "Enter a trade on a run, checked against the rules of the run's version as of `at`. "
        "The results are recorded with it, and a breach never refuses it: each result's `ok` is "
        "true (kept), false (breached) or null (unknown), and `says` gives the fact or what is "
        "missing. `check_only: true` runs the same checks and records nothing (a screenshot is "
        "read but not copied); its reply is a CheckView: the `checks` and `checklist` an entry "
        "would be shown, and the trade's identity (run, at, ticker, side, instrument, "
        "point_value, currency), to show the user before they commit. Otherwise each "
        "screenshot file is copied into the desk's own storage when the trade is recorded, and "
        "the reply is the trade: its id "
        "(`<ticker>-<YYYYMMDD>`), its decisions, its outcome and `checks_shown`.\n\n"
        "Terms. Long profits if the price rises, short if it falls. A market entry fills at "
        "once at its price; a limit entry fills only at its price or better, and stays pending "
        "until it fills or is cancelled. The stop is the price that exits a losing position, "
        "the target the price that takes the profit, and the risk, 1R, is the distance from the "
        "entry to the stop. A future's `point_value` is the cash a one-point move is worth on "
        "one contract; a continuous ticker such as GC1! reads as a future. `market` holds the "
        "figures the rules read that you supply: indicator keys such as rsi_14_15m and the "
        "facts the strategy declares; a key its rules do not read is refused. RSI is the "
        "relative strength index, a 0 to 100 reading of recent gains against recent losses. "
        "`checklist` answers the version's checklist items, matched by their exact text.\n\n"
        "Before an entry whose rules read market keys, take them from get_strategy's "
        "market_keys and supply each, read off the last completed bar: a key left out reads "
        "unknown for good, since market is not corrected. The rules that read the run's other "
        "trades count this run only, and max_consecutive_losses counts within the day."), {
        "run": _p("string", "The id of the run the trade is on, e.g. dip-buyer-r1."),
        "ticker": _p("string", "The symbol, as the user's chart names it, e.g. ACME or GC1!."),
        "side": _p("string", "long: profit if the price rises; short: profit if it falls.",
                   enum=["long", "short"]),
        "entry_type": _p("string", "market: filled at once at the entry price; limit: an order "
                         "filled only at the entry price or better.", enum=["market", "limit"]),
        "entry": _p("number", "The entry price: a market entry's fill, a limit's price."),
        "size": _p("integer", "How many shares, or contracts for a future; above zero."),
        "currency": _p("string", "The trade's currency, which is its run's.",
                       enum=["CAD", "USD"]),
        "reasons": _p("string", "Why the user takes the trade, exactly as they wrote it."),
        "stop": _p("number", "The stop: the price that exits the position to cap a loss; "
                   "left out: none."),
        "target": _p("number", "The target: the price that takes the profit; left out: none."),
        "screenshots": _p("array", "Charts of the trade, each a full file path or an https:// "
                          "link.", items={"type": "string"}),
        "at": _at("When the trade was entered"),
        "instrument": _p("string", "stock, etf (an exchange-traded fund) or future; required "
                         "unless the ticker is a continuous future such as GC1!, which reads as "
                         "a future.", enum=["stock", "etf", "future"]),
        "point_value": _p("number", "A future's, and required for one: the cash a one-point "
                          "move is worth on one contract, e.g. 100."),
        "market": _p("object", "The figures the rules read that you supply, by key, each a "
                     "number read at the bar acted on, e.g. {\"rsi_14_15m\": 28.5}.",
                     additionalProperties={"type": "number"}),
        "checklist": _p("array", "One answer per checklist item of the version, as you read "
                        "it: the item's exact text, a verdict, and a one-line reason.",
                        items={"type": "object", "properties": {
                            "item": {"type": "string"},
                            "verdict": {"type": "string", "enum": ["pass", "fail"]},
                            "reason": {"type": "string"}},
                            "required": ["item", "verdict", "reason"],
                            "additionalProperties": False}),
        "check_only": _p("boolean", "true: run the same checks and record nothing; left out or "
                         "false: record the trade."),
    }, ("run", "ticker", "side", "entry_type", "entry", "size", "currency", "reasons")),
    "move_order": Tool(api.move_order, (
        "Move a trade's stop, its target or both, from `at` on. Refused once the trade has "
        "exited, and on a limit order its run's stop cancelled. The reply is the trade, with the "
        "move in `decisions.moves`, in time order."), {
        "trade": _TRADE,
        "reason": _p("string", "Why the order moves, exactly as the user wrote it."),
        "stop": _p("number", "The new stop price."),
        "target": _p("number", "The new target price."),
        "at": _at("When the order moved"),
    }, ("trade", "reason")),
    "report": Tool(api.report, (
        "Report the trade's state as of `at`, never after now. `filled`: a limit order filled, "
        "at the limit unless a price is given. `cancelled`: it never will; give `why`. `open`: "
        "the position is still open. `exited`: the position is out; give `kind`, and a hand "
        "exit gives "
        "its price (a stop or target exit given none is at the level in force). Reports are the "
        "only source of a trade's outcome, in a live run as in a replay one. A "
        "limit's fill is reported before its exit. A wrong report is fixed with correct_trade "
        "or withdrawn with void_trade. The reply is the trade, with `outcome` read from its "
        "record."), {
        "trade": _TRADE,
        "state": _p("string", "filled: the limit order filled; cancelled: it never will; open: "
                    "the position is still open; exited: the position is out.",
                    enum=["filled", "cancelled", "open", "exited"]),
        "at": _p("string", "The moment the trade was in that state: a UTC ISO-8601 time with "
                 "its offset, e.g. 2026-03-02T14:30:00Z."),
        "reason": _p("string", "What the report rests on: the user's words when they gave them; "
                     "otherwise what you read, marked as yours, e.g. \"chart: the 15:00 bar "
                     "traded down to 100\"."),
        "price": _p("number", "filled: the fill price (left out: the limit); exited: the exit "
                    "price, required for a hand exit."),
        "kind": _p("string", "exited only: stop (the stop filled), target (the target filled) "
                   "or hand (exited by hand; left out: hand).", enum=["hand", "stop", "target"]),
        "why": _p("string", "cancelled only, and required there: not filled (the market never "
                  "reached the limit, or the order expired), pulled (withdrawn) or run "
                  "stopped (the run's stop cancelled it; only on a stopped run, dated at its "
                  "stop).",
                  enum=["not filled", "pulled", "run stopped"]),
    }, ("trade", "state", "at", "reason")),
    "correct_trade": Tool(api.correct_trade, (
        "Correct a recorded decision: the correction is read in place of the original, which "
        "stays in the log. `fields` corrects one of three things. The entry: `entry`, `stop`, "
        "`target`, `size`, `at`, or `point_value`, e.g. "
        "{\"stop\": 97.5}. One move: {\"move\": i, …} with its `stop`, `target` or `at`, where "
        "i is the move's place in get_trade's `decisions.moves`, from 0. One standing report: "
        "{\"report\": \"filled\" | \"cancelled\" | \"open\" | \"exited\", …} with its `price`, "
        "`why` or `kind`, where \"open\" is the latest open report. A field given as null is "
        "read as not given, so a correction never removes a stop or a target. A wrong run, "
        "ticker or side is not corrected: void the trade and enter it again with its original "
        "`at`. The reply is the trade, as corrected."), {
        "trade": _TRADE,
        "fields": _p("object", "What to correct, each with its corrected value."),
        "reason": _p("string", "Why the correction, exactly as the user wrote it."),
    }, ("trade", "fields", "reason")),
    "void_trade": Tool(api.void_trade, (
        "Take a trade out of every stat and every other trade's checks; it stays in the log, "
        "with `decisions.voided` true. It cannot be undone. With `report`, withdraw that one "
        "standing report instead: it is read as absent, and the trade stands. A trade entered "
        "under the wrong run, ticker or side is voided and entered again. The reply is the "
        "trade."), {
        "trade": _TRADE,
        "reason": _p("string", "Why it is withdrawn, exactly as the user wrote it."),
        "report": _p("string", "The one standing report to withdraw instead of the trade; "
                     "open: the latest open report.",
                     enum=["filled", "cancelled", "open", "exited"]),
    }, ("trade", "reason")),
    "get_trade": Tool(api.get_trade, (
        "One trade, read now. `decisions`: the entry, its moves and its reports, as "
        "corrected. `outcome`: what its record reads as, with `status` (pending, open, exited or "
        "cancelled), the fill, the exit, R and cash. R is the move from "
        "the fill to the exit divided by the risk, the distance from the fill to the entry's "
        "stop (never a moved stop); cash is the price move times the size times the point "
        "value, in the trade's currency. `checks_shown`: "
        "the rule results shown at entry; `checks_now`: as they read now, from the run's "
        "current trades; `checks_differ`: the rules whose result changed (null for a voided trade, "
        "or when the entry stored no results to compare)."), {
        "trade": _TRADE,
    }, ("trade",)),
    "list_trades": Tool(api.list_trades, (
        "Every trade in recording order, voids included, narrowed to a run or a strategy, and "
        "to the open ones (`true`) or the rest (`false`). Open means a status of pending or open. "
        "Each trade carries its whole "
        "record, as get_trade gives it, except that `checks_now` and `checks_differ` are null: "
        "only get_trade works them out, so narrow to a run, and to `open: true`, when you "
        "can."), {
        "run": _p("string", "A run's id, to list only its trades."),
        "strategy": _p("string", "A strategy's id, to list only its runs' trades."),
        "open": _p("boolean", "true: only the open trades; false: only the rest; left out: "
                   "all."),
    }),
    "stats": Tool(api.stats, (
        "Counts, rates and averages for one scope's non-void trades: a run, a strategy, or a "
        "strategy with a version, narrowed by mode, by tags and by `conforming_only`. Never a "
        "verdict or a ranking. Every list of trades comes with its count, as "
        "{\"count\": n, \"ids\": [...]}. `closed`: the exits with R, the result in multiples of "
        "the risk, split into wins (R above 0), losses (below 0) and breakevens; `win_rate` is "
        "wins over closed, and `average_r`, `total_r`, `average_win`, `average_loss` (negative) "
        "and `average_planned_rr` (the planned reward over the risk, from the entry's target "
        "and stop) are over closed. `closed_no_r`: exits without R, whose cash is in `pnl`, the "
        "profit or loss per currency, gross of fees, never converted. `open`, `pending` and "
        "`cancelled` (by why) are the rest. `limits` is the limit orders' fill rate: filled over "
        "placed, less those pending or cancelled by a run stop. `left_out`: what "
        "conforming_only left out. `rests_on`: the trades by mode, tag set and version. A figure "
        "with nothing under it is null."), {
        "strategy": _p("string", "A strategy's id: the scope, alone or with a version."),
        "version": _p("integer", "With strategy: that version alone, from 1.", minimum=1),
        "run": _p("string", "A run's id: the scope alone, since a run names its strategy and "
                  "version."),
        "mode": _p("string", "Only the runs in this mode.", enum=["live", "replay"]),
        "tags": _p("array", "Only the runs holding every one of these tags, matched exactly.",
                   items={"type": "string"}),
        "conforming_only": _p("boolean", "true: only the trades whose rule results now are all "
                              "kept."),
    }),
}


def schema(name: str) -> dict:
    tool = TOOLS[name]
    return {"type": "object", "properties": tool.params, "required": list(tool.required),
            "additionalProperties": False}


def plain(value) -> object:
    """A reply as JSON data: a time in UTC, and a figure that is not finite as null."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
        if isinstance(value, api.TradeView):   # checks_shown carries the results; say them once
            del out["decisions"]["checks"]
        return out
    if isinstance(value, tuple | list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, float):
        return float(f"{value:.10g}") if math.isfinite(value) else None
    return value


def encode(value) -> str:
    return json.dumps(plain(value), allow_nan=False, ensure_ascii=False, separators=(",", ":"))


def _ids(ids: list) -> dict:
    return {"count": len(ids), "ids": ids}


def _each(groups: dict) -> dict:
    return {k: _ids(ids) for k, ids in groups.items()}


def _counted(s: dict) -> dict:
    """stats, as plain data, with every list of trade ids beside its count."""
    for k in ("trades", "closed", "wins", "losses", "breakevens", "no_planned_rr", "closed_no_r",
              "open", "pending"):
        s[k] = _ids(s[k])
    s["cancelled"] = _each(s["cancelled"])
    limits = s["limits"]
    limits.update(_each({k: limits[k] for k in ("placed", "filled", "pending")}),
                  cancelled=_each(limits["cancelled"]))
    if s["left_out"]:
        s["left_out"] = {"breached": _ids(s["left_out"]["breached"]),
                         "unknown": _each(s["left_out"]["unknown"])}
    r = s["rests_on"]
    s["rests_on"] = {"mode": _each(r["mode"]), "version": _each(r["version"]),
                     "tags": [{"tags": g["tags"], "trades": _ids(g["trades"])} for g in r["tags"]]}
    return s


def _open() -> sqlite3.Connection:
    try:
        return store.open()
    except FileNotFoundError:   # its text names store.create(), which an agent cannot call
        path = config.db_path()
        raise Refused(f"There is no desk.db at {path}; restart the MCP server, which creates one, "
                      "or check TRADING_DESK_HOME and TRADING_DESK_DB.") from None


def _reply(text: str, *, error: bool) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=[mcp_types.TextContent(text=text)], is_error=error)


_AGAIN = "Read the trade or the run again before relying on what this call did."


def call(name: str, arguments: dict) -> mcp_types.CallToolResult:
    """One tool call on a connection of its own: the reply as JSON, or the error in words."""
    tool = TOOLS[name]
    if unknown := set(arguments) - set(tool.params):
        takes = f"takes {', '.join(tool.params)}" if tool.params else "takes no arguments"
        return _reply(f"{name} {takes}; it has no {', '.join(sorted(unknown))}.", error=True)
    given = {p: arguments.get(p) for p in tool.required} | arguments   # a missing one: refused
    try:
        with contextlib.closing(_open()) as con:
            reply = tool.fn(con, **given)
        text = encode(_counted(plain(reply)) if name == "stats" else reply)
    except Refused as e:
        return _reply(str(e), error=True)
    except store.NotADeskDatabase as e:   # the user's file, named
        return _reply(f"The desk's data could not be used: {e}", error=True)
    except Exception as e:
        traceback.print_exc()   # stderr: the server's log
        if isinstance(e, sqlite3.OperationalError | OSError) or type(e) is sqlite3.DatabaseError:
            return _reply(f"The desk's data could not be used: {e}. {_AGAIN}", error=True)
        # ProgrammingError and InterfaceError included: a value reached SQLite unchecked
        return _reply(f"The desk hit an unexpected error, a bug: {type(e).__name__}: {e}. The "
                      f"traceback is in the server's log. {_AGAIN}", error=True)
    return _reply(text, error=False)


def first_start() -> None:
    """Create desk.db when neither it nor its iCloud placeholder exists, so a desk.db iCloud has
    evicted is never covered over. A failure goes to stderr, and every call then names it."""
    try:
        path = config.db_path()
        if not path.exists() and not config.icloud_placeholder(path).exists():
            store.create(path)
    except Refused as e:   # every call then says it again
        print(f"trading-desk did not create its desk.db: {e}", file=sys.stderr)
    except (OSError, sqlite3.Error) as e:
        print(f"trading-desk could not create its desk.db: {e}", file=sys.stderr)


async def _list_tools(ctx, params):
    return mcp_types.ListToolsResult(tools=[
        mcp_types.Tool(name=n, description=t.description, input_schema=schema(n))
        for n, t in TOOLS.items()])


_lock = anyio.Lock()   # one call at a time: the desk has one writer


async def _call_tool(ctx, params):
    if params.name not in TOOLS:   # the spec's protocol error, not a tool result
        raise MCPError(mcp_types.INVALID_PARAMS, f"Unknown tool: {shown(params.name)}")
    async with _lock:
        # A worker thread keeps the loop free to answer a ping; `call` opens its connection
        # there, since a sqlite3 connection stays in its thread.
        return await anyio.to_thread.run_sync(call, params.name, params.arguments or {})


try:
    _SERVER_VERSION = importlib.metadata.version("trading-desk")
except importlib.metadata.PackageNotFoundError:   # a checkout that was never installed
    _SERVER_VERSION = "unknown"

server = Server("trading-desk", version=_SERVER_VERSION,
                instructions=config.instructions().read_text(encoding="utf-8"),
                on_list_tools=_list_tools, on_call_tool=_call_tool)


async def _main() -> None:
    async with stdio_server() as (read, write):
        # Only now: entering, stdio_server serves the wire from the fd behind sys.stdout. A
        # print to sys.stdout is buffered and flushed at exit, after fd 1 is the wire again.
        sys.stdout = sys.stderr
        await server.run(read, write, server.create_initialization_options())


def serve() -> int:
    """The stdio loop; 0 once the client closes stdin."""
    first_start()
    out = sys.stdout
    try:
        anyio.run(_main)
    finally:
        sys.stdout = out
    return 0
