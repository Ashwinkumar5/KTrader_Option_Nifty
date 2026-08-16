# Broker Administration

This project keeps broker-specific code under `app/broker/<broker_name>`.

`app/broker/angleone` is the reference implementation. When onboarding another broker such as Zerodha, create a separate folder, copy the Angle One structure, and replace only broker-specific API/session/feed/instrument mapping code.

## Current Broker Folders

- `angleone`: live SmartAPI implementation and reference structure
- `administration`: helper code and onboarding guidance

## Onboarding Checklist

1. Create a new broker folder, for example `app/broker/zerodha`.
2. Copy the Python file structure from `app/broker/angleone`.
3. Update `client.py` for the broker login/session/REST API.
4. Update `feed.py` for websocket connection and subscription format.
5. Update `instruments.py` for instrument master parsing.
6. Update `data_map.py` for broker-specific market-data modes and field names.
7. Keep output mapped to shared domain models:
   - `InstrumentToken`
   - `OptionContract`
   - `MarketTick`
   - `GreeksSnapshot`
   - `OptionChainSnapshot`
8. Add tests under `tests/` before enabling the broker in runtime settings.

## Optional Scaffold Helper

From the project root:

```powershell
python scripts/create_broker.py zerodha
```

The script copies the reference files from `app/broker/angleone` into `app/broker/zerodha` and adds onboarding notes in the new folder.
