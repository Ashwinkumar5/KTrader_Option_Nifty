from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from app.analytics.engine import AnalyticsEngine
from app.broker.angleone.instruments import build_instrument_master
from app.core.config import Settings, load_settings
from app.core.strategy_config import (
    StrategyProfile,
    apply_runtime_strategy_selection,
    load_strategy_configuration,
)
from app.domain.models import AnalyticsSnapshot, OptionChainSnapshot, OptionQuote
from app.execution.paper import PaperExecutionEngine, PaperFill
from app.execution.risk import PositionSizer, PositionSizingSettings
from app.greeks.broker import normalize_broker_greeks
from app.instruments.master import InstrumentMaster
from app.marketdata.normalizer import normalize_tick
from app.microstructure.engine import MicrostructureEngine, MicrostructureSettings
from app.optionchain.state import OptionChainState
from app.signals.display import format_signal_line
from app.signals.gate import SignalGate, SignalGateDecision, SignalGateSettings
from app.signals.timely_entry import TimelyEntryGuard, TimelyEntryTrigger
from app.storage.serialization import to_jsonable

from .dummy_broker import RecordedBrokerClient, quote_rows
from .dummy_broker_feed import RecordedMarketDataFeed
from .reader import RecordedSessionReader, SessionAudit
from .serde import parse_snapshot
class ReplayMode(StrEnum):
    FAITHFUL = "faithful"
    EVENT_TIME = "event-time"


@dataclass(frozen=True)
class ReplayResult:
    run_directory: Path
    mode: str
    source_path: Path
    market_events_seen: int
    market_events_decoded: int
    microstructure_candidates: int
    frames_processed: int
    source_qualified: int
    replay_qualified: int
    strong_signals_count: int
    strong_signal_details: tuple[dict[str, object], ...]
    qualified_by_side: dict[str, int]
    raw_by_side: dict[str, int]
    setup_counts: dict[str, int]
    strategy_candidate_counts: dict[str, int]
    selected_strategy_counts: dict[str, int]
    qualified_strategy_counts: dict[str, int]
    paper_outcomes_by_strategy: dict[str, dict[str, int]]
    rejection_counts: dict[str, int]
    enabled_strategies: tuple[str, ...]
    strategy_priority: tuple[str, ...]
    resolver_policy: str
    gamma_candidates: int
    gamma_qualified: int
    paper_entries: int
    paper_exits: int
    target_exits: int
    stop_exits: int
    time_exits: int
    management_exits: int
    unresolved_positions: int
    completed_trade_return_percent: Decimal
    average_trade_return_percent: Decimal
    maximum_trade_drawdown_percent: Decimal
    paper_realized_pnl: Decimal
    round_trip_cost_percent: Decimal
    estimated_transaction_cost: Decimal
    net_completed_trade_return_percent: Decimal
    net_average_trade_return_percent: Decimal
    net_maximum_trade_drawdown_percent: Decimal
    net_paper_realized_pnl: Decimal
    average_maximum_favorable_excursion_percent: Decimal
    average_maximum_adverse_excursion_percent: Decimal
    feature_coverage: dict[str, dict[str, Decimal | int]]
    unique_session_days: int
    sufficient_evidence: bool


