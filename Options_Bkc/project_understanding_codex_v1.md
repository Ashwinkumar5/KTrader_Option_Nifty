# Project Understanding: Options Analytics Platform

## Purpose and Current Boundary

This repository is a Python 3.11 backend for live NIFTY and BANKNIFTY option-chain collection and analytics, using Angel One SmartAPI. Its current runnable product is a market-data worker, not an autonomous trading system: it authenticates, subscribes to market data, builds a moving ATM option window, calculates analytics/signals, and persists the results. `app/execution/` and `app/paper_trader/` are placeholders, so there is no implemented order-management, position-management, risk, or kill-switch path.

Primary entry point: `scripts/run_worker.py`.

## Runtime Flow

```text
scripts/run_worker.py
  -> load Settings from .env and configure logging
  -> run_market_data_worker()
  -> Angel One login + instrument-master load
  -> build broker-neutral contracts/tokens
  -> WebSocket subscribe to NIFTY/BANKNIFTY spot tokens
  -> on each spot tick: select nearest expiry and ATM +/- configured strikes
  -> subscribe newly required CE/PE contracts
  -> store each normalized tick
  -> on snapshot interval: REST quote refresh + optional Greeks refresh
  -> OptionChainState builds a snapshot
  -> AnalyticsEngine emits a directional signal and levels
  -> persist history and publish latest chain/analytics state
```

The worker's default universe is `NIFTY,BANKNIFTY`; its default window is four strikes on either side of ATM (nine strikes, CE and PE where available). It chooses the first available expiry, which is normally the nearest expiry because expiries are sorted.

## Directory Map

| Path | Role | Current status |
| --- | --- | --- |
| `scripts/` | Operational commands: worker runner, storage query, broker scaffold. | Active |
| `app/core/` | Immutable environment-backed settings and logging setup. | Active |
| `app/domain/` | Broker-neutral dataclasses: tokens, contracts, ticks, option-chain and analytics snapshots. | Architectural core |
| `app/broker/angleone/` | SmartAPI login, instrument parsing, WebSocket feed, endpoint/mode mappings. | Active broker adapter |
| `app/instruments/` | Expiry lookup, strike normalization, contract lookup. | Active |
| `app/marketdata/` | Raw broker-payload to `MarketTick` normalization. | Active |
| `app/optionchain/` | ATM/window selection and in-memory latest-tick/Greeks state. | Active |
| `app/greeks/` | Broker Greeks normalization and optimal-strike selection. | Active, broker-driven Greeks |
| `app/analytics/` | PCR/OI/volume, IV/skew, gamma-spring and strike-selection signal logic. | Active; iterative versions retained |
| `app/signals/` | PCR rule and terminal display formatting. | Active |
| `app/storage/` | JSONL, SQLite, and Redis implementations behind storage protocols. | Active |
| `app/api/` | Minimal FastAPI health endpoint. | Skeleton |
| `app/backtesting/` | Recorder/replayer utility and observations. | Partial/future integration |
| `app/execution/`, `app/paper_trader/`, `app/news/` | Intended execution, simulation, and news components. | Not implemented |
| `tests/` | `unittest` coverage for adapter, normalizer, chain, signal, storage-facing helpers, and analytics. | Present but currently not green |

## Design Assessment

The cleanest architectural decision is the broker-neutral domain layer. The Angel One adapter maps external data into `InstrumentToken`, `OptionContract`, `MarketTick`, and snapshot models before the option-chain and analytics modules consume it. Storage also uses protocols, enabling JSONL for local capture, SQLite for local durable state, and Redis for live state. This is a good base for adding brokers and a later execution service without coupling strategy code to SmartAPI payloads.

`run_market_data_worker()` supports dependency injection for the broker client, feed, and stores, plus `max_ticks` for bounded runs. That is the right testability seam, although the current tests focus mainly on individual units rather than an end-to-end simulated worker run.

## Analytics and Signal Logic

The active `app/analytics/engine.py` is stateful per worker process. It combines:

- PCR from a selected ITM/ATM chain subset, support/resistance from OI,
- volume-to-OI and localized PCR divergence checks,
- straddle-based morning range capture after 09:45,
- IV rank, vega-trap, and IV-skew vetoes,
- a rolling gamma-spring detector, and
- selection of a liquid, approximately 50-delta target option.

It outputs `BUY_CALL`, `BUY_PUT`, or `NEUTRAL`, with the reason stored in `AnalyticsSnapshot`. It does not submit an order. Treat these signals as research outputs until they have been replay-tested with realistic quote, slippage, latency, and expiry-day data.

## Storage and Operations

`STORAGE_BACKEND=sqlite` uses `data/store.db`. `jsonl` writes tick and chain history under `LOCAL_STORAGE_DIR`. With an unset backend, the worker attempts Redis when `REDIS_URL` is configured, then falls back to SQLite or JSONL if Redis is unavailable. The API currently exposes only `GET /health`; `scripts/query_storage.py` is the local inspection path.

Secrets are expected in `.env` and should remain outside source control. The worker checks that all four Angel One credentials exist before starting.

## Validation Baseline (2026-07-20)

`python -m unittest discover -s tests -v` ran 25 tests: 20 passed, 3 failed, and 2 errored. Do not promote this to trading or paper trading until these are resolved and the full suite is green.

1. `app/marketdata/normalizer.py` turns `exchange_timestamp` into an IST time string; `MarketTick` and its test expect a `datetime`.
2. `app/signals/pcr.py` now requires keyword-only `spot_price`, but its existing test calls the older interface.
3. The active analytics engine's PCR/window behavior differs from three historical expectations in `tests/test_analytics_engine.py`.

## Trading Readiness Priorities

Before connecting this project to live orders, add a deterministic execution boundary separate from the analytics engine: order intent schema, position and order state, broker acknowledgements, idempotency, rejected-order handling, reconciliation, and a manual/global kill switch. Enforce maximum position, loss, order-rate, and stale-data limits in code outside the signal generator. First stabilize timestamps and test contracts, then build replayable data capture and paper trading; only after measured out-of-sample performance should controlled live execution be considered.

## Suggested Commands

```powershell
python -m unittest discover -s tests -v
python scripts/run_worker.py
python scripts/query_storage.py --help
```
