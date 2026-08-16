# Quant Strategy Research Checkpoint — 2026-08-02

## Current objective

Use each complete daily broker tape as a causal strategy simulation, compare a
frozen baseline with small research challengers, and seek positive aggregate
expectancy after costs without repeatedly fitting the same tapes.

## Safety boundary

- Active/live profile remains `derivatives_only`.
- No production strategy code was changed in this research step.
- Replay stays in shadow/paper mode and does not contact the broker.
- Do not promote a configuration merely because it wins on tapes used to design
  it.

## Configuration state

Added the EOD-only profile `derivatives_quant_balanced_research` in
`config/strategy_config.json`. It extends `derivatives_only` and changes exactly:

```text
early_min_horizon_agreement     3 -> 2
early_min_independent_families 4 -> 3
early_min_buyability_score     0.65 -> 0.60
```

Acceleration is still mandatory. No acceleration code/config change has been
made.

If acceleration is tested later, implement a default-true configuration flag
such as `require_early_acceleration`; set it false only in a separate research
profile. Do not remove the hard condition globally.

## Authoritative 31-Jul full-tape replay

Source:

```text
E:\Option_Trade\data\31072026\broker_replay_tape_2026-07-31.jsonl
SHA-256: a11c09030e44d387b2c73751931ca62d9f276f0f906fa73297bf6d3d6becf3e5
```

Comparison output:

```text
dummy_broker_replay/runs/strategy_comparisons/2026-08-01/
  broker_replay_tape_2026-07-31_event-time_20260731_balanced_thresholds/
```

Aggregate result before explicit transaction costs:

| Metric | `derivatives_only` | balanced research |
|---|---:|---:|
| Qualified BUY_CALL signals | 4 | 4 |
| Targets | 0 | 0 |
| Stop exits | 3 | 3 |
| Time exits | 1 | 1 |
| Completed return sum | -15.1443% | -14.9632% |

Three trades were identical. The fourth changed:

```text
Baseline: 13:19:18 IST, entry 74.35, SL 13:31:22 at 70.40, -5.31%
Balanced: 13:07:59 IST, entry 76.00, SL 13:11:40 at 72.10, -5.13%
```

The candidate entered 11 minutes 19 seconds earlier but still stopped out.

At the candidate's earlier frame:

```text
direction score = +0.2971
horizons         = 3/3
buyability       = 0.8378
aligned families = exactly 3
acceleration     = true
```

Therefore, only reducing independent families from four to three caused the
earlier entry on this tape. Horizon and buyability relaxation had no practical
effect that day. The candidate is not approved for live use.

Correction retained for future analysis: the complete broker tape contains an
additional 09:22 morning signal. The authoritative daily total is four signals,
not the three previously found from the split analytics trace files.

## Validation completed

```text
20 relevant tests passed:
- strategy configuration
- derivatives-quant strategy
- dummy broker replay
- microstructure replay
- replay tape validation
```

The full causal matrix processed 2,289 decision frames from approximately
848,000 market events for both profiles. Production settings were unchanged.

## Research discipline agreed with the user

1. Freeze `derivatives_only` as the champion baseline.
2. Do not tune repeatedly until the same historical days become profitable.
3. Test one causal change per challenger:
   - horizon only: 3 -> 2
   - independent families only: 4 -> 3
   - buyability only: 0.65 -> 0.60
   - acceleration only: mandatory -> optional
4. Treat existing reviewed tapes as discovery data. Lock candidates before
   evaluating future unseen tapes.
5. Replay each new daily tape once and append results; review weekly rather than
   retuning after every loss.
6. Evaluate net expectancy, targets/stops/timeouts, MFE, MAE, entry delay,
   transaction costs and drawdown. Win rate alone is insufficient.
7. Preliminary ranking requires at least 10 unique trading days and 30 completed
   trades. Production consideration should use at least 20 days and preferably
   60+ completed trades with positive cost-adjusted expectancy.
8. If all locked candidates remain negative on sufficient unseen data, stop
   relaxing gates and reject/redesign the directional hypothesis.

## Morning resume sequence

1. Do not change the live profile.
2. Record the balanced profile as `NOT_IMPROVED / DO_NOT_PROMOTE`.
3. Add a persistent research ledger containing experiment ID, hypothesis, exact
   config diff, tape dates/hashes, outcomes and verdict.
4. Make transaction costs plus MFE/MAE authoritative in the comparison report.
5. Create only isolated challenger profiles; do not generate an uncontrolled
   combination matrix.
6. Keep acceleration unchanged until its isolated experiment is explicitly
   approved.
7. Run locked profiles on newly captured unseen tapes and append to the dashboard.

## Replay command used

```powershell
python -m dummy_broker_replay.run_strategy_matrix `
  E:\Option_Trade\data\31072026\broker_replay_tape_2026-07-31.jsonl `
  --mode event-time `
  --strategy-config E:\Option_Trade\Options\config\strategy_config.json `
  --profiles derivatives_only,derivatives_quant_balanced_research `
  --output-root E:\Option_Trade\Options\dummy_broker_replay\runs\strategy_comparisons\2026-08-01 `
  --matrix-id 20260731_balanced_thresholds
```

