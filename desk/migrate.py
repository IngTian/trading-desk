"""Create a book, or check that an existing one matches the schema in the repo.

    desk-migrate --new  [PATH]    create an empty book
    desk-migrate --check [PATH]   compare a book against schema.sql

With no PATH, uses the same resolution as the desk: $TRADING_DESK_BOOK, then
`book =` in config.toml inside the data root, then <root>/hub.db -- where the root is
$TRADING_DESK_HOME or ~/Documents/trading-desk. So `--new` with no arguments creates
the book at the default place, and that is the intended first run.

WHAT A FRESH BOOK CONTAINS: the schema, and the two singleton rows the schema needs
to function -- `session` (which actor the events log attributes writes to) and
NOTHING ELSE. `clock` went in v14 with the rules that read it.

No accounts, no instruments, and NO RULES -- and by v15 there is no longer anywhere a
rule could go. This paragraph used to say the tool knew the SHAPE of a rule (wall or
dial or prompt, versioned, with effective dates) while the numbers stayed the user's,
and pointed at a SYSTEM.md for the derivations. The shape went with the rules engine in
v14, the file was deleted in v15, and `loss_limit()` had already gone with them.

A fresh book is therefore just the structure: what you record and nothing that judges
it.

--check exists because the schema and the data now live apart, and things that live
apart drift. It compares normalised DDL object by object and names what differs, so
a book created before a schema change is a reported gap rather than a mystery.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

# NO sys.path HACK. This file lived in book/ and had to reach up a directory to import
# desk.config, so it carried its own two-line sys.path.insert. It is a module inside the
# package now -- moved there 2026-09-30 so that `pip install trading-desk` ships the schema
# and this script, without which an installed user could not create a book at all.
#
# Run it as `python -m desk.migrate` (or the `desk-migrate` console script), never as a path:
# a relative import fails when a package module is executed as a file.
from . import config

# The version desk/schema.sql represents. Kept HERE rather than as an INSERT inside
# schema.sql so that file stays pure structure -- and a test asserts this matches the
# live book's MAX(schema_version), so the two cannot drift silently.
SCHEMA_VERSION = 16


def _structure(sql: str) -> str:
    """DDL with comments and whitespace removed. What "the same object" means here.

    SQLite stores a CREATE statement's text VERBATIM, comments included, so every comment in
    schema.sql ends up inside the book. That makes an edit to a comment indistinguishable
    from an edit to a constraint as far as a string compare is concerned -- and it bit
    exactly that way: rewording the comments on 2026-09-27 made `--check` report DDL drift
    on 15 objects whose structure had not moved at all.

    COMMENTS ARE NOT SCHEMA. A reworded CHECK comment needs no migration; a reworded CHECK
    does, and still shows up here because the expression itself changes. Stripping them makes
    "drift" mean one thing -- structure -- which is the same principle as collapsing
    whitespace, one step further.

    The cost, stated: a book carries the comments it was BUILT with, so the explanations
    inside an older `sqlite3 hub.db .schema` can be older than the repo's. schema.sql is the
    file anyone reads, and `--upgrade` refreshes the book's copy whenever a real version
    lands.
    """
    sql = re.sub(r"--[^\n]*", " ", sql)          # line comments
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)   # block comments
    return " ".join(sql.split())


def fingerprint(con: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Structural DDL per object. A reformat or a reworded comment is not a change."""
    return {(t, n): _structure(s) for t, n, s in con.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL")}


def create(path: Path) -> int:
    # A ZERO-BYTE FILE IS NOT A BOOK. One gets left behind whenever something
    # connects to a path before it exists, and refusing with "already exists" sends
    # the reader looking for a database that is not there.
    if path.exists() and path.stat().st_size == 0:
        print(f"note: {path} exists but is EMPTY (0 bytes), which is not a book. "
              f"Replacing it.")
        path.unlink()
    if path.exists():
        print(f"FAIL: {path} already exists. Refusing to touch a book that is "
              f"already there — use --check, or move it aside first.")
        return 1
    schema = config.schema_sql()
    if not schema.exists():
        print(f"FAIL: no schema at {schema}")
        return 1
    # The default root will not exist on a fresh machine, and that is the normal
    # first run rather than a mistake. Creating it is the whole point of --new.
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None)
    con.executescript(schema.read_text())
    con.execute("PRAGMA journal_mode = DELETE")     # never WAL: sync-client safety

    # The two singletons. Both tables are constrained to exactly one row, so the
    # database is unusable without them and there is nothing to decide.
    con.execute("INSERT INTO session(actor) VALUES ('migrate.py')")
    # $TRADING_DESK_TODAY WAS READ HERE to seed the `clock` singleton, which every
    # rules-in-force view compared its dates against. The clock went in v14 with those
    # rules, and the read outlived it by two versions with nothing using the value.

    # Record the version this book was BUILT at, so --check and any future forward
    # migration know where it starts. Without it a fresh book reads as version None
    # and is indistinguishable from one predating the version table.
    con.execute("INSERT INTO schema_version VALUES (?, date('now'), 'schema.sql', ?)",
                (SCHEMA_VERSION,
                 "Created from desk/schema.sql, which is the extracted state of the "
                 "book at version 9 -- versions 1 through 9 were applied to the "
                 "original database before the schema became code, and their notes "
                 "are in that book's own schema_version table."))

    n = len(fingerprint(con))
    con.close()
    print(f"created {path} at schema version {SCHEMA_VERSION}")
    print(f"  {n} schema objects, and the session singleton")
    print("  no accounts, no instruments, NO RULES — a fresh book has no loss limit, "
          "and that is the honest state")
    return 0


