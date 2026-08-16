from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path


FORBIDDEN_SETTING_KEYS = {
    "angleone_api_key",
    "angleone_client_code",
    "angleone_password",
    "angleone_totp_secret",
    "redis_url",
    "database_url",
}


def validate_tape(path: Path) -> tuple[dict[str, object], list[str]]:
    path = path.resolve()
    counts: Counter[str] = Counter()
    frame_statuses: Counter[str] = Counter()
    research_statuses: Counter[str] = Counter()
    schemas: Counter[str] = Counter()
    sessions: set[str] = set()
    issues: list[str] = []
    sha256 = hashlib.sha256()
    contaminated_contracts: set[str] = set()
    manifest_setting_keys: set[str] = set()
    expected_contract_counts: Counter[int] = Counter()
    session_configurations: dict[str, dict[str, object]] = {}
    last_sequence_by_session: dict[str, int] = {}
    schema_v4_sessions: set[str] = set()
    schema_v4_manifest_sessions: set[str] = set()
    schema_v4_session_ends: set[str] = set()
    schema_v4_records_by_session: Counter[str] = Counter()
    future_contract_count = 0

    with path.open("rb") as raw_handle:
        for raw_line in raw_handle:
            sha256.update(raw_line)

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append(f"invalid JSON at line {line_number}: {exc}")
                continue
            if not isinstance(record, dict):
                issues.append(f"non-object record at line {line_number}")
                continue
            record_type = str(record.get("record_type") or "unknown")
            counts[record_type] += 1
            schema_version = _integer(record.get("schema_version"), default=0)
            schemas[str(schema_version or "missing")] += 1
            session_id = record.get("session_id")
            if isinstance(session_id, str):
                sessions.add(session_id)
            session_key = str(session_id or "default")
            if schema_version >= 4:
                schema_v4_sessions.add(session_key)
                schema_v4_records_by_session[session_key] += 1
                sequence = _integer(record.get("sequence"), default=0)
                expected_sequence = (
                    last_sequence_by_session.get(session_key, 0) + 1
                )
                if sequence != expected_sequence:
                    issues.append(
                        f"schema-v4 sequence gap at line {line_number}: "
                        f"expected {expected_sequence}, got {sequence}"
                    )
                last_sequence_by_session[session_key] = sequence

            if record_type == "session_manifest":
                if schema_version >= 4:
                    schema_v4_manifest_sessions.add(session_key)
                settings = record.get("effective_settings")
                if isinstance(settings, dict):
                    manifest_setting_keys.update(str(key) for key in settings)
                    each_side = _integer(
                        settings.get("option_window_each_side"),
                        default=4,
                    )
                    session_configurations[session_key] = {
                        "option_window_each_side": each_side,
                        "expected_contract_count": (each_side * 2 + 1) * 2,
                        "option_greeks_enabled": bool(
                            settings.get("option_greeks_enabled", True)
                        ),
                        "replay_require_complete_window": bool(
                            settings.get("replay_require_complete_window", True)
                        ),
                        "snapshot_interval_ms": _integer(
                            settings.get("snapshot_interval_ms"),
                            default=1000,
                        ),
                        "broker_name": str(
                            settings.get("broker_name") or "unknown"
                        ),
                        "market_timezone": str(
                            settings.get("market_timezone")
                            or record.get("market_timezone")
                            or "unknown"
                        ),
                    }
                    if schema_version >= 4:
                        capabilities = record.get("capture_capabilities")
                        if not isinstance(capabilities, dict):
                            issues.append(
                                f"schema-v4 manifest at line {line_number} "
                                "has no capture capabilities"
                            )
                else:
                    issues.append(
                        f"session_manifest at line {line_number} has no settings"
                    )

            elif record_type == "instrument_master":
                spot_tokens = record.get("spot_tokens")
                option_contracts = record.get("option_contracts")
                future_contracts = record.get("future_contracts")
                if not isinstance(spot_tokens, list) or not spot_tokens:
                    issues.append(
                        f"instrument_master at line {line_number} has no spot tokens"
                    )
                if not isinstance(option_contracts, list) or not option_contracts:
                    issues.append(
                        f"instrument_master at line {line_number} has no option contracts"
                    )
                if isinstance(option_contracts, list):
                    for contract in option_contracts:
                        if not isinstance(contract, dict):
                            continue
                        if not _contract_matches_underlying(contract):
                            token = contract.get("token")
                            if isinstance(token, dict):
                                contaminated_contracts.add(
                                    str(token.get("trading_symbol") or token.get("token"))
                                )
                if schema_version >= 4:
                    if not isinstance(future_contracts, list):
                        issues.append(
                            f"schema-v4 instrument master at line "
                            f"{line_number} has no future contracts list"
                        )
                    else:
                        future_contract_count += len(future_contracts)

            elif record_type == "market_event":
                role = str(record.get("event_role") or "")
                if role in {"spot", "future", "option"}:
                    counts[f"market_event_{role}"] += 1

            elif record_type == "gate_decision":
                frame = record.get("frame")
                if not isinstance(frame, dict):
                    issues.append(f"gate_decision at line {line_number} has no frame")
                    continue
                data_quality = frame.get("data_quality")
                status = (
                    str(data_quality.get("status"))
                    if isinstance(data_quality, dict)
                    else "missing"
                )
                frame_statuses[status] += 1
                research_quality = frame.get("research_quality")
                research_status = (
                    str(research_quality.get("status"))
                    if isinstance(research_quality, dict)
                    else "missing"
                )
                research_statuses[research_status] += 1
                window = frame.get("window")
                if isinstance(window, dict):
                    expected = window.get("expected_contract_count")
                    if isinstance(expected, int):
                        expected_contract_counts[expected] += 1
                    if status == "VALID":
                        configuration = session_configurations.get(
                            str(record.get("session_id") or "default"),
                            {},
                        )
                        configured_expected = _integer(
                            configuration.get("expected_contract_count"),
                            default=expected if isinstance(expected, int) else 0,
                        )
                        _validate_complete_frame(
                            window,
                            line_number,
                            issues,
                            expected_contract_count=configured_expected,
                            require_greeks=bool(
                                configuration.get(
                                    "option_greeks_enabled",
                                    True,
                                )
                            ),
                        )
                        if schema_version >= 4:
                            _validate_research_frame(
                                record,
                                frame,
                                line_number,
                                issues,
                                expected_contract_count=configured_expected,
                                require_greeks=bool(
                                    configuration.get(
                                        "option_greeks_enabled",
                                        True,
                                    )
                                ),
                                research_ready=(
                                    research_status == "RESEARCH_READY"
                                ),
                            )
                else:
                    issues.append(
                        f"gate_decision at line {line_number} has no window metadata"
                    )
            elif record_type == "session_end" and schema_version >= 4:
                schema_v4_session_ends.add(session_key)

    leaked_keys = sorted(FORBIDDEN_SETTING_KEYS & manifest_setting_keys)
    if leaked_keys:
        issues.append(
            "manifest contains forbidden secret/connection settings: "
            + ", ".join(leaked_keys)
        )
    if contaminated_contracts:
        issues.append(
            "instrument master contains cross-underlying contracts: "
            + ", ".join(sorted(contaminated_contracts))
        )
    physical_configurations = {
        (
            configuration.get("option_window_each_side"),
            configuration.get("option_greeks_enabled"),
            configuration.get("replay_require_complete_window"),
        )
        for configuration in session_configurations.values()
    }
    if len(physical_configurations) > 1:
        issues.append(
            "sessions use conflicting option-window/Greeks capture settings"
        )

    required = {
        "session_manifest": 1,
        "instrument_master": 1,
        "subscription_change": 1,
        "market_event_spot": 1,
        "market_event_option": 1,
        "gate_decision": 1,
    }
    for record_type, minimum in required.items():
        if counts[record_type] < minimum:
            issues.append(f"missing required records: {record_type}")
    if frame_statuses["VALID"] == 0:
        issues.append("no strict VALID configured-window frames were captured")
    if schema_v4_sessions:
        if counts["market_event_future"] == 0:
            issues.append("schema-v4 tape has no nearest-future market events")
        if future_contract_count == 0:
            issues.append("schema-v4 tape has no future contracts")
        unfinished = schema_v4_sessions - schema_v4_session_ends
        abandoned_manifest_only = {
            session
            for session in unfinished
            if session in schema_v4_manifest_sessions
            and schema_v4_records_by_session[session] == 1
        }
        blocking_unfinished = unfinished - abandoned_manifest_only
        if blocking_unfinished:
            issues.append(
                "schema-v4 sessions lack a clean session_end: "
                + ", ".join(sorted(blocking_unfinished))
            )
        if research_statuses["RESEARCH_READY"] == 0:
            issues.append(
                "no RESEARCH_READY frames contain opening, futures, "
                "bid/ask, OI/volume and Greek context"
            )
    summary: dict[str, object] = {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256.hexdigest(),
        "sessions": len(sessions),
        "schema_versions": dict(schemas),
        "record_counts": dict(counts),
        "frame_statuses": dict(frame_statuses),
        "research_statuses": dict(research_statuses),
        "expected_contract_counts": dict(expected_contract_counts),
        "session_configurations": session_configurations,
        "contaminated_contracts": sorted(contaminated_contracts),
        "future_contract_count": future_contract_count,
        "clean_session_ends": len(schema_v4_session_ends),
        "abandoned_manifest_only_sessions": sorted(
            abandoned_manifest_only if schema_v4_sessions else ()
        ),
        "unfinished_sessions": sorted(
            blocking_unfinished if schema_v4_sessions else ()
        ),
    }
    summary["replay_ready"] = not issues
    return summary, issues


