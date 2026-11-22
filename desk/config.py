"""Where the desk's data lives. The repo is the tool; the record is not in it.

The author, 2026-09-07: "data is data and code is code. ideally i think it's better to have
the data and code separated. so this repo is pure tool instead of tool combined with
data."

THE DEFAULT IS A REAL PLACE, so a fresh install needs no configuration at all. Everything
lives under one data root:

    ~/Documents/trading-desk/
        desk.db       the store: the owner's decisions, an append-only log
        attachments/  screenshots attached to trades, named by content hash
        bars.db       price bars for live runs, insert-only, and the spans each fetch covered

The ROOT is $TRADING_DESK_HOME, else ~/Documents/trading-desk. The desk reads no settings
file: where the data is belongs with the data, never in the repo.

desk.db resolves most explicit first:

    1. an explicit path passed in code
    2. $TRADING_DESK_DB                one store, absolute
    3. <root>/desk.db                  the default

(2) lets a one-off `TRADING_DESK_DB=/tmp/scratch.db trading-desk integrity` read there.

ISOLATING A RUN: set $TRADING_DESK_HOME (which moves attachments/ and bars.db too).
$TRADING_DESK_DB moves desk.db alone, so screenshots and bars.db still go to the real root.
The test suite clears the override and points the root at a temp dir before every test
(conftest.py).

NOTE ON SYNCED FOLDERS. The default root is under ~/Documents, which iCloud syncs on many
Macs. store.open() sets journal_mode=DELETE precisely so there is no -wal sidecar for a
sync client to upload out of step with its database. Even so, two machines with the file
open at once can corrupt it -- these services sync files, not transactions. One writer at a
time.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from desk.errors import Refused, shown

ENV_HOME = "TRADING_DESK_HOME"
ENV_DB = "TRADING_DESK_DB"

def home() -> Path:
    """The data root. Everything the user owns lives under here.

    A real default rather than an instruction, which is the whole point: no shell
    export, no file to create, nothing in ~/.zshrc. $TRADING_DESK_HOME overrides it,
    and that is also how a test isolates itself from a real book.
    """
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser()
    return Path.home() / "Documents" / "trading-desk"


def db_path(explicit: str | os.PathLike | None = None) -> Path:
    """The store, desk.db. Precedence: explicit, then $TRADING_DESK_DB, then <root>/desk.db."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    from_env = os.environ.get(ENV_DB)
    if from_env:
        return Path(from_env).expanduser().resolve()
    return (home() / "desk.db").resolve()


def refuse_legacy_db() -> None:
    """0.8 read `db` from <root>/config.toml; 0.9 does not. Refuse, rather than quietly open a
    new empty desk.db, when the default location is in use and that file still sets `db`."""
    if os.environ.get(ENV_DB):
        return
    try:
        db = tomllib.loads((home() / "config.toml").read_text(encoding="utf-8")).get("db")
    except (OSError, ValueError):   # no file, or one that is not TOML: ignored as before
        return
    if db is not None:
        raise Refused(f"config.toml in {home()} sets db = {shown(db)}, which the desk no longer "
                      f"reads; set TRADING_DESK_DB to that file (or move it to {home()}/desk.db), "
                      "then remove the db line from config.toml.")


def bars_path() -> Path:
    """bars.db, the price bars. Always under the root: no environment override."""
    return (home() / "bars.db").resolve()


def attachments_dir() -> Path:
    """Where screenshots attached to trades are stored, named by content hash."""
    return (home() / "attachments").resolve()


def icloud_placeholder(path: Path) -> Path:
    """What iCloud leaves beside a file it has evicted from this Mac."""
    return path.parent / f".{path.name}.icloud"


def refuse_if_in_icloud(path: Path) -> None:
    """Refuse when `path` is missing because iCloud has evicted it from this Mac."""
    if icloud_placeholder(path).exists():
        raise Refused(f"desk.db at {path} is in iCloud but not downloaded to this Mac; open "
                      f"{path.parent} in Finder to download it, then call again.")


# The currencies a trade may be in.
CURRENCIES = ("CAD", "USD")


def schema_sql() -> Path:
    """The store's schema. Package-relative.

    PACKAGE-RELATIVE, NOT REPO-RELATIVE, and that was a real defect rather than a tidy-up.
    It returned `REPO / "book" / "schema.sql"` where REPO is this file's grandparent -- which
    is the checkout in development and `site-packages/` in an installed wheel. `book/` was
    not a package, so an installed copy resolved to `site-packages/book/schema.sql`, which
    does not exist: `pip install` produced a tool that could not create a book. Proven by
    installing the wheel in a clean venv before the move.

    A plain path works for both cases because a wheel is unpacked into site-packages, so
    `desk/schema.sql` is a real file on disk either way -- no importlib.resources needed.
    """
    return Path(__file__).resolve().parent / "schema.sql"


def bars_sql() -> Path:
    """bars.db's schema. Package-relative, as schema_sql() is and for the same reason."""
    return Path(__file__).resolve().parent / "bars.sql"


def example_strategy() -> Path:
    """The worked example strategy a new user copies to start their own. Package-relative,
    a real file on disk for a checkout and an installed wheel alike."""
    return Path(__file__).resolve().parent / "strategy.example.md"


def instructions() -> Path:
    """The agent's standing instructions, sent as the MCP server's `instructions`.
    Package-relative, as schema_sql() is."""
    return Path(__file__).resolve().parent / "instructions.md"
