from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.execution import (
    PROVENANCE_ACTUAL,
    PROVENANCE_MODELLED,
    PROVENANCE_UNAVAILABLE,
    build_execution_cost_model,
    build_execution_cost_semantics,
    build_execution_review_metrics,
    weighted_average_fill_price,
)
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _semantics(*, side: str, expected: float, actual: float | None):
    return build_execution_cost_semantics(
        entry=100.0,
        expected_fill=expected,
        actual_fill=actual,
        side=side,
        expected_fill_provenance=PROVENANCE_MODELLED,
        actual_fill_provenance=(
            PROVENANCE_ACTUAL if actual is not None else PROVENANCE_UNAVAILABLE),
        decision_timestamp="2026-09-22T10:00:00Z",
        fill_timestamp="2026-09-22T10:00:01Z" if actual is not None else None,
    )


def _paper_runtime() -> RuntimeOrchestrator:
    return RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=SimpleNamespace(),
        market_scanner=lambda: None,
        paper_slippage_bps=2.0,
    )


def _execution_ctx() -> dict[str, object]:
    return {
        "spread_pct": 0.001,
        "expected_slippage_pct": 0.0002,
        "latency_ms": 0.0,
        "fee_pct": 0.0004,
        "funding_rate_pct": 0.0,
        "liquidity_score": 1.0,
        "volatility_regime": "normal",
    }


def test_long_and_short_expected_cost_use_positive_is_adverse_sign() -> None:
    long_cost = _semantics(side="LONG", expected=100.2, actual=None)
    short_cost = _semantics(side="SHORT", expected=99.8, actual=None)

    assert long_cost.expected_execution_cost_price == pytest.approx(0.2)
    assert short_cost.expected_execution_cost_price == pytest.approx(0.2)
    assert long_cost.expected_execution_cost_bps == pytest.approx(20.0)
    assert short_cost.expected_execution_cost_bps == pytest.approx(20.0)


def test_long_and_short_realized_deviation_use_positive_is_adverse_sign() -> None:
    long_cost = _semantics(side="LONG", expected=100.2, actual=100.35)
    short_cost = _semantics(side="SHORT", expected=99.8, actual=99.65)

    assert long_cost.realized_execution_deviation_price == pytest.approx(0.15)
    assert short_cost.realized_execution_deviation_price == pytest.approx(0.15)
    assert long_cost.realized_execution_deviation_bps == pytest.approx(15.0)
    assert short_cost.realized_execution_deviation_bps == pytest.approx(15.0)


def test_favorable_short_fill_remains_negative_instead_of_becoming_absolute_cost() -> None:
    costs = _semantics(side="SHORT", expected=99.8, actual=100.1)

    assert costs.realized_execution_deviation_price == pytest.approx(-0.3)
    assert costs.total_realized_execution_cost_price == pytest.approx(-0.1)


@pytest.mark.parametrize(
    ("side", "expected", "actual"),
    [("LONG", 100.2, 100.35), ("SHORT", 99.8, 99.65)],
)
def test_total_cost_decomposes_into_expected_plus_realized(
    side: str, expected: float, actual: float,
) -> None:
    costs = _semantics(side=side, expected=expected, actual=actual)

    assert costs.total_realized_execution_cost_price == pytest.approx(
        costs.expected_execution_cost_price
        + costs.realized_execution_deviation_price
    )
    assert costs.total_realized_execution_cost_pct == pytest.approx(
        costs.expected_execution_cost_pct
        + costs.realized_execution_deviation_pct
    )
    assert costs.total_realized_execution_cost_bps == pytest.approx(
        costs.expected_execution_cost_bps
        + costs.realized_execution_deviation_bps
    )


def test_all_percent_and_bps_metrics_use_strategy_entry_denominator() -> None:
    costs = _semantics(side="LONG", expected=101.0, actual=102.0)

    assert costs.expected_execution_cost_pct == pytest.approx(0.01)
    assert costs.realized_execution_deviation_pct == pytest.approx(0.01)
    assert costs.total_realized_execution_cost_pct == pytest.approx(0.02)
    assert costs.expected_execution_cost_bps == pytest.approx(100.0)
    assert costs.percentage_denominator == "STRATEGY_ENTRY"


