"""Retrospective R diagnostics cannot become realized cash or promotion evidence."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("backtest_order", Path(__file__).resolve().parents[1]/"backtest_order.py")
bo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bo)


def record(**changes):
    base = dict(source="REJECTED_SHADOW", timestamp=1767225600000, symbol="BTCUSDT",
        side="SHORT", setup_type="BREAKDOWN_DOWN", regime="BREAKOUT", reject_reason="LOW_SCORE",
        effective_rr=2., score=6., spread_pct=.001, expected_slippage_pct=.001,
        stop_distance_pct_bucket="NORMAL", liquidity_ok=True, volatility_ok=True,
        outcome="WOULD_TP")
    return {**base, **changes}


def metrics(rows=None, baseline=100., scale=1.):
    return bo._quality_gate_metrics([record()] if rows is None else rows,
        bo.QualityGateConfig(enabled=True, size_multiplier=scale, max_trades_per_day=20), baseline)


def test_two_r_cannot_be_added_to_one_hundred_usdt():
    result = metrics()
    assert result["baseline_plus_quality_gate_net_pnl"] is None


@pytest.mark.parametrize("risk", [None, 10., 25., 0., float("nan"), float("inf")])
@pytest.mark.parametrize("scale", [0., .5, 1.])
def test_reported_risk_does_not_manufacture_counterfactual_cash(risk, scale):
    rows = [record(initial_risk_usd=risk, risk_scale=scale, initial_risk_source="TEST"),
            record(symbol="ETHUSDT", initial_risk_usd=30., risk_scale=scale,
                   realized_costs_usd={"fee":.1}, initial_risk_source="TEST")]
    result = metrics(rows, scale=scale)
    assert result["quality_gate_baseline_realized_net_pnl_usdt"] == 100
    assert result["quality_gate_counterfactual_scaled_total_r"] == pytest.approx(4*scale)
    assert result["quality_gate_counterfactual_cash_usdt"] is None
    assert result["baseline_plus_quality_gate_net_pnl"] is None
    assert result["quality_gate_cash_evidence_status"] == "UNAVAILABLE_NO_CAUSAL_CLOSURE_COST_MODEL"


@pytest.mark.parametrize("baseline", [None, "UNKNOWN", float("nan"), float("inf"), -float("inf"), True])
def test_missing_baseline_never_becomes_zero(baseline):
    result = metrics(baseline=baseline)
    assert result["quality_gate_baseline_realized_net_pnl_usdt"] is None
    assert result["quality_gate_baseline_evidence_status"] == "UNAVAILABLE"
    assert result["baseline_plus_quality_gate_net_pnl"] is None


@pytest.mark.parametrize("baseline", [-100., 0., 100.])
def test_signed_realized_cash_is_invariant_and_diagnostic_only(baseline):
    result = metrics(baseline=baseline)
    assert result["quality_gate_baseline_realized_net_pnl_usdt"] == baseline
    assert result["quality_gate_schema_version"] == "QUALITY_GATE_DIAGNOSTIC_V2"
    assert result["quality_gate_authority"] == "DIAGNOSTIC_ONLY"
    assert result["quality_gate_uses_future_outcomes"] is True
    assert result["quality_gate_selection_basis"] == "RETROSPECTIVE_WOULD_TP_CONDITIONED"
    assert result["quality_gate_promotion_eligible"] is False
    assert result["quality_gate_decision_time_eligible"] is False
    assert result["quality_gate_r_basis"] == "DECISION_EFFECTIVE_RR_NOT_REALIZED_NET_R"


@pytest.mark.parametrize("outcome,rr", [("WOULD_SL",-1.),("WOULD_SL",2.),("UNKNOWN",2.),("WOULD_TIMEOUT",2.),("WOULD_TP",-2.)])
def test_losses_are_not_hidden_in_deployable_positive_expectancy(outcome, rr):
    result = metrics([record(outcome=outcome, effective_rr=rr)], baseline=-100)
    assert result["quality_gate_candidate_count"] == 0
    assert result["quality_gate_counterfactual_expectancy_r"] is None
    assert result["quality_gate_counterfactual_scaled_total_r"] == 0
    assert result["quality_gate_counterfactual_cash_usdt"] is None
    assert result["quality_gate_baseline_realized_net_pnl_usdt"] == -100
    assert result["quality_gate_promotion_eligible"] is False


def test_empty_sample_is_distinct_from_missing_economics():
    result = metrics([])
    assert result["quality_gate_r_evidence_status"] == "NO_SAMPLE"
    assert result["quality_gate_mean_effective_rr"] is None
    assert result["quality_gate_counterfactual_expectancy_r"] is None
    assert result["quality_gate_counterfactual_scaled_total_r"] == 0
    assert result["baseline_plus_quality_gate_net_pnl"] is None
    assert result["quality_gate_counterfactual_cash_usdt"] is None


@pytest.mark.parametrize("field", ["effective_rr", "score", "spread_pct", "expected_slippage_pct"])
@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -float("inf"), True])
def test_invalid_diagnostic_inputs_are_not_favorable(field, bad):
    result = metrics([record(**{field:bad})])
    # Score is optional under the existing min_score=None policy; it never
    # authorizes economic evidence. Mandatory cost/R fields reject the sample.
    if field != "score":
        assert result["quality_gate_candidate_count"] == 0
    assert result["baseline_plus_quality_gate_net_pnl"] is None
    assert result["quality_gate_promotion_eligible"] is False


@pytest.mark.parametrize("scale", [None, float("nan"), float("inf"), -1., 2., True])
def test_invalid_scale_does_not_create_zero_or_favorable_r(scale):
    result = metrics(scale=scale)
    assert result["quality_gate_counterfactual_scaled_total_r"] is None
    assert result["quality_gate_r_evidence_status"] == "UNAVAILABLE_INVALID_SCALE"
    assert result["quality_gate_counterfactual_cash_usdt"] is None


def test_determinism_no_input_mutation_or_double_cost_debit():
    from copy import deepcopy
    rows = [record(realized_costs_usd={"entry_fee":10,"exit_fee":10})]
    original = deepcopy(rows)
    first = metrics(rows)
    assert metrics(rows) == first and rows == original
    assert first["quality_gate_counterfactual_expectancy_r"] == 2
    assert first["quality_gate_counterfactual_scaled_total_r"] == 2
    assert first["quality_gate_counterfactual_cash_usdt"] is None


@pytest.mark.parametrize("mode", ["PAPER", "LIVE", "LIVE_PRECHECK"])
def test_non_backtest_modes_cannot_promote_retrospective_selection(mode):
    result = bo._quality_gate_metrics([record()], bo.QualityGateConfig(enabled=True,modes=(mode,)),100)
    assert result["quality_gate_candidate_count"] == 0
    assert result["quality_gate_promotion_eligible"] is False
    assert result["baseline_plus_quality_gate_net_pnl"] is None


def test_signal_quality_and_offline_csv_share_explicit_contract(tmp_path):
    import csv
    import json
    import os
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root/"src"),
        ALPHAFORGE_DATABASE_URL="sqlite+pysqlite:///"+str(tmp_path/"isolated.db"))
    env.pop("ALPHAFORGE_BACKTEST_DATABASE_URL", None)
    result = subprocess.run([sys.executable,str(root/"backtest_order.py"),"--ci","--offline",
        "--output-dir",str(tmp_path)],cwd=root,env=env,capture_output=True,text=True,timeout=45)
    assert result.returncode == 0, result.stderr
    with (tmp_path/"order_backtest_summary.csv").open() as handle:
        summary = next(csv.DictReader(handle))
    signal = json.loads((tmp_path/"signal_quality_summary.json").read_text())
    assert summary["quality_gate_schema_version"] == signal["quality_gate_schema_version"] == "QUALITY_GATE_DIAGNOSTIC_V2"
    assert summary["quality_gate_authority"] == signal["quality_gate_authority"] == "DIAGNOSTIC_ONLY"
    assert summary["baseline_plus_quality_gate_net_pnl"] == ""
    assert signal["baseline_plus_quality_gate_net_pnl"] is None
    assert summary["quality_gate_counterfactual_cash_usdt"] == ""
    assert signal["quality_gate_counterfactual_cash_usdt"] is None
    assert float(summary["total_net_pnl_usdt"]) == float(summary["quality_gate_baseline_realized_net_pnl_usdt"])


def test_omitted_baseline_is_unavailable_not_realized_zero():
    result = bo._quality_gate_metrics([record()], bo.QualityGateConfig(enabled=True))
    assert result["quality_gate_baseline_realized_net_pnl_usdt"] is None
    assert result["quality_gate_baseline_evidence_status"] == "UNAVAILABLE"
