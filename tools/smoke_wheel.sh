#!/usr/bin/env bash
# Install a built wheel into a clean venv OUTSIDE the checkout and use it the way a new user
# would: save the shipped example strategy, record one trade through the API, read a trade's
# outcome from its reports on a live run and on a replay one, run the integrity checker, and
# assert each outcome. Run by CI and by the release, before publishing.
#
#     bash tools/smoke_wheel.sh <dir holding the .whl>
#
# The wheel's shape is asserted by tools/check_wheel.py; this is the step that RUNS it, so it
# is the one that exercises [project.scripts], the package-data paths and the absence of any
# accidental dependency on the source tree. Everything happens in a temp dir with the data
# root pointed into it, so it can never reach a real desk.db.
set -euo pipefail

dist="$(cd "${1:?usage: smoke_wheel.sh <dist dir>}" && pwd)"
# Physical path (pwd -P): on macOS mktemp hands out /var/..., which resolves to /private/var/...
work="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$work"' EXIT

python3 -m venv "$work/venv"
"$work/venv/bin/pip" install -q "$dist"/*.whl
bin="$work/venv/bin"

# From outside the checkout, so `import desk` cannot pick up the source tree by accident.
cd "$work"
unset TRADING_DESK_DB
export TRADING_DESK_HOME="$work/home"
mkdir -p "$TRADING_DESK_HOME"

"$bin/trading-desk" --help > /dev/null
"$bin/python" - "$work" "$dist" <<'PY'
import importlib.metadata
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import desk
from desk import api, config, outcome, store

where = Path(desk.__file__).resolve()
assert str(where).startswith(sys.argv[1]), f"desk imported from {where}, not the clean venv"

# The installed version is the one the wheel was built from, and its package data is there:
# the example strategy a new user copies and the instructions the MCP server sends.
version = importlib.metadata.version("trading-desk")
wheels = list(Path(sys.argv[2]).glob("*.whl"))
assert len(wheels) == 1 and f"-{version}-" in wheels[0].name, (version, wheels)
text = config.example_strategy().read_text(encoding="utf-8")
assert "```rules" in text, "the shipped example strategy has no rules block"
assert config.instructions().read_text(encoding="utf-8").strip(), "the instructions are empty"

now = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
store.create(config.db_path())
with store.open() as con:
    api.save_strategy(con, "example", text, now=now)
    run = api.start_run(con, "example", mode="live", currency="USD", at=now, now=now)
    # Invented figures: a market buy of 10 shares of a made-up ticker.
    t = api.trade(con, run.id, ticker="ACME", side="long", entry_type="market", entry=100.0,
                  stop=98.5, target=103.0, size=10, currency="USD", instrument="stock",
                  reasons="Smoke test.", at=now, now=now)
    assert api.get_trade(con, t.id).id == t.id
    kinds = [e["kind"] for e in store.events(con, trade_id=t.id)]
    assert len(store.trades(con)) == 1 and kinds == ["entered"], kinds
    later = now + timedelta(hours=1)   # a report may not be dated after its `now`

    # A live run reads its outcome from reports, exactly as a replay one does.
    o = api.report(con, t.id, state="exited", kind="target", at=later, reason="Smoke test.",
                   now=later).outcome
    assert isinstance(o, outcome.Outcome), o
    assert (o.status, o.fill, o.exit, o.exit_kind, o.r, o.cash) == (
        "exited", 100.0, 103.0, "target", 2.0, 30.0), o
    st = api.stats(con, run=run.id, now=later)
    assert st.trades == (t.id,) and st.closed == (t.id,), st

    # Replay: check_only writes nothing, and the recorded trade reads the same way.
    replay = api.start_run(con, "example", mode="replay", currency="USD", at=now, now=now)
    plan = dict(ticker="ACME", side="long", entry_type="limit", entry=100.0, stop=98.5,
                target=104.0, size=10, currency="USD", instrument="stock",
                reasons="Smoke test.", at=now, now=now)
    before = len(store.events(con))
    seen = api.trade(con, replay.id, check_only=True, **plan)
    assert seen.checks and len(store.events(con)) == before and len(store.trades(con)) == 1
    r = api.trade(con, replay.id, **plan)
    assert api.get_trade(con, r.id, now=later).outcome.status == "pending"
    api.report(con, r.id, state="filled", at=now + timedelta(minutes=15), reason="Smoke test.",
               now=later)
    o = api.report(con, r.id, state="exited", kind="target", at=now + timedelta(minutes=30),
                   reason="Smoke test.", now=later).outcome
    assert (o.status, o.exit, o.exit_kind, o.cash) == ("exited", 104.0, "target", 40.0), o
    assert abs(o.r - 4.0 / 1.5) < 1e-9, o
    st = api.stats(con, run=replay.id, now=later)
    assert st.trades == (r.id,) and st.closed == (r.id,), st
print(f"installed wheel {version} recorded {t.id} and {r.id} through the API")
PY
"$bin/trading-desk" integrity

# The MCP server, driven as a client drives it: one message at a time (send a line, read its
# reply, then send the next). Piping every line and closing stdin makes the SDK answer the
# requests still in flight with "Connection closed". Its own home, so the records above stand apart.
"$bin/python" - "$bin/trading-desk" "$work/home2" <<'PY'
import json
import os
import queue
import subprocess
import sys
import threading
from importlib.resources import files

exe, home = sys.argv[1], sys.argv[2]
env = dict(os.environ, TRADING_DESK_HOME=home)
p = subprocess.Popen([exe, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
try:
    lines: queue.Queue = queue.Queue()


    def pump():
        for line in p.stdout:
            lines.put(line)
        lines.put(None)


    threading.Thread(target=pump, daemon=True).start()
    ids = iter(range(1, 100))


    def send(msg):
        p.stdin.write(json.dumps(msg).encode() + b"\n")
        p.stdin.flush()


    def ask(method, params=None):
        i = next(ids)
        send({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}})
        while True:
            line = lines.get(timeout=30)
            assert line is not None, "the server closed its output"
            reply = json.loads(line)
            if reply.get("id") == i:
                assert "error" not in reply, reply
                return reply["result"]


    def call(name, **arguments):
        result = ask("tools/call", {"name": name, "arguments": arguments})
        text = result["content"][0]["text"]
        assert not result.get("isError"), f"{name}: {text}"
        return json.loads(text)


    ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "0"}})
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = ask("tools/list")["tools"]
    assert len(tools) == 14, [t["name"] for t in tools]

    at = "2026-03-02T12:00:00Z"
    call("save_strategy", id="example",
         markdown=files("desk").joinpath("strategy.example.md").read_text(encoding="utf-8"))
    run = call("start_run", strategy="example", mode="replay", currency="USD", at=at)["id"]
    t = call("trade", run=run, ticker="ACME", side="long", entry_type="limit", entry=100.0,
             stop=98.5, target=104.0, size=10, currency="USD", instrument="stock",
             reasons="Smoke test.", at=at)
    call("report", trade=t["id"], state="filled", at="2026-03-02T12:15:00Z", reason="Smoke test.")
    call("report", trade=t["id"], state="exited", kind="target", at="2026-03-02T12:30:00Z",
         reason="Smoke test.")
    st = call("stats", run=run)
    assert st["closed"]["count"] == 1, st

    p.stdin.close()
    assert p.wait(timeout=30) == 0, "the server did not exit 0 at end of input"
    assert subprocess.run([exe, "integrity"], env=env).returncode == 0
finally:
    if p.poll() is None:
        p.kill()
print("installed wheel served 14 tools over MCP")
PY
