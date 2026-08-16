# EOD Quant Research Dashboard Checkpoint

Status: research methodology corrected on 2026-07-31. Live bot code and live
strategy configuration were not changed.

## Morning resume point

Saved on 2026-07-31 after implementation and verification. The research
harness is ready for a full replay against the real broker-tape folder:

```powershell
.\scripts\run_eod_quant_research.ps1 -BrokerTapeFolder E:\Option_Trade\data
```

Next action: run the real tapes, inspect the generated cost-adjusted dashboard,
and interpret the feature ablations and combinations. Do not change the live
strategy from the fixture smoke results.

## Daily command

```powershell
.\scripts\run_eod_quant_research.ps1 -BrokerTapeFolder E:\Option_Trade\data
```

Optional trace provenance:

```powershell
.\scripts\run_eod_quant_research.ps1 -BrokerTapeFolder E:\Option_Trade\data -AnalyticsTrace E:\Option_Trade\data\analytics_engine_stress_YYYY-MM-DD.jsonl
```

The folder is scanned recursively for every `broker_replay_tape*.jsonl` file,
including date subfolders. Files are replayed sequentially in full-path order
and consolidated into one batch dashboard.

## Output structure

`dummy_broker_replay/runs/eod_quant_research/YYYY-MM-DD/run_<unique-id>/`

- `phase1/`: seven standalone directional tests, one shared directional
  baseline, and seven paired context/confirmation/ATR ablations
- `phase2/`: all seven logical quantitative combinations
- `dashboard/dashboard.html`: human-readable dashboard
- `dashboard/dashboard.json`: complete machine-readable consolidation
- `dashboard/daily_features.csv`
- `dashboard/daily_combinations.csv`
- `dashboard/rolling_14d_combinations.csv`

Same-day reruns create unique run folders. Rolling history deduplicates the same
broker tape by SHA-256 and retains the newest replay.

## Ranking safeguards

- Default rolling window: 14 calendar days
- Minimum unique trading days: 8
- Minimum completed trades: 30
- Default replay-only round-trip cost: 0.20% of entry premium
- Only cost-adjusted profitable combinations receive a rolling rank
- Sort: net average return, decisive target/stop win rate, drawdown, sample
- No automatic production configuration change

The dashboard labels a positive eligible setup as `PROMISING`, not approved.
It displays no leader when every eligible setup is losing. It reports tape
files separately from trading days, actual `DERIVATIVES_QUANT` versus
`GAMMA_EXPANSION` signal attribution, estimated costs, and input coverage.

## Verification

- PowerShell and Python syntax checks passed.
- Corrected end-to-end PowerShell smoke passed with all 14 feature
  experiments, the shared baseline, all seven combinations, and the HTML
  dashboard.
- Windows path-length regression fixed by using compact inner replay folders
  (`b00`, `f01` through `f14`, `c01` through `c07`) and shorter batch
  identifiers.
- The input scan is recursive to support `data/DDMMYYYY` folders.
- Full regression suite after correction: 166 tests passed.
