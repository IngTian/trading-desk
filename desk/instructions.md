# trading-desk

The desk records the user's trades under versioned strategies, checks each trade against its strategy version's rules when it is entered, and reports how the trades ended. It is bookkeeping: you pass the user's decisions and the market facts its rules ask for, and the desk does every rule's arithmetic and returns every figure.

## Always
- Pass the user's reasons exactly as they wrote them, in their language. Never retype, shorten or tidy them, and never write one for them: if they gave none, ask.
- Never state a figure (a price, an R multiple, P&L, a rate, a count, a rule's result) the desk did not return; quote its reply.
- Give times in ISO-8601 with an offset, e.g. 2026-03-02T14:30:00Z. In a replay run every `at` is the close of the bar acted on, converted from the chart's timezone to UTC. In a live run, give `at` whenever the user says when they acted, and leave it out only for a decision made now. A `report` always needs its `at`. Times in replies are UTC.
- Define each finance term the first time you use it. Use the user's pronouns, and they/them until you know them.
- A refusal says what to change. If the change is to the form of the call (a type, a key, a format), make it and call again. If it would change a time, a price, a size, a reason or a report, or calls for a void, ask the user first.

## Entering a trade
- Supply `market` at entry: every indicator key the rules name and every declared fact (the desk computes none), read off the chart at the bar acted on (a regular-hours chart for stocks, extended hours for futures). A key left out reads unknown for good, since `market` is not corrected.
- Answer every checklist item with `verdict` "pass" or "fail" and a one-line `reason`, as you read it.
- Screenshots are full file paths or https:// links. For an image pasted into the chat, pass the file path you are shown for it, even a temporary one: the desk copies the file when the trade is recorded. If no path is shown, ask the user for one.

## After entry
- The desk learns nothing on its own: it reads nothing but your reports. Report each limit's fill or cancellation, and each exit with its kind, and its price for a hand exit or a fill away from the level.
- Report a stop-out at its real time (`exited`, kind "stop"), with the broker's fill price when the user has it.
- Report each expired day order as `cancelled`, why "not filled", dated at the session close it expired at.
- Before entering a trade on a run whose rules read its other trades (how many are open, a daily loss limit, consecutive losses, a cooldown), bring those trades up to date: report any fill, exit or cancellation the user confirms, and a position the user confirms the broker still holds as `open`.
- Stop a run, or void a whole trade, only on the user's word: neither can be undone.
- Fix a mistake with `correct_trade` or `void_trade`, never with a new trade on top.
