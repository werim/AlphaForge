from __future__ import annotations

import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from alphaforge.burnin_campaign import build_phase8_campaign_identity
from alphaforge.multi_timeframe import build_mtf_candidate_context, evaluate_mtf_alignment
from alphaforge.regime_guided_research import (
    FrozenResearchIdentity,
    RegimeGuidedResearchStore,
    calibration_report,
    build_research_observation,
    compute_research_features,
    load_research_rows,
    research_report,
    stack_env,
    stack_summary,
)
from alphaforge.runtime import ExecutionMode, RuntimeConfig


BASE_TS = 1_700_000_000_000


def _candles(n: int, *, step: float, tf_ms: int, start: float = 100.0):
    out = []
    for i in range(n):
        close = start + i * step
        out.append({
            "open_ts": BASE_TS + i * tf_ms,
            "close_ts": BASE_TS + (i + 1) * tf_ms - 1,
            "open": close - 0.1,
            "high": close + 0.4,
            "low": close - 0.4,
            "close": close,
            "volume": 1000.0 + i,
        })
    return out


def _feature_inputs():
    regime = _candles(25, step=0.5, tf_ms=3_600_000)
    setup = _candles(20, step=0.2, tf_ms=900_000)
    execution = _candles(10, step=0.05, tf_ms=60_000)
    decision = max(regime[-1]["close_ts"], setup[-1]["close_ts"], execution[-1]["close_ts"])
    return regime, setup, execution, decision


def _identity(stack: str = "1h-15m-1m", role: str = "OOS") -> FrozenResearchIdentity:
    return FrozenResearchIdentity(
        git_sha="a" * 40,
        base_config_hash="e" * 64,
        config_hash="b" * 64,
        data_hash="c" * 64,
        universe_hash="d" * 64,
        experiment_id="issue565-v1",
        stack_id=stack,
        segment_role=role,
        segment_id=f"{role.lower()}-1",
        minimum_segment_samples=2,
    )


def test_future_candles_cannot_change_research_features() -> None:
    regime, setup, execution, decision = _feature_inputs()
    baseline = compute_research_features(
        regime_candles=regime,
        setup_candles=setup,
        execution_candles=execution,
        decision_ts_ms=decision,
        trade_side="LONG",
        executable_entry=104.0,
        structural_stop=102.0,
    )
    future = {
        "open_ts": decision + 1,
        "close_ts": decision + 60_000,
        "open": 999.0,
        "high": 2000.0,
        "low": 1.0,
        "close": 1500.0,
        "volume": 1.0,
    }
    with_future = compute_research_features(
        regime_candles=[*regime, future],
        setup_candles=[*setup, future],
        execution_candles=[*execution, future],
        decision_ts_ms=decision,
        trade_side="LONG",
        executable_entry=104.0,
        structural_stop=102.0,
    )
    assert with_future == baseline


def test_features_are_deterministic_and_missing_evidence_stays_unavailable() -> None:
    regime, setup, execution, decision = _feature_inputs()
    kwargs = dict(
        regime_candles=regime,
        setup_candles=setup,
        execution_candles=execution,
        decision_ts_ms=decision,
        trade_side="LONG",
        executable_entry=104.0,
        structural_stop=102.0,
    )
    first = compute_research_features(**kwargs)
    second = compute_research_features(**kwargs)
    assert first == second
    assert first["h1_regime_strength"]["normalized_strength"] is not None
    assert first["h2_setup_structure"]["structural_state"] == "HH_HL"
    assert first["h3_stop_noise"]["stop_noise_ratio"] is not None
    assert first["h4_execution_confirmation"]["two_bar_persistence_confirmation"] is True

    incomplete = compute_research_features(
        regime_candles=regime[:5],
        setup_candles=setup[:5],
        execution_candles=execution[:2],
        decision_ts_ms=decision,
        trade_side="LONG",
        executable_entry=104.0,
        structural_stop=102.0,
    )
    assert incomplete["h1_regime_strength"]["normalized_strength"] is None
    assert incomplete["h2_setup_structure"]["atr"] is None
    assert incomplete["h3_stop_noise"]["stop_noise_ratio"] is None
    assert incomplete["h4_execution_confirmation"]["reclaim_confirmation"] is None