def test_missing_actual_fill_is_unavailable_not_numeric_zero() -> None:
    costs = _semantics(side="LONG", expected=100.2, actual=None)

    assert costs.actual_fill is None
    assert costs.realized_execution_deviation_price is None
    assert costs.realized_execution_deviation_pct is None
    assert costs.realized_execution_deviation_bps is None
    assert costs.total_realized_execution_cost_price is None
    assert costs.total_realized_execution_cost_pct is None
    assert costs.total_realized_execution_cost_bps is None
    assert costs.actual_fill_provenance == PROVENANCE_UNAVAILABLE
    assert costs.fill_timestamp is None


def test_paper_simulated_fill_is_modelled_not_exchange_actual() -> None:
    result = _paper_runtime()._simulate_paper_execution(
        "BTCUSDT",
        {"order_type": "MARKET", "decision_time": "2026-09-22T10:00:00Z"},
        {"entry": 100.0, "side": "LONG"},
    )

    assert result["actual_fill"] == result["expected_fill"]
    assert result["fill_provenance"] == PROVENANCE_MODELLED
    assert result["execution_cost_semantics"]["actual_fill_provenance"] == PROVENANCE_MODELLED
    assert result["execution_cost_semantics"]["actual_fill_provenance"] != PROVENANCE_ACTUAL


