"""Where the desk's data lives. The repo is the tool; the record is not in it.

THE DEFAULT IS A REAL PLACE, so a fresh install needs no configuration at all. Everything
lives under one data root:

    ~/Documents/trading-desk/
        desk.db       the store: the owner's decisions, an append-only log
        attachments/  screenshots attached to trades, named by content hash

The ROOT is $TRADING_DESK_HOME, else ~/Documents/trading-desk. The desk reads no settings
file: where the data is belongs with the data, never in the repo.

desk.db resolves most explicit first:

    1. an explicit path passed in code
    2. $TRADING_DESK_DB                one store, absolute
    3. <root>/desk.db                  the default

(2) lets a one-off `TRADING_DESK_DB=/tmp/scratch.db trading-desk integrity` read there.

ISOLATING A RUN: set $TRADING_DESK_HOME (which moves attachments/ too). $TRADING_DESK_DB
moves desk.db alone, so screenshots still go to the real root. The test suite clears the
override and points the root at a temp dir before every test (conftest.py).

NOTE ON SYNCED FOLDERS. The default root is under ~/Documents, which iCloud syncs on many
Macs. store.open() sets journal_mode=DELETE precisely so there is no -wal sidecar for a
sync client to upload out of step with its database. Even so, two machines with the file
open at once can corrupt it -- these services sync files, not transactions. One writer at a
time.
"""
from __future__ import annotations

import os
from pathlib import Path

from desk.errors import Refused

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


# The currencies a run may trade in.
CURRENCIES = ("CAD", "USD")


def schema_sql() -> Path:
    """The store's schema, beside this file: a wheel unpacks desk/ into site-packages, so it is a
    real file in a checkout and an installed copy alike (only a built wheel's smoke test catches
    a path that resolves outside the package)."""
    return Path(__file__).resolve().parent / "schema.sql"


def example_strategy() -> Path:
    """The worked example strategy a new user copies to start their own. Package-relative,
    a real file on disk for a checkout and an installed wheel alike."""
    return Path(__file__).resolve().parent / "strategy.example.md"


def instructions() -> Path:
    """The agent's standing instructions, sent as the MCP server's `instructions`.
    Package-relative, as schema_sql() is."""
    return Path(__file__).resolve().parent / "instructions.md"
