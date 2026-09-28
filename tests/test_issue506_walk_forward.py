from __future__ import annotations

from dataclasses import replace

import pytest

from alphaforge.execution import (
    PROVENANCE_ESTIMATED,
    build_execution_cost_breakdown,
    build_execution_cost_semantics,
)
from alphaforge.walk_forward import (
    HistoricalRow,
    Membership,
    Segment,
    SegmentRole,
    UniverseMode,
    UniverseProvenance,
    WalkForwardContract,
    build_validation_report,
)


GIT_SHA = "a" * 40
EXECUTION_CONTEXT = {
    "spread_pct": 0.001,
    "spread_status": "ESTIMATED_BACKTEST",
    "expected_slippage_pct": 0.001,
    "slippage_status": "MODELLED",
    "fee_pct": 0.001,
    "fee_status": "MODELLED",
    "funding_rate_pct": 0.0,
    "funding_status": "ESTIMATED_BACKTEST",
    "latency_ms": 10,
    "latency_status": "MODELLED",
    "liquidity_score": 0.9,
    "liquidity_status": "ESTIMATED_BACKTEST",
    "volatility_penalty_pct": 0.0,
    "volatility_status": "ESTIMATED_BACKTEST",
}


def _segment(role: SegmentRole, start: str, end: str, suffix: str, universe: UniverseProvenance, config=None, window="window-1"):
    return Segment.freeze(
        segment_id=f"segment-{suffix}",
        window_id=window,
        role=role,
        start=start,
        end=end,
        git_sha=GIT_SHA,
        strategy_config={"strategy": "mtf", "lookback": 20},
        config=config or {"minimum_rr": 1.6},
        data_identity={"dataset": "bars-v1", "window": suffix},
        universe=universe,
        frozen_at="2026-01-01T00:00:00Z",
        calibration_segment_ids=() if role == SegmentRole.CALIBRATION else ("segment-is",),
    )


def _contract(config=None):
    universe = UniverseProvenance.fixed(["BTCUSDT"], source_identity="research-universe-v1")
    return WalkForwardContract(
        segments=(
            _segment(SegmentRole.CALIBRATION, "2026-01-02T00:00:00Z", "2026-02-01T00:00:00Z", "is", universe, config),
            _segment(SegmentRole.OOS_VALIDATION, "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z", "oos", universe, config),
        ),
        universe=universe,
    )


def _row(row_id: str, timestamp: str, net_return: float) -> HistoricalRow:
    canonical = build_execution_cost_breakdown(2.0, EXECUTION_CONTEXT, min_effective_rr=1.6)
    cost_semantics = build_execution_cost_semantics(
        entry=100.0,
        expected_fill=100.1,
        actual_fill=None,
        side="LONG",
        expected_fill_provenance=PROVENANCE_ESTIMATED,
    ).decision_time_dict()
    return HistoricalRow(
        row_id=row_id,
        timestamp=timestamp,
        symbol="BTCUSDT",
        regime="TRENDING",
        net_return=net_return,
        candidate_rr=2.1,
        executable_rr=2.0,
        remaining_execution_penalty=canonical.cost_penalty_rr,
        effective_rr=canonical.effective_rr,
        rr_basis="EXPECTED_FILL_RUNTIME_PARITY",
        execution_cost_semantics=cost_semantics,
    )


def test_split_identity_is_reproducible_and_config_is_frozen():
    config = {"minimum_rr": 1.6, "nested": {"enabled": True}}
    first = _contract(config)
    second = _contract({"nested": {"enabled": True}, "minimum_rr": 1.6})
    original_hash = first.segments[1].config_hash
    config["minimum_rr"] = 99
    config["nested"]["enabled"] = False
    assert first.identity == second.identity
    assert first.segments[1].config_hash == original_hash


