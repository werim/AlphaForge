from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

def snapshot_database(source_path: Path, destination_path: Path) -> Path:
    """Create a transactionally consistent SQLite image, including committed WAL."""
    if not source_path.is_file():
        raise RuntimeError("snapshot blocked: source database does not exist")
    if destination_path.exists():
        raise RuntimeError("snapshot blocked: destination already exists")
    try:
        with sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True) as source, sqlite3.connect(destination_path) as target:
            source.backup(target)
        if not destination_path.is_file() or destination_path.stat().st_size == 0:
            raise RuntimeError("snapshot validation failed")
        with sqlite3.connect(destination_path) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("snapshot integrity validation failed")
    except Exception:
        destination_path.unlink(missing_ok=True)
        raise
    return destination_path


def create_backup(path: Path) -> Path:
    backup = path.with_name(f"{path.name}.doctor-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.bak")
    return snapshot_database(path, backup)
