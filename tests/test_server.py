"""`trading-desk mcp`: the 15 tools on the MCP SDK's low-level server. Every figure is invented.

In process through `mcp.Client`, through `server.call` directly, and as a subprocess driven one
message at a time, as a client drives it."""
import dataclasses
import importlib.metadata
import inspect
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import anyio
import mcp
import mcp.types as mcp_types
import pytest
from mcp.shared.exceptions import MCPError

from desk import api, config, server, store

NAMES = ["save_strategy", "get_strategy", "list_strategies", "start_run", "stop_run", "list_runs",
         "trade", "move_order", "close_trade", "report", "correct_trade", "void_trade",
         "get_trade", "list_trades", "stats"]
TEXT = ('---\nname: Tester\n---\n\n# Tester\n\nAll figures here are invented.\n\n```rules\n'
        'timeframes = ["15m"]\n\n[facts]\nrvol = { at_least = 2.0 }\n```\n\n## Checklist\n\n'
        '- Invented item.\n')
ENTRY = dict(ticker="ACME", side="long", entry_type="market", entry=100.0, stop=98.0,
             target=104.0, size=10, currency="USD", instrument="stock", reasons="Invented.")
MCP = [sys.executable, "-c", "from desk.cli import main; raise SystemExit(main(['mcp']))"]


def body(result):
    return json.loads(result.content[0].text)


def ok(name, **arguments):
    """server.call, which must succeed; its reply, parsed."""
    result = server.call(name, arguments)
    assert result.is_error is False, result.content[0].text
    return body(result)


def error(name, **arguments) -> str:
    result = server.call(name, arguments)
    assert result.is_error is True
    return result.content[0].text


@pytest.fixture
def desk():
    store.create()


# ---------------------------------------------------------------- in process, mcp.Client ---
@pytest.mark.anyio
async def test_the_server_names_itself_and_carries_the_instructions():
    async with mcp.Client(server.server) as c:
        assert c.instructions == config.instructions().read_text(encoding="utf-8")
    assert server.server.name == "trading-desk"
    assert server.server.version == importlib.metadata.version("trading-desk")


@pytest.mark.anyio
async def test_the_tools_are_the_15_api_functions_one_to_one():
    async with mcp.Client(server.server) as c:
        listed = (await c.list_tools()).tools
    assert [t.name for t in listed] == NAMES
    for t in listed:
        tool = server.TOOLS[t.name]
        params = set(inspect.signature(tool.fn).parameters) - {"con", "now"}
        assert set(tool.params) == params, t.name
        assert set(tool.required) <= set(tool.params), t.name
        assert all(p["description"].strip() for p in tool.params.values()), t.name
        assert t.input_schema["type"] == "object", t.name
        assert t.input_schema["additionalProperties"] is False, t.name
        assert t.input_schema["properties"] == tool.params, t.name
        assert len(t.description) <= 4000, t.name
        assert len("mcp__plugin_trading-desk_desk__" + t.name) <= 128


@pytest.mark.anyio
async def test_a_tool_call_replies_with_the_view_as_json(desk):
    async with mcp.Client(server.server) as c:
        r = await c.call_tool("save_strategy", {
            "id": "example", "markdown": config.example_strategy().read_text()})
        assert r.is_error is False and body(r)["saved"] == "version 1 created"
        r = await c.call_tool("start_run", {"strategy": "example", "mode": "replay",
                                            "currency": "USD", "at": "2026-03-02T09:30:00-05:00"})
        assert r.is_error is False and body(r)["started"] == "2026-03-02T14:30:00+00:00"
        r = await c.call_tool("trade", {"run": body(r)["id"], **ENTRY,
                                        "at": "2026-03-02T15:00:00Z", "check_only": True})
        assert r.is_error is False and "checks" in body(r)
        assert body(await c.call_tool("list_trades", {})) == []


@pytest.mark.anyio
async def test_a_refusal_is_a_tool_error_with_its_message(desk):
    async with mcp.Client(server.server) as c:
        r = await c.call_tool("get_strategy", {"id": "nope"})
    assert r.is_error is True and r.content[0].text.startswith("There is no strategy")


