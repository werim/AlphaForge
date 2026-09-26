from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine, text

from alphaforge.release_gates import (
    MAX_OPERATOR_ACK_TTL_MINUTES,
    latest_valid_operator_ack,
    persist_operator_ack,
    required_operator_ack_text,
)


def _database_url(path: str) -> str:
    return "sqlite+pysqlite:///" + str(Path(path).expanduser().resolve())


def main() -> None:
    parser = argparse.ArgumentParser(description="Record explicit release-scoped AlphaForge operator acknowledgement")
    parser.add_argument("--db", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--phase", default="PHASE6")
    parser.add_argument("--operator-id", default="operator")
    parser.add_argument("--ttl-minutes", type=int, default=MAX_OPERATOR_ACK_TTL_MINUTES)
    parser.add_argument("--acknowledgement-text")
    parser.add_argument("--show-required-text", action="store_true")
    args = parser.parse_args()

    engine = create_engine(_database_url(args.db), future=True)
    try:
        with engine.connect() as conn:
            campaign = conn.execute(text("""
                SELECT release_id FROM burnin_campaigns WHERE campaign_id=:campaign_id
            """), {"campaign_id": args.campaign_id}).mappings().first()
        if campaign is None:
            raise SystemExit("CAMPAIGN_NOT_FOUND")
        release_id = str(campaign["release_id"])
        required = required_operator_ack_text(release_id)
        if args.show_required_text:
            print(required)
            return
        if not args.acknowledgement_text:
            parser.error("--acknowledgement-text is required unless --show-required-text is used")

        row = persist_operator_ack(
            engine,
            release_id=release_id,
            phase=args.phase,
            acknowledgement_text=args.acknowledgement_text,
            operator_id=args.operator_id,
            ttl_minutes=args.ttl_minutes,
            evidence={"source": "release_operator_ack_cli", "campaign_id": args.campaign_id},
        )
        valid = latest_valid_operator_ack(engine, release_id=release_id, phase=args.phase)
        output = {
            "campaign_id": args.campaign_id,
            "release_id": release_id,
            "phase": args.phase,
            "ack_id": row["ack_id"],
            "operator_id": args.operator_id,
            "acknowledged_at": row["acknowledged_at"],
            "valid_until": row["valid_until"],
            "valid": valid is not None,
            "blocker_reason": row.get("blocker_reason"),
        }
        print(json.dumps(output, indent=2, sort_keys=True))
        raise SystemExit(0 if valid is not None else 2)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
