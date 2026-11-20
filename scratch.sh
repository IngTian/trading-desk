#!/usr/bin/env bash
# A THROWAWAY DESK, to try commands on without touching the real record.
#
#   bash scratch.sh check --ticker ACME --side long --currency USD --entry 10 --stop 9 \
#        --target 13 --size 10 --account paper
#   bash scratch.sh --fresh check ...      wipe the scratch desk first
#
# Any trading-desk command, run against a data root under $TMPDIR seeded with the shipped
# example rules. The events table is append-only, so a test trade written to the real
# desk.db could never be removed: that is a mistake worth making impossible rather than
# documenting.
#
# WHY BOTH OVERRIDES ARE UNSET. $TRADING_DESK_DB beats the data root, so an exported one
# would send this "scratch" run straight to whatever file it names; and $TRADING_DESK_BOOK
# does not isolate v1 at all (it names only the frozen legacy book).
set -euo pipefail

ROOT="${TMPDIR:-/tmp}/trading-desk-scratch"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = "--fresh" ]; then
  rm -rf "$ROOT"
  shift
fi
mkdir -p "$ROOT"
[ -f "$ROOT/rules.md" ] || cp "$HERE/desk/rules.example.md" "$ROOT/rules.md"

unset TRADING_DESK_DB TRADING_DESK_BOOK
export TRADING_DESK_HOME="$ROOT"
echo "  scratch desk at $ROOT (the real ~/Documents/trading-desk is not reachable from here)" >&2
exec uv run --project "$HERE" trading-desk "$@"
