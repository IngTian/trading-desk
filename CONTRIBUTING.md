# Contributing

Thanks for looking. Two principles decide most questions about this tool, and both are in the
README: **only decisions and bars are stored** (decisions include reports; everything else is
derived on read), and **one timing rule**. A change that stores something derived (an exit, a P&L,
a check result beyond the ones shown at entry, which the README names as the one exception) or that
dates a decision any other way is a design change. Please open an issue and make the case before
writing it.

## Ground rules

- `uv run pytest -q`, `uv run ruff check .` and `uv run python tools/no_real_figures.py` all
  pass. CI runs ruff and pytest on Python 3.11 and 3.14. A packaging job builds a wheel, checks its
  shape (`tools/check_wheel.py`) and installs it in a clean environment that records trades, reads
  their outcomes and serves the 16 tools over MCP, with no network (`tools/smoke_wheel.sh`). A
  third job runs the figure guard.
- **New behaviour comes with a test that fails without it.** If reverting your change does
  not turn your test red, the test is not testing your change.
- **A refusal says what was wrong and how to fix it.** Every refusal from the API and the
  strategy parser raises `desk.errors.Refused` (re-exported as `api.Refused`), and a refused
  call writes nothing. Known gaps: a screenshot file that cannot be read (`api.py`'s
  `path.read_bytes()`), and, from Python only, a field name in `correct_trade` that is not text,
  still raise Python's own error (still writing nothing). Over MCP the first comes back as "The
  desk's data could not be used", and the second cannot occur, since a JSON key is always text.
- **Messages are written in JSON terms.** A refusal or a tool description shows a value as a JSON
  client wrote it (`true`, `null`, `"text"`, through `desk.errors.shown`), never in Python's forms;
  `tests/test_messages.py` guards it.
- **Nothing is silently dropped or guessed.** An unknown rule, a value in the wrong unit or a
  time without a timezone is refused rather than read as something else. A figure that
  cannot be computed is withheld, never shown as zero.
- **The log is append-only.** Triggers keep events and versions insert-only, and the bars and
  spans in bars.db too. A correction is a new event read in place of the old one; nothing is
  updated or deleted.
- **Tests never reach the network.** `tests/conftest.py` replaces `yahoo.fetch`, the desk's
  one network call, in every test; a test that wants Yahoo bars installs the `fake_yahoo`
  fixture.
- **Reads that decide a write go inside `store.transaction(con)`**, which takes the write lock
  first, so two sessions cannot both pass a check and then both write.
- **Fixtures and examples use obviously invented data.** This is a tool for someone's own
  trading record, and nobody's real numbers belong in the repo. `tools/no_real_figures.py`
  is run by CI to keep it that way.

## Layout

| File | Holds |
|---|---|
| `desk/schema.sql` | desk.db's SQLite schema, version 4. There is no migration: `store.open` refuses a desk.db whose version differs from `store.SCHEMA_VERSION`, so a schema change is a design change. |
| `desk/store.py` | The only module that writes desk.db. |
| `desk/bars.py`, `desk/bars.sql` | bars.db and its schema: the only module that writes bars.db. |
| `desk/yahoo.py` | Yahoo's bars for live runs, through yfinance, which it imports only inside the call; `fetch` is the one network call. |
| `desk/judge.py` | Pure: the bar walk, with a table-driven test, and the replay and live readers. |
| `desk/config.py` | Where the data lives, and the order `TRADING_DESK_DB` and `TRADING_DESK_HOME` are read in; and the guards the openers run: `refuse_legacy_db`, for a config.toml that still sets `db` (`store.create` and `store.open`, given no path), and the iCloud placeholder checks, for a file evicted from the Mac (those two and `bars.open`). |
| `desk/strategy.py`, `desk/rules.py` | Reading a strategy's Markdown and its rules block. |
| `desk/checks.py` | Each rule's value reader and its check. |
| `desk/indicators.py` | Pure: the indicator formulas (TradingView Pine v6's), and how many bars each needs. |
| `desk/model.py` | Folding a trade's decisions into one current view, and `report_problem`, the one place a report's rules live. |
| `desk/stats.py` | Pure: the stats over one scope's trades, which `api.stats` builds the rows for. |
| `desk/errors.py` | `Refused`, which every layer raises, and `shown`, which writes a value as JSON for a message. |
| `desk/api.py` | The public API: one function per MCP tool. |
| `desk/server.py` | The MCP server and its 16 tools, each one `api` function (`trading-desk mcp`). |
| `desk/instructions.md` | The agent's standing instructions, which the server sends at the start of every session. |
| `desk/integrity.py` | The checks `trading-desk integrity` runs. |
| `desk/cli.py` | The two commands, `trading-desk mcp` and `trading-desk integrity`. |
| `plugin/`, `.claude-plugin/` | The Claude Code plugin, which runs `trading-desk mcp`, and the repo as its marketplace. |
| `tools/` | The wheel's shape check, the smoke test of an installed wheel, and the figure guard. |
| `.github/workflows/` | CI, and the release, which publishes to PyPI on a version tag. |

## Setup

```bash
uv sync                  # installs the dev group from pyproject.toml
uv run pytest -q
```

Run anything that writes against a scratch root, never your real one:
`env -u TRADING_DESK_DB TRADING_DESK_HOME=$(mktemp -d) …`. Unset `TRADING_DESK_DB`: it
beats the root for the database, while screenshots still follow the root. The test suite does this for every test by itself (`tests/conftest.py`).
