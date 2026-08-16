from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module

from app.broker.interfaces import (
    BrokerClient,
    BrokerSession,
    MarketDataFeed,
)
from app.core.config import BrokerName, Settings
from app.domain.models import InstrumentToken
from app.instruments.master import InstrumentMaster


ClientFactory = Callable[[Settings], BrokerClient]
FeedFactory = Callable[
    [Settings, BrokerSession, dict[str, InstrumentToken]],
    MarketDataFeed,
]
ConfigurationValidator = Callable[[Settings], tuple[str, ...]]
InstrumentMasterBuilder = Callable[
    [list[dict[str, object]], tuple[str, ...]],
    InstrumentMaster,
]


@dataclass(frozen=True)
class BrokerProvider:
    """Factories and validation owned by one broker adapter."""

    name: str
    client_factory: ClientFactory
    feed_factory: FeedFactory
    configuration_validator: ConfigurationValidator
    instrument_master_builder: InstrumentMasterBuilder


_PROVIDERS: dict[str, BrokerProvider] = {}


def register_broker_provider(
    name: BrokerName | str,
    provider: BrokerProvider,
    *,
    replace: bool = False,
) -> None:
    normalized = _normalize_name(name)
    provider_name = _normalize_name(provider.name)
    if provider_name != normalized:
        raise ValueError(
            f"Broker provider name '{provider_name}' does not match "
            f"registration name '{normalized}'"
        )
    if normalized in _PROVIDERS and not replace:
        raise ValueError(f"Broker provider is already registered: {normalized}")
    _PROVIDERS[normalized] = provider


def broker_configuration_errors(settings: Settings) -> tuple[str, ...]:
    provider = _provider(
        settings.broker_name,
        module_override=settings.broker_adapter_module,
    )
    return provider.configuration_validator(settings)


def create_broker_client(settings: Settings) -> BrokerClient:
    return _provider(
        settings.broker_name,
        module_override=settings.broker_adapter_module,
    ).client_factory(settings)


def create_market_data_feed(
    *,
    settings: Settings,
    session: BrokerSession,
    token_lookup: dict[str, InstrumentToken],
) -> MarketDataFeed:
    return _provider(
        settings.broker_name,
        module_override=settings.broker_adapter_module,
    ).feed_factory(
        settings,
        session,
        token_lookup,
    )


def build_configured_instrument_master(
    *,
    settings: Settings,
    rows: list[dict[str, object]],
) -> InstrumentMaster:
    return _provider(
        settings.broker_name,
        module_override=settings.broker_adapter_module,
    ).instrument_master_builder(rows, settings.default_underlyings)


def registered_brokers() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def _provider(
    name: BrokerName | str,
    *,
    module_override: str = "",
) -> BrokerProvider:
    normalized = _normalize_name(name)
    provider = _PROVIDERS.get(normalized)
    module_name = (
        module_override.strip()
        or f"app.broker.{normalized}.provider"
    )
    if provider is None:
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as exc:
            missing_module = str(exc.name or "")
            if not (
                missing_module == module_name
                or module_name.startswith(f"{missing_module}.")
            ):
                raise
            installed = ", ".join(registered_brokers()) or "none"
            raise ValueError(
                f"Broker adapter '{normalized}' is not installed. "
                f"Expected provider module: {module_name}. "
                f"Loaded adapters: {installed}"
            ) from exc
        candidate = getattr(module, "BROKER_PROVIDER", None)
        if not isinstance(candidate, BrokerProvider):
            raise ValueError(
                f"Broker provider module '{module_name}' does not export "
                "BROKER_PROVIDER"
            )
        if _normalize_name(candidate.name) != normalized:
            raise ValueError(
                f"Broker provider module '{module_name}' exports adapter "
                f"'{candidate.name}', not configured broker '{normalized}'"
            )
        register_broker_provider(normalized, candidate)
        provider = candidate
    if provider is None:
        installed = ", ".join(registered_brokers()) or "none"
        raise ValueError(
            f"Broker adapter '{normalized}' is not installed. "
            f"Registered adapters: {installed}"
        )
    return provider


def _normalize_name(name: BrokerName | str) -> str:
    return str(name).strip().lower()
