from __future__ import annotations

import argparse
import asyncio

from ktrader_simulator.broker.angleone import AngleOneReadOnlyBroker
from ktrader_simulator.config import ConfigurationError, load_settings
from ktrader_simulator.domain.models import format_strike
from ktrader_simulator.market.snapshots import MarketSnapshotService


async def _market_check(index: str | None) -> int:
    settings = load_settings()
    selected = (index or settings.default_index).strip().upper()
    if selected not in settings.supported_indices:
        supported = ", ".join(settings.supported_indices)
        raise ConfigurationError(f"Index must be one of: {supported}")

    broker = AngleOneReadOnlyBroker(settings)
    await broker.connect()
    service = await MarketSnapshotService.create(broker=broker, settings=settings)
    available = ", ".join(service.available_indices)
    print("Broker connection: ANGLEONE READ-ONLY")
    print(f"Option instruments loaded: {service.instrument_count}")
    print(f"Available indices: {available}")

    snapshot = await service.snapshot(selected)
    print(
        f"{snapshot.underlying} spot={snapshot.spot_price} "
        f"atm={format_strike(snapshot.atm_strike)} expiry={snapshot.expiry}"
    )
    for row in snapshot.rows:
        call_bid = row.call_quote.bid if row.call_quote else None
        call_ask = row.call_quote.ask if row.call_quote else None
        put_bid = row.put_quote.bid if row.put_quote else None
        put_ask = row.put_quote.ask if row.put_quote else None
        print(f"{row.strike_label}: CE {call_bid}/{call_ask} | PE {put_bid}/{put_ask}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only KTrader market-data check")
    parser.add_argument("--index", help="NIFTY, BANKNIFTY, SENSEX, or BANKEX")
    arguments = parser.parse_args()
    try:
        exit_code = asyncio.run(_market_check(arguments.index))
    except (ConfigurationError, RuntimeError) as exc:
        raise SystemExit(f"Market check failed: {exc}") from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
