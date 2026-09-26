from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alphaforge.release_gates import (
    build_release_snapshot,
    persist_release_snapshot,
    persist_rollback_verification,
    persist_runbook_evidence,
)
from alphaforge.release_identity import GitRunner, require_campaign_checkout


def persist_campaign_release_safety_evidence(
    engine: Engine,
    *,
    campaign_id: str,
    phase: str = "PHASE6",
    runbook_path: str | Path = "RUNBOOK.md",
    rollback_max_age_sec: float = 900.0,
    repo_path: str | Path = ".",
    git_runner: GitRunner | None = None,
) -> dict[str, object]:
    """Persist rollback/runbook evidence for the release owned by one campaign."""
    with engine.connect() as conn:
        campaign = conn.execute(text("""
            SELECT campaign_id, release_id, git_commit
            FROM burnin_campaigns
            WHERE campaign_id=:campaign_id
        """), {"campaign_id": campaign_id}).mappings().first()
    if campaign is None:
        raise ValueError("CAMPAIGN_NOT_FOUND")

    release_id = str(campaign["release_id"])
    campaign_git_commit = str(campaign["git_commit"] or "")
    identity = require_campaign_checkout(
        campaign_git_commit,
        repo_path=repo_path,
        git_runner=git_runner,
    )
    rollback = persist_rollback_verification(
        engine,
        release_id=release_id,
        phase=phase,
        max_evidence_age_sec=rollback_max_age_sec,
        git_commit=campaign_git_commit,
    )
    runbook = persist_runbook_evidence(
        engine,
        release_id=release_id,
        phase=phase,
        runbook_path=runbook_path,
        git_commit=campaign_git_commit,
    )
    snapshot = build_release_snapshot(engine, release_id=release_id, phase=phase)
    persist_release_snapshot(engine, snapshot)

    rollback_pass = str(rollback.get("status") or "").upper() == "PASS"
    runbook_pass = str(runbook.get("status") or "").upper() == "PASS"
    return {
        "campaign_id": str(campaign["campaign_id"]),
        "release_id": release_id,
        "campaign_git_commit": campaign_git_commit,
        "checkout_git_commit": str(identity["git_commit"]),
        "phase": phase,
        "status": "PASS" if rollback_pass and runbook_pass else "FAIL",
        "rollback": rollback,
        "runbook": runbook,
        "release_gate_status": snapshot.status,
        "release_gate_blockers": snapshot.blocking_reasons,
        "safety": {
            "operator_ack_created": False,
            "canary_evidence_created": False,
            "full_test_evidence_created": False,
            "live_mutation_attempted": False,
            "thresholds_changed": False,
        },
    }


def _database_url(path: str) -> str:
    return "sqlite+pysqlite:///" + str(Path(path).expanduser().resolve())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Persist campaign-scoped rollback and runbook release safety evidence"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--phase", default="PHASE6")
    parser.add_argument("--runbook", default="RUNBOOK.md")
    parser.add_argument("--rollback-max-age-sec", type=float, default=900.0)
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()
    if args.rollback_max_age_sec <= 0:
        parser.error("--rollback-max-age-sec must be > 0")

    engine = create_engine(_database_url(args.db), future=True)
    try:
        result = persist_campaign_release_safety_evidence(
            engine,
            campaign_id=args.campaign_id,
            phase=args.phase,
            runbook_path=args.runbook,
            rollback_max_age_sec=args.rollback_max_age_sec,
            repo_path=args.repo,
        )
    finally:
        engine.dispose()

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
