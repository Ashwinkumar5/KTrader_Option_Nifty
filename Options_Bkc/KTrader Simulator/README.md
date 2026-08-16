# KTrader Simulator

An isolated Python 3.13 options trading simulator using Dear PyGui. The simulator
may read the existing bot's broker feed and signal output, but its source code and
configuration files are never modified.

## Safety

`BROKER_ORDER_EXECUTION_ENABLED=false` is the default and prevents construction of
the live-order adapter. In this mode every fill, balance change, risk exit, and bot
signal remains inside the simulator. Setting the flag to `true` enables real
AngleOne BUY/SELL requests and must only be done intentionally with the correct
API-key permissions, static IP configuration, and product type.

## Setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ktrader_simulator
```

Copy `.env.example` to `.env` and provide credentials locally. The simulator loads
operating-system values first, its own `.env` second, and the bot `.env` only as a
fallback for missing values.

## Phase status

- Configuration and Dear PyGui dashboard complete
- Read-only AngleOne authentication and instrument discovery complete
- NIFTY, BANKNIFTY, SENSEX, and BANKEX five-strike market snapshots complete
- Selected-strike CE/PE LTP, bid/ask, moneyness, lot totals, and colors complete
- Local Market/Limit execution, balance checks, and maximum-lot sizing complete
- Pending-order and live-position rows, consolidated P&L, and EXIT actions complete
- Percentage target, stop-loss, and trailing-stop exits complete
- Event-driven `KTraderUI` bot BUY intake over local IPC complete
- Crash-safe JSONL trade-ledger recovery complete
- Optional AngleOne order routing and RMS balance refresh complete behind the live flag

## Runtime hot paths

- The main thread owns Dear PyGui. It consumes immutable events, coalesces stale
  snapshots, and writes only widget values that actually changed.
- The runtime thread owns two independent asyncio tasks: the market-data task
  acquires and publishes quotes, while the trading task handles BUY, EXIT,
  cancellation, marking, and risk evaluation from the latest published quotes.
- The local `KTraderUI` socket is handled by the runtime event loop and places
  accepted bot events into a bounded in-memory queue. It uses no Redis, files,
  polling loop, or Dear PyGui callback.
- Blocking calls inside the reused AngleOne SDK wrapper are offloaded to the
  bounded `ktrader-broker-io` pool, configured by `KTRADER_BROKER_IO_WORKERS`.
  The existing bot source is not modified.
- The `ktrader-ledger-writer` thread serializes JSONL writes from a bounded queue,
  so file flush and fsync do not block market data, trading commands, or rendering.
  Queue capacity is configured with `KTRADER_LEDGER_QUEUE_CAPACITY`.

## Execution behavior

- A Market BUY is simulated at the best ask; an exit is simulated at the best bid.
- A Limit BUY remains visible as `PENDING` until the best ask reaches its limit.
- Every pending order and open position has a one-click `EXIT`; pending orders are
  cancelled and their reserved funds are released, while open positions are squared off.
- GUI orders use the entered whole-lot quantity.
- Bot entries use a Market BUY at the current ask in paper-simulator mode and
  size to the maximum whole lots affordable from remaining balance.
- Open-position marks outside the visible five-strike window are still polled.
- Target, stop-loss, and trailing-stop values are percentages; zero disables a rule.
- State is recovered from `KTRADER_TRADE_LEDGER_PATH` after a clean or interrupted exit.

## Bot event input

Start the simulator. In a second terminal, start the bot's one shared signal
router:

```powershell
cd E:\Option_Trade\Options
.\myenv\Scripts\python.exe .\scripts\run_signal_router.py
```

Then launch the bot through the IPC runner from the Simulator directory:

```powershell
.\.venv\Scripts\python.exe .\scripts\run_bot_with_ipc.py
```

The router must be running before a strategy can send an entry. The local event
contains `signal_id`, `profile`, `strategy`, `underlying`, `strike`, `side`,
`action=BUY`, and its timestamp. The simulator resolves the current
active-expiry contract and submits a paper Market BUY at the latest ask. The
simulator owns and records target, stop-loss, trailing-stop, manual exits, and
P&L.

To inject one event into an already-running GUI without starting the bot:

```powershell
.\.venv\Scripts\python.exe .\scripts\send_offline_bot_order.py `
  --underlying NIFTY --strike 24400 --side CALL
```

The sender refuses to run unless `BROKER_ORDER_EXECUTION_ENABLED=false`.

## Read-only market verification

```powershell
.\.venv\Scripts\python.exe -m ktrader_simulator.diagnostics --index NIFTY
```

The diagnostic authenticates, loads instruments, and requests quotes. It cannot
place, modify, or cancel orders, regardless of the live-order flag.

## Verification

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy --strict src tests
.\.venv\Scripts\python.exe -m pytest
```
