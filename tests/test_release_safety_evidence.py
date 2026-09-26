from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from alphaforge.release_gates import ensure_release_gate_schema, latest_release_snapshot
from alphaforge.release_safety_evidence import persist_campaign_release_safety_evidence
from alphaforge.rollback_evidence import (
    ensure_rollback_evidence_schema,
    persist_rollback_validation_evidence,
)


def _git_runner(commit="sha-a", status=""):
    def run(args, cwd):
        if args == ["rev-parse", "HEAD"]:
            return commit
        if args == ["status", "--porcelain=v1"]:
            return status
        raise AssertionError(args)
    return run


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    ensure_release_gate_schema(engine)
    ensure_rollback_evidence_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE burnin_campaigns(
                campaign_id TEXT PRIMARY KEY,
                release_id TEXT NOT NULL,
                git_commit TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO burnin_campaigns(campaign_id,release_id,git_commit)
            VALUES ('camp-a','rel-a','sha-a'),('camp-b','rel-b','sha-b')
        """))
    return engine


def _valid_rollback(engine, *, validation_id="rollback-valid", recorded_at=None):
    payload = {
        "validation_id": validation_id,
        "git_commit": "sha-a",
        "kill_switch_block_verified": True,
        "no_submit_on_kill_switch_verified": True,
        "fail_closed_reconciliation_verified": True,
        "repair_actions_non_mutating_verified": True,
        "execution_mutation_attempt_count": 0,
        "blocking_reasons": [],
        "evidence_payload": {"validation_scope": "RELEASE_SAFETY_CLI_TEST"},
    }
    if recorded_at is not None:
        payload["recorded_at"] = recorded_at
    return persist_rollback_validation_evidence(engine, payload)


def _valid_runbook(tmp_path):
    path = tmp_path / "RUNBOOK.md"
    path.write_text(
        "# Test Runbook\n"
        "## Explicit LIVE boundary\nLIVE remains blocked.\n"
        "## Suspension conditions\nFail closed.\n"
        "## Operator workflow\nOperator verifies evidence.\n"
        "## Phase 9 PAPER Burn-in Operations\n"
        "Use recovery-drill before promotion and finalize only after qualification.\n",
        encoding="utf-8",
    )
    return path


def test_campaign_release_safety_requires_existing_campaign(tmp_path):
    engine = _engine()
    with pytest.raises(ValueError, match="CAMPAIGN_NOT_FOUND"):
        persist_campaign_release_safety_evidence(
            engine,
            campaign_id="missing",
            runbook_path=_valid_runbook(tmp_path),
            repo_path=tmp_path,
        git_runner=_git_runner(),
        )


def test_missing_rollback_source_fails_closed_but_records_runbook(tmp_path):
    engine = _engine()
    result = persist_campaign_release_safety_evidence(
        engine,
        campaign_id="camp-a",
        runbook_path=_valid_runbook(tmp_path),
        repo_path=tmp_path,
        git_runner=_git_runner(),
    )
    assert result["status"] == "FAIL"
    assert result["rollback"]["status"] == "FAIL"
    assert "ROLLBACK_EVIDENCE_MISSING" in result["rollback"]["evidence"]["blocking_reasons"]
    assert result["runbook"]["status"] == "PASS"
    assert result["runbook"]["evidence"]["git_commit"] == "sha-a"
    assert "ROLLBACK_EVIDENCE_UNVERIFIED" in result["release_gate_blockers"]


def test_stale_rollback_source_cannot_be_promoted_to_release_pass(tmp_path):
    engine = _engine()
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    _valid_rollback(engine, recorded_at=stale)
    result = persist_campaign_release_safety_evidence(
        engine,
        campaign_id="camp-a",
        runbook_path=_valid_runbook(tmp_path),
        rollback_max_age_sec=900,
        repo_path=tmp_path,
        git_runner=_git_runner(),
    )
    assert result["status"] == "FAIL"
    assert result["rollback"]["status"] == "FAIL"
    assert "ROLLBACK_EVIDENCE_STALE" in result["rollback"]["evidence"]["blocking_reasons"]


def test_invalid_runbook_fails_even_with_fresh_verified_rollback(tmp_path):
    engine = _engine()
    _valid_rollback(engine)
    runbook = tmp_path / "RUNBOOK.md"
    runbook.write_text("# Missing required safety sections\n", encoding="utf-8")
    result = persist_campaign_release_safety_evidence(
        engine,
        campaign_id="camp-a",
        runbook_path=runbook,
        repo_path=tmp_path,
        git_runner=_git_runner(),
    )
    assert result["status"] == "FAIL"
    assert result["rollback"]["status"] == "PASS"
    assert result["rollback"]["evidence"]["git_commit"] == "sha-a"
    assert result["runbook"]["status"] == "FAIL"
    assert result["runbook"]["evidence"]["missing_markers"]


def test_valid_rollback_and_runbook_are_scoped_to_campaign_release_without_other_gate_mutations(tmp_path):
    engine = _engine()
    _valid_rollback(engine)
    result = persist_campaign_release_safety_evidence(
        engine,
        campaign_id="camp-a",
        runbook_path=_valid_runbook(tmp_path),
        repo_path=tmp_path,
        git_runner=_git_runner(),
    )
    assert result["status"] == "PASS"
    assert result["release_id"] == "rel-a"
    assert result["campaign_git_commit"] == "sha-a"
    assert result["checkout_git_commit"] == "sha-a"
    assert result["rollback"]["status"] == "PASS"
    assert result["rollback"]["evidence"]["git_commit"] == "sha-a"
    assert result["runbook"]["status"] == "PASS"
    assert result["runbook"]["evidence"]["git_commit"] == "sha-a"
    assert result["safety"] == {
        "operator_ack_created": False,
        "canary_evidence_created": False,
        "full_test_evidence_created": False,
        "live_mutation_attempted": False,
        "thresholds_changed": False,
    }

    with engine.connect() as conn:
        rollback_releases = {
            row[0] for row in conn.execute(text("SELECT DISTINCT release_id FROM rollback_verification_events"))
        }
        runbook_releases = {
            row[0] for row in conn.execute(text("SELECT DISTINCT release_id FROM runbook_evidence"))
        }
        assert conn.execute(text("SELECT COUNT(*) FROM operator_acknowledgements")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM canary_run_events")).scalar_one() == 0

    assert rollback_releases == {"rel-a"}
    assert runbook_releases == {"rel-a"}
    snapshot = latest_release_snapshot(engine, release_id="rel-a", phase="PHASE6")
    assert snapshot is not None
    assert snapshot.rollback_verified is True
    assert snapshot.runbook_verified is True
    assert snapshot.operator_acknowledged is False
    assert snapshot.canary_ready is False


def test_campaign_release_safety_rejects_mismatched_checkout_before_writing_evidence(tmp_path):
    engine = _engine()
    _valid_rollback(engine)
    with pytest.raises(ValueError, match="CAMPAIGN_CHECKOUT_COMMIT_MISMATCH"):
        persist_campaign_release_safety_evidence(
            engine,
            campaign_id="camp-a",
            runbook_path=_valid_runbook(tmp_path),
            repo_path=tmp_path,
            git_runner=_git_runner("different-sha"),
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM rollback_verification_events")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM runbook_evidence")).scalar_one() == 0


def test_campaign_release_safety_rejects_dirty_checkout_before_writing_evidence(tmp_path):
    engine = _engine()
    _valid_rollback(engine)
    with pytest.raises(ValueError, match="WORKTREE_DIRTY"):
        persist_campaign_release_safety_evidence(
            engine,
            campaign_id="camp-a",
            runbook_path=_valid_runbook(tmp_path),
            repo_path=tmp_path,
            git_runner=_git_runner("sha-a", " M local.py"),
        )


def test_campaign_release_safety_rejects_runbook_outside_campaign_repository(tmp_path):
    engine = _engine()
    _valid_rollback(engine)
    outside = tmp_path.parent / "outside-runbook.md"
    outside.write_text(
        "# Test Runbook\n## Explicit LIVE boundary\n## Suspension conditions\n"
        "## Operator workflow\n## Phase 9 PAPER Burn-in Operations\nrecovery-drill finalize\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="RUNBOOK_OUTSIDE_CAMPAIGN_REPOSITORY"):
        persist_campaign_release_safety_evidence(
            engine,
            campaign_id="camp-a",
            runbook_path=outside,
            repo_path=tmp_path,
            git_runner=_git_runner(),
        )
