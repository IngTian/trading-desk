#!/usr/bin/env python3
"""Fail if a tracked file reports a figure as coming from the author's own book.

    python tools/no_real_figures.py

WHY A SCRIPT AND NOT A ONE-OFF TIDY-UP. The repo is published, so an amount copied out of the
real record is a leak rather than a typo, and "we scrubbed it once" is a memory, not a
property. This is the property. It runs in CI on every push and again on a release tag,
because publishing is the moment a leftover figure stops being harmless.

WHAT IT LOOKS FOR, and this is the part worth getting right: NOT every money figure. 155 of
them are money-shaped and almost all are legitimate -- the synthetic fixture's amounts, test
arithmetic, number-formatting inputs, and worked examples with invented round numbers. A rule
that flagged all of them would be turned off within a week for crying wolf, which is worse
than no rule.

The leak is a figure PLUS a claim that it came from the real book. So this looks for the
claim: a money figure within two lines of a phrase like "on the live book", "this book's",
"on the real record". That is exactly the shape every one of the sixteen real figures had,
because a comment only bothers to cite the live book when it is reporting from it.

WHAT IT DELIBERATELY DOES NOT FLAG:
  * bet names and trade ids. The author, 2026-09-08: "i think they are fine tbh. not very
    sensitive. trading history is actually fine." Amounts reconstruct a balance sheet; a bet
    called `semis-and-big-techs` does not.
  * figures in a worked example ("a $60 loss on a $1,000 position"), which is what the real
    ones were replaced BY and the reason the comments still teach anything.

Exit 0 and say so when clean, non-zero and name file:line when not.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# A money-shaped figure: C$/US$/$ with digits, a thousands group, or bare cents.
MONEY = re.compile(r"(?:C\$|US\$|\$)\s?\d[\d,]*(?:\.\d{2})?"
                   r"|\b\d{1,3},\d{3}(?:\.\d{2})?\b"
                   r"|\b\d+\.\d{2}\b")

# A claim that the surrounding sentence is reporting the real record. Add to this rather than
# weakening MONEY: the claim is the signal, and the figure alone never was.
CLAIMS_REAL = re.compile(
    r"\b(?:on|from|in)\s+(?:the\s+)?(?:live|real|author's|owner's)\s+"
    r"(?:book|record|data)\b"
    r"|\bthis\s+book(?:'s)?\b"
    r"|\bhis\s+book(?:'s)?\b"
    r"|\bthe\s+author's\s+(?:book|record|net|sleeve|balance)\b"
    r"|\bon\s+the\s+live\s+one\b"
    r"|\bin\s+real\s+life\b",
    re.IGNORECASE)

SKIP_SUFFIX = {".png", ".svg", ".lock", ".db"}
# This file names the patterns it hunts for, so it would flag itself.
SKIP_FILES = {"tools/no_real_figures.py"}

# ---------------------------------------------------------------------------- #
# The owner's identity, which is a different leak from a figure and needs a
# different rule: an exact match, not a heuristic.
#
# ADDED BECAUSE I REGRESSED IT. A sweep on 2026-09-28 replaced 236 occurrences of the
# owner's name with "the author" and 103 gendered pronouns with they/them. Two days later
# three new names were written into comments during unrelated work, and the only reason
# they were caught is that someone looked again by hand before publishing. That is the
# argument for every check in this file: a cleanup is a memory, a check is a property.
# ---------------------------------------------------------------------------- #
IDENTITY = {
    "the owner's name": re.compile(r"\bZeying\b|\bIng\b(?!Tian)|\bING\b"),
    # A local path gives away a username and the layout of someone's disk.
    "a personal path": re.compile(r"/Users/[a-z]|/home/[a-z]|\bdevpro\b"),
    # "the author" is anonymous, so a pronoun for them asserts something unknown. They/them
    # is the house style; see the sweep's commit message.
    "a gendered pronoun": re.compile(r"\b(?:he|his|him|He|His|Him)\b"),
    # Naming the broker identifies where the account is, and no argument here needs it.
    "the broker's name": re.compile(r"[Ww]ealthsimple"),
}
# LICENSE names the copyright holder on purpose: an MIT grant without one is unenforceable.
IDENTITY_ALLOW = {"LICENSE"}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("not a git checkout, so there is nothing to scan")
    return out.stdout.split()


def main() -> int:
    offenders: list[str] = []
    scanned = 0
    for rel in tracked_files():
        if rel in SKIP_FILES:
            continue
        p = ROOT / rel
        if not p.exists() or p.suffix in SKIP_SUFFIX:
            continue
        scanned += 1
        lines = p.read_text(errors="replace").split("\n")
        for i, line in enumerate(lines):
            figures = MONEY.findall(line)
            if not figures:
                continue
            # The sentence, not the line: these comments wrap at 100 columns, so the figure
            # and the claim are routinely on different lines.
            window = " ".join(lines[max(0, i - 2):i + 3])
            if CLAIMS_REAL.search(window):
                offenders.append(
                    f"  {rel}:{i + 1}  {', '.join(figures)}\n"
                    f"      {line.strip()[:100]}")

    # ---- the owner's identity, checked exactly rather than heuristically ----
    leaks: list[str] = []
    for rel in tracked_files():
        if rel in SKIP_FILES or rel in IDENTITY_ALLOW:
            continue
        p = ROOT / rel
        if not p.exists() or p.suffix in SKIP_SUFFIX:
            continue
        lines = p.read_text(errors="replace").split("\n")
        for i, line in enumerate(lines):
            for label, pat in IDENTITY.items():
                hits = pat.findall(line)
                if hits:
                    leaks.append(f"  {rel}:{i + 1}  {label}: {sorted(set(hits))}\n"
                                 f"      {line.strip()[:100]}")

    if offenders:
        print(f"{len(offenders)} figure(s) are presented as coming from the real book:\n")
        print("\n".join(offenders))
        print("\nReplace the amount with an invented round one. Keep the argument -- the "
              "\nreasoning is the point of the comment; the digits are not.")
    if leaks:
        print(f"\n{len(leaks)} line(s) name the owner, a personal path, a gendered pronoun "
              f"or the broker:\n")
        print("\n".join(leaks))
        print("\nUse \"the author\" and they/them; say \"the broker\" rather than naming one. "
              "\nThe reasoning stays -- only the attribution changes.")
    if offenders or leaks:
        return 1
    print(f"no real figures and no identifying strings in {scanned} tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