@pytest.mark.anyio
async def test_an_unknown_tool_is_a_protocol_error():
    async with mcp.Client(server.server) as c:
        with pytest.raises(MCPError) as e:
            await c.call_tool("nosuch", {})
    assert e.value.code == mcp_types.INVALID_PARAMS == -32602
    assert "Unknown tool" in e.value.message


@pytest.mark.anyio
async def test_reasons_in_any_language_arrive_exactly(desk):
    reasons = "Invented: 突破后回踩, é\nsecond line"
    async with mcp.Client(server.server) as c:
        await c.call_tool("save_strategy", {"id": "s", "markdown": TEXT})
        run = body(await c.call_tool("start_run", {"strategy": "s", "mode": "replay",
                                                   "currency": "USD", "at": "2026-03-02T14:00Z"}))
        t = body(await c.call_tool("trade", {"run": run["id"], **ENTRY, "reasons": reasons,
                                             "at": "2026-03-02T14:30:00Z"}))
        got = body(await c.call_tool("get_trade", {"trade": t["id"]}))
    assert got["decisions"]["reasons"] == reasons


@pytest.mark.anyio
async def test_calls_run_one_at_a_time(desk, monkeypatch):
    spans = []

    def slow(con):
        entered = time.monotonic()
        time.sleep(0.2)
        spans.append((entered, time.monotonic()))
        return []
    monkeypatch.setitem(server.TOOLS, "list_strategies",
                        dataclasses.replace(server.TOOLS["list_strategies"], fn=slow))
    async with mcp.Client(server.server) as c:
        async with anyio.create_task_group() as tg:
            tg.start_soon(c.call_tool, "list_strategies", {})
            tg.start_soon(c.call_tool, "list_strategies", {})
    (_, first_left), (second_entered, _) = sorted(spans)
    assert len(spans) == 2 and first_left <= second_entered


# ------------------------------------------------------------------------ call directly ---
def test_unknown_and_missing_arguments_are_refused(desk):
    assert "close_trade takes trade, reason, at; it has no price" in error(
        "close_trade", trade="acme-20260302", reason="Invented.", price=101.0)
    assert "get_trade takes trade; it has no now" in error(
        "get_trade", trade="acme-20260302", now="2026-03-02T14:30:00Z")
    assert "list_strategies takes no arguments; it has no x" in error("list_strategies", x=1)
    assert "id must be text" in error("save_strategy")


WRONG = [True, False, 0, -1, 1.5, float("nan"), float("inf"), 10**400, "", "x", [], ["a"], {},
         {"a": 1}, None]


def _seed(tmp_path) -> dict[str, dict]:
    """One strategy, a replay run with trade A, a live run with trade C; each tool's valid call."""
    store.create()
    current = ok("save_strategy", id="s", markdown=TEXT)["current_hash"]
    replay = ok("start_run", strategy="s", mode="replay", currency="USD", tags=["a"],
                at="2026-03-02T14:00:00Z")["id"]
    live = ok("start_run", strategy="s", mode="live", currency="USD",
              at="2026-03-02T14:00:00Z")["id"]
    a = ok("trade", run=replay, **ENTRY, at="2026-03-02T14:30:00Z")["id"]
    c = ok("trade", run=live, **ENTRY, at="2026-03-02T14:30:00Z")["id"]
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG invented")
    return {
        "save_strategy": {"id": "s", "markdown": TEXT, "if_current": current},
        "get_strategy": {"id": "s", "version": 1},
        "list_strategies": {},
        "start_run": {"strategy": "s", "mode": "replay", "currency": "USD", "tags": ["a"],
                      "version": 1, "at": "2026-03-02T14:00:00Z"},
        "stop_run": {"run": replay, "at": "2026-03-03T14:00:00Z"},
        "list_runs": {"strategy": "s"},
        "trade": {"run": replay, **ENTRY, "entry_type": "limit", "screenshots": [str(shot)],
                  "at": "2026-03-02T15:00:00Z", "instrument": "future", "point_value": 50.0,
                  "market": {"rvol": 2.5}, "check_only": False,
                  "checklist": [{"item": "Invented item.", "verdict": "pass",
                                 "reason": "Invented."}]},
        "move_order": {"trade": a, "reason": "Invented.", "stop": 99.0, "target": 105.0,
                       "at": "2026-03-02T15:00:00Z"},
        "close_trade": {"trade": c, "reason": "Invented.", "at": "2026-03-02T15:00:00Z"},
        "report": {"trade": a, "state": "exited", "at": "2026-03-02T16:00:00Z",
                   "reason": "Invented.", "price": 103.0, "kind": "hand"},
        "correct_trade": {"trade": a, "fields": {"stop": 97.5}, "reason": "Invented."},
        "void_trade": {"trade": a, "reason": "Invented."},
        "get_trade": {"trade": a},
        "list_trades": {"run": replay, "strategy": "s", "open": True},
        "stats": {"strategy": "s", "version": 1, "mode": "replay", "tags": ["a"],
                  "conforming_only": True},
    }


