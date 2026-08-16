# Morning Resume v1

## Context

Date reviewed:

```text
2026-07-21
```

Files reviewed:

```text
overview_v1.md
data/TODAYS_OBSER.txt
scripts/run_worker.py
app/workers/market_data_worker.py
app/analytics/engine.py
app/signals/gate.py
app/optionchain/memory_state.py
app/microstructure/engine.py
app/greeks/strike_selector.py
```

## Current Understanding

The project is a live NIFTY option-chain analytics worker using Angel One SmartAPI.

Current live flow:

```text
scripts/run_worker.py
  -> load settings
  -> configure logging
  -> check Angel One credentials
  -> run_market_data_worker()
  -> login
  -> load instrument master
  -> subscribe spot token
  -> spot tick updates ATM
  -> subscribe ATM +/- option window
  -> save ticks
  -> refresh option quote snapshots
  -> fetch Greeks
  -> build OptionChainSnapshot
  -> AnalyticsEngine generates raw signal
  -> SignalGate validates or rejects raw signal
  -> print final signal line
  -> persist chain and analytics state
```

Important safety boundary:

```text
MICROSTRUCTURE_MODE=shadow
```

In shadow mode, final published signal remains `NEUTRAL` even when a raw setup qualifies. In today's log, no `SHADOW QUALIFIED` events were found.

## Today's Log Review

Reviewed:

```text
data/TODAYS_OBSER.txt
```

Summary from log:

```text
SHADOW QUALIFIED: 0
BUY_PUT rejected: 942
BUY_CALL rejected: 184
NEUTRAL rejected: 855

location rejection: 896
microstructure mismatch: 115

Gamma blast up detected: 58
Gamma blast down detected: 14
```

Conclusion:

```text
Raw strategies were generated, but no strategy was fully validated by the current gate.
```

The output should be treated as research/debug information, not confident trade direction.

## User's Three Questions

### 1. 09:38:33 Gamma Spring BUY_PUT Rejected

Log pattern:

```text
STRIKE ACQUIRED: Selected 2.425E+4 PUT | Delta: -0.63
[SIGNAL] ... SIGNAL=NEUTRAL REASON=GATE REJECTED BUY_PUT: spot is not at the structural location required by this setup
```

Explanation:

`AnalyticsEngine` generated raw `BUY_PUT` from Gamma Spring / Gamma Blast logic.

But `SignalGate._at_valid_location()` only recognizes these structural reason patterns:

```text
BREAKOUT VALIDATED
BREAKDOWN VALIDATED
EXHAUSTION REVERSAL
EXHAUSTION TOP
```

Gamma reasons are like:

```text
GAMMA PUT EXPANSION
GAMMA CALL EXPANSION
```

Those Gamma reasons are not currently accepted by the gate, so the gate rejects them as not being at a valid structural location.

Finding:

```text
Gamma Spring can propose a direction, but SignalGate does not yet have a Gamma-specific validation path.
```

### 2. 10:03:30 BUY_PUT Rejected, Then NEUTRAL Rejected

Log pattern:

```text
STRIKE ACQUIRED: Selected 2.42E+4 PUT
[SIGNAL] ... REASON=GATE REJECTED BUY_PUT: spot is not at the structural location required by this setup
[SIGNAL] ... REASON=GATE REJECTED NEUTRAL: candidate is not directional
```

Explanation:

These are separate snapshots.

One snapshot produced raw `BUY_PUT`, then the gate rejected it.

The next snapshot produced no directional setup, so analytics returned `NEUTRAL`; gate rejected it as non-directional.

This is expected with current code, but the log format makes it confusing because it does not show:

```text
raw signal
published signal
spot price
microstructure side
distance from support/resistance
gate branch
```

### 3. 10:46:48 BUY_PUT Rejected Because Microstructure Confirmed BUY_CALL

Log pattern:

