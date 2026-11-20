#!/usr/bin/env bash
# Install a built wheel into a clean venv OUTSIDE the checkout and use it the way a new user
# would: copy the shipped example rules, check one trade, run the integrity checker, and
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
cp "$("$bin/python" -c 'from desk import config; print(config.example_rules())')" \
   "$TRADING_DESK_HOME/rules.md"
"$bin/trading-desk" check --ticker ACME --side long --currency USD \
    --entry 100 --stop 95 --target 110 --size 10 --account paper
"$bin/trading-desk" integrity

"$bin/python" - "$work" <<'PY'
import sys
from pathlib import Path

import desk
from desk import store

where = Path(desk.__file__).resolve()
assert str(where).startswith(sys.argv[1]), f"desk imported from {where}, not the clean venv"
con = store.open(read_only=True)
ids = store.trade_ids(con)
kinds = [e["kind"] for e in store.events(con)]
assert len(ids) == 1 and kinds == ["checked"], (ids, kinds)
print(f"installed wheel recorded {ids[0]} and passed its integrity check")
PY
