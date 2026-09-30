from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text

from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _runtime(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'issue561.db'}")
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            phase7_burnin_release_id="issue561",
            reject_forward_horizon_bars=1,
            min_signal_score=0.50,
            min_rr=1.20,
            min_effective_rr=1.10,
            min_sl_pct=0.15,
            max_sl_pct=1.50,
        ),
        ai_brain=object(),
        market_scanner=lambda: None,
        persistence_engine=engine,
        scanner_source="PAPER_RUNTIME",
        paper_slippage_bps=2.0,
    )
    runtime._start_or_resume_burnin_run()
    assert runtime._burnin_run_id
    return engine, runtime


def _execution_ctx():
    return {
        "spread_pct": 0.0002,
        "expected_slippage_pct": 0.0002,
        "fee_pct": 0.0004,
        "funding_rate_pct": 0.0,
        "latency_ms": 50.0,
        "liquidity_score": 1.0,
        "volatility_regime": "normal",
    }


def _guided_missing_payload(
    signal_id: str,
    *,
    alignment_reasons=(),
    generation_reason=None,
    geometry_reason=None,
    geometry_status="INCOMPLETE",
    geometry_overrides=None,
):
    geometry = {
        "evidence_status": geometry_status,
        "reason": geometry_reason,
        "execution_entry": None,
        "entry_zone_low": None,
        "entry_zone_high": None,
        "structural_stop": None,
        "structural_target": None,
        "setup_type": None,
        "setup_phase": None,
        "side": None,
        "geometry_source": "MTF_SETUP_STRUCTURE",
        "forward_geometry_valid": False,
        **(geometry_overrides or {}),
    }
    return {
        "signal_id": signal_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 101.1,
        "rr": 1.10,
        "candidate_rr": 1.10,
        "expected_fill": 100.02,
        "executable_raw_rr": 0.50,
        "remaining_execution_penalty": 0.10,
        "effective_rr": 0.40,
        "score": 0.20,
        "expectancy_after_costs": -0.20,
        "reason": "NEGATIVE_EXPECTANCY_AFTER_COSTS",
        "reject_reasons": [
            "NEGATIVE_EXPECTANCY_AFTER_COSTS",
            "LOW_EFFECTIVE_RR",
            "LOW_SCORE",
            "RR_TOO_LOW",
            "STOP_TOO_WIDE",
        ],
        "geometry_status": "COMPLETE",
        "execution_ctx": _execution_ctx(),
        "timeframe": "1m",
        "decision_timestamp": "2026-09-30T12:00:00Z",
        "mtf": {
            "alignment": {
                "aligned": False,
                "reasons": list(alignment_reasons),
            },
            "generation": {
                "mode": "REGIME_GUIDED",
                "evidence_status": "INCOMPLETE",
                "candidate": None,
                "reason": generation_reason,
                "geometry_evidence": geometry,
            },
        },
    }


@pytest.mark.parametrize(
    ("reason", "alignment_reasons"),
    [
        ("MTF_EXECUTION_NOT_CONFIRMED", ["MTF_EXECUTION_NOT_CONFIRMED"]),
        ("MTF_EXECUTION_COUNTER_REGIME", ["MTF_EXECUTION_COUNTER_REGIME"]),
        ("MTF_NO_VALID_SETUP", ["MTF_NO_VALID_SETUP"]),
    ],
)
def test_missing_guided_candidate_uses_specific_mtf_reason_and_scrubs_shadow_gates(
    tmp_path, reason, alignment_reasons
):
    _engine, runtime = _runtime(tmp_path)
    payload = runtime._canonical_reject_payload(
        _guided_missing_payload(
            f"guided-{reason.lower()}",
            alignment_reasons=alignment_reasons,
        )
    )

    assert payload["primary_reject_reason"] == reason
    assert payload["authoritative_reject_reason"] == reason
    assert payload["reject_reasons"] == [reason]
    assert reason in payload["all_failed_gates"]

    for leaked in (
        "LOW_EFFECTIVE_RR",
        "RR_TOO_LOW",
        "STOP_TOO_TIGHT",
        "STOP_TOO_WIDE",
        "LOW_SCORE",
        "NEGATIVE_EXPECTANCY_AFTER_COSTS",
    ):
        assert leaked not in payload["all_failed_gates"]
        assert leaked not in payload["reject_reasons"]

    assert payload["effective_rr"] is None
    assert payload["rr"] is None
    assert payload["entry"] is None
    assert payload["score"] is None
    assert payload["expectancy_after_costs"] is None
    assert payload["reject_quality_attributable"] is False

    shadow = payload["legacy_shadow_geometry"]
    assert shadow["effective_rr"] == pytest.approx(0.40)
    assert shadow["score"] == pytest.approx(0.20)
    assert "LOW_EFFECTIVE_RR" in shadow["all_failed_gates"]
    assert "LOW_SCORE" in shadow["all_failed_gates"]
    assert shadow["attributable"] is False


