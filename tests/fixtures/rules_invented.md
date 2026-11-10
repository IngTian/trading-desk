# Trading Rules

Rules for the invented test portfolio. All figures are invented.

## All

```rules
min_reward_risk: 1
max_trades_per_day: 5
```

### Ask me

- Is this sector overheated?
- Does the thesis still hold?

## Real

```rules
min_reward_risk: 3
max_position_pct: 10
```

### Ask me

- Will you check this trade tomorrow?

## Paper

```rules
max_position_pct: 20
feels_right: yes
```

### Ask me

- Are you tracking this elsewhere?

## Paper — run 2

```rules
max_position_pct: 25
```

## Replay

```rules
max_position_pct: 15
```

## Groups

```rules
tech: ACME, WXYZ
consumer: TEST
```

## Changes

- 2026-01-15: Initial rules
- 2026-02-20: Increased real min_reward_risk to 3
- 2026-03-10: Added paper run 2 with higher position limit
