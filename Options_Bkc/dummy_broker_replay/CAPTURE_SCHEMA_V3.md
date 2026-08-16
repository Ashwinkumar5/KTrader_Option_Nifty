# Broker Replay Tape — Capture Schema v3

> Legacy reference. New captures use
> [CAPTURE_SCHEMA_V4.md](CAPTURE_SCHEMA_V4.md).

## Goal

Starting with the next live shadow session, one daily JSONL file contains enough
information to reconstruct broker inputs without Angel One access:

```text
broker_replay_tape_YYYY-MM-DD.jsonl
```

The file remains research-only and contains no order execution records.

## Record Types

### `session_manifest`

Written once per worker start:

```text
session_id
capture schema/version
UTC and market timezone
code revision
sanitized effective settings
```

API keys, client code, password, TOTP secret, Redis URL, and database URL are
explicitly excluded.

### `instrument_master`

Contains:

```text
spot/index tokens
nearest selected expiry per underlying
all option contracts for the selected expiry
```

This permits offline reconstruction of ATM windows without downloading the
broker's master file.

### `subscription_change`

Records initial spot subscriptions and every dynamically added ATM-window option
subscription, including spot and ATM at the time of the change.

### `market_event`

Records every received spot and option tick:

```text
event_role = spot or option
normalized MarketTick
original raw broker payload
normalized microstructure features when applicable
microstructure candidate when applicable
```

Raw best-five option depth remains available for deterministic microstructure
recalculation.

### `gate_decision`

Records the normalized option-chain snapshot, current analytics, final shadow gate
decision, and frame provenance:

```text
scheduled_for
frame_started_at
frame_completed_at
trigger_tick_received_at
schedule_lag_ms
configured_interval_ms
spot value/source/timestamp/age
expected and selected option contracts
quote and Greek token coverage
quote request/response timing
Greek request/response timing
VALID or INVALID_DATA status and reasons
```

For the current configured default `OPTION_WINDOW_EACH_SIDE=4`, a strict valid
frame requires:

```text
(2 x OPTION_WINDOW_EACH_SIDE + 1) strikes x CE/PE
18 selected contracts when OPTION_WINDOW_EACH_SIDE=4
the same configured count of quotes with LTP
the same configured count of Greek rows when Greeks are enabled
```

## Monday Configuration

Ensure these values are present in `.env`:

```text
LOCAL_STORAGE_DIR=E:\Option_Trade\data
BROKER_NAME=angleone
# Optional override; blank uses app.broker.<BROKER_NAME>.provider
BROKER_ADAPTER_MODULE=
MARKET_TIMEZONE=Asia/Kolkata
SNAPSHOT_INTERVAL_MS=15000
OPTION_WINDOW_EACH_SIDE=4
OPTION_GREEKS_ENABLED=true
MICROSTRUCTURE_ENABLED=true
MICROSTRUCTURE_MODE=shadow
REPLAY_CAPTURE_ENABLED=true
REPLAY_CAPTURE_FILE_PREFIX=broker_replay_tape
REPLAY_REQUIRE_COMPLETE_WINDOW=true
```

These values are examples of the current runtime configuration, not simulator
constants. The manifest persists the effective values, and validation derives the
required contract count from `OPTION_WINDOW_EACH_SIDE`.

Do not paste credentials into logs, commands, or replay documentation.

## Capture

From `E:\Option_Trade\Options`:

```powershell
python scripts\run_worker.py
```

Expected output file:

```text
E:\Option_Trade\data\broker_replay_tape_YYYY-MM-DD.jsonl
```

## After-Market Validation

```powershell
python -m dummy_broker_replay.validate_tape `
  E:\Option_Trade\data\broker_replay_tape_YYYY-MM-DD.jsonl
```

The validator calculates SHA256, reports all record counts and frame statuses,
checks for cross-underlying contamination, verifies secret-setting keys are
absent, and requires at least one complete strict 18-contract frame.

Only a tape ending with:

```text
VALIDATION PASSED: tape is ready for strict offline broker simulation.
```

should be treated as conclusive broker-simulation input.

## Multiple Worker Starts

The daily file is append-only. Restarting the worker writes a new
`session_manifest` with a new `session_id` into the same file. The simulator and
analyzer must keep session state separated by `session_id`.
