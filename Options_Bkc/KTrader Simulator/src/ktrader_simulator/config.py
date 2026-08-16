from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values


class ConfigurationError(ValueError):
    """Raised when simulator configuration is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class Settings:
    simulator_root: Path
    bot_root: Path
    bot_env_path: Path
    data_dir: Path

    broker_name: str
    angleone_api_key: str = field(repr=False)
    angleone_client_code: str = field(repr=False)
    angleone_password: str = field(repr=False)
    angleone_totp_secret: str = field(repr=False)
    angleone_instrument_master_url: str
    angleone_instrument_master_path: str
    angleone_http_timeout_seconds: float
    broker_order_execution_enabled: bool
    order_execution_mode: str
    broker_order_product_type: str
    broker_order_variety: str
    broker_order_duration: str

    market_timezone: str
    storage_backend: str
    operational_tick_journal_enabled: bool
    operational_chain_journal_enabled: bool
    market_data_queue_capacity: int
    market_data_queue_pressure_ratio: Decimal
    runtime_metrics_sample_capacity: int
    market_data_price_source: str
    market_data_oi_source: str
    market_data_greeks_source: str
    market_data_ws_mode: str
    option_greeks_enabled: bool
    microstructure_enabled: bool
    microstructure_mode: str
    broker_pcr_enabled: bool
    broker_oi_buildup_enabled: bool

    app_title: str
    viewport_width: int
    viewport_height: int
    viewport_resizable: bool
    viewport_start_maximized: bool
    viewport_vsync: bool
    top_panel_height: int
    left_panel_ratio: Decimal
    ui_refresh_hz: int
    auto_connect: bool
    quote_mode: str
    quote_refresh_ms: int
    broker_io_workers: int
    broker_retry_seconds: Decimal
    max_consecutive_quote_errors: int

    supported_indices: tuple[str, ...]
    default_index: str
    starting_balance: Decimal
    default_lots: int
    default_order_type: str
    default_limit_price: Decimal
    default_buy_price_offset: Decimal
    default_target_percent: Decimal
    default_stop_loss_percent: Decimal
    default_trailing_sl_percent: Decimal
    max_capital_utilization: Decimal
    charges_buffer_percent: Decimal
    slippage_points: Decimal
    feed_stale_seconds: Decimal
    bot_order_intake_enabled: bool
    bot_ipc_endpoint: str
    bot_ipc_host: str
    bot_ipc_port: int
    bot_ipc_queue_capacity: int
    trade_ledger_path: Path
    trade_ledger_fsync: bool
    ledger_queue_capacity: int
    session_recovery_enabled: bool
    bot_signal_max_age_seconds: Decimal
    max_accepted_trades_per_day: int
    eod_close_time: str
    trade_journal_dir: Path
    chain_analytics_refresh_seconds: int
    oi_pcr_bearish_threshold: Decimal
    oi_pcr_bullish_threshold: Decimal
    volume_pcr_bearish_threshold: Decimal
    volume_pcr_bullish_threshold: Decimal
    log_level: str

    @property
    def frame_interval_seconds(self) -> float:
        return 1.0 / self.ui_refresh_hz

    @property
    def broker_credentials_configured(self) -> bool:
        return all(
            value.strip()
            for value in (
                self.angleone_api_key,
                self.angleone_client_code,
                self.angleone_password,
                self.angleone_totp_secret,
            )
        )

    @property
    def live_execution_enabled(self) -> bool:
        return self.order_execution_mode == "live"


def load_settings(
    *,
    simulator_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load bot fallback, simulator override, then process-environment values."""

    root = (simulator_root or Path(__file__).resolve().parents[2]).resolve()
    process_values = dict(os.environ if environ is None else environ)
    simulator_values = _read_dotenv(root / ".env")

    raw_bot_root = process_values.get(
        "KTRADER_BOT_ROOT",
        simulator_values.get("KTRADER_BOT_ROOT", ".."),
    )
    bot_root = _resolve_path(root, raw_bot_root)
    raw_bot_env_path = process_values.get(
        "KTRADER_BOT_ENV_PATH",
        simulator_values.get("KTRADER_BOT_ENV_PATH", ".env"),
    )
    bot_env_path = _resolve_path(bot_root, raw_bot_env_path)

    values = _read_dotenv(bot_env_path)
    values.update(simulator_values)
    values.update(process_values)

    supported_indices = _csv_upper(
        values,
        "KTRADER_SUPPORTED_INDICES",
        "NIFTY,SENSEX,BANKNIFTY,BANKEX",
    )
    invalid_indices = set(supported_indices) - {
        "NIFTY",
        "SENSEX",
        "BANKNIFTY",
        "BANKEX",
    }
    if invalid_indices:
        invalid = ", ".join(sorted(invalid_indices))
        raise ConfigurationError(f"Unsupported KTRADER_SUPPORTED_INDICES value(s): {invalid}")

    default_index = _text(values, "KTRADER_DEFAULT_INDEX", "NIFTY").upper()
    if default_index not in supported_indices:
        raise ConfigurationError(
            "KTRADER_DEFAULT_INDEX must be present in KTRADER_SUPPORTED_INDICES"
        )

    market_timezone = _text(values, "MARKET_TIMEZONE", "Asia/Kolkata")
    try:
        ZoneInfo(market_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"Unknown MARKET_TIMEZONE: {market_timezone}") from exc

    viewport_height = _integer(
        values,
        "KTRADER_VIEWPORT_HEIGHT",
        820,
        minimum=600,
        maximum=4320,
    )
    top_panel_height = _integer(
        values,
        "KTRADER_TOP_PANEL_HEIGHT",
        520,
        minimum=300,
        maximum=4000,
    )
    if top_panel_height >= viewport_height - 120:
        raise ConfigurationError(
            "KTRADER_TOP_PANEL_HEIGHT must leave at least 120 pixels for the portfolio"
        )

    data_dir = _resolve_path(
        root,
        _text(values, "KTRADER_DATA_DIR", "data"),
    )
    bot_order_intake_enabled = _boolean(
        values,
        "KTRADER_BOT_ORDER_INTAKE_ENABLED",
        False,
    )
    legacy_broker_execution_enabled = _boolean(
        values,
        "BROKER_ORDER_EXECUTION_ENABLED",
        False,
    )
    order_execution_mode = _choice(
        values,
        "KTRADER_ORDER_EXECUTION_MODE",
        "live" if legacy_broker_execution_enabled else "shadow",
        {"shadow", "live"},
        normalize=str.lower,
    )
    oi_pcr_bearish_threshold = _decimal(
        values,
        "KTRADER_OI_PCR_BEARISH_THRESHOLD",
        Decimal("0.95"),
        minimum=Decimal("0"),
    )
    oi_pcr_bullish_threshold = _decimal(
        values,
        "KTRADER_OI_PCR_BULLISH_THRESHOLD",
        Decimal("1.05"),
        minimum=Decimal("0"),
    )
    volume_pcr_bearish_threshold = _decimal(
        values,
        "KTRADER_VOLUME_PCR_BEARISH_THRESHOLD",
        Decimal("0.90"),
        minimum=Decimal("0"),
    )
    volume_pcr_bullish_threshold = _decimal(
        values,
        "KTRADER_VOLUME_PCR_BULLISH_THRESHOLD",
        Decimal("1.10"),
        minimum=Decimal("0"),
    )
    if oi_pcr_bearish_threshold >= oi_pcr_bullish_threshold:
        raise ConfigurationError(
            "KTRADER_OI_PCR_BEARISH_THRESHOLD must be lower than "
            "KTRADER_OI_PCR_BULLISH_THRESHOLD"
        )
    if volume_pcr_bearish_threshold >= volume_pcr_bullish_threshold:
        raise ConfigurationError(
            "KTRADER_VOLUME_PCR_BEARISH_THRESHOLD must be lower than "
            "KTRADER_VOLUME_PCR_BULLISH_THRESHOLD"
        )

    return Settings(
        simulator_root=root,
        bot_root=bot_root,
        bot_env_path=bot_env_path,
        data_dir=data_dir,
        broker_name=_choice(
            values,
            "BROKER_NAME",
            "angleone",
            {"angleone"},
            normalize=str.lower,
        ),
        angleone_api_key=_text(values, "ANGLEONE_API_KEY", ""),
        angleone_client_code=_text(values, "ANGLEONE_CLIENT_CODE", ""),
        angleone_password=_text(values, "ANGLEONE_PASSWORD", ""),
        angleone_totp_secret=_text(values, "ANGLEONE_TOTP_SECRET", ""),
        angleone_instrument_master_url=_text(
            values,
            "ANGLEONE_INSTRUMENT_MASTER_URL",
            "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        ),
        angleone_instrument_master_path=_text(
            values,
            "ANGLEONE_INSTRUMENT_MASTER_PATH",
            "",
        ),
        angleone_http_timeout_seconds=float(
            _decimal(
                values,
                "ANGLEONE_HTTP_TIMEOUT_SECONDS",
                Decimal("2.0"),
                minimum=Decimal("0.1"),
                maximum=Decimal("60"),
            )
        ),
        broker_order_execution_enabled=order_execution_mode == "live",
        order_execution_mode=order_execution_mode,
        broker_order_product_type=_choice(
            values,
            "KTRADER_BROKER_ORDER_PRODUCT_TYPE",
            "INTRADAY",
            {"INTRADAY", "CARRYFORWARD"},
            normalize=str.upper,
        ),
        broker_order_variety=_choice(
            values,
            "KTRADER_BROKER_ORDER_VARIETY",
            "NORMAL",
            {"NORMAL"},
            normalize=str.upper,
        ),
        broker_order_duration=_choice(
            values,
            "KTRADER_BROKER_ORDER_DURATION",
            "DAY",
            {"DAY", "IOC"},
            normalize=str.upper,
        ),
        market_timezone=market_timezone,
        storage_backend=_choice(
            values,
            "STORAGE_BACKEND",
            "jsonl",
            {"jsonl", "redis", "auto"},
            normalize=str.lower,
        ),
        operational_tick_journal_enabled=_boolean(
            values,
            "OPERATIONAL_TICK_JOURNAL_ENABLED",
            False,
        ),
        operational_chain_journal_enabled=_boolean(
            values,
            "OPERATIONAL_CHAIN_JOURNAL_ENABLED",
            False,
        ),
        market_data_queue_capacity=_integer(
            values,
            "MARKET_DATA_QUEUE_CAPACITY",
            8192,
            minimum=128,
            maximum=1_000_000,
        ),
        market_data_queue_pressure_ratio=_decimal(
            values,
            "MARKET_DATA_QUEUE_PRESSURE_RATIO",
            Decimal("0.80"),
            minimum=Decimal("0.10"),
            maximum=Decimal("1.00"),
        ),
        runtime_metrics_sample_capacity=_integer(
            values,
            "RUNTIME_METRICS_SAMPLE_CAPACITY",
            2048,
            minimum=128,
            maximum=1_000_000,
        ),
        market_data_price_source=_text(
            values,
            "MARKET_DATA_PRICE_SOURCE",
            "websocket_snap_quote",
        ),
        market_data_oi_source=_text(
            values,
            "MARKET_DATA_OI_SOURCE",
            "websocket_snap_quote",
        ),
        market_data_greeks_source=_text(
            values,
            "MARKET_DATA_GREEKS_SOURCE",
            "option_greek",
        ),
        market_data_ws_mode=_text(values, "MARKET_DATA_WS_MODE", "SNAP_QUOTE").upper(),
        option_greeks_enabled=_boolean(values, "OPTION_GREEKS_ENABLED", True),
        microstructure_enabled=_boolean(values, "MICROSTRUCTURE_ENABLED", True),
        microstructure_mode=_choice(
            values,
            "MICROSTRUCTURE_MODE",
            "shadow",
            {"off", "shadow", "live"},
            normalize=str.lower,
        ),
        broker_pcr_enabled=_boolean(values, "BROKER_PCR_ENABLED", True),
        broker_oi_buildup_enabled=_boolean(values, "BROKER_OI_BUILDUP_ENABLED", True),
        app_title=_text(
            values,
            "KTRADER_APP_TITLE",
            "Options Trading Bot Dashboard (Pro Layout)",
        ),
        viewport_width=_integer(
            values,
            "KTRADER_VIEWPORT_WIDTH",
            1280,
            minimum=800,
            maximum=7680,
        ),
        viewport_height=viewport_height,
        viewport_resizable=_boolean(values, "KTRADER_VIEWPORT_RESIZABLE", True),
        viewport_start_maximized=_boolean(
            values, "KTRADER_VIEWPORT_START_MAXIMIZED", True
        ),
        viewport_vsync=_boolean(values, "KTRADER_VIEWPORT_VSYNC", True),
        top_panel_height=top_panel_height,
        left_panel_ratio=_decimal(
            values,
            "KTRADER_LEFT_PANEL_RATIO",
            Decimal("0.58"),
            minimum=Decimal("0.30"),
            maximum=Decimal("0.75"),
        ),
        ui_refresh_hz=_integer(
            values,
            "KTRADER_UI_REFRESH_HZ",
            20,
            minimum=1,
            maximum=240,
        ),
        auto_connect=_boolean(values, "KTRADER_AUTO_CONNECT", True),
        quote_mode=_choice(
            values,
            "KTRADER_QUOTE_MODE",
            "FULL",
            {"LTP", "OHLC", "FULL"},
            normalize=str.upper,
        ),
        quote_refresh_ms=_integer(
            values,
            "KTRADER_QUOTE_REFRESH_MS",
            1000,
            minimum=250,
            maximum=60_000,
        ),
        broker_io_workers=_integer(
            values,
            "KTRADER_BROKER_IO_WORKERS",
            2,
            minimum=1,
            maximum=8,
        ),
        broker_retry_seconds=_decimal(
            values,
            "KTRADER_BROKER_RETRY_SECONDS",
            Decimal("5"),
            minimum=Decimal("1"),
            maximum=Decimal("300"),
        ),
        max_consecutive_quote_errors=_integer(
            values,
            "KTRADER_MAX_CONSECUTIVE_QUOTE_ERRORS",
            3,
            minimum=1,
            maximum=100,
        ),
        supported_indices=supported_indices,
        default_index=default_index,
        starting_balance=_decimal(
            values,
            "KTRADER_STARTING_BALANCE",
            Decimal("100000.00"),
            minimum=Decimal("0.01"),
        ),
        default_lots=_integer(
            values,
            "KTRADER_DEFAULT_LOTS",
            1,
            minimum=1,
            maximum=100_000,
        ),
        default_order_type=_choice(
            values,
            "KTRADER_DEFAULT_ORDER_TYPE",
            "LIMIT",
            {"MARKET", "LIMIT"},
            normalize=str.upper,
        ),
        default_limit_price=_decimal(
            values,
            "KTRADER_DEFAULT_LIMIT_PRICE",
            Decimal("0.00"),
            minimum=Decimal("0"),
        ),
        default_buy_price_offset=_decimal(
            values,
            "KTRADER_DEFAULT_BUY_PRICE_OFFSET",
            Decimal("0.10"),
            minimum=Decimal("0"),
            maximum=Decimal("100"),
        ),
        default_target_percent=_decimal(
            values,
            "KTRADER_DEFAULT_TARGET_PERCENT",
            Decimal("10.00"),
            minimum=Decimal("0"),
            maximum=Decimal("1000"),
        ),
        default_stop_loss_percent=_decimal(
            values,
            "KTRADER_DEFAULT_STOP_LOSS_PERCENT",
            Decimal("0.00"),
            minimum=Decimal("0"),
            maximum=Decimal("100"),
        ),
        default_trailing_sl_percent=_decimal(
            values,
            "KTRADER_DEFAULT_TRAILING_SL_PERCENT",
            Decimal("0.00"),
            minimum=Decimal("0"),
            maximum=Decimal("100"),
        ),
        max_capital_utilization=_decimal(
            values,
            "KTRADER_MAX_CAPITAL_UTILIZATION",
            Decimal("1.00"),
            minimum=Decimal("0.01"),
            maximum=Decimal("1.00"),
        ),
        charges_buffer_percent=_decimal(
            values,
            "KTRADER_CHARGES_BUFFER_PERCENT",
            Decimal("0.00"),
            minimum=Decimal("0"),
            maximum=Decimal("100"),
        ),
        slippage_points=_decimal(
            values,
            "KTRADER_SLIPPAGE_POINTS",
            Decimal("0.00"),
            minimum=Decimal("0"),
        ),
        feed_stale_seconds=_decimal(
            values,
            "KTRADER_FEED_STALE_SECONDS",
            Decimal("3.00"),
            minimum=Decimal("0.10"),
            maximum=Decimal("300"),
        ),
        bot_order_intake_enabled=bot_order_intake_enabled,
        bot_ipc_endpoint=_text(values, "KTRADER_BOT_IPC_ENDPOINT", "KTraderUI"),
        bot_ipc_host=_choice(
            values,
            "KTRADER_BOT_IPC_HOST",
            "127.0.0.1",
            {"127.0.0.1", "::1", "localhost"},
            normalize=str,
        ),
        bot_ipc_port=_integer(
            values,
            "KTRADER_BOT_IPC_PORT",
            47821,
            minimum=1,
            maximum=65_535,
        ),
        bot_ipc_queue_capacity=_integer(
            values,
            "KTRADER_BOT_IPC_QUEUE_CAPACITY",
            1024,
            minimum=16,
            maximum=100_000,
        ),
        trade_ledger_path=_resolve_path(
            root,
            _text(
                values,
                "KTRADER_TRADE_LEDGER_PATH",
                str(data_dir / "trade_ledger.jsonl"),
            ),
        ),
        trade_ledger_fsync=_boolean(values, "KTRADER_TRADE_LEDGER_FSYNC", True),
        ledger_queue_capacity=_integer(
            values,
            "KTRADER_LEDGER_QUEUE_CAPACITY",
            1024,
            minimum=16,
            maximum=100_000,
        ),
        session_recovery_enabled=_boolean(
            values,
            "KTRADER_SESSION_RECOVERY_ENABLED",
            True,
        ),
        bot_signal_max_age_seconds=_decimal(
            values,
            "KTRADER_BOT_SIGNAL_MAX_AGE_SECONDS",
            Decimal("30"),
            minimum=Decimal("1"),
            maximum=Decimal("3600"),
        ),
        max_accepted_trades_per_day=_integer(
            values,
            "KTRADER_MAX_ACCEPTED_TRADES_PER_DAY",
            2,
            minimum=1,
            maximum=10_000,
        ),
        eod_close_time=_market_time(values, "KTRADER_EOD_CLOSE_TIME", "15:15"),
        trade_journal_dir=_resolve_path(
            root,
            _text(values, "KTRADER_TRADE_JOURNAL_DIR", "trade_journal"),
        ),
        chain_analytics_refresh_seconds=_integer(
            values,
            "KTRADER_CHAIN_ANALYTICS_REFRESH_SECONDS",
            180,
            minimum=10,
            maximum=3600,
        ),
        oi_pcr_bearish_threshold=oi_pcr_bearish_threshold,
        oi_pcr_bullish_threshold=oi_pcr_bullish_threshold,
        volume_pcr_bearish_threshold=volume_pcr_bearish_threshold,
        volume_pcr_bullish_threshold=volume_pcr_bullish_threshold,
        log_level=_choice(
            values,
            "KTRADER_LOG_LEVEL",
            "INFO",
            {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
            normalize=str.upper,
        ),
    )


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        return {key: value for key, value in dotenv_values(path).items() if value is not None}
    except OSError as exc:
        raise ConfigurationError(f"Unable to read environment file: {path}") from exc


def _resolve_path(base: Path, raw_value: str) -> Path:
    path = Path(raw_value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _text(values: Mapping[str, str], key: str, default: str) -> str:
    value = values.get(key, default).strip()
    if not value:
        return default
    return value


def _boolean(values: Mapping[str, str], key: str, default: bool) -> bool:
    raw_value = values.get(key)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{key} must be true or false")


def _integer(
    values: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw_value = values.get(key, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{key} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{key} must be at most {maximum}")
    return value


def _decimal(
    values: Mapping[str, str],
    key: str,
    default: Decimal,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    raw_value = values.get(key, str(default)).strip()
    try:
        value = Decimal(raw_value)
    except InvalidOperation as exc:
        raise ConfigurationError(f"{key} must be numeric") from exc
    if not value.is_finite():
        raise ConfigurationError(f"{key} must be finite")
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{key} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{key} must be at most {maximum}")
    return value


def _csv_upper(
    values: Mapping[str, str],
    key: str,
    default: str,
) -> tuple[str, ...]:
    raw_value = values.get(key, default)
    entries = tuple(
        dict.fromkeys(part.strip().upper() for part in raw_value.split(",") if part.strip())
    )
    if not entries:
        raise ConfigurationError(f"{key} must contain at least one value")
    return entries


def _choice(
    values: Mapping[str, str],
    key: str,
    default: str,
    allowed: set[str],
    *,
    normalize: Callable[[str], str],
) -> str:
    value = normalize(values.get(key, default).strip())
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ConfigurationError(f"{key} must be one of: {choices}")
    return value


def _market_time(values: Mapping[str, str], key: str, default: str) -> str:
    value = _text(values, key, default)
    try:
        hour_text, minute_text = value.split(":", maxsplit=1)
        hour, minute = int(hour_text), int(minute_text)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must use HH:MM format") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigurationError(f"{key} must be a valid 24-hour time")
    return f"{hour:02d}:{minute:02d}"