```text
STRIKE ACQUIRED: Selected 2.42E+4 PUT | Delta: -0.63
[SIGNAL] ... REASON=GATE REJECTED BUY_PUT: microstructure confirms BUY_CALL, not BUY_PUT
```

Explanation:

Analytics/Gamma logic said:

```text
BUY_PUT
```

Latest microstructure signal said:

```text
BUY_CALL
```

Current `SignalGate` requires alignment:

```text
analytics side == microstructure side
```

They conflicted, so rejection was correct.

Conclusion:

```text
Do not take the PUT from this signal. Chain/Gamma and order-book pressure disagreed.
```

## Main Problem Identified

Gamma Spring is partially integrated.

It currently can:

```text
detect Gamma Blast up/down
generate BUY_CALL or BUY_PUT
select an option strike
```

But it currently cannot cleanly pass the gate because `SignalGate` has no Gamma-specific structural validation branch.

This creates many messages like:

```text
GATE REJECTED BUY_PUT: spot is not at the structural location required by this setup
GATE REJECTED BUY_CALL: spot is not at the structural location required by this setup
```

## Position Confidence Conclusion

Based on 2026-07-21 log:

```text
No confident trading direction should be taken from the current final signal output.
```

Reason:

```text
Final signal stayed NEUTRAL.
No SHADOW QUALIFIED event appeared.
Raw Gamma/analytics signals were repeatedly rejected.
Microstructure sometimes conflicted with Gamma direction.
```

## Suggested Morning Work

### 1. Improve Signal Display

Update `app/signals/display.py` and/or analytics/gate output to show:

```text
spot price
raw signal
published signal
gate qualified true/false
gate reason
support
resistance
distance from support
distance from resistance
microstructure side
microstructure age
selected strike
selected delta
strategy source
```

This will make logs easier to trust.

### 2. Add Gamma-Specific Gate Logic

In `app/signals/gate.py`, add structural validation for:

```text
GAMMA CALL EXPANSION
GAMMA PUT EXPANSION
```

Possible rules:

```text
GAMMA CALL EXPANSION:
  accept only if spot is near resistance or breaking pinned range high
  require microstructure BUY_CALL
  require repeated confirmations
  reject if selected strike delta is bad

GAMMA PUT EXPANSION:
  accept only if spot is near support or breaking pinned range low
  require microstructure BUY_PUT
  require repeated confirmations
  reject if selected strike delta is bad
```

### 3. Persist Rich Gate Decisions

Current printed line is too compressed.

Add explicit structured gate-decision logging so replay analysis can answer:

```text
Why did this signal fire?
Why was it rejected?
What side did each subsystem vote?
Was the rejection correct after price movement?
```

### 4. Fix Strike Selection Quality

Late-day logs showed suspicious selections like:

```text
Delta: 0.00
Delta: 1.00
```

Those should likely be rejected for naked option buying.

Add a delta-quality guard, for example:

```text
CALL delta allowed: 0.35 to 0.65
PUT abs(delta) allowed: 0.35 to 0.65
```

### 5. Backtest Today's Rejections

Use today's captured data to answer:

```text
After each Gamma BUY_CALL/BUY_PUT rejection, did price actually move in that direction?
Was SignalGate too strict, or was Gamma Spring noisy?
Was microstructure mismatch a good filter?
```

## Key Files for Next Session

```text
app/signals/gate.py
app/signals/display.py
app/analytics/engine.py
app/optionchain/memory_state.py
app/microstructure/engine.py
app/greeks/strike_selector.py
data/TODAYS_OBSER.txt
overview_v1.md
```

## Morning Starting Point

Start with:

```text
1. Open resume_morning_v1.md and overview_v1.md.
2. Review SignalGate._at_valid_location().
3. Add Gamma-specific validation path.
4. Improve signal logging format.
5. Add selected-strike delta guard.
6. Re-run tests.
7. Replay or inspect today's log again.
```