def test_research_computation_cannot_mutate_authoritative_mtf_result() -> None:
    regime, setup, execution, decision = _feature_inputs()
    candles = {"regime": regime, "setup": setup, "execution": execution}
    market = {
        "spread_pct": 0.0001,
        "expected_slippage_pct": 0.0001,
        "market_data_latency_ms": 10.0,
        "latency_ms": 50.0,
        "liquidity_score": 1.0,
    }
    frozen_candles = copy.deepcopy(candles)
    before = build_mtf_candidate_context(
        candles,
        execution_ctx=market,
        decision_ts_ms=decision,
        regime_timeframe="1h",
        setup_timeframe="15m",
        execution_timeframe="1m",
        guided_signal_generation_enabled=True,
    )
    compute_research_features(
        regime_candles=regime,
        setup_candles=setup,
        execution_candles=execution,
        decision_ts_ms=decision,
        trade_side="LONG",
        executable_entry=104.0,
        structural_stop=102.0,
    )
    after = build_mtf_candidate_context(
        candles,
        execution_ctx=market,
        decision_ts_ms=decision,
        regime_timeframe="1h",
        setup_timeframe="15m",
        execution_timeframe="1m",
        guided_signal_generation_enabled=True,
    )
    assert before == after
    assert candles == frozen_candles


def test_4h_and_1d_contexts_have_explicit_freshness_semantics() -> None:
    decision = BASE_TS + 10 * 86_400_000
    regime = {
        "timeframe": "1d",
        "direction": "LONG",
        "evidence_status": "COMPLETE",
        "last_closed_candle_ms": decision - 86_400_000,
    }
    setup = {
        "timeframe": "4h",
        "direction": "LONG",
        "trade_side": "LONG",
        "phase": "PULLBACK",
        "generation_mode": "REGIME_GUIDED",
        "evidence_status": "COMPLETE",
        "last_closed_candle_ms": decision - 4 * 3_600_000,
    }
    execution = {
        "timeframe": "1h",
        "direction": "LONG",
        "trigger": "MOMENTUM_CONFIRMED",
        "confirmed_for_side": True,
        "evidence_status": "COMPLETE",
        "last_closed_candle_ms": decision - 3_600_000,
    }
    result = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=decision)
    assert result["aligned"] is True
    assert "MTF_CONTEXT_STALE" not in result["reasons"]

    regime["last_closed_candle_ms"] = decision - 3 * 86_400_000
    stale = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=decision)
    assert "MTF_CONTEXT_STALE" in stale["reasons"]


def test_timeframe_stack_changes_campaign_config_identity_deterministically() -> None:
    baseline_cfg = RuntimeConfig(execution_mode=ExecutionMode.PAPER)
    wider_cfg = RuntimeConfig(
        execution_mode=ExecutionMode.PAPER,
        regime_timeframe="4h",
        setup_timeframe="1h",
        execution_timeframe="15m",
    )
    baseline = build_phase8_campaign_identity(
        baseline_cfg, ["BTCUSDT", "ETHUSDT"], ["1h"], release_id="issue565"
    )
    wider = build_phase8_campaign_identity(
        wider_cfg, ["BTCUSDT", "ETHUSDT"], ["1h"], release_id="issue565"
    )
    wider_repeat = build_phase8_campaign_identity(
        wider_cfg, ["BTCUSDT", "ETHUSDT"], ["1h"], release_id="issue565"
    )
    assert baseline["config_hash"] != wider["config_hash"]
    assert wider["config_hash"] == wider_repeat["config_hash"]
    assert wider["config_payload"]["multi_timeframe"] == {
        "regime_timeframe": "4h",
        "setup_timeframe": "1h",
        "execution_timeframe": "15m",
        "guided_signal_generation_enabled": True,
    }


def test_research_store_is_separate_and_idempotent(tmp_path) -> None:
    regime, setup, execution, decision = _feature_inputs()
    features = compute_research_features(
        regime_candles=regime,
        setup_candles=setup,
        execution_candles=execution,
        decision_ts_ms=decision,
        trade_side="LONG",
        executable_entry=104.0,
        structural_stop=102.0,
    )
    observation = build_research_observation(
        identity=_identity(),
        symbol="BTCUSDT",
        signal_id="signal-1",
        decision_ts_ms=decision,
        authoritative={
            "decision": "REJECTED",
            "reject_reason": "LOW_SCORE",
            "side": "LONG",
            "entry": 104.0,
            "sl": 102.0,
            "tp": 108.0,
            "raw_rr": 2.0,
            "effective_rr": 1.8,
            "score": 0.4,
            "risk_scale": 1.0,
        },
        features=features,
    )
    path = tmp_path / "research.db"
    store = RegimeGuidedResearchStore(path)
    first = store.record(observation)
    second = store.record(observation)
    assert first == second
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM regime_guided_research_observations"
        ).fetchone()[0] == 1
        row = conn.execute(
            "SELECT authoritative_json,feature_json,evidence_complete "
            "FROM regime_guided_research_observations"
        ).fetchone()
        authoritative = json.loads(row[0])
        assert authoritative["decision"] == "REJECTED"
        assert authoritative["sl"] == 102.0
        assert row[2] == 1

    campaign_db = tmp_path / "campaign.db"
    with sqlite3.connect(campaign_db) as conn:
        conn.execute("CREATE TABLE burnin_campaigns(campaign_id TEXT)")
    with pytest.raises(ValueError, match="must not share"):
        RegimeGuidedResearchStore(campaign_db).record(observation)


