# Dummy Broker Replay Architecture

## Purpose

This package evaluates the current analytics and strong-signal code against a
recorded trading session without logging in to Angel One, opening a WebSocket,
placing an order, or changing the production worker.

The source capture is expected to contain:

- `market_event` records with raw SmartAPI option ticks and best-five depth;
- `gate_decision` records containing periodic option-chain snapshots;
- quote OHLC, OI, volume, bid/ask, and Greeks inside each snapshot.

The July 22 capture does not contain spot WebSocket events or an instrument-master
response. The replay therefore derives option contracts from snapshot metadata and
creates an explicitly synthetic NIFTY spot instrument. Recorded snapshot
`spot_price` values are used as the replay frame clock and spot source.

The July 22 snapshots also contain contracts whose stored underlying is `NIFTY`
but whose trading symbol begins with `FINNIFTY`. The dummy instrument master
applies the current production symbol-boundary rule and excludes those contracts.
The audit reports the exclusion count. Missing genuine NIFTY quotes are not
invented.

## Configured Chain Contract

The broker simulator derives its required option window from the captured
configuration:

```text
each_side = OPTION_WINDOW_EACH_SIDE
strikes = ATM - each_side through ATM + each_side
contracts per strike = CE and PE
total required contracts = (2 x each_side + 1) x 2
```

With the current `.env` value `OPTION_WINDOW_EACH_SIDE=4`, this is nine strikes
and 18 contracts. It is not hardcoded into tape validation.

Strict broker-simulation frames are invalid if any expected contract, quote, or
Greek row is missing or beyond its configured maximum age. Reduced chains may be
inspected diagnostically but cannot be counted as evidence for or against strong
signal generation.

## Broker Push/Pull Boundary

The simulator must preserve live broker semantics:

```text
RecordedMarketDataFeed pushes spot and subscribed option ticks
worker derives ATM and updates subscriptions
worker pulls market_quote() and option_greeks() on scheduled frames
```

The dummy broker does not proactively push REST snapshots into analytics.

## Isolation Boundary

All replay implementation lives under:

```text
dummy_broker_replay/
```

It imports production calculation components but does not modify or invoke the
live entry point:

```text
AnalyticsEngine
MicrostructureEngine
SignalGate
OptimalStrikeSelector
OptionChainState
normalize_tick
normalize_broker_greeks
```

The live `scripts/run_worker.py` and `app/workers/market_data_worker.py` remain
unchanged.

## Flow

```text
Recorded JSONL
   |
   +--> Session audit
   |      contracts, quote/Greek coverage, timestamp regressions
   |
   +--> RecordedMarketDataFeed
   |      raw WS payload -> production normalize_tick()
   |                         |
   |                         +--> production MicrostructureEngine
   |                                   |
   |                                   +--> production SignalGate history
   |
   +--> gate_decision snapshot frame
          |
          +--> RecordedBrokerClient selects current as-of frame
          |      |
          |      +--> market_quote() -> production normalize_tick()
          |      +--> option_greeks() -> production Greek normalizer
          |
          +--> production OptionChainState builds populated snapshot
          +--> production AnalyticsEngine
          +--> production SignalGate (forced shadow mode)
          +--> production strike selector for qualified signals
          |
          +--> run.log
          +--> gate_decisions.jsonl
          +--> summary.json / summary.txt
```

## Replay Modes

| Mode | Purpose | Scheduling |
|---|---|---|
| `decision-replay` | Current component-level baseline | Stored gate snapshot frames |
| `broker-sim` | Intended offline broker/worker simulation | Deterministic fixed interval |
| `file-order-diagnostic` | Diagnose original backlog/order | Physical JSONL order |

The current CLI names `decision-replay` behavior `event-time`, and names the
file-order diagnostic `faithful`. Those existing names should remain supported as
compatibility aliases when the modes are made explicit.

### Current `event-time`

This is the primary current-code assessment. An offset-only SQLite index orders
option events and snapshot frames by captured timestamp. Market events sort before
a snapshot when their timestamps are equal. Quote and Greek data always come from
the current frame, so future frames cannot leak into an earlier decision.

This mode answers:

> Would the current analytics and gate produce strong signals if the recorded
> information were processed in event-time order?

### `faithful`

This preserves physical JSONL line order. It includes the original queue backlog,
timestamp reversals, and resulting staleness.

This mode answers:

> What does current logic do when subjected to the recorded session's original
> control-flow ordering?

The two modes are intentionally reported separately.

## Broker-Simulation Time Contract

The future `broker-sim` mode must use an injected replay clock rather than wall
time. The interval must be explicit, normally:

```text
--interval-ms 15000
```

The current production settings default is 1000 ms, so 15 seconds must never be
assumed. The run manifest also records the scheduling anchor:

```text
first-complete
market-open
explicit ISO timestamp
```

At frame time `T`, ordering is:

```text
drain subscribed option events with received_at <= T
resolve latest spot observation <= T
inject synthetic spot/frame tick
calculate ATM +/- 4 and update subscriptions
pull quotes and Greeks with source timestamp <= T
enforce completeness/freshness
run analytics and gate
write decision
```

No source value timestamped after `T` may be used.

## Data-Quality States

Each scheduled broker-simulation frame must be labelled:

