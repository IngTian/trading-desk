# Where your data lives

Everything lives under one folder, the data root: `~/Documents/trading-desk/` by default, or
`$TRADING_DESK_HOME`.

```
desk.db        strategies, runs, trades and every decision, reports included (append-only)
attachments/   screenshots, named by the hash of their bytes
```

Append-only means rows are added, never changed or deleted. `$TRADING_DESK_DB` beats
`$TRADING_DESK_HOME`, for the database only: it names a file in place of desk.db, and the rest stays
under the root, so to keep a session apart from your real data, set `$TRADING_DESK_HOME`. No
config.toml is read, and every opener of desk.db refuses one that sets `db` (unless
`$TRADING_DESK_DB`, or from Python a path, names the database).

desk.db uses a rollback journal, never WAL (a write-ahead log, a second file beside the database),
because `~/Documents` is often synced by iCloud, and a database synced apart from its log can be
corrupted. Write from one machine at a time. Where iCloud has evicted desk.db, the desk never
creates an empty one over it, and names the folder to open in Finder. Without desk.db every call is
refused. That guard catches the `.icloud` stub that macOS 13 and earlier leave for an evicted file;
on macOS 14 and later an evicted desk.db keeps its name, and is downloaded when it is opened.

`trading-desk integrity` checks the log for shapes it should never hold: a trade with no entry, a
decision dated before its trade, a move at or after the trade's end, a report the record does not
allow, a second fill or exit, a decision after a void, and more, after SQLite's `quick_check` of the
file (for the fuller check, run `sqlite3 -readonly desk.db 'PRAGMA integrity_check'`). It prints
each violation, and exits 1 if it found any, or when there is no desk.db, the file is not a desk.db
this version reads, config.toml sets `db`, or desk.db is in iCloud but not on this Mac; otherwise 0.
