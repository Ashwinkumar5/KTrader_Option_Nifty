from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.domain.models import (
    Exchange,
    GreeksSnapshot,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionType,
    TickQuality,
    UnderlyingMarketSnapshot,
)
from app.marketdata.normalizer import normalize_tick


def parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def parse_date(value: object) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def decimal_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(Decimal(str(value)))


def parse_token(data: dict[str, object]) -> InstrumentToken:
    kind_value = data.get("kind")
    return InstrumentToken(
        exchange=Exchange(str(data["exchange"])),
        token=str(data["token"]),
        symbol=str(data["symbol"]),
        trading_symbol=str(data["trading_symbol"]),
        kind=InstrumentKind(str(kind_value)) if kind_value else None,
    )


def parse_contract(data: dict[str, object]) -> OptionContract:
    return OptionContract(
        underlying=str(data["underlying"]),
        expiry=parse_date(data["expiry"]),
        strike=Decimal(str(data["strike"])),
        option_type=OptionType(str(data["option_type"])),
        token=parse_token(_dict(data["token"])),
        lot_size=int_or_none(data.get("lot_size")),
    )


def parse_snapshot(data: dict[str, object]) -> OptionChainSnapshot:
    quotes: list[OptionQuote] = []
    for raw_quote in _list_of_dicts(data.get("quotes")):
        contract = parse_contract(_dict(raw_quote["contract"]))
        raw_greeks = raw_quote.get("greeks")
        greeks = None
        if isinstance(raw_greeks, dict):
            greeks = GreeksSnapshot(
                contract=contract,
                captured_at=parse_datetime(raw_greeks["captured_at"]),
                implied_volatility=decimal_or_none(raw_greeks.get("implied_volatility")),
                delta=decimal_or_none(raw_greeks.get("delta")),
                gamma=decimal_or_none(raw_greeks.get("gamma")),
                theta=decimal_or_none(raw_greeks.get("theta")),
                vega=decimal_or_none(raw_greeks.get("vega")),
                source=str(raw_greeks.get("source") or "recorded"),
            )
        quotes.append(
            OptionQuote(
                contract=contract,
                ltp=decimal_or_none(raw_quote.get("ltp")),
                open_price=decimal_or_none(raw_quote.get("open_price")),
                high_price=decimal_or_none(raw_quote.get("high_price")),
                low_price=decimal_or_none(raw_quote.get("low_price")),
                close_price=decimal_or_none(raw_quote.get("close_price")),
                oi=int_or_none(raw_quote.get("oi")),
                oi_change=int_or_none(raw_quote.get("oi_change")),
                oi_change_percent=decimal_or_none(raw_quote.get("oi_change_percent")),
                volume=int_or_none(raw_quote.get("volume")),
                bid=decimal_or_none(raw_quote.get("bid")),
                ask=decimal_or_none(raw_quote.get("ask")),
                greeks=greeks,
            )
        )
    raw_market = data.get("market")
    market = (
        parse_underlying_market_snapshot(raw_market)
        if isinstance(raw_market, dict)
        else None
    )
    return OptionChainSnapshot(
        underlying=str(data["underlying"]),
        expiry=parse_date(data["expiry"]),
        spot_price=Decimal(str(data["spot_price"])),
        atm_strike=Decimal(str(data["atm_strike"])),
        captured_at=parse_datetime(data["captured_at"]),
        quotes=tuple(quotes),
        market=market,
    )


def parse_underlying_market_snapshot(
    data: dict[str, object],
) -> UnderlyingMarketSnapshot:
    return UnderlyingMarketSnapshot(
        underlying=str(data["underlying"]),
        captured_at=parse_datetime(data["captured_at"]),
        spot_observed_at=(
            parse_datetime(data["spot_observed_at"])
            if data.get("spot_observed_at")
            else None
        ),
        open_price=decimal_or_none(data.get("open_price")),
        high_price=decimal_or_none(data.get("high_price")),
        low_price=decimal_or_none(data.get("low_price")),
        previous_close=decimal_or_none(data.get("previous_close")),
        future_observed_at=(
            parse_datetime(data["future_observed_at"])
            if data.get("future_observed_at")
            else None
        ),
        future_price=decimal_or_none(data.get("future_price")),
        future_open=decimal_or_none(data.get("future_open")),
        future_high=decimal_or_none(data.get("future_high")),
        future_low=decimal_or_none(data.get("future_low")),
        future_previous_close=decimal_or_none(
            data.get("future_previous_close")
        ),
        future_volume=int_or_none(data.get("future_volume")),
        future_oi=int_or_none(data.get("future_oi")),
        future_vwap=decimal_or_none(data.get("future_vwap")),
        basis=decimal_or_none(data.get("basis")),
        previous_20d_atr=decimal_or_none(data.get("previous_20d_atr")),
        previous_session_expected_move=decimal_or_none(
            data.get("previous_session_expected_move")
        ),
        market_breadth=decimal_or_none(data.get("market_breadth")),
        india_vix=decimal_or_none(data.get("india_vix")),
    )


def parse_market_tick(record: dict[str, object]) -> MarketTick:
    tick_data = _dict(record["tick"])
    token = parse_token(_dict(tick_data["token"]))
    raw = tick_data.get("raw")
    if isinstance(raw, dict) and raw:
        # Exercise the same broker-payload normalizer as the live WebSocket path.
        return normalize_tick(
            token=token,
            payload=dict(raw),
            received_at=parse_datetime(tick_data["received_at"]),
        )
    return MarketTick(
        token=token,
        exchange_timestamp=parse_datetime(tick_data["exchange_timestamp"]),
        received_at=parse_datetime(tick_data["received_at"]),
        ltp=decimal_or_none(tick_data.get("ltp")),
        open_price=decimal_or_none(tick_data.get("open_price")),
        high_price=decimal_or_none(tick_data.get("high_price")),
        low_price=decimal_or_none(tick_data.get("low_price")),
        close_price=decimal_or_none(tick_data.get("close_price")),
        oi=int_or_none(tick_data.get("oi")),
        oi_change=int_or_none(tick_data.get("oi_change")),
        oi_change_percent=decimal_or_none(tick_data.get("oi_change_percent")),
        volume=int_or_none(tick_data.get("volume")),
        bid=decimal_or_none(tick_data.get("bid")),
        ask=decimal_or_none(tick_data.get("ask")),
        quality=TickQuality(str(tick_data.get("quality") or TickQuality.LIVE.value)),
        raw={},
    )


def contract_matches_underlying(contract: dict[str, object]) -> bool:
    underlying = str(contract.get("underlying") or "").upper()
    token = contract.get("token")
    if not underlying or not isinstance(token, dict):
        return False
    trading_symbol = str(token.get("trading_symbol") or "").upper()
    if not trading_symbol.startswith(underlying):
        return False
    remainder = trading_symbol[len(underlying):]
    return not remainder or not remainder[0].isalpha()


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Expected object, got {type(value).__name__}")
    return value


def _list_of_dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
