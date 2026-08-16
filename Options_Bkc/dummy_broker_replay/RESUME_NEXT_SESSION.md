# Strategy Engine — Next Session Handoff

Saved: 2026-07-26 (Asia/Kolkata)

## Current State

The engine is organized around three strategy families:

1. `LEVEL_REVERSAL` / range rotation
2. `BREAKOUT_MOMENTUM`
3. `GAMMA_EXPANSION`

PCR, chain velocity, IV skew, smart-money divergence, local divergence,
microstructure, liquidity, and volatility cost are supporting evidence or
filters—not independent entry strategies.

The routing order is:

```text
NIFTY/data-quality gate
  -> risk and session gate
  -> regime classification
  -> compatible strategy family
  -> directional confirmations
  -> strike/contract selection
  -> exact-contract microstructure trigger
  -> final score and position sizing
  -> paper/shadow execution
```

## Noise and Structural-Level Handling

- Support/resistance uses a configurable **4-minute (240-second)** structural
  frame.
- Levels stay fixed during the active four-minute frame.
- The most persistent candidate from the completed frame becomes the next
  active level.
- Signal execution still uses **15-second closed frames**.
- A directional signal requires **2 of the last 3 closed frames**.
- One contrary frame changes the setup to `DEGRADED` and suppresses entry.
- Two soft contrary/breach frames invalidate the setup.
- A hard structural breach invalidates immediately.
- CALL and PUT handling is symmetrical.

Primary implementation:

```text
app/analytics/structural_levels.py
app/analytics/range_rotation.py
app/signals/noise_filter.py
app/analytics/engine.py
```

Effective setting:

```text
STRUCTURAL_LEVEL_FRAME_SECONDS=240
```

## Safety and Execution State

- Only NIFTY is accepted by default.
- Contaminated FINNIFTY contracts are rejected.
- Exact selected-contract alignment is required for microstructure evidence.
- Chain completeness, quote/Greek freshness, spread, liquidity, delta, DTE,
  exposure, cooldown, session, and daily-loss checks are enforced.
- Replay execution is paper-only.
- Live strategy publication remains shadow/paper; no live broker order path was
  enabled.

## Verification Completed

```text
80 tests passed
compileall passed
120-frame event-time smoke replay passed
```

Latest smoke replay:

```text
dummy_broker_replay/runs/
microstructure_events_2026-07-22_event-time_four-minute-levels-smoke-20260726
```

The smoke replay produced no strong signals. This is still data-limited because
the July 22 source contains FINNIFTY-contaminated contracts; after removing them,
some required NIFTY ATM CE/PE pairs are incomplete. Zero trades must not be
interpreted as proof that the strategy has no edge.

## Configurable Strategy Experiments

Strategy families now emit independent typed candidates. Enable/disable,
resolver policy, and priority are configurable for replay research without
reordering safety stages. `run_strategy_matrix` evaluates all seven non-empty
family combinations, with an optional nineteen-run matrix including meaningful
priority permutations. See `STRATEGY_EXPERIMENTS.md`.

## Tomorrow's Strategy-Only Agenda

Discuss and agree on the role, sequencing, thresholds, conflicts, and scoring
for:

1. IV level, IV rank, and IV change/velocity
2. IV skew and skew change
3. Order-book imbalance and persistence
4. Volume/OI ratio and OI change
5. LTP/premium movement and premium velocity
6. How these factors confirm or reject:
   - `LEVEL_REVERSAL`
   - `BREAKOUT_MOMENTUM`
   - `GAMMA_EXPANSION`
7. Separate CALL and PUT examples, while keeping implementation symmetrical
8. Decide which conditions are:
   - mandatory confirmations
   - weighted confirmations
   - conflict penalties
   - hard vetoes

Do not tune thresholds or add new strategy logic before this discussion. First
define the market meaning of each factor and its strategy-specific role, then
implement and replay-test it.

## Useful Commands

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m dummy_broker_replay.run_replay `
  E:\Option_Trade\data\microstructure_events_2026-07-22.jsonl `
  --mode event-time
```
