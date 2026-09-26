from __future__ import annotations

import pytest

from alphaforge.signal_geometry import (
    build_structural_geometry_with_diagnostics,
    derive_setup_structure_with_diagnostics,
)
from alphaforge.order import OrderCandidate, evaluate_trade_quality


def _candles() -> list[dict[str, float]]:
    return [
        {"high": 101.0 + index, "low": 99.0 + index, "close": 100.0 + index}
        for index in range(12)
    ]


@pytest.mark.parametrize(
    ("side", "entry", "expected_stop", "expected_target"),
    [("LONG", 110.0, 99.0, 112.0), ("SHORT", 101.0, 112.0, 99.0)],
)
def test_setup_structure_produces_independent_long_and_short_geometry(
    side: str, entry: float, expected_stop: float, expected_target: float,
) -> None:
    structure, reason = derive_setup_structure_with_diagnostics(_candles(), side=side)
    candidate, candidate_reason = build_structural_geometry_with_diagnostics(
        entry=entry, side=side, setup_type=f"{side}_PULLBACK", setup_phase="PULLBACK",
        structure=structure, setup_timeframe="15m", execution_timeframe="1m",
        entry_source="execution_close_within_setup_entry_zone",
    )

    assert reason is None and candidate_reason is None
    assert candidate["sl"] == expected_stop
    assert candidate["tp"] == expected_target
    assert candidate["rr"] == pytest.approx(
        abs(candidate["tp"] - candidate["entry"])
        / abs(candidate["entry"] - candidate["sl"])
    )
    assert candidate["geometry_source"] == "MTF_SETUP_STRUCTURE"
    assert candidate["setup_timeframe"] == "15m"
    assert candidate["execution_timeframe"] == "1m"


def test_min_rr_cannot_move_structural_target_or_force_low_rr_geometry() -> None:
    structure = {
        "structural_stop": 90.0,
        "structural_target": 110.5,
        "stop_source": "setup_window_support",
        "target_source": "setup_window_resistance",
    }
    candidate, reason = build_structural_geometry_with_diagnostics(
        entry=100.0, side="LONG", setup_type="LONG_PULLBACK", setup_phase="PULLBACK",
        structure=structure, setup_timeframe="15m", execution_timeframe="1m",
        entry_source="execution_close_within_setup_entry_zone",
    )

    assert reason is None
    assert candidate["tp"] == pytest.approx(110.5)
    assert candidate["rr"] == pytest.approx(1.05)
    # The candidate builder takes no threshold: MIN_RR is a downstream filter,
    # so 1.2 and 2.0 can change acceptance but cannot manufacture a 1.2R TP.
    assert candidate["rr"] < 1.2 < 2.0
    quality_candidate = OrderCandidate(
        symbol="BTCUSDT", side="LONG", setup_type="GENERIC", setup_reason="STRUCTURE",
        regime="TREND", score=10.0, rr=candidate["rr"], expectancy=0.1,
        entry=candidate["entry"], sl=candidate["sl"], tp=candidate["tp"],
    )
    decision = evaluate_trade_quality(quality_candidate, {
        "effective_rr": candidate["rr"], "spread_pct": 0.0,
        "expected_slippage_pct": 0.0, "orderbook_status": "MEASURED",
    }, {}, {
        "MODE": "PAPER", "MIN_RR": 1.2, "MIN_TRADE_SCORE": 1.0,
        "MIN_EFFECTIVE_RR": 0.1, "MIN_EXPECTANCY": -1.0,
        "BLOCK_UNKNOWN_EXPECTANCY": False, "MIN_SL_PCT": 0.01,
        "MAX_SL_PCT": 10.0, "MAX_SPREAD_PCT": 1.0,
        "MAX_EXPECTED_SLIPPAGE_PCT": 1.0, "REQUIRE_REGIME_ALIGNMENT": False,
        "ENABLE_ORDERBOOK_FILTER": False, "RUNTIME_LIMITS_ACTIVE": False,
    })
    assert decision.reject_reason == "RR_TOO_LOW"


def test_min_rr_changes_only_filter_result_not_structural_tp() -> None:
    candidate, reason = build_structural_geometry_with_diagnostics(
        entry=100.0, side="LONG", setup_type="LONG_PULLBACK", setup_phase="PULLBACK",
        structure={"structural_stop": 90.0, "structural_target": 115.0},
        setup_timeframe="15m", execution_timeframe="1m",
        entry_source="execution_close_within_setup_entry_zone",
    )
    assert reason is None and candidate["tp"] == pytest.approx(115.0)
    quality_candidate = OrderCandidate(
        symbol="BTCUSDT", side="LONG", setup_type="GENERIC", setup_reason="STRUCTURE",
        regime="TREND", score=10.0, rr=candidate["rr"], expectancy=0.1,
        entry=candidate["entry"], sl=candidate["sl"], tp=candidate["tp"],
    )
    controls = {
        "MODE": "PAPER", "MIN_TRADE_SCORE": 1.0, "MIN_EFFECTIVE_RR": 0.1,
        "MIN_EXPECTANCY": -1.0, "BLOCK_UNKNOWN_EXPECTANCY": False,
        "MIN_SL_PCT": 0.01, "MAX_SL_PCT": 10.0, "MAX_SPREAD_PCT": 1.0,
        "MAX_EXPECTED_SLIPPAGE_PCT": 1.0, "REQUIRE_REGIME_ALIGNMENT": False,
        "ENABLE_ORDERBOOK_FILTER": False, "RUNTIME_LIMITS_ACTIVE": False,
    }
    market = {"effective_rr": candidate["rr"], "spread_pct": 0.0,
              "expected_slippage_pct": 0.0, "orderbook_status": "MEASURED"}
    low_threshold = evaluate_trade_quality(quality_candidate, market, {}, {**controls, "MIN_RR": 1.2})
    high_threshold = evaluate_trade_quality(quality_candidate, market, {}, {**controls, "MIN_RR": 2.0})

    assert candidate["tp"] == pytest.approx(115.0)
    assert low_threshold.accepted is True
    assert high_threshold.reject_reason == "RR_TOO_LOW"


def test_high_structural_reward_is_preserved_not_anchored_to_min_rr() -> None:
    candidate, reason = build_structural_geometry_with_diagnostics(
        entry=100.0, side="LONG", setup_type="LONG_PULLBACK", setup_phase="PULLBACK",
        structure={"structural_stop": 90.0, "structural_target": 123.0},
        setup_timeframe="15m", execution_timeframe="1m",
        entry_source="execution_close_within_setup_entry_zone",
    )

    assert reason is None
    assert candidate["rr"] == pytest.approx(2.3)
    assert candidate["tp"] == pytest.approx(123.0)


def test_missing_or_non_favorable_structure_fails_closed() -> None:
    candidate, reason = build_structural_geometry_with_diagnostics(
        entry=100.0, side="LONG", setup_type="LONG_PULLBACK", setup_phase="PULLBACK",
        structure={}, setup_timeframe="15m", execution_timeframe="1m",
        entry_source="execution_close_within_setup_entry_zone",
    )
    assert candidate == {}
    assert reason == "NO_STRUCTURAL_GEOMETRY"

    candidate, reason = build_structural_geometry_with_diagnostics(
        entry=100.0, side="LONG", setup_type="LONG_PULLBACK", setup_phase="PULLBACK",
        structure={"structural_stop": 90.0, "structural_target": 99.0},
        setup_timeframe="15m", execution_timeframe="1m",
        entry_source="execution_close_within_setup_entry_zone",
    )
    assert candidate == {}
    assert reason == "INSUFFICIENT_STRUCTURAL_REWARD"
