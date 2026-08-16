# Configurable Broker Adapters

The market-data worker depends on the `MarketDataFeedHandler` boundary. Its
current embedded implementation depends on the shared `BrokerClient` and
`MarketDataFeed` protocols. Broker construction, credential validation, feed
construction, and instrument-master parsing are loaded through a provider module.

## Runtime Selection

```text
BROKER_NAME=angleone
```

By convention, the provider is loaded from:

```text
app.broker.<BROKER_NAME>.provider
```

An external or differently located provider can be selected without changing the
worker:

```text
BROKER_NAME=dhan
BROKER_ADAPTER_MODULE=my_brokers.dhan_provider
```

The configured module must export:

```python
BROKER_PROVIDER = BrokerProvider(
    name="dhan",
    client_factory=...,
    feed_factory=...,
    configuration_validator=...,
    instrument_master_builder=...,
)
```

Broker-specific environment values are exposed only to that provider through:

```python
settings.broker_config
```

For `BROKER_NAME=dhan`, every `DHAN_*` environment variable is available with the
prefix removed:

```text
DHAN_CLIENT_ID      -> settings.broker_config["CLIENT_ID"]
DHAN_ACCESS_TOKEN   -> settings.broker_config["ACCESS_TOKEN"]
```

The core `Settings` class therefore does not need new credential fields for each
broker. `broker_config` is deliberately excluded from replay manifests and logs.

The provider name must match `BROKER_NAME`; this prevents accidentally using an
Angle One adapter when Dhan or Zerodha is configured.

## Provider Responsibilities

Each broker package owns:

```text
credential validation
login/session creation
REST quote calls
option Greek calls or normalized fallback
WebSocket connection and subscription payloads
raw tick normalization contract
instrument-master parsing
broker exchange/token mappings
```

The provider must return the shared domain models expected by the option-chain,
analytics, recorder, and storage layers.

## Current Providers

```text
angleone: implemented
dhan: configuration name reserved; provider not yet implemented
zerodha: configuration name reserved; provider not yet implemented
```

Selecting an unimplemented provider fails before login with an explicit
adapter-not-installed message.
