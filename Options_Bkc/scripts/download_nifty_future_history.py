"""Download recent NIFTY futures candles through the configured broker API.

Angle One historical candles do not expose a five-second interval. The script
therefore requests one-minute candles for structural research and writes one
CSV per completed session plus an auditable JSON manifest. Full SMC execution
tests must still use the repository's five-second broker replay tapes.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.broker.registry import (  # noqa: E402
    build_configured_instrument_master,
    create_broker_client,
)
from app.core.config import load_settings  # noqa: E402


DEFAULT_OUTPUT = ROOT_DIR / "data" / "nifty_future"
CSV_HEADER = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download recent NIFTY front-future one-minute candles."
    )
    parser.add_argument("--calendar-days", type=int, default=15)
    parser.add_argument("--through", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.calendar_days <= 0:
        raise ValueError("--calendar-days must be positive")

    settings = load_settings()
    if not settings.broker_credentials_configured:
        raise RuntimeError("configured broker credentials are required")

    through = args.through or _last_completed_weekday(date.today())
    start = through - timedelta(days=args.calendar_days - 1)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    client = create_broker_client(settings)
    manifest_sessions: list[dict[str, object]] = []
    try:
        await client.login()
        rows = await client.instrument_master()
        master = build_configured_instrument_master(
            settings=settings,
            rows=rows,
        )

        for session_date in _weekdays(start, through):
            contract = master.nearest_future(
                underlying="NIFTY",
                as_of=session_date,
            )
            if contract is None:
                manifest_sessions.append(
                    {
                        "session_date": session_date.isoformat(),
                        "status": "SKIPPED",
                        "reason": "no eligible NIFTY future in instrument master",
                    }
                )
                continue
            response = await client.historical_candles(
                {
                    "exchange": contract.token.exchange.value,
                    "symboltoken": contract.token.token,
                    "interval": "ONE_MINUTE",
                    "fromdate": f"{session_date.isoformat()} 09:15",
                    "todate": f"{session_date.isoformat()} 15:30",
                }
            )
            candles = _normalized_rows(response)
            path = output / (
                f"{session_date:%Y%m%d}_"
                f"{contract.token.trading_symbol}_ONE_MINUTE.csv"
            )
            if candles:
                _write_csv(path, candles)
                status = "READY"
                reason = None
            else:
                status = "NO_DATA"
                reason = _response_message(response)
            manifest_sessions.append(
                {
                    "session_date": session_date.isoformat(),
                    "status": status,
                    "reason": reason,
                    "contract": contract.token.trading_symbol,
                    "token": contract.token.token,
                    "expiry": contract.expiry.isoformat(),
                    "interval": "ONE_MINUTE",
                    "rows": len(candles),
                    "file": str(path) if candles else None,
                }
            )
            await asyncio.sleep(0.35)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            await close()

    manifest = {
        "downloaded_at": datetime.now().astimezone().isoformat(),
        "underlying": "NIFTY",
        "requested_calendar_days": args.calendar_days,
        "from_date": start.isoformat(),
        "through_date": through.isoformat(),
        "historical_interval": "ONE_MINUTE",
        "execution_interval": "FIVE_SECONDS_FROM_REPLAY_TAPES",
        "sessions": manifest_sessions,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    ready = sum(item["status"] == "READY" for item in manifest_sessions)
    print(f"Downloaded {ready} sessions to {output}")
    print(f"Manifest: {manifest_path}")
    return 0 if ready else 1


def _last_completed_weekday(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _weekdays(start: date, end: date):
    current = start
    while current <= end:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def _normalized_rows(response: object) -> list[tuple[object, ...]]:
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    if not isinstance(data, list):
        return []
    result: list[tuple[object, ...]] = []
    for row in data:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        result.append(tuple(row[:6]))
    return result


def _write_csv(path: Path, rows: list[tuple[object, ...]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)


def _response_message(response: object) -> str | None:
    if not isinstance(response, dict):
        return "broker returned a non-object response"
    message = response.get("message") or response.get("errorcode")
    return str(message) if message else None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
