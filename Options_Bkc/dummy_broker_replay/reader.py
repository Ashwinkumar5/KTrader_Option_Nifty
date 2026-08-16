from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Literal

from .serde import contract_matches_underlying, parse_datetime


ReplayMode = Literal["faithful", "event-time"]


@dataclass(frozen=True)
class SessionAudit:
    source_path: Path
    market_events: int
    gate_frames: int
    source_qualified: int
    timestamp_regressions: int
    maximum_regression_seconds: float
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    unique_contracts: tuple[dict[str, object], ...]
    excluded_contaminated_contracts: int
    quotes: int
    quotes_with_greeks: int
    market_spot_events: int
    market_future_events: int
    capture_configuration: dict[str, object]
    spot_tokens: tuple[dict[str, object], ...]
    future_contracts: tuple[dict[str, object], ...]

    @property
    def underlyings(self) -> tuple[str, ...]:
        return tuple(
            sorted({str(item["underlying"]) for item in self.unique_contracts})
        )


class RecordedSessionReader:
    """Validate and stream a recorded microstructure/gate JSONL session."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)

    def audit(self) -> SessionAudit:
        market_events = gate_frames = source_qualified = 0
        regressions = 0
        max_regression = 0.0
        quotes = quotes_with_greeks = market_spot_events = 0
        market_future_events = 0
        previous: datetime | None = None
        first: datetime | None = None
        last: datetime | None = None
        contracts: dict[str, dict[str, object]] = {}
        capture_configuration: dict[str, object] = {}
        spot_tokens: dict[str, dict[str, object]] = {}
        future_contracts: dict[str, dict[str, object]] = {}

        for _, record in self._iter_file_records():
            timestamp = _record_timestamp(record)
            if first is None or timestamp < first:
                first = timestamp
            if last is None or timestamp > last:
                last = timestamp
            if previous is not None and timestamp < previous:
                regressions += 1
                max_regression = max(
                    max_regression,
                    (previous - timestamp).total_seconds(),
                )
            previous = timestamp

            record_type = record.get("record_type")
            if record_type == "instrument_master":
                option_contracts = record.get("option_contracts")
                raw_spot_tokens = record.get("spot_tokens")
                raw_reference_tokens = record.get("reference_tokens")
                raw_future_contracts = record.get("future_contracts")
                for raw_tokens in (raw_spot_tokens, raw_reference_tokens):
                    if not isinstance(raw_tokens, list):
                        continue
                    for token in raw_tokens:
                        if isinstance(token, dict) and token.get("token") is not None:
                            spot_tokens[str(token["token"])] = token
                if isinstance(raw_future_contracts, list):
                    for contract in raw_future_contracts:
                        if not isinstance(contract, dict):
                            continue
                        token = contract.get("token")
                        if isinstance(token, dict) and token.get("token") is not None:
                            future_contracts[str(token["token"])] = contract
                if isinstance(option_contracts, list):
                    for contract in option_contracts:
                        if not isinstance(contract, dict):
                            continue
                        token = contract.get("token")
                        if isinstance(token, dict) and token.get("token") is not None:
                            contracts[str(token["token"])] = contract
            elif record_type == "session_manifest":
                settings = record.get("effective_settings")
                if isinstance(settings, dict):
                    candidate = {
                        "option_window_each_side": int(
                            settings.get("option_window_each_side", 4)
                        ),
                        "option_greeks_enabled": bool(
                            settings.get("option_greeks_enabled", True)
                        ),
                        "replay_require_complete_window": bool(
                            settings.get(
                                "replay_require_complete_window",
                                True,
                            )
                        ),
                    }
                    if (
                        capture_configuration
                        and candidate != capture_configuration
                    ):
                        raise ValueError(
                            "Capture sessions use conflicting physical "
                            "window/Greeks settings"
                        )
                    capture_configuration = candidate
            elif record_type == "market_event":
                market_events += 1
                tick = record.get("tick")
                if isinstance(tick, dict):
                    token = tick.get("token")
                    if (
                        record.get("event_role") == "spot"
                        or isinstance(token, dict)
                        and token.get("kind") == "index"
                    ):
                        market_spot_events += 1
                    if (
                        record.get("event_role") == "future"
                        or isinstance(token, dict)
                        and token.get("kind") == "future"
                    ):
                        market_future_events += 1
            elif record_type == "gate_decision":
                gate_frames += 1
                decision = record.get("decision")
                if isinstance(decision, dict) and decision.get("qualified") is True:
                    source_qualified += 1
                snapshot = record.get("snapshot")
                if not isinstance(snapshot, dict):
                    raise ValueError("gate_decision record has no snapshot object")
                raw_quotes = snapshot.get("quotes")
                if not isinstance(raw_quotes, list):
                    raise ValueError("gate_decision snapshot has no quotes list")
                for quote in raw_quotes:
                    if not isinstance(quote, dict):
                        continue
                    quotes += 1
                    if isinstance(quote.get("greeks"), dict):
                        quotes_with_greeks += 1
                    contract = quote.get("contract")
                    if isinstance(contract, dict):
                        token = contract.get("token")
                        if isinstance(token, dict) and token.get("token") is not None:
                            contracts[str(token["token"])] = contract

        if gate_frames == 0:
            raise ValueError("Capture contains no gate_decision snapshots")
        if market_events == 0:
            raise ValueError("Capture contains no market_event records")
        if not contracts:
            raise ValueError("Capture contains no reconstructable option contracts")

        return SessionAudit(
            source_path=self.path,
            market_events=market_events,
            gate_frames=gate_frames,
            source_qualified=source_qualified,
            timestamp_regressions=regressions,
            maximum_regression_seconds=max_regression,
            first_timestamp=first,
            last_timestamp=last,
            unique_contracts=tuple(contracts.values()),
            excluded_contaminated_contracts=sum(
                1
                for contract in contracts.values()
                if not contract_matches_underlying(contract)
            ),
            quotes=quotes,
            quotes_with_greeks=quotes_with_greeks,
            market_spot_events=market_spot_events,
            market_future_events=market_future_events,
            capture_configuration=capture_configuration,
            spot_tokens=tuple(spot_tokens.values()),
            future_contracts=tuple(future_contracts.values()),
        )

    def records(
        self,
        *,
        mode: ReplayMode,
        index_path: Path | None = None,
    ) -> Iterator[tuple[int, dict[str, object]]]:
        if mode == "faithful":
            yield from self._iter_file_records()
            return
        if mode != "event-time":
            raise ValueError(f"Unknown replay mode: {mode}")
        if index_path is None:
            records = list(self._iter_file_records())
            records.sort(
                key=lambda item: (
                    _record_timestamp(item[1]),
                    _record_priority(item[1]),
                    item[0],
                )
            )
            yield from records
            return
        yield from self._iter_event_time(index_path)

    def _iter_file_records(self) -> Iterator[tuple[int, dict[str, object]]]:
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                yield line_number, _decode_record(line, self.path, line_number)

    def _iter_event_time(
        self,
        index_path: Path,
    ) -> Iterator[tuple[int, dict[str, object]]]:
        self._build_event_index(index_path)
        connection = sqlite3.connect(index_path)
        source = self.path.open("rb")
        try:
            cursor = connection.execute(
                """
                SELECT line_number, byte_offset
                FROM replay_index
                ORDER BY timestamp_epoch, record_priority, line_number
                """
            )
            for line_number, byte_offset in cursor:
                source.seek(byte_offset)
                line = source.readline().decode("utf-8")
                yield line_number, _decode_record(line, self.path, line_number)
        finally:
            source.close()
            connection.close()

    def _build_event_index(self, index_path: Path) -> None:
        if index_path.exists():
            return
        index_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(index_path)
        try:
            connection.execute(
                """
                CREATE TABLE replay_index (
                    timestamp_epoch REAL NOT NULL,
                    record_priority INTEGER NOT NULL,
                    line_number INTEGER NOT NULL,
                    byte_offset INTEGER NOT NULL
                )
                """
            )
            rows: list[tuple[float, int, int, int]] = []
            with self.path.open("rb") as source:
                line_number = 0
                while True:
                    byte_offset = source.tell()
                    raw_line = source.readline()
                    if not raw_line:
                        break
                    line_number += 1
                    if not raw_line.strip():
                        continue
                    record = _decode_record(
                        raw_line.decode("utf-8"),
                        self.path,
                        line_number,
                    )
                    record_type = record.get("record_type")
                    if record_type not in {
                        "session_manifest",
                        "market_event",
                        "gate_decision",
                        "session_end",
                    }:
                        continue
                    priority = _record_priority(record)
                    rows.append(
                        (
                            _record_timestamp(record).timestamp(),
                            priority,
                            line_number,
                            byte_offset,
                        )
                    )
                    if len(rows) >= 5000:
                        connection.executemany(
                            "INSERT INTO replay_index VALUES (?, ?, ?, ?)",
                            rows,
                        )
                        rows.clear()
                if rows:
                    connection.executemany(
                        "INSERT INTO replay_index VALUES (?, ?, ?, ?)",
                        rows,
                    )
            connection.execute(
                """
                CREATE INDEX replay_index_order
                ON replay_index(timestamp_epoch, record_priority, line_number)
                """
            )
            connection.commit()
        except Exception:
            connection.close()
            if index_path.exists():
                index_path.unlink()
            raise
        finally:
            if connection:
                connection.close()


def _record_timestamp(record: dict[str, object]) -> datetime:
    record_type = record.get("record_type")
    if record_type == "market_event":
        tick = record.get("tick")
        if isinstance(tick, dict) and tick.get("received_at") is not None:
            return parse_datetime(tick["received_at"])
    if record_type == "gate_decision":
        snapshot = record.get("snapshot")
        if isinstance(snapshot, dict) and snapshot.get("captured_at") is not None:
            return parse_datetime(snapshot["captured_at"])
    if record.get("captured_at") is None:
        raise ValueError(f"Record has no usable timestamp: {record_type!r}")
    return parse_datetime(record["captured_at"])


def _record_priority(record: dict[str, object]) -> int:
    return {
        "session_manifest": -1,
        "market_event": 0,
        "gate_decision": 1,
        "session_end": 2,
    }.get(str(record.get("record_type")), 3)


def _decode_record(line: str, path: Path, line_number: int) -> dict[str, object]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSONL at {path}:{line_number}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"Non-object JSONL record at {path}:{line_number}")
    return record
