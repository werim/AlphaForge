from __future__ import annotations
import sqlite3
from pathlib import Path

def inspect_database(path: Path) -> dict:
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        objects = [dict(zip(("type", "name", "table", "sql"), row)) for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('table','index','trigger') ORDER BY type,name")]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = [dict(zip(("cid", "name", "type", "notnull", "default", "pk"), r)) for r in conn.execute("PRAGMA table_info(trade_lifecycle_events)")] if "trade_lifecycle_events" in tables else []
        revisions = [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")] if "alembic_version" in tables else []
        migrations = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] if "schema_migrations" in tables else []
        return {"integrity": integrity, "objects": objects, "tables": sorted(tables), "columns": columns,
                "create_sql": next((o["sql"] for o in objects if o["type"] == "table" and o["name"] == "trade_lifecycle_events"), None),
                "alembic_revisions": revisions, "schema_migrations": migrations}
    finally: conn.close()

