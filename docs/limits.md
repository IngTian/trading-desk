# Limits

- A split or another corporate action,
  such as a merger or a special dividend, during an open live trade is not adjusted for, because
  Yahoo's bars are unadjusted.
- A future's 1d or 1w indicator, and one on any timeframe Yahoo lacks (it has 1m, 2m, 5m, 15m, 30m,
  1h, 90m, 1d and 1w), is not computed: supply it in `market`.
- `trade_bars` at 1m returns about 45,000 characters per trading day of a stock, and about three
  times that for a future. Claude Code saves a reply over 50,000 characters to a file, so past about
  a day the agent reads it from there; a trade open for weeks can return a megabyte or more.

What 0.10 does not do: place orders, or send alerts; keep account balances or account-level P&L;
convert currencies; take more than one exit per trade; expire orders (report an expired order
cancelled, with why `not filled`); move a pending limit's price (cancel it and enter again); remove
a stop or a target by correction (void the trade and enter again); trade anything but stocks, ETFs
and futures, a future being Yahoo's rolling front month; look inside a bar at finer bars; or judge
replay runs from bars.
