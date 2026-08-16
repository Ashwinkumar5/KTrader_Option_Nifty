# Options Platform Overview v1

## Purpose

This repository is a Python backend for live NIFTY and BANKNIFTY option-chain collection and analytics using Angel One SmartAPI.

The current runnable product is a market-data and analytics worker. It logs in to Angel One, subscribes to live market data, builds an ATM option window, refreshes quotes and Greeks, calculates analytics/signals, and persists snapshots.

It is not currently a live trading bot. There is no implemented order placement, position management, risk management, reconciliation, or kill-switch path connected to the worker.

Primary entry point:

```text
scripts/run_worker.py
```

## High-Level Runtime Flow

```text
scripts/run_worker.py
  -> load Settings from .env
  -> configure logging
  -> validate Angel One credentials
  -> run_market_data_worker()
  -> Angel One login
  -> load instrument master
  -> build broker-neutral InstrumentMaster
  -> connect WebSocket feed
  -> subscribe NIFTY/BANKNIFTY spot tokens
  -> receive spot ticks
  -> calculate ATM and option window
  -> subscribe required CE/PE option tokens
  -> save every tick
  -> periodically refresh option quotes by REST
  -> optionally refresh broker Greeks
  -> build OptionChainSnapshot
  -> run AnalyticsEngine
  -> run SignalGate
  -> print signal line
  -> persist chain and analytics state
```

## Entrypoint Walkthrough

File:

```text
scripts/run_worker.py
```

Flow:

1. Adds repository root to `sys.path` so `app` imports work.
2. Calls `load_settings()` from `app/core/config.py`.
3. Calls `configure_logging()` from `app/core/logging.py`.
4. Prints runtime configuration:
   - broker name
   - default underlyings
   - option window size
   - snapshot interval
   - microstructure mode
5. Checks `settings.broker_credentials_configured`.
6. If credentials are missing, exits before any broker connection.
7. Calls:

```python
await run_market_data_worker(settings=settings)
```

## Settings

File:

```text
app/core/config.py
```

Settings are loaded from `.env` using `python-dotenv`.

Important settings:

```text
ANGLEONE_API_KEY
ANGLEONE_CLIENT_CODE
ANGLEONE_PASSWORD
ANGLEONE_TOTP_SECRET
ANGLEONE_INSTRUMENT_MASTER_URL
ANGLEONE_INSTRUMENT_MASTER_PATH
DEFAULT_UNDERLYINGS
OPTION_WINDOW_EACH_SIDE
SNAPSHOT_INTERVAL_MS
STORAGE_BACKEND
REDIS_URL
LOCAL_STORAGE_DIR
MARKET_DATA_WS_MODE
OPTION_GREEKS_ENABLED
MICROSTRUCTURE_ENABLED
MICROSTRUCTURE_MODE
SIGNAL_GATE_MIN_CONFIRMATIONS
SIGNAL_GATE_COOLDOWN_SECONDS
SIGNAL_GATE_LEVEL_DISTANCE_POINTS
SIGNAL_GATE_MIN_MICRO_CONFIDENCE
SIGNAL_GATE_MIN_SCORE
SIGNAL_GATE_STRADDLE_ZONE_RATIO
SIGNAL_GATE_MIN_RANGE_ROOM_POINTS
```

Defaults:

```text
DEFAULT_UNDERLYINGS=NIFTY,BANKNIFTY
OPTION_WINDOW_EACH_SIDE=4
SNAPSHOT_INTERVAL_MS=1000
MARKET_DATA_WS_MODE=SNAP_QUOTE
OPTION_GREEKS_ENABLED=true
MICROSTRUCTURE_ENABLED=true
MICROSTRUCTURE_MODE=shadow
```

## Worker Orchestration

File:

```text
app/workers/market_data_worker.py
```

Main function:

```python
run_market_data_worker(...)
```

The function supports dependency injection for:

```text
client
feed
tick_store
chain_store
live_store
max_ticks
```

This makes it possible to test the worker with fake broker clients, fake feeds, fake stores, and bounded tick counts.

## Storage Selection

The worker chooses storage early.

If `STORAGE_BACKEND=sqlite`:

```text
data/store.db
```

Tables:

```text
ticks
chain_snapshots
live_chain
live_analytics
```

