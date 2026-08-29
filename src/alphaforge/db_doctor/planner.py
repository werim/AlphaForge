from __future__ import annotations
from pathlib import Path
from .diagnostics import blockers, diagnose

def plan(path: Path) -> dict:
    report=diagnose(path)
    manual=blockers(report,"repair")
    steps=[] if not report["issues"] else ["create and validate SQLite online backup", "run Alembic upgrade to canonical head", "re-diagnose", "run actual persistence writer probes"]
    return {"status":"BLOCKED_MANUAL_REVIEW" if manual else ("NO_ACTION" if not steps else "READY"), "diagnosis":report, "steps":steps}
