from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from app.analytics.engine import AnalyticsEngine
from app.broker.interfaces import BrokerClient, MarketDataFeed
from app.core.config import Settings, load_settings
from app.core.strategy_config import (
    apply_runtime_strategy_selection,
    load_strategy_configuration,
)
from app.domain.models import (
    InstrumentToken,
    MarketTick,
    MicrostructureSignal,
    OptionContract,
)
from app.execution.signal_router import (
    CentralSignalRouterClient,
    EntrySignalPublisher,
)
from app.execution.simulator_ipc import SimulatorEntrySignal
from app.instruments.master import available_expiries
from app.marketdata.feed_handler import (
    EmbeddedMarketDataFeedHandler,
    MarketDataFeedHandler,
    initialize_reference_data as _initialize_reference_data,
    normalize_market_quote_payloads as _normalize_market_quote_payloads,
)
from app.marketdata.frame_materializer import materialize_option_chain_frame
from app.marketdata.runtime_metrics import MarketDataRuntimeMetrics
from app.microstructure.engine import MicrostructureEngine, MicrostructureSettings
from app.optionchain.atm import select_option_window
from app.optionchain.state import OptionChainState
from app.signals.display import ActiveStrategyTarget
from app.signals.gate import SignalGate, SignalGateSettings
from app.signals.timely_entry import TimelyEntryGuard, TimelyEntryTrigger
from app.storage.interfaces import ChainSnapshotStore, LiveStateStore, TickStore
from app.storage.local import (
    InMemoryLiveStateStore,
    JsonlChainSnapshotStore,
    JsonlTickStore,
    NullChainSnapshotStore,
    NullTickStore,
)
from app.storage.microstructure_recorder import JsonlMicrostructureRecorder
from app.storage.strategy_journal import StrategyJournal, strategy_journal_filename
from app.storage.redis_store import (
    RedisChainSnapshotStore,
    RedisLiveStateStore,
    RedisTickStore,
)
from app.workers.progress_heartbeat import WorkerProgressHeartbeat


async def run_market_data_worker(
    *,
    settings: Settings | None = None,
    client: BrokerClient | None = None,
    feed: MarketDataFeed | None = None,
    feed_handler: MarketDataFeedHandler | None = None,
    tick_store: TickStore | None = None,
    chain_store: ChainSnapshotStore | None = None,
    live_store: LiveStateStore | None = None,
    max_ticks: int | None = None,
    enabled_strategies: tuple[str, ...] | None = None,
    enabled_features: tuple[str, ...] | None = None,
    minimum_book_imbalance: Decimal | None = None,
    heartbeat_file: Path | None = None,
    heartbeat_stall_timeout_seconds: float = 10.0,
) -> None:
    settings = settings or load_settings()
    if feed_handler is not None and (client is not None or feed is not None):
        raise ValueError(
            "feed_handler cannot be combined with client or feed overrides"
        )
    market_data = feed_handler or EmbeddedMarketDataFeedHandler(
        settings=settings,
        client=client,
        feed=feed,
    )
    progress_heartbeat = (
        WorkerProgressHeartbeat(
            heartbeat_file,
            stall_timeout_seconds=heartbeat_stall_timeout_seconds,
        )
        if heartbeat_file is not None
        else None
    )
    try:
        if progress_heartbeat is not None:
            await progress_heartbeat.start()
        await _run_market_data_worker(
            settings=settings,
            market_data=market_data,
            tick_store=tick_store,
            chain_store=chain_store,
            live_store=live_store,
            max_ticks=max_ticks,
            enabled_strategies=enabled_strategies,
            enabled_features=enabled_features,
            minimum_book_imbalance=minimum_book_imbalance,
            progress_heartbeat=progress_heartbeat,
        )
    finally:
        try:
            await market_data.close()
        finally:
            if progress_heartbeat is not None:
                await progress_heartbeat.close()


