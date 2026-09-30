#!/usr/bin/env bash
# A THROWAWAY DESK, so the grammar can be hammered on without touching the real record.
#
# The author: "yes we need to test out the grammar. we can test it out with a new db maybe, for
# testing only."
#
#   bash scratch.sh              open a fresh scratch desk (wipes the previous one)
#   bash scratch.sh --keep       reopen the existing scratch desk
#
# It sets both $TRADING_DESK_HOME and $TRADING_DESK_BOOK, so nothing here can reach
# ~/Documents/trading-desk: HOME redirects config.toml as well as the book, which matters
# because the profile lives there and `t` writes to it.
#
# WHY A SCRIPT RATHER THAN A NOTE IN THE README. The safe invocation is two environment
# variables and a migrate call in the right order, and getting it half right points a
# scratch session at the real book. That is a mistake worth making impossible rather than
# documenting.
set -euo pipefail

ROOT="${TMPDIR:-/tmp}/trading-desk-scratch"
BOOK="$ROOT/hub.db"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

if [ "${1:-}" != "--keep" ]; then
  rm -rf "$ROOT"
fi
mkdir -p "$ROOT"

export TRADING_DESK_HOME="$ROOT"
export TRADING_DESK_BOOK="$BOOK"

if [ ! -f "$BOOK" ]; then
  "$PY" -m desk.migrate --new "$BOOK"
  # A fresh book records NOTHING until an account exists, so the sandbox would refuse the
  # first line typed into it. Seeded with one account and a few instruments in both
  # currencies, which is the minimum for the grammar to be exercised at all.
  "$PY" - <<'SEED'
import os
from desk import book
con = book.connect()
book.add_account(con, account_id="tfsa", name="Scratch TFSA",
                 on_date="2026-01-02", make_default=True)
book.add_account(con, account_id="rrsp", name="Scratch RRSP", on_date="2026-01-02")
for tk, ccy, kind, lev in (("ACME", "CAD", "common", None),
                           ("WIDGET", "USD", "common", None),
                           ("PARK", "USD", "money-market", None),
                           ("LEVR", "USD", "leveraged-etf", 3.0)):
    book.add_instrument(con, ticker=tk, name=f"{tk} Inc", currency=ccy, kind=kind,
                        quote_symbol=tk, leverage_factor=lev)
book.add_flow(con, amount=50_000.0, on_date="2026-01-02", note="scratch funding")
con.execute("INSERT INTO prices(on_date,base,quote,amount,kind,source) VALUES "
            "('2026-01-02','USD','CAD',1.38,'fx','scratch')")
con.close()
print(f"  seeded: 2 accounts, 4 instruments, 50,000 CAD in, and a USD->CAD rate")
SEED
fi

echo
echo "  scratch desk at $BOOK"
echo "  the real book at ~/Documents/trading-desk/hub.db is NOT reachable from here"
echo
exec "$PY" -m desk