@pytest.mark.parametrize("name", NAMES)
def test_every_tool_refuses_every_wrong_json_type(name, tmp_path):
    valid = _seed(tmp_path)[name]
    pristine = tmp_path / "pristine.db"
    shutil.copyfile(config.db_path(), pristine)
    assert server.call(name, valid).is_error is False, server.call(name, valid).content[0].text
    failing = []
    for param in server.TOOLS[name].params:
        values = WRONG + (["2026-03-02T14:30:00"] if param == "at" else [])
        for value in values:
            shutil.copyfile(pristine, config.db_path())   # each call on the seeded store
            try:
                r = server.call(name, {**valid, param: value})
            except Exception as e:
                failing.append((name, param, value, f"raised {type(e).__name__}: {e}"))
                continue
            text = r.content[0].text
            if r.is_error and text.startswith(("The desk hit an unexpected error",
                                               "The desk's data could not be used")):
                failing.append((name, param, value, text))
    assert failing == [], failing


def test_a_reply_is_strict_json():
    assert server.plain(float("nan")) is None
    assert server.plain(datetime(2026, 3, 2, 9, 30, tzinfo=timezone(timedelta(hours=-5)))) == (
        "2026-03-02T14:30:00+00:00")
    assert server.encode({"r": float("inf")}) == '{"r":null}'
    assert server.encode({"t": (1, "é")}) == '{"t":[1,"é"]}'


def test_the_stats_reply_counts_beside_every_id_list(desk):
    ok("save_strategy", id="s", markdown=TEXT)
    run = ok("start_run", strategy="s", mode="replay", currency="USD",
             at="2026-03-02T14:00:00Z")["id"]
    limit = {**ENTRY, "entry_type": "limit", "at": "2026-03-02T14:30:00Z"}
    a = ok("trade", run=run, **limit)["id"]
    b = ok("trade", run=run, **limit)["id"]
    ok("report", trade=a, state="filled", at="2026-03-02T14:45:00Z", reason="Invented.")
    ok("report", trade=a, state="exited", kind="target", at="2026-03-02T15:30:00Z",
       reason="Invented.")
    got = ok("stats", run=run)
    assert got["trades"] == {"count": 2, "ids": [a, b]}
    assert got["closed"] == {"count": 1, "ids": [a]}
    assert got["wins"] == {"count": 1, "ids": [a]} and got["total_r"] == 2.0
    assert got["pending"] == {"count": 1, "ids": [b]}
    assert got["cancelled"] == {} and got["not_judged"] == {"count": 0, "trades": {}}
    assert got["limits"]["placed"] == {"count": 2, "ids": [a, b]}
    assert got["limits"]["rate"] == 1.0
    assert got["rests_on"]["mode"] == {"replay": {"count": 2, "ids": [a, b]}}
    assert got["rests_on"]["tags"] == [{"tags": [], "trades": {"count": 2, "ids": [a, b]}}]
    assert got["rests_on"]["live_exits"]["reported"] == {"count": 0, "ids": []}
    assert got["tags"] == []


