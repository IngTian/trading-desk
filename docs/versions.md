# Versions

| Version | Where it shipped, and what it holds |
|---|---|
| 0.4.0 | On PyPI: the old portfolio book, a different tool. |
| 0.5 | On PyPI: the record. |
| 0.6 to 0.9 | Development versions, installed from the repository and never uploaded to PyPI; they can no longer be installed. 0.6: reported states, bars and the judge. 0.7: rule checks at entry, and stats per run, version and strategy. 0.8: the MCP server and the Claude Code plugin. 0.9: live runs judged on 1-minute bars, `instrument` required, wrong-side stops and targets refused, and the agent told more. |
| 0.10 | On PyPI: the README, `checks_differ` null where it was not worked out, `trade_bars` serving 1m, and Python's openers guarded. |
| 0.11 | On PyPI. Removed: `close_trade`, `trade_bars`, the `judged` and `differs` reply fields, Yahoo and bars.db, and the indicators the desk computed. A hand exit is `report(state="exited", price=…)`, with its price required. Changed: reports are named `filled`, `cancelled`, `open` and `exited` everywhere — the states `report` takes, the stored event kinds, the fields of the folded record, and what `correct_trade` and `void_trade` take; a trade's outcome is read from its record alone, on a live run as on a replay one, so the only difference a caller sees is that `at` defaults to now on a live run; every rule about what a trade's record may hold lives in one validator, run after every write and by the integrity checker. The schema is version 5 and a 0.10 desk.db is not read: move it aside and start a new one. |
| 1.0 | When the interface settles. |
