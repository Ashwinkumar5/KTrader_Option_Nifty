from __future__ import annotations

from app.broker.angleone.client import AngleOneClient
from app.broker.angleone.feed import AngleOneWebSocketFeed
from app.broker.angleone.instruments import build_instrument_master
from app.broker.interfaces import (
    BrokerClient,
    BrokerSession,
    MarketDataFeed,
)
from app.broker.registry import BrokerProvider
from app.core.config import Settings
from app.domain.models import InstrumentToken
from app.instruments.master import InstrumentMaster


def _create_client(settings: Settings) -> BrokerClient:
    return AngleOneClient(settings)


def _create_feed(
    settings: Settings,
    session: BrokerSession,
    token_lookup: dict[str, InstrumentToken],
) -> MarketDataFeed:
    return AngleOneWebSocketFeed(
        settings=settings,
        session=session,
        token_lookup=token_lookup,
    )


def _configuration_errors(settings: Settings) -> tuple[str, ...]:
    required = {
        "ANGLEONE_API_KEY": settings.angleone_api_key,
        "ANGLEONE_CLIENT_CODE": settings.angleone_client_code,
        "ANGLEONE_PASSWORD": settings.angleone_password,
        "ANGLEONE_TOTP_SECRET": settings.angleone_totp_secret,
    }
    return tuple(
        f"{name} is not configured"
        for name, value in required.items()
        if not value
    )


def _build_instrument_master(
    rows: list[dict[str, object]],
    underlyings: tuple[str, ...],
) -> InstrumentMaster:
    return build_instrument_master(rows, underlyings=underlyings)


BROKER_PROVIDER = BrokerProvider(
    name="angleone",
    client_factory=_create_client,
    feed_factory=_create_feed,
    configuration_validator=_configuration_errors,
    instrument_master_builder=_build_instrument_master,
)
