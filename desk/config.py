"""Where the BOOK lives. The repo is the tool; the record is not in it.

The author, 2026-09-07: "data is data and code is code. ideally i think it's better to have
the data and code separated. so this repo is pure tool instead of tool combined with
data."

THE DEFAULT IS A REAL PLACE, so a fresh install needs no configuration at all:

    ~/Documents/trading-desk/hub.db

Shape borrowed from daylogs, which has the same problem and solves it better than the
first attempt here did. That attempt put a `desk.toml` in the REPO -- a file about
where the data is, living in the tool -- which is the same category error this session
spent its length removing from `notes_ref` and the vault path. Configuration belongs
WITH the data. daylogs' own config.py says it plainly: "Every key has a default, so an
absent file is a supported state -- a fresh install runs with no configuration at all."

Resolution, most explicit first:

    1. an explicit path passed in code
    2. $TRADING_DESK_BOOK              one book, absolute. Also how tests isolate.
    3. `book = "..."` in config.toml   inside the data root
    4. <root>/hub.db                   the default

and the ROOT itself is $TRADING_DESK_HOME, else ~/Documents/trading-desk.

(2) deliberately beats (3) so a one-off `TRADING_DESK_BOOK=/tmp/scratch.db desk
positions` reads that book rather than silently using the configured one.

THERE IS NO IN-REPO FALLBACK any more. There was, while the database was being moved
out of the repository, and it printed a note saying so. Keeping it now would be
actively harmful: it would prefer a stale book left in a checkout over the real one at
the default path, silently, which is exactly the trap the note existed to warn about.

NOTE ON SYNCED FOLDERS. A book on iCloud or Dropbox is workable but not free.
book.connect() sets journal_mode=DELETE precisely so there is no -wal sidecar for a
sync client to upload out of step with its database. Even so, two machines with the
file open at once can corrupt it -- these services sync files, not transactions.
One writer at a time.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# The repo root, resolved from this file so the working directory is irrelevant.
# Used ONLY to find schema.sql, which is code and ships with the tool.
REPO = Path(__file__).resolve().parent.parent

ENV_HOME = "TRADING_DESK_HOME"
ENV_BOOK = "TRADING_DESK_BOOK"

# THERE IS NO VAULT SETTING. There was one, briefly, and the author removed it on
# 2026-09-07: "maybe we should remove the TRADING_DESK_VAULT as well. so the tool
# stays on the book only. i manually handle the thesis, i create notes via shortcuts
# in obsidian or by chatting with AI, the book tool only manages the [book]."
#
# So this tool touches exactly one thing: the book. It does not read the vault, write
# to it, create folders in it, or have an opinion about whether a bet's thesis exists.
# The two live in separate repositories and share one string -- the bet's name -- and
# nothing resolves that string to a file any more.


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


_THEME_LINE = re.compile(r"^\s*theme\s*=.*$", re.M)


def save_theme(name: str) -> Path | None:
    """Persist the chosen theme, PRESERVING EVERYTHING ELSE IN THE FILE.

    Returns the path written, or None if it could not be -- the caller says so rather than
    pretending the choice will survive a restart.

    A LINE EDIT, NOT A TOML REWRITE. `tomllib` reads and does not write, and the round trip
    through any writer that does would discard this file's comments -- which explain what
    each key is for and are the reason it is editable by hand at all. So the one line is
    replaced in place, or appended if absent.

    Only a TOP-LEVEL `theme =` is matched: the search stops at the first `[table]` header,
    so a `theme` key inside some future section is never mistaken for this one.
    """
    p = config_file()
    text = p.read_text() if p.is_file() else ""
    head, sep, tail = text.partition("\n[")
    line = f'theme = "{name}"'
    if _THEME_LINE.search(head):
        head = _THEME_LINE.sub(line, head, count=1)
    else:
        head = (head.rstrip("\n") + "\n\n" if head.strip() else "") + line + "\n"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(head + sep + tail)
    except OSError:
        return None
    global _config_cache
    _config_cache = None            # so the next read sees what was just written
    return p


def book_path(explicit: str | os.PathLike | None = None) -> Path:
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


# The currencies the book can translate between. Two, because there are exactly two
# recorded FX pairs in play and a third would need a rate nobody fetches.
CURRENCIES = ("CAD", "USD")


def profile() -> Profile:
    """WHO the book belongs to and WHAT CURRENCY it reports in.

    The author asked for this in daylogs' shape: "we need a profile for this tool. inside the
    profile, you need my name, and my home currency."

    Every key has a default, so an absent config.toml is a supported state and a fresh
    install runs unconfigured -- the same promise daylogs' own config makes. A BAD value is
    corrected rather than raised on: home_currency = "EUR" falls back to CAD with a note,
    because a typo in a profile should not stop the desk opening.
    """
    raw = config()
    name = str(raw.get("name") or "").strip()
    ccy = str(raw.get("home_currency") or "").strip().upper()
    return Profile(
        name=name or None,
        home_currency=ccy if ccy in CURRENCIES else "CAD",
        home_currency_was=ccy if ccy and ccy not in CURRENCIES else None,
    )


@dataclass(frozen=True)
class Profile:
    name: str | None
    home_currency: str
    # The rejected value, if one was written. Kept so the desk can SAY it ignored it --
    # silently defaulting a currency would misquote every figure on the screen.
    home_currency_was: str | None = None


def book_source() -> str:
    """WHY the current book, for `desk where`. Four things can decide it."""
    if os.environ.get(ENV_BOOK):
        return f"${ENV_BOOK}"
    if config().get("book"):
        return f"`book =` in {config_file()}"
    return f"the default, {ENV_HOME} unset" if not os.environ.get(ENV_HOME) \
        else f"the default under ${ENV_HOME}"


def schema_sql() -> Path:
    """The schema is CODE and ships with the tool, wherever the data is.

    PACKAGE-RELATIVE, NOT REPO-RELATIVE, and that was a real defect rather than a tidy-up.
    It returned `REPO / "book" / "schema.sql"` where REPO is this file's grandparent -- which
    is the checkout in development and `site-packages/` in an installed wheel. `book/` was
    not a package, so an installed copy resolved to `site-packages/book/schema.sql`, which
    does not exist: `pip install` produced a tool that could not create a book. Proven by
    installing the wheel in a clean venv before the move.

    A plain path works for both cases because a wheel is unpacked into site-packages, so
    `desk/schema.sql` is a real file on disk either way -- no importlib.resources needed, and
    `--new` can hand the path to sqlite3 directly the way it always has.

    NO BUNDLE BRANCH. This also read sys._MEIPASS so a PyInstaller build could find the
    schema it had bundled; the build path was deleted on 2026-09-27 -- the author: "No --
    delete it" -- because its artifact started ~80x slower than the console script and
    could not create the book it refused to start without.
    """
    return Path(__file__).resolve().parent / "schema.sql"
