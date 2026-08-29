from __future__ import annotations
from pathlib import Path
from .diagnostics import diagnose, issue
from .writer_probes import run_writer_probes

def certify(path: Path) -> dict:
    diagnosis=diagnose(path)
    if diagnosis["issues"]: return {"status":"NOT_CERTIFIED","diagnosis":diagnosis,"writer_probes":None}
    probes=run_writer_probes(path)
    if not probes["passed"]:
        diagnosis["issues"].append(issue("WRITER_PROBE_FAILED","CRITICAL",expected="all real writers succeed",observed=probes,repair="MANUAL_REVIEW",evidence=probes,action="inspect writer/schema failure")); diagnosis["status"]="BLOCKED"
    return {"status":"DATABASE_CERTIFIED" if probes["passed"] else "NOT_CERTIFIED","diagnosis":diagnosis,"writer_probes":probes}

