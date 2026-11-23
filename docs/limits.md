# Limits

- The desk reads no market data. It fetches no price bar and computes no indicator: every
  indicator value and every declared fact its rules read comes from you, in `market`, on a live
  run as on a replay one.
- A `market` value is not corrected after entry. A key left out reads unknown for good, so supply
  every key `get_strategy`'s `market_keys` names.
- A trade's outcome rests on your reports. The desk cannot tell you whether a stop filled, at what
  price, or whether a limit is still pending: report what your broker and your chart show.

What 0.11 does not do: place orders, or send alerts; keep account balances or account-level P&L;
convert currencies; take more than one fill or one exit per trade; expire orders (report an expired
order cancelled, with why `not filled`); move a pending limit's price (cancel it and enter again);
remove a stop or a target by correction (void the trade and enter again); correct a trade's run,
ticker or side (void it and enter it again); trade anything but stocks, ETFs and futures;
or read a desk.db an earlier version wrote.