If `STORAGE_BACKEND=jsonl`:

```text
data/ticks.jsonl
data/option_chain_snapshots.jsonl
```

If storage backend is unset:

1. Try Redis when `REDIS_URL` exists.
2. If Redis fails, try SQLite.
3. If SQLite fails, fall back to JSONL.

Storage modules:

```text
app/storage/sqlite_store.py
app/storage/local.py
app/storage/redis_store.py
app/storage/serialization.py
```

## Broker Login and Instrument Master

Broker client:

```text
app/broker/angleone/client.py
```

Login flow:

1. Imports `SmartConnect` and `pyotp`.
2. Generates TOTP from `ANGLEONE_TOTP_SECRET`.
3. Calls `smart_api.generateSession(...)`.
4. Extracts JWT, refresh token, and feed token.
5. Returns broker-neutral `BrokerSession`.

Instrument master flow:

1. If `ANGLEONE_INSTRUMENT_MASTER_PATH` is set, loads local JSON.
2. Otherwise downloads from `ANGLEONE_INSTRUMENT_MASTER_URL`.
3. Returns raw Angel One rows.
4. Worker passes rows to:

```python
build_instrument_master(raw_master, underlyings=settings.default_underlyings)
```

Instrument conversion:

```text
app/broker/angleone/instruments.py
```

It converts broker rows into:

```text
InstrumentToken
OptionContract
InstrumentMaster
```

The project tries to keep broker payloads isolated in the broker adapter layer.

## Domain Layer

File:

```text
app/domain/models.py
```

Important dataclasses:

```text
InstrumentToken
OptionContract
MarketTick
GreeksSnapshot
OptionQuote
OptionChainSnapshot
AnalyticsSnapshot
SupportResistanceLevel
MicrostructureFeatures
MicrostructureSignal
```

This is the broker-neutral layer consumed by option-chain, analytics, storage, and signal modules.

## WebSocket Feed

File:

```text
app/broker/angleone/feed.py
```

Flow:

1. Creates `SmartWebSocketV2`.
2. Registers callbacks:
   - `on_data`
   - `on_error`
   - `on_open`
   - `on_close`
3. Starts the WebSocket in a background thread.
4. Waits up to 20 seconds for socket open.
5. Converts incoming broker payloads to `MarketTick`.
6. Pushes ticks into an async queue.
7. Worker consumes ticks through:

```python
async for tick in feed.ticks():
```

Initial subscription:

```python
await feed.subscribe(master.spot_tokens.values())
```

Only spot/index tokens are subscribed first. Option tokens are subscribed later after spot price is known.

## Tick Normalization

File:

```text
app/marketdata/normalizer.py
```

Function:

```python
normalize_tick(...)
```

It maps broker payload fields into `MarketTick`:

```text
ltp
open_price
high_price
low_price
close_price
oi
oi_change
oi_change_percent
volume
bid
ask
exchange_timestamp
received_at
raw
```

Important current behavior:

```text
exchange_timestamp is kept as a timezone-aware datetime.
```

This is important for event ordering and replay.

## Spot Price and ATM Window

Worker maintains:

```python
spot_prices: dict[str, Decimal]
subscribed_option_tokens: set[str]
```

When a spot tick arrives:

```python
_update_spot_price(...)
```

Then for each underlying with a known spot price:

1. Find nearest expiry from `available_expiries(...)`.
2. Select ATM option window:

```python
select_option_window(...)
```

Files:

```text
app/instruments/master.py
app/optionchain/atm.py
```

Strike intervals:

```text
NIFTY = 50
BANKNIFTY = 100
```

Default window:

```text
ATM +/- 4 strikes
```

For each strike, it tries to include both:

```text
CE
PE
```

New option tokens are subscribed dynamically.

## Option Chain State

File:

```text
app/optionchain/state.py
```

Class:

```python
OptionChainState
```

Responsibilities:

1. Keep latest tick by token.
2. Keep latest Greeks by token.
3. Build `OptionChainSnapshot`.

Snapshot contains:

```text
underlying
expiry
spot_price
atm_strike
captured_at
quotes
```

Each quote is an `OptionQuote` built from latest tick data and latest Greeks.

## Snapshot Refresh

Function:

