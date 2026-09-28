from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.orm import Session

from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _runtime() -> RuntimeOrchestrator:
    class _Brain:
        def score_signal(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
            return SimpleNamespace(total_score=9.0, components={}, probabilistic={})

        def choose_order_plan(self, signal_payload, market_ctx, score_ctx):
            return SimpleNamespace(
                decision="ACCEPTED",
                reason="",
                confidence=0.9,
                order_type="LIMIT",
                limit_price=signal_payload.get("entry_price"),
                stop_price=None,
            )

        def explain_decision(self, signal_payload, score_ctx, order_plan):
            return "parity-fixture"

    return RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE_PRECHECK),
        ai_brain=_Brain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        paper_slippage_bps=2.0,
    )


def _surface(**overrides):
    base = {
        "decision": "ACCEPT",
        "primary_reject_reason": "",
        "all_failed_gates": [],
        "score": 9.0,
        "candidate_rr": 2.0,
        "executable_raw_rr": 1.9,
        "remaining_execution_penalty": 0.1,
        "effective_rr": 1.8,
        "rr_basis": "EXPECTED_FILL_RUNTIME_PARITY",
        "execution_cost_semantics": {
            "reference_price": "STRATEGY_ENTRY",
            "percentage_denominator": "STRATEGY_ENTRY",
            "sign_convention": "POSITIVE_IS_ADVERSE",
        },
        "threshold_provenance": {
            "MIN_RR": "runtime_filter_config:1.5",
            "MIN_EFFECTIVE_RR": "runtime_filter_config:1.1",
        },
        "execution_evidence_status": "COMPLETE",
        "portfolio_decision": {"accepted": True, "decision": "ACCEPT"},
        "original_notional": 10.0,
        "risk_scale": 0.5,
        "effective_notional": 5.0,
        "stop_distance_basis": "EXPECTED_FILL",
        "geometry_status": "COMPLETE",
        "geometry_source": "MTF_SETUP_STRUCTURE",
        "lifecycle_pre_submit_terminal_state": "ORDER_PLACED",
    }
    base.update(overrides)
    return base


def test_mode_parity_fails_on_capital_sizing_drift(monkeypatch):
    def fake(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        mode = str(market_ctx["mode"])
        if mode == "LIVE_PRECHECK":
            return _surface(risk_scale=1.0, effective_notional=10.0)
        return _surface()

    monkeypatch.setattr(RuntimeOrchestrator, "_evaluate_pre_submit", fake)
    evidence = _runtime()._build_mode_parity_evidence(min_sample_count=1)

    assert evidence["evidence_status"] == "INCOMPLETE"
    assert evidence["mismatch_count"] >= 2
    sample = evidence["samples"][0]
    assert "risk_scale" in sample["mismatch_fields"]["live_precheck"]
    assert "effective_notional" in sample["mismatch_fields"]["live_precheck"]
    assert sample["parity_result"] == "FAIL"


def test_mode_parity_fails_when_all_modes_share_same_wrong_sizing(monkeypatch):
    wrong = _surface(
        original_notional=10.0,
        risk_scale=0.5,
        effective_notional=10.0,
    )

    monkeypatch.setattr(
        RuntimeOrchestrator,
        "_evaluate_pre_submit",
        lambda self, signal_payload, market_ctx, regime_ctx, stats_ctx: dict(wrong),
    )
    evidence = _runtime()._build_mode_parity_evidence(min_sample_count=1)

    assert evidence["evidence_status"] == "INCOMPLETE"
    assert evidence["mismatch_count"] == 0
    assert evidence["missing_field_count"] == 0
    assert evidence["semantic_violation_count"] == 1
    assert "EFFECTIVE_NOTIONAL_SCALING_MISMATCH" in evidence["samples"][0]["semantic_error"]


def test_mode_parity_complete_for_valid_identical_protected_surfaces(monkeypatch):
    valid = _surface()
    monkeypatch.setattr(
        RuntimeOrchestrator,
        "_evaluate_pre_submit",
        lambda self, signal_payload, market_ctx, regime_ctx, stats_ctx: dict(valid),
    )
    evidence = _runtime()._build_mode_parity_evidence(min_sample_count=1)

    assert evidence["evidence_status"] == "COMPLETE"
    assert evidence["mismatch_count"] == 0
    assert evidence["missing_field_count"] == 0
    assert evidence["semantic_violation_count"] == 0
    assert evidence["modes_compared"] == ["BACKTEST", "PAPER", "LIVE_PRECHECK"]
    assert {
        "risk_scale",
        "effective_notional",
        "stop_distance_basis",
        "portfolio_decision",
        "rr_basis",
    } <= set(evidence["comparison_fields"])


def test_current_backtest_legacy_rr_stage_cannot_fake_three_mode_parity():
    evidence = _runtime()._build_mode_parity_evidence(min_sample_count=1)

    assert evidence["evidence_status"] == "INCOMPLETE"
    assert evidence["modes_compared"] == ["BACKTEST", "PAPER", "LIVE_PRECHECK"]
    sample = evidence["samples"][0]
    assert sample["backtest"]["rr_basis"] == "PLANNED_ENTRY_LEGACY_BACKTEST"
    assert "executable_raw_rr" in sample["missing_fields"]["backtest"]
    assert "remaining_execution_penalty" in sample["missing_fields"]["backtest"]
    assert sample["parity_result"] == "FAIL"