def check(path: Path) -> int:
    if not path.exists():
        print(f"FAIL: no book at {path}")
        return 1
    if path.stat().st_size == 0:
        print(f"FAIL: {path} is EMPTY (0 bytes) — not a book. Something connected to "
              f"this path before it existed; delete it and run --new.")
        return 1
    # REPORT, DO NOT RAISE. The whole promise of --check is turning a mystery into a
    # named gap, and it was raising a bare sqlite3 exception for every case that is
    # not a well-formed book: a non-database, a truncated file, or a book predating
    # the schema_version table.
    try:
        live = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        a = fingerprint(live)
    except sqlite3.DatabaseError as exc:
        print(f"FAIL: {path} is not a readable SQLite database — {exc}")
        return 1
    ref = sqlite3.connect(":memory:")
    ref.executescript(config.schema_sql().read_text())
    b = fingerprint(ref)

    only_live = sorted(set(a) - set(b))
    only_ref = sorted(set(b) - set(a))
    differs = sorted(k for k in set(a) & set(b) if a[k] != b[k])

    try:
        ver = live.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    except sqlite3.OperationalError:
        # No schema_version table at all. That IS the finding, not a crash.
        ver = "UNKNOWN (no schema_version table)"
    print(f"{path}")
    print(f"  schema version {ver}, {len(a)} objects")
    if not (only_live or only_ref or differs):
        print("  MATCHES desk/schema.sql exactly")
        live.close()
        return 0
    # Named, not counted. "3 objects differ" sends you back to a binary; the names
    # send you to the line.
    for label, items in (("in the book but NOT in schema.sql", only_live),
                         ("in schema.sql but NOT in the book", only_ref),
                         ("DIFFERENT DDL", differs)):
        if items:
            print(f"  {label}:")
            for t, n in items:
                print(f"      {t:<8} {n}")
    print("\n  The schema and the data live apart now, so they can drift. "
          "Re-extract schema.sql if the book is right; migrate the book if it is not.")
    live.close()
    return 1


