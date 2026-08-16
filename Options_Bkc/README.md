# Options Analytics Platform

Backend foundation for a NIFTY / BANKNIFTY options analytics platform using Angle One SmartAPI as the first broker integration.

The first implementation target is intentionally narrow:

- SmartAPI session and feed connectivity
- Instrument master and token resolution
- Live NIFTY / BANKNIFTY spot tracking
- ATM strike detection
- 4 ITM + ATM + 4 OTM option-chain window
- Normalized OI / LTP / volume / quote snapshots
- Broker Greek / IV, PCR, and OI buildup ingestion hooks
- Redis live state and database-ready snapshot models

The project structure is intentionally larger than the first feature set because this is the base of a bigger trading platform. Analytics, paper trading, live execution, news crawling, backtesting, and multi-broker support should attach to domain models and normalized events, not directly to broker packets.

## Architecture Rule

Broker-specific code stays inside `app/broker`.

Everything else consumes internal domain models:

- `MarketTick`
- `OptionContract`
- `OptionChainSnapshot`
- `GreeksSnapshot`
- `AnalyticsSnapshot`

This keeps the platform portable if another broker/feed is added later.

## SmartAPI Data Sources

For the option-chain MVP, the preferred Angle One sources are:

- Login/session/feed token: `SmartConnect.generateSession`, then `getfeedToken`.
- Live prices, volume, OI, OI change, bid/ask, OHLC, timestamps: `SmartWebSocketV2`
  in `SNAP_QUOTE` mode, with `SmartConnect.getMarketData` as the REST fallback.
- Historical or fallback OI: `SmartConnect.getOIData`.
- IV and Greeks: `SmartConnect.optionGreek`.
- Broker-level cross-checks: `SmartConnect.putCallRatio` and `SmartConnect.oIBuildup`.
- Instrument/token discovery: Angle One instrument master, normalized into
  `InstrumentMaster`.

These choices are configurable through `.env` so the source preference and broker can
change later without changing analytics/domain code.

## Worker Flow

`run_market_data_worker` consumes the `MarketDataFeedHandler` boundary. The
current `EmbeddedMarketDataFeedHandler` performs the credential-ready live path:

1. Login with SmartAPI and collect `jwtToken`, `refreshToken`, and `feedToken`.
2. Load the Angel One instrument master from a configured URL or local JSON file.
3. Build broker-neutral spot and option contracts.
4. Connect `SmartWebSocketV2`.
5. Subscribe to NIFTY/BANKNIFTY spot tokens.
6. Detect ATM from spot ticks and subscribe to the configured CE/PE strike window.
7. Save every normalized tick once in the schema-v4 replay tape. The optional
   `ticks.jsonl` and `option_chain_snapshots.jsonl` duplicate journals are
   disabled when replay capture is active.
8. Refresh IV/Greeks through `optionGreek` when enabled.
9. Build option-chain snapshots on `SNAPSHOT_INTERVAL_MS` and capture them
   inside the corresponding replay-tape decision frame.

Broker login, instrument loading, WebSocket operations, REST quote refreshes,
Greeks refreshes, and feed health are therefore outside strategy processing.
The production watchdog runs every enabled strategy in every
`watchdog_enable: "Y"` profile as a subscriber of one supervised
`mkt_data_feed_handler`. That handler owns the broker login, WebSocket, option
subscriptions, FULL quote calls, and Greek refreshes, then broadcasts ordered
normalized ticks plus immutable atomic frames over a loopback Core-NATS server.
Each strategy keeps its isolated analytics, gate, tape, and router policy while
using the same 15-second decision cadence from the 5-second producer frames.
Any disconnect, epoch change, queue loss, malformed frame, or stalled worker
fails closed and the watchdog starts a fresh process.

Profiles marked `watchdog_enable: "N"` remain research-only and are not
launched. If later enabled, their strategies automatically use this same
subscriber topology; they never create a second broker connection.

The canonical feed tape is written under
`MARKET_DATA_FEED_TAPE_DIRECTORY` (default `data/feed_handler`). It is separate
from each strategy's decision/research tape.