def test_a_wrong_file_at_desk_db_is_named_in_every_reply():
    path = config.db_path()
    path.write_bytes(b"not a database")
    assert "is not a desk.db" in error("list_strategies")
    assert "is not a desk.db" in error("get_strategy", id="s")
    assert path.read_bytes() == b"not a database"


def test_each_call_sees_writes_made_between_calls(desk):
    assert ok("list_strategies") == []
    with store.open() as con:
        api.save_strategy(con, "s", TEXT)
    assert [s["id"] for s in ok("list_strategies")] == ["s"]


# ------------------------------------------------------------------------- first_start ---
def test_the_first_start_creates_the_desk():
    assert not config.db_path().exists()
    server.first_start()
    assert config.db_path().exists()
    store.open().close()
    server.first_start()   # a second start leaves it be
    assert ok("list_strategies") == []


def test_a_desk_db_that_cannot_be_created_does_not_stop_the_server(monkeypatch, capsys):
    def broken(path):
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(store, "create", broken)
    server.first_start()
    assert "could not create its desk.db" in capsys.readouterr().err
    assert "a bug" not in error("list_strategies")


def test_a_desk_db_in_icloud_only_is_never_replaced():
    (config.db_path().parent / ".desk.db.icloud").write_bytes(b"an invented placeholder")
    server.first_start()
    assert not config.db_path().exists()
    assert "is in iCloud but not downloaded" in error("list_strategies")


def test_no_desk_db_names_the_restart():
    assert "restart the MCP server, which creates one" in error("list_strategies")


# ------------------------------------------------------- a subprocess, one message at a time ---
class Wire:
    """`trading-desk mcp` in a subprocess, driven as a client drives it: send a line, read its
    reply, then send the next. Piping every line and closing stdin gets "Connection closed" for
    the requests still in flight. stderr goes to a file, so a long log never blocks the server."""

    def __init__(self, tmp_path, command=MCP):
        env = {k: v for k, v in os.environ.items() if k not in (config.ENV_DB,)}
        env[config.ENV_HOME] = str(tmp_path / "home")
        self.err = tmp_path / f"stderr-{time.monotonic_ns()}.txt"
        with self.err.open("wb") as err:
            self.p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=err, env=env)
        self.lines: queue.Queue = queue.Queue()
        self.out: list[bytes] = []   # every stdout line, in order
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.p.stdout:
            self.out.append(line)
            self.lines.put(line)
        self.lines.put(None)

    def send(self, message):
        raw = message if isinstance(message, bytes) else json.dumps(message).encode()
        self.p.stdin.write(raw + b"\n")
        self.p.stdin.flush()

    def ask(self, message) -> dict:
        """Send a request; the reply with its id (the lines before it are kept in `out`)."""
        self.send(message)
        while True:
            line = self.lines.get(timeout=10)
            assert line is not None, self.err.read_text()
            reply = json.loads(line)
            if reply.get("id") == message["id"]:
                return reply

    def close(self) -> int:
        """Close stdin; the exit status, once every stdout line is in `out`."""
        self.p.stdin.close()
        code = self.p.wait(timeout=10)
        self.reader.join(timeout=10)
        return code

    def stderr(self) -> str:
        return self.err.read_text()


@pytest.fixture
def wire(tmp_path):
    made = []

    def start(command=MCP):
        made.append(Wire(tmp_path, command))
        return made[-1]
    yield start
    for w in made:
        if w.p.poll() is None:
            w.p.kill()
            w.p.wait()


def _is_jsonrpc(line: bytes) -> bool:
    try:
        mcp_types.jsonrpc_message_adapter.validate_json(line, by_name=False)
    except ValueError:
        return False
    return True