```python
_refresh_chain_snapshot(...)
```

File:

```text
app/workers/market_data_worker.py
```

Every configured snapshot interval, the worker:

1. Calls `_refresh_option_quotes(...)`.
2. Optionally calls `_fetch_greeks(...)`.
3. Updates `OptionChainState`.
4. Builds `OptionChainSnapshot`.
5. Runs analytics.
6. Runs signal gate.
7. Prints signal line.
8. Records gate decision if microstructure recorder exists.
9. Saves chain snapshot.
10. Publishes latest chain snapshot.
11. Publishes latest analytics snapshot.
12. Returns latest PCR for short PCR history.

## REST Quote Refresh

Function:

```python
_refresh_option_quotes(...)
```

It groups selected contracts by exchange and calls:

```python
client.market_quote(mode="FULL", exchange_tokens=exchange_tokens)
```

Then it normalizes returned quote payloads into `MarketTick` and updates `OptionChainState`.

This means snapshots are not based only on WebSocket ticks. The worker also refreshes selected option contracts by REST.

## Greeks Flow

Files:

```text
app/greeks/broker.py
app/greeks/strike_selector.py
```

If `OPTION_GREEKS_ENABLED=true`, the worker calls:

```python
client.option_greeks(option_greek_params(...))
```

`normalize_broker_greeks(...)` maps broker rows to `GreeksSnapshot` by:

1. Trading symbol match.
2. Fallback strike and option type match.

The analytics engine uses Greeks for:

```text
IV checks
IV skew
delta-based strike selection
```

`OptimalStrikeSelector` tries to select a liquid strike near 50 delta when a directional signal exists.

Current minimum volume threshold:

```text
5000
```

## Analytics Engine

File:

```text
app/analytics/engine.py
```

Class:

```python
AnalyticsEngine
```

Main method:

```python
from_chain(snapshot)
```

It calculates:

```text
put/call OI
put/call OI change
put/call volume
PCR
ATM straddle price
support levels
resistance levels
strike-level PCR
volume/OI ratios
ATM IV
active-zone PCR
```

Signal logic includes:

```text
PCR regime signal
local active-zone divergence
chain velocity sweep checks
morning straddle boundary checks
breakout/breakdown validation
support/resistance exhaustion checks
IV rank filter
vega trap filter
IV skew veto
gamma spring / gamma blast detector
smart money divergence note
optimal strike targeting
```

Returns:

```text
AnalyticsSnapshot
```

Possible raw signals:

```text
BUY_CALL
BUY_PUT
NEUTRAL
```

## Signal Gate

File:

```text
app/signals/gate.py
```

Class:

```python
SignalGate
```

The signal gate is separate from analytics. Analytics now emits a structured
setup type:

```text
BREAKOUT
LEVEL_REVERSAL
RANGE_ROTATION
MOMENTUM_EXPANSION
```

It validates:

```text
candidate is directional
instrument-chain identity is clean
analytics and chain timestamps match
setup-specific structural conditions pass
multiple fresh microstructure events confirm the same side
no fresh opposite microstructure signal exists
confidence score reaches the configured threshold
cooldown is not active
```

Support/resistance is strict for breakouts and level reversals. Range rotation
and momentum expansion may qualify away from a level when sufficient room
remains before the next opposing level.

Critical safety behavior:

```python
published = "NEUTRAL" if self._settings.mode == "shadow" else raw_signal if qualified else "NEUTRAL"
```

Default mode is:

```text
shadow
```

So even if analytics produces `BUY_CALL` or `BUY_PUT`, the published signal is forced to `NEUTRAL` in default runtime.

This keeps the system research-only.

Qualified research output is exposed separately as `STRONG=BUY_CALL` or
`STRONG=BUY_PUT`. Rejected candidates are persisted for replay but are not
printed as terminal signal lines.

## Microstructure Flow

File:

```text
app/microstructure/engine.py
```

If enabled, every option tick is observed by `MicrostructureEngine`.

It measures:

```text
order book imbalance
bid depth
ask depth
spread
premium velocity
event count
```

It emits `MicrostructureSignal` only when:

```text
option tick has LTP
complete order book exists
enough events are present
depth imbalance is high enough
premium velocity is high enough
spread is small enough
direction persists across events
```

