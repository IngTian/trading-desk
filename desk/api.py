"""The desk's one outward face: what the MCP server (plan 5) wraps, function for function.

Every function takes an open connection and `now` (default: the wall clock, in UTC) so tests
control time. Every refusal is a `Refused` whose message says what to change.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from desk import store, strategy
from desk.checks import _hours
from desk.errors import Refused

__all__ = ["Refused", "StrategyView", "save_strategy", "get_strategy", "list_strategies"]

_SLUG = re.compile(r"[a-z0-9-]+")
_BARS = re.compile(r"(\d+) bars after exit")
_AFTER = " after exit"


def _at(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat()


@dataclass(frozen=True)
class StrategyView:
    id: str
    name: str
    version: int
    versions: int
    text: str
    rules: dict[str, str]
    timeframes: tuple[str, ...]
    tracker_script: str | None
    tracker_limit: str | None
    current_hash: str   # of the latest version and tracker: pass it back as `if_current`
    saved: str          # "version 3 created" | "tracker updated" | "unchanged"


def _current_hash(latest: sqlite3.Row, tracker: sqlite3.Row | None) -> str:
    parts = [latest["text_hash"], (tracker and tracker["script_hash"]) or "",
             (tracker and tracker["tracker_limit"]) or ""]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _view(con, id: str, version: int | None, saved: str) -> StrategyView:
    rows = store.versions(con, id)
    if not rows:
        raise Refused(f"There is no strategy {id!r}; save it first, or list_strategies to see "
                      "the ones that exist.")
    row = rows[-1] if version is None else next((r for r in rows if r["version"] == version), None)
    if row is None:
        raise Refused(f"Strategy {id!r} has no version {version}; it has versions 1 to "
                      f"{len(rows)}.")
    t = store.latest_tracker(con, id)
    text = row["text"]
    return StrategyView(
        id=id, name=strategy.parse(text).name, version=row["version"], versions=len(rows),
        text=text, rules=json.loads(row["rules"]), timeframes=tuple(json.loads(row["timeframes"])),
        tracker_script=t and t["script"], tracker_limit=t and t["tracker_limit"],
        current_hash=_current_hash(rows[-1], t), saved=saved)


def _check_limit(limit: str) -> None:
    n = _BARS.fullmatch(limit)
    hours = _hours(limit.removesuffix(_AFTER)) if limit.endswith(_AFTER) else None
    if (n and int(n[1]) > 0) or (hours and hours > 0):
        return
    raise Refused(f"tracker_limit {limit!r} is not readable; write how long the tracker may keep "
                  "watching after the trade closes, e.g. '40 bars after exit' or '90m after exit'.")


def save_strategy(con: sqlite3.Connection, id: str, markdown: str, *,
                  tracker_script: str | None = None, tracker_limit: str | None = None,
                  if_current: str | None = None, now: datetime | None = None) -> StrategyView:
    """Save a strategy (new id: version 1; existing id: needs `if_current`, the `current_hash`
    last read). Text that differs after whitespace is normalised makes a new version; a changed
    tracker script or limit makes a new tracker row and no new version. A tracker argument left
    out keeps what is saved."""
    if not _SLUG.fullmatch(id):
        raise Refused(f"Strategy id {id!r} must be lowercase letters, digits and hyphens, "
                      "like 'dip-buyer'.")
    parsed = strategy.parse(markdown)
    if tracker_limit is not None:
        _check_limit(tracker_limit)
    at = _at(now)
    with store.transaction(con):  # read, compare and write under one write lock
        rows = store.versions(con, id)
        old = store.latest_tracker(con, id) if rows else None
        if rows:
            if if_current is None:
                raise Refused(f"Strategy {id!r} already exists; pass if_current (the current_hash "
                              "from get_strategy) so a save cannot overwrite someone else's "
                              "change.")
            if if_current != _current_hash(rows[-1], old):
                raise Refused(f"Strategy {id!r} was changed since you read it; get it again, "
                              "redo your edit on that, and save.")
        elif if_current is not None:
            raise Refused(f"Strategy {id!r} is new, so if_current must be left out.")

        script = tracker_script if tracker_script is not None else (old and old["script"])
        limit = tracker_limit if tracker_limit is not None else (old and old["tracker_limit"])
        if limit and not script:
            raise Refused("tracker_limit needs a tracker_script: give the script, or drop the "
                          "limit.")
        script_hash = hashlib.sha256(script.encode()).hexdigest() if script else None
        new_text = not rows or rows[-1]["text_hash"] != parsed.text_hash
        new_tracker = (script_hash, limit or None) != ((old and old["script_hash"]) or None,
                                                       (old and old["tracker_limit"]) or None)
        try:
            if not rows:
                store.add_strategy(con, id, at)
            if new_text:
                store.add_version(con, id, len(rows) + 1, parsed.text, parsed.text_hash,
                                  dict(parsed.rules), list(parsed.timeframes), at)
            if new_tracker:
                store.add_tracker(con, id, script, script_hash, limit or None, at)
        except sqlite3.IntegrityError as e:
            raise Refused(f"Strategy {id!r} was changed by another session; get it again, "
                          "redo your edit on that, and save.") from e
    saved = (f"version {len(rows) + 1} created" if new_text
             else "tracker updated" if new_tracker else "unchanged")
    return _view(con, id, None, saved)


def get_strategy(con: sqlite3.Connection, id: str, version: int | None = None) -> StrategyView:
    return _view(con, id, version, "unchanged")


def list_strategies(con: sqlite3.Connection) -> list[StrategyView]:
    return [_view(con, r["id"], None, "unchanged")
            for r in con.execute("SELECT id FROM strategies ORDER BY id")]
