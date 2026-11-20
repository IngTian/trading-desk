#!/usr/bin/env bash
# Install a built wheel into a clean venv OUTSIDE the checkout and use it the way a new user
# would: save the shipped example strategy, record one trade through the API, run the integrity
# checker, and
# assert the trade was recorded. Run by CI and by the release, before publishing.
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
import sys
from datetime import UTC, datetime
from pathlib import Path

import desk
from desk import api, config, store

where = Path(desk.__file__).resolve()
assert str(where).startswith(sys.argv[1]), f"desk imported from {where}, not the clean venv"
now = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
store.create(config.db_path())
with store.open() as con:
    saved = api.save_strategy(con, "example", config.example_strategy().read_text(), now=now)
    run = api.start_run(con, "example", mode="live", at=now, now=now)
    # Invented figures: a market buy of 10 shares of a made-up ticker.
    t = api.trade(con, run.id, ticker="ACME", side="long", entry_type="market", entry=100.0,
                  stop=98.5, target=103.0, size=10, currency="USD", reasons="Smoke test.",
                  at=now, now=now)
    assert api.get_trade(con, t.id).id == t.id
    kinds = [e["kind"] for e in store.events(con, trade_id=t.id)]
    assert len(store.trades(con)) == 1 and kinds == ["entered"], kinds
print(f"installed wheel recorded {t.id} through the API")
PY
"$bin/trading-desk" integrity