def test_validation_rows_cannot_enter_calibration_and_future_cannot_leak_backward():
    contract = _contract()
    rows = (
        _row("train", "2026-01-15T00:00:00Z", 1.0),
        _row("oos", "2026-02-15T00:00:00Z", -1.0),
    )
    observed = contract.calibrate(
        rows,
        target_segment_id="segment-oos",
        calibrator=lambda calibration_rows: [row.row_id for row in calibration_rows],
    )
    assert observed == ["train"]
    with pytest.raises(ValueError, match="validation/test data"):
        WalkForwardContract(
            segments=(
                contract.segments[0],
                replace(contract.segments[1], calibration_segment_ids=("segment-oos",)),
            ),
            universe=contract.universe,
        )

    universe = contract.universe
    with pytest.raises(ValueError, match="future rows"):
        WalkForwardContract(
            segments=(
                _segment(SegmentRole.OOS_VALIDATION, "2026-01-02T00:00:00Z", "2026-02-01T00:00:00Z", "oos", universe),
                _segment(SegmentRole.CALIBRATION, "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z", "is", universe),
            ),
            universe=universe,
        )


def test_universe_provenance_is_explicit_and_timestamp_correct_when_claimed():
    fixed = UniverseProvenance.fixed(["BTCUSDT"], source_identity="fixed-2026")
    assert fixed.mode == UniverseMode.FIXED
    assert fixed.survivorship_bias_protected is False

    point_in_time = UniverseProvenance.point_in_time(
        [Membership("BTCUSDT", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")],
        source_identity="exchange-membership-snapshot-v1",
    )
    contract = WalkForwardContract(
        segments=(
            _segment(SegmentRole.CALIBRATION, "2026-01-02T00:00:00Z", "2026-01-15T00:00:00Z", "is", point_in_time),
            _segment(SegmentRole.OOS_VALIDATION, "2026-01-15T00:00:00Z", "2026-02-15T00:00:00Z", "oos", point_in_time),
        ),
        universe=point_in_time,
    )
    with pytest.raises(ValueError, match="outside the declared universe"):
        contract.partition([_row("expired", "2026-02-10T00:00:00Z", 1.0)])


def test_negative_oos_is_not_masked_and_report_separates_evidence_roles():
    report = build_validation_report(
        _contract(),
        [
            _row("train", "2026-01-15T00:00:00Z", 10.0),
            _row("oos", "2026-02-15T00:00:00Z", -1.0),
        ],
        min_effective_rr=1.6,
    )
    assert report["historical_validation"] == "FAIL"
    assert report["segments"][1]["git_sha"] == GIT_SHA
    assert report["segments"][1]["calibration_segment_ids"] == ("segment-is",)
    assert report["negative_oos_segments"][0]["role"] == SegmentRole.OOS_VALIDATION
    assert {item["role"] for item in report["summaries"]["by_role_symbol"]} == {
        SegmentRole.CALIBRATION,
        SegmentRole.OOS_VALIDATION,
    }
    assert report["summaries"]["by_role_regime"]
    assert report["promotion_evidence"] == {
        "historical_calibration": "PRESENT",
        "historical_oos": "FAIL",
        "future_paper": "REQUIRED_NOT_PROVIDED",
        "oos_replaces_fresh_paper": False,
        "live_authorized": False,
    }


def test_report_reuses_canonical_execution_cost_and_rr_semantics():
    row = _row("oos", "2026-02-15T00:00:00Z", 1.0)
    report = build_validation_report(
        _contract(),
        [_row("train", "2026-01-15T00:00:00Z", 1.0), row],
        min_effective_rr=1.6,
    )
    evidence = next(item for item in report["rows"] if item["row_id"] == "oos")
    assert evidence["candidate_rr"] == row.candidate_rr
    assert evidence["executable_rr"] == row.executable_rr
    assert evidence["effective_rr"] == row.effective_rr
    assert evidence["execution_cost_semantics"] == row.execution_cost_semantics

    mismatched = replace(row, effective_rr=row.effective_rr + 0.01)
    with pytest.raises(ValueError, match="canonical decision semantics"):
        build_validation_report(
            _contract(),
            [_row("train", "2026-01-15T00:00:00Z", 1.0), mismatched],
            min_effective_rr=1.6,
        )