def test_calibration_uses_only_complete_unambiguous_resolved_labels() -> None:
    report = calibration_report([
        {
            "p_win": 0.8, "resolved_win": True, "evidence_complete": True,
            "ambiguous": False, "side": "LONG", "setup_phase": "PULLBACK",
            "regime": "TRENDING",
        },
        {
            "p_win": 0.2, "resolved_win": False, "evidence_complete": True,
            "ambiguous": False, "side": "SHORT", "setup_phase": "CONTINUATION",
            "regime": "TRENDING",
        },
        {
            "p_win": 0.9, "resolved_win": True, "evidence_complete": False,
            "ambiguous": False,
        },
        {
            "p_win": 0.9, "resolved_win": True, "evidence_complete": True,
            "ambiguous": True,
        },
    ])
    assert report["status"] == "COMPLETE"
    assert report["sample_count"] == 2
    assert report["excluded_count"] == 2
    assert report["brier_score"] == pytest.approx(0.04)
    assert report["expected_calibration_error"] == pytest.approx(0.2)
    assert all(bucket["observed_rate_ci95"][0] <= bucket["observed_rate"] <= bucket["observed_rate_ci95"][1]
               for bucket in report["buckets"])


def test_report_fails_closed_without_oos_and_untouched_evidence() -> None:
    report = research_report([], candidate_stack_id="4h-1h-15m", minimum_segment_samples=2)
    assert report["verdict"] == "INCONCLUSIVE"
    assert any(blocker.startswith("MISSING_OOS") for blocker in report["blockers"])
    assert report["production_semantics_changed"] is False
    assert report["live_authorized"] is False


def test_report_only_supports_separate_issue_after_oos_and_holdout() -> None:
    rows = []
    for role in ("OOS", "UNTOUCHED_HOLDOUT"):
        for stack, values in (
            ("1h-15m-1m", [0.1, -0.1]),
            ("4h-1h-15m", [0.2, 0.2]),
        ):
            for index, net_r in enumerate(values):
                rows.append({
                    "segment_role": role,
                    "segment_id": role.lower() + "-1",
                    "stack_id": stack,
                    "experiment_id": "issue565-v1",
                    "git_sha": "a" * 40,
                    "base_config_hash": "e" * 64,
                    "config_hash": ("b" if stack == "1h-15m-1m" else "f") * 64,
                    "data_hash": "c" * 64,
                    "universe_hash": "d" * 64,
                    "evaluation_count": 1,
                    "declared_fresh": True,
                    "evidence_complete": True,
                    "ambiguous": False,
                    "net_r": net_r,
                    "mfe_r": max(net_r, 0.0) + 0.5,
                    "mae_r": max(-net_r, 0.0) + 0.2,
                    "execution_cost_drag_r": 0.05,
                })
    report = research_report(
        rows,
        candidate_stack_id="4h-1h-15m",
        minimum_segment_samples=2,
    )
    assert report["blockers"] == ()
    assert report["verdict"] == "SUPPORTED_FOR_SEPARATE_PRODUCTION_CHANGE"
    assert report["production_semantics_changed"] is False


def test_stack_env_is_explicit_and_does_not_need_mutable_shared_env() -> None:
    assert stack_env("1h-15m-1m") == {
        "ALPHAFORGE_REGIME_TIMEFRAME": "1h",
        "ALPHAFORGE_SETUP_TIMEFRAME": "15m",
        "ALPHAFORGE_EXECUTION_TIMEFRAME": "1m",
    }
    assert stack_env("1d-context-4h-1h-15m") == {
        "ALPHAFORGE_REGIME_TIMEFRAME": "4h",
        "ALPHAFORGE_SETUP_TIMEFRAME": "1h",
        "ALPHAFORGE_EXECUTION_TIMEFRAME": "15m",
        "ALPHAFORGE_RESEARCH_MACRO_CONTEXT_TIMEFRAME": "1d",
    }