async def _run_market_data_worker(
    *,
    settings: Settings,
    market_data: MarketDataFeedHandler,
    tick_store: TickStore | None,
    chain_store: ChainSnapshotStore | None,
    live_store: LiveStateStore | None,
    max_ticks: int | None,
    enabled_strategies: tuple[str, ...] | None,
    enabled_features: tuple[str, ...] | None,
    minimum_book_imbalance: Decimal | None,
    progress_heartbeat: WorkerProgressHeartbeat | None,
) -> None:
    if progress_heartbeat is not None:
        progress_heartbeat.begin_work()
    strategy_configuration = load_strategy_configuration(
        settings.strategy_config_path or None,
        profile_name=settings.strategy_profile,
    )
    capture_profile_name = strategy_configuration.profile.name
    strategy_configuration = apply_runtime_strategy_selection(
        strategy_configuration,
        enabled_strategies=enabled_strategies,
        enabled_features=enabled_features,
        minimum_book_imbalance=minimum_book_imbalance,
    )
    storage_dir = Path(settings.local_storage_dir)
    # SQLite is retired from the live path. JSONL is authoritative for local
    # capture, while Redis remains optional for external live-state consumers.
    storage_backend = settings.storage_backend

    def _use_jsonl():
        persist_duplicate_ticks = (
            settings.operational_tick_journal_enabled
            or not settings.replay_capture_enabled
        )
        persist_duplicate_chains = (
            settings.operational_chain_journal_enabled
            or not settings.replay_capture_enabled
        )
        default_tick_store: TickStore = (
            JsonlTickStore(storage_dir / "ticks.jsonl")
            if persist_duplicate_ticks
            else NullTickStore()
        )
        return (
            tick_store or default_tick_store,
            chain_store
            or (
                JsonlChainSnapshotStore(
                    storage_dir / "option_chain_snapshots.jsonl"
                )
                if persist_duplicate_chains
                else NullChainSnapshotStore()
            ),
            live_store or InMemoryLiveStateStore(),
        )

    if storage_backend == "jsonl":
        tick_store, chain_store, live_store = _use_jsonl()
    elif storage_backend in {"", "auto", "redis"}:
        if settings.redis_url:
            try:
                candidate_tick_store = RedisTickStore(settings.redis_url, key="ticks")
                candidate_chain_store = RedisChainSnapshotStore(settings.redis_url, key="chain_snapshots")
                candidate_live_store = RedisLiveStateStore(settings.redis_url)

                # Verify connectivity / compatibility (some servers reject HELLO)
                try:
                    await candidate_tick_store._redis.ping()
                except Exception as exc:  # pragma: no cover - runtime environment dependent
                    raise RuntimeError(f"Redis server not compatible or unavailable: {exc}") from exc

                tick_store = tick_store or candidate_tick_store
                chain_store = chain_store or candidate_chain_store
                live_store = live_store or candidate_live_store
            except Exception as exc:  # pragma: no cover - runtime environment dependent
                print(f"WARNING: Redis unavailable or incompatible ({exc}); falling back to local storage.")
                tick_store, chain_store, live_store = _use_jsonl()
        else:
            tick_store, chain_store, live_store = _use_jsonl()
    else:
        raise ValueError(
            "STORAGE_BACKEND must be jsonl, redis, or auto; "
            "SQLite is retired from the live worker."
        )

    feed_runtime = await market_data.prepare()
    master = feed_runtime.master
    token_lookup = feed_runtime.token_lookup
    state = OptionChainState(master=master)
    market_date = datetime.now(
        ZoneInfo(settings.market_timezone)
    ).date()
    reference_data_status = await market_data.initialize_reference_data(
        state=state,
        market_date=market_date,
    )
    analytics_engine = AnalyticsEngine(
        pcr_bullish_threshold=Decimal(str(settings.pcr_bullish_threshold)),
        pcr_bearish_threshold=Decimal(str(settings.pcr_bearish_threshold)),
        market_timezone=settings.market_timezone,
        signal_debounce_frame_seconds=settings.signal_debounce_frame_seconds,
        signal_debounce_window_frames=settings.signal_debounce_window_frames,
        signal_debounce_min_confirmed_frames=(
            settings.signal_debounce_min_confirmed_frames
        ),
        range_soft_breach_frames=settings.range_soft_breach_frames,
        range_hard_invalidation_points=Decimal(
            str(settings.range_hard_invalidation_points)
        ),
        range_recovery_buffer_points=Decimal(
            str(settings.range_recovery_buffer_points)
        ),
        structural_level_frame_seconds=(
            settings.structural_level_frame_seconds
        ),
        strategy_resolver_policy=settings.strategy_resolver_policy,
        strategy_level_reversal_enabled=(
            settings.strategy_level_reversal_enabled
        ),
        strategy_breakout_momentum_enabled=(
            settings.strategy_breakout_momentum_enabled
        ),
        strategy_gamma_expansion_enabled=(
            settings.strategy_gamma_expansion_enabled
        ),
        strategy_level_reversal_priority=(
            settings.strategy_level_reversal_priority
        ),
        strategy_breakout_momentum_priority=(
            settings.strategy_breakout_momentum_priority
        ),
        strategy_gamma_expansion_priority=(
            settings.strategy_gamma_expansion_priority
        ),
        feature_opening_context_enabled=(
            settings.feature_opening_context_enabled
        ),
        feature_opening_context_sequence=(
            settings.feature_opening_context_sequence
        ),
        feature_expected_move_enabled=settings.feature_expected_move_enabled,
        feature_expected_move_sequence=settings.feature_expected_move_sequence,
        feature_premium_response_enabled=(
            settings.feature_premium_response_enabled
        ),
        feature_premium_response_sequence=(
            settings.feature_premium_response_sequence
        ),
        feature_futures_flow_enabled=settings.feature_futures_flow_enabled,
        feature_futures_flow_sequence=settings.feature_futures_flow_sequence,
        feature_candle_patterns_enabled=(
            settings.feature_candle_patterns_enabled
        ),
        feature_candle_patterns_sequence=(
            settings.feature_candle_patterns_sequence
        ),
        feature_momentum_exhaustion_enabled=(
            settings.feature_momentum_exhaustion_enabled
        ),
        feature_momentum_exhaustion_sequence=(
            settings.feature_momentum_exhaustion_sequence
        ),
        opening_observation_minutes=settings.opening_observation_minutes,
        expected_move_capture_time=settings.expected_move_capture_time,
        expected_move_first_band_ratio=Decimal(
            str(settings.expected_move_first_band_ratio)
        ),
        expected_move_extended_band_ratio=Decimal(
            str(settings.expected_move_extended_band_ratio)
        ),
        expected_move_exhaustion_band_ratio=Decimal(
            str(settings.expected_move_exhaustion_band_ratio)
        ),
        exhaustion_earliest_time=settings.exhaustion_earliest_time,
        exhaustion_minimum_premium_return_percent=Decimal(
            str(settings.exhaustion_minimum_premium_return_percent)
        ),
        exhaustion_minimum_move_utilization=Decimal(
            str(settings.exhaustion_minimum_move_utilization)
        ),
        gamma_window_seconds=settings.gamma_window_seconds,
        regime_window_seconds=settings.regime_window_seconds,
        futures_flow_window_seconds=settings.futures_flow_window_seconds,
        reversal_candle_confirmation_required=(
            settings.reversal_candle_confirmation_required
        ),
        strategy_profile=strategy_configuration.profile,
    )
    signal_gate = SignalGate(
        SignalGateSettings(
            min_confirmations=settings.signal_gate_min_confirmations,
            cooldown_seconds=settings.signal_gate_cooldown_seconds,
            local_reversal_cooldown_seconds=(
                settings.local_reversal_cooldown_seconds
            ),
            max_level_distance=Decimal(str(settings.signal_gate_level_distance_points)),
            max_microstructure_age_seconds=(
                strategy_configuration.profile.microstructure
                .maximum_age_seconds
            ),
            mode=settings.microstructure_mode,
            min_microstructure_confidence=(
                strategy_configuration.profile.microstructure
                .minimum_confidence
            ),
            min_signal_score=Decimal(str(settings.signal_gate_min_score)),
            straddle_zone_ratio=Decimal(
                str(settings.signal_gate_straddle_zone_ratio)
            ),
            min_range_room_points=Decimal(
                str(settings.signal_gate_min_range_room_points)
            ),
            allowed_underlyings=tuple(settings.default_underlyings),
            require_complete_chain=settings.signal_gate_require_complete_chain,
            min_chain_quotes=settings.signal_gate_min_chain_quotes,
            require_greeks=settings.signal_gate_require_greeks,
            require_target_contract=settings.signal_gate_require_target_contract,
            max_contract_spread=(
                strategy_configuration.profile.microstructure
                .maximum_spread_points
            ),
            min_directional_confirmations=(
                settings.signal_gate_min_directional_confirmations
            ),
            min_independent_confirmation_families=(
                settings.signal_gate_min_independent_confirmation_families
            ),
            require_regime_match=True,
            enforce_session=settings.risk_enforce_session,
            market_timezone=settings.market_timezone,
            max_underlying_age_seconds=(
                settings.signal_gate_max_underlying_age_seconds
            ),
            max_daily_loss=Decimal(str(settings.risk_max_daily_loss)),
            max_concurrent_positions=settings.risk_max_concurrent_positions,
            max_gross_exposure=Decimal(
                str(settings.risk_max_gross_exposure)
            ),
            premium_transmission_enabled=(
                settings.premium_transmission_enabled
            ),
            premium_transmission_min_expected_return_percent=Decimal(
                str(
                    settings.premium_transmission_min_expected_return_percent
                )
            ),
            premium_transmission_min_ratio=Decimal(
                str(settings.premium_transmission_min_ratio)
            ),
            quant_require_target_option_confirmation=(
                strategy_configuration.profile.microstructure
                .require_target_option_confirmation
            ),
            quant_require_futures_confirmation=(
                strategy_configuration.profile.microstructure
                .require_futures_confirmation
            ),
            quant_min_option_confirmations=(
                strategy_configuration.profile.microstructure
                .minimum_option_confirmations
            ),
            quant_min_futures_confirmations=(
                strategy_configuration.profile.microstructure
                .minimum_futures_confirmations
            ),
            quant_cooldown_seconds=(
                strategy_configuration.profile.execution.cooldown_seconds
            ),
            profile_min_directional_confirmations=(
                strategy_configuration.profile.microstructure
                .gate_minimum_directional_confirmations
            ),
            profile_min_independent_confirmation_families=(
                strategy_configuration.profile.microstructure
                .gate_minimum_independent_families
            ),
            profile_min_confirmations=(
                strategy_configuration.profile.microstructure
                .gate_minimum_confirmations
            ),
            gamma_require_microstructure_confirmation=(
                strategy_configuration.profile.feature_enabled(
                    "order_book_imbalance"
                )
            ),
            gamma_require_target_option_confirmation=(
                strategy_configuration.profile.microstructure
                .require_target_option_confirmation
            ),
            gamma_require_structural_room=(
                strategy_configuration.profile.microstructure
                .gamma_require_structural_room
            ),
        )
    )
    timely_entry_guard = TimelyEntryGuard(
        strategy_configuration.profile.microstructure,
        market_timezone=settings.market_timezone,
    )
    signal_router_client = (
        CentralSignalRouterClient(
            configured_profile=capture_profile_name,
            host=settings.signal_router_host,
            port=settings.signal_router_port,
            queue_capacity=settings.signal_router_queue_capacity,
            timeout_seconds=settings.signal_router_timeout_seconds,
            max_retries=settings.signal_router_max_retries,
        )
        if settings.signal_router_enabled
        else None
    )
    microstructure_engine = None
    if settings.microstructure_enabled:
        microstructure_engine = MicrostructureEngine(
            MicrostructureSettings(
                window_seconds=(
                    strategy_configuration.profile.microstructure
                    .feature_window_seconds
                ),
                min_events=(
                    strategy_configuration.profile.microstructure
                    .feature_min_events
                ),
                min_imbalance=(
                    strategy_configuration.profile.microstructure
                    .minimum_book_imbalance
                ),
                min_velocity=(
                    strategy_configuration.profile.microstructure
                    .minimum_price_velocity
                ),
                max_spread=(
                    strategy_configuration.profile.microstructure
                    .maximum_spread_points
                ),
                min_option_velocity_percent=(
                    strategy_configuration.profile.microstructure
                    .minimum_option_velocity_percent_per_second
                ),
                require_directional_option_book=(
                    strategy_configuration.profile.microstructure
                    .require_directional_option_book
                ),
            )
        )
    microstructure_recorder = None
    strategy_name = _single_enabled_strategy_name(
        strategy_configuration.profile.strategies
    )
    strategy_journal = None
    if strategy_name is not None:
        journal_directory = _profile_capture_directory(
            storage_dir,
            capture_profile_name,
            strategy_name=strategy_name,
        )
        strategy_journal = StrategyJournal(
            journal_directory
            / "journals"
            / strategy_journal_filename(
                strategy_name,
                datetime.now(ZoneInfo(settings.market_timezone)),
            ),
            strategy_name=strategy_name,
        )
        await strategy_journal.start()
    if settings.replay_capture_enabled:
        session_date = datetime.now(
            ZoneInfo(settings.market_timezone)
        ).date().isoformat()
        capture_prefix = _safe_capture_prefix(
            settings.replay_capture_file_prefix
        )
        capture_directory = _profile_capture_directory(
            storage_dir,
            capture_profile_name,
            strategy_name=strategy_name,
        )
        microstructure_recorder = JsonlMicrostructureRecorder(
            capture_directory / f"{capture_prefix}_{session_date}.jsonl",
            analytics_trace_enabled=False,
        )

    expiries = {
        underlying: underlying_expiries[0]
        for underlying in settings.default_underlyings
        for underlying_expiries in (available_expiries(master.options, underlying),)
        if underlying_expiries
    }
    if microstructure_recorder is not None:
        capture_started_at = datetime.now(UTC)
        replay_settings = _replay_capture_settings(settings)
        replay_settings["strategy_configuration"] = (
            strategy_configuration.manifest()
        )
        replay_settings["reference_data"] = reference_data_status
        replay_settings["market_data_transport"] = {
            "mode": (
                "nats-subscriber"
                if getattr(market_data, "is_remote_subscriber", False)
                else "embedded"
            ),
            "consumer_interval_ms": settings.snapshot_interval_ms,
            "producer_interval_ms": (
                getattr(
                    getattr(market_data, "bootstrap", None),
                    "source_interval_ms",
                    settings.snapshot_interval_ms,
                )
            ),
        }
        replay_settings["simulator_entry_ipc"] = {
            "enabled": settings.simulator_ipc_enabled,
            "endpoint": settings.simulator_ipc_endpoint,
            "host": settings.simulator_ipc_host,
            "port": settings.simulator_ipc_port,
            "max_retries": settings.simulator_ipc_max_retries,
            "owner": "central_signal_router",
            "exit_owner": "KTrader Simulator",
        }
        replay_settings["signal_router"] = {
            "enabled": settings.signal_router_enabled,
            "host": settings.signal_router_host,
            "port": settings.signal_router_port,
            "configured_profile": capture_profile_name,
        }
        await microstructure_recorder.record_session_manifest(
            started_at=capture_started_at,
            effective_settings=replay_settings,
            code_revision=_code_revision(),
            market_timezone=settings.market_timezone,
        )
        replay_contracts = tuple(
            contract
            for contract in master.options
            if contract.underlying in expiries
            and contract.expiry == expiries[contract.underlying]
        )
        await microstructure_recorder.record_instrument_master(
            captured_at=datetime.now(UTC),
            spot_tokens=tuple(master.spot_tokens.values()),
            option_contracts=replay_contracts,
            selected_expiries=expiries,
            future_contracts=master.futures,
            reference_tokens=tuple(master.reference_tokens.values()),
        )

    spot_prices: dict[str, Decimal] = {}
    spot_observed_at: dict[str, datetime] = {}
    option_windows: dict[
        str,
        tuple[Decimal, tuple[OptionContract, ...]],
    ] = {}
    active_option_tokens: dict[str, set[str]] = {}
    last_snapshot_at: dict[str, datetime] = {}
    snapshot_tasks: dict[str, asyncio.Task[None]] = {}
    pcr_history: dict[str, list[Decimal | None]] = {}
    latest_micro_signal: dict[str, MicrostructureSignal] = {}
    active_gamma_targets: dict[str, ActiveStrategyTarget] = {}
    runtime_metrics = MarketDataRuntimeMetrics(
        settings.runtime_metrics_sample_capacity
    )

    processed = 0
    status = "completed"
    terminal_error: str | None = None
    monitored_ticks = None
    try:
        try:
            reference_tokens = await market_data.start(
                market_date=datetime.now(
                    ZoneInfo(settings.market_timezone)
                ).date()
            )
            if microstructure_recorder is not None:
                await microstructure_recorder.record_subscription_change(
                    captured_at=datetime.now(UTC),
                    action="subscribe",
                    tokens=reference_tokens,
                    reason="initial_reference_subscription",
                )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize market data feed: {exc}"
            ) from exc
        finally:
            if progress_heartbeat is not None:
                await progress_heartbeat.finish_work()

        monitored_ticks = _monitor_worker_ticks(
            market_data.ticks(),
            snapshot_tasks=snapshot_tasks,
        )
        async for tick in monitored_ticks:
            if progress_heartbeat is not None:
                progress_heartbeat.begin_work()
            _raise_completed_snapshot_errors(snapshot_tasks)
            processing_started_at = datetime.now(UTC)
            runtime_metrics.observe_tick(
                tick,
                processing_started_at=processing_started_at,
            )
            await tick_store.save_tick(tick)
            features = None
            micro_signal = None
            if microstructure_engine is not None:
                features, micro_signal = microstructure_engine.observe(tick)
            if microstructure_recorder is not None:
                await microstructure_recorder.record_market_event(
                    tick=tick,
                    features=features,
                    signal=micro_signal,
                )
            if micro_signal is not None:
                latest_micro_signal[micro_signal.underlying] = micro_signal
                signal_gate.observe_microstructure(micro_signal)
            state.update_tick(tick)
            _update_spot_price(tick, master.spot_tokens, spot_prices)
            spot_underlying = _spot_underlying_for_tick(
                tick,
                master.spot_tokens,
            )
            if spot_underlying is not None and tick.ltp is not None:
                spot_observed_at[spot_underlying] = tick.received_at

            if micro_signal is not None:
                trigger = timely_entry_guard.consider(
                    tick=tick,
                    signal=micro_signal,
                )
                if trigger is not None:
                    await _process_timely_entry_trigger(
                        trigger=trigger,
                        tick=tick,
                        state=state,
                        signal_gate=signal_gate,
                        spot_prices=spot_prices,
                        spot_observed_at=spot_observed_at,
                        microstructure_recorder=microstructure_recorder,
                        signal_router_client=signal_router_client,
                        strategy_journal=strategy_journal,
                    )

            for underlying, spot_price in spot_prices.items():
                expiry = expiries.get(underlying)
                if expiry is None:
                    continue

                cached_window = option_windows.get(underlying)
                if cached_window is None or spot_underlying == underlying:
                    atm, selected_contracts = select_option_window(
                        master=master,
                        underlying=underlying,
                        expiry=expiry,
                        spot_price=spot_price,
                        each_side=settings.option_window_each_side,
                    )
                    if cached_window is None or cached_window[0] != atm:
                        contracts = tuple(selected_contracts)
                        if not getattr(
                            market_data,
                            "is_remote_subscriber",
                            False,
                        ):
                            await _rotate_option_subscriptions(
                                feed=market_data,
                                recorder=microstructure_recorder,
                                token_lookup=token_lookup,
                                active_tokens=active_option_tokens,
                                underlying=underlying,
                                spot_price=spot_price,
                                atm_strike=atm,
                                contracts=contracts,
                            )
                        option_windows[underlying] = (atm, contracts)
                        cached_window = option_windows[underlying]
                if cached_window is None:
                    continue
                _atm, contracts = cached_window

                now = datetime.now(UTC)
                previous_snapshot = last_snapshot_at.get(underlying)
                running = snapshot_tasks.get(underlying)
                due = (
                    previous_snapshot is None
                    or (
                        now - previous_snapshot
                    ).total_seconds()
                    * 1000
                    >= settings.snapshot_interval_ms
                )
                if due and running is None:
                    scheduled_for = (
                        now
                        if previous_snapshot is None
                        else previous_snapshot
                        + timedelta(
                            milliseconds=settings.snapshot_interval_ms
                        )
                    )
                    last_snapshot_at[underlying] = now
                    snapshot_tasks[underlying] = asyncio.create_task(
                        _run_snapshot_frame_with_heartbeat(
                            progress_heartbeat=progress_heartbeat,
                            feed_handler=market_data,
                            state=state,
                            analytics_engine=analytics_engine,
                            chain_store=chain_store,
                            live_store=live_store,
                            underlying=underlying,
                            expiry=expiry,
                            fallback_spot_price=spot_price,
                            contracts=contracts,
                            settings=settings,
                            pcr_history=pcr_history,
                            signal_gate=signal_gate,
                            latest_micro_signal=(
                                latest_micro_signal.get(underlying)
                            ),
                            active_gamma_targets=active_gamma_targets,
                            microstructure_recorder=(
                                microstructure_recorder
                            ),
                            strategy_journal=strategy_journal,
                            scheduled_for=scheduled_for,
                            frame_started_at=now,
                            trigger_tick_received_at=tick.received_at,
                            spot_price_provider=(
                                lambda key=underlying: spot_prices.get(key)
                            ),
                            spot_observed_at_provider=(
                                lambda key=underlying: (
                                    spot_observed_at.get(key)
                                )
                            ),
                            feed_health_provider=(
                                market_data.health_snapshot
                            ),
                            runtime_metrics=runtime_metrics,
                            signal_router_client=signal_router_client,
                            timely_entry_guard=timely_entry_guard,
                        ),
                        name=f"snapshot-{underlying}",
                    )

            if progress_heartbeat is not None:
                await progress_heartbeat.finish_work()
            processed += 1
            if max_ticks is not None and processed >= max_ticks:
                break
            await asyncio.sleep(0)

        await monitored_ticks.aclose()
        await _settle_snapshot_tasks(snapshot_tasks, cancel=False)
    except asyncio.CancelledError:
        status = "cancelled"
        terminal_error = "CancelledError"
        raise
    except BaseException as exc:
        status = "failed"
        terminal_error = type(exc).__name__
        raise
    finally:
        try:
            if monitored_ticks is not None:
                await monitored_ticks.aclose()
        finally:
            try:
                await _settle_snapshot_tasks(snapshot_tasks, cancel=True)
            finally:
                try:
                    if signal_router_client is not None:
                        await signal_router_client.close()
                finally:
                    if microstructure_recorder is not None:
                        try:
                            await microstructure_recorder.finish(
                                completed_at=datetime.now(UTC),
                                processed_ticks=processed,
                                status=status,
                                error=terminal_error,
                            )
                        finally:
                            if strategy_journal is not None:
                                await strategy_journal.close()
                    elif strategy_journal is not None:
                        await strategy_journal.close()