The default local journal is:

- `data/broker_replay_tape_YYYY-MM-DD.jsonl`

The two legacy operational JSONLs can be enabled explicitly when a downstream
consumer requires duplicate tick or chain files.

## Central Simulator Signal Router

Strategy workers never connect directly to KTrader Simulator. The watchdog
starts one `central_signal_router` process, and every qualified live signal is
queued to that process over loopback TCP. The router always writes an audit
record, rejects duplicate `signal_id` values, and is the only owner of the
existing KTrader Simulator publisher.

Each strategy has a strict Boolean routing flag in
`config/strategy_config.json`:

```json
"DERIVATIVES_QUANT": {
  "enabled": true,
  "priority": 10,
  "publish_to_simulator": true
}
```

Missing, invalid, disabled, and unknown strategy policies fail closed. A
`false` flag produces a `LOG_ONLY` audit record and never reaches the Simulator
UI. The default audit is
`<LOCAL_STORAGE_DIR>/signal_router_audit.jsonl`; it contains both the router
decision and the later Simulator delivery result. Backtest/replay paper
execution does not use this live routing flag.

`ROUTER_CLIENT_QUEUED` in a worker tape means only that the worker's bounded
local queue accepted the signal; the central audit is authoritative for
`AUTHORIZED`, `QUEUED`, `LOG_ONLY`, `DUPLICATE`, `DROPPED`, and final Simulator
delivery. Account state, position limits, daily loss limits, and fills remain
owned by KTrader Simulator; this router adds policy gating and idempotent
transport, not a second independent risk ledger.

The watchdog starts the router automatically. For a standalone worker, start
the router first:

```powershell
.\myenv\Scripts\python.exe scripts\run_signal_router.py
```

The relevant optional environment settings are `SIGNAL_ROUTER_ENABLED`,
`SIGNAL_ROUTER_HOST`, `SIGNAL_ROUTER_PORT`,
`SIGNAL_ROUTER_QUEUE_CAPACITY`, `SIGNAL_ROUTER_TIMEOUT_SECONDS`,
`SIGNAL_ROUTER_MAX_RETRIES`, `SIGNAL_ROUTER_DEDUP_CAPACITY`, and
`SIGNAL_ROUTER_AUDIT_PATH`. `SIMULATOR_IPC_ENABLED=false` is the global
fail-safe switch above every per-strategy flag, and
`SIMULATOR_IPC_MAX_RETRIES` controls short delivery retries using the same
idempotent `signal_id`. Restart the central router after changing a routing
flag so the new immutable policy is loaded.

## Project Layout

```text
app/
  api/               FastAPI entrypoint and routes
  analytics/         PCR, skew, gamma exposure, market regime modules
  backtesting/       Future replay engine
  broker/            Broker interface and Angle One implementation
  core/              Settings, logging, application lifecycle
  domain/            Broker-neutral trading/domain models
  execution/         Future order, risk, and kill-switch controls
  greeks/            IV and Greeks calculation
  instruments/       Instrument master, expiry, strike/token resolver
  marketdata/        WebSocket ingestion and tick normalization
  news/              Future crawler and sentiment pipeline
  optionchain/       ATM selection and option-chain state
  signals/           Signal scoring and alert rules
  storage/           Redis/Postgres/Timescale interfaces
  workers/           Long-running background services
tests/
```

## Quick Start

```powershell
python -m unittest discover -s tests
```

Install runtime dependencies before starting the API or broker worker:

```powershell
pip install -r requirements.txt
```

Create `.env` from `.env.example` and add broker credentials only on the machine where the worker runs.

## EOD Phase-1 Feature Research

Run the completed schema-v4 broker tape through the approved quantitative
feature experiments:

```powershell
python -m dummy_broker_replay.run_phase1_feature_research E:\Option_Trade\data\broker_replay_tape_2026-07-29.jsonl
```

Optionally attach one or more analytics trace/stress files for provenance:

