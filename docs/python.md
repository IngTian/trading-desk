# Use it from Python

```python
from datetime import UTC, datetime

from desk import api, config, store

store.create()                  # once: makes desk.db under the data root
con = store.open()

text = config.example_strategy().read_text()      # or your own strategy's Markdown
api.save_strategy(con, "pullback", text)

def utc(day, hour, minute=0):   # a moment in the replayed history
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)

run = api.start_run(con, "pullback", mode="replay", currency="USD", tags=["paper"], at=utc(2, 14))
t = api.trade(con, run.id, ticker="ACME", side="long", entry_type="limit",
              entry=100.0, stop=98.5, target=103.0, size=10,
              instrument="stock",
              reasons="Held the rising trend line on the hourly; a limit at the dip's low.",
              screenshots=["https://www.tradingview.com/x/AbCd1234/"], at=utc(2, 14))
api.report(con, t.id, state="filled", at=utc(2, 15, 15),
           reason="The 15:00 bar traded down to it.")
t = api.report(con, t.id, state="exited", kind="target", at=utc(3, 16, 30),
               reason="The target filled.")
t.outcome   # status "exited", fill 100.0, exit 103.0 at its target, r 2.0, cash 30.0
```

The functions are [the tools](tools.md), one for one, with the same arguments after the connection.
Only Python has: `at` as an aware datetime or an ISO 8601 string with its offset; `now`, which
tests inject, a keyword of every function but `get_strategy`, `list_strategies` and `list_runs`;
and the frozen dataclasses returned, `StrategyView`, `RunView`, `api.StoppedRun` (from `stop_run`),
`TradeView`, `CheckView` (from `trade` with `check_only=True`), `desk.outcome.Outcome` and
`desk.stats.Stats`, whose id lists are plain tuples, without MCP's counts. The API may change
before 1.0. A refusal raises `api.Refused`, whose message says what was wrong and how to fix it,
and a refused call writes nothing.

Tracking a trade (its largest move for you or against you while it was open, the drift after the
exit, anything per strategy) is your own code: it reads `get_trade` each time it runs, so a
correction or a void shows up on its next run.
