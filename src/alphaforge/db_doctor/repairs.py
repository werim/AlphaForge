from __future__ import annotations
from pathlib import Path
from alembic import command
from alembic.config import Config
from .backup import create_backup
from .diagnostics import diagnose

def repair(path: Path) -> dict:
    before=diagnose(path)
    if any(i["repair_classification"] == "MANUAL_REVIEW" for i in before["issues"]):
        return {"status":"BLOCKED_MANUAL_REVIEW", "before":before, "backup_path":None}
    if not before["issues"]: return {"status":"NO_ACTION", "before":before, "after":before, "backup_path":None}
    backup=create_backup(path)
    root=Path(__file__).resolve().parents[3]
    config=Config(str(root / "alembic.ini")); config.set_main_option("script_location",str(root / "alembic")); config.set_main_option("sqlalchemy.url",f"sqlite+pysqlite:///{path.resolve()}")
    try: command.upgrade(config,"head")
    except Exception as exc:
        return {"status":"REPAIR_FAILED", "before":before, "backup_path":str(backup), "error":repr(exc), "recovery":"source retained; validated backup available"}
    after=diagnose(path)
    return {"status":"REPAIRED" if not after["issues"] else "VERIFICATION_FAILED", "before":before,"after":after,"backup_path":str(backup)}

