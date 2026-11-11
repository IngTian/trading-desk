# Trading Rules — Example

All figures in this file are invented. Copy this file to start your own rules.

This file is read by `trading-desk check`, which compares a planned trade against your written
rules before you click. The tool computes the checks below; rules it does not implement (like a
sector limit or a setup preference) are kept as questions and you answer them each time.

## All

Rules that apply to every account.

```rules
min_reward_risk: 2
max_risk_per_trade: 150 USD
stop_required: yes
target_required: yes
```

### Ask me

- Does this trade fit my current market view?
- Have I checked recent news on this ticker?
- Is my risk tolerance appropriate for current conditions?

## Real

Rules for real-money trades. Stricter limits while building skill.

**min_reward_risk:** The minimum reward-to-risk ratio (R). A 2:1 trade means the distance from
entry to target is twice the distance from entry to stop, so if the trade works you gain 2R and if
it fails you lose 1R.

```rules
min_reward_risk: 2.5
max_risk_per_trade: 100 CAD
size: computed
max_open_trades: 2
no_open_trade_in_group: tech
cooldown_after_stop: 1 day, same ticker
max_session_length: 3 hours
daily_loss_limit: -2R
banned: shorts, futures, options
```

### Ask me

- Am I trading for the right reasons today?
- Does this setup match my proven edge?

## Paper

Rules for paper-trading (simulated money). Looser risk limits to test strategies.

**ACB:** Adjusted cost base — the average price paid for the position, used to compute profit or
loss.

```rules
max_risk_per_trade: 300 USD
max_open_trades: 5
cooldown_after_stop: 6 hours
max_session_length: 4 hours
stop_headroom_candles: 1.5
max_volatility_ratio: 2.5
```

**Note:** `stop_headroom_candles` measures how many median candle ranges sit between entry and stop.
It requires price bars, which are not tracked in v1 of this tool — the check reports "cannot
compute" until bars are added in a later version.

**Note:** `max_volatility_ratio` compares 20-day volatility to its 1-year median. It also requires
price bars and will report "cannot compute" until they are available.

### Ask me

- Is this setup worth tracking for later real-money use?
- What would I do differently with real money on the line?

## Replay

Rules for backtesting historical trades using recorded prices.

**Point value:** The dollar value per point of price movement. For stocks and ETFs this is $1.00
per share (one point = one dollar). For futures it depends on the contract — ES (S&P 500 E-mini)
has a point value of $50, meaning a 1-point move equals $50 per contract.

```rules
max_risk_per_trade: 200 USD
max_open_trades: 3
daily_loss_limit: -3R
```

### Ask me

- Does this replay scenario reflect realistic market conditions?
- Am I respecting the time-of-day constraints I would face live?

## Groups

Named groups of tickers, used by `no_open_trade_in_group` to prevent simultaneous exposure in
correlated assets.

```rules
tech: ACME, WXYZ, BETA
test_symbols: TEST, DEMO
```

## Changes

A dated log of edits to this file. The tool hashes the file's content, so when you change a rule
the hash changes and the record shows which version of your rules governed each trade.

- 2026-01-10: Initial rules for this example file
- 2026-02-15: Tightened real-money reward-to-risk to 2.5
- 2026-03-20: Added volatility and stop-headroom checks to paper account
- 2026-04-05: Defined groups for tech tickers
