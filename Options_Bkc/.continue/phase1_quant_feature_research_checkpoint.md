# Quant Feature Research Checkpoint

Saved: 2026-07-29 (Asia/Kolkata)

Superseded on 2026-07-31 by
`.continue/eod_quant_dashboard_checkpoint.md`. Phase 1 now has 14 research
features: directional features run standalone, while context, confirmation,
and ATR normalization run as paired ablations against a shared directional
baseline. The historical notes below describe the earlier implementation.

## Current State

Phase 1 is implemented and complete. Phase 2 has not been implemented.

The EOD research runner:

- enables only `DERIVATIVES_QUANT` and `GAMMA_EXPANSION`;
- disables `LEVEL_REVERSAL`, `BREAKOUT_MOMENTUM`, opening context,
  candle patterns, momentum exhaustion, and ATR normalization;
- tests the 13 approved quantitative features one at a time;
- uses causal event-time replay;
- applies a 5% stop, 10% target, and 15-minute maximum holding period;
- reports qualified signals, entered trades, target exits, stop exits,
  time exits, management exits, unresolved positions, returns, P&L,
  and outcomes by selected strategy;
- appends `phase1_feature_status` as the final record in every
  `broker_tape_<feature>.jsonl`;
- records analytics trace/stress files as provenance while recalculating
  signals from the broker tape;
- creates research-only runtime profiles and does not change production
  strategy configuration;
- shares the tape audit and event-time index across experiments and writes
  only qualified/trade events to the per-feature JSONLs.

## Phase 1 Features

1. `expected_move`
2. `premium_response`
3. `futures_flow`
4. `consolidated_pcr`
5. `strike_pcr`
6. `volume_oi`
7. `iv_surface`
8. `iv_skew`
9. `india_vix_regime`
10. `gamma_concentration`
11. `straddle_expansion`
12. `futures_basis`
13. `order_book_imbalance`

Some features are non-directional filters and may correctly generate zero
standalone signals. Their value should be tested in Phase 2 combinations.

## Important Files

- `dummy_broker_replay/run_phase1_feature_research.py`
- `dummy_broker_replay/runner.py`
- `app/analytics/engine.py`
- `app/analytics/strategies/derivatives_quant.py`
- `app/analytics/strategies/gamma_expansion.py`
- `tests/test_phase1_feature_research.py`
- `tests/test_derivatives_quant_strategy.py`
- `README.md`

## Verification

- All 13 generated profiles completed a schema-v4 smoke replay.
- Full repository suite: 154 tests passed.
- No new Python dependency was required.

Run tests:

```powershell
.\myenv\Scripts\python.exe -m unittest discover -s tests
```

Run Phase 1 after a tape has a clean schema-v4 `session_end`:

```powershell
.\myenv\Scripts\python.exe -m dummy_broker_replay.run_phase1_feature_research E:\Option_Trade\data\broker_replay_tape_YYYY-MM-DD.jsonl --analytics-trace E:\Option_Trade\data\analytics_engine_trace_<session>.jsonl
```

## Phase 2 Starting Point

Implement logical combination research using the same causal replay and
outcome/reporting path. Do not modify production settings automatically.

Initial combination families agreed in the conversation:

1. Derivatives flow and microstructure:
   `premium_response`, `futures_flow`, `volume_oi`, `futures_basis`,
   `order_book_imbalance`.
2. Options positioning:
   `consolidated_pcr`, `strike_pcr`, `volume_oi`, `iv_skew`,
   `gamma_concentration`.
3. Volatility surface and regime:
   `expected_move`, `iv_surface`, `iv_skew`, `india_vix_regime`,
   `straddle_expansion`.
4. Gamma-expansion core:
   `gamma_concentration`, `straddle_expansion`, `iv_surface`, `iv_skew`,
   `expected_move`, `premium_response`.
5. Flow-confirmed gamma:
   `gamma_concentration`, `straddle_expansion`, `premium_response`,
   `volume_oi`, `futures_flow`, `order_book_imbalance`.
6. Cross-market derivatives confirmation:
   `premium_response`, `futures_flow`, `futures_basis`, `volume_oi`,
   `strike_pcr`, `iv_skew`, `order_book_imbalance`.
7. Full quantitative ensemble:
   all 13 approved quantitative features.

Before finalizing the Phase 2 matrix, use accumulated Phase 1 results to
remove consistently inactive/noisy directional features while retaining
non-directional filters in combinations where they have a mathematical role.
Rank combinations across multiple out-of-sample days using expectancy,
target rate, signal count, stability, and drawdown—not win rate alone.
