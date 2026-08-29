from __future__ import annotations
import sqlite3
from pathlib import Path

def inspect_database(path: Path) -> dict:
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        objects = [dict(zip(("type", "name", "table", "sql"), row)) for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('table','index','trigger','view') ORDER BY type,name")]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = [dict(zip(("cid", "name", "type", "notnull", "default", "pk"), r)) for r in conn.execute("PRAGMA table_info(trade_lifecycle_events)")] if "trade_lifecycle_events" in tables else []
        revisions = [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")] if "alembic_version" in tables else []
        migrations = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] if "schema_migrations" in tables else []
        schemas={}
        for table in tables:
            q=table.replace('"','""')
            indexes=[dict(zip(("seq","name","unique","origin","partial"),r)) for r in conn.execute(f'PRAGMA index_list("{q}")')]
            for idx in indexes: idx["columns"]=[r[2] for r in conn.execute(f'PRAGMA index_info("{idx["name"]}")')]
            schemas[table]={"columns":[dict(zip(("cid","name","type","notnull","default","pk"),r)) for r in conn.execute(f'PRAGMA table_info("{q}")')],"indexes":indexes,"foreign_keys":[tuple(r) for r in conn.execute(f'PRAGMA foreign_key_list("{q}")')],"row_count":conn.execute(f'SELECT COUNT(*) FROM "{q}"').fetchone()[0],"sql":next((o["sql"] for o in objects if o["type"]=="table" and o["name"]==table),None)}
        return {"integrity": integrity, "objects": objects, "tables": sorted(tables), "schemas":schemas, "columns": columns,
                "create_sql": next((o["sql"] for o in objects if o["type"] == "table" and o["name"] == "trade_lifecycle_events"), None),
                "alembic_revisions": revisions, "schema_migrations": migrations,
                "foreign_keys_enabled":bool(conn.execute("PRAGMA foreign_keys").fetchone()[0]),"journal_mode":conn.execute("PRAGMA journal_mode").fetchone()[0],"busy_timeout_ms":conn.execute("PRAGMA busy_timeout").fetchone()[0],"json1":conn.execute("SELECT json_extract('{\"probe\":1}','$.probe')").fetchone()[0]==1}
    finally: conn.close()