def test_entry_outside_setup_zone_preserves_specific_guided_geometry_reason(tmp_path):
    _engine, runtime = _runtime(tmp_path)
    payload = runtime._canonical_reject_payload(
        _guided_missing_payload(
            "guided-outside-zone",
            generation_reason="EXECUTION_ENTRY_OUTSIDE_SETUP_ZONE",
            geometry_reason="EXECUTION_ENTRY_OUTSIDE_SETUP_ZONE",
            geometry_status="COMPLETE",
            geometry_overrides={
                "execution_entry": 84405.2,
                "entry_zone_low": 84176.6,
                "entry_zone_high": 84337.2,
                "structural_stop": 84000.0,
                "structural_target": 85000.0,
                "setup_type": "LONG_CONTINUATION",
                "setup_phase": "CONTINUATION",
                "side": "LONG",
                "forward_geometry_valid": False,
            },
        )
    )

    assert payload["primary_reject_reason"] == "EXECUTION_ENTRY_OUTSIDE_SETUP_ZONE"
    assert payload["geometry_status"] == "REJECTED"
    assert payload["geometry_reason"] == "EXECUTION_ENTRY_OUTSIDE_SETUP_ZONE"
    assert payload["entry"] == pytest.approx(84405.2)
    assert payload["sl"] == pytest.approx(84000.0)
    assert payload["tp"] == pytest.approx(85000.0)
    assert payload["rr"] is None
    assert payload["effective_rr"] is None
    assert payload["score"] is None
    assert payload["reject_quality_attributable"] is False
    assert payload["non_attributable_reason"] == "GUIDED_REJECT_FORWARD_GEOMETRY_INVALID"
    assert payload["forward_label_subject"] == "GUIDED_GEOMETRY_REJECT"
    assert "LOW_EFFECTIVE_RR" not in payload["all_failed_gates"]


def test_generic_guided_geometry_reason_is_only_fallback_when_specific_reason_absent(tmp_path):
    _engine, runtime = _runtime(tmp_path)
    payload = runtime._canonical_reject_payload(
        _guided_missing_payload("guided-unspecified")
    )
    assert payload["primary_reject_reason"] == "MTF_GUIDED_GEOMETRY_UNAVAILABLE"
    assert payload["reject_reasons"] == ["MTF_GUIDED_GEOMETRY_UNAVAILABLE"]
    assert payload["all_failed_gates"] == ["MTF_GUIDED_GEOMETRY_UNAVAILABLE"]


def test_canonical_reject_identity_stays_idempotent_when_shadow_reason_is_rewritten(tmp_path):
    _engine, runtime = _runtime(tmp_path)
    source = _guided_missing_payload(
        "guided-idempotent",
        alignment_reasons=["MTF_EXECUTION_NOT_CONFIRMED"],
    )
    first = runtime._canonical_reject_payload(source)
    second = runtime._canonical_reject_payload(source)

    assert first["reject_decision_id"] == second["reject_decision_id"]
    assert first["primary_reject_reason"] == second["primary_reject_reason"]
    assert first["all_failed_gates"] == second["all_failed_gates"]


def test_forward_label_from_missing_guided_candidate_remains_non_attributable(tmp_path):
    engine, runtime = _runtime(tmp_path)
    source = _guided_missing_payload(
        "guided-forward-label",
        alignment_reasons=["MTF_EXECUTION_NOT_CONFIRMED"],
    )

    asyncio.run(runtime._persist_reject(source))

    with engine.connect() as conn:
        observation = conn.execute(
            text(
                """SELECT metrics_json FROM burnin_observations
                   WHERE burnin_run_id=:bid AND decision='REJECTED'
                     AND json_extract(metrics_json,'$.signal_id')='guided-forward-label'"""
            ),
            {"bid": runtime._burnin_run_id},
        ).scalar_one()
        pending = conn.execute(
            text(
                """SELECT source_provenance_json
                   FROM burnin_pending_reject_labels
                   WHERE signal_id='guided-forward-label'"""
            )
        ).scalar_one()

    metrics = json.loads(observation)
    provenance = json.loads(pending)

    assert metrics["primary_reject_reason"] == "MTF_EXECUTION_NOT_CONFIRMED"
    assert "LOW_EFFECTIVE_RR" not in metrics["all_failed_gates"]
    assert provenance["reject_quality_attributable"] is False
    assert provenance["non_attributable_reason"] == "LEGACY_SHADOW_NOT_GUIDED_EQUIVALENT"
    assert provenance["forward_label_subject"] == "LEGACY_SCANNER_SHADOW_CANDIDATE"
