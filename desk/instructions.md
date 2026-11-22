# trading-desk

The desk records the user's trades under versioned strategies, checks each trade against its strategy version's rules when it is entered, and reports how the trades ended. It is bookkeeping: you pass the user's decisions and the market facts its rules ask for, and the desk does every rule's arithmetic and returns every figure.

## Always
- Pass the user's reasons exactly as they wrote them, in their language. Never retype, shorten or tidy them, and never write one for them: if they gave none, ask.
- Never state a figure (a price, an R multiple, P&L, a rate, a count, a rule's result) the desk did not return; quote its reply.
- Give times in ISO-8601 with an offset, e.g. 2026-03-02T14:30:00Z. In a replay run every `at` is the close of the bar acted on, converted from the chart's timezone to UTC. In a live run, give `at` whenever the user says when they acted, and leave it out only for a decision made now. A `report` always needs its `at`. Times in replies are UTC.
- Define each finance term the first time you use it. Use the user's pronouns, and they/them until you know them.
- A refusal says what to change. If the change is to the form of the call (a type, a key, a format), make it and call again. If it would change a time, a price, a size, a reason or a report, or calls for a void, ask the user first.

## Entering a trade
- Supply `market` at entry. In a replay run: every indicator key the rules name and every declared fact, read off the chart at the bar acted on (a regular-hours chart for stocks, extended hours for futures). In a live run: every declared fact (the desk computes none), the indicator keys for futures, whose Yahoo bars are delayed 10 to 30 minutes, and any value you trust over the desk's.
- Answer every checklist item with `verdict` "pass" or "fail" and a one-line `reason`, as you judge it.
- Screenshots are full file paths or https:// links. For an image pasted into the chat, pass the file path you are shown for it, even a temporary one: the desk copies the file when the trade is recorded. If no path is shown, ask the user for one.

## After entry
- In a replay run the desk reads nothing but your reports: report each limit's fill or cancellation, and each exit with its kind, and its price for a hand exit or a fill away from the level.
- Report each expired day order as `cancelled`, why "not filled", dated at the session close it expired at.
- In a live run, report a stop-out at its real time (`exited`, kind "stop"), with the broker's fill price when the user has it: the bars date it at its bar's close, and a trade entered inside that bar does not see it.
- If the bars ended a live position the broker still holds, report it `open`, dated at or after the judged exit, at a moment it was still held.
- Before entering a trade on a live run, settle that run's futures trades for the rules that read the run's other trades, since Yahoo's futures feed lags: report a position the user confirms the broker still holds as `open` at the current time, one that was stopped out as `exited` at its real time with its price, and a limit that filled as `filled`.
- Read each open live trade with `get_trade` at least weekly: Yahoo keeps 1-minute bars for about 30 days.
- When you report a futures trade `open` before a new entry on its run, give the report and the entry the same `at`: the trade is known open only to the end of that minute.
- Stop a run, or void a whole trade, only on the user's word: neither can be undone.
- Fix a mistake with `correct_trade` or `void_trade`, never with a new trade on top.