def _table_columns(con, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def upgrade(path: Path, *, note: str, apply: bool = False) -> int:
    """Rebuild a book at the current SCHEMA_VERSION, carrying every row across.

    THERE WAS NO FORWARD MIGRATION AT ALL. create() built a book and check() reported
    drift, and between them the answer to "the schema changed" was to start a new book or
    edit SQLite by hand. schema_version's own comment promised this: "so --check and any
    future forward migration know where it starts."

    REBUILD AND COPY, not a sequence of ALTERs. schema.sql is the source of truth and is
    round-trip verified against a real book, so the safest transformation is to build a
    fresh database FROM IT and move the rows over: no 12-step table rebuild to get wrong,
    no dependent views to drop and recreate in the right order, and a widened column or a
    dropped NOT NULL needs no special handling at all.

    THE TRIGGERS ARE DROPPED FOR THE COPY and recreated afterwards. The ev_* triggers
    write an events row for every INSERT, so copying with them live would append thousands
    of fabricated audit entries dated today for rows that were written weeks ago -- an
    append-only log describing a migration as if it were trading.

    NOTHING IS DROPPED SILENTLY. A table or column that the new schema does not have is
    reported with its row count before anything is written, and without --apply this
    function only reports. Data loss should be a sentence someone read, not a surprise.
    """
    if not path.exists():
        print(f"FAIL: no book at {path}")
        return 1
    # as_uri() for the reason in desk/mcp.py -- and here the stakes are higher: an
    # unescaped `?` in the path made this open a NEW EMPTY file, so `was` came back None,
    # `old_tables` was empty, the dry run announced all 16 existing tables as "new", the
    # THIS DROPS DATA block never fired, the copy carried zero rows, and the swap below put
    # the empty rebuild over the real book while printing "upgraded" and exiting 0.
    live = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        was = live.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    except sqlite3.OperationalError:
        was = None
    if was == SCHEMA_VERSION:
        print(f"{path} is already at schema version {SCHEMA_VERSION}")
        live.close()
        return 0
    print(f"{path}: schema version {was} → {SCHEMA_VERSION}")

    ref = sqlite3.connect(":memory:")
    ref.executescript(config.schema_sql().read_text())
    new_tables = {r[0] for r in ref.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    old_tables = {r[0] for r in live.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    # ---- say what will be lost, BEFORE touching anything ------------------
    losing_tables, losing_cols = [], []
    for t in sorted(old_tables - new_tables):
        n = live.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        losing_tables.append((t, n))
    for t in sorted(old_tables & new_tables):
        gone = [c for c in _table_columns(live, t) if c not in _table_columns(ref, t)]
        for c in gone:
            n = live.execute(
                f'SELECT COUNT(*) FROM "{t}" WHERE "{c}" IS NOT NULL').fetchone()[0]
            losing_cols.append((t, c, n))
    if losing_tables or losing_cols:
        print("\n  THIS DROPS DATA:")
        for t, n in losing_tables:
            print(f"      table  {t:<24} {n} row(s)")
        for t, c, n in losing_cols:
            print(f"      column {t}.{c:<20} {n} row(s) hold a value")
    for t in sorted(new_tables - old_tables):
        print(f"  new table: {t}")

    if not apply:
        print("\n  This was a dry run. Re-run with --apply to write it.")
        live.close()
        return 0

    # ---- build the target, without triggers -------------------------------
    target = path.with_suffix(f".v{SCHEMA_VERSION}.new")
    if target.exists():
        target.unlink()
    out = sqlite3.connect(target, isolation_level=None)
    out.executescript(config.schema_sql().read_text())
    out.execute("PRAGMA journal_mode = DELETE")
    triggers = [r[0] for r in out.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL")]
    for name in [r[0] for r in out.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")]:
        out.execute(f'DROP TRIGGER "{name}"')

    # ---- copy every shared table, column intersection ---------------------
    out.execute("PRAGMA foreign_keys = OFF")
    # BOUND, NOT INTERPOLATED. As a SQL literal, a path containing an apostrophe --
    # `~/Nat's books/hub.db` -- aborted the upgrade here with a raw syntax error, after the
    # rebuild had been created and before anything was copied.
    out.execute("ATTACH DATABASE ? AS old", (str(path),))
    copied = {}
    for t in sorted(new_tables & old_tables):
        cols = [c for c in _table_columns(out, t) if c in _table_columns(live, t)]
        if not cols:
            continue
        q = ",".join(f'"{c}"' for c in cols)
        out.execute(f'INSERT INTO main."{t}" ({q}) SELECT {q} FROM old."{t}"')
        copied[t] = out.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
    out.execute("DETACH DATABASE old")

    for sql in triggers:
        out.execute(sql)
    out.execute(
        "INSERT INTO schema_version VALUES (?, date('now'), 'migrate.py --upgrade', ?)",
        (SCHEMA_VERSION, note))

    # ---- verify before swapping ------------------------------------------
    problems = [r for r in out.execute("PRAGMA foreign_key_check")]
    integrity = out.execute("PRAGMA integrity_check").fetchone()[0]
    print("\n  rows carried across:")
    for t, n in sorted(copied.items()):
        old_n = live.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        flag = "" if n == old_n else f"   MISMATCH: was {old_n}"
        print(f"      {t:<24} {n}{flag}")
        if n != old_n:
            problems.append((t, n, old_n))
    out.close()
    live.close()
    if problems or integrity != "ok":
        print(f"\n  REFUSING TO SWAP: integrity_check={integrity}, "
              f"{len(problems)} problem(s). The rebuilt book is at {target} for "
              f"inspection; the original is untouched.")
        return 1

    # NOTHING COPIED MEANS NOTHING TO SWAP, and this guard is the one that would have
    # contained the URI bug above rather than merely fixing it. If the source opened as an
    # empty database -- a mis-escaped path, a truncated file, a book from the future with no
    # tables in common -- every check above passes vacuously: no rows to mismatch, no columns
    # to lose, integrity_check "ok" on an empty file. The swap then replaces a real book with
    # a blank one and prints "upgraded". A rebuild that carried no rows at all is never what
    # anyone meant.
    if not copied or not any(copied.values()):
        print(f"\n  REFUSING TO SWAP: the rebuild carried no rows at all, which means the "
              f"source read as empty. The original is untouched; the rebuild is at {target} "
              f"for inspection. Check that {path} is really a book.")
        return 1

    keep = path.with_suffix(f".pre-v{SCHEMA_VERSION}.db")
    path.replace(keep)
    target.replace(path)
    print(f"\n  upgraded. The book before this is kept at {keep.name}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--new", action="store_true", help="create an empty book")
    g.add_argument("--check", action="store_true",
                   help="compare a book against desk/schema.sql")
    g.add_argument("--upgrade", action="store_true",
                   help="rebuild a book at the current schema version, carrying its rows")
    p.add_argument("--apply", action="store_true",
                   help="with --upgrade, actually write it (otherwise a dry run)")
    p.add_argument("--note", default="rebuilt by migrate.py --upgrade",
                   help="with --upgrade, why this version happened")
    p.add_argument("path", nargs="?",
                   help="defaults to the resolved book: $TRADING_DESK_BOOK, "
                        "config.toml, then ~/Documents/trading-desk/hub.db")
    args = p.parse_args(argv)
    path = config.book_path(args.path)
    if args.new:
        return create(path)
    if args.upgrade:
        return upgrade(path, note=args.note, apply=args.apply)
    return check(path)


if __name__ == "__main__":
    sys.exit(main())
