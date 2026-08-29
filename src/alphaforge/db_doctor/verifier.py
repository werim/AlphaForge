from __future__ import annotations
from pathlib import Path
from .diagnostics import blockers, diagnose, issue
from .writer_probes import run_writer_probes

def certify(path: Path) -> dict:
    diagnosis=diagnose(path)
    certification_blockers=blockers(diagnosis,"paper_certification")
    repository_audit={"status":"HEALTHY" if not diagnosis.get("repository_findings") else "FINDINGS",
                      "findings":diagnosis.get("repository_findings",[]),
                      "autogenerate_safe":diagnosis.get("ORM_alignment",{}).get("autogenerate_safe",False)}
    if certification_blockers:
        return {"status":"NOT_CERTIFIED","runtime_certification":{"status":"NOT_CERTIFIED","blockers":certification_blockers},"repository_audit":repository_audit,"diagnosis":diagnosis,"writer_probes":None}
    probes=run_writer_probes(path)
    if not probes["passed"]:
        finding=issue("WRITER_PROBE_FAILED","CRITICAL",expected="all real writers succeed",observed=probes,repair="MANUAL_REVIEW",evidence=probes,action="inspect writer/schema failure",blocks=("paper_certification",))
        diagnosis["issues"].append(finding); diagnosis["runtime_blockers"].append(finding); diagnosis["status"]="BLOCKED"
    status="DATABASE_CERTIFIED" if probes["passed"] else "NOT_CERTIFIED"
    return {"status":status,"runtime_certification":{"status":status,"blockers":[] if probes["passed"] else [diagnosis["issues"][-1]]},"repository_audit":repository_audit,"diagnosis":diagnosis,"writer_probes":probes}