```text
VALID         all 18 contracts and freshness rules pass
DEGRADED      allowed only for diagnostics, never signal proof
INVALID_DATA  required contract/value is missing or stale
SKIPPED       no valid spot/ATM or frame cannot be constructed
```

The pre-run coverage matrix reports expected contracts, available contracts,
source ages, contamination, and frame status. July 22 results must be labelled
`DATA_LIMITED` if no strict-complete frames exist.

## Reproducibility Manifest

Every broker-simulation run records:

```text
source SHA256 and size
code/source revision identity
simulator and output schema version
effective settings
mode, interval, anchor, and timezone
subscription policy
maximum spot/quote/Greek ages
chain-completeness policy
```

Canonical structured JSONL is the source of truth. Repeated runs with identical
inputs and settings should produce byte-identical decision records. Human-readable
`regression_YYYY-MM-DD.txt` is derived from JSONL.

## Production-Parity Boundary

The current isolated runner validates production analytics, microstructure, gate,
and targeting components. It does not prove parity with the unchanged
`run_market_data_worker()` scheduling loop.

Accelerated testing of that exact loop requires small backward-compatible
injection seams for:

```text
clock
snapshot-due policy
recorder/output destination
```

Without those seams, broker simulation must remain a replay-only orchestration and
must not claim worker scheduling parity.

Production analytics currently compares UTC snapshot `time()` values to naive
09:20/09:45 market-time thresholds. Parity mode exposes that behavior. Any
timezone-corrected research mode must be separately labelled and follow an
explicit production correction.

## Output

Every invocation creates a new directory and refuses to overwrite an existing run:

```text
dummy_broker_replay/runs/
  microstructure_events_2026-07-22_event-time_<run-id>/
    schema_audit.json
    event_time_index.sqlite
    run.log
    gate_decisions.jsonl
    summary.json
    summary.txt
```

`published_signal` remains `NEUTRAL` because replay forces shadow mode. A successful
research signal is represented by:

```text
qualified=true
strong_signal=BUY_CALL or BUY_PUT
```

For Gamma validation, also require:

```text
setup_type=MOMENTUM_EXPANSION
strategy_source=GAMMA
0.35 <= abs(target_delta) <= 0.65
```

For the future broker simulator, structured frame status and decisions remain
authoritative; `regression_YYYY-MM-DD.txt` is only a readable rendering consumed
alongside the structured analyzer output.

## Run Procedure

From `E:\Option_Trade\Options`:

Smoke-test the first 10 frames:

```powershell
python -m dummy_broker_replay.run_replay `
  E:\Option_Trade\data\microstructure_events_2026-07-22.jsonl `
  --mode event-time `
  --max-frames 10
```

Run the full current-code assessment:

```powershell
python -m dummy_broker_replay.run_replay `
  E:\Option_Trade\data\microstructure_events_2026-07-22.jsonl `
  --mode event-time
```

Run an exclusive live-compatible quantitative selection:

```powershell
python -m dummy_broker_replay.run_replay `
  E:\Option_Trade\data\broker_replay_tape_2026-07-31.jsonl `
  --mode event-time `
  --strategies DERIVATIVES_QUANT,GAMMA_EXPANSION `
  --features premium_response,futures_flow,iv_skew,gamma_concentration,order_book_imbalance `
  --minimum-book-imbalance 0.25
```

If no strategy, feature or threshold override is supplied, replay uses the
selected central strategy profile without modification.

The same `--strategies`, `--features` and `--minimum-book-imbalance` arguments
are accepted by `run_strategy_matrix` and `run_eod_research`. Use `--profiles`
to control which base profiles they compare. The common arguments are parsed
by `dummy_broker_replay.runtime_selection`, and every matrix/EOD manifest saves
the effective runtime selection.

Phase 1 and Phase 2 are intentionally different: they generate isolated
feature and logical-combination profiles. In Phase 1, `--features` selects the
features to ablate one at a time; it is not a global runtime feature override.
Phase 2 uses its reviewed combination definitions. Use `run_replay` for one
arbitrary feature combination.

Run the file-order comparison:

```powershell
python -m dummy_broker_replay.run_replay `
  E:\Option_Trade\data\microstructure_events_2026-07-22.jsonl `
  --mode faithful
```

Inspect the latest summary:

```powershell
Get-ChildItem .\dummy_broker_replay\runs -Directory |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 |
  ForEach-Object { Get-Content (Join-Path $_.FullName 'summary.txt') }
```

## Limitations

- This is deterministic research replay, not an execution simulator.
- Spot values come from stored snapshot frames because no spot market events were
  captured.
- Broker REST payloads are reconstructed from normalized stored quotes and Greeks;
  unavailable broker-only fields are not invented.
- Analytics state starts at the beginning of the selected replay every time.
- Results demonstrate how the current code responds to the recorded information;
  they do not establish profitability or future market performance.
- Current stored-frame replay does not enforce a complete 18-contract chain after
  contaminated contracts are excluded, so its July 22 zero-signal result is
  data-limited for broker-simulation purposes.
- Historical option events reflect only the subscriptions that existed during the
  original session. A newly selected replay ATM window may have no depth history.
- Stored snapshot spot values can provide synthetic carried-forward observations,
  but the capture has no genuine spot WebSocket events.
