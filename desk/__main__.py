"""The desk. One entry point: `desk` opens the TUI, and everything is settled in there.

The author, 2026-09-08: "i think you should remove those desk .... i would only evern type desk
and settle things inside the tui."

THERE WERE SEVEN SUBCOMMANDS and they are gone: positions, balance, marks, where, account,
instrument, asof. Each existed for a reason, and each reason is now answered inside the TUI
rather than beside it:

    desk positions    the positions tab
    desk balance      the dashboard, which is tab 1
    desk marks        nothing to press: marks refresh every 30s on their own
    desk where        `?`, which names the book in use and why that one
    desk account add  `a` on the dashboard, where the sleeves are listed
    desk instrument   nothing to press: a ticker is looked up on its first fill
    desk asof         nothing to press: the clock went with the rules that read it

THREE OF THOSE SEVEN ENTRIES USED TO NAME A KEY THAT NO LONGER EXISTS -- `m`, `i` and `k`,
each deleted after this list was written. So did the map in main() below, and so did the
README. That is this project's most-repeated defect and it is now a test:
test_no_user_facing_string_names_an_unbound_key asserts that nothing here or in the footer
tells anyone to press something that is bound to nothing.

The point is not tidiness. TWO SURFACES MEANT TWO ANSWERS: `desk balance` had its own
obligation printer, its own exit codes and its own early-return that dropped every
obligation when there was no snapshot -- a bug that existed only because the same figures
were rendered twice. One surface cannot disagree with itself.

CREATING A BOOK IS NOW OFFERED HERE, and it used to be impossible for anyone who had
installed the tool rather than cloned it: creation was `python book/migrate.py --new`, a file
no wheel ever shipped, so `pip install trading-desk` produced something that could not make
the book it refuses to start without.

It is still not automatic, for the reason it never was: a mistyped $TRADING_DESK_BOOK would
silently produce a second empty book, and "which book produced this figure" is a worse
question to have to ask than a y/n is to read. So _offer_to_create prints the resolved path
AND what chose it, then waits. A typo is visible in the line you are being asked to confirm,
which is what makes asking safe rather than merely polite. `desk-migrate --new` still exists
for anyone scripting it.

The connection is opened HERE and injected into the App, which is what makes the app
testable: a test builds it against a temporary book rather than the real one.
"""
from __future__ import annotations

import os
import sys

from . import book, config


def _no_textual(exc: ModuleNotFoundError) -> int:
    """Textual is now required, because the TUI is the whole tool.

    It used to be optional and this message used to end by naming the read-only commands
    that worked without it. There are none left, so it says what to run instead.
    """
    if exc.name != "textual":
        raise exc
    venv = os.path.join(book.ROOT, ".venv", "bin", "python")
    hint = (f"    {venv} -m desk" if os.path.exists(venv)
            else "    uv sync   # then retry")
    print(f"the desk needs Textual, and this interpreter does not have it:\n"
          f"    {sys.executable}\n\n"
          f"run it with the project's environment instead:\n"
          f"{hint}\n"
          f"    uv run python -m desk", file=sys.stderr)
    return 1


def _offer_to_create(path) -> bool:
    """No book here yet. Say where one would go, say what chose that, then ask.

    ASKING RATHER THAN DOING, and the reason is in this module's own docstring: creating a
    book automatically means a mistyped $TRADING_DESK_BOOK silently produces a SECOND empty
    one, and "which book produced this figure" is a worse question to have to answer than a
    y/n is to read. Printing the resolved path and what decided it removes that failure --
    a typo is visible in the line you are being asked to confirm.

    ASKING RATHER THAN A SUBCOMMAND, because the author kept "i would only ever type desk and
    settle things inside the tui" when this was decided on 2026-09-30. `desk-migrate --new`
    still exists for anyone scripting it.

    NOT A TUI SCREEN. There is no book to open, so there is no app to draw one in; this runs
    before Textual is imported. Returns True when a book now exists.
    """
    print("no book yet.\n")
    print(f"  it would be created at   {path}")
    # `book_source()` ALREADY EXISTS for the `?` overlay, which names the book in use and why
    # that one. The same sentence is what makes this prompt safe rather than merely polite: a
    # typo'd $TRADING_DESK_BOOK is only obvious if you are told the variable is what chose
    # the path you are being asked to confirm.
    print(f"  because                  {config.book_source()}")
    print("\n  the book is DATA and lives outside this repo, so back up that path.")
    print("  to put it somewhere else, answer n and set TRADING_DESK_BOOK first.\n")
    try:
        answer = input("create it? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if answer not in ("y", "yes"):
        print("nothing written.", file=sys.stderr)
        return False
    from .migrate import create
    return create(path) == 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # --version BEFORE the no-arguments refusal below. It is the one flag an installed
    # command is expected to answer, it is what a packaging smoke test can assert without a
    # book or a terminal, and it costs nothing: the number comes from the installed
    # distribution rather than a second copy in the source.
    if argv and argv[0] in ("--version", "-V"):
        from importlib.metadata import PackageNotFoundError, version
        try:
            print(f"trading-desk {version('trading-desk')}")
        except PackageNotFoundError:
            print("trading-desk (not installed; running from a checkout)")
        return 0
    if argv:
        # NAMES WHERE EACH ONE WENT. A tool that answers a removed subcommand with
        # "unrecognised arguments" teaches nothing; muscle memory outlives a release.
        moved = {
            "positions": "the positions tab (2)",
            "balance": "the dashboard, which is tab 1",
            # `m` WAS DELETED when the refresh became automatic, so naming it here sent
            # someone to a key that does nothing.
            "marks": "automatic — prices reload every 30s and whenever holdings change",
            "where": "?",
            "account": "a on the dashboard",
            # `i` WAS DELETED TOO -- a test asserts it is bound to nothing, because the
            # instrument key is the UX this project removed on purpose: an unknown ticker
            # is now looked up at the price source rather than registered by hand.
            "instrument": "gone: a ticker is looked up when you first record a fill",
            # `k` went with the frozen clock in v14, along with every rule that read it.
            "asof": "gone: the clock it set was deleted with the rules that read it",
        }
        first = argv[0]
        print("`desk` takes no arguments — it opens the desk and everything is done "
              "in there.", file=sys.stderr)
        if first in moved:
            print(f"`desk {first}` is now {moved[first]}.", file=sys.stderr)
        print(f"\nthe book in use is {config.book_path()}", file=sys.stderr)
        return 2

    # DeskError is written to be read: its docstring says "shown verbatim to the user, so
    # it reads as guidance". Nothing showed it verbatim once -- it escaped as a traceback
    # with the guidance buried at the bottom, found by pointing the built binary at a path
    # with no book.
    try:
        con = book.connect()
    except (book.DeskError, RuntimeError) as exc:
        # A FIRST RUN IS NOT AN ERROR. Everything else here is, so the missing-book case is
        # separated by asking config for the path rather than by matching on the message.
        path = config.book_path()
        if not path.exists():
            if not _offer_to_create(path):
                return 1
            con = book.connect()
        else:
            print(exc, file=sys.stderr)
            return 1
    try:
        try:
            from .tui.app import DeskApp
        except ModuleNotFoundError as exc:
            return _no_textual(exc)
        DeskApp(con).run()
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
