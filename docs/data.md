# Where your data lives

Everything lives under one folder, the data root: `~/Documents/trading-desk/` by default, or
`$TRADING_DESK_HOME`.

```
desk.db        strategies, runs, trades and every decision, reports included (append-only)
bars.db        price bars for live runs, and the range each fetch covered (insert-only)
attachments/   screenshots, named by the hash of their bytes
```

Append-only and insert-only mean rows are added, never changed or deleted. `$TRADING_DESK_DB` beats
`$TRADING_DESK_HOME`, for the database only: it names a file in place of desk.db, and the rest stays
under the root, so to keep a session apart from your real data, set `$TRADING_DESK_HOME`. No
config.toml is read, and every opener of desk.db refuses one that sets `db` (unless
`$TRADING_DESK_DB`, or from Python a path, names the database).

Both databases use a rollback journal, never WAL (a write-ahead log, a second file beside the
database), because `~/Documents` is often synced by iCloud, and a database synced apart from its
log can be corrupted. Write from one machine at a time. Where iCloud has evicted desk.db or
bars.db, the desk never creates an empty one over either, and names the folder to open in Finder.
Without desk.db every call is refused; without bars.db a live trade reads `not judged`, an entry is
still recorded with its indicator results unknown, and `trade_bars` fails. That guard catches the `.icloud` stub that macOS 13 and earlier leave for an evicted file;
on macOS 14 and later an evicted desk.db keeps its name, and is downloaded when it is opened.

`trading-desk integrity` checks the log for shapes it should never hold: a trade with no entry, a
decision dated before its trade, a move at or after the close, a report the record does not allow,
and more, after SQLite's `quick_check` of the file (for the fuller check, run
`sqlite3 -readonly desk.db 'PRAGMA integrity_check'`). It prints each violation, and exits 1 if it
found any, or when there is no desk.db, the file is not a desk.db this version reads, config.toml
sets `db`, or desk.db is in iCloud but not on this Mac; otherwise 0.
