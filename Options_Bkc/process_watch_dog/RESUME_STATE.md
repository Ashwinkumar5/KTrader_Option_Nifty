# Process Watchdog Resume State

Saved: 2026-08-10 (Asia/Calcutta), latest checkpoint

## Current status

- Standalone watchdog implementation is complete under `process_watch_dog`.
- No existing bot source file was changed for this work.
- `watchdog_config.json` reads the existing `config/strategy_config.json` as
  read-only input.
- The current configuration expands to seven enabled profile/strategy child
  processes.
- Each profile in `config/strategy_config.json` has a watchdog-specific
  `watchdog_enable: "Y" | "N"` switch. Only `Y` profiles are expanded.
- All six current profiles are set to `watchdog_enable: "Y"`, producing seven
  managed processes. Change individual profiles to `"N"` and restart the
  watchdog to exclude them.
- The watchdog has not been launched against the live broker yet.

## Implemented behavior

- Only commands declared under `run_process` can be launched or monitored.
- One fresh operating-system process is created for each resolved
  `(profile, enabled strategy)` pair.
- Non-zero exits and configured fatal broker-output patterns trigger restart
  with a new PID.
- Intentional stop does not restart.
- Exponential restart backoff and crash-loop protection are enabled.
- Registered processes are supervised independently.
- Optional heartbeat-file and output-idle health checks are supported.
- Graceful shutdown is attempted before complete process-tree termination.
- A watchdog instance lock prevents duplicate supervisors.
- Runtime status, separate process logs, log rotation, and local lifecycle
  control commands are available.
- The foreground console is a fixed dashboard that clears and redraws every
  two seconds with PID, state, uptime, output age, restart count, latest bot
  message, and latest lifecycle event. Full output remains in log files.

## Validation

- Configuration validation: passed.
- Expanded process count: 7.
- Automated watchdog tests: 16 passed, 0 failed.
- All six current profiles load successfully through the existing
  `app.core.strategy_config` loader with the new flag present.
- Covered cases include profile inheritance, enabled-strategy expansion,
  invalid configuration, fatal broker-output restart with a new PID,
  intentional stop, crash-loop protection, independent supervision,
  heartbeat timeout, descendant cleanup, control commands, and duplicate
  watchdog locking, plus visible console start/output/status reporting.
- Tests used fixture child processes only and did not connect to the broker.
- The broader existing strategy-config suite still contains eight unrelated
  failures for historical profile names already absent from the current JSON.

## Desktop launcher

- Batch file:
  `C:\Users\Administrator\Desktop\Start Process Watchdog.bat`
- Custom-icon shortcut:
  `C:\Users\Administrator\Desktop\Process Watchdog.lnk`
- Project icon:
  `E:\Option_Trade\Options\process_watch_dog\assets\watchdog.ico`

Use the `Process Watchdog` Desktop shortcut for normal startup. Starting it
will launch all seven processes currently enabled by `watchdog_config.json`.

## Resume checklist

1. Review `watchdog_config.json`, especially `profiles`, restart limits, and
   fatal-output patterns.
2. If all seven processes should not start, restrict `profiles` or disable the
   relevant `run_process` entries before launch.
3. Start the Desktop shortcut during a controlled broker/sandbox window.
4. In another terminal, run:
   `myenv\Scripts\python.exe -m process_watch_dog status`
5. Confirm fresh PIDs, per-process logs, and `runtime/state.json`.
6. Simulate or observe one safe disconnection and verify a new PID is created
   without affecting the other managed bots.

## Known boundary

Because the existing bot source was intentionally untouched, a completely
silent broker disconnection is detectable only if the bot exits, emits a
configured fatal line, stops producing expected output when an output-idle
timeout is configured, or updates an authoritative heartbeat file that later
becomes stale.