def _validate_complete_frame(
    window: dict[str, object],
    line_number: int,
    issues: list[str],
    *,
    expected_contract_count: int,
    require_greeks: bool,
) -> None:
    expected = window.get("expected_contract_count")
    selected = window.get("selected_contract_count")
    quote_tokens = window.get("quote_tokens")
    greeks_tokens = window.get("greeks_tokens")
    if expected != expected_contract_count or selected != expected_contract_count:
        issues.append(
            f"VALID frame at line {line_number} does not match configured "
            f"contract count {expected_contract_count}"
        )
    if (
        not isinstance(quote_tokens, list)
        or len(quote_tokens) != expected_contract_count
    ):
        issues.append(
            f"VALID frame at line {line_number} lacks "
            f"{expected_contract_count} quotes"
        )
    if require_greeks and (
        not isinstance(greeks_tokens, list)
        or len(greeks_tokens) != expected_contract_count
    ):
        issues.append(
            f"VALID frame at line {line_number} lacks "
            f"{expected_contract_count} Greek rows"
        )


def _validate_research_frame(
    record: dict[str, object],
    frame: dict[str, object],
    line_number: int,
    issues: list[str],
    *,
    expected_contract_count: int,
    require_greeks: bool,
    research_ready: bool,
) -> None:
    window = frame.get("window")
    if not isinstance(window, dict):
        return
    for key in ("valid_bid_ask_tokens", "oi_volume_tokens"):
        tokens = window.get(key)
        if (
            research_ready
            and (
                not isinstance(tokens, list)
                or len(tokens) != expected_contract_count
            )
        ):
            issues.append(
                f"RESEARCH_READY frame at line {line_number} lacks "
                f"{expected_contract_count} {key}"
            )
    if require_greeks and research_ready:
        usable = window.get("usable_iv_delta_tokens")
        if (
            not isinstance(usable, list)
            or len(usable) != expected_contract_count
        ):
            issues.append(
                f"RESEARCH_READY frame at line {line_number} lacks "
                f"{expected_contract_count} usable IV/delta rows"
            )
    if research_ready:
        snapshot = record.get("snapshot")
        if not isinstance(snapshot, dict):
            issues.append(
                f"RESEARCH_READY frame at line {line_number} has no snapshot"
            )
        else:
            quotes = snapshot.get("quotes")
            if (
                not isinstance(quotes, list)
                or len(quotes) != expected_contract_count
            ):
                issues.append(
                    f"RESEARCH_READY frame at line {line_number} lacks "
                    f"{expected_contract_count} normalized option quotes"
                )
            elif any(not _research_quote_ready(quote) for quote in quotes):
                issues.append(
                    f"RESEARCH_READY frame at line {line_number} contains "
                    "an incomplete normalized option quote"
                )
        market = frame.get("market_context")
        if not isinstance(market, dict):
            issues.append(
                f"RESEARCH_READY frame at line {line_number} has no "
                "market context"
            )
        else:
            required_market = (
                "open_price",
                "previous_close",
                "spot_observed_at",
                "future_observed_at",
                "future_price",
                "future_volume",
                "future_oi",
            )
            missing = [key for key in required_market if market.get(key) is None]
            if missing:
                issues.append(
                    f"RESEARCH_READY frame at line {line_number} is missing "
                    "market fields: "
                    + ", ".join(missing)
                )
        analytics = record.get("analytics")
        if not isinstance(analytics, dict):
            issues.append(
                f"RESEARCH_READY frame at line {line_number} has no analytics"
            )
        else:
            required_analytics = (
                "directional_evidence",
                "opening_context",
                "expected_move_context",
                "premium_responses",
                "momentum_exhaustion",
                "strategy_candidates",
            )
            missing = [
                key for key in required_analytics if key not in analytics
            ]
            if missing:
                issues.append(
                    f"RESEARCH_READY frame at line {line_number} lacks "
                    "analytics fields: "
                    + ", ".join(missing)
                )


