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

__all__ = ["Refused", "StrategyView", "save_strategy", "get_strategy", "list_strategies",
           "RunView", "start_run", "stop_run", "list_runs"]

_SLUG = re.compile(r"[a-z0-9-]+")
_BARS = re.compile(r"(\d+) bars after exit")
_AFTER = " after exit"


def _aware(when: datetime | None, name: str = "now") -> datetime:
    """`when` (default: the wall clock) as an aware datetime; a naive one is refused, since
    its zone would be guessed."""
    if when is None:
        return datetime.now(UTC)
    if when.tzinfo is None:
        raise Refused(f"`{name}` has no timezone; give an aware time, e.g. "
                      "datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc).")
    return when


def _at(now: datetime | None) -> str:
    return _aware(now).astimezone(UTC).isoformat()


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


@dataclass(frozen=True)
class RunView:
    id: str
    strategy: str
    version: int
    mode: str
    tags: tuple[str, ...]
    started: datetime
    stopped: datetime | None


def _run_view(con, row: sqlite3.Row) -> RunView:
    when = {e["kind"]: datetime.fromisoformat(e["at"])
            for e in store.events(con, run_id=row["id"]) if e["kind"].startswith("run_")}
    return RunView(id=row["id"], strategy=row["strategy_id"], version=row["version"],
                   mode=row["mode"], tags=tuple(json.loads(row["tags"])),
                   started=when["run_started"], stopped=when.get("run_stopped"))


def _decision_time(mode: str, at: datetime | None, now: datetime | None, what: str) -> datetime:
    """A live decision defaults to `now`; a replay one never defaults, or it would be dated
    after every bar in the history being replayed."""
    _aware(now)
    if at is not None:
        return _aware(at, "at")
    if mode == "replay":
        raise Refused(f"A replay run needs `at` to {what}: give the moment in the replayed "
                      "history, not the wall clock.")
    return _aware(now)


def start_run(con: sqlite3.Connection, strategy: str, *, mode: str, tags=(),
              version: int | None = None, at: datetime | None = None,
              now: datetime | None = None) -> RunView:
    """Deploy a strategy version as run `<strategy>-r<N>`. Tags are kept verbatim (deduplicated,
    order kept) and never interpreted."""
    if mode not in ("live", "replay"):
        raise Refused(f"mode {mode!r} is not known; use 'live' (real time) or 'replay' "
                      "(re-running past history).")
    start = _decision_time(mode, at, now, "start")
    with store.transaction(con):  # the run number and version are read under the write lock
        rows = store.versions(con, strategy)
        if not rows:
            raise Refused(f"There is no strategy {strategy!r}; save it first, or list_strategies "
                          "to see the ones that exist.")
        v = rows[-1]["version"] if version is None else version
        if v not in {r["version"] for r in rows}:
            raise Refused(f"Strategy {strategy!r} has no version {v}; it has versions 1 to "
                          f"{len(rows)}.")
        id = f"{strategy}-r{len(store.runs(con, strategy)) + 1}"
        store.add_run(con, id, strategy, v, mode, list(dict.fromkeys(tags)))
        store.append(con, kind="run_started", run_id=id, at=_at(start), payload={},
                     recorded_at=_at(now))
    return _run_view(con, store.run(con, id))


def stop_run(con: sqlite3.Connection, run: str, *, at: datetime | None = None,
             now: datetime | None = None) -> RunView:
    row = store.run(con, run)
    if row is None:
        raise Refused(f"There is no run {run!r}; list_runs to see the ones that exist.")
    stop = _decision_time(row["mode"], at, now, "stop")
    with store.transaction(con):  # the run's state is checked under the write lock
        view = _run_view(con, row)
        if view.stopped is not None:
            raise Refused(f"Run {run!r} is already stopped; start a new run instead.")
        if stop < view.started:
            raise Refused(f"Run {run!r} started at {view.started.isoformat()}; its stop cannot "
                          "be before that, so give a later `at`.")
        store.append(con, kind="run_stopped", run_id=run, at=_at(stop), payload={},
                     recorded_at=_at(now))
    return _run_view(con, row)


def list_runs(con: sqlite3.Connection, strategy: str | None = None) -> list[RunView]:
    return [_run_view(con, r) for r in store.runs(con, strategy)]
