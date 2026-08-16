# Strategy-Family Replay Experiments

New live captures must pass the schema-v4 readiness audit described in
[CAPTURE_SCHEMA_V4.md](CAPTURE_SCHEMA_V4.md) before strategy results are used.

The analytics engine evaluates strategy candidates independently and resolves
them using explicit configuration. Strategy order cannot change market data,
feature state, risk checks, contract selection, or execution-stage order.

## Strategy Families

```text
LEVEL_REVERSAL
BREAKOUT_MOMENTUM
GAMMA_EXPANSION
```

The production-safe resolver is `REGIME_EXCLUSIVE`. The other policies are
research comparisons:

```text
REGIME_EXCLUSIVE
FIXED_PRIORITY
HIGHEST_CONFIDENCE
CONFLICT_NO_TRADE
```

For `FIXED_PRIORITY`, the lower number wins. Priorities resolve candidate
collisions; they are not signal confidence scores.

The final signal gate still enforces regime compatibility, data quality,
two independent confirmation families, liquidity, risk, persistence, and
exact-contract microstructure. Replay remains paper/shadow-only.

## Environment Configuration

```text
STRATEGY_RESOLVER_POLICY=REGIME_EXCLUSIVE
STRATEGY_LEVEL_REVERSAL_ENABLED=true
STRATEGY_BREAKOUT_MOMENTUM_ENABLED=true
STRATEGY_GAMMA_EXPANSION_ENABLED=true
STRATEGY_LEVEL_REVERSAL_PRIORITY=10
STRATEGY_BREAKOUT_MOMENTUM_PRIORITY=20
STRATEGY_GAMMA_EXPANSION_PRIORITY=30

FEATURE_OPENING_CONTEXT_ENABLED=true
FEATURE_OPENING_CONTEXT_SEQUENCE=10
FEATURE_EXPECTED_MOVE_ENABLED=true
FEATURE_EXPECTED_MOVE_SEQUENCE=20
FEATURE_PREMIUM_RESPONSE_ENABLED=true
FEATURE_PREMIUM_RESPONSE_SEQUENCE=30
FEATURE_FUTURES_FLOW_ENABLED=true
FEATURE_FUTURES_FLOW_SEQUENCE=35
FEATURE_CANDLE_PATTERNS_ENABLED=true
FEATURE_CANDLE_PATTERNS_SEQUENCE=37
FEATURE_MOMENTUM_EXHAUSTION_ENABLED=true
FEATURE_MOMENTUM_EXHAUSTION_SEQUENCE=40

OPENING_OBSERVATION_MINUTES=15
EXPECTED_MOVE_CAPTURE_TIME=09:45:00
EXPECTED_MOVE_FIRST_BAND_RATIO=0.50
EXPECTED_MOVE_EXTENDED_BAND_RATIO=0.80
EXPECTED_MOVE_EXHAUSTION_BAND_RATIO=1.00
EXHAUSTION_EARLIEST_TIME=13:15:00
EXHAUSTION_MINIMUM_PREMIUM_RETURN_PERCENT=75
EXHAUSTION_MINIMUM_MOVE_UTILIZATION=0.80
GAMMA_WINDOW_SECONDS=300
REGIME_WINDOW_SECONDS=300
FUTURES_FLOW_WINDOW_SECONDS=60
REVERSAL_CANDLE_CONFIRMATION_REQUIRED=false
PREMIUM_TRANSMISSION_ENABLED=true
PREMIUM_TRANSMISSION_MIN_EXPECTED_RETURN_PERCENT=3
PREMIUM_TRANSMISSION_MIN_RATIO=0.35
SIGNAL_GATE_MIN_INDEPENDENT_CONFIRMATION_FAMILIES=2
LOCAL_REVERSAL_COOLDOWN_SECONDS=900
```

## Local-Level Reversal

`LEVEL_REVERSAL` also evaluates `LOCAL_LEVEL_REVERSAL` setups. These require a
persistent nearby OI level with at least half the primary level's OI, a closed
four-minute directional reversal candle, follow-through, PCR agreement and no
opposing futures flow. Opening-drive context raises confidence when the setup
is an opening failure; it no longer overrides an intact structural range.

For this setup only, exact-contract microstructure is additive rather than
mandatory. Fresh exact-contract opposition remains a hard veto, while signals
from unrelated strikes are ignored. The setup has a fifteen-minute cooldown
to match its research holding period.

Every analytics frame now records `strategy_diagnostics` with passed/failed
checks for all three strategy families, including candidates that were never
created or were suppressed by regime resolution.

Paper execution buys at ask, exits at bid, uses a 5% option stop, a 10% target
and a fifteen-minute time exit. It remains isolated from all broker order
paths.

Feature sequence controls evaluation inside the session-context stage only.
Duplicate sequences and unsafe dependencies are rejected at startup. It cannot
move a feature ahead of market-data quality/risk stages or move execution ahead
of contract/microstructure checks.

## One Configured Replay

```powershell
python -m dummy_broker_replay.run_replay `
  E:\Option_Trade\data\microstructure_events_2026-07-22.jsonl `
  --mode event-time `
  --strategies LEVEL_REVERSAL,BREAKOUT_MOMENTUM `
  --features opening_context,expected_move,premium_response,candle_patterns,momentum_exhaustion `
  --resolver-policy FIXED_PRIORITY `
  --strategy-priority BREAKOUT_MOMENTUM,LEVEL_REVERSAL
```

The supplied strategy and feature lists are exclusive. The command assigns
priorities 10 and 20 in the supplied order. When these arguments are omitted,
the selected central strategy profile remains unchanged.

Quantitative replay uses the same runtime selection as the live worker:

```powershell
python -m dummy_broker_replay.run_replay `
  E:\Option_Trade\data\broker_replay_tape_2026-07-31.jsonl `
  --strategies DERIVATIVES_QUANT,GAMMA_EXPANSION `
  --features premium_response,futures_flow,iv_skew,gamma_concentration,order_book_imbalance `
  --minimum-book-imbalance 0.25
```

Each run writes:

```text
run_manifest.json
gate_decisions.jsonl
summary.json
summary.txt
regression_YYYY-MM-DD.txt
```

The manifest includes the source hash, enabled families, exact priority order,
resolver policy, feature enable/sequence settings, signal timing, structural
timeframe and paper-risk settings.
Every analytics record contains all generated candidates and the selected
strategy family.

## EOD Strategy Matrix

Preferred one-command workflow (validation runs first):

```powershell
python -m dummy_broker_replay.run_eod_research `
  E:\Option_Trade\data\broker_replay_tape_YYYY-MM-DD.jsonl
```

Run all seven non-empty enable/disable combinations:

```powershell
python -m dummy_broker_replay.run_strategy_matrix `
  E:\Option_Trade\data\microstructure_events_2026-07-22.jsonl `
  --mode event-time
```

Also test meaningful fixed-priority permutations:

```powershell
python -m dummy_broker_replay.run_strategy_matrix `
  E:\Option_Trade\data\microstructure_events_2026-07-22.jsonl `
  --mode event-time `
  --include-priority-permutations
```

This produces 19 experiments:

```text
7 regime-exclusive family ablations
6 priority orders for two-family combinations
6 priority orders for all three families
```

The matrix shares one event-time index and one source hash. Its
`matrix_summary.json` records every configuration and result.

## Research Safety

Matrix runs never alter `.env`, production settings, or broker state. They do
not automatically promote a winning configuration.

Do not choose the largest same-sample P&L as the production strategy. Preserve
all attempted configurations, apply realistic spread/slippage, and compare
results across chronological out-of-sample sessions and market regimes.
