# July 22 Replay Results

## Source

```text
E:\Option_Trade\data\microstructure_events_2026-07-22.jsonl
```

The replay was executed with the current production analytics, microstructure,
signal-gate, and strike-selection code. Broker login and network access were not
used. Signal publication remained in shadow mode.

## Capture Audit

```text
Market events:                    107,950
Gate/option-chain frames:           1,145
Quote rows:                        20,610
Quote rows with Greeks:            20,610
Unique stored option contracts:        24
Timestamp regressions:              1,139
Largest timestamp regression:       4,608.189 seconds
Stored qualified decisions:                 0
```

Eight contracts stored as NIFTY contracts have FINNIFTY trading symbols. The
dummy broker applies the current production symbol-boundary rule and excludes
them. No replacement NIFTY prices are manufactured.

## Event-Time Result

Run directory:

```text
dummy_broker_replay\runs\
microstructure_events_2026-07-22_event-time_full-current-v2
```

```text
Market events decoded:             105,328
Microstructure candidates:              97
Frames processed:                     1,145
Raw BUY_PUT candidates:                  54
Raw NEUTRAL frames:                   1,091
Gamma candidates:                         0
Strong signals:                           0
```

Directional rejection breakdown:

```text
Stale microstructure:                   47
Microstructure conflict:                 3
No fresh confirmation:                   2
Insufficient confirmations:              2
```

## Faithful File-Order Result

Run directory:

```text
dummy_broker_replay\runs\
microstructure_events_2026-07-22_faithful_full-current
```

```text
Market events decoded:             105,328
Microstructure candidates:              97
Frames processed:                     1,145
Raw BUY_PUT candidates:                  54
Raw NEUTRAL frames:                   1,091
Gamma candidates:                         0
Strong signals:                           0
```

Directional rejection breakdown:

```text
Stale microstructure:                   52
No fresh confirmation:                   2
```

## Conclusion

The replay mechanism successfully reconstructs recorded option quotes and Greeks,
regenerates microstructure events from raw depth payloads, and passes populated
snapshots through the current option-bot calculations.

The current code did not produce a strong signal from this capture in either
mode. This is a valid strategy result, not a replay failure.

The session cannot conclusively validate the improved Gamma qualification path:
after applying the current chain-identity rule, the recorded data produces no
Gamma candidates. Because eight contaminated contracts displaced genuine NIFTY
contracts, the missing NIFTY quote history cannot be recovered from this file.

Gamma gate behavior remains covered by the controlled unit test:

```text
test_signal_gate.SignalGateTests.
test_qualifies_intrarange_gamma_put_with_room_to_support
```

A clean new live shadow capture, produced with the corrected instrument parser,
is required for a conclusive historical Gamma replay.
