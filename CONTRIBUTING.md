# Contributing

Thanks for looking. One thing to know before you open a PR: **this tool records and it
does not judge.**

It keeps a book — fills, flows, bets — and shows you what is in it. It has no risk limits,
no stop grading, no position sizing, no compliance checks and no opinion about your
trading, and none of those are missing by accident. Four separate rounds of deletion
removed exactly that machinery, because a rule this tool cannot enforce becomes a rule it
merely *stores*, and a stored rule drifts from the one you actually follow while still
looking authoritative on screen. A PR that adds enforcement is a scope decision, not a
detail — please open an issue and make the case. A PR that makes the recording better needs
no preamble.

## Ground rules

- `pytest -q` and `ruff check .` both clean. CI runs them on 3.11 and 3.13, plus a wheel
  build, a shape check on the wheel, and a smoke test that installs it and creates a book.
- **New behaviour comes with a test that fails without it.** Not a formality: an audit of
  this repo found six tests that could not fail at all, including two that wrote a formula
  out as a literal and asserted SQLite could evaluate it. If you cannot make your test go
  red by reverting your change, it is not testing your change.
- **`NULL` means NOT RECORDED — never zero, never parity, never a sensible default.** A
  total that cannot be computed is withheld, not computed partially. `or 0.0` in a sum is
  this project's signature defect; it once advertised a five-figure gain on a book that had
  made two hundred dollars.
- **Arithmetic lives in `desk/book.py`.** Panes render and handle keys. A pane that
  recomputes a figure `book.py` already withheld is how the same word ends up with two
  answers on one screen — that has happened four times.
- **Rows are native, totals are home.** A position row shows its own currency; only totals
  translate, and they say which currency they are in.
- Keys are declared once, in `desk/tui/keymap.py`, and prompt hints once in the same file.
  Tests enforce both, including that no user-facing string names a key that is bound to
  nothing. Never hand-write a hint.
- **Fixtures and examples use obviously-synthetic data.** This is a tool for someone's own
  trading record; nobody's real numbers belong in the repo, and
  `python tools/no_real_figures.py` is run by CI to keep it that way.
- A check whose subject spans currencies cannot be written in SQL. Translation can fail, and
  a view has no way to say "unknown" — it either invents a rate or drops the row, and both
  have shipped here. Put it in Python over `desk/book.py`.

`CLAUDE.md` holds the longer version: the invariants, and the measured reasons behind the
ones that look arbitrary. Worth a skim before a non-trivial change — several exist because
the obvious simplification was tried and broke something.

## Setup

```bash
uv sync                 # installs the dev group: pytest, pytest-asyncio, ruff
uv run pytest -q
uv run ruff check .
uv run desk             # the TUI, against whatever book config resolves
```

`uv sync` rather than `pip install -e '.[dev]'`, because the dev tools are declared in
`[dependency-groups]` — `uv sync` installs groups by default and extras only with `--extra`,
so declaring them as an extra produced a venv with no pytest in it. If you would rather not
install uv, `python -m venv .venv && .venv/bin/pip install -e .` gets you the runtime
dependencies and you can add `pytest pytest-asyncio ruff` by hand.

## Running the parts

```bash
uv run desk-migrate --new      # create a book (or just run `desk` and answer y)
uv run desk-migrate --check    # does an existing book match desk/schema.sql?
uv run desk-migrate --upgrade  # rebuild it at the current schema version, carrying rows
uv run desk-check              # the invariants, then the attribution panel
uv run desk-check --quiet      # invariants only; exits non-zero if one fails
uv run python -m desk.prices --dry   # fetch marks and print, writing nothing
```

`desk/schema.sql` is the source of truth and it is **code**. Change it there, bump
`SCHEMA_VERSION` in `desk/migrate.py`, and `--upgrade` carries the rows across; a test
asserts the two cannot drift.
