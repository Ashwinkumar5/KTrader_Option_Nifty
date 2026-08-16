from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum

from dotenv import load_dotenv

# Load environment variables from .env file at application startup
load_dotenv()


class BrokerName(StrEnum):
    ANGLEONE = "angleone"
    DHAN = "dhan"
    ZERODHA = "zerodha"


def _csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    items = tuple(item.strip().upper() for item in value.split(",") if item.strip())
    return items or default


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_env: str
    log_level: str
    angleone_api_key: str
    angleone_client_code: str
    angleone_password: str
    angleone_totp_secret: str
    angleone_instrument_master_url: str
    angleone_instrument_master_path: str
    redis_url: str
    database_url: str
    local_storage_dir: str
    default_underlyings: tuple[str, ...]
    option_window_each_side: int
    snapshot_interval_ms: int
    storage_backend: str
    broker_name: str
    market_data_price_source: str
    market_data_oi_source: str
    market_data_greeks_source: str
    market_data_ws_mode: str
    option_greeks_enabled: bool
    broker_pcr_enabled: bool
    broker_oi_buildup_enabled: bool
    angleone_http_timeout_seconds: float = 2.0
    strategy_config_path: str = ""
    strategy_profile: str = "derivatives_only"
    pcr_bullish_threshold: float = 1.5
    pcr_bearish_threshold: float = 0.7
    microstructure_enabled: bool = True
    microstructure_mode: str = "shadow"
    microstructure_window_seconds: int = 3
    microstructure_min_events: int = 4
    microstructure_min_imbalance: float = 0.25
    microstructure_min_velocity: float = 0.75
    microstructure_max_spread_points: float = 1.50
    signal_gate_min_confirmations: int = 3
    signal_gate_cooldown_seconds: int = 60
    local_reversal_cooldown_seconds: int = 900
    signal_gate_level_distance_points: float = 10.0
    signal_gate_min_micro_confidence: float = 0.40
    signal_gate_min_score: float = 80.0
    signal_gate_straddle_zone_ratio: float = 0.10
    signal_gate_min_range_room_points: float = 20.0
    signal_gate_min_directional_confirmations: int = 2
    signal_gate_min_independent_confirmation_families: int = 2
    signal_gate_require_complete_chain: bool = True
    signal_gate_min_chain_quotes: int = 6
    signal_gate_require_greeks: bool = True
    signal_gate_require_target_contract: bool = True
    signal_gate_max_underlying_age_seconds: int = 3
    premium_transmission_enabled: bool = True
    premium_transmission_min_expected_return_percent: float = 3.0
    premium_transmission_min_ratio: float = 0.35
    signal_debounce_frame_seconds: int = 15
    signal_debounce_window_frames: int = 3
    signal_debounce_min_confirmed_frames: int = 2
    range_soft_breach_frames: int = 2
    range_hard_invalidation_points: float = 5.0
    range_recovery_buffer_points: float = 2.0
    structural_level_frame_seconds: int = 240
    feature_opening_context_enabled: bool = True
    feature_opening_context_sequence: int = 10
    feature_expected_move_enabled: bool = True
    feature_expected_move_sequence: int = 20
    feature_premium_response_enabled: bool = True
    feature_premium_response_sequence: int = 30
    feature_futures_flow_enabled: bool = True
    feature_futures_flow_sequence: int = 35
    feature_candle_patterns_enabled: bool = True
    feature_candle_patterns_sequence: int = 37
    feature_momentum_exhaustion_enabled: bool = True
    feature_momentum_exhaustion_sequence: int = 40
    opening_observation_minutes: int = 15
    expected_move_capture_time: str = "09:45:00"
    expected_move_first_band_ratio: float = 0.50
    expected_move_extended_band_ratio: float = 0.80
    expected_move_exhaustion_band_ratio: float = 1.00
    exhaustion_earliest_time: str = "13:15:00"
    exhaustion_minimum_premium_return_percent: float = 75.0
    exhaustion_minimum_move_utilization: float = 0.80
    gamma_window_seconds: int = 300
    regime_window_seconds: int = 300
    futures_flow_window_seconds: int = 60
    reversal_candle_confirmation_required: bool = False
    strategy_resolver_policy: str = "REGIME_EXCLUSIVE"
    strategy_level_reversal_enabled: bool = True
    strategy_breakout_momentum_enabled: bool = True
    strategy_gamma_expansion_enabled: bool = True
    strategy_level_reversal_priority: int = 10
    strategy_breakout_momentum_priority: int = 20
    strategy_gamma_expansion_priority: int = 30
    risk_enforce_session: bool = True
    risk_max_daily_loss: float = 2000.0
    risk_max_concurrent_positions: int = 1
    risk_max_gross_exposure: float = 100000.0
    execution_account_capital: float = 100000.0
    execution_risk_per_trade_percent: float = 0.50
    replay_capture_enabled: bool = True
    replay_capture_file_prefix: str = "broker_replay_tape"
    replay_require_complete_window: bool = True
    market_timezone: str = "Asia/Kolkata"
    broker_adapter_module: str = ""
    broker_config: dict[str, str] = field(default_factory=dict)
    operational_tick_journal_enabled: bool = False
    operational_chain_journal_enabled: bool = False
    market_data_queue_capacity: int = 8192
    market_data_queue_pressure_ratio: float = 0.80
    runtime_metrics_sample_capacity: int = 2048
    simulator_ipc_enabled: bool = True
    simulator_ipc_endpoint: str = "KTraderUI"
    simulator_ipc_host: str = "127.0.0.1"
    simulator_ipc_port: int = 47821
    simulator_ipc_queue_capacity: int = 64
    simulator_ipc_timeout_seconds: float = 0.50
    simulator_ipc_max_retries: int = 2
    signal_router_enabled: bool = True
    signal_router_host: str = "127.0.0.1"
    signal_router_port: int = 47820
    signal_router_queue_capacity: int = 256
    signal_router_timeout_seconds: float = 0.50
    signal_router_max_retries: int = 5
    signal_router_dedup_capacity: int = 4096
    signal_router_audit_path: str = "data/signal_router_audit.jsonl"
    nats_url: str = "nats://127.0.0.1:4222"
    market_data_subject_prefix: str = "ktrader.marketdata.v1"
    market_data_bus_queue_capacity: int = 8192
    market_data_bootstrap_timeout_seconds: float = 15.0
    market_data_feed_interval_ms: int = 5000
    market_data_feed_tape_directory: str = "data/feed_handler"

    @property
    def broker_credentials_configured(self) -> bool:
        """Return whether the configured Angle One login credentials are usable."""

        return all(
            value.strip()
            for value in (
                self.angleone_api_key,
                self.angleone_client_code,
                self.angleone_password,
                self.angleone_totp_secret,
            )
        )

