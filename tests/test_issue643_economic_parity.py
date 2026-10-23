"""Frozen economics at equivalent pre-submit stages; no execution side effects."""
from copy import deepcopy
import asyncio

import pytest

from alphaforge.execution import build_decision_rr_metrics, build_execution_context, build_execution_cost_model
from alphaforge.order import evaluate_signal_decision
from alphaforge.execution import build_execution_cost_semantics, PROVENANCE_MODELLED
from alphaforge.portfolio_risk import scale_candidate_exposure
from test_issue490_protected_mode_parity import _runtime
from alphaforge.runtime import ExecutionMode
from alphaforge.decision_invariant import assert_pre_submit_invariant_parity


def frozen_case(side="LONG", **overrides):
    runtime = _runtime()
    market = deepcopy(runtime._qualification_samples[0])
    market.update(score=9.0, side=side, sl=99.0 if side == "LONG" else 101.0,
                  tp=102.0 if side == "LONG" else 98.0,
                  decision_timestamp="2024-05-20T10:13:20Z")
    market.update(overrides)
    market["execution_ctx"] = build_execution_context(market)
    signal = {"symbol": market["symbol"], "side": side, "entry_price": market["entry"],
              "stop_loss": market["sl"], "take_profit": market["tp"],
              "risk_reward": market["rr"], "expectancy": market["expectancy"],
              "setup": market["setup_type"], "regime": market["regime"]}
    return runtime, market, signal


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("threshold", [1.8, 1.9])
def test_actual_shared_boundary_and_modes_gate_on_same_executable_rr(side, threshold):
    runtime, market, signal = frozen_case(side)
    runtime.config.min_effective_rr = threshold
    expected = build_decision_rr_metrics(market["rr"], market, market["execution_ctx"], mode="BACKTEST")
    rows = [runtime._evaluate_pre_submit(signal, {**market, "mode": mode}, {}, {})
            for mode in ("BACKTEST", "PAPER", "LIVE_PRECHECK")]
    for row in rows:
        assert row["effective_rr"] == expected["effective_rr"]
        assert row["executable_raw_rr"] == expected["executable_raw_rr"]
        assert row["remaining_execution_penalty"] == expected["remaining_execution_penalty"]
        assert row["rr_basis"] == "EXPECTED_FILL_RUNTIME_PARITY"
        assert row["decision"] == ("ACCEPT" if threshold == 1.8 else "REJECT")
    assert len({row["primary_reject_reason"] for row in rows}) == 1
    # Exercise the shared production decision entrypoint, not only parity projection.
    decision = evaluate_signal_decision(market, runtime._canonical_filter_config(mode="BACKTEST"),
        {"balance": 1000.0}, market["execution_ctx"], "BACKTEST")
    assert decision.effective_rr == expected["effective_rr"]
    assert decision.decision == rows[0]["decision"]
    breakdown = decision.diagnostics["effective_rr_breakdown"]
    assert breakdown["effective_rr"] == pytest.approx(
        max(breakdown["raw_rr"] - breakdown["cost_penalty_rr"], 0), abs=1e-6)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_fill_geometry_and_cost_credit_have_independent_directional_constraints(side):
    _, market, _ = frozen_case(side)
    adverse = build_decision_rr_metrics(2.0, market, market["execution_ctx"], mode="BACKTEST")
    assert adverse["executable_raw_rr"] < adverse["candidate_rr"]
    model = build_execution_cost_model(market["execution_ctx"], include_missing_penalty=False)
    assert adverse["remaining_execution_penalty"] == pytest.approx(
        model.total_penalty - model.slippage_penalty / 2, abs=1e-6)
    # Affine price transforms must preserve RR and the signed entry cost.
    scaled = {**market, "entry": 1000.0, "sl": market["sl"] * 10, "tp": market["tp"] * 10}
    second = build_decision_rr_metrics(2.0, scaled, market["execution_ctx"], mode="BACKTEST")
    assert second["effective_rr"] == adverse["effective_rr"]
    assert second["expected_execution_cost_price"] == pytest.approx(adverse["expected_execution_cost_price"] * 10)


