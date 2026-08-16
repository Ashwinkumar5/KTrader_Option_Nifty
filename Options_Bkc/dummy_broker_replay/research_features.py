from __future__ import annotations


DIRECTIONAL_FEATURES = (
    "premium_response",
    "futures_flow",
    "consolidated_pcr",
    "strike_pcr",
    "volume_oi",
    "iv_skew",
    "futures_basis",
)

CONTEXT_FEATURES = (
    "expected_move",
    "iv_surface",
    "india_vix_regime",
)

CONFIRMATION_FEATURES = (
    "gamma_concentration",
    "straddle_expansion",
    "order_book_imbalance",
)

NORMALIZATION_FEATURES = ("atr_normalization",)

RESEARCH_FEATURES = (
    DIRECTIONAL_FEATURES
    + CONTEXT_FEATURES
    + CONFIRMATION_FEATURES
    + NORMALIZATION_FEATURES
)

DISABLED_PRICE_ACTION_FEATURES = (
    "opening_context",
    "candle_patterns",
    "momentum_exhaustion",
)

PAIRED_BASELINE_FEATURES = (
    "premium_response",
    "futures_flow",
)

FEATURE_ROLES = {
    **{name: "DIRECTIONAL" for name in DIRECTIONAL_FEATURES},
    **{name: "CONTEXT" for name in CONTEXT_FEATURES},
    **{name: "CONFIRMATION" for name in CONFIRMATION_FEATURES},
    **{name: "NORMALIZATION" for name in NORMALIZATION_FEATURES},
}


def feature_role(name: str) -> str:
    try:
        return FEATURE_ROLES[name]
    except KeyError as exc:
        raise ValueError(f"unknown research feature: {name}") from exc


def experiment_mode(name: str) -> str:
    return (
        "STANDALONE"
        if feature_role(name) == "DIRECTIONAL"
        else "PAIRED_ABLATION"
    )
