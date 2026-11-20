-- The v1 event store schema: append-only log, derived state.
--
-- Every change to a trade becomes an event. Current state is derived by folding events.
-- No updates, no deletes. A correction is a new event naming the one it corrects.

CREATE TABLE events (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,              -- ISO-8601 with a UTC offset
    actor      TEXT NOT NULL CHECK (actor  IN ('owner','tracker','import')),
    source     TEXT NOT NULL CHECK (source IN ('chat','cli','export','yahoo')),
    kind       TEXT NOT NULL CHECK (kind   IN ('checked','imported','passed','entered',
                 'stop_moved','target_moved','exit_proposed','exited','note','closed',
                 'labelled','corrected','session_started','session_ended',
                 'run_started','run_ended')),
    trade_id   TEXT REFERENCES trades(id),
    session_id INTEGER REFERENCES sessions(id),
    corrects   INTEGER REFERENCES events(id),
    rules_hash TEXT,
    payload    TEXT NOT NULL,              -- JSON object
    CHECK (json_valid(payload)),
    CHECK (corrects IS NULL OR corrects < id)       -- a correction names an EARLIER event
) STRICT;

CREATE TRIGGER events_are_append_only_upd BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT,'events are append-only: append a corrected event instead'); END;

CREATE TRIGGER events_are_append_only_del BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT,'events are append-only: append a corrected event instead'); END;

CREATE TABLE trades (
    id           TEXT PRIMARY KEY,
    account      TEXT NOT NULL CHECK (account IN ('real','paper','replay')),
    account_name TEXT,
    run_id       TEXT,
    ticker       TEXT NOT NULL,
    instrument   TEXT NOT NULL CHECK (instrument IN ('stock','etf','future')),
    point_value  REAL NOT NULL CHECK (point_value > 0),
    currency     TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('long','short')),
    created_at   TEXT NOT NULL,
    -- A run belongs to one account kind, so a replay trade can only name a replay run.
    FOREIGN KEY (account, run_id) REFERENCES runs(kind, id)
) STRICT;

-- Keyed by kind AND id: paper run 3 and replay run 3 are two runs with two rules sections.
-- Keyed by id alone they were one row, and a replay trade's run named the Paper section.
CREATE TABLE runs (
    id            TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('paper','replay')),
    vault_folder  TEXT,
    started       TEXT NOT NULL,
    reset_on      TEXT,
    rules_section TEXT NOT NULL,
    PRIMARY KEY (kind, id)
) STRICT;

CREATE TABLE sessions (
    id         INTEGER PRIMARY KEY,
    started    TEXT NOT NULL,
    ended      TEXT,
    rules_hash TEXT NOT NULL,
    limits     TEXT NOT NULL CHECK (json_valid(limits))
) STRICT;

CREATE TABLE messages (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    session_id INTEGER REFERENCES sessions(id),
    text       TEXT NOT NULL
) STRICT;

CREATE TABLE event_messages (
    event_id   INTEGER NOT NULL REFERENCES events(id),
    message_id INTEGER NOT NULL REFERENCES messages(id),
    role       TEXT NOT NULL,
    PRIMARY KEY (event_id, message_id, role)
) STRICT;