@pytest.mark.parametrize("missing", ["spread_pct", "expected_slippage_pct", "fee_pct", "funding_rate_pct"])
def test_missing_costs_do_not_authorize_shared_backtest_decision(missing):
    runtime, market, _ = frozen_case()
    market[missing] = None
    market["execution_ctx"] = build_execution_context(market)
    decision = evaluate_signal_decision(market, runtime._canonical_filter_config(mode="BACKTEST"),
        {}, market["execution_ctx"], "BACKTEST")
    assert decision.decision == "REJECT"
    assert build_execution_cost_model(market["execution_ctx"]).completeness != "COMPLETE"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("scale", [0.0, 0.5, 1.0])
def test_wide_stop_final_sizing_and_portfolio_caps_across_actual_modes(side, scale):
    runtime, market, signal = frozen_case(side, sl=98.0 if side == "LONG" else 102.0,
                                         tp=105.0 if side == "LONG" else 95.0, rr=2.5)
    runtime.config.stop_too_wide_max_risk_scale = scale
    runtime.config.max_symbol_notional = 7.0
    for mode in ("BACKTEST", "PAPER", "LIVE_PRECHECK"):
        row = runtime._evaluate_pre_submit(signal, {**market, "mode": mode}, {}, {})
        oracle = scale_candidate_exposure(original_notional=10.0, original_quantity=0.1,
                                           risk_scale=scale, require_scale=True)
        assert row["risk_scale"] == scale
        assert row["effective_notional"] == oracle["effective_notional"] == 10.0 * scale
        assert row["stop_distance_basis"] == "EXPECTED_FILL"
        assert row["decision"] == ("ACCEPT" if scale == 0.5 else "REJECT")
    assert not runtime._active_positions  # projections never submit


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("favorable", [False, True])
@pytest.mark.parametrize("expected_improvement", [False, True])
def test_future_fill_signs_do_not_change_initial_decision_evidence(side, favorable, expected_improvement):
    direction = 1 if side == "LONG" else -1
    expected = 100 + direction * (-0.2 if expected_improvement else 0.2)
    actual = expected + direction * (-0.3 if favorable else 0.3)
    initial = build_execution_cost_semantics(entry=100, expected_fill=expected, actual_fill=None,
        side=side, expected_fill_provenance=PROVENANCE_MODELLED,
        decision_timestamp="2024-05-20T10:13:20Z")
    realized = build_execution_cost_semantics(entry=100, expected_fill=expected, actual_fill=actual,
        side=side, expected_fill_provenance=PROVENANCE_MODELLED,
        decision_timestamp="2024-05-20T10:13:20Z", fill_timestamp="2024-05-20T10:14:20Z")
    assert realized.decision_time_dict() == initial.decision_time_dict()
    assert initial.total_realized_execution_cost_price is None
    assert realized.realized_execution_deviation_price == pytest.approx(-0.3 if favorable else 0.3)
    assert realized.total_realized_execution_cost_price == pytest.approx(
        realized.expected_execution_cost_price + realized.realized_execution_deviation_price)


@pytest.mark.parametrize("missing", ["spread_pct", "expected_slippage_pct", "fee_pct", "funding_rate_pct"])
def test_all_modes_reject_missing_costs_without_false_parity_pass(missing):
    runtime, market, signal = frozen_case()
    market[missing] = None
    market["execution_ctx"] = build_execution_context(market)
    for mode in ("BACKTEST", "PAPER", "LIVE_PRECHECK"):
        row = runtime._evaluate_pre_submit(signal, {**market, "mode": mode}, {}, {})
        assert row["decision"] == "REJECT"
        assert row["execution_evidence_status"] != "COMPLETE"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("scale", [0.0, 0.5, 1.0])
