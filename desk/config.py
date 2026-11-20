"""Where the desk's data lives. The repo is the tool; the record is not in it.

The author, 2026-09-07: "data is data and code is code. ideally i think it's better to have
the data and code separated. so this repo is pure tool instead of tool combined with
data."

THE DEFAULT IS A REAL PLACE, so a fresh install needs no configuration at all. Everything
lives under one data root:

    ~/Documents/trading-desk/
        config.toml   optional; every key has a default
        desk.db       the store: the owner's decisions, an append-only log
        attachments/  screenshots attached to trades, named by content hash
        (later releases add price bars, chart imports and tracker data here)

The ROOT is $TRADING_DESK_HOME, else ~/Documents/trading-desk. Configuration belongs WITH
the data, never in the repo: a file about where the data is, living in the tool, is how a
checkout ends up carrying one person's paths.

desk.db resolves most explicit first:

    1. an explicit path passed in code
    2. $TRADING_DESK_DB                one store, absolute
    3. `db = "..."` in config.toml     relative paths resolve against the root
    4. <root>/desk.db                  the default

(2) deliberately beats (3) so a one-off `TRADING_DESK_DB=/tmp/scratch.db trading-desk
integrity` reads there rather than to the configured store.

ISOLATING A RUN: set $TRADING_DESK_HOME (which moves config.toml and attachments/ too).
$TRADING_DESK_DB moves desk.db alone, so screenshots still go to the real root. NOT
$TRADING_DESK_BOOK: 0.5 does not use it (in 0.4 it named hub.db), so a scratch run that
sets just it still writes to the real desk.db. The test suite
clears both overrides and points the root at a temp dir before every test (conftest.py).

NOTE ON SYNCED FOLDERS. The default root is under ~/Documents, which iCloud syncs on many
Macs. store.open() sets journal_mode=DELETE precisely so there is no -wal sidecar for a
sync client to upload out of step with its database. Even so, two machines with the file
open at once can corrupt it -- these services sync files, not transactions. One writer at a
time.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "TRADING_DESK_HOME"
ENV_BOOK = "TRADING_DESK_BOOK"
ENV_DB = "TRADING_DESK_DB"

# THE VAULT. The author removed the setting on 2026-09-07: "maybe we should remove the
# TRADING_DESK_VAULT as well. so the tool stays on the book only. i manually handle the
# thesis, i create notes via shortcuts in obsidian or by chatting with AI, the book tool
# only manages the [book]."
#
# The 2026-11-20 design (§2) keeps that boundary: the desk never writes the vault; it reads
# and writes only its own data. `vault_path()` survives as an OPTIONAL config.toml setting
# that nothing in the package reads. The ENVIRONMENT VARIABLE stays gone, so there is no
# one-off override the way TRADING_DESK_DB provides.


_config_cache: dict | None = None


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


def config_file() -> Path:
    """`config.toml` INSIDE the data root, never in the repo.

    A file describing where the data is belongs with the data. Putting it in the tool
    is how a checkout ends up carrying one person's paths.
    """
    return home() / "config.toml"


def config() -> dict:
    """Parse config.toml once. Absent is fine; MALFORMED is an error.

    Swallowing a syntax error would send someone hunting for a missing book with the
    answer being an unclosed quote in a file they have forgotten they wrote.
    """
    global _config_cache
    if _config_cache is None:
        p = config_file()
        if not p.is_file():
            _config_cache = {}
        else:
            import tomllib
            try:
                _config_cache = tomllib.loads(p.read_text())
            except tomllib.TOMLDecodeError as exc:
                raise RuntimeError(f"{p} is not valid TOML: {exc}") from None
    return _config_cache


def book_path(explicit: str | os.PathLike | None = None) -> Path:
    """0.4's book path, hub.db. Nothing in 0.5 calls this; the old setting still resolves.

    Explicit path, then $TRADING_DESK_BOOK, then `book =` in config.toml, then <root>/hub.db.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    from_env = os.environ.get(ENV_BOOK)
    if from_env:
        return Path(from_env).expanduser().resolve()
    from_file = config().get("book")
    if from_file:
        p = Path(str(from_file)).expanduser()
        # A relative path in config.toml is relative to the ROOT, not to the cwd --
        # otherwise the same config means different books from different directories.
        return (p if p.is_absolute() else home() / p).resolve()
    return (home() / "hub.db").resolve()


def data_dir() -> Path:
    """The data root; the same as home()."""
    return home()


def db_path(explicit: str | os.PathLike | None = None) -> Path:
    """The store, desk.db. Precedence: explicit → env var → config.toml → default."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    from_env = os.environ.get(ENV_DB)
    if from_env:
        return Path(from_env).expanduser().resolve()
    from_file = config().get("db")
    if from_file:
        p = Path(str(from_file)).expanduser()
        return (p if p.is_absolute() else home() / p).resolve()
    return (home() / "desk.db").resolve()


def bars_dir() -> Path:
    """Where fetched OHLC bars are cached."""
    return (home() / "bars").resolve()


def reports_dir() -> Path:
    """Where generated reports are written."""
    return (home() / "reports").resolve()


def attachments_dir() -> Path:
    """Where screenshots attached to trades are stored, named by content hash."""
    return (home() / "attachments").resolve()


def imports_dir() -> Path:
    """Where imported trade CSVs are staged."""
    return (home() / "imports").resolve()


def vault_path() -> Path | None:
    """The obsidian vault. Optional; comes from config.toml only."""
    from_file = config().get("vault")
    if not from_file:
        return None
    p = Path(str(from_file)).expanduser()
    return (p if p.is_absolute() else home() / p).resolve()


def legacy_book() -> Path:
    """0.4's book; nothing in 0.5 calls this. Same path resolution as book_path()."""
    return book_path()


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


def example_strategy() -> Path:
    """The worked example strategy a new user copies to start their own. Package-relative,
    a real file on disk for a checkout and an installed wheel alike."""
    return Path(__file__).resolve().parent / "strategy.example.md"