async def _run_snapshot_frame_with_heartbeat(
    *,
    progress_heartbeat: WorkerProgressHeartbeat | None,
    **kwargs,
) -> None:
    if progress_heartbeat is not None:
        progress_heartbeat.begin_work()
    try:
        await _run_snapshot_frame(**kwargs)
    finally:
        if progress_heartbeat is not None:
            await progress_heartbeat.finish_work()


def _spot_underlying_for_tick(
    tick: MarketTick,
    spot_tokens: dict[str, InstrumentToken],
) -> str | None:
    for underlying, token in spot_tokens.items():
        if token.token == tick.token.token:
            return underlying
    return None


def _feed_health_error(
    feed_health: dict[str, object] | None,
) -> str | None:
    if not feed_health:
        return None
    status = str(feed_health.get("status") or "UNAVAILABLE").upper()
    if status not in {"PRESSURE", "DATA_LOSS", "FAILED"}:
        return None
    reason = str(
        feed_health.get("reason") or f"feed_status_{status.lower()}"
    )
    return f"feed_unhealthy={status}:{reason}"


def _replay_capture_settings(settings: Settings) -> dict[str, object]:
    """Return replay-relevant settings without credentials or connection URLs."""

    return {
        "app_name": settings.app_name,
        "app_env": settings.app_env,
        "broker_name": str(settings.broker_name),
        "broker_adapter_module": settings.broker_adapter_module,
        "angleone_http_timeout_seconds": (
            settings.angleone_http_timeout_seconds
        ),
        "default_underlyings": settings.default_underlyings,
        "option_window_each_side": settings.option_window_each_side,
        "snapshot_interval_ms": settings.snapshot_interval_ms,
        "market_data_feed_interval_ms": (
            settings.market_data_feed_interval_ms
        ),
        "storage_backend": settings.storage_backend,
        "operational_tick_journal_enabled": (
            settings.operational_tick_journal_enabled
        ),
        "operational_chain_journal_enabled": (
            settings.operational_chain_journal_enabled
        ),
        "market_data_queue_capacity": settings.market_data_queue_capacity,
        "market_data_queue_pressure_ratio": (
            settings.market_data_queue_pressure_ratio
        ),
        "runtime_metrics_sample_capacity": (
            settings.runtime_metrics_sample_capacity
        ),
        "simulator_ipc_enabled": settings.simulator_ipc_enabled,
        "simulator_ipc_endpoint": settings.simulator_ipc_endpoint,
        "simulator_ipc_host": settings.simulator_ipc_host,
        "simulator_ipc_port": settings.simulator_ipc_port,
        "simulator_ipc_queue_capacity": (
            settings.simulator_ipc_queue_capacity
        ),
        "simulator_ipc_timeout_seconds": (
            settings.simulator_ipc_timeout_seconds
        ),
        "simulator_ipc_max_retries": settings.simulator_ipc_max_retries,
        "signal_router_enabled": settings.signal_router_enabled,
        "signal_router_host": settings.signal_router_host,
        "signal_router_port": settings.signal_router_port,
        "signal_router_queue_capacity": (
            settings.signal_router_queue_capacity
        ),
        "signal_router_timeout_seconds": (
            settings.signal_router_timeout_seconds
        ),
        "signal_router_max_retries": settings.signal_router_max_retries,
        "market_data_price_source": settings.market_data_price_source,
        "market_data_oi_source": settings.market_data_oi_source,
        "market_data_greeks_source": settings.market_data_greeks_source,
        "market_data_ws_mode": settings.market_data_ws_mode,
        "option_greeks_enabled": settings.option_greeks_enabled,
        "broker_pcr_enabled": settings.broker_pcr_enabled,
        "broker_oi_buildup_enabled": settings.broker_oi_buildup_enabled,
        "pcr_bullish_threshold": settings.pcr_bullish_threshold,
        "pcr_bearish_threshold": settings.pcr_bearish_threshold,
        "microstructure_enabled": settings.microstructure_enabled,
        "microstructure_mode": settings.microstructure_mode,
        "microstructure_window_seconds": settings.microstructure_window_seconds,
        "microstructure_min_events": settings.microstructure_min_events,
        "microstructure_min_imbalance": settings.microstructure_min_imbalance,
        "microstructure_min_velocity": settings.microstructure_min_velocity,
        "microstructure_max_spread_points": settings.microstructure_max_spread_points,
        "signal_gate_min_confirmations": settings.signal_gate_min_confirmations,
        "signal_gate_cooldown_seconds": settings.signal_gate_cooldown_seconds,
        "local_reversal_cooldown_seconds": (
            settings.local_reversal_cooldown_seconds
        ),
        "signal_gate_level_distance_points": settings.signal_gate_level_distance_points,
        "signal_gate_min_micro_confidence": settings.signal_gate_min_micro_confidence,
        "signal_gate_min_score": settings.signal_gate_min_score,
        "signal_gate_straddle_zone_ratio": settings.signal_gate_straddle_zone_ratio,
        "signal_gate_min_range_room_points": settings.signal_gate_min_range_room_points,
        "signal_gate_min_directional_confirmations": (
            settings.signal_gate_min_directional_confirmations
        ),
        "signal_gate_min_independent_confirmation_families": (
            settings.signal_gate_min_independent_confirmation_families
        ),
        "signal_gate_require_complete_chain": (
            settings.signal_gate_require_complete_chain
        ),
        "signal_gate_min_chain_quotes": settings.signal_gate_min_chain_quotes,
        "signal_gate_require_greeks": settings.signal_gate_require_greeks,
        "signal_gate_require_target_contract": (
            settings.signal_gate_require_target_contract
        ),
        "signal_gate_max_underlying_age_seconds": (
            settings.signal_gate_max_underlying_age_seconds
        ),
        "signal_debounce_frame_seconds": settings.signal_debounce_frame_seconds,
        "signal_debounce_window_frames": settings.signal_debounce_window_frames,
        "signal_debounce_min_confirmed_frames": (
            settings.signal_debounce_min_confirmed_frames
        ),
        "range_soft_breach_frames": settings.range_soft_breach_frames,
        "range_hard_invalidation_points": (
            settings.range_hard_invalidation_points
        ),
        "range_recovery_buffer_points": (
            settings.range_recovery_buffer_points
        ),
        "structural_level_frame_seconds": (
            settings.structural_level_frame_seconds
        ),
        "strategy_resolver_policy": settings.strategy_resolver_policy,
        "strategy_level_reversal_enabled": (
            settings.strategy_level_reversal_enabled
        ),
        "strategy_breakout_momentum_enabled": (
            settings.strategy_breakout_momentum_enabled
        ),
        "strategy_gamma_expansion_enabled": (
            settings.strategy_gamma_expansion_enabled
        ),
        "strategy_level_reversal_priority": (
            settings.strategy_level_reversal_priority
        ),
        "strategy_breakout_momentum_priority": (
            settings.strategy_breakout_momentum_priority
        ),
        "strategy_gamma_expansion_priority": (
            settings.strategy_gamma_expansion_priority
        ),
        "feature_opening_context_enabled": (
            settings.feature_opening_context_enabled
        ),
        "feature_opening_context_sequence": (
            settings.feature_opening_context_sequence
        ),
        "feature_expected_move_enabled": settings.feature_expected_move_enabled,
        "feature_expected_move_sequence": settings.feature_expected_move_sequence,
        "feature_premium_response_enabled": (
            settings.feature_premium_response_enabled
        ),
        "feature_premium_response_sequence": (
            settings.feature_premium_response_sequence
        ),
        "feature_momentum_exhaustion_enabled": (
            settings.feature_momentum_exhaustion_enabled
        ),
        "feature_momentum_exhaustion_sequence": (
            settings.feature_momentum_exhaustion_sequence
        ),
        "opening_observation_minutes": settings.opening_observation_minutes,
        "expected_move_capture_time": settings.expected_move_capture_time,
        "expected_move_first_band_ratio": (
            settings.expected_move_first_band_ratio
        ),
        "expected_move_extended_band_ratio": (
            settings.expected_move_extended_band_ratio
        ),
        "expected_move_exhaustion_band_ratio": (
            settings.expected_move_exhaustion_band_ratio
        ),
        "exhaustion_earliest_time": settings.exhaustion_earliest_time,
        "exhaustion_minimum_premium_return_percent": (
            settings.exhaustion_minimum_premium_return_percent
        ),
        "exhaustion_minimum_move_utilization": (
            settings.exhaustion_minimum_move_utilization
        ),
        "gamma_window_seconds": settings.gamma_window_seconds,
        "regime_window_seconds": settings.regime_window_seconds,
        "risk_enforce_session": settings.risk_enforce_session,
        "risk_max_daily_loss": settings.risk_max_daily_loss,
        "risk_max_concurrent_positions": settings.risk_max_concurrent_positions,
        "risk_max_gross_exposure": settings.risk_max_gross_exposure,
        "execution_account_capital": settings.execution_account_capital,
        "execution_risk_per_trade_percent": (
            settings.execution_risk_per_trade_percent
        ),
        "replay_capture_enabled": settings.replay_capture_enabled,
        "replay_capture_file_prefix": settings.replay_capture_file_prefix,
        "replay_require_complete_window": settings.replay_require_complete_window,
        "market_timezone": settings.market_timezone,
    }