INITIALIZE = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                         "clientInfo": {"name": "invented-client", "version": "1"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def test_trading_desk_mcp_speaks_both_protocol_eras(wire, tmp_path):
    instructions = config.instructions().read_text(encoding="utf-8")
    modern = wire()
    found = modern.ask({"jsonrpc": "2.0", "id": "server-discover-probe-1",
                        "method": "server/discover", "params": {"_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientInfo": {"name": "invented-client",
                                                                   "version": "1"},
                            "io.modelcontextprotocol/clientCapabilities": {}}}})
    assert found["result"]["instructions"] == instructions
    assert "2026-07-28" in found["result"]["supportedVersions"]
    assert modern.close() == 0

    legacy = wire()
    init = legacy.ask(INITIALIZE)["result"]
    assert init["protocolVersion"] == "2025-11-25" and init["instructions"] == instructions
    legacy.send(INITIALIZED)
    tools = legacy.ask({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == NAMES
    called = legacy.ask({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "list_strategies", "arguments": {}}})["result"]
    assert called["isError"] is False and json.loads(called["content"][0]["text"]) == []
    assert legacy.close() == 0
    assert (tmp_path / "home" / "desk.db").exists()


def test_bad_lines_are_skipped_and_the_server_goes_on(wire):
    w = wire()
    w.ask(INITIALIZE)
    w.send(INITIALIZED)
    for bad in (b"not json", json.dumps([{"jsonrpc": "2.0", "id": 5, "method": "ping"}]).encode(),
                b"\xff\xfe", b"[" * 100_000):
        w.send(bad)
    assert w.ask({"jsonrpc": "2.0", "id": 7, "method": "ping"}) == {
        "jsonrpc": "2.0", "id": 7, "result": {}}
    assert w.p.poll() is None
    assert w.close() == 0
    assert w.out and all(_is_jsonrpc(line) for line in w.out), w.out


NOISY = """\
import dataclasses, os, subprocess
from desk import server
def noisy(con):
    print("noise-print")
    os.write(1, b"noise-fd\\n")
    subprocess.run(["echo", "noise-child"])
    return []
server.TOOLS["list_strategies"] = dataclasses.replace(server.TOOLS["list_strategies"], fn=noisy)
raise SystemExit(server.serve())
"""


def test_stray_output_never_reaches_the_wire(wire):
    w = wire([sys.executable, "-c", NOISY])
    w.ask(INITIALIZE)
    w.send(INITIALIZED)
    called = w.ask({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "list_strategies", "arguments": {}}})
    assert called["result"]["isError"] is False
    assert w.close() == 0
    assert all(_is_jsonrpc(line) for line in w.out), w.out
    assert all(n in w.stderr() for n in ("noise-print", "noise-fd", "noise-child")), w.stderr()


# ------------------------------------------------------------------- the instruction file ---
def test_the_instruction_file_fits_and_says_what_design_9_asks():
    text = config.instructions().read_text(encoding="utf-8")
    assert len(text) <= 4000
    for phrase in ("exactly as they wrote", "never write one for them", "Never state a figure",
                   "close of the bar acted on", "UTC", "Define each finance term", "they/them",
                   "ask the user first", "`market`", "every declared fact (the desk computes none)",
                   "checklist item", "nothing but your reports", "not filled",
                   "stop-out at its real time", "broker's fill price",
                   "dated at or after the judged exit", "confirms the broker still holds",
                   "at least weekly", "full file paths or https://"):
        assert phrase in text, phrase


def test_a_trade_reply_carries_its_rule_results_once_and_round_figures(desk):
    ok("save_strategy", id="s", markdown=TEXT)
    run = ok("start_run", strategy="s", mode="replay", currency="USD",
             at="2026-03-02T14:00:00Z")["id"]
    t = ok("trade", run=run, **ENTRY, at="2026-03-02T14:30:00Z")
    assert "checks" not in t["decisions"] and "checks_shown" in t
    assert "checks" not in ok("list_trades")[0]["decisions"]
    assert server.plain(0.17499999999999716) == 0.175


def test_trade_names_which_market_keys_to_supply():
    text = server.TOOLS["trade"].description
    assert "supply each, read off the last completed bar" in text
