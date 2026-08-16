from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SmartApiDataSource(StrEnum):
    WEBSOCKET_SNAP_QUOTE = "websocket_snap_quote"
    WEBSOCKET_QUOTE = "websocket_quote"
    WEBSOCKET_LTP = "websocket_ltp"
    MARKET_QUOTE = "market_quote"
    LTP_DATA = "ltp_data"
    OPTION_GREEK = "option_greek"
    HISTORICAL_OI = "historical_oi"
    PUT_CALL_RATIO = "put_call_ratio"
    OI_BUILDUP = "oi_buildup"
    INSTRUMENT_MASTER = "instrument_master"


class SmartApiWebSocketMode(StrEnum):
    LTP = "LTP"
    QUOTE = "QUOTE"
    SNAP_QUOTE = "SNAP_QUOTE"
    DEPTH = "DEPTH"

    @property
    def code(self) -> int:
        return {
            SmartApiWebSocketMode.LTP: 1,
            SmartApiWebSocketMode.QUOTE: 2,
            SmartApiWebSocketMode.SNAP_QUOTE: 3,
            SmartApiWebSocketMode.DEPTH: 4            
        }[self]


@dataclass(frozen=True)
class RequiredMarketField:
    name: str
    preferred_source: SmartApiDataSource
    fallback_source: SmartApiDataSource | None
    notes: str


REQUIRED_OPTION_CHAIN_FIELDS: tuple[RequiredMarketField, ...] = (
    RequiredMarketField(
        name="ltp",
        preferred_source=SmartApiDataSource.WEBSOCKET_SNAP_QUOTE,
        fallback_source=SmartApiDataSource.MARKET_QUOTE,
        notes="Live option price for selected CE/PE contracts.",
    ),
    RequiredMarketField(
        name="open_high_low_close",
        preferred_source=SmartApiDataSource.WEBSOCKET_SNAP_QUOTE,
        fallback_source=SmartApiDataSource.MARKET_QUOTE,
        notes="Carry OHLC/close when broker snap quote or market quote provides it.",
    ),
    RequiredMarketField(
        name="volume",
        preferred_source=SmartApiDataSource.WEBSOCKET_SNAP_QUOTE,
        fallback_source=SmartApiDataSource.MARKET_QUOTE,
        notes="Use quote/snap quote when available; persist with snapshot timestamp.",
    ),
    RequiredMarketField(
        name="oi",
        preferred_source=SmartApiDataSource.WEBSOCKET_SNAP_QUOTE,
        fallback_source=SmartApiDataSource.HISTORICAL_OI,
        notes="OI may update slower than LTP; track freshness separately.",
    ),
    RequiredMarketField(
        name="oi_change",
        preferred_source=SmartApiDataSource.WEBSOCKET_SNAP_QUOTE,
        fallback_source=SmartApiDataSource.HISTORICAL_OI,
        notes="If broker does not send direct OI change, calculate from previous snapshot.",
    ),
    RequiredMarketField(
        name="best_bid_ask",
        preferred_source=SmartApiDataSource.WEBSOCKET_SNAP_QUOTE,
        fallback_source=SmartApiDataSource.MARKET_QUOTE,
        notes="Useful for spread-aware analytics and future execution readiness.",
    ),
    RequiredMarketField(
        name="implied_volatility",
        preferred_source=SmartApiDataSource.OPTION_GREEK,
        fallback_source=None,
        notes="Phase 3 should calculate internal IV for validation and faster analytics.",
    ),
    RequiredMarketField(
        name="delta_gamma_theta_vega",
        preferred_source=SmartApiDataSource.OPTION_GREEK,
        fallback_source=None,
        notes="Broker Greeks are useful initially; internal Greeks engine remains planned.",
    ),
    RequiredMarketField(
        name="put_call_ratio",
        preferred_source=SmartApiDataSource.PUT_CALL_RATIO,
        fallback_source=None,
        notes="Broker-level PCR can be stored as a cross-check against our chain-derived PCR.",
    ),
    RequiredMarketField(
        name="oi_buildup",
        preferred_source=SmartApiDataSource.OI_BUILDUP,
        fallback_source=None,
        notes="Broker OI buildup can enrich directional/regime analytics when enabled.",
    ),
    RequiredMarketField(
        name="exchange_timestamp",
        preferred_source=SmartApiDataSource.WEBSOCKET_SNAP_QUOTE,
        fallback_source=SmartApiDataSource.MARKET_QUOTE,
        notes="Required to track tick freshness and stale-chain detection.",
    ),
)


REQUIRED_UNDERLYINGS: tuple[str, ...] = (
    "NIFTY_INDEX",
    "NIFTY_NEAREST_FUTURE",
    "BANKNIFTY_INDEX",
    "BANKNIFTY_NEAREST_FUTURE",
)


SMARTAPI_ENDPOINTS: dict[SmartApiDataSource, str] = {
    SmartApiDataSource.WEBSOCKET_LTP: "SmartWebSocketV2 mode 1",
    SmartApiDataSource.WEBSOCKET_QUOTE: "SmartWebSocketV2 mode 2",
    SmartApiDataSource.WEBSOCKET_SNAP_QUOTE: "SmartWebSocketV2 mode 3",
    SmartApiDataSource.MARKET_QUOTE: "SmartConnect.getMarketData",
    SmartApiDataSource.LTP_DATA: "SmartConnect.ltpData",
    SmartApiDataSource.OPTION_GREEK: "SmartConnect.optionGreek",
    SmartApiDataSource.HISTORICAL_OI: "SmartConnect.getOIData",
    SmartApiDataSource.PUT_CALL_RATIO: "SmartConnect.putCallRatio",
    SmartApiDataSource.OI_BUILDUP: "SmartConnect.oIBuildup",
    SmartApiDataSource.INSTRUMENT_MASTER: "SmartConnect instrument master download/load",
}


def required_fields_by_source() -> dict[SmartApiDataSource, tuple[RequiredMarketField, ...]]:
    grouped: dict[SmartApiDataSource, list[RequiredMarketField]] = {}
    for field in REQUIRED_OPTION_CHAIN_FIELDS:
        grouped.setdefault(field.preferred_source, []).append(field)
    return {source: tuple(fields) for source, fields in grouped.items()}
