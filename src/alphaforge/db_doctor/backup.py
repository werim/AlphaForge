from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

def create_backup(path: Path) -> Path:
    if not path.is_file(): raise RuntimeError("backup blocked: source database does not exist")
    backup = path.with_name(f"{path.name}.doctor-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.bak")
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        if not backup.is_file() or backup.stat().st_size == 0: raise RuntimeError("backup validation failed")
        with sqlite3.connect(backup) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",): raise RuntimeError("backup integrity validation failed")
    except Exception:
        backup.unlink(missing_ok=True)
        raise
    return backup