def _research_quote_ready(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    bid = _number(value.get("bid"))
    ask = _number(value.get("ask"))
    greeks = value.get("greeks")
    return (
        value.get("ltp") is not None
        and value.get("oi") is not None
        and value.get("volume") is not None
        and bid is not None
        and ask is not None
        and bid > 0
        and ask >= bid
        and isinstance(greeks, dict)
        and greeks.get("implied_volatility") is not None
        and greeks.get("delta") is not None
    )


def _number(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _contract_matches_underlying(contract: dict[str, object]) -> bool:
    underlying = str(contract.get("underlying") or "").upper()
    token = contract.get("token")
    if not underlying or not isinstance(token, dict):
        return False
    trading_symbol = str(token.get("trading_symbol") or "").upper()
    if not trading_symbol.startswith(underlying):
        return False
    remainder = trading_symbol[len(underlying):]
    return not remainder or not remainder[0].isalpha()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a schema-v3/v4 broker replay tape after market close."
    )
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    summary, issues = validate_tape(args.path)
    print(json.dumps(summary, indent=2))
    if issues:
        print("")
        print("VALIDATION FAILED")
        for issue in issues:
            print(f"- {issue}")
        raise SystemExit(1)
    print("")
    print("VALIDATION PASSED: tape is ready for strict offline broker simulation.")


if __name__ == "__main__":
    main()