def test_final_paper_position_obeys_scaling_not_just_evidence(side, scale):
    from test_issue489_risk_scale_sizing import _runtime as paper_runtime, _selection, _wide_softenable_market
    runtime = paper_runtime(ExecutionMode.PAPER)
    runtime.config.stop_too_wide_max_risk_scale = scale
    market = _wide_softenable_market()
    market.update(side=side, sl=98.0 if side == "LONG" else 102.0,
                  tp=105.0 if side == "LONG" else 95.0)
    asyncio.run(runtime._process_symbol(_selection(market)))
    if scale == 0:
        assert runtime.metrics.executions == 0
        assert not runtime._active_positions
    else:
        assert runtime.metrics.executions == 1
        assert runtime._active_positions["BTCUSDT"] == pytest.approx(10.0 * scale)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_realized_sql_outcome_uses_executable_risk_and_all_cash_costs(tmp_path, side):
    from test_phase8_position_resolver import setup
    from alphaforge.burnin_resolver import persist_pending_position, resolve_position_closure
    conn, campaign, run = setup(tmp_path)
    fill = 101 if side == "LONG" else 99
    stop = 90 if side == "LONG" else 110
    target = 120 if side == "LONG" else 80
    persist_pending_position(conn, trade_id="frozen", campaign_id=campaign.campaign_id,
        burnin_run_id=run["burnin_run_id"], signal_id="frozen", symbol="BTCUSDT", side=side,
        entry_time="2026-01-01T00:00:00Z", planned_entry=100, simulated_fill=fill,
        stop=stop, target=target, quantity=2, notional=2 * fill,
        entry_spread=.01, entry_slippage=.02, entry_fee=.03, regime="TRENDING",
        source_provenance={"provider": "TEST", "execution_cost_unit": "USD"})
    exit_costs = dict(exit_spread=.04, exit_slippage=.05, exit_fee=.06,
                      funding=.07, latency_impact_penalty=.08, cost_unit="USD")
    result = resolve_position_closure(conn, trade_id="frozen", exit_time="2026-01-01T01:00:00Z",
        exit_price=target, exit_reason="TP_HIT", exit_costs=exit_costs)
    row = dict(conn.execute("SELECT * FROM burnin_trade_outcomes WHERE trade_id='frozen'").fetchone())
    # Cash identities independently protect shared-authority bugs across modes.
    assert result["evidence_complete"] is True
    assert row["gross_pnl"] == pytest.approx(38)
    assert row["total_execution_cost"] == pytest.approx(.36)
    assert row["net_r"] * (abs(fill - stop) * 2) == pytest.approx(row["gross_pnl"] - .36)
    assert row["hold_duration_seconds"] == 3600
    assert resolve_position_closure(conn, trade_id="frozen", exit_time="2026-01-02T00:00:00Z",
        exit_price=1, exit_reason="SL_HIT", exit_costs={})["status"] == "IDEMPOTENT"
    assert dict(conn.execute("SELECT * FROM burnin_trade_outcomes WHERE trade_id='frozen'").fetchone()) == row


def test_multiple_execution_failures_cannot_be_overridden_by_trade_quality():
    runtime, market, signal = frozen_case(spread_pct=.01, expected_slippage_pct=.004, funding_rate_pct=.002)
    runtime.config.max_spread_pct = .0025
    runtime.config.max_expected_slippage_pct = .002
    for mode in ("BACKTEST", "PAPER", "LIVE_PRECHECK"):
        row = runtime._evaluate_pre_submit(signal, {**market, "mode": mode}, {}, {})
        assert row["decision"] == "REJECT"
        assert {"SPREAD_TOO_HIGH", "SLIPPAGE_TOO_HIGH", "FUNDING_TOO_HIGH"} <= set(
            row["execution_safety"]["all_failed_gates"])


@pytest.mark.parametrize("status", ["UNAVAILABLE_BLOCKING", "INVALID_FAKE_ZERO"])
def test_identically_invalid_execution_evidence_cannot_prove_parity(status):
    from test_issue421_decision_invariant import _surface
    row = _surface(execution_evidence_status=status)
    with pytest.raises(ValueError, match="execution_evidence_status"):
        assert_pre_submit_invariant_parity(row, deepcopy(row), deepcopy(row))


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("raw_rr", [1.49, 1.51])
def test_raw_rr_boundary_is_independent_of_effective_threshold(side, raw_rr):
    runtime, market, signal = frozen_case(side, rr=raw_rr,
        tp=100 + raw_rr if side == "LONG" else 100 - raw_rr)
    runtime.config.min_rr = 1.5
    runtime.config.min_effective_rr = 1.1
    decisions = [runtime._evaluate_pre_submit(signal, {**market, "mode": mode}, {}, {})
                 for mode in ("BACKTEST", "PAPER", "LIVE_PRECHECK")]
    assert {row["decision"] for row in decisions} == ({"REJECT"} if raw_rr < 1.5 else {"ACCEPT"})


def test_absent_fee_key_is_missing_not_a_zero_fee_schedule():
    _, market, _ = frozen_case()
    context = dict(market["execution_ctx"])
    del context["fee_pct"]
    assert "fee_pct" in build_execution_cost_model(context).missing_fields
