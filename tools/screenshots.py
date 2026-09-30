#!/usr/bin/env python3
"""Render the README's screenshots from a live app, on SYNTHETIC data.

    uv run python tools/screenshots.py

Writes `assets/{dashboard,positions,bets}.svg`, and a PNG beside each when a converter is
available. Run it after any change that moves the layout, and commit what it produces.

NOTHING HERE TOUCHES THE REAL BOOK. It builds a throwaway one in a temp directory from
`tests/seed.py` -- the same fixture the test suite runs against -- so a screenshot cannot
publish a real position, a real balance, or a real account, and cannot drift from the state
the tests already cover. That reuse is the point: one synthetic book, two consumers.

The figures are deliberately round and obviously invented. A screenshot is the one artifact
where numbers are published verbatim, so it is the one place where "it is only a number"
stops being true.

RENDERED, NOT DRAWN BY HAND, which is the other half of why this exists. The README carried
hand-pasted ASCII of every tab, and it drifted the moment a column width changed -- an
illustration maintained by hand cannot be verified against the thing it illustrates. This one
is the real compositor's output: every border, colour and cell exactly as a terminal draws
them.

DETERMINISTIC. The clock is pinned and the fixture is fixed, so two consecutive runs produce
identical files and a regeneration that shows a diff means the UI actually changed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import seed  # noqa: E402  the test fixture
from rich.text import Text  # noqa: E402
from textual.widgets._header import HeaderClock  # noqa: E402

from desk import book, config  # noqa: E402
from desk.tui.app import DeskApp  # noqa: E402

ASSETS = ROOT / "assets"
# WIDE ENOUGH FOR THE FULL POSITIONS TABLE, which budgets 108 cells. Narrower and the
# screenshots would advertise a clipped layout as the normal one.
SIZE = (130, 44)
TABS = (("1", "dashboard"), ("2", "positions"), ("3", "bets"))
# THE DAY AFTER THE FIXTURE'S MARKS, not an arbitrary date. Pinned earlier, the dashboard
# titled itself "marked dated ahead of today" -- a real warning about a real inconsistency,
# and the wrong thing for a screenshot to advertise as the normal state. Derived from
# seed.AS_OF so it cannot drift if the fixture moves.
NOW = dt.datetime.fromisoformat(seed.AS_OF) + dt.timedelta(hours=16, minutes=30)


async def shoot(con, out: pathlib.Path) -> list[pathlib.Path]:
    app = DeskApp(con)
    # NO POLL AND NO LOOKUP. A screenshot must not depend on a market being open, a network
    # being up, or a rate limit -- and a run that silently fetched would make the output
    # non-deterministic, which is the property this script is for.
    app.POLL_DISABLED = True
    app.quote_lookup = lambda ticker: []
    app.now = lambda: NOW
    written = []
    async with app.run_test(size=SIZE) as pilot:
        # THE HEADER CLOCK IS A REAL CLOCK, and it was the last source of variance: Textual's
        # HeaderClock renders `datetime.now()` directly, so it ignores `app.now` and two runs
        # eight seconds apart produced two different files. Overridden rather than hidden,
        # because the clock is part of the layout and removing it would make the screenshot
        # advertise a header the app does not have.
        for clock in app.query(HeaderClock):
            clock.render = lambda: Text(NOW.strftime("%H:%M:%S"))
            clock.refresh()
        await pilot.pause()
        for key, name in TABS:
            await pilot.press(key)
            await pilot.pause()
            path = out / f"{name}.svg"
            path.write_text(_stable(app.export_screenshot(title=f"desk — {name}")))
            written.append(path)
    return written


def _stable(svg: str) -> str:
    """Pin Rich's per-export CSS namespace, so identical output is identical BYTES.

    Rich prefixes every class in an exported SVG with a random integer --
    `.terminal-1286147539-r1` -- to keep two screenshots on one HTML page from colliding.
    That made consecutive runs differ in about forty lines while the rendered content was
    character-for-character the same, which defeats the reason to commit these files: a diff
    should mean the UI moved.

    Only the namespace is rewritten. Nothing about the drawing is touched.
    """
    return re.sub(r"terminal-\d+-", "terminal-desk-", svg)


def to_png(svg: pathlib.Path, png: pathlib.Path) -> bool:
    """SVG to PNG, by whichever converter is installed. False if none is.

    GITHUB DOES NOT RELIABLY RENDER SVG in a README, which is the only reason this step
    exists -- the SVG is the faithful artifact and the PNG is what a reader actually sees.

    `qlmanage` is last because it is the worst behaved: it names its output after the whole
    filename (`dashboard.svg.png`) and pads the result to a square, so the padding has to be
    cropped back off with `sips`. It is also the only one present on a stock macOS, which is
    why it is here at all.
    """
    attempts = (
        ["rsvg-convert", "-w", "1600", str(svg), "-o", str(png)],
        ["cairosvg", str(svg), "-o", str(png), "--output-width", "1600"],
        ["qlmanage", "-t", "-s", "1600", "-o", str(svg.parent), str(svg)],
    )
    for cmd in attempts:
        if not shutil.which(cmd[0]):
            continue
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            continue
        stamped = svg.parent / f"{svg.name}.png"
        if stamped.exists():
            stamped.replace(png)
        if png.exists():
            if cmd[0] == "qlmanage":
                _crop_padding(svg, png)
            return True
    return False


def _crop_padding(svg: pathlib.Path, png: pathlib.Path) -> None:
    """qlmanage pads to a square; crop back to the SVG's own aspect ratio.

    Rich's root `<svg>` carries only a viewBox, no width or height, so the ratio comes from
    there. Silent on any failure: a padded screenshot is worse than a cropped one but far
    better than no screenshot, and this is a cosmetic step on a best-effort converter.
    """
    m = re.search(r'<svg[^>]*viewBox="0 0 ([0-9.]+) ([0-9.]+)"',
                  svg.read_text()[:4000])
    if not m or not shutil.which("sips"):
        return
    aspect = float(m.group(1)) / float(m.group(2))
    probe = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(png)],
                           capture_output=True, text=True)
    dims = dict(re.findall(r"(pixelWidth|pixelHeight): (\d+)", probe.stdout))
    if len(dims) != 2:
        return
    width = int(dims["pixelWidth"])
    target = max(1, round(width / aspect))
    subprocess.run(["sips", "-c", str(target), str(width), str(png)],
                   capture_output=True)


def main() -> int:
    ASSETS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        # THE PROFILE IS NOT IN THE BOOK, and this is the leak that proves the whole point of
        # the script. The dashboard titles itself from `config.profile()`, which reads
        # config.toml under $TRADING_DESK_HOME -- so a synthetic BOOK is not enough, and the
        # first run of this tool published the author's real name over invented figures.
        # Pointing HOME at the temp directory makes the profile unconfigured, which is a
        # supported state and exactly what a stranger's first run looks like.
        os.environ[config.ENV_HOME] = tmp
        os.environ.pop(config.ENV_BOOK, None)
        con = seed.build(pathlib.Path(tmp) / "demo.db")
        book.set_actor(con, "demo")
        svgs = asyncio.run(shoot(con, ASSETS))
        con.close()

    converted = 0
    for svg in svgs:
        if to_png(svg, svg.with_suffix(".png")):
            converted += 1
    for svg in svgs:
        print(f"  {svg.relative_to(ROOT)}")
    if converted:
        print(f"  + {converted} PNG(s)")
    else:
        print("  no SVG converter found — install librsvg (`brew install librsvg`) for PNGs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
