from __future__ import annotations
from pathlib import Path
from alembic import command
from alembic.config import Config
from .backup import create_backup
from .diagnostics import blockers, diagnose
from .writer_probes import run_writer_probes

def repair(path: Path) -> dict:
    before=diagnose(path)
    repair_blockers=blockers(before,"repair")
    if repair_blockers:
        return {"status":"BLOCKED_MANUAL_REVIEW", "before":before, "backup_path":None}
    repairable=[i for i in before["issues"] if i["repair_classification"] in {"SAFE_AUTO_REPAIR","REBUILD_REQUIRED"}]
    if not repairable: return {"status":"NO_ACTION", "before":before, "after":before, "backup_path":None}
    backup=create_backup(path)
    root=Path(__file__).resolve().parents[3]
    config=Config(str(root / "alembic.ini")); config.set_main_option("script_location",str(root / "alembic")); config.set_main_option("sqlalchemy.url",f"sqlite+pysqlite:///{path.resolve()}")
    try: command.upgrade(config,"head")
    except Exception as exc:
        return {"status":"REPAIR_FAILED", "before":before, "backup_path":str(backup), "error":repr(exc), "recovery":"source retained; validated backup available"}
    after=diagnose(path)
    post_blockers=blockers(after,"paper_certification")
    probes = None if post_blockers else run_writer_probes(path)
    verified = not post_blockers and bool(probes and probes["passed"])
    result = {"status":"REPAIRED" if verified else "VERIFICATION_FAILED", "before":before,
              "after":after,"writer_probes":probes,"backup_path":str(backup)}
    if not verified:
        result["recommended_action"] = "retain the validated backup and review structural/writer evidence before retrying or restoring"
    return result
