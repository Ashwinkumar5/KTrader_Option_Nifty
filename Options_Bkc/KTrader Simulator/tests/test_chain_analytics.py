from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from ktrader_simulator.domain.models import (
    ChainRow,
    Instrument,
    MarketSnapshot,
    Moneyness,
    OptionInstrument,
    OptionType,
    Quote,
)
from ktrader_simulator.market.analytics import ChainAnalyticsEngine, MetricStatus


def _option(strike: Decimal, option_type: OptionType) -> OptionInstrument:
    return OptionInstrument(
        underlying="NIFTY",
        expiry=date(2026, 8, 6),
        strike=strike,
        option_type=option_type,
        instrument=Instrument(
            "NFO",
            f"{strike}{option_type.value}",
            f"NIFTY{strike}{option_type.value}",
        ),
        lot_size=65,
    )


def _quote(token: str, ltp: str, volume: str, oi: str) -> Quote:
    return Quote(
        token=token,
        ltp=Decimal(ltp),
        bid=Decimal(ltp),
        ask=Decimal(ltp),
        captured_at=datetime(2026, 8, 1, tzinfo=UTC),
        volume=Decimal(volume),
        open_interest=Decimal(oi),
    )


def test_consolidated_pcr_uses_otm_and_atm_sides_only() -> None:
    strikes = tuple(Decimal(value) for value in ("100", "110", "120", "130", "140"))
    rows = []
    for strike in strikes:
        call, put = _option(strike, OptionType.CALL), _option(strike, OptionType.PUT)
        rows.append(
            ChainRow(
                strike=strike,
                call=call,
                put=put,
                call_quote=_quote(call.instrument.token, "10", "10", "20"),
                put_quote=_quote(put.instrument.token, "11", "30", "60"),
                call_moneyness=Moneyness.ATM if strike == Decimal("120") else Moneyness.OTM,
                put_moneyness=Moneyness.ATM if strike == Decimal("120") else Moneyness.OTM,
            )
        )
    snapshot = MarketSnapshot(
        underlying="NIFTY",
        expiry=date(2026, 8, 6),
        spot_price=Decimal("120"),
        atm_strike=Decimal("120"),
        captured_at=datetime(2026, 8, 1, tzinfo=UTC),
        rows=tuple(rows),
    )
    engine = ChainAnalyticsEngine()
    analytics = engine.build(snapshot)
    # Put: strikes 100/110/120 = 180 OI; Call: strikes 120/130/140 = 60 OI.
    assert analytics.oi_pcr == Decimal("3")
    assert analytics.volume_pcr == Decimal("3")
    assert analytics.oi_pcr_status == MetricStatus.BULLISH
    assert analytics.volume_pcr_status == MetricStatus.BULLISH
    assert analytics.put_volume_oi_status == "NEUTRAL"
    assert analytics.call_volume_oi_status == "NEUTRAL"
    assert all(row.oi_pcr_status == MetricStatus.BULLISH for row in analytics.rows)
    assert all(row.put_volume_oi_status == MetricStatus.NEUTRAL for row in analytics.rows)
    assert all(row.straddle_status == MetricStatus.NEUTRAL for row in analytics.rows)

    bullish_rows = tuple(
        replace(
            row,
            call_quote=_quote(row.call.instrument.token, "11", "10", "21"),
            put_quote=_quote(row.put.instrument.token, "10", "30", "61"),
        )
        for row in snapshot.rows
    )
    bullish = engine.build(replace(snapshot, rows=bullish_rows))
    assert bullish.put_volume_oi_status == "WRITING"
    assert bullish.call_volume_oi_status == "LONG BUILD"
    assert all(row.put_volume_oi_status == MetricStatus.BULLISH for row in bullish.rows)

    bearish_rows = tuple(
        replace(
            row,
            call_quote=_quote(row.call.instrument.token, "10", "10", "22"),
            put_quote=_quote(row.put.instrument.token, "11", "30", "62"),
        )
        for row in snapshot.rows
    )
    bearish = engine.build(replace(snapshot, rows=bearish_rows))
    assert bearish.put_volume_oi_status == "LONG BUILD"
    assert bearish.call_volume_oi_status == "SHORT BUILD"
    assert all(row.put_volume_oi_status == MetricStatus.BEARISH for row in bearish.rows)
