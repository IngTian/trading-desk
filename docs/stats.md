# Stats

`stats` takes one scope (`strategy`, `version` with a strategy, or `run`), narrowed by `mode`,
`tags` (the runs holding every tag given) and `conforming_only` (the trades whose rule results now
are all kept). Voided trades never count. It returns the trade ids behind each figure: `closed`,
the exits with R, split into `wins`, `losses` and `breakevens`; `win_rate` (wins over closed), and
`average_r`, `total_r`, `average_win`, `average_loss` and `average_planned_rr` (the reward-to-risk
planned at entry), over closed; `closed_no_r`, the exits without R; `pnl`, the profit or loss per
currency, gross of fees and never converted; `open`, `pending`, `cancelled` (by why) and
`not_judged` (each with its missing range and the report that settles it); `limits`; `left_out`,
what `conforming_only` left out; and `rests_on`, the trades by mode, tag set and version, and the
live exits by reported or judged. A figure with nothing under it is null. Over MCP, every list of
trade ids comes as `{count, ids}`, and `not_judged` as `{count, trades}`.

The fill rate, `limits.rate`, is filled ÷ (placed − still pending − not judged without a fill −
cancelled by the run's stop). The stats are never a verdict or a ranking.