```powershell
python -m dummy_broker_replay.run_phase1_feature_research E:\Option_Trade\data\broker_replay_tape_2026-07-29.jsonl --analytics-trace E:\Option_Trade\data\analytics_engine_trace_20260729_092618_IST_bd3faa83.jsonl
```

Directional features run independently. Context, confirmation, and ATR
normalization features run as paired on/off ablations against one shared
`premium_response + futures_flow` baseline. The command enables only
`DERIVATIVES_QUANT` and `GAMMA_EXPANSION`, keeps price-action features
disabled, and scores paper entries with a 5% stop, 10% target, and configurable
replay-only transaction cost (default 0.20% of option premium per completed
round trip). Outputs include input coverage, strategy attribution, gross
results, cost-adjusted results, and paired-baseline deltas. Generated research
profiles remain inside the run directory; production strategy configuration is
not changed.

## EOD Phase-2 Combination Research

After Phase 1, replay the completed schema-v4 tape through the seven approved
quantitative feature combinations:

```powershell
python -m dummy_broker_replay.run_phase2_combination_research E:\Option_Trade\data\broker_replay_tape_2026-07-29.jsonl
```

When a Phase-1 summary exists, attach it as provenance:

```powershell
python -m dummy_broker_replay.run_phase2_combination_research E:\Option_Trade\data\broker_replay_tape_2026-07-29.jsonl --phase1-summary E:\Option_Trade\data\phase1_summary.json
```

The command tests logical flow, positioning, volatility, gamma, cross-market,
and full-ensemble combinations. It keeps price-action strategies and features
disabled, uses only `DERIVATIVES_QUANT` and `GAMMA_EXPANSION`, and preserves the
5% stop, 10% target, and 15-minute maximum hold. Outputs include
`phase2_summary.json`, CSV and text reports, one
`broker_tape_<combination>.jsonl` per experiment, and the exact generated
profiles. A combination receives a same-tape diagnostic rank only after meeting
`--minimum-ranking-trades`; ranking never changes the live configuration.

## Daily Quant Research Dashboard

Run Phase 1, all seven Phase-2 combinations, and the consolidated dashboard
with one PowerShell command:

```powershell
.\scripts\run_eod_quant_research.ps1 -BrokerTapeFolder E:\Option_Trade\data
```

Analytics stress/trace files can be attached for provenance:

```powershell
.\scripts\run_eod_quant_research.ps1 -BrokerTapeFolder E:\Option_Trade\data -AnalyticsTrace E:\Option_Trade\data\analytics_engine_stress_2026-07-30.jsonl
```

The script recursively finds every `broker_replay_tape*.jsonl` below the
folder, sorts them by full path, and replays them one by one. This supports
date subfolders such as `data/29072026`. A missing or invalid tape stops the
batch so incomplete evidence is not silently ignored.

By default, reports are written below
`dummy_broker_replay/runs/eod_quant_research/YYYY-MM-DD/run_<unique-id>`.
Each run contains separate Phase-1 and Phase-2 evidence for every tape plus one
consolidated HTML dashboard, JSON manifest, batch CSV files, and a rolling
14-day combination CSV. Rerunning on the same date preserves the earlier run.

The rolling dashboard deduplicates repeated analysis of the same source tape
and counts unique market dates rather than tape segments. By default, a
combination needs at least eight trading days and 30 completed trades. Only a
cost-adjusted profitable setup can receive a rolling rank or appear as the
research leader. The dashboard also shows actual strategy-source signals and
feature-data coverage. It is research evidence, not an automatic live-strategy
selection.

## Implementation Phases

1. Foundation: config, logging, domain models, tests.
2. Instrument master: download/load, expiry selection, token lookup.
3. Broker gateway: SmartAPI auth, feed token, WebSocket lifecycle.
4. Option-chain MVP: ATM detection, strike window, normalized chain snapshots.
5. Storage: Redis latest state, Timescale/Postgres persistence.
6. Greeks/IV and analytics: PCR, skew, GEX, straddle, directional score.
7. Alerts and paper trading.
8. News crawler, sentiment, and later controlled execution.
