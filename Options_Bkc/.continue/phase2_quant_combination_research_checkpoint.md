# Phase 2 Quant Combination Research Checkpoint

Status: implemented and verified on 2026-07-30.

Methodology updated on 2026-07-31: ATR normalization is part of the volatility
regime and full-ensemble combinations; replay reports include transaction
costs, feature coverage, and explicit strategy attribution. Rolling eligibility
is documented in `.continue/eod_quant_dashboard_checkpoint.md`.

## Scope

- Research-only EOD replay; live strategy configuration is never modified.
- Uses only `DERIVATIVES_QUANT` and `GAMMA_EXPANSION`.
- Disables level reversal, breakout momentum, opening context, candle patterns,
  momentum exhaustion, and index-price momentum.
- Paper outcomes use a 5% stop, 10% target, and 15-minute maximum hold.

## Implemented combinations

1. `derivatives_flow_microstructure`
2. `options_positioning`
3. `volatility_surface_regime`
4. `gamma_expansion_core`
5. `flow_confirmed_gamma`
6. `cross_market_derivatives`
7. `full_quant_ensemble`

Each combination has explicit feature flags, normalized directional weights,
and confirmation requirements appropriate to its evidence. Diagnostic ranking
requires a configurable minimum trade sample and does not select or promote a
live profile.

## Main files

- `dummy_broker_replay/run_phase2_combination_research.py`
- `dummy_broker_replay/runner.py`
- `app/analytics/strategies/derivatives_quant.py`
- `app/signals/gate.py`
- `tests/test_phase2_combination_research.py`
- `README.md`

## Run

```powershell
python -m dummy_broker_replay.run_phase2_combination_research E:\Option_Trade\data\broker_replay_tape_YYYY-MM-DD.jsonl
```

Optional provenance:

```powershell
python -m dummy_broker_replay.run_phase2_combination_research E:\Option_Trade\data\broker_replay_tape_YYYY-MM-DD.jsonl --phase1-summary E:\path\to\phase1_summary.json --analytics-trace E:\path\to\analytics_engine_stress.jsonl
```

Outputs are stored in a unique run directory and include JSON, CSV, and text
summaries; generated profiles; capture audit; and one final-status JSONL file
per combination. Reports include signal/trade counts, target/stop/time exits,
unresolved trades, per-strategy results, return, P&L, and maximum drawdown.

## Verification

- Python compilation passed for all changed Phase-2 modules.
- All seven profiles completed a one-frame schema-v4 fixture smoke replay.
- Full regression suite: 160 tests passed.

## Next EOD action

Run Phase 1 and Phase 2 against the completed real trading-day tape. Accumulate
multiple days before making profitability or win-rate conclusions; rankings
below the minimum sample remain explicitly unranked.
