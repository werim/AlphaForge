from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine, text

from alphaforge.release_gates import run_canary_mutation_trap_validation


def _database_url(path: str) -> str:
    return "sqlite+pysqlite:///" + str(Path(path).expanduser().resolve())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run isolated non-mutating canary guard validation and persist measured release evidence"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--phase", default="PHASE6")
    args = parser.parse_args()

    engine = create_engine(_database_url(args.db), future=True)
    try:
        with engine.connect() as conn:
            campaign = conn.execute(text("""
                SELECT campaign_id, release_id, git_commit
                FROM burnin_campaigns
                WHERE campaign_id=:campaign_id
            """), {"campaign_id": args.campaign_id}).mappings().first()
        if campaign is None:
            raise SystemExit("CAMPAIGN_NOT_FOUND")

        result = run_canary_mutation_trap_validation(
            engine,
            release_id=str(campaign["release_id"]),
            phase=args.phase,
        )
        output = {
            "campaign_id": str(campaign["campaign_id"]),
            "release_id": str(campaign["release_id"]),
            "campaign_git_commit": str(campaign["git_commit"] or ""),
            "phase": args.phase,
            "status": result["status"],
            "event_id": result["event_id"],
            "evidence": result["evidence"],
        }
        print(json.dumps(output, indent=2, sort_keys=True, default=str))
        raise SystemExit(0 if result["status"] == "PASS" else 2)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
