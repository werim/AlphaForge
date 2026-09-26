from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alphaforge.release_gates import run_canary_mutation_trap_validation
from alphaforge.release_identity import GitRunner, require_campaign_checkout


def _database_url(path: str) -> str:
    return "sqlite+pysqlite:///" + str(Path(path).expanduser().resolve())


def persist_campaign_canary_evidence(
    engine: Engine,
    *,
    campaign_id: str,
    phase: str = "PHASE6",
    repo_path: str | Path = ".",
    git_runner: GitRunner | None = None,
) -> dict[str, object]:
    with engine.connect() as conn:
        campaign = conn.execute(text("""
            SELECT campaign_id, release_id, git_commit
            FROM burnin_campaigns
            WHERE campaign_id=:campaign_id
        """), {"campaign_id": campaign_id}).mappings().first()
    if campaign is None:
        raise ValueError("CAMPAIGN_NOT_FOUND")

    campaign_git_commit = str(campaign["git_commit"] or "")
    identity = require_campaign_checkout(
        campaign_git_commit,
        repo_path=repo_path,
        git_runner=git_runner,
    )
    result = run_canary_mutation_trap_validation(
        engine,
        release_id=str(campaign["release_id"]),
        phase=phase,
        git_commit=campaign_git_commit,
    )
    return {
        "campaign_id": str(campaign["campaign_id"]),
        "release_id": str(campaign["release_id"]),
        "campaign_git_commit": campaign_git_commit,
        "checkout_git_commit": str(identity["git_commit"]),
        "phase": phase,
        "status": result["status"],
        "event_id": result["event_id"],
        "evidence": result["evidence"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run isolated non-mutating canary guard validation and persist measured release evidence"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--phase", default="PHASE6")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()

    engine = create_engine(_database_url(args.db), future=True)
    try:
        result = persist_campaign_canary_evidence(
            engine,
            campaign_id=args.campaign_id,
            phase=args.phase,
            repo_path=args.repo,
        )
    finally:
        engine.dispose()

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
