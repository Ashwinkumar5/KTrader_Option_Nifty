# Broker Replay Tape — Capture Schema v4

## Runtime performance and feed health

Every new `gate_decision.frame` contains a `performance` object with bounded
latency samples, feed queue health, recorder backlog and recent chain-write
latency. UTC remains authoritative; `_ist` companion fields are included for
operators reading the JSONL directly.

The live Angel One queue is bounded. `PRESSURE`, `DATA_LOSS`, or `FAILED`
health forces a preflight rejection and therefore `NEUTRAL`/no-trade. Queue
overflow is never hidden: the dropped-event counter is latched because an
event gap invalidates velocity and book-persistence evidence.

With `REPLAY_CAPTURE_ENABLED=true`, the schema-v4 tape is the authoritative
raw-tick journal. Set `OPERATIONAL_TICK_JOURNAL_ENABLED=true` only when a
second `ticks.jsonl` copy is explicitly required. The same rule applies to
`OPERATIONAL_CHAIN_JOURNAL_ENABLED` and `option_chain_snapshots.jsonl`.

Each single-strategy live worker appends to:

```text
E:\Option_Trade\data\<profile_name>\<strategy_name>\broker_replay_tape_YYYY-MM-DD.jsonl
```

This keeps DQ, Gamma, SMC, and other concurrently running strategy workers in
separate tapes even when they share the same profile. A legacy worker that
intentionally enables several strategies in one process retains one
profile-level tape.

The live worker does not write a separate `analytics_engine_trace_*.jsonl`;
strategy analytics remain embedded in each replay tape `gate_decision` record.

Schema v4 is designed to support chronological replay, strategy-family
ablations and feature research without reconnecting to the broker. It never
records or submits live orders.

## Captured records

- `session_manifest`: sanitized resolved settings, code revision, timezones,
  ordering semantics and capture capabilities.
- `instrument_master`: NIFTY spot token, selected-expiry options and available
  NIFTY futures.
- `subscription_change`: initial spot/future and dynamic option subscriptions.
- `market_event`: every normalized spot, future and option event, exchange and
  receive timestamps, original broker payload, best-five-derived features and
  any microstructure candidate.
- `gate_decision`: complete normalized option frame, underlying/future market
  context, refresh provenance, raw strategy analytics/evidence, final shadow
  decision and data/research readiness. A qualified simulator entry includes
  `execution_signal` with the stable `signal_id`, profile, strategy, side and
  strike used by KTrader Simulator, plus `dispatch_status` (`QUEUED`, `DROPPED`
  or `DISABLED`).
- `paper_fill`: simulated entry/exit, strategy attribution, contract, position
  plan and account state. It never represents a broker order.
- `session_end`: clean-close status, processed tick count and zero-drop writer
  diagnostics, including any unresolved paper position at shutdown.

Every record has a strictly increasing per-session `sequence`. Disk output uses
a bounded batched queue. Full gate frames are durable flush points, preventing
per-tick file-open latency while bounding data loss during an abnormal stop.

KTrader Simulator uses `execution_signal.signal_id` as its order ID. Its
background ledger therefore provides the actual entry, exit, price, P&L and
exit reason for EOD joining without returning fill traffic to the bot.

## Frame readiness

`data_quality.status=VALID` requires the configured CE/PE window to contain:

- every selected contract and fresh normalized quote;
- LTP, valid bid/ask, OI and volume;
- synchronized ATM CE/PE;
- IV and delta when Greeks are enabled;
- current REST-refresh token coverage;
- a fresh underlying observation.

`research_quality.status=RESEARCH_READY` additionally requires:

- spot open and previous close;
- spot exchange timestamp;
- nearest-future timestamp, price, volume and OI;
- synchronized ATM bid/ask mids;
- complete premium-attribution inputs.

Missing values are recorded explicitly and never replaced with zero or inferred
future data.

`analytics.strategy_diagnostics` records the observed and required values for
every strategy family, including `NO_CANDIDATE`, `SUPPRESSED`, and `SELECTED`
states. A selected local reversal also records its `activation_level`,
`local_support`, and `local_resistance`.

## Runtime configuration

```text
LOCAL_STORAGE_DIR=E:\Option_Trade\data
SNAPSHOT_INTERVAL_MS=15000
OPTION_WINDOW_EACH_SIDE=4
OPTION_GREEKS_ENABLED=true
MICROSTRUCTURE_ENABLED=true
MICROSTRUCTURE_MODE=shadow
REPLAY_CAPTURE_ENABLED=true
REPLAY_CAPTURE_FILE_PREFIX=broker_replay_tape
REPLAY_REQUIRE_COMPLETE_WINDOW=true
```

The feature/strategy enable flags, sequences, thresholds and risk controls are
also stored in the sanitized manifest. Credentials and connection strings are
never stored.

## EOD workflow

Validate only:

```powershell
python -m dummy_broker_replay.run_eod_research `
  E:\Option_Trade\data\derivatives_only\DERIVATIVES_QUANT\broker_replay_tape_2026-07-27.jsonl `
  --validate-only
```

Validate and run all seven strategy-family ablations:

```powershell
python -m dummy_broker_replay.run_eod_research `
  E:\Option_Trade\data\derivatives_only\DERIVATIVES_QUANT\broker_replay_tape_2026-07-27.jsonl
```

Include fixed-priority permutations:

```powershell
python -m dummy_broker_replay.run_eod_research `
  E:\Option_Trade\data\derivatives_only\DERIVATIVES_QUANT\broker_replay_tape_2026-07-27.jsonl `
  --include-priority-permutations
```

The EOD command first writes `capture_audit.json`. If validation fails, it writes
`EOD_FAILED.txt` and does not run experiments. A successful matrix writes
`EOD_COMPLETE.txt`, isolated replay runs and `matrix_summary.json`. It never
changes live configuration or promotes a same-sample “winner.”
