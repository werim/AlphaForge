from __future__ import annotations

import hashlib
import json

from alphaforge.multi_timeframe import REGIME_GUIDED_SETUP_PHASES
from alphaforge.pullback_policy_research import (
    BASELINE_PHASES,
    BASELINE_VARIANT,
    MANIFEST_SCHEMA,
    STRICT_PHASES,
    STRICT_VARIANT,
    FrozenResearchManifest,
    ResearchVerdict,
    evaluate_policy_rows,
    policy_row_from_mapping,
    report_from_payload,
)
from alphaforge.walk_forward import SegmentRole


def _raw(
    row_id: str,
    *,
    role: str,
    phase: str,
    net_r: float,
    decision: str = "ACCEPT",
    setup_type: str | None = None,
    segment_id: str | None = None,
    mfe_r: float = 1.5,
    mae_r: float = 0.5,
    tp: bool | None = None,
    sl: bool | None = None,
) -> dict:
    suffix = {
        "PULLBACK": "PULLBACK",
        "REENTRY_READY": "REENTRY",
        "CONTINUATION": "CONTINUATION",
    }.get(phase, phase)
    return {
        "row_id": row_id,
        "segment_id": segment_id or ("test" if role == "UNTOUCHED_TEST" else "oos"),
        "role": role,
        "symbol": "BTCUSDT",
        "side": "SHORT",
        "regime": "SHORT",
        "setup_type": setup_type or f"SHORT_{suffix}",
        "setup_phase": phase,
        "baseline_decision": decision,
        "net_r": net_r,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "would_have_hit_tp": tp,
        "would_have_hit_sl": sl,
    }


def _manifest(*, source_hash: str = "d" * 64, fresh: bool = True) -> FrozenResearchManifest:
    return FrozenResearchManifest(
        git_sha="a" * 40,
        config_hash="b" * 64,
        data_hash="c" * 64,
        universe_hash="e" * 64,
        source_artifact_sha256=source_hash,
        search_lineage_id="issue549-selection-v1",
        candidate_variant_ids=("strict-v1",),
        evaluation_count=1 if fresh else 2,
        influenced_selection=False,
        declared_fresh=fresh,
        untouched_segment_ids=("test",),
    )


def test_research_baseline_is_exactly_current_production_policy() -> None:
    assert BASELINE_PHASES == tuple(REGIME_GUIDED_SETUP_PHASES)
    assert BASELINE_PHASES == ("CONTINUATION", "PULLBACK", "REENTRY_READY")
    assert STRICT_PHASES == ("CONTINUATION", "REENTRY_READY")


def test_strict_variant_rejects_only_baseline_accepted_pullback() -> None:
    rows = tuple(
        policy_row_from_mapping(row)
        for row in (
            _raw("oos-p", role="OOS_VALIDATION", phase="PULLBACK", net_r=-1.0, tp=False, sl=True),
            _raw("oos-r", role="OOS_VALIDATION", phase="REENTRY_READY", net_r=1.0),
            _raw("test-r", role="UNTOUCHED_TEST", phase="REENTRY_READY", net_r=1.2),
        )
    )
    report = evaluate_policy_rows(rows, _manifest())
    assert report["production_policy_changed"] is False
    assert report["baseline_variant"]["eligible_phases"] == BASELINE_PHASES
    assert report["strict_variant"]["eligible_phases"] == STRICT_PHASES
    assert report["oos_validation"]["baseline"]["accepted_count"] == 2
    assert report["oos_validation"]["strict"]["accepted_count"] == 1
    assert report["oos_validation"]["strict"]["newly_rejected_count"] == 1
    assert report["oos_validation"]["strict"]["reject_saved_loss_count"] == 1


def test_positive_oos_and_positive_fresh_single_variant_holdout_support_strict() -> None:
    rows = tuple(
        policy_row_from_mapping(row)
        for row in (
            _raw("oos-p", role="OOS_VALIDATION", phase="PULLBACK", net_r=-1.0),
            _raw("oos-r", role="OOS_VALIDATION", phase="REENTRY_READY", net_r=1.0),
            _raw("oos-c", role="OOS_VALIDATION", phase="CONTINUATION", net_r=0.6),
            _raw("test-r", role="UNTOUCHED_TEST", phase="REENTRY_READY", net_r=0.8),
            _raw("test-c", role="UNTOUCHED_TEST", phase="CONTINUATION", net_r=0.4),
        )
    )
    report = evaluate_policy_rows(rows, _manifest())
    assert report["blockers"] == ()
    assert report["verdict"] == ResearchVerdict.PASS_STRICT.value
    assert report["untouched_test"]["selected_variant"] == STRICT_VARIANT
    assert "baseline" not in report["untouched_test"]
    assert report["live_authorized"] is False


def test_oos_that_does_not_improve_retains_current_policy() -> None:
    rows = tuple(
        policy_row_from_mapping(row)
        for row in (
            _raw("oos-p", role="OOS_VALIDATION", phase="PULLBACK", net_r=2.0),
            _raw("oos-r", role="OOS_VALIDATION", phase="REENTRY_READY", net_r=-0.5),
            _raw("test-r", role="UNTOUCHED_TEST", phase="REENTRY_READY", net_r=1.0),
        )
    )
    report = evaluate_policy_rows(rows, _manifest())
    assert report["verdict"] == ResearchVerdict.RETAIN_BASELINE.value