def _safe_capture_prefix(value: str) -> str:
    prefix = value.strip()
    if (
        not prefix
        or Path(prefix).name != prefix
        or prefix in {".", ".."}
    ):
        raise ValueError(
            "REPLAY_CAPTURE_FILE_PREFIX must be a plain filename prefix"
        )
    return prefix


def _profile_capture_directory(
    storage_directory: Path,
    profile_name: str,
    *,
    strategy_name: str | None = None,
) -> Path:
    directory_name = profile_name.strip()
    if (
        not directory_name
        or Path(directory_name).name != directory_name
        or directory_name in {".", ".."}
    ):
        raise ValueError("strategy profile must be a plain directory name")
    profile_directory = storage_directory / directory_name
    if strategy_name is None:
        return profile_directory
    strategy_directory = strategy_name.strip()
    if (
        not strategy_directory
        or Path(strategy_directory).name != strategy_directory
        or strategy_directory in {".", ".."}
    ):
        raise ValueError("strategy name must be a plain directory name")
    return profile_directory / strategy_directory


def _single_enabled_strategy_name(strategies: Mapping[str, object]) -> str | None:
    """Return the strategy identity only for a single-strategy worker.

    A configured NATS worker always selects one strategy.  Its tape must not
    share a profile-level JSONL path with another process.  The legacy
    multi-strategy embedded worker keeps its one profile-level capture instead.
    """

    enabled = tuple(
        name
        for name, toggle in strategies.items()
        if bool(getattr(toggle, "enabled", False))
    )
    return enabled[0] if len(enabled) == 1 else None


