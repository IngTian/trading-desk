"""Assert a built wheel has the shape v1 needs. Run by CI and by the release, before publishing.

    python tools/check_wheel.py <dir holding the .whl>

An editable install reads the source tree, so it cannot catch a missing package-data
declaration: only a built wheel can. This is the check that would have caught `pip install`
shipping a tool that could not create its store.
"""
from __future__ import annotations

import configparser
import glob
import sys
import zipfile

# THE SCHEMA IS THE TOOL: without it a fresh install cannot create desk.db. It once lived in
# book/, which was not a package, so an installed copy resolved to a path that did not exist.
# THE EXAMPLE RULES are what a new user copies first; `check` points at them when rules.md is
# missing, so a wheel without them sends the user to a file that is not there.
REQUIRED = ("desk/schema.sql", "desk/rules.example.md")
SCRIPTS = {"trading-desk": "desk.cli:main"}


def main(folder: str) -> int:
    wheels = glob.glob(f"{folder}/*.whl")
    if len(wheels) != 1:
        return _fail(f"expected one wheel in {folder}, found {wheels}")
    wheel = wheels[0]
    archive = zipfile.ZipFile(wheel)
    names = archive.namelist()

    missing = [want for want in REQUIRED if want not in names]
    if missing:
        return _fail(f"{', '.join(missing)} missing from {wheel}")

    # A stale build/ can smuggle a package that no longer exists in the source into the
    # wheel. CI checks out clean and would not reproduce it, so assert the shape.
    tops = {n.split("/")[0] for n in names if "/" in n}
    unexpected = {t for t in tops - {"desk"} if not t.endswith(".dist-info")}
    if unexpected:
        return _fail(f"unexpected top-level packages in {wheel}: {sorted(unexpected)}")

    # The v0 commands (desk, desk-migrate, desk-check, desk-mcp) are gone; a wheel that still
    # declared one would install a command that imports a deleted module.
    (entry_points,) = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
    parsed = configparser.ConfigParser()
    parsed.read_string(archive.read(entry_points).decode())
    scripts = dict(parsed["console_scripts"]) if parsed.has_section("console_scripts") else {}
    if scripts != SCRIPTS:
        return _fail(f"console scripts are {scripts}, expected {SCRIPTS}")

    print(f"{wheel}: {', '.join(REQUIRED)} present, only desk/ ships, one command")
    return 0


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dist"))