async def run_replay(
    source_path: Path,
    *,
    mode: ReplayMode | str = ReplayMode.EVENT_TIME,
    output_root: Path | None = None,
    run_id: str | None = None,
    max_frames: int | None = None,
    settings: Settings | None = None,
    source_sha256: str | None = None,
    event_index_path: Path | None = None,
    session_audit: SessionAudit | None = None,
    decision_file_name: str = "gate_decisions.jsonl",
    write_all_decisions: bool = True,
    run_directory_name: str | None = None,
    round_trip_cost_percent: Decimal = Decimal("0"),
    enabled_strategies: tuple[str, ...] | None = None,
    enabled_features: tuple[str, ...] | None = None,
    minimum_book_imbalance: Decimal | None = None,
    strategy_priority: tuple[str, ...] | None = None,
) -> ReplayResult:
    round_trip_cost_percent = Decimal(str(round_trip_cost_percent))
    if round_trip_cost_percent < 0:
        raise ValueError("round_trip_cost_percent cannot be negative")
    replay_mode = ReplayMode(str(mode))
    source_path = source_path.resolve()
    settings = settings or load_settings()
    strategy_configuration = load_strategy_configuration(
        settings.strategy_config_path or None,
        profile_name=settings.strategy_profile,
    )
    strategy_configuration = apply_runtime_strategy_selection(
        strategy_configuration,
        enabled_strategies=enabled_strategies,
        enabled_features=enabled_features,
        minimum_book_imbalance=minimum_book_imbalance,
        strategy_priority=strategy_priority,
    )
    output_root = (
        output_root.resolve()
        if output_root is not None
        else (Path(__file__).resolve().parent / "runs")
    )
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory_name = (
        run_directory_name
        or f"{source_path.stem}_{replay_mode.value}_{run_id}"
    )
    directory_path = Path(directory_name)
    if (
        directory_path.name != directory_name
        or directory_name in {".", ".."}
    ):
        raise ValueError("run_directory_name must be a plain directory name")
    run_directory = output_root / directory_name
    run_directory.mkdir(parents=True, exist_ok=False)
    decision_name = Path(decision_file_name)
    if (
        decision_name.name != decision_file_name
        or decision_name.suffix.lower() != ".jsonl"
    ):
        raise ValueError(
            "decision_file_name must be a plain .jsonl file name"
        )

    logger = _build_logger(run_directory / "run.log", run_id)
    reader = RecordedSessionReader(source_path)
    if session_audit is not None:
        if session_audit.source_path.resolve() != source_path:
            raise ValueError("session audit does not match replay source")
        audit = session_audit
        logger.info("Using shared source audit: %s", source_path)
    else:
        logger.info("Auditing source capture: %s", source_path)
        audit = reader.audit()
    _write_json(run_directory / "schema_audit.json", _audit_payload(audit))
    logger.info(
        "Audit: market_events=%d frames=%d contracts=%d quotes=%d greeks=%d "
        "excluded_contaminated_contracts=%d timestamp_regressions=%d "
        "max_regression=%.3fs",
        audit.market_events,
        audit.gate_frames,
        len(audit.unique_contracts),
        audit.quotes,
        audit.quotes_with_greeks,
        audit.excluded_contaminated_contracts,
        audit.timestamp_regressions,
        audit.maximum_regression_seconds,
    )

    settings = _apply_capture_configuration(
        settings,
        audit.capture_configuration,
    )
    _write_json(
        run_directory / "run_manifest.json",
        _run_manifest(
            source_path=source_path,
            replay_mode=replay_mode,
            run_id=run_id,
            max_frames=max_frames,
            settings=settings,
            strategy_configuration=strategy_configuration.manifest(),
            source_sha256=source_sha256,
            capture_configuration=audit.capture_configuration,
            write_all_decisions=write_all_decisions,
            round_trip_cost_percent=round_trip_cost_percent,
        ),
    )
    broker = RecordedBrokerClient(
        audit.unique_contracts,
        spot_tokens=audit.spot_tokens,
        future_contracts=audit.future_contracts,
    )
    await broker.login()
    raw_master = await broker.instrument_master()
    master = build_instrument_master(
        raw_master,
        underlyings=("NIFTY",),
    )
    if not master.options or not master.spot_tokens:
        raise RuntimeError("Dummy broker could not reconstruct the instrument master")

    feed = RecordedMarketDataFeed()
    await feed.connect()
    await feed.subscribe(
        tuple(master.spot_tokens.values())
        + tuple(contract.token for contract in master.futures)
        + tuple(contract.token for contract in master.options)
    )
    token_lookup = {
        token.token: token for token in master.spot_tokens.values()
    }
    token_lookup.update(
        {contract.token.token: contract.token for contract in master.futures}
    )
    token_lookup.update(
        {contract.token.token: contract.token for contract in master.options}
    )
    state = OptionChainState(master=master)
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
    micro_engine = MicrostructureEngine(
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
    gate = SignalGate(
        SignalGateSettings(
            min_confirmations=settings.signal_gate_min_confirmations,
            cooldown_seconds=settings.signal_gate_cooldown_seconds,
            local_reversal_cooldown_seconds=(
                settings.local_reversal_cooldown_seconds
            ),
            max_level_distance=Decimal(
                str(settings.signal_gate_level_distance_points)
            ),
            max_microstructure_age_seconds=(
                strategy_configuration.profile.microstructure
                .maximum_age_seconds
            ),
            mode="shadow",
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
            allowed_underlyings=("NIFTY",),
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
            max_gross_exposure=Decimal(
                str(settings.risk_max_gross_exposure)
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
    position_sizer = PositionSizer(
        PositionSizingSettings(
            account_capital=Decimal(str(settings.execution_account_capital)),
            risk_per_trade_percent=Decimal(
                str(settings.execution_risk_per_trade_percent)
            ),
            max_gross_exposure=Decimal(
                str(settings.risk_max_gross_exposure)
            ),
            option_stop_loss_fraction=(
                strategy_configuration.profile.execution.stop_percent
                / Decimal("100")
            ),
            reward_risk_multiple=(
                strategy_configuration.profile.execution.target_percent
                / strategy_configuration.profile.execution.stop_percent
            ),
        )
    )
    paper_execution = PaperExecutionEngine(
        max_positions=max(1, settings.risk_max_concurrent_positions),
        maximum_holding_minutes=(
            strategy_configuration.profile.execution
            .maximum_hold_minutes
        ),
        trailing_activation_percent=(
            strategy_configuration.profile.execution
            .trailing_activation_percent
        ),
        trailing_drawdown_percent=(
            strategy_configuration.profile.execution
            .trailing_drawdown_percent
        ),
        no_follow_through_seconds=(
            strategy_configuration.profile.execution
            .no_follow_through_seconds
        ),
        minimum_follow_through_percent=(
            strategy_configuration.profile.execution
            .minimum_follow_through_percent
        ),
    )

    counters: Counter[str] = Counter()
    raw_by_side: Counter[str] = Counter()
    qualified_by_side: Counter[str] = Counter()
    setup_counts: Counter[str] = Counter()
    strategy_candidate_counts: Counter[str] = Counter()
    selected_strategy_counts: Counter[str] = Counter()
    qualified_strategy_counts: Counter[str] = Counter()
    rejection_counts: Counter[str] = Counter()
    latest_micro = None
    latest_option_book_at: datetime | None = None
    latest_futures_book_at: datetime | None = None
    latest_replay_at: datetime | None = None
    decision_path = run_directory / decision_file_name
    entry_prices: dict[str, Decimal] = {}
    entry_strategies: dict[str, str] = {}
    strong_signal_details: list[dict[str, object]] = []
    active_signal_detail_by_token: dict[str, int] = {}
    paper_outcomes_by_strategy: dict[str, Counter[str]] = defaultdict(
        Counter
    )
    completed_trade_returns: list[Decimal] = []
    net_completed_trade_returns: list[Decimal] = []
    completed_trade_mfe_percent: list[Decimal] = []
    completed_trade_mae_percent: list[Decimal] = []
    completed_trade_return_percent = Decimal("0")
    net_completed_trade_return_percent = Decimal("0")
    estimated_transaction_cost = Decimal("0")
    coverage_counts: Counter[str] = Counter()
    unique_session_dates: set = set()
    index_path = (
        event_index_path.resolve()
        if event_index_path is not None
        else run_directory / "event_time_index.sqlite"
    )

    with decision_path.open("w", encoding="utf-8") as decision_file:
        for line_number, record in reader.records(
            mode=replay_mode.value,
            index_path=index_path,
        ):
            record_type = record.get("record_type")
            if record_type == "market_event":
                counters["market_events_seen"] += 1
                tick = feed.decode_market_event(record)
                if tick is None:
                    continue
                if not _is_nifty_token(tick.token.symbol, tick.token.trading_symbol):
                    continue
                counters["market_events_decoded"] += 1
                latest_replay_at = tick.received_at
                unique_session_dates.add(latest_replay_at.date())
                state.update_tick(tick)
                tick_exit_fills = (
                    paper_execution.mark_tick(tick)
                    if strategy_configuration.profile.execution
                    .event_driven_exit
                    else ()
                )
                if tick_exit_fills:
                    _update_strong_signal_exits(
                        tick_exit_fills,
                        details=strong_signal_details,
                        active_by_token=active_signal_detail_by_token,
                    )
                    counters["paper_exits"] += len(tick_exit_fills)
                    gross_return, net_return, exit_cost = _record_paper_exits(
                        tick_exit_fills,
                        entry_prices=entry_prices,
                        entry_strategies=entry_strategies,
                        strategy_outcomes=paper_outcomes_by_strategy,
                        completed_trade_returns=completed_trade_returns,
                        net_completed_trade_returns=(
                            net_completed_trade_returns
                        ),
                        completed_trade_mfe_percent=(
                            completed_trade_mfe_percent
                        ),
                        completed_trade_mae_percent=(
                            completed_trade_mae_percent
                        ),
                        counters=counters,
                        round_trip_cost_percent=round_trip_cost_percent,
                    )
                    completed_trade_return_percent += gross_return
                    net_completed_trade_return_percent += net_return
                    estimated_transaction_cost += exit_cost
                    gate.update_risk_state(
                        realized_pnl=paper_execution.realized_pnl,
                        open_positions=paper_execution.open_positions,
                        gross_exposure=paper_execution.gross_exposure,
                    )
                micro_features, micro_signal = micro_engine.observe(tick)
                if (
                    micro_features is not None
                    and micro_features.has_complete_book
                ):
                    if tick.token.kind.value == "future":
                        latest_futures_book_at = micro_features.captured_at
                    elif tick.token.kind.value == "option":
                        latest_option_book_at = micro_features.captured_at
                if micro_signal is not None:
                    counters["microstructure_candidates"] += 1
                    latest_micro = micro_signal
                    gate.observe_microstructure(micro_signal)
                    trigger = timely_entry_guard.consider(
                        tick=tick,
                        signal=micro_signal,
                    )
                    if trigger is not None:
                        (
                            event_snapshot,
                            event_analytics,
                            event_underlying_observed_at,
                        ) = _materialize_timely_trigger(
                            trigger=trigger,
                            tick=tick,
                            state=state,
                            master=master,
                        )
                        gated_event_analytics, event_decision = gate.evaluate(
                            snapshot=event_snapshot,
                            analytics=event_analytics,
                            microstructure_signal=micro_signal,
                            underlying_observed_at=(
                                event_underlying_observed_at
                            ),
                            refreshed_quote_tokens=set(
                                trigger.candidate.refreshed_quote_tokens
                            ),
                            refreshed_greeks_tokens=set(
                                trigger.candidate.refreshed_greeks_tokens
                            ),
                            microstructure_not_before=(
                                trigger.candidate.armed_at
                            ),
                        )
                        event_entry_fill = None
                        event_position_plan = None
                        event_target_quote = None
                        if event_decision.qualified:
                            event_target_quote = _selected_quote(
                                event_snapshot,
                                event_analytics,
                            )
                            if event_target_quote is not None:
                                event_position_plan = (
                                    position_sizer.size_long_option(
                                        event_target_quote
                                    )
                                )
                            if event_position_plan is not None:
                                event_entry_fill = paper_execution.submit(
                                    event_position_plan,
                                    event_snapshot.captured_at,
                                )
                            counters["replay_qualified"] += 1
                            qualified_by_side[
                                event_decision.strong_signal
                            ] += 1
                            if event_analytics.selected_strategy is not None:
                                qualified_strategy_counts[
                                    event_analytics.selected_strategy.value
                                ] += 1
                            if event_entry_fill is not None:
                                counters["paper_entries"] += 1
                                entry_prices[event_entry_fill.token] = (
                                    event_entry_fill.price
                                )
                                entry_strategies[event_entry_fill.token] = (
                                    event_analytics.selected_strategy.value
                                    if event_analytics.selected_strategy
                                    is not None
                                    else "UNATTRIBUTED"
                                )
                                gate.update_risk_state(
                                    realized_pnl=paper_execution.realized_pnl,
                                    open_positions=(
                                        paper_execution.open_positions
                                    ),
                                    gross_exposure=(
                                        paper_execution.gross_exposure
                                    ),
                                )
                            signal_detail = {
                                "signal_time": event_snapshot.captured_at,
                                "side": event_decision.strong_signal,
                                "strategy": (
                                    event_analytics.selected_strategy.value
                                    if event_analytics.selected_strategy
                                    is not None
                                    else "UNATTRIBUTED"
                                ),
                                "strike": (
                                    event_target_quote.contract.strike
                                    if event_target_quote is not None
                                    else event_analytics.target_strike
                                ),
                                "option_type": (
                                    event_target_quote.contract.option_type.value
                                    if event_target_quote is not None
                                    else None
                                ),
                                "token": (
                                    event_target_quote.contract.token.token
                                    if event_target_quote is not None
                                    else None
                                ),
                                "entry_price": (
                                    event_entry_fill.price
                                    if event_entry_fill is not None
                                    else trigger.ask
                                ),
                                "stop_percent": (
                                    strategy_configuration.profile.execution
                                    .stop_percent
                                ),
                                "target_percent": (
                                    strategy_configuration.profile.execution
                                    .target_percent
                                ),
                                "horizon_minutes": (
                                    strategy_configuration.profile.execution
                                    .maximum_hold_minutes
                                ),
                                "outcome": (
                                    "OPEN"
                                    if event_entry_fill is not None
                                    else "NOT_ENTERED"
                                ),
                                "exit_time": None,
                                "exit_price": None,
                                "gain_percent": None,
                                "entry_trigger": "event_driven",
                                "candidate_armed_at": (
                                    trigger.candidate.armed_at
                                ),
                                "premium_chase_percent": (
                                    trigger.premium_chase_percent
                                ),
                            }
                            strong_signal_details.append(signal_detail)
                            if event_entry_fill is not None:
                                active_signal_detail_by_token[
                                    event_entry_fill.token
                                ] = len(strong_signal_details) - 1
                            logger.info(
                                "QUALIFIED event_line=%d %s",
                                line_number,
                                format_signal_line(
                                    snapshot=event_snapshot,
                                    analytics=gated_event_analytics,
                                    gate_decision=event_decision,
                                ),
                            )
                        else:
                            rejection_counts[
                                _rejection_bucket(event_decision.reason)
                            ] += 1
                        event_record = {
                            "schema_version": 1,
                            "record_type": "replay_event_entry_decision",
                            "replay_mode": replay_mode.value,
                            "source_line": line_number,
                            "captured_at": event_snapshot.captured_at,
                            "populated_snapshot": event_snapshot,
                            "analytics": event_analytics,
                            "gated_analytics": gated_event_analytics,
                            "decision": event_decision,
                            "position_plan": event_position_plan,
                            "paper_entry_fill": event_entry_fill,
                            "candidate_armed_at": (
                                trigger.candidate.armed_at
                            ),
                            "premium_chase_percent": (
                                trigger.premium_chase_percent
                            ),
                        }
                        decision_file.write(
                            json.dumps(
                                to_jsonable(event_record),
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                continue

            if record_type != "gate_decision":
                continue
            if max_frames is not None and counters["frames_processed"] >= max_frames:
                break

            source_snapshot_data = record.get("snapshot")
            if not isinstance(source_snapshot_data, dict):
                raise ValueError(f"Missing snapshot at source line {line_number}")
            source_snapshot = parse_snapshot(source_snapshot_data)
            if source_snapshot.underlying.upper() != "NIFTY":
                counters["non_nifty_frames_skipped"] += 1
                continue
            broker.set_frame(source_snapshot_data)
            populated_snapshot = await _populate_snapshot(
                broker=broker,
                state=state,
                token_lookup=token_lookup,
                master=master,
                source_snapshot=source_snapshot,
                each_side=settings.option_window_each_side,
            )
            latest_replay_at = populated_snapshot.captured_at
            unique_session_dates.add(latest_replay_at.date())
            exit_fills = paper_execution.mark(populated_snapshot)
            _update_strong_signal_exits(
                exit_fills,
                details=strong_signal_details,
                active_by_token=active_signal_detail_by_token,
            )
            counters["paper_exits"] += len(exit_fills)
            gross_return, net_return, exit_cost = _record_paper_exits(
                exit_fills,
                entry_prices=entry_prices,
                entry_strategies=entry_strategies,
                strategy_outcomes=paper_outcomes_by_strategy,
                completed_trade_returns=completed_trade_returns,
                net_completed_trade_returns=net_completed_trade_returns,
                completed_trade_mfe_percent=completed_trade_mfe_percent,
                completed_trade_mae_percent=completed_trade_mae_percent,
                counters=counters,
                round_trip_cost_percent=round_trip_cost_percent,
            )
            completed_trade_return_percent += gross_return
            net_completed_trade_return_percent += net_return
            estimated_transaction_cost += exit_cost
            gate.update_risk_state(
                realized_pnl=paper_execution.realized_pnl,
                open_positions=paper_execution.open_positions,
                gross_exposure=paper_execution.gross_exposure,
            )
            refreshed_quote_tokens = {
                quote.contract.token.token
                for quote in populated_snapshot.quotes
            }
            refreshed_greeks_tokens = {
                quote.contract.token.token
                for quote in populated_snapshot.quotes
                if quote.greeks is not None
            }
            preflight_error = gate.preflight_data(
                snapshot=populated_snapshot,
                underlying_observed_at=populated_snapshot.captured_at,
                refreshed_quote_tokens=refreshed_quote_tokens,
                refreshed_greeks_tokens=refreshed_greeks_tokens,
            )
            management_fills = ()
            if preflight_error is not None:
                timely_entry_guard.cancel(populated_snapshot.underlying)
                targeted_analytics, decision = gate.reject_preflight(
                    snapshot=populated_snapshot,
                    reason=preflight_error,
                )
                gated_analytics = targeted_analytics
            else:
                raw_analytics = analytics_engine.from_chain(
                    populated_snapshot
                )
                management_fills = paper_execution.apply_management(
                    populated_snapshot,
                    raw_analytics.momentum_exhaustion,
                )
                _update_strong_signal_exits(
                    management_fills,
                    details=strong_signal_details,
                    active_by_token=active_signal_detail_by_token,
                )
                counters["paper_exits"] += len(management_fills)
                gross_return, net_return, exit_cost = _record_paper_exits(
                    management_fills,
                    entry_prices=entry_prices,
                    entry_strategies=entry_strategies,
                    strategy_outcomes=paper_outcomes_by_strategy,
                    completed_trade_returns=completed_trade_returns,
                    net_completed_trade_returns=net_completed_trade_returns,
                    completed_trade_mfe_percent=completed_trade_mfe_percent,
                    completed_trade_mae_percent=completed_trade_mae_percent,
                    counters=counters,
                    round_trip_cost_percent=round_trip_cost_percent,
                )
                completed_trade_return_percent += gross_return
                net_completed_trade_return_percent += net_return
                estimated_transaction_cost += exit_cost
                gate.update_risk_state(
                    realized_pnl=paper_execution.realized_pnl,
                    open_positions=paper_execution.open_positions,
                    gross_exposure=paper_execution.gross_exposure,
                )
                targeted_analytics = analytics_engine.with_optimal_target(
                    snapshot=populated_snapshot,
                    analytics=raw_analytics,
                )
                gated_analytics, decision = gate.evaluate(
                    snapshot=populated_snapshot,
                    analytics=targeted_analytics,
                    microstructure_signal=latest_micro,
                    underlying_observed_at=populated_snapshot.captured_at,
                    refreshed_quote_tokens=refreshed_quote_tokens,
                    refreshed_greeks_tokens=refreshed_greeks_tokens,
                    microstructure_not_before=(
                        timely_entry_guard.microstructure_not_before(
                            populated_snapshot.captured_at
                        )
                    ),
                )
                timely_entry_guard.arm_from_decision(
                    snapshot=populated_snapshot,
                    analytics=targeted_analytics,
                    decision=decision,
                    refreshed_quote_tokens=refreshed_quote_tokens,
                    refreshed_greeks_tokens=refreshed_greeks_tokens,
                    underlying_observed_at=populated_snapshot.captured_at,
                )
            for feature, available in _feature_availability(
                populated_snapshot,
                targeted_analytics,
                option_book_available=_is_recent(
                    latest_option_book_at,
                    populated_snapshot.captured_at,
                    strategy_configuration.profile.microstructure
                    .maximum_age_seconds,
                ),
                futures_book_available=_is_recent(
                    latest_futures_book_at,
                    populated_snapshot.captured_at,
                    strategy_configuration.profile.microstructure
                    .maximum_age_seconds,
                ),
            ).items():
                if available:
                    coverage_counts[feature] += 1
            entry_fill = None
            position_plan = None
            target_quote = None
            if decision.qualified:
                target_quote = _selected_quote(
                    populated_snapshot,
                    targeted_analytics,
                )
                if target_quote is not None:
                    position_plan = position_sizer.size_long_option(target_quote)
                if position_plan is not None:
                    entry_fill = paper_execution.submit(
                        position_plan,
                        populated_snapshot.captured_at,
                    )
                if entry_fill is not None:
                    counters["paper_entries"] += 1
                    entry_prices[entry_fill.token] = entry_fill.price
                    entry_strategies[entry_fill.token] = (
                        targeted_analytics.selected_strategy.value
                        if targeted_analytics.selected_strategy is not None
                        else "UNATTRIBUTED"
                    )
                    gate.update_risk_state(
                        realized_pnl=paper_execution.realized_pnl,
                        open_positions=paper_execution.open_positions,
                        gross_exposure=paper_execution.gross_exposure,
                    )
                signal_detail = {
                    "signal_time": populated_snapshot.captured_at,
                    "side": decision.strong_signal,
                    "strategy": (
                        targeted_analytics.selected_strategy.value
                        if targeted_analytics.selected_strategy is not None
                        else "UNATTRIBUTED"
                    ),
                    "strike": (
                        target_quote.contract.strike
                        if target_quote is not None
                        else targeted_analytics.target_strike
                    ),
                    "option_type": (
                        target_quote.contract.option_type.value
                        if target_quote is not None
                        else (
                            targeted_analytics.target_option_type.value
                            if targeted_analytics.target_option_type is not None
                            else None
                        )
                    ),
                    "token": (
                        target_quote.contract.token.token
                        if target_quote is not None
                        else None
                    ),
                    "entry_price": (
                        entry_fill.price
                        if entry_fill is not None
                        else target_quote.ask
                        if target_quote is not None
                        else None
                    ),
                    "stop_percent": (
                        strategy_configuration.profile.execution.stop_percent
                    ),
                    "target_percent": (
                        strategy_configuration.profile.execution.target_percent
                    ),
                    "horizon_minutes": (
                        strategy_configuration.profile.execution
                        .maximum_hold_minutes
                    ),
                    "outcome": (
                        "OPEN"
                        if entry_fill is not None
                        else "NOT_ENTERED"
                    ),
                    "exit_time": None,
                    "exit_price": None,
                    "gain_percent": None,
                }
                strong_signal_details.append(signal_detail)
                if entry_fill is not None:
                    active_signal_detail_by_token[entry_fill.token] = (
                        len(strong_signal_details) - 1
                    )

            counters["frames_processed"] += 1
            raw_by_side[decision.raw_signal] += 1
            setup_counts[decision.setup_type.value] += 1
            frame_candidate_families = {
                candidate.family.value
                for candidate in targeted_analytics.strategy_candidates
            }
            for family in frame_candidate_families:
                strategy_candidate_counts[family] += 1
            if targeted_analytics.selected_strategy is not None:
                selected_strategy_counts[
                    targeted_analytics.selected_strategy.value
                ] += 1
            if "GAMMA_EXPANSION" in frame_candidate_families:
                counters["gamma_candidates"] += 1
            if decision.qualified:
                counters["replay_qualified"] += 1
                qualified_by_side[decision.strong_signal] += 1
                if targeted_analytics.selected_strategy is not None:
                    qualified_strategy_counts[
                        targeted_analytics.selected_strategy.value
                    ] += 1
                if (
                    targeted_analytics.selected_strategy is not None
                    and targeted_analytics.selected_strategy.value
                    == "GAMMA_EXPANSION"
                ):
                    counters["gamma_qualified"] += 1
                logger.info(
                    "QUALIFIED source_line=%d %s",
                    line_number,
                    format_signal_line(
                        snapshot=populated_snapshot,
                        analytics=gated_analytics,
                        gate_decision=decision,
                    ),
                )
            else:
                bucket = _rejection_bucket(decision.reason)
                rejection_counts[bucket] += 1
                logger.info(
                    "REJECTED source_line=%d captured_at=%s raw=%s setup=%s reason=%s",
                    line_number,
                    populated_snapshot.captured_at.isoformat(),
                    decision.raw_signal,
                    decision.setup_type.value,
                    decision.reason,
                )

            replay_record = {
                "schema_version": 1,
                "record_type": "replay_gate_decision",
                "replay_mode": replay_mode.value,
                "source_line": line_number,
                "captured_at": populated_snapshot.captured_at,
                "source_snapshot": source_snapshot,
                "populated_snapshot": populated_snapshot,
                "analytics": targeted_analytics,
                "gated_analytics": gated_analytics,
                "decision": decision,
                "position_plan": position_plan,
                "paper_entry_fill": entry_fill,
                "paper_exit_fills": exit_fills + management_fills,
            }
            if (
                write_all_decisions
                or decision.qualified
                or entry_fill is not None
                or exit_fills
                or management_fills
            ):
                decision_file.write(
                    json.dumps(
                        to_jsonable(replay_record),
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            if counters["frames_processed"] % 100 == 0:
                print(
                    f"[{replay_mode.value}] frames={counters['frames_processed']} "
                    f"qualified={counters['replay_qualified']} "
                    f"micro_candidates={counters['microstructure_candidates']}"
                )

    if (
        strategy_configuration.profile.execution.close_at_tape_end
        and latest_replay_at is not None
    ):
        final_fills = paper_execution.close_all(
            latest_replay_at,
            reason="tape_end",
        )
        _update_strong_signal_exits(
            final_fills,
            details=strong_signal_details,
            active_by_token=active_signal_detail_by_token,
        )
        counters["paper_exits"] += len(final_fills)
        gross_return, net_return, exit_cost = _record_paper_exits(
            final_fills,
            entry_prices=entry_prices,
            entry_strategies=entry_strategies,
            strategy_outcomes=paper_outcomes_by_strategy,
            completed_trade_returns=completed_trade_returns,
            net_completed_trade_returns=net_completed_trade_returns,
            completed_trade_mfe_percent=completed_trade_mfe_percent,
            completed_trade_mae_percent=completed_trade_mae_percent,
            counters=counters,
            round_trip_cost_percent=round_trip_cost_percent,
        )
        completed_trade_return_percent += gross_return
        net_completed_trade_return_percent += net_return
        estimated_transaction_cost += exit_cost

    completed_trades = counters["paper_exits"]
    for strategy in entry_strategies.values():
        paper_outcomes_by_strategy[strategy]["unresolved"] += 1
    for detail_index in active_signal_detail_by_token.values():
        strong_signal_details[detail_index]["outcome"] = "OPEN_AT_TAPE_END"
    result = ReplayResult(
        run_directory=run_directory,
        mode=replay_mode.value,
        source_path=source_path,
        market_events_seen=counters["market_events_seen"],
        market_events_decoded=counters["market_events_decoded"],
        microstructure_candidates=counters["microstructure_candidates"],
        frames_processed=counters["frames_processed"],
        source_qualified=audit.source_qualified,
        replay_qualified=counters["replay_qualified"],
        strong_signals_count=len(strong_signal_details),
        strong_signal_details=tuple(strong_signal_details),
        qualified_by_side=dict(qualified_by_side),
        raw_by_side=dict(raw_by_side),
        setup_counts=dict(setup_counts),
        strategy_candidate_counts=dict(strategy_candidate_counts),
        selected_strategy_counts=dict(selected_strategy_counts),
        qualified_strategy_counts=dict(qualified_strategy_counts),
        paper_outcomes_by_strategy={
            strategy: dict(outcomes)
            for strategy, outcomes in paper_outcomes_by_strategy.items()
        },
        rejection_counts=dict(rejection_counts),
        enabled_strategies=_profile_strategy_names(
            strategy_configuration.profile
        ),
        strategy_priority=_profile_strategy_names(
            strategy_configuration.profile
        ),
        resolver_policy=settings.strategy_resolver_policy,
        gamma_candidates=counters["gamma_candidates"],
        gamma_qualified=counters["gamma_qualified"],
        paper_entries=counters["paper_entries"],
        paper_exits=counters["paper_exits"],
        target_exits=counters["target_exits"],
        stop_exits=counters["stop_exits"],
        time_exits=counters["time_exits"],
        management_exits=counters["management_exits"],
        unresolved_positions=paper_execution.open_positions,
        completed_trade_return_percent=(
            completed_trade_return_percent.quantize(Decimal("0.0001"))
        ),
        average_trade_return_percent=(
            (
                completed_trade_return_percent
                / Decimal(completed_trades)
            ).quantize(Decimal("0.0001"))
            if completed_trades
            else Decimal("0")
        ),
        maximum_trade_drawdown_percent=_maximum_drawdown(
            completed_trade_returns
        ),
        paper_realized_pnl=paper_execution.realized_pnl,
        round_trip_cost_percent=round_trip_cost_percent,
        estimated_transaction_cost=estimated_transaction_cost.quantize(
            Decimal("0.0001")
        ),
        net_completed_trade_return_percent=(
            net_completed_trade_return_percent.quantize(Decimal("0.0001"))
        ),
        net_average_trade_return_percent=(
            (
                net_completed_trade_return_percent
                / Decimal(completed_trades)
            ).quantize(Decimal("0.0001"))
            if completed_trades
            else Decimal("0")
        ),
        net_maximum_trade_drawdown_percent=_maximum_drawdown(
            net_completed_trade_returns
        ),
        net_paper_realized_pnl=(
            paper_execution.realized_pnl - estimated_transaction_cost
        ).quantize(Decimal("0.0001")),
        average_maximum_favorable_excursion_percent=_average_decimal(
            completed_trade_mfe_percent
        ),
        average_maximum_adverse_excursion_percent=_average_decimal(
            completed_trade_mae_percent
        ),
        feature_coverage=_coverage_summary(
            coverage_counts,
            counters["frames_processed"],
        ),
        unique_session_days=len(unique_session_dates),
        sufficient_evidence=(
            len(unique_session_dates) >= 8 and completed_trades >= 30
        ),
    )
    _write_json(run_directory / "summary.json", asdict(result))
    (run_directory / "summary.txt").write_text(
        _format_summary(result, audit),
        encoding="utf-8",
    )
    _write_regression_report(run_directory, result, audit)
    logger.info("Replay complete: %s", run_directory)
    _close_logger(logger)
    return result


async def _populate_snapshot(
    *,
    broker: RecordedBrokerClient,
    state: OptionChainState,
    token_lookup,
    master: InstrumentMaster,
    source_snapshot: OptionChainSnapshot,
    each_side: int,
) -> OptionChainSnapshot:
    # Option WebSocket events and REST quote frames have different timestamp
    # semantics. Reusing the event state can make OptionChainState reject the
    # recorded REST quote as "older", which silently drops its executable
    # bid/ask. Rebuild quote state per causal gate frame while retaining the
    # shared event state only for underlying/futures market context.
    frame_state = OptionChainState(master=master)
    requested: dict[str, list[str]] = {}
    for quote in source_snapshot.quotes:
        token = quote.contract.token
        if token.token not in token_lookup:
            continue
        requested.setdefault(token.exchange.value, []).append(token.token)
    response = await broker.market_quote(mode="FULL", exchange_tokens=requested)
    for payload in quote_rows(response):
        token_id = str(payload.get("token") or "")
        token = token_lookup.get(token_id)
        if token is None:
            continue
        frame_state.update_tick(
            normalize_tick(
                token=token,
                payload=payload,
                received_at=source_snapshot.captured_at,
            )
        )
    greeks_response = await broker.option_greeks(
        {
            "name": source_snapshot.underlying,
            "expirydate": source_snapshot.expiry.isoformat(),
        }
    )
    frame_state.update_greeks(
        normalize_broker_greeks(
            greeks_response,
            contracts=master.options,
            captured_at=source_snapshot.captured_at,
            source="dummy_broker.recorded_frame",
        )
    )
    populated = frame_state.build_snapshot(
        underlying=source_snapshot.underlying,
        expiry=source_snapshot.expiry,
        spot_price=source_snapshot.spot_price,
        each_side=each_side,
        captured_at=source_snapshot.captured_at,
        market=(
            source_snapshot.market
            or state.build_underlying_market_snapshot(
                underlying=source_snapshot.underlying,
                captured_at=source_snapshot.captured_at,
            )
        ),
    )
    source_tokens = {
        quote.contract.token.token
        for quote in source_snapshot.quotes
        if quote.contract.token.token in token_lookup
    }
    populated_tokens = {
        quote.contract.token.token for quote in populated.quotes
    }
    if source_tokens != populated_tokens:
        missing = source_tokens - populated_tokens
        extra = populated_tokens - source_tokens
        raise RuntimeError(
            f"Dummy broker frame mismatch: missing={sorted(missing)} "
            f"extra={sorted(extra)}"
        )
    return populated


def _build_logger(path: Path, run_id: str) -> logging.Logger:
    logger = logging.getLogger(f"dummy_broker_replay.{run_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _close_logger(logger)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    return logger


def _close_logger(logger: logging.Logger) -> None:
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def _rejection_bucket(reason: str) -> str:
    text = reason.lower()
    mappings = (
        ("candidate is not directional", "not_directional"),
        ("incomplete option chain", "invalid_chain_data"),
        ("underlying freshness", "missing_underlying_freshness"),
        ("no executable target contract", "no_executable_contract"),
        ("selected target contract", "invalid_target_contract"),
        ("weak premium transmission", "weak_premium_transmission"),
        ("directional confirmations", "insufficient_directional_confirmations"),
        ("regime", "regime_mismatch"),
        ("outside entry session", "outside_session"),
        ("daily-loss", "daily_loss_limit"),
        ("maximum concurrent positions", "position_limit"),
        ("maximum gross exposure", "exposure_limit"),
        ("no fresh", "no_fresh_microstructure"),
        ("stale", "stale_microstructure"),
        ("conflict", "microstructure_conflict"),
        ("fresh microstructure confirmations", "insufficient_confirmations"),
        ("microstructure confidence", "low_microstructure_confidence"),
        ("breakout setup", "invalid_breakout_location"),
        ("level-reversal setup", "invalid_reversal_location"),
        ("too close", "too_close_to_boundary"),
        ("insufficient room", "insufficient_range_room"),
        ("has no support", "missing_support"),
        ("has no resistance", "missing_resistance"),
        ("cooldown", "cooldown"),
        ("signal-quality score", "low_score"),
        ("no recognized structured setup", "unrecognized_setup"),
        ("chain contamination", "chain_contamination"),
    )
    for needle, bucket in mappings:
        if needle in text:
            return bucket
    return "other"


def _record_paper_exits(
    fills: tuple[PaperFill, ...],
    *,
    entry_prices: dict[str, Decimal],
    entry_strategies: dict[str, str],
    strategy_outcomes: dict[str, Counter[str]],
    completed_trade_returns: list[Decimal],
    net_completed_trade_returns: list[Decimal],
    completed_trade_mfe_percent: list[Decimal] | None = None,
    completed_trade_mae_percent: list[Decimal] | None = None,
    counters: Counter[str],
    round_trip_cost_percent: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    gross_return_sum = Decimal("0")
    net_return_sum = Decimal("0")
    estimated_cost = Decimal("0")
    for fill in fills:
        if fill.reason == "target":
            outcome = "target"
            counters["target_exits"] += 1
        elif fill.reason == "stop":
            outcome = "stop"
            counters["stop_exits"] += 1
        elif fill.reason == "time_exit":
            outcome = "time_exit"
            counters["time_exits"] += 1
        else:
            outcome = "management_exit"
            counters["management_exits"] += 1

        strategy = entry_strategies.pop(fill.token, "UNATTRIBUTED")
        strategy_outcomes.setdefault(strategy, Counter())[outcome] += 1
        entry_price = entry_prices.pop(fill.token, None)
        if entry_price is None or entry_price <= 0:
            continue
        trade_return = (
            (fill.price - entry_price)
            / entry_price
            * Decimal("100")
        )
        net_trade_return = trade_return - round_trip_cost_percent
        completed_trade_returns.append(trade_return)
        net_completed_trade_returns.append(net_trade_return)
        if completed_trade_mfe_percent is not None:
            completed_trade_mfe_percent.append(
                fill.maximum_favorable_excursion_percent
            )
        if completed_trade_mae_percent is not None:
            completed_trade_mae_percent.append(
                fill.maximum_adverse_excursion_percent
            )
        gross_return_sum += trade_return
        net_return_sum += net_trade_return
        estimated_cost += (
            entry_price
            * Decimal(fill.quantity)
            * round_trip_cost_percent
            / Decimal("100")
        )
    return gross_return_sum, net_return_sum, estimated_cost


def _update_strong_signal_exits(
    fills: tuple[PaperFill, ...],
    *,
    details: list[dict[str, object]],
    active_by_token: dict[str, int],
) -> None:
    for fill in fills:
        detail_index = active_by_token.pop(fill.token, None)
        if detail_index is None:
            continue
        detail = details[detail_index]
        entry_price = detail.get("entry_price")
        gain_percent = None
        if isinstance(entry_price, Decimal) and entry_price > 0:
            gain_percent = (
                (fill.price - entry_price)
                / entry_price
                * Decimal("100")
            ).quantize(Decimal("0.0001"))
        detail.update(
            {
                "outcome": (
                    "TARGET"
                    if fill.reason == "target"
                    else "STOP"
                    if fill.reason == "stop"
                    else "TIME_EXIT"
                    if fill.reason == "time_exit"
                    else "MANAGEMENT_EXIT"
                ),
                "exit_time": fill.captured_at,
                "exit_price": fill.price,
                "gain_percent": gain_percent,
            }
        )


def _average_decimal(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(
        Decimal("0.0001")
    )


def _feature_availability(
    snapshot: OptionChainSnapshot,
    analytics: AnalyticsSnapshot,
    *,
    option_book_available: bool,
    futures_book_available: bool,
) -> dict[str, bool]:
    market = snapshot.market
    quotes = snapshot.quotes
    option_types_with_iv = {
        quote.contract.option_type.value
        for quote in quotes
        if (
            quote.greeks is not None
            and quote.greeks.implied_volatility is not None
        )
    }
    option_types_with_oi = {
        quote.contract.option_type.value
        for quote in quotes
        if quote.oi is not None
    }
    atm_types_with_price = {
        quote.contract.option_type.value
        for quote in quotes
        if quote.contract.strike == snapshot.atm_strike and quote.ltp is not None
    }
    has_volume_oi = any(
        quote.volume is not None and quote.oi is not None
        for quote in quotes
    )
    has_gamma_inputs = any(
        quote.oi is not None
        and quote.greeks is not None
        and quote.greeks.gamma is not None
        for quote in quotes
    )
    has_future_price = bool(
        market is not None and market.future_price is not None
    )
    return {
        "premium_response": bool(analytics.premium_responses),
        "futures_flow": bool(
            has_future_price
            and market is not None
            and market.future_oi is not None
        ),
        "consolidated_pcr": {"CE", "PE"}.issubset(
            option_types_with_oi
        ),
        "strike_pcr": bool(analytics.strike_level_ratios),
        "volume_oi": has_volume_oi,
        "iv_surface": bool(option_types_with_iv),
        "iv_skew": {"CE", "PE"}.issubset(option_types_with_iv),
        "atr_normalization": bool(
            market is not None and market.previous_20d_atr is not None
        ),
        "india_vix_regime": bool(
            market is not None and market.india_vix is not None
        ),
        "gamma_concentration": has_gamma_inputs,
        "straddle_expansion": {"CE", "PE"}.issubset(
            atm_types_with_price
        ),
        "futures_basis": bool(
            market is not None and market.basis is not None
        ),
        "order_book_imbalance": option_book_available,
        "option_book": option_book_available,
        "futures_book": futures_book_available,
        "expected_move": bool(
            (
                analytics.expected_move_context is not None
                and analytics.expected_move_context.available
            )
            or (
                market is not None
                and market.previous_session_expected_move is not None
            )
        ),
    }


def _coverage_summary(
    counts: Counter[str],
    total_frames: int,
) -> dict[str, dict[str, Decimal | int]]:
    feature_names = (
        "premium_response",
        "futures_flow",
        "consolidated_pcr",
        "strike_pcr",
        "volume_oi",
        "iv_surface",
        "iv_skew",
        "atr_normalization",
        "india_vix_regime",
        "gamma_concentration",
        "straddle_expansion",
        "futures_basis",
        "order_book_imbalance",
        "option_book",
        "futures_book",
        "expected_move",
    )
    return {
        feature: {
            "available_frames": counts[feature],
            "total_frames": total_frames,
            "coverage_percent": (
                (
                    Decimal(counts[feature])
                    * Decimal("100")
                    / Decimal(total_frames)
                ).quantize(Decimal("0.01"))
                if total_frames
                else Decimal("0")
            ),
        }
        for feature in feature_names
    }


def _is_recent(
    observed_at: datetime | None,
    frame_at: datetime,
    maximum_age_seconds: int,
) -> bool:
    if observed_at is None:
        return False
    age_seconds = (frame_at - observed_at).total_seconds()
    return 0 <= age_seconds <= maximum_age_seconds


def _maximum_drawdown(returns: list[Decimal]) -> Decimal:
    cumulative = Decimal("0")
    peak = Decimal("0")
    maximum = Decimal("0")
    for value in returns:
        cumulative += value
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum.quantize(Decimal("0.0001"))


def _audit_payload(audit: SessionAudit) -> dict[str, object]:
    return {
        "source_path": audit.source_path,
        "market_events": audit.market_events,
        "gate_frames": audit.gate_frames,
        "source_qualified": audit.source_qualified,
        "timestamp_regressions": audit.timestamp_regressions,
        "maximum_regression_seconds": audit.maximum_regression_seconds,
        "first_timestamp": audit.first_timestamp,
        "last_timestamp": audit.last_timestamp,
        "unique_contracts": len(audit.unique_contracts),
        "excluded_contaminated_contracts": audit.excluded_contaminated_contracts,
        "quotes": audit.quotes,
        "quotes_with_greeks": audit.quotes_with_greeks,
        "market_spot_events": audit.market_spot_events,
        "underlyings": audit.underlyings,
    }


def _format_summary(result: ReplayResult, audit: SessionAudit) -> str:
    lines = [
        "Dummy Broker Replay Summary",
        f"Source: {result.source_path}",
        f"Mode: {result.mode}",
        f"Source timestamp regressions: {audit.timestamp_regressions}",
        (
            "Contaminated contracts excluded by current symbol-boundary rule: "
            f"{audit.excluded_contaminated_contracts}"
        ),
        f"Source recorded qualified: {result.source_qualified}",
        f"Market events seen: {result.market_events_seen}",
        f"Market events decoded: {result.market_events_decoded}",
        f"Microstructure candidates regenerated: {result.microstructure_candidates}",
        f"Frames processed: {result.frames_processed}",
        f"Replay qualified: {result.replay_qualified}",
        f"Qualified by side: {result.qualified_by_side or 'none'}",
        f"Raw signals: {result.raw_by_side or 'none'}",
        f"Setup counts: {result.setup_counts or 'none'}",
        (
            "Strategy candidates by family: "
            f"{result.strategy_candidate_counts or 'none'}"
        ),
        (
            "Selected strategies: "
            f"{result.selected_strategy_counts or 'none'}"
        ),
        (
            "Qualified signals by strategy: "
            f"{result.qualified_strategy_counts or 'none'}"
        ),
        (
            "Paper outcomes by strategy: "
            f"{result.paper_outcomes_by_strategy or 'none'}"
        ),
        f"Enabled strategies: {result.enabled_strategies}",
        f"Strategy priority: {result.strategy_priority}",
        f"Resolver policy: {result.resolver_policy}",
        f"Gamma candidates: {result.gamma_candidates}",
        f"Gamma qualified: {result.gamma_qualified}",
        f"Paper entries: {result.paper_entries}",
        f"Paper exits: {result.paper_exits}",
        f"Target exits: {result.target_exits}",
        f"Stop exits: {result.stop_exits}",
        f"Time exits: {result.time_exits}",
        f"Management exits: {result.management_exits}",
        f"Open at tape end: {result.unresolved_positions}",
        (
            "Completed-trade return sum: "
            f"{result.completed_trade_return_percent}%"
        ),
        (
            "Average completed-trade return: "
            f"{result.average_trade_return_percent}%"
        ),
        (
            "Maximum sequential-trade drawdown: "
            f"{result.maximum_trade_drawdown_percent}%"
        ),
        f"Paper realized P&L: {result.paper_realized_pnl}",
        (
            "Research round-trip cost: "
            f"{result.round_trip_cost_percent}%"
        ),
        (
            "Estimated transaction cost: "
            f"{result.estimated_transaction_cost}"
        ),
        (
            "Net average completed-trade return: "
            f"{result.net_average_trade_return_percent}%"
        ),
        f"Net paper realized P&L: {result.net_paper_realized_pnl}",
        (
            "Average maximum favorable excursion: "
            f"{result.average_maximum_favorable_excursion_percent}%"
        ),
        (
            "Average maximum adverse excursion: "
            f"{result.average_maximum_adverse_excursion_percent}%"
        ),
        f"Feature coverage: {result.feature_coverage}",
        f"Unique session days: {result.unique_session_days}",
        (
            "Evidence threshold (>= 8 days and >= 30 trades): "
            + ("MET" if result.sufficient_evidence else "NOT MET")
        ),
        f"Rejection counts: {result.rejection_counts or 'none'}",
    ]
    lines.extend(_format_strong_signal_summary(result))
    lines.extend(
        [
            "",
            "Interpretation:",
            (
                "At least one current-code strong signal qualified in shadow mode."
                if result.replay_qualified
                else "No current-code strong signal qualified in this replay mode."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def _write_regression_report(
    run_directory: Path,
    result: ReplayResult,
    audit: SessionAudit,
) -> None:
    source_date = result.source_path.stem.split("_")[-1]
    report_path = run_directory / f"regression_{source_date}.txt"
    lines = [
        "Regression Replay Report",
        f"Source: {result.source_path}",
        f"Run directory: {run_directory}",
        f"Mode: {result.mode}",
        f"Frames processed: {result.frames_processed}",
        f"Market events seen: {result.market_events_seen}",
        f"Market events decoded: {result.market_events_decoded}",
        f"Microstructure candidates regenerated: {result.microstructure_candidates}",
        f"Replay qualified: {result.replay_qualified}",
        (
            "Strategy candidates by family: "
            f"{result.strategy_candidate_counts or 'none'}"
        ),
        (
            "Selected strategies: "
            f"{result.selected_strategy_counts or 'none'}"
        ),
        f"Enabled strategies: {result.enabled_strategies}",
        f"Strategy priority: {result.strategy_priority}",
        f"Resolver policy: {result.resolver_policy}",
        f"Gamma candidates: {result.gamma_candidates}",
        f"Gamma qualified: {result.gamma_qualified}",
        f"Paper entries: {result.paper_entries}",
        f"Paper exits: {result.paper_exits}",
        f"Paper realized P&L: {result.paper_realized_pnl}",
        f"Estimated transaction cost: {result.estimated_transaction_cost}",
        f"Net paper realized P&L: {result.net_paper_realized_pnl}",
        (
            "Average maximum favorable excursion: "
            f"{result.average_maximum_favorable_excursion_percent}%"
        ),
        (
            "Average maximum adverse excursion: "
            f"{result.average_maximum_adverse_excursion_percent}%"
        ),
        f"Unique session days: {result.unique_session_days}",
        (
            "Evidence threshold (>= 8 days and >= 30 trades): "
            + ("MET" if result.sufficient_evidence else "NOT MET")
        ),
        f"Source timestamp regressions: {audit.timestamp_regressions}",
        (
            "Contaminated contracts excluded by current symbol-boundary rule: "
            f"{audit.excluded_contaminated_contracts}"
        ),
    ]
    lines.extend(_format_strong_signal_summary(result))
    lines.extend(
        [
            "",
            "Frame Interpretation:",
            (
                "This report reflects stored-frame replay, not the planned "
                "broker-sim fixed-interval simulation."
            ),
            (
                "Use the structured gate_decisions.jsonl and summary.json as "
                "the canonical outputs."
            ),
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_strong_signal_summary(result: ReplayResult) -> list[str]:
    lines = [
        "",
        "Strong Signal Position Summary",
        f"Strong signals identified: {result.strong_signals_count}",
    ]
    if not result.strong_signal_details:
        lines.append("No strong signals were identified.")
        return lines

    for index, detail in enumerate(result.strong_signal_details, start=1):
        strike = _summary_value(detail.get("strike"))
        option_type = _summary_value(detail.get("option_type"))
        contract = (
            f"{strike} {option_type}"
            if option_type != "-"
            else strike
        )
        lines.append(
            " | ".join(
                (
                    f"{index}. time={_summary_value(detail.get('signal_time'))}",
                    f"strategy={_summary_value(detail.get('strategy'))}",
                    f"side={_summary_value(detail.get('side'))}",
                    f"contract={contract}",
                    f"entry={_summary_value(detail.get('entry_price'))}",
                    f"SL={_summary_percent(detail.get('stop_percent'))}",
                    f"target={_summary_percent(detail.get('target_percent'))}",
                    f"horizon={_summary_value(detail.get('horizon_minutes'))}m",
                    f"outcome={_summary_value(detail.get('outcome'))}",
                    f"exit_time={_summary_value(detail.get('exit_time'))}",
                    f"exit={_summary_value(detail.get('exit_price'))}",
                    f"gain={_summary_percent(detail.get('gain_percent'))}",
                )
            )
        )
    return lines


def _summary_value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _summary_percent(value: object) -> str:
    if value is None:
        return "-"
    return f"{value}%"


def _is_nifty_token(symbol: str, trading_symbol: str) -> bool:
    if symbol.upper() != "NIFTY":
        return False
    text = trading_symbol.upper()
    if not text.startswith("NIFTY"):
        return False
    remainder = text[len("NIFTY"):]
    return not remainder or not remainder[0].isalpha()


def _selected_quote(
    snapshot: OptionChainSnapshot,
    analytics: AnalyticsSnapshot,
) -> OptionQuote | None:
    if analytics.target_strike is None or analytics.target_option_type is None:
        return None
    return next(
        (
            quote
            for quote in snapshot.quotes
            if quote.contract.strike == analytics.target_strike
            and quote.contract.option_type == analytics.target_option_type
        ),
        None,
    )


def _materialize_timely_trigger(
    *,
    trigger: TimelyEntryTrigger,
    tick,
    state: OptionChainState,
    master: InstrumentMaster,
) -> tuple[OptionChainSnapshot, AnalyticsSnapshot, datetime]:
    candidate = trigger.candidate
    underlying = candidate.snapshot.underlying
    spot_token = master.spot_tokens.get(underlying)
    spot_tick = (
        state.latest_tick(spot_token.token)
        if spot_token is not None
        else None
    )
    spot_price = (
        spot_tick.ltp
        if spot_tick is not None and spot_tick.ltp is not None
        else candidate.snapshot.spot_price
    )
    observed_at = (
        spot_tick.received_at
        if spot_tick is not None
        else candidate.underlying_observed_at
    )
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
        spot_price=spot_price,
        quotes=quotes,
        market=market,
    )
    analytics = replace(
        candidate.analytics,
        captured_at=trigger.captured_at,
        target_ltp=trigger.ltp,
    )
    return snapshot, analytics, observed_at


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(to_jsonable(value), indent=2),
        encoding="utf-8",
    )


def _apply_capture_configuration(
    settings: Settings,
    capture: dict[str, object],
) -> Settings:
    if not capture:
        return settings
    each_side = int(
        capture.get(
            "option_window_each_side",
            settings.option_window_each_side,
        )
    )
    expected_quotes = (each_side * 2 + 1) * 2
    return replace(
        settings,
        option_window_each_side=each_side,
        option_greeks_enabled=bool(
            capture.get(
                "option_greeks_enabled",
                settings.option_greeks_enabled,
            )
        ),
        replay_require_complete_window=bool(
            capture.get(
                "replay_require_complete_window",
                settings.replay_require_complete_window,
            )
        ),
        signal_gate_min_chain_quotes=min(
            settings.signal_gate_min_chain_quotes,
            expected_quotes,
        ),
    )


def _profile_strategy_names(
    profile: StrategyProfile,
) -> tuple[str, ...]:
    return tuple(
        name
        for name, item in sorted(
            profile.strategies.items(),
            key=lambda pair: (pair[1].priority, pair[0]),
        )
        if item.enabled
    )


def _run_manifest(
    *,
    source_path: Path,
    replay_mode: ReplayMode,
    run_id: str,
    max_frames: int | None,
    settings: Settings,
    strategy_configuration: dict[str, object],
    source_sha256: str | None,
    capture_configuration: dict[str, object],
    write_all_decisions: bool,
    round_trip_cost_percent: Decimal,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_type": "strategy_replay_manifest",
        "created_at": datetime.now(UTC),
        "run_id": run_id,
        "source_path": source_path,
        "source_size": source_path.stat().st_size,
        "source_sha256": source_sha256 or _sha256_file(source_path),
        "replay_mode": replay_mode.value,
        "max_frames": max_frames,
        "decision_output_policy": (
            "all_frames"
            if write_all_decisions
            else "qualified_signals_and_trade_events"
        ),
        "safety_pipeline_reorderable": False,
        "capture_configuration_applied": capture_configuration,
        "strategy_configuration": strategy_configuration,
        "strategy_experiment": {
            "strategy_toggles": (
                strategy_configuration["profile"]["strategies"]
            ),
            "profile": strategy_configuration["profile"]["name"],
            "resolver_policy": settings.strategy_resolver_policy,
        },
        "execution": {
            "mode": "paper_shadow",
            "max_positions": settings.risk_max_concurrent_positions,
            "option_stop_percent": (
                strategy_configuration["profile"]["execution"][
                    "stop_percent"
                ]
            ),
            "option_target_percent": (
                strategy_configuration["profile"]["execution"][
                    "target_percent"
                ]
            ),
            "maximum_holding_minutes": (
                strategy_configuration["profile"]["execution"][
                    "maximum_hold_minutes"
                ]
            ),
            "account_capital": settings.execution_account_capital,
            "risk_per_trade_percent": (
                settings.execution_risk_per_trade_percent
            ),
            "research_round_trip_cost_percent": round_trip_cost_percent,
        },
        "signal_controls": {
            "debounce_frame_seconds": (
                settings.signal_debounce_frame_seconds
            ),
            "debounce_window_frames": (
                settings.signal_debounce_window_frames
            ),
            "debounce_min_confirmed_frames": (
                settings.signal_debounce_min_confirmed_frames
            ),
            "structural_level_frame_seconds": (
                settings.structural_level_frame_seconds
            ),
            "gamma_window_seconds": settings.gamma_window_seconds,
            "regime_window_seconds": settings.regime_window_seconds,
            "minimum_independent_confirmation_families": (
                settings.signal_gate_min_independent_confirmation_families
            ),
            "local_reversal_cooldown_seconds": (
                settings.local_reversal_cooldown_seconds
            ),
        },
        "feature_pipeline": {
            "opening_context": {
                "enabled": settings.feature_opening_context_enabled,
                "sequence": settings.feature_opening_context_sequence,
                "observation_minutes": settings.opening_observation_minutes,
            },
            "expected_move": {
                "enabled": settings.feature_expected_move_enabled,
                "sequence": settings.feature_expected_move_sequence,
                "capture_time": settings.expected_move_capture_time,
                "bands": (
                    settings.expected_move_first_band_ratio,
                    settings.expected_move_extended_band_ratio,
                    settings.expected_move_exhaustion_band_ratio,
                ),
            },
            "premium_response": {
                "enabled": settings.feature_premium_response_enabled,
                "sequence": settings.feature_premium_response_sequence,
            },
            "futures_flow": {
                "enabled": settings.feature_futures_flow_enabled,
                "sequence": settings.feature_futures_flow_sequence,
                "window_seconds": settings.futures_flow_window_seconds,
            },
            "candle_patterns": {
                "enabled": settings.feature_candle_patterns_enabled,
                "sequence": settings.feature_candle_patterns_sequence,
                "frame_seconds": settings.structural_level_frame_seconds,
                "required_for_level_reversal": (
                    settings.reversal_candle_confirmation_required
                ),
            },
            "premium_transmission_gate": {
                "enabled": settings.premium_transmission_enabled,
                "minimum_expected_return_percent": (
                    settings.premium_transmission_min_expected_return_percent
                ),
                "minimum_ratio": settings.premium_transmission_min_ratio,
            },
            "momentum_exhaustion": {
                "enabled": settings.feature_momentum_exhaustion_enabled,
                "sequence": settings.feature_momentum_exhaustion_sequence,
                "earliest_time": settings.exhaustion_earliest_time,
                "minimum_premium_return_percent": (
                    settings.exhaustion_minimum_premium_return_percent
                ),
                "minimum_move_utilization": (
                    settings.exhaustion_minimum_move_utilization
                ),
            },
        },
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