def _code_revision() -> str:
    """Best-effort local Git identity without spawning a subprocess."""

    repository_root = Path(__file__).resolve().parents[2]
    git_dir = repository_root / ".git"
    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head or "unknown"
        reference = head[5:]
        reference_path = git_dir / Path(reference)
        if reference_path.is_file():
            return reference_path.read_text(encoding="utf-8").strip()
        return f"unborn:{reference}"
    except OSError:
        return "unknown"


def _raise_completed_snapshot_errors(
    tasks: dict[str, asyncio.Task[None]],
) -> None:
    for underlying, task in tuple(tasks.items()):
        if not task.done():
            continue
        del tasks[underlying]
        task.result()


async def _monitor_worker_ticks(
    ticks,
    *,
    snapshot_tasks: dict[str, asyncio.Task[None]],
):
    """Yield ticks while surfacing a failed frame during a quiet feed."""

    iterator = ticks.__aiter__()
    next_tick = asyncio.create_task(
        anext(iterator),
        name="strategy-worker-next-tick",
    )
    try:
        while True:
            watched: set[asyncio.Task] = {next_tick}
            watched.update(snapshot_tasks.values())
            done, _pending = await asyncio.wait(
                watched,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for underlying, task in tuple(snapshot_tasks.items()):
                if task in done:
                    del snapshot_tasks[underlying]
                    task.result()
            if next_tick not in done:
                continue
            try:
                tick = next_tick.result()
            except StopAsyncIteration:
                return
            yield tick
            # Do not prefetch before the worker finishes its tick body. The
            # remote handler uses generator resumption as acknowledgement that
            # a bus-earlier tick cannot be overtaken by a later frame.
            next_tick = asyncio.create_task(
                anext(iterator),
                name="strategy-worker-next-tick",
            )
    finally:
        if not next_tick.done():
            next_tick.cancel()
        await asyncio.gather(next_tick, return_exceptions=True)
        close_iterator = getattr(iterator, "aclose", None)
        if callable(close_iterator):
            await close_iterator()


async def _settle_snapshot_tasks(
    tasks: dict[str, asyncio.Task[None]],
    *,
    cancel: bool,
) -> None:
    pending = tuple(tasks.values())
    tasks.clear()
    if cancel:
        for task in pending:
            if not task.done():
                task.cancel()
    if not pending:
        return
    results = await asyncio.gather(*pending, return_exceptions=True)
    if not cancel:
        for result in results:
            if isinstance(result, BaseException):
                raise result


async def _rotate_option_subscriptions(
    *,
    feed: MarketDataFeed,
    recorder: JsonlMicrostructureRecorder | None,
    token_lookup: Mapping[str, InstrumentToken],
    active_tokens: dict[str, set[str]],
    underlying: str,
    spot_price: Decimal,
    atm_strike: Decimal,
    contracts: tuple[OptionContract, ...],
    protected_tokens: set[str] | None = None,
) -> None:
    selected = {contract.token.token for contract in contracts}
    previous = active_tokens.get(underlying, set())
    protected = (protected_tokens or set()) & previous
    new_tokens = tuple(
        contract.token
        for contract in contracts
        if contract.token.token not in previous
    )
    stale_tokens = tuple(
        token_lookup[token]
        for token in sorted(previous - selected - protected)
        if token in token_lookup
    )

    if new_tokens:
        await feed.subscribe(new_tokens)
        if recorder is not None:
            await recorder.record_subscription_change(
                captured_at=datetime.now(UTC),
                action="subscribe",
                tokens=new_tokens,
                reason=(
                    "initial_atm_window"
                    if not previous
                    else "atm_window_rotation"
                ),
                underlying=underlying,
                spot_price=spot_price,
                atm_strike=atm_strike,
            )
    if stale_tokens:
        await feed.unsubscribe(stale_tokens)
        if recorder is not None:
            await recorder.record_subscription_change(
                captured_at=datetime.now(UTC),
                action="unsubscribe",
                tokens=stale_tokens,
                reason="atm_window_rotation",
                underlying=underlying,
                spot_price=spot_price,
                atm_strike=atm_strike,
            )
    active_tokens[underlying] = selected | protected


async def _process_timely_entry_trigger(
    *,
    trigger: TimelyEntryTrigger,
    tick: MarketTick,
    state: OptionChainState,
    signal_gate: SignalGate,
    spot_prices: dict[str, Decimal],
    spot_observed_at: dict[str, datetime],
    microstructure_recorder: JsonlMicrostructureRecorder | None,
    signal_router_client: EntrySignalPublisher | None,
    strategy_journal: StrategyJournal | None = None,
) -> None:
    candidate = trigger.candidate
    underlying = candidate.snapshot.underlying
    current_spot = spot_prices.get(underlying, candidate.snapshot.spot_price)
    market = state.build_underlying_market_snapshot(
        underlying=underlying,
        captured_at=trigger.captured_at,
    )
    quotes = tuple(
        replace(
            quote,
            ltp=trigger.ltp,
            bid=trigger.bid,
            ask=trigger.ask,
            volume=tick.volume if tick.volume is not None else quote.volume,
            oi=tick.oi if tick.oi is not None else quote.oi,
        )
        if quote.contract.token.token == candidate.target_token
        else quote
        for quote in candidate.snapshot.quotes
    )
    snapshot = replace(
        candidate.snapshot,
        captured_at=trigger.captured_at,
        spot_price=current_spot,
        quotes=quotes,
        market=market,
    )
    analytics = replace(
        candidate.analytics,
        captured_at=trigger.captured_at,
        target_ltp=trigger.ltp,
    )
    _gated_analytics, decision = signal_gate.evaluate(
        snapshot=snapshot,
        analytics=analytics,
        microstructure_signal=trigger.signal,
        underlying_observed_at=spot_observed_at.get(
            underlying,
            candidate.underlying_observed_at,
        ),
        refreshed_quote_tokens=set(candidate.refreshed_quote_tokens),
        refreshed_greeks_tokens=set(candidate.refreshed_greeks_tokens),
        microstructure_not_before=candidate.armed_at,
    )
    entry_signal = _build_simulator_entry_signal(
        snapshot=snapshot,
        analytics=analytics,
        decision=decision,
    )
    dispatch_status = "NOT_APPLICABLE"
    if decision.qualified:
        dispatch_status = _publish_simulator_entry(
            signal=entry_signal,
            publisher=signal_router_client,
        )
        _record_strategy_target(
            journal=strategy_journal,
            analytics=analytics,
            state="ACQUIRED",
            router_status=dispatch_status,
        )
    if microstructure_recorder is not None:
        await microstructure_recorder.record_gate_decision(
            snapshot=snapshot,
            decision=decision,
            analytics=analytics,
            frame={
                "trigger": "event_driven_entry",
                "candidate_armed_at": candidate.armed_at,
                "candidate_expires_at": candidate.expires_at,
                "premium_chase_percent": trigger.premium_chase_percent,
                "trigger_tick_received_at": tick.received_at,
            },
            execution_signal=_execution_signal_record(
                entry_signal,
                dispatch_status=dispatch_status,
            ),
        )


def _build_simulator_entry_signal(
    *,
    snapshot,
    analytics,
    decision,
) -> SimulatorEntrySignal | None:
    if not decision.qualified:
        return None
    target_strike = analytics.target_strike
    strong_signal = decision.strong_signal
    if target_strike is None or strong_signal not in {"BUY_CALL", "BUY_PUT"}:
        return None
    selected_strategy = analytics.selected_strategy
    strategy = (
        selected_strategy.value
        if selected_strategy is not None
        else analytics.strategy_source
    )
    return SimulatorEntrySignal(
        underlying=snapshot.underlying,
        strike=target_strike,
        side=strong_signal,
        captured_at=decision.captured_at,
        profile=analytics.strategy_profile,
        strategy=strategy,
    )


def _execution_signal_record(
    signal: SimulatorEntrySignal | None,
    *,
    dispatch_status: str,
) -> dict[str, object] | None:
    if signal is None:
        return None
    return {
        "signal_id": signal.signal_id,
        "profile": signal.profile,
        "strategy": signal.strategy,
        "side": signal.side,
        "strike": signal.strike,
        "dispatch_status": dispatch_status,
    }


def _publish_simulator_entry(
    *,
    signal: SimulatorEntrySignal | None,
    publisher: EntrySignalPublisher | None,
) -> str:
    if signal is None:
        return "NOT_APPLICABLE"
    if publisher is None:
        return "ROUTER_DISABLED"
    queued = publisher.publish(signal)
    status = (
        "ROUTER_CLIENT_QUEUED"
        if queued
        else "ROUTER_CLIENT_DROPPED"
    )
    return status


def _record_strategy_target(
    *,
    journal: StrategyJournal | None,
    analytics,
    state: str,
    router_status: str | None = None,
) -> None:
    if journal is None:
        return
    line = journal.record_target(
        analytics=analytics,
        state=state,
        router_status=router_status,
    )
    if line is not None:
        print(line)


async def _run_snapshot_frame(
    *,
    feed_handler: MarketDataFeedHandler,
    state: OptionChainState,
    analytics_engine: AnalyticsEngine,
    chain_store: ChainSnapshotStore,
    live_store: LiveStateStore,
    underlying: str,
    expiry,
    fallback_spot_price: Decimal,
    contracts: tuple[OptionContract, ...],
    settings: Settings,
    pcr_history: dict[str, list[Decimal | None]],
    signal_gate: SignalGate,
    latest_micro_signal: MicrostructureSignal | None,
    active_gamma_targets: dict[str, ActiveStrategyTarget],
    microstructure_recorder: JsonlMicrostructureRecorder | None,
    strategy_journal: StrategyJournal | None,
    scheduled_for: datetime,
    frame_started_at: datetime,
    trigger_tick_received_at: datetime,
    spot_price_provider: Callable[[], Decimal | None],
    spot_observed_at_provider: Callable[[], datetime | None],
    feed_health_provider: Callable[[], dict[str, object]],
    runtime_metrics: MarketDataRuntimeMetrics,
    signal_router_client: EntrySignalPublisher | None,
    timely_entry_guard: TimelyEntryGuard,
) -> None:
    latest_pcr = await _refresh_chain_snapshot(
        feed_handler=feed_handler,
        state=state,
        analytics_engine=analytics_engine,
        chain_store=chain_store,
        live_store=live_store,
        underlying=underlying,
        expiry=expiry,
        spot_price=fallback_spot_price,
        contracts=contracts,
        settings=settings,
        received_at=frame_started_at,
        recent_pcr=tuple(pcr_history.get(underlying, [])[-3:]),
        signal_gate=signal_gate,
        latest_micro_signal=latest_micro_signal,
        active_gamma_targets=active_gamma_targets,
        microstructure_recorder=microstructure_recorder,
        strategy_journal=strategy_journal,
        scheduled_for=scheduled_for,
        frame_started_at=frame_started_at,
        trigger_tick_received_at=trigger_tick_received_at,
        spot_observed_at=spot_observed_at_provider(),
        spot_price_provider=spot_price_provider,
        spot_observed_at_provider=spot_observed_at_provider,
        feed_health_provider=feed_health_provider,
        runtime_metrics=runtime_metrics,
        signal_router_client=signal_router_client,
        timely_entry_guard=timely_entry_guard,
    )
    history = pcr_history.setdefault(underlying, [])
    history.append(latest_pcr)
    if len(history) > 10:
        del history[:-10]


async def _refresh_chain_snapshot(
    *,
    feed_handler: MarketDataFeedHandler,
    state: OptionChainState,
    analytics_engine: AnalyticsEngine,
    chain_store: ChainSnapshotStore,
    live_store: LiveStateStore,
    underlying: str,
    expiry,
    spot_price: Decimal,
    contracts: tuple[OptionContract, ...],
    settings: Settings,
    received_at: datetime,
    recent_pcr: tuple[Decimal | None, ...] = (),
    signal_gate: SignalGate,
    latest_micro_signal: MicrostructureSignal | None,
    active_gamma_targets: dict[str, ActiveStrategyTarget],
    microstructure_recorder: JsonlMicrostructureRecorder | None,
    strategy_journal: StrategyJournal | None = None,
    scheduled_for: datetime | None = None,
    frame_started_at: datetime | None = None,
    trigger_tick_received_at: datetime | None = None,
    spot_observed_at: datetime | None = None,
    spot_price_provider: Callable[[], Decimal | None] | None = None,
    spot_observed_at_provider: (
        Callable[[], datetime | None] | None
    ) = None,
    feed_health_provider: (
        Callable[[], dict[str, object]] | None
    ) = None,
    runtime_metrics: MarketDataRuntimeMetrics | None = None,
    signal_router_client: EntrySignalPublisher | None = None,
    timely_entry_guard: TimelyEntryGuard | None = None,
) -> Decimal | None:
    frame_started_at = frame_started_at or received_at
    scheduled_for = scheduled_for or frame_started_at
    remote_frame_provider = getattr(
        feed_handler,
        "next_materialized_frame",
        None,
    )
    if callable(remote_frame_provider):
        frame = await remote_frame_provider(
            underlying=underlying,
            scheduled_for=scheduled_for,
            consumer_interval_ms=settings.snapshot_interval_ms,
            window_each_side=settings.option_window_each_side,
        )
    else:
        frame = await materialize_option_chain_frame(
            feed_handler=feed_handler,
            state=state,
            underlying=underlying,
            expiry=expiry,
            fallback_spot_price=spot_price,
            contracts=contracts,
            option_window_each_side=settings.option_window_each_side,
            option_greeks_enabled=settings.option_greeks_enabled,
            source_interval_ms=settings.snapshot_interval_ms,
            scheduled_for=scheduled_for,
            frame_started_at=frame_started_at,
            trigger_tick_received_at=(
                trigger_tick_received_at or frame_started_at
            ),
            spot_observed_at=spot_observed_at,
            spot_price_provider=spot_price_provider,
            spot_observed_at_provider=spot_observed_at_provider,
            feed_health_provider=feed_health_provider,
        )
    snapshot = frame.snapshot
    snapshot_captured_at = snapshot.captured_at
    current_spot_price = snapshot.spot_price
    current_spot_observed_at = frame.spot_observed_at
    quote_refresh = frame.quote_refresh.as_mapping()
    greeks_refresh = frame.greeks_refresh.as_mapping()
    feed_health = frame.feed_health.as_mapping()
    scheduled_for = frame.scheduled_for
    frame_started_at = frame.frame_started_at
    trigger_tick_received_at = frame.trigger_tick_received_at
    current_quote_tokens = {
        str(token)
        for token in quote_refresh.get("normalized_tokens", ())
    }
    current_greeks_tokens = {
        str(token)
        for token in greeks_refresh.get("normalized_tokens", ())
    }
    armed_candidate = None
    preflight_error = _feed_health_error(feed_health)
    if preflight_error is None:
        preflight_error = signal_gate.preflight_data(
            snapshot=snapshot,
            underlying_observed_at=current_spot_observed_at,
            refreshed_quote_tokens=current_quote_tokens,
            refreshed_greeks_tokens=current_greeks_tokens,
        )
    if preflight_error is not None:
        (
            targeted_raw_analytics,
            gate_decision,
        ) = signal_gate.reject_preflight(
            snapshot=snapshot,
            reason=preflight_error,
        )
        analytics_snapshot = targeted_raw_analytics
    else:
        raw_analytics_snapshot = analytics_engine.from_chain(snapshot)
        targeted_raw_analytics = analytics_engine.with_optimal_target(
            snapshot=snapshot,
            analytics=raw_analytics_snapshot,
        )
        analytics_snapshot, gate_decision = signal_gate.evaluate(
            snapshot=snapshot,
            analytics=targeted_raw_analytics,
            microstructure_signal=latest_micro_signal,
            underlying_observed_at=current_spot_observed_at,
            refreshed_quote_tokens=current_quote_tokens,
            refreshed_greeks_tokens=current_greeks_tokens,
            microstructure_not_before=(
                timely_entry_guard.microstructure_not_before(
                    snapshot.captured_at
                )
                if timely_entry_guard is not None
                else None
            ),
        )
        if timely_entry_guard is not None:
            armed_candidate = timely_entry_guard.arm_from_decision(
                snapshot=snapshot,
                analytics=targeted_raw_analytics,
                decision=gate_decision,
                refreshed_quote_tokens=current_quote_tokens,
                refreshed_greeks_tokens=current_greeks_tokens,
                underlying_observed_at=current_spot_observed_at,
            )
    if preflight_error is not None and timely_entry_guard is not None:
        timely_entry_guard.cancel(underlying)
    if (
        gate_decision.qualified
        and targeted_raw_analytics.strategy_source == "GAMMA"
        and targeted_raw_analytics.signal in {"BUY_CALL", "BUY_PUT"}
        and targeted_raw_analytics.target_strike is not None
    ):
        active_gamma_targets[underlying] = ActiveStrategyTarget(
            source="GAMMA",
            side=targeted_raw_analytics.signal,
            strike=targeted_raw_analytics.target_strike,
            option_type=targeted_raw_analytics.target_option_type,
            ltp=targeted_raw_analytics.target_ltp,
            delta=targeted_raw_analytics.target_delta,
            captured_at=snapshot.captured_at,
        )
    elif underlying in active_gamma_targets:
        active_gamma_targets[underlying] = _refresh_active_target_ltp(
            snapshot=snapshot,
            target=active_gamma_targets[underlying],
        )
    # The complete decision remains in the structured replay tape.  The
    # human-readable journal below logs only target lifecycle changes.
    entry_signal = _build_simulator_entry_signal(
        snapshot=snapshot,
        analytics=targeted_raw_analytics,
        decision=gate_decision,
    )
    dispatch_status = _publish_simulator_entry(
        signal=entry_signal,
        publisher=signal_router_client,
    )
    if gate_decision.qualified:
        _record_strategy_target(
            journal=strategy_journal,
            analytics=targeted_raw_analytics,
            state="ACQUIRED",
            router_status=dispatch_status,
        )
    else:
        _record_strategy_target(
            journal=strategy_journal,
            analytics=targeted_raw_analytics,
            state=(
                "STARTED_ACQUIRING"
                if armed_candidate is not None
                else "TRYING_TO_ACQUIRE"
            ),
        )
    if microstructure_recorder is not None:
        expected_contract_count = (settings.option_window_each_side * 2 + 1) * 2
        quote_tokens = tuple(
            quote.contract.token.token
            for quote in snapshot.quotes
            if quote.ltp is not None
        )
        greeks_tokens = tuple(
            quote.contract.token.token
            for quote in snapshot.quotes
            if quote.greeks is not None
        )
        bid_ask_tokens = tuple(
            quote.contract.token.token
            for quote in snapshot.quotes
            if quote.bid is not None
            and quote.ask is not None
            and quote.bid > 0
            and quote.ask >= quote.bid
        )
        bid_ask_token_set = set(bid_ask_tokens)
        oi_volume_tokens = tuple(
            quote.contract.token.token
            for quote in snapshot.quotes
            if quote.oi is not None and quote.volume is not None
        )
        usable_greeks_tokens = tuple(
            quote.contract.token.token
            for quote in snapshot.quotes
            if quote.greeks is not None
            and quote.greeks.implied_volatility is not None
            and quote.greeks.delta is not None
        )
        selected_tokens = tuple(
            quote.contract.token.token for quote in snapshot.quotes
        )
        selected_token_set = set(selected_tokens)
        refreshed_quote_tokens = {
            str(token)
            for token in quote_refresh.get("normalized_tokens", ())
        }
        refreshed_greeks_tokens = {
            str(token)
            for token in greeks_refresh.get("normalized_tokens", ())
        }
        data_quality_reasons: list[str] = []
        feed_health_error = _feed_health_error(feed_health)
        if feed_health_error is not None:
            data_quality_reasons.append(feed_health_error)
        if len(snapshot.quotes) != expected_contract_count:
            data_quality_reasons.append(
                f"selected_contracts={len(snapshot.quotes)}/"
                f"{expected_contract_count}"
            )
        if len(quote_tokens) != expected_contract_count:
            data_quality_reasons.append(
                f"quotes_with_ltp={len(quote_tokens)}/{expected_contract_count}"
            )
        if len(bid_ask_tokens) != expected_contract_count:
            data_quality_reasons.append(
                f"quotes_with_valid_bid_ask={len(bid_ask_tokens)}/"
                f"{expected_contract_count}"
            )
        if len(oi_volume_tokens) != expected_contract_count:
            data_quality_reasons.append(
                f"quotes_with_oi_volume={len(oi_volume_tokens)}/"
                f"{expected_contract_count}"
            )
        if quote_refresh.get("status") != "ok":
            data_quality_reasons.append(
                f"quote_refresh_status={quote_refresh.get('status')}"
            )
        if refreshed_quote_tokens != selected_token_set:
            data_quality_reasons.append(
                "current_quote_response_does_not_cover_selected_contracts"
            )
        if settings.option_greeks_enabled and len(greeks_tokens) != expected_contract_count:
            data_quality_reasons.append(
                f"quotes_with_greeks={len(greeks_tokens)}/{expected_contract_count}"
            )
        if (
            settings.option_greeks_enabled
            and len(usable_greeks_tokens) != expected_contract_count
        ):
            data_quality_reasons.append(
                f"quotes_with_iv_delta={len(usable_greeks_tokens)}/"
                f"{expected_contract_count}"
            )
        if settings.option_greeks_enabled and greeks_refresh.get("status") != "ok":
            data_quality_reasons.append(
                f"greeks_refresh_status={greeks_refresh.get('status')}"
            )
        if (
            settings.option_greeks_enabled
            and refreshed_greeks_tokens != selected_token_set
        ):
            data_quality_reasons.append(
                "current_greeks_response_does_not_cover_selected_contracts"
            )
        if current_spot_observed_at is None:
            data_quality_reasons.append("spot_observation_missing")
        frame_completed_at = datetime.now(UTC)
        frame_duration_ms = (
            frame_completed_at - frame_started_at
        ).total_seconds() * 1000
        if runtime_metrics is not None:
            runtime_metrics.observe_frame(frame_duration_ms)
        spot_age_ms = (
            (
                snapshot_captured_at - current_spot_observed_at
            ).total_seconds()
            * 1000
            if current_spot_observed_at is not None
            else None
        )
        if (
            spot_age_ms is not None
            and spot_age_ms
            > settings.signal_gate_max_underlying_age_seconds * 1000
        ):
            data_quality_reasons.append(
                f"spot_stale_ms={spot_age_ms:.1f}"
            )
        market = snapshot.market
        research_quality_reasons = list(data_quality_reasons)
        if market is None:
            research_quality_reasons.append("market_context_missing")
        else:
            if market.open_price is None:
                research_quality_reasons.append("spot_open_missing")
            if market.previous_close is None:
                research_quality_reasons.append("spot_previous_close_missing")
            if market.spot_observed_at is None:
                research_quality_reasons.append(
                    "spot_exchange_timestamp_missing"
                )
            if market.future_observed_at is None:
                research_quality_reasons.append(
                    "future_exchange_timestamp_missing"
                )
            if market.future_price is None:
                research_quality_reasons.append("future_price_missing")
            if market.future_volume is None:
                research_quality_reasons.append("future_volume_missing")
            if market.future_oi is None:
                research_quality_reasons.append("future_oi_missing")
        atm_bid_ask_types = {
            quote.contract.option_type.value
            for quote in snapshot.quotes
            if quote.contract.strike == snapshot.atm_strike
            and quote.contract.token.token in bid_ask_token_set
        }
        if atm_bid_ask_types != {"CE", "PE"}:
            research_quality_reasons.append(
                "synchronized_atm_bid_ask_pair_missing"
            )
        frame_metadata = {
            "scheduled_for": scheduled_for,
            "frame_started_at": frame_started_at,
            "frame_completed_at": frame_completed_at,
            "trigger_tick_received_at": trigger_tick_received_at,
            "schedule_lag_ms": (
                frame_started_at - scheduled_for
            ).total_seconds()
            * 1000,
            "configured_interval_ms": settings.snapshot_interval_ms,
            "spot": {
                "value": current_spot_price,
                "observed_at": current_spot_observed_at,
                "age_ms": spot_age_ms,
                "source": "websocket",
            },
            "window": {
                "each_side": settings.option_window_each_side,
                "expected_contract_count": expected_contract_count,
                "selected_contract_count": len(contracts),
                "selected_contract_tokens": selected_tokens,
                "quote_tokens": quote_tokens,
                "greeks_tokens": greeks_tokens,
                "valid_bid_ask_tokens": bid_ask_tokens,
                "oi_volume_tokens": oi_volume_tokens,
                "usable_iv_delta_tokens": usable_greeks_tokens,
            },
            "market_context": market,
            "quote_refresh": quote_refresh,
            "greeks_refresh": greeks_refresh,
            "preflight": {
                "status": "PASSED" if preflight_error is None else "REJECTED",
                "reason": preflight_error,
            },
            "data_quality": {
                "status": (
                    "VALID"
                    if not data_quality_reasons
                    else (
                        "INVALID_DATA"
                        if settings.replay_require_complete_window
                        else "DEGRADED"
                    )
                ),
                "reasons": tuple(data_quality_reasons),
            },
            "research_quality": {
                "status": (
                    "RESEARCH_READY"
                    if not research_quality_reasons
                    else "PARTIAL"
                ),
                "reasons": tuple(research_quality_reasons),
                "opening_context_ready": (
                    market is not None
                    and market.open_price is not None
                    and market.previous_close is not None
                ),
                "future_flow_ready": (
                    market is not None
                    and market.future_price is not None
                    and market.future_volume is not None
                    and market.future_oi is not None
                ),
                "expected_move_ready": atm_bid_ask_types == {"CE", "PE"},
                "premium_attribution_ready": (
                    len(usable_greeks_tokens) == expected_contract_count
                    and len(bid_ask_tokens) == expected_contract_count
                ),
            },
            "performance": (
                runtime_metrics.snapshot(
                    feed_health=feed_health,
                    recorder_health=(
                        microstructure_recorder.health_snapshot()
                        if microstructure_recorder is not None
                        else None
                    ),
                )
                if runtime_metrics is not None
                else {
                    "feed": feed_health
                    or {"status": "UNAVAILABLE"},
                }
            ),
        }
        await microstructure_recorder.record_gate_decision(
            snapshot=snapshot,
            decision=gate_decision,
            analytics=targeted_raw_analytics,
            frame=frame_metadata,
            execution_signal=_execution_signal_record(
                entry_signal,
                dispatch_status=dispatch_status,
            ),
        )
    chain_write_started = perf_counter()
    await chain_store.save_chain_snapshot(snapshot)
    if runtime_metrics is not None:
        runtime_metrics.observe_chain_write(
            (perf_counter() - chain_write_started) * 1000
        )
    await live_store.publish_chain_snapshot(snapshot)
    await live_store.publish_analytics_snapshot(analytics_snapshot)
    return analytics_snapshot.put_call_ratio_oi


def _refresh_active_target_ltp(
    *,
    snapshot,
    target: ActiveStrategyTarget,
) -> ActiveStrategyTarget:
    for quote in snapshot.quotes:
        if (
            quote.contract.strike == target.strike
            and quote.contract.option_type == target.option_type
        ):
            return ActiveStrategyTarget(
                source=target.source,
                side=target.side,
                strike=target.strike,
                option_type=target.option_type,
                ltp=quote.ltp,
                delta=quote.greeks.delta if quote.greeks else target.delta,
                captured_at=target.captured_at,
            )
    return target


'''
def _update_spot_price(
    tick: MarketTick,
    spot_tokens: dict[str, InstrumentToken],
    spot_prices: dict[str, Decimal],
) -> None:
    if tick.ltp is None:
        return
    for underlying, token in spot_tokens.items():
        if token.token == tick.token.token:
            spot_prices[underlying] = tick.ltp
'''
def _update_spot_price(
    tick: MarketTick,
    spot_tokens: dict[str, InstrumentToken],
    spot_prices: dict[str, Decimal],
) -> None:
    # 1. Reject ticks that do not contain a Last Traded Price
    if tick.ltp is None:
        return
        
    for underlying, token in spot_tokens.items():
        if token.token == tick.token.token:
            # Check if the price actually changed
            previous_price = spot_prices.get(underlying)
            
            if previous_price != tick.ltp:
                spot_prices[underlying] = tick.ltp
                
                # Optional: Uncomment this line to debug if NSE tokens are arriving
                # print(f"[DEBUG] {underlying} Spot updated: {previous_price} -> {tick.ltp}")
