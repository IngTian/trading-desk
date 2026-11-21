#!/usr/bin/env bash
# Install a built wheel into a clean venv OUTSIDE the checkout and use it the way a new user
# would: save the shipped example strategy, record one trade through the API, read a replay trade
# from its reports and a live trade from bars already in bars.db (no yfinance, no network), run
# the integrity checker, and assert each outcome. Run by CI and by the release, before publishing.
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
unset TRADING_DESK_DB TRADING_DESK_BOOK
export TRADING_DESK_HOME="$work/home"
mkdir -p "$TRADING_DESK_HOME"

"$bin/trading-desk" --help > /dev/null
"$bin/python" - "$work" <<'PY'
import contextlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import desk
from desk import api, bars, config, judge, store

where = Path(desk.__file__).resolve()
assert str(where).startswith(sys.argv[1]), f"desk imported from {where}, not the clean venv"
now = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
store.create(config.db_path())
with store.open() as con:
    saved = api.save_strategy(con, "example", config.example_strategy().read_text(), now=now)
    run = api.start_run(con, "example", mode="live", currency="USD", at=now, now=now)
    # Invented figures: a market buy of 10 shares of a made-up ticker.
    t = api.trade(con, run.id, ticker="ACME", side="long", entry_type="market", entry=100.0,
                  stop=98.5, target=103.0, size=10, currency="USD", reasons="Smoke test.",
                  at=now, now=now)
    assert api.get_trade(con, t.id).id == t.id
    kinds = [e["kind"] for e in store.events(con, trade_id=t.id)]
    assert len(store.trades(con)) == 1 and kinds == ["entered"], kinds
    later = now + timedelta(hours=1)   # a report may not be dated after its `now`

    # Replay: the outcome is what is reported, and no bars are read.
    replay = api.start_run(con, "example", mode="replay", currency="USD", at=now, now=now)
    plan = dict(ticker="ACME", side="long", entry_type="limit", entry=100.0, stop=98.5,
                target=104.0, size=10, currency="USD", reasons="Smoke test.", at=now, now=now)
    # check_only: the example strategy's rules give results, and nothing is written.
    before = len(store.events(con))
    seen = api.trade(con, replay.id, check_only=True, **plan)
    assert seen.checks and len(store.events(con)) == before and len(store.trades(con)) == 1
    r = api.trade(con, replay.id, **plan)
    api.report(con, r.id, state="filled", at=now + timedelta(minutes=15), reason="Smoke test.",
               now=later)
    o = api.report(con, r.id, state="exited", kind="target", at=now + timedelta(minutes=30),
                   reason="Smoke test.", now=later).outcome
    assert (o.status, o.exit, o.reported) == ("exited", 104.0, ("fill", "exit")), o
    st = api.stats(con, run=replay.id, now=later)
    assert st.trades == (r.id,) and st.closed == (r.id,), st

    # Live, before any bars: the clean venv has no yfinance, so the fetch fails without reaching
    # the network, and the read names the extra.
    notes = api.get_trade(con, t.id, now=later).outcome.notes
    assert any("trading-desk[yahoo]" in n for n in notes), notes

    # Live, from bars stored here: three quiet 15m bars, then one reaching the 103.0 target. The
    # trade ended, so it is never fetched.
    quiet = [judge.Bar(now + timedelta(minutes=15 * i), 100.0, 101.0, 99.5, 100.5, None)
             for i in range(3)]
    q = quiet + [judge.Bar(now + timedelta(minutes=45), 100.5, 104.5, 100.0, 104.0, None)]
    with contextlib.closing(bars.open()) as b:
        bars.add(b, "yahoo", "ACME", 15, q, now, later, now)
    o = api.get_trade(con, t.id, now=later).outcome
    assert (o.status, o.exit, o.exit_reason) == ("exited", 103.0, "target"), o
print(f"installed wheel recorded {t.id} through the API")
PY
"$bin/trading-desk" integrity