def test_stack_comparison_blocks_cross_scoped_or_reused_holdout_evidence() -> None:
    rows = []
    for role in ("OOS", "UNTOUCHED_HOLDOUT"):
        for stack in ("1h-15m-1m", "4h-1h-15m"):
            rows.append({
                "segment_role": role,
                "segment_id": role.lower() + "-1",
                "stack_id": stack,
                "experiment_id": "issue565-v1",
                "git_sha": "a" * 40,
                "base_config_hash": "e" * 64,
                "config_hash": ("b" if stack == "1h-15m-1m" else "f") * 64,
                "data_hash": ("c" if stack == "1h-15m-1m" else "9") * 64,
                "universe_hash": "d" * 64,
                "evaluation_count": 2 if role == "UNTOUCHED_HOLDOUT" else 1,
                "declared_fresh": role != "UNTOUCHED_HOLDOUT",
                "evidence_complete": True,
                "ambiguous": False,
                "net_r": 0.2,
            })
    report = research_report(
        rows,
        candidate_stack_id="4h-1h-15m",
        minimum_segment_samples=1,
    )
    assert report["verdict"] == "INCONCLUSIVE"
    assert "OOS_DATA_HASH_MISMATCH" in report["blockers"]
    assert "UNTOUCHED_HOLDOUT_REUSED_OR_SELECTED" in report["blockers"]


def test_calibration_supports_p_tp_hit_without_reinterpreting_missing_labels() -> None:
    report = calibration_report(
        [
            {
                "p_tp_hit": 0.75, "resolved_tp_hit": True,
                "evidence_complete": True, "ambiguous": False,
                "side": "LONG", "setup_phase": "PULLBACK", "regime": "TRENDING",
            },
            {
                "p_tp_hit": 0.25, "resolved_tp_hit": False,
                "evidence_complete": True, "ambiguous": False,
                "side": "SHORT", "setup_phase": "CONTINUATION", "regime": "TRENDING",
            },
            {
                "p_tp_hit": 0.9, "resolved_tp_hit": None,
                "evidence_complete": True, "ambiguous": False,
            },
        ],
        probability_field="p_tp_hit",
        outcome_field="resolved_tp_hit",
    )
    assert report["probability_field"] == "p_tp_hit"
    assert report["outcome_field"] == "resolved_tp_hit"
    assert report["sample_count"] == 2
    assert report["excluded_count"] == 1
    assert report["brier_score"] == pytest.approx(0.0625)


def test_funding_carry_is_persisted_and_reported_separately(tmp_path) -> None:
    regime, setup, execution, decision = _feature_inputs()
    features = compute_research_features(
        regime_candles=regime,
        setup_candles=setup,
        execution_candles=execution,
        decision_ts_ms=decision,
        trade_side="LONG",
        executable_entry=104.0,
        structural_stop=102.0,
    )
    observation = build_research_observation(
        identity=_identity(),
        symbol="BTCUSDT",
        signal_id="signal-funding",
        decision_ts_ms=decision,
        authoritative={
            "decision": "ACCEPTED", "reject_reason": None, "side": "LONG",
            "entry": 104.0, "sl": 102.0, "tp": 108.0,
            "raw_rr": 2.0, "effective_rr": 1.8, "score": 0.8,
            "risk_scale": 1.0,
        },
        features=features,
    )
    path = tmp_path / "funding.db"
    store = RegimeGuidedResearchStore(path)
    rid = store.record(observation)
    store.record_outcome(
        rid,
        outcome_status="RESOLVED",
        net_r=0.5,
        mfe_r=0.8,
        mae_r=0.2,
        execution_cost_drag_r=0.12,
        funding_cost_r=0.03,
        hold_duration_seconds=7200,
        resolved_win=True,
        evidence_complete=True,
    )
    rows = load_research_rows(path)
    assert rows[0]["funding_cost_r"] == pytest.approx(0.03)
    summary = stack_summary(rows)[0]
    assert summary["mean_execution_cost_drag_r"] == pytest.approx(0.12)
    assert summary["mean_funding_cost_r"] == pytest.approx(0.03)
