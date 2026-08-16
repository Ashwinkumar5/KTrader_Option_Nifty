# SMC Liquidity Sweep Reclaim

## Production status

Research/shadow only. The profile does not replace the active
`derivatives_only` profile and does not publish broker-facing orders.

Use the exact profile name `Liquidity_Sweep_Reclaim`.

## Signal definition

The implementation uses synchronized NIFTY futures prices and is causal:

1. Build the 09:15-09:30 India-time opening range plus non-repainting,
   confirmed swing highs and lows.
2. Detect a futures-price sweep at least 2 points beyond a confirmed level.
3. Require reclaim within 30 seconds.
4. Require a micro-structure break within 60 seconds and displacement of at
   least 4 points or 1.5 times the rolling median price change.
5. Require same-side cross-strike option-premium impulse.
6. Before entry, require an executable target option, fresh same-side dynamic
   OFI for that exact CE/PE, and fresh same-side NIFTY-futures dynamic OFI.
7. Limit event alignment to 30 seconds and option-premium chase to 1.5%.

All SMC chart state is isolated in `app/analytics/strategies/smc.py` and uses
bounded deques. Existing option-chain impulse and order-book engines are
reused; their work is not duplicated.

## Required data

Use the broker event tape / 5-second decision data. One-minute candles are not
used for this profile because a 30-second sweep/reclaim cannot be reproduced
reliably from minute OHLC data.

## Reference-tape result

Faithful capture-order replay, 0.20% estimated round-trip cost:

| Date | Frames | Qualified trades | Net result |
| --- | ---: | ---: | ---: |
| 2026-08-03 | 1,299 | 0 | 0.0000% |
| 2026-08-04 (two partial tapes) | 660 | 0 | 0.0000% |
| 2026-08-05 | 1,030 | 0 | 0.0000% |
| 2026-08-06 | 796 | 0 | 0.0000% |
| 2026-08-07 | 812 | 1 | +1.9852% |
| **Total** | **4,597** | **1** | **+1.9852% on the one trade** |

The qualified trade was BUY 24,600 PE at 135.00 on 2026-08-07 11:50:08 IST,
time-exited after 15 minutes at 137.95. Gross return was +2.1852%; net return
after the configured cost was +1.9852%, with paper P&L of 174.20.

The sample is too small to claim profitability. Aug 4 is incomplete for this
strategy: its tapes start after the opening range and many candidate frames
had no executable contract after DTE, volume, OI, spread, IV and delta checks.
Aug 7 starts at 09:34 IST and therefore uses confirmed swing levels rather
than a recorded opening range.

## Replay command

```powershell
.\myenv\Scripts\python.exe -m dummy_broker_replay.run_replay `
  E:\Option_Trade\data\tapes\broker_replay_tape_2026-08-07_1.jsonl `
  --mode faithful `
  --strategy-profile Liquidity_Sweep_Reclaim `
  --compact-output `
  --output-root .task-tmp\smc-reference
```

`--compact-output` evaluates every frame and retains complete summaries,
signals, trades and exits, but avoids writing large rejected-frame snapshots.
