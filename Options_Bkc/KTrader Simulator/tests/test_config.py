from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from ktrader_simulator.config import ConfigurationError, load_settings


def _write_environment(root: Path) -> Path:
    simulator_root = root / "KTrader Simulator"
    simulator_root.mkdir()
    (root / ".env").write_text(
        "ANGLEONE_API_KEY=bot-key\n"
        "ANGLEONE_CLIENT_CODE=bot-client\n"
        "ANGLEONE_PASSWORD=bot-password\n"
        "ANGLEONE_TOTP_SECRET=bot-totp\n",
        encoding="utf-8",
    )
    (simulator_root / ".env").write_text(
        "ANGLEONE_API_KEY=simulator-key\n"
        "BROKER_ORDER_EXECUTION_ENABLED=false\n"
        "KTRADER_SUPPORTED_INDICES=NIFTY,SENSEX,BANKNIFTY,BANKEX\n"
        "KTRADER_BOT_ROOT=..\n",
        encoding="utf-8",
    )
    return simulator_root


def test_simulator_values_override_bot_values_and_os_is_highest(tmp_path: Path) -> None:
    simulator_root = _write_environment(tmp_path)

    settings = load_settings(
        simulator_root=simulator_root,
        environ={"KTRADER_DEFAULT_INDEX": "BANKEX"},
    )

    assert settings.angleone_api_key == "simulator-key"
    assert settings.angleone_client_code == "bot-client"
    assert settings.default_index == "BANKEX"
    assert settings.bot_root == tmp_path.resolve()
    assert settings.broker_order_execution_enabled is False
    assert settings.order_execution_mode == "shadow"
    assert settings.live_execution_enabled is False
    assert settings.angleone_http_timeout_seconds == 2.0
    assert settings.default_target_percent == Decimal("10.00")
    assert settings.default_buy_price_offset == Decimal("0.10")
    assert settings.default_stop_loss_percent == Decimal("0.00")
    assert settings.default_trailing_sl_percent == Decimal("0.00")
    assert settings.trade_ledger_path == simulator_root / "data" / "trade_ledger.jsonl"
    assert settings.trade_ledger_fsync is True
    assert settings.ledger_queue_capacity == 1024
    assert settings.broker_io_workers == 2
    assert settings.session_recovery_enabled is True
    assert settings.bot_signal_max_age_seconds == Decimal("30")
    assert settings.bot_ipc_endpoint == "KTraderUI"
    assert settings.bot_ipc_host == "127.0.0.1"
    assert settings.bot_ipc_port == 47821
    assert settings.oi_pcr_bearish_threshold == Decimal("0.95")
    assert settings.oi_pcr_bullish_threshold == Decimal("1.05")
    assert settings.volume_pcr_bearish_threshold == Decimal("0.90")
    assert settings.volume_pcr_bullish_threshold == Decimal("1.10")


def test_secrets_are_excluded_from_settings_representation(tmp_path: Path) -> None:
    simulator_root = _write_environment(tmp_path)

    settings = load_settings(simulator_root=simulator_root, environ={})
    representation = repr(settings)

    assert "simulator-key" not in representation
    assert "bot-password" not in representation
    assert "bot-totp" not in representation


def test_default_index_must_be_supported(tmp_path: Path) -> None:
    simulator_root = _write_environment(tmp_path)

    with pytest.raises(ConfigurationError, match="KTRADER_DEFAULT_INDEX"):
        load_settings(
            simulator_root=simulator_root,
            environ={"KTRADER_DEFAULT_INDEX": "FINNIFTY"},
        )


def test_live_order_flag_requires_a_strict_boolean(tmp_path: Path) -> None:
    simulator_root = _write_environment(tmp_path)

    with pytest.raises(ConfigurationError, match="BROKER_ORDER_EXECUTION_ENABLED"):
        load_settings(
            simulator_root=simulator_root,
            environ={"BROKER_ORDER_EXECUTION_ENABLED": "maybe"},
        )


def test_explicit_execution_mode_is_the_single_routing_authority(tmp_path: Path) -> None:
    simulator_root = _write_environment(tmp_path)

    settings = load_settings(
        simulator_root=simulator_root,
        environ={
            "BROKER_ORDER_EXECUTION_ENABLED": "false",
            "KTRADER_ORDER_EXECUTION_MODE": "live",
        },
    )

    assert settings.order_execution_mode == "live"
    assert settings.broker_order_execution_enabled is True
    assert settings.live_execution_enabled is True


def test_stop_loss_percentage_cannot_exceed_one_hundred(tmp_path: Path) -> None:
    simulator_root = _write_environment(tmp_path)

    with pytest.raises(ConfigurationError, match="KTRADER_DEFAULT_STOP_LOSS_PERCENT"):
        load_settings(
            simulator_root=simulator_root,
            environ={"KTRADER_DEFAULT_STOP_LOSS_PERCENT": "100.01"},
        )


def test_pcr_threshold_bands_cannot_overlap(tmp_path: Path) -> None:
    simulator_root = _write_environment(tmp_path)

    with pytest.raises(ConfigurationError, match="OI_PCR_BEARISH_THRESHOLD"):
        load_settings(
            simulator_root=simulator_root,
            environ={
                "KTRADER_OI_PCR_BEARISH_THRESHOLD": "1.05",
                "KTRADER_OI_PCR_BULLISH_THRESHOLD": "1.05",
            },
        )
