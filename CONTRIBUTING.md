# Contributing

Thanks for looking. Two principles decide most questions about this tool, and both are in the
README: **only decisions are stored**, and **one timing rule**. A change that stores something
derived (an exit, a P&L, a check result) or that dates a decision any other way is a design
change. Please open an issue and make the case before writing it.

## Ground rules

- `uv run pytest -q`, `uv run ruff check .` and `uv run python tools/no_real_figures.py` all
  pass. CI runs ruff and pytest on Python 3.11 and 3.14. A packaging job builds a wheel,
  checks its shape (`tools/check_wheel.py`) and installs it in a clean environment that
  records a trade (`tools/smoke_wheel.sh`). A third job runs the figure guard.
- **New behaviour comes with a test that fails without it.** If reverting your change does
  not turn your test red, the test is not testing your change.
- **A refusal says what was wrong and how to fix it.** Every refusal from the API and the
  strategy parser raises `desk.errors.Refused` (re-exported as `api.Refused`), and a refused
  call writes nothing. Known gaps, for the MCP release: an id of the wrong JSON type, an
  unreadable screenshot file and a non-text field name in `correct_trade` still raise Python's
  own error (still writing nothing).
- **Nothing is silently dropped or guessed.** An unknown rule, a value in the wrong unit or a
  time without a timezone is refused rather than read as something else. A figure that
  cannot be computed is withheld, never shown as zero.
- **The log is append-only.** Triggers keep events and versions insert-only.
  A correction is a new event read in place of the old one; nothing is updated or deleted.
- **Reads that decide a write go inside `store.transaction(con)`**, which takes the write lock
  first, so two sessions cannot both pass a check and then both write.
- **Fixtures and examples use obviously invented data.** This is a tool for someone's own
  trading record, and nobody's real numbers belong in the repo. `tools/no_real_figures.py`
  is run by CI to keep it that way.

## Layout

| File | Holds |
|---|---|
| `desk/schema.sql` | The SQLite schema. There is no migration yet: `store.open` refuses a desk.db whose version differs from `store.SCHEMA_VERSION`, so a schema change is a design change. |
| `desk/store.py` | The only module that writes to the database. |
| `desk/config.py` | Where the data lives, and the order `TRADING_DESK_DB`, `TRADING_DESK_HOME` and `config.toml` are read in. |
| `desk/strategy.py`, `desk/rules.py` | Reading a strategy's Markdown and its rules block. |
| `desk/checks.py` | Each rule's value reader and its check. |
| `desk/model.py` | Folding a trade's decisions into one current view. |
| `desk/api.py` | The public API: one function per future MCP tool. |
| `desk/integrity.py`, `desk/cli.py` | `trading-desk integrity`. |

## Setup

```bash
uv sync                  # installs the dev group from pyproject.toml
uv run pytest -q
```

Run anything that writes against a scratch root, never your real one:
`env -u TRADING_DESK_DB TRADING_DESK_HOME=$(mktemp -d) …`. Unset `TRADING_DESK_DB`: it
beats the root for the database, while screenshots still follow the root. The test suite does this for every test by itself (`tests/conftest.py`).
