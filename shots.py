#!/usr/bin/env python3
"""Save an SVG of every screen, so a change can be LOOKED AT rather than described.

    uv run python shots.py                    every tab, from the real book
    uv run python shots.py --out /tmp/x       somewhere else
    uv run python shots.py --size 140x50      a wider terminal
    uv run python shots.py --theme nord       one theme
    uv run python shots.py --themes           every screen in every theme

The author: "i dont know if it's possible for you to launch some visual companions or spin up dev
server so that i can see each one and let's resolve each at a time."

THIS IS THE HALF THAT NEEDS NO INSTALL. Textual's App.export_screenshot() renders the real
compositor output -- every colour, border and cell exactly as a terminal would draw it --
into an SVG that opens in any browser. So a pane can be reviewed without either of us
running the app, and two versions of a screen can be put side by side.

The other half is a live desk in a browser, which needs `textual-serve`; see the README.

READ-ONLY BY CONSTRUCTION. It opens the book, screenshots, and writes nothing but SVGs --
and it never presses a key that could write. Point it at a scratch book with
$TRADING_DESK_BOOK if you want to shoot a state the real record does not have.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from desk import book  # noqa: E402
from desk.tui import themes  # noqa: E402
from desk.tui.app import DeskApp  # noqa: E402

# (filename, the keys that get there, what it is)
SCREENS = (
    ("1-dashboard", ["1"], "net, the return split, the sleeves, and the capital ledger"),
    ("2-positions", ["2"], "legs grouped under their bet, and the closed ones"),
    ("3-bets", ["3"], "allocation, spend, weight and P&L per bet"),
    ("4-help", ["1", "question_mark"], "the ? overlay: every key, generated from keymap"),
    ("5-theme-picker", ["1", "t"], "`t`, previewing live"),
    ("6-prompt-fill", ["2", "n"], "the fill prompt, with its hint and placeholder"),
    ("7-prompt-offset", ["1", "o"], "the offset prompt, on the dashboard now"),
)


async def shoot(out: pathlib.Path, size: tuple[int, int], theme: str | None) -> list[str]:
    con = book.connect()
    made = []
    for name, keys, _what in SCREENS:
        app = DeskApp(con)
        # NO POLL AND NO NETWORK. A screenshot must not depend on a market being open, and
        # a fetch mid-capture would change the numbers between one shot and the next.
        app.POLL_DISABLED = True
        async with app.run_test(size=size) as pilot:
            if theme:
                app.theme = theme
            await pilot.pause()
            for key in keys:
                await pilot.press(key)
                await pilot.pause()
            # Twice: the first settles the layout, the second the content that layout
            # revealed -- panes measure their own width and re-wrap on resize.
            await pilot.pause()
            suffix = f".{theme}" if theme else ""
            path = out / f"{name}{suffix}.svg"
            path.write_text(app.export_screenshot(title=f"desk — {name}"))
            made.append(path.name)
    con.close()
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "shots"))
    ap.add_argument("--size", default="120x44", help="COLSxROWS")
    ap.add_argument("--theme", default=None)
    ap.add_argument("--themes", action="store_true", help="every screen in every theme")
    args = ap.parse_args()

    cols, _, rows = args.size.partition("x")
    size = (int(cols), int(rows))
    out = pathlib.Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    wanted = list(themes.names()) if args.themes else [args.theme]
    total = []
    for theme in wanted:
        total += asyncio.run(shoot(out, size, theme))

    print(f"{len(total)} SVG(s) in {out}")
    for name, _keys, what in SCREENS:
        print(f"  {name:<16} {what}")
    print(f"\nopen them:  open {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