def load_settings() -> Settings:
    broker_name = (
        os.getenv("BROKER_NAME")
        or BrokerName.ANGLEONE.value
    ).strip().lower()
    broker_prefix = f"{broker_name.upper()}_"
    broker_config = {
        key[len(broker_prefix):]: value
        for key, value in os.environ.items()
        if key.upper().startswith(broker_prefix)
    }
    local_storage_dir = os.getenv("LOCAL_STORAGE_DIR", "data")
    return Settings(
        app_name=os.getenv("APP_NAME", "options-analytics-platform"),
        app_env=os.getenv("APP_ENV", "local"),
        log_level=str(os.getenv("LOG_LEVEL", "INFO") or "INFO").upper(),
        angleone_api_key=os.getenv("ANGLEONE_API_KEY", ""),
        angleone_client_code=os.getenv("ANGLEONE_CLIENT_CODE", ""),
        angleone_password=os.getenv("ANGLEONE_PASSWORD", ""),
        angleone_totp_secret=os.getenv("ANGLEONE_TOTP_SECRET", ""),
        angleone_instrument_master_url=os.getenv(
            "ANGLEONE_INSTRUMENT_MASTER_URL",
            "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        ),
        angleone_instrument_master_path=os.getenv("ANGLEONE_INSTRUMENT_MASTER_PATH", ""),
        angleone_http_timeout_seconds=float(
            os.getenv("ANGLEONE_HTTP_TIMEOUT_SECONDS", "2.0")
        ),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6380/0"),
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://postgres:postgres@localhost:5432/options_platform",
        ),
        local_storage_dir=local_storage_dir,
        storage_backend=os.getenv("STORAGE_BACKEND", "").lower(),
        default_underlyings=_csv(
            os.getenv("DEFAULT_UNDERLYINGS", "NIFTY"),
            ("NIFTY",),
        ),
        option_window_each_side=int(os.getenv("OPTION_WINDOW_EACH_SIDE", "4")),
        snapshot_interval_ms=int(os.getenv("SNAPSHOT_INTERVAL_MS", "1000")),
        broker_name=broker_name,
        market_data_price_source=os.getenv("MARKET_DATA_PRICE_SOURCE", "websocket_snap_quote"),
        market_data_oi_source=os.getenv("MARKET_DATA_OI_SOURCE", "websocket_snap_quote"),
        market_data_greeks_source=os.getenv("MARKET_DATA_GREEKS_SOURCE", "option_greek"),
        market_data_ws_mode=os.getenv("MARKET_DATA_WS_MODE", "SNAP_QUOTE"),
        option_greeks_enabled=os.getenv("OPTION_GREEKS_ENABLED", "true").lower() == "true",
        broker_pcr_enabled=os.getenv("BROKER_PCR_ENABLED", "true").lower() == "true",
        broker_oi_buildup_enabled=os.getenv("BROKER_OI_BUILDUP_ENABLED", "true").lower() == "true",
        strategy_config_path=os.getenv("STRATEGY_CONFIG_PATH", ""),
        strategy_profile=os.getenv(
            "STRATEGY_PROFILE", "derivatives_only"
        ).strip(),
        pcr_bullish_threshold=float(os.getenv("PCR_BULLISH_THRESHOLD", "1.5")),
        pcr_bearish_threshold=float(os.getenv("PCR_BEARISH_THRESHOLD", "0.7")),
        # Shadow is deliberately the default: this project records and validates
        # signals but never turns a research signal into a broker order.
        microstructure_enabled=os.getenv("MICROSTRUCTURE_ENABLED", "true").lower() == "true",
        microstructure_mode=os.getenv("MICROSTRUCTURE_MODE", "shadow").lower(),
        microstructure_window_seconds=int(os.getenv("MICROSTRUCTURE_WINDOW_SECONDS", "3")),
        microstructure_min_events=int(os.getenv("MICROSTRUCTURE_MIN_EVENTS", "4")),
        microstructure_min_imbalance=float(os.getenv("MICROSTRUCTURE_MIN_IMBALANCE", "0.25")),
        microstructure_min_velocity=float(os.getenv("MICROSTRUCTURE_MIN_VELOCITY", "0.75")),
        microstructure_max_spread_points=float(os.getenv("MICROSTRUCTURE_MAX_SPREAD_POINTS", "1.50")),
        signal_gate_min_confirmations=int(os.getenv("SIGNAL_GATE_MIN_CONFIRMATIONS", "3")),
        signal_gate_cooldown_seconds=int(os.getenv("SIGNAL_GATE_COOLDOWN_SECONDS", "60")),
        local_reversal_cooldown_seconds=int(
            os.getenv("LOCAL_REVERSAL_COOLDOWN_SECONDS", "900")
        ),
        signal_gate_level_distance_points=float(os.getenv("SIGNAL_GATE_LEVEL_DISTANCE_POINTS", "10")),
        signal_gate_min_micro_confidence=float(
            os.getenv("SIGNAL_GATE_MIN_MICRO_CONFIDENCE", "0.40")
        ),
        signal_gate_min_score=float(os.getenv("SIGNAL_GATE_MIN_SCORE", "80")),
        signal_gate_straddle_zone_ratio=float(
            os.getenv("SIGNAL_GATE_STRADDLE_ZONE_RATIO", "0.10")
        ),
        signal_gate_min_range_room_points=float(
            os.getenv("SIGNAL_GATE_MIN_RANGE_ROOM_POINTS", "20")
        ),
        signal_gate_min_directional_confirmations=int(
            os.getenv("SIGNAL_GATE_MIN_DIRECTIONAL_CONFIRMATIONS", "2")
        ),
        signal_gate_min_independent_confirmation_families=int(
            os.getenv(
                "SIGNAL_GATE_MIN_INDEPENDENT_CONFIRMATION_FAMILIES",
                "2",
            )
        ),
        signal_gate_require_complete_chain=os.getenv(
            "SIGNAL_GATE_REQUIRE_COMPLETE_CHAIN", "true"
        ).lower()
        == "true",
        signal_gate_min_chain_quotes=int(
            os.getenv("SIGNAL_GATE_MIN_CHAIN_QUOTES", "6")
        ),
        signal_gate_require_greeks=os.getenv(
            "SIGNAL_GATE_REQUIRE_GREEKS", "true"
        ).lower()
        == "true",
        signal_gate_require_target_contract=os.getenv(
            "SIGNAL_GATE_REQUIRE_TARGET_CONTRACT", "true"
        ).lower()
        == "true",
        signal_gate_max_underlying_age_seconds=int(
            os.getenv("SIGNAL_GATE_MAX_UNDERLYING_AGE_SECONDS", "3")
        ),
        premium_transmission_enabled=os.getenv(
            "PREMIUM_TRANSMISSION_ENABLED", "true"
        ).lower()
        == "true",
        premium_transmission_min_expected_return_percent=float(
            os.getenv(
                "PREMIUM_TRANSMISSION_MIN_EXPECTED_RETURN_PERCENT",
                "3",
            )
        ),
        premium_transmission_min_ratio=float(
            os.getenv("PREMIUM_TRANSMISSION_MIN_RATIO", "0.35")
        ),
        signal_debounce_frame_seconds=int(
            os.getenv("SIGNAL_DEBOUNCE_FRAME_SECONDS", "15")
        ),
        signal_debounce_window_frames=int(
            os.getenv("SIGNAL_DEBOUNCE_WINDOW_FRAMES", "3")
        ),
        signal_debounce_min_confirmed_frames=int(
            os.getenv("SIGNAL_DEBOUNCE_MIN_CONFIRMED_FRAMES", "2")
        ),
        range_soft_breach_frames=int(
            os.getenv("RANGE_SOFT_BREACH_FRAMES", "2")
        ),
        range_hard_invalidation_points=float(
            os.getenv("RANGE_HARD_INVALIDATION_POINTS", "5")
        ),
        range_recovery_buffer_points=float(
            os.getenv("RANGE_RECOVERY_BUFFER_POINTS", "2")
        ),
        structural_level_frame_seconds=int(
            os.getenv("STRUCTURAL_LEVEL_FRAME_SECONDS", "240")
        ),
        feature_opening_context_enabled=os.getenv(
            "FEATURE_OPENING_CONTEXT_ENABLED", "true"
        ).lower()
        == "true",
        feature_opening_context_sequence=int(
            os.getenv("FEATURE_OPENING_CONTEXT_SEQUENCE", "10")
        ),
        feature_expected_move_enabled=os.getenv(
            "FEATURE_EXPECTED_MOVE_ENABLED", "true"
        ).lower()
        == "true",
        feature_expected_move_sequence=int(
            os.getenv("FEATURE_EXPECTED_MOVE_SEQUENCE", "20")
        ),
        feature_premium_response_enabled=os.getenv(
            "FEATURE_PREMIUM_RESPONSE_ENABLED", "true"
        ).lower()
        == "true",
        feature_premium_response_sequence=int(
            os.getenv("FEATURE_PREMIUM_RESPONSE_SEQUENCE", "30")
        ),
        feature_futures_flow_enabled=os.getenv(
            "FEATURE_FUTURES_FLOW_ENABLED", "true"
        ).lower()
        == "true",
        feature_futures_flow_sequence=int(
            os.getenv("FEATURE_FUTURES_FLOW_SEQUENCE", "35")
        ),
        feature_candle_patterns_enabled=os.getenv(
            "FEATURE_CANDLE_PATTERNS_ENABLED", "true"
        ).lower()
        == "true",
        feature_candle_patterns_sequence=int(
            os.getenv("FEATURE_CANDLE_PATTERNS_SEQUENCE", "37")
        ),
        feature_momentum_exhaustion_enabled=os.getenv(
            "FEATURE_MOMENTUM_EXHAUSTION_ENABLED", "true"
        ).lower()
        == "true",
        feature_momentum_exhaustion_sequence=int(
            os.getenv("FEATURE_MOMENTUM_EXHAUSTION_SEQUENCE", "40")
        ),
        opening_observation_minutes=int(
            os.getenv("OPENING_OBSERVATION_MINUTES", "15")
        ),
        expected_move_capture_time=os.getenv(
            "EXPECTED_MOVE_CAPTURE_TIME", "09:45:00"
        ),
        expected_move_first_band_ratio=float(
            os.getenv("EXPECTED_MOVE_FIRST_BAND_RATIO", "0.50")
        ),
        expected_move_extended_band_ratio=float(
            os.getenv("EXPECTED_MOVE_EXTENDED_BAND_RATIO", "0.80")
        ),
        expected_move_exhaustion_band_ratio=float(
            os.getenv("EXPECTED_MOVE_EXHAUSTION_BAND_RATIO", "1.00")
        ),
        exhaustion_earliest_time=os.getenv(
            "EXHAUSTION_EARLIEST_TIME", "13:15:00"
        ),
        exhaustion_minimum_premium_return_percent=float(
            os.getenv(
                "EXHAUSTION_MINIMUM_PREMIUM_RETURN_PERCENT",
                "75",
            )
        ),
        exhaustion_minimum_move_utilization=float(
            os.getenv("EXHAUSTION_MINIMUM_MOVE_UTILIZATION", "0.80")
        ),
        gamma_window_seconds=int(
            os.getenv("GAMMA_WINDOW_SECONDS", "300")
        ),
        regime_window_seconds=int(
            os.getenv("REGIME_WINDOW_SECONDS", "300")
        ),
        futures_flow_window_seconds=int(
            os.getenv("FUTURES_FLOW_WINDOW_SECONDS", "60")
        ),
        reversal_candle_confirmation_required=os.getenv(
            "REVERSAL_CANDLE_CONFIRMATION_REQUIRED", "false"
        ).lower()
        == "true",
        strategy_resolver_policy=os.getenv(
            "STRATEGY_RESOLVER_POLICY",
            "REGIME_EXCLUSIVE",
        ).strip().upper(),
        strategy_level_reversal_enabled=os.getenv(
            "STRATEGY_LEVEL_REVERSAL_ENABLED",
            "true",
        ).lower()
        == "true",
        strategy_breakout_momentum_enabled=os.getenv(
            "STRATEGY_BREAKOUT_MOMENTUM_ENABLED",
            "true",
        ).lower()
        == "true",
        strategy_gamma_expansion_enabled=os.getenv(
            "STRATEGY_GAMMA_EXPANSION_ENABLED",
            "true",
        ).lower()
        == "true",
        strategy_level_reversal_priority=int(
            os.getenv("STRATEGY_LEVEL_REVERSAL_PRIORITY", "10")
        ),
        strategy_breakout_momentum_priority=int(
            os.getenv("STRATEGY_BREAKOUT_MOMENTUM_PRIORITY", "20")
        ),
        strategy_gamma_expansion_priority=int(
            os.getenv("STRATEGY_GAMMA_EXPANSION_PRIORITY", "30")
        ),
        risk_enforce_session=os.getenv(
            "RISK_ENFORCE_SESSION", "true"
        ).lower()
        == "true",
        risk_max_daily_loss=float(os.getenv("RISK_MAX_DAILY_LOSS", "2000")),
        risk_max_concurrent_positions=int(
            os.getenv("RISK_MAX_CONCURRENT_POSITIONS", "1")
        ),
        risk_max_gross_exposure=float(
            os.getenv("RISK_MAX_GROSS_EXPOSURE", "100000")
        ),
        execution_account_capital=float(
            os.getenv("EXECUTION_ACCOUNT_CAPITAL", "100000")
        ),
        execution_risk_per_trade_percent=float(
            os.getenv("EXECUTION_RISK_PER_TRADE_PERCENT", "0.50")
        ),
        replay_capture_enabled=os.getenv(
            "REPLAY_CAPTURE_ENABLED", "true"
        ).lower() == "true",
        replay_capture_file_prefix=os.getenv(
            "REPLAY_CAPTURE_FILE_PREFIX", "broker_replay_tape"
        ),
        replay_require_complete_window=os.getenv(
            "REPLAY_REQUIRE_COMPLETE_WINDOW", "true"
        ).lower() == "true",
        market_timezone=os.getenv("MARKET_TIMEZONE", "Asia/Kolkata"),
        broker_adapter_module=os.getenv("BROKER_ADAPTER_MODULE", ""),
        broker_config=broker_config,
        operational_tick_journal_enabled=os.getenv(
            "OPERATIONAL_TICK_JOURNAL_ENABLED", "false"
        ).lower()
        == "true",
        operational_chain_journal_enabled=os.getenv(
            "OPERATIONAL_CHAIN_JOURNAL_ENABLED", "false"
        ).lower()
        == "true",
        market_data_queue_capacity=max(
            int(os.getenv("MARKET_DATA_QUEUE_CAPACITY", "8192")),
            1,
        ),
        market_data_queue_pressure_ratio=min(
            max(
                float(
                    os.getenv(
                        "MARKET_DATA_QUEUE_PRESSURE_RATIO",
                        "0.80",
                    )
                ),
                0.10,
            ),
            1.0,
        ),
        runtime_metrics_sample_capacity=max(
            int(os.getenv("RUNTIME_METRICS_SAMPLE_CAPACITY", "2048")),
            1,
        ),
        simulator_ipc_enabled=os.getenv(
            "SIMULATOR_IPC_ENABLED", "true"
        ).lower()
        == "true",
        simulator_ipc_endpoint=os.getenv(
            "KTRADER_BOT_IPC_ENDPOINT", "KTraderUI"
        ).strip(),
        simulator_ipc_host=os.getenv(
            "KTRADER_BOT_IPC_HOST", "127.0.0.1"
        ).strip(),
        simulator_ipc_port=int(
            os.getenv("KTRADER_BOT_IPC_PORT", "47821")
        ),
        simulator_ipc_queue_capacity=max(
            int(os.getenv("SIMULATOR_IPC_QUEUE_CAPACITY", "64")),
            1,
        ),
        simulator_ipc_timeout_seconds=max(
            float(os.getenv("SIMULATOR_IPC_TIMEOUT_SECONDS", "0.50")),
            0.05,
        ),
        simulator_ipc_max_retries=max(
            int(os.getenv("SIMULATOR_IPC_MAX_RETRIES", "2")),
            0,
        ),
        signal_router_enabled=os.getenv(
            "SIGNAL_ROUTER_ENABLED", "true"
        ).lower()
        == "true",
        signal_router_host=os.getenv(
            "SIGNAL_ROUTER_HOST", "127.0.0.1"
        ).strip(),
        signal_router_port=int(
            os.getenv("SIGNAL_ROUTER_PORT", "47820")
        ),
        signal_router_queue_capacity=max(
            int(os.getenv("SIGNAL_ROUTER_QUEUE_CAPACITY", "256")),
            1,
        ),
        signal_router_timeout_seconds=max(
            float(os.getenv("SIGNAL_ROUTER_TIMEOUT_SECONDS", "0.50")),
            0.05,
        ),
        signal_router_max_retries=max(
            int(os.getenv("SIGNAL_ROUTER_MAX_RETRIES", "5")),
            0,
        ),
        signal_router_dedup_capacity=max(
            int(os.getenv("SIGNAL_ROUTER_DEDUP_CAPACITY", "4096")),
            1,
        ),
        signal_router_audit_path=os.getenv(
            "SIGNAL_ROUTER_AUDIT_PATH",
            os.path.join(local_storage_dir, "signal_router_audit.jsonl"),
        ).strip(),
        nats_url=(
            os.getenv("NATS_URL", "nats://127.0.0.1:4222").strip()
            or "nats://127.0.0.1:4222"
        ),
        market_data_subject_prefix=(
            os.getenv(
                "MARKET_DATA_SUBJECT_PREFIX",
                "ktrader.marketdata.v1",
            ).strip().strip(".")
            or "ktrader.marketdata.v1"
        ),
        market_data_bus_queue_capacity=max(
            int(os.getenv("MARKET_DATA_BUS_QUEUE_CAPACITY", "8192")),
            1,
        ),
        market_data_bootstrap_timeout_seconds=max(
            float(
                os.getenv(
                    "MARKET_DATA_BOOTSTRAP_TIMEOUT_SECONDS",
                    "15.0",
                )
            ),
            0.1,
        ),
        market_data_feed_interval_ms=max(
            int(os.getenv("MARKET_DATA_FEED_INTERVAL_MS", "5000")),
            1,
        ),
        market_data_feed_tape_directory=(
            os.getenv(
                "MARKET_DATA_FEED_TAPE_DIRECTORY",
                os.path.join(local_storage_dir, "feed_handler"),
            ).strip()
            or os.path.join(local_storage_dir, "feed_handler")
        ),
    )
