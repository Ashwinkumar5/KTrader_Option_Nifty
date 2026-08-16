# Process Watchdog

This folder contains a standalone process supervisor for the trading bots. It
does not modify or import the bot implementation. The existing
`config/strategy_config.json` is read-only input used to validate each
configured profile and strategy.

## What it launches

Only entries under `run_process` in `watchdog_config.json` are allowed to
create child processes. The supplied production configuration starts these six
explicit processes in order:

1. loopback Core-NATS server;
2. singleton broker-owning market-data feed handler;
3. singleton central signal router;
4. one subscriber-only worker for every enabled strategy in each profile with
   `watchdog_enable: "Y"`.

The processes tolerate simultaneous startup: the NATS clients retry their
initial connection, and the subscriber retries the bootstrap request. After
readiness, a NATS disconnect is fatal because event continuity cannot be
proved. Shutdown happens in reverse order so infrastructure remains available
until its consumers have stopped.

The singleton router is common to every worker and is the only process that
connects to KTrader Simulator. A singleton entry uses `"singleton": true` and
is not expanded through the strategy catalog.

Set a profile's `watchdog_enable` to `"N"` to prevent the watchdog from
creating any process for that profile. This switch affects only the watchdog;
the profile remains available to other bot and research commands. Restart the
watchdog after changing a flag.

With the current strategy configuration this expands to six processes. Run
validation to see the exact list without starting anything:

```powershell
cd E:\Option_Trade\Options
.\myenv\Scripts\python.exe -m process_watch_dog validate
.\myenv\Scripts\python.exe -m process_watch_dog catalog
```

To restrict the watchdog, set `watchdog_enable` to `"N"` on the unwanted
profiles in `config/strategy_config.json`. Setting the generic `run_process`
entry to `"enabled": false` disables the complete watchdog command template.

Commands are arrays, not shell strings. Supported placeholders are
`{project_root}`, `{strategy_config}`, `{profile}`, `{strategy}`,
`{strategy_slug}`, and `{process_id}`. Keeping executable and arguments
separate avoids shell quoting and injection problems.

## Run and control

Start the foreground watchdog using `Run Watchdog.cmd`, or:

```powershell
cd E:\Option_Trade\Options
.\myenv\Scripts\python.exe -m process_watch_dog run
```

Leave that terminal open. From another terminal:

On a new machine, install the pinned standalone NATS server once before the
first launch:

```powershell
.\scripts\install_nats_server.ps1
```

The script verifies the official v2.14.3 release SHA-256 before extraction;
the downloaded `.runtime` directory is intentionally not committed.

From another terminal:

```powershell
.\myenv\Scripts\python.exe -m process_watch_dog status
.\myenv\Scripts\python.exe -m process_watch_dog stop derivatives_only__GAMMA_EXPANSION
.\myenv\Scripts\python.exe -m process_watch_dog start derivatives_only__GAMMA_EXPANSION
.\myenv\Scripts\python.exe -m process_watch_dog restart derivatives_only__GAMMA_EXPANSION
.\myenv\Scripts\python.exe -m process_watch_dog stop-all
.\myenv\Scripts\python.exe -m process_watch_dog start-all
.\myenv\Scripts\python.exe -m process_watch_dog shutdown
```

The watchdog console is a fixed live dashboard. It clears and redraws every
two seconds instead of appending bot logs. For every registered process it
shows current state, PID, uptime, last-output age, restart count, profile,
strategy, latest bot message, and latest lifecycle event. Full child output
continues to be written to the per-process log files.

`console_status_interval_seconds` controls the dashboard refresh interval.
Keep `console_show_child_output` false for the fixed-screen view. Process
liveness is still checked every `poll_interval_seconds`; an exited process is
detected and restarted without waiting for the next screen refresh.

`stop` and `stop-all` are intentional stops and never trigger an automatic
restart. `start`, `restart`, and automatic failure recovery always create a
fresh operating-system process and PID.

## Failure handling

The watchdog restarts a bot when it exits with a non-zero code. It also scans
the bot's unbuffered stdout/stderr for the configured fatal broker patterns.
When a pattern matches, it stops the complete managed process tree and starts
a fresh process after the configured backoff.

Rapid failures use exponential delay and enter `crash_loop` after the restart
limit. A manual `start` clears that crash-loop history. One bot's failure does
not interrupt other registered bots.

For a bot that hangs without exiting or printing an error, configure either:

```json
"heartbeat_file": "path\\to\\bot.heartbeat",
"heartbeat_timeout_seconds": 30
```

or `output_idle_timeout_seconds`. A heartbeat file must be updated by the child
process or another authoritative health probe. Output-idle monitoring should
only be enabled when the process is expected to log regularly, otherwise it
can cause false restarts.

Because the existing bot source is intentionally untouched, the watchdog
cannot identify a completely silent broker disconnection unless the bot exits,
prints a matching error, stops producing expected output, or exposes a
heartbeat. The current worker already exits on surfaced WebSocket errors; the
fatal-output rules provide an additional external recovery path.

## Logs and runtime state

- `logs/watchdog.log`: supervisor lifecycle and restart decisions.
- `logs/central_signal_router.log`: common signal-routing service output.
- `logs/<profile>__<strategy>.log`: captured stdout/stderr for each bot.
- `runtime/state.json`: current PID, status, restart count, and last failure.
- `runtime/watchdog.lock`: prevents duplicate watchdog instances.

Logs rotate by size. On Windows, children are placed in a job object when the
operating system permits it; graceful shutdown is attempted first, followed by
full process-tree termination. The watchdog never attaches to, stops, or
monitors arbitrary processes that it did not create.

## Tests

The tests launch fixture processes only; they never connect to the broker:

```powershell
cd E:\Option_Trade\Options
.\myenv\Scripts\python.exe -m unittest discover -s process_watch_dog\tests -v
```

They cover strategy/profile expansion, configuration rejection, fatal broker
output restart with a new PID, intentional stop, crash-loop protection,
independent supervision, heartbeat timeout, and descendant cleanup.