def test_negative_selected_variant_holdout_retains_current_policy() -> None:
    rows = tuple(
        policy_row_from_mapping(row)
        for row in (
            _raw("oos-p", role="OOS_VALIDATION", phase="PULLBACK", net_r=-1.0),
            _raw("oos-r", role="OOS_VALIDATION", phase="REENTRY_READY", net_r=1.0),
            _raw("test-r", role="UNTOUCHED_TEST", phase="REENTRY_READY", net_r=-0.5),
        )
    )
    report = evaluate_policy_rows(rows, _manifest())
    assert report["verdict"] == ResearchVerdict.RETAIN_BASELINE.value


def test_missing_untouched_rows_is_inconclusive() -> None:
    rows = (policy_row_from_mapping(
        _raw("oos-r", role="OOS_VALIDATION", phase="REENTRY_READY", net_r=1.0)
    ),)
    report = evaluate_policy_rows(rows, _manifest())
    assert report["verdict"] == ResearchVerdict.INCONCLUSIVE.value
    assert "NO_UNTOUCHED_TEST_ROWS" in report["blockers"]


def test_reused_holdout_cannot_be_declared_admissible() -> None:
    rows = tuple(
        policy_row_from_mapping(row)
        for row in (
            _raw("oos-r", role="OOS_VALIDATION", phase="REENTRY_READY", net_r=1.0),
            _raw("test-r", role="UNTOUCHED_TEST", phase="REENTRY_READY", net_r=1.0),
        )
    )
    report = evaluate_policy_rows(rows, _manifest(fresh=False))
    assert report["verdict"] == ResearchVerdict.INCONCLUSIVE.value
    assert "UNTOUCHED_LINEAGE_NOT_PROMOTION_ADMISSIBLE" in report["blockers"]
    assert "MISSING_UNTOUCHED_TEST_SEGMENT" not in report["blockers"]
    assert report["manifest"]["evaluation_count"] == 2


def test_two_candidate_variants_cannot_claim_fresh_holdout() -> None:
    manifest = FrozenResearchManifest(
        git_sha="a" * 40,
        config_hash="b" * 64,
        data_hash="c" * 64,
        universe_hash="e" * 64,
        source_artifact_sha256="d" * 64,
        search_lineage_id="bad",
        candidate_variant_ids=("baseline", "strict"),
        evaluation_count=2,
        influenced_selection=False,
        declared_fresh=True,
        untouched_segment_ids=("test",),
    )
    assert "INVALID_SEARCH_SELECTION_LINEAGE" in manifest.validate()
    assert manifest.untouched_admissible is False


def test_source_hash_mismatch_fails_closed() -> None:
    raw_rows = [
        _raw("oos-r", role="OOS_VALIDATION", phase="REENTRY_READY", net_r=1.0),
        _raw("test-r", role="UNTOUCHED_TEST", phase="REENTRY_READY", net_r=1.0),
    ]
    payload = {
        "schema_version": MANIFEST_SCHEMA,
        "frozen_research": {
            "git_sha": "a" * 40,
            "config_hash": "b" * 64,
            "data_hash": "c" * 64,
            "universe_hash": "e" * 64,
            "source_artifact_sha256": "f" * 64,
            "search_lineage_id": "issue549-selection-v1",
            "candidate_variant_ids": ["strict-v1"],
            "evaluation_count": 1,
            "influenced_selection": False,
            "declared_fresh": True,
            "untouched_segment_ids": ["test"],
        },
        "rows": raw_rows,
    }
    report = report_from_payload(payload)
    assert report["verdict"] == ResearchVerdict.INCONCLUSIVE.value
    assert "SOURCE_ARTIFACT_HASH_MISMATCH" in report["blockers"]


def test_matching_source_hash_preserves_frozen_identity() -> None:
    raw_rows = [
        _raw("oos-p", role="OOS_VALIDATION", phase="PULLBACK", net_r=-1.0),
        _raw("oos-r", role="OOS_VALIDATION", phase="REENTRY_READY", net_r=1.0),
        _raw("test-r", role="UNTOUCHED_TEST", phase="REENTRY_READY", net_r=1.0),
    ]
    canonical = json.dumps(raw_rows, sort_keys=True, separators=(",", ":"), default=str)
    source_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload = {
        "schema_version": MANIFEST_SCHEMA,
        "frozen_research": {
            "git_sha": "a" * 40,
            "config_hash": "b" * 64,
            "data_hash": "c" * 64,
            "universe_hash": "e" * 64,
            "source_artifact_sha256": source_hash,
            "search_lineage_id": "issue549-selection-v1",
            "candidate_variant_ids": ["strict-v1"],
            "evaluation_count": 1,
            "influenced_selection": False,
            "declared_fresh": True,
            "untouched_segment_ids": ["test"],
        },
        "rows": raw_rows,
    }
    report = report_from_payload(payload)
    assert report["manifest"]["git_sha"] == "a" * 40
    assert report["manifest"]["source_artifact_sha256"] == source_hash
    assert report["production_policy_changed"] is False


def test_grouped_output_keeps_role_setup_regime_side_dimensions() -> None:
    rows = tuple(
        policy_row_from_mapping(row)
        for row in (
            _raw("oos-p", role="OOS_VALIDATION", phase="PULLBACK", net_r=-1.0),
            _raw("test-r", role="UNTOUCHED_TEST", phase="REENTRY_READY", net_r=1.0),
        )
    )
    report = evaluate_policy_rows(rows, _manifest())
    keys = report["by_role_setup_regime_side"][0].keys()
    assert {"role", "setup_phase", "regime", "side", "baseline", "strict"}.issubset(keys)
    assert report["baseline_variant"]["name"] == BASELINE_VARIANT
    assert report["strict_variant"]["name"] == STRICT_VARIANT