Microstructure signals are used by `SignalGate`, not by broker execution.

Recorder:

```text
app/storage/microstructure_recorder.py
```

Output:

```text
data/microstructure_events_YYYY-MM-DD.jsonl
```

## Signal Display

File:

```text
app/signals/display.py
```

Function:

```python
format_signal_line(...)
```

Printed line includes:

```text
underlying
ATM
CE OI total
PE OI total
PCR
published signal
signal reason
support
resistance
straddle
```

Because gate default is shadow mode, printed signal usually remains:

```text
SIGNAL=NEUTRAL
```

even when the reason records a qualified or rejected directional setup.

## Current Directory Map

```text
scripts/
  run_worker.py              Main worker command
  query_storage.py           Local storage inspection

app/core/
  config.py                  Environment settings
  logging.py                 Logging setup

app/domain/
  models.py                  Broker-neutral dataclasses

app/broker/
  interfaces.py              Broker/feed protocols
  angleone/
    client.py                SmartAPI login and REST calls
    feed.py                  SmartWebSocketV2 wrapper
    instruments.py           Broker instrument rows -> domain contracts
    data_map.py              WebSocket mode mappings

app/instruments/
  master.py                  InstrumentMaster and expiry/strike helpers

app/marketdata/
  normalizer.py              Broker tick payload -> MarketTick
  depth_normalizer.py        Depth payload -> normalized order book

app/optionchain/
  atm.py                     ATM and strike-window selection
  state.py                   Latest tick/Greeks state -> chain snapshot
  memory_state.py            Gamma spring memory helpers

app/analytics/
  engine.py                  Active analytics engine
  iv_strategy.py             IV rank, skew, trap checks
  engine_v1.py ...           Older retained versions

app/greeks/
  broker.py                  Broker Greeks normalization
  strike_selector.py         Liquid 50-delta strike targeting

app/signals/
  pcr.py                     PCR signal rule
  gate.py                    Signal validation and shadow-mode gate
  display.py                 Terminal output formatting

app/microstructure/
  engine.py                  Depth/velocity research signal engine
  velocity.py                Premium velocity tracking

app/storage/
  local.py                   JSONL and in-memory stores
  sqlite_store.py            SQLite stores
  redis_store.py             Redis stores
  serialization.py           Dataclass JSON conversion
  microstructure_recorder.py Microstructure and gate-decision JSONL

app/api/
  main.py                    Minimal FastAPI skeleton

app/backtesting/
  data_recorder.py.py        Partial recorder/replay area
  microstructure_replay.py   Replay helper
  todays_observation.txt     Notes

app/execution/
  __init__.py                Placeholder

app/paper_trader/
  Not implemented / absent in current active flow

app/news/
  __init__.py                Placeholder

tests/
  unittest coverage

data/
  store.db
  microstructure_events_*.jsonl
  observation text files

logs/
  daily app logs
```

## Restart Mental Model

When restarting work, think of the system as four layers:

```text
1. Broker ingestion
   Angel One login, instrument master, WebSocket, REST quote refresh

2. State building
   Normalize ticks, maintain latest token state, build ATM option-chain snapshot

3. Research analytics
   PCR, OI, volume, IV, Greeks, gamma, support/resistance, microstructure

4. Publishing/storage
   Print signal line, save tick/snapshot history, publish latest chain/analytics
```

No current path should be treated as live trading execution.

The most important active file for runtime behavior is:

```text
app/workers/market_data_worker.py
```

The most important active file for signal logic is:

```text
app/analytics/engine.py
```

The most important safety file is:

```text
app/signals/gate.py
```

## Current Safety Boundary

This project currently produces analytics and research signals only.

Execution is intentionally absent from the live path. `SignalGate` defaults to `shadow` mode, which forces published output to `NEUTRAL`.

Before any live order placement is added, the project needs a separate execution layer with:

```text
order intent schema
position state
broker acknowledgement handling
idempotency
rejected-order handling
order reconciliation
max position limits
max loss limits
order rate limits
stale data protection
manual/global kill switch
paper-trading validation
replay-tested performance
```

## Useful Commands

```powershell
python scripts/run_worker.py
python scripts/query_storage.py --help
python -m unittest discover -s tests -v
```
