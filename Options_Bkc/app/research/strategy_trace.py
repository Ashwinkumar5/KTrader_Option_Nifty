from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterable


EVENT_STATUSES = frozenset({"SELECTED", "SUPPRESSED", "CANDIDATE"})


@dataclass(frozen=True)
class StrategyOutcome:
    source: Path
    session_id: str
    strategy: str
    captured_at: datetime
    status: str
    side: str
    reason: str
    symbol: str | None
    token: str | None
    entry_ask: Decimal | None
    horizon_bid: Decimal | None
    return_percent: Decimal | None
    maximum_gain_percent: Decimal | None
    maximum_drawdown_percent: Decimal | None
    complete_horizon: bool
    raw_signal: str
    qualified: bool
    gate_reason: str


@dataclass(frozen=True)
class TraceCatalog:
    files: tuple[Path, ...]
    enabled_strategies: tuple[str, ...]


def discover_trace_files(source: Path) -> tuple[Path, ...]:
    if source.is_file():
        return (source,)
    if not source.exists():
        return ()
    return tuple(
        sorted(
            source.rglob("analytics_engine_trace_*.jsonl"),
            key=lambda item: (item.stat().st_mtime, str(item)),
        )
    )


def catalog_traces(source: Path) -> TraceCatalog:
    files = discover_trace_files(source)
    enabled: set[str] = set()
    for path in files:
        with path.open("r", encoding="utf-8") as handle:
            first = handle.readline()
        if not first:
            continue
        try:
            manifest = json.loads(first)
        except json.JSONDecodeError:
            continue
        if manifest.get("record_type") != "analytics_trace_manifest":
            continue
        enabled.update(str(item) for item in manifest.get("enabled_strategies", ()))
    return TraceCatalog(files=files, enabled_strategies=tuple(sorted(enabled)))


def analyze_strategy(
    files: Iterable[Path],
    *,
    strategy: str,
    horizon_minutes: int = 10,
) -> tuple[StrategyOutcome, ...]:
    outcomes: list[StrategyOutcome] = []
    for path in files:
        outcomes.extend(
            _analyze_file(
                path,
                strategy=strategy,
                horizon=timedelta(minutes=horizon_minutes),
            )
        )
    return tuple(sorted(outcomes, key=lambda item: item.captured_at))


def _analyze_file(
    path: Path,
    *,
    strategy: str,
    horizon: timedelta,
) -> list[StrategyOutcome]:
    session_id = ""
    frames: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            record_type = record.get("record_type")
            if record_type == "analytics_trace_manifest":
                session_id = str(record.get("session_id") or "")
            elif record_type == "analytics_engine_trace":
                frames.append(record)

    timed_frames = [
        (_parse_time(frame.get("captured_at_ist") or frame.get("captured_at")), frame)
        for frame in frames
    ]
    timed_frames = [
        (captured_at, frame)
        for captured_at, frame in timed_frames
        if captured_at is not None
    ]
    timed_frames.sort(key=lambda item: item[0])
    if not timed_frames:
        return []

    result: list[StrategyOutcome] = []
    last_frame_time = timed_frames[-1][0]
    for index, (captured_at, frame) in enumerate(timed_frames):
        diagnostic = next(
            (
                item
                for item in frame.get("strategies", ())
                if item.get("strategy") == strategy
            ),
            None,
        )
        if not isinstance(diagnostic, dict):
            continue
        status = str(diagnostic.get("status") or "")
        if status not in EVENT_STATUSES:
            continue
        reason = str(diagnostic.get("reason") or "")
        side = _strategy_side(diagnostic, reason)
        if side is None:
            continue
        contract = diagnostic.get("research_contract")
        if not isinstance(contract, dict):
            contract = _atm_proxy_contract(frame, side)
        token = str(contract.get("token")) if contract and contract.get("token") else None
        symbol = (
            str(contract.get("trading_symbol"))
            if contract and contract.get("trading_symbol")
            else None
        )
        entry_ask = _decimal(contract.get("entry_ask")) if contract else None
        horizon_at = captured_at + horizon
        window = [
            future_frame
            for future_time, future_frame in timed_frames[index:]
            if future_time <= horizon_at
        ]
        bids = tuple(
            bid
            for future_frame in window
            if (bid := _quote_bid(future_frame, token)) is not None
        )
        horizon_bid = bids[-1] if bids else None
        complete = last_frame_time >= horizon_at
        result.append(
            StrategyOutcome(
                source=path,
                session_id=session_id,
                strategy=strategy,
                captured_at=captured_at,
                status=status,
                side=side,
                reason=reason,
                symbol=symbol,
                token=token,
                entry_ask=entry_ask,
                horizon_bid=horizon_bid if complete else None,
                return_percent=(
                    _return_percent(entry_ask, horizon_bid)
                    if complete
                    else None
                ),
                maximum_gain_percent=(
                    _return_percent(entry_ask, max(bids))
                    if entry_ask is not None and bids
                    else None
                ),
                maximum_drawdown_percent=(
                    _return_percent(entry_ask, min(bids))
                    if entry_ask is not None and bids
                    else None
                ),
                complete_horizon=complete,
                raw_signal=str(frame.get("raw_signal") or ""),
                qualified=bool(frame.get("qualified")),
                gate_reason=str(frame.get("gate_reason") or ""),
            )
        )
    return result


def _strategy_side(diagnostic: dict[str, object], reason: str) -> str | None:
    side = diagnostic.get("proposed_side")
    if side in {"BUY_CALL", "BUY_PUT"}:
        return str(side)
    upper_reason = reason.upper()
    if "GAMMA CALL EXPANSION" in upper_reason:
        return "BUY_CALL"
    if "GAMMA PUT EXPANSION" in upper_reason:
        return "BUY_PUT"
    return None


def _atm_proxy_contract(
    frame: dict[str, object],
    side: str,
) -> dict[str, object] | None:
    market_frame = frame.get("market_frame")
    if not isinstance(market_frame, dict):
        return None
    atm = _decimal(market_frame.get("atm_strike"))
    option_type = "CE" if side == "BUY_CALL" else "PE"
    for quote in market_frame.get("option_quotes", ()):
        if (
            isinstance(quote, dict)
            and _decimal(quote.get("strike")) == atm
            and quote.get("option_type") == option_type
        ):
            return {
                "token": quote.get("token"),
                "trading_symbol": quote.get("trading_symbol"),
                "entry_ask": quote.get("ask"),
            }
    return None


def _quote_bid(frame: dict[str, object], token: str | None) -> Decimal | None:
    if token is None:
        return None
    market_frame = frame.get("market_frame")
    if not isinstance(market_frame, dict):
        return None
    for quote in market_frame.get("option_quotes", ()):
        if isinstance(quote, dict) and str(quote.get("token")) == token:
            return _decimal(quote.get("bid"))
    return None


def _return_percent(
    entry_ask: Decimal | None,
    exit_bid: Decimal | None,
) -> Decimal | None:
    if entry_ask is None or exit_bid is None or entry_ask <= 0:
        return None
    return ((exit_bid - entry_ask) / entry_ask * Decimal("100")).quantize(
        Decimal("0.01")
    )


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None

