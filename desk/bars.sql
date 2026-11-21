-- bars.db: price bars from every source, insert-only. The first write of a bar wins.
-- A source is an adapter's name: 'yahoo' (later adapters add their own).
CREATE TABLE bars (
    source    TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,   -- the source's own symbol (GC=F, XYZ.TO)
    timeframe INTEGER NOT NULL CHECK (timeframe > 0),   -- minutes
    open_time TEXT    NOT NULL,   -- UTC, ISO-8601; the bar closes at open_time + timeframe
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    REAL,
    PRIMARY KEY (source, symbol, timeframe, open_time),
    CHECK (low <= min(open, close) AND high >= max(open, close))
) STRICT;

-- What a source covers: one row per range fetched. Inside a span, a missing bar is a closed
-- market; outside every span, the history is missing.
CREATE TABLE spans (
    source      TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    timeframe   INTEGER NOT NULL,
    start_time  TEXT    NOT NULL,
    end_time    TEXT    NOT NULL,
    recorded_at TEXT    NOT NULL,
    CHECK (start_time < end_time)
) STRICT;

CREATE TRIGGER bars_are_insert_only_upd BEFORE UPDATE ON bars
BEGIN SELECT RAISE(ABORT,'bars are insert-only: the first write of a bar wins'); END;
CREATE TRIGGER bars_are_insert_only_del BEFORE DELETE ON bars
BEGIN SELECT RAISE(ABORT,'bars are insert-only: the first write of a bar wins'); END;

CREATE TRIGGER spans_are_insert_only_upd BEFORE UPDATE ON spans
BEGIN SELECT RAISE(ABORT,'spans are insert-only: add a new span instead'); END;
CREATE TRIGGER spans_are_insert_only_del BEFORE DELETE ON spans
BEGIN SELECT RAISE(ABORT,'spans are insert-only: add a new span instead'); END;