def test_paper_position_persists_canonical_costs_in_existing_provenance_json() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        campaign = create_campaign(
            conn,
            release_id="issue-369",
            duration_days=1,
            symbols=["BTCUSDT"],
            intervals=["1m"],
        )
        run = start_or_resume_campaign(conn, campaign.campaign_id)

    runtime = _paper_runtime()
    runtime.persistence_engine = engine
    runtime._campaign_id = campaign.campaign_id
    runtime._burnin_run_id = run["burnin_run_id"]
    market = {
        "entry": 100.0,
        "sl": 99.0,
        "tp": 102.0,
        "side": "LONG",
        "rr": 2.0,
        "execution_ctx": _execution_ctx(),
    }
    market.update(runtime._execution_rr_metrics(2.0, market, market["execution_ctx"]))
    decision = {
        "signal_id": "issue-369-signal",
        "decision_time": "2026-09-22T10:00:00Z",
        "order_type": "MARKET",
    }
    result = runtime._simulate_paper_execution("BTCUSDT", decision, market)

    runtime._persist_pending_paper_position(
        "BTCUSDT", "issue-369-trade", decision, market, result)

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT planned_entry, simulated_fill, entry_slippage, entry_fee,
                   source_provenance_json
            FROM burnin_pending_position_outcomes
            WHERE trade_id='issue-369-trade'
        """)).mappings().one()
    provenance = json.loads(row["source_provenance_json"])
    semantics = provenance["execution_cost_semantics"]
    assert row["planned_entry"] == pytest.approx(100.0)
    assert row["simulated_fill"] == pytest.approx(100.02)
    assert row["entry_slippage"] == pytest.approx(0.0)
    assert row["entry_fee"] > 0.0
    assert semantics["expected_execution_cost_bps"] == pytest.approx(2.0)
    assert semantics["realized_execution_deviation_bps"] == pytest.approx(0.0)
    assert semantics["total_realized_execution_cost_bps"] == pytest.approx(2.0)
    assert semantics["actual_fill_provenance"] == PROVENANCE_MODELLED
    assert semantics["fee_treatment"] == "SEPARATE_NOT_INCLUDED"


def test_one_and_multiple_partial_fills_use_one_weighted_average_formula() -> None:
    assert weighted_average_fill_price([{"qty": 2.0, "price": 100.0}]) == pytest.approx(100.0)
    long_weighted = weighted_average_fill_price([
        {"qty": 1.0, "price": 100.0},
        {"qty": 3.0, "price": 101.0},
    ])
    short_weighted = weighted_average_fill_price([
        {"qty": 1.0, "price": 100.0},
        {"qty": 3.0, "price": 99.0},
    ])

    assert long_weighted == pytest.approx(100.75)
    assert short_weighted == pytest.approx(99.25)
    long_costs = build_execution_cost_semantics(
        entry=100.0,
        expected_fill=100.2,
        actual_fill=long_weighted,
        side="LONG",
        actual_fill_provenance=PROVENANCE_ACTUAL,
    )
    short_costs = build_execution_cost_semantics(
        entry=100.0,
        expected_fill=99.8,
        actual_fill=short_weighted,
        side="SHORT",
        actual_fill_provenance=PROVENANCE_ACTUAL,
    )
    assert long_costs.realized_execution_deviation_price == pytest.approx(0.55)
    assert short_costs.realized_execution_deviation_price == pytest.approx(0.55)


def test_empty_or_invalid_partial_fill_evidence_is_not_coerced_to_zero() -> None:
    assert weighted_average_fill_price([]) is None
    with pytest.raises(ValueError):
        weighted_average_fill_price([{"qty": 0.0, "price": 100.0}])


def test_fees_are_explicitly_outside_price_deviation_contract() -> None:
    costs = _semantics(side="LONG", expected=100.2, actual=100.35)

    assert costs.fee_treatment == "SEPARATE_NOT_INCLUDED"
    assert [key for key in costs.as_dict() if key.startswith("fee_")] == ["fee_treatment"]


def test_expected_entry_movement_is_not_deducted_twice_from_effective_rr() -> None:
    runtime = _paper_runtime()
    market = {"entry": 100.0, "sl": 99.0, "tp": 102.0, "side": "LONG"}
    ctx = _execution_ctx()
    metrics = runtime._execution_rr_metrics(2.0, market, ctx)
    model = build_execution_cost_model(ctx)

    assert metrics["expected_fill"] == pytest.approx(100.02)
    assert metrics["remaining_execution_penalty"] == pytest.approx(
        model.total_penalty - model.slippage_penalty / 2.0
    )
    assert metrics["effective_rr"] == pytest.approx(
        metrics["executable_raw_rr"] - metrics["remaining_execution_penalty"],
        abs=1e-6,
    )


def test_decision_time_evidence_is_independent_of_actual_future_fill() -> None:
    better = _semantics(side="LONG", expected=100.2, actual=100.1)
    worse = _semantics(side="LONG", expected=100.2, actual=100.8)

    assert better.decision_time_dict() == worse.decision_time_dict()
    assert "actual_fill" not in better.decision_time_dict()
    assert "realized_execution_deviation_pct" not in better.decision_time_dict()
    assert "total_realized_execution_cost_pct" not in better.decision_time_dict()


def test_legacy_actual_slippage_alias_is_total_cost_not_model_deviation() -> None:
    metrics = build_execution_review_metrics({
        "side": "SHORT",
        "entry_price": 100.0,
        "expected_fill": 99.8,
        "filled_entry_price": 99.7,
        "actual_fill_provenance": PROVENANCE_ACTUAL,
    })

    assert metrics["expected_execution_cost_pct"] == pytest.approx(0.002)
    assert metrics["realized_execution_deviation_pct"] == pytest.approx(0.001)
    assert metrics["total_realized_execution_cost_pct"] == pytest.approx(0.003)
    assert metrics["realized_slippage_pct"] == pytest.approx(0.003)
    assert metrics["actual_slippage_pct_semantics"] == "TOTAL_REALIZED_EXECUTION_COST_PCT"


def test_review_does_not_fabricate_fill_when_actual_fill_is_missing() -> None:
    metrics = build_execution_review_metrics({
        "side": "LONG",
        "entry_price": 100.0,
        "expected_fill": 100.2,
    })

    assert metrics["realized_execution_deviation_pct"] is None
    assert metrics["total_realized_execution_cost_pct"] is None
    assert metrics["realized_slippage_pct"] is None
    assert metrics["fill_quality_score"] is None
