-- The v2 store: strategies, versions, runs, trades and an append-only log of decisions.
-- The database holds only the owner's decisions; everything else is derived by folding events.

CREATE TABLE strategies (
    id         TEXT PRIMARY KEY CHECK (id NOT GLOB '*[^a-z0-9-]*' AND length(id) BETWEEN 1 AND 64),
    created_at TEXT NOT NULL
) STRICT;

-- A version's text never changes (trigger below).
CREATE TABLE versions (
    strategy_id TEXT    NOT NULL REFERENCES strategies(id),
    version     INTEGER NOT NULL CHECK (version >= 1),
    text        TEXT    NOT NULL,
    text_hash   TEXT    NOT NULL,                       -- sha256 of the normalised text
    rules       TEXT    NOT NULL CHECK (json_valid(rules)),       -- {name: value}, timeframes excluded
    timeframes  TEXT    NOT NULL CHECK (json_valid(timeframes)),  -- ["15m","1h"], finest first
    saved_at    TEXT    NOT NULL,
    PRIMARY KEY (strategy_id, version)
) STRICT;

-- Tracker script and limit: saved apart from the text, never versioned; the latest row is current.
CREATE TABLE trackers (
    id            INTEGER PRIMARY KEY,
    strategy_id   TEXT NOT NULL REFERENCES strategies(id),
    script        TEXT,               -- NULL: the strategy has no tracker
    script_hash   TEXT,
    tracker_limit TEXT,               -- "40 bars after exit" | "90m after exit"
    saved_at      TEXT NOT NULL
) STRICT;

CREATE TABLE runs (
    id          TEXT    PRIMARY KEY,                     -- "<strategy>-r<N>"
    strategy_id TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    mode        TEXT    NOT NULL CHECK (mode IN ('live','replay')),
    tags        TEXT    NOT NULL CHECK (json_valid(tags)),          -- ["paper","GC 15m"], verbatim
    FOREIGN KEY (strategy_id, version) REFERENCES versions(strategy_id, version)
) STRICT;

CREATE TABLE trades (
    id          TEXT PRIMARY KEY,                        -- "<ticker>-<YYYYMMDD>[-N]"
    run_id      TEXT NOT NULL REFERENCES runs(id),
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('long','short')),
    instrument  TEXT NOT NULL CHECK (instrument IN ('stock','etf','future')),
    point_value REAL NOT NULL CHECK (point_value > 0),
    currency    TEXT NOT NULL
) STRICT;

-- Every decision. `at` is when it takes effect (UTC); `recorded_at` is when it was written.
CREATE TABLE events (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('run_started','run_stopped','entered',
                    'order_moved','closed','corrected','voided')),
    run_id      TEXT NOT NULL REFERENCES runs(id),
    trade_id    TEXT REFERENCES trades(id),
    corrects    INTEGER REFERENCES events(id),
    payload     TEXT NOT NULL CHECK (json_valid(payload)),
    CHECK (corrects IS NULL OR corrects < id),
    CHECK ((kind IN ('run_started','run_stopped')) = (trade_id IS NULL))
) STRICT;

-- strategies, runs and trades are identity rows, written once. versions, trackers and events
-- are refused any change at all: a correction is a new row.
CREATE TRIGGER versions_are_immutable_upd BEFORE UPDATE ON versions
BEGIN SELECT RAISE(ABORT,'versions never change: save a new version instead'); END;
CREATE TRIGGER versions_are_immutable_del BEFORE DELETE ON versions
BEGIN SELECT RAISE(ABORT,'versions never change: save a new version instead'); END;

CREATE TRIGGER trackers_are_append_only_upd BEFORE UPDATE ON trackers
BEGIN SELECT RAISE(ABORT,'trackers are append-only: save a new tracker instead'); END;
CREATE TRIGGER trackers_are_append_only_del BEFORE DELETE ON trackers
BEGIN SELECT RAISE(ABORT,'trackers are append-only: save a new tracker instead'); END;

CREATE TRIGGER events_are_append_only_upd BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT,'events are append-only: append a corrected event instead'); END;
CREATE TRIGGER events_are_append_only_del BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT,'events are append-only: append a corrected event instead'); END;
