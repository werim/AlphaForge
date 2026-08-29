from __future__ import annotations
import sqlite3
from pathlib import Path
from .contracts import CURRENT_REVISION, INTEGER_COLUMNS, REAL_COLUMNS, TABLE, TEXT_COLUMNS, UNIQUE_IDENTITIES, WRITER_COLUMNS
from .identity import collect_identity
from .inspector import inspect_database

def issue(code, severity, *, expected, observed, repair, evidence, action, table=None, column=None):
    return {"code": code, "severity": severity, "table": table, "column": column, "expected": expected,
            "observed": observed, "repair_classification": repair, "evidence": evidence, "recommended_action": action}

def diagnose(path: Path) -> dict:
    identity = collect_identity(path); issues=[]
    if not identity["exists"]:
        issues.append(issue("DATABASE_IDENTITY_UNVERIFIED", "CRITICAL", expected="existing SQLite file", observed="missing", repair="MANUAL_REVIEW", evidence=identity, action="verify --db path"))
        return {"status":"BLOCKED", "identity":identity, "schema_family":"UNKNOWN", "inspection":None, "issues":issues}
    try: inspection=inspect_database(path)
    except Exception as exc:
        issues.append(issue("DATABASE_IDENTITY_UNVERIFIED", "CRITICAL", expected="readable SQLite database", observed=repr(exc), repair="MANUAL_REVIEW", evidence=identity, action="verify database identity and permissions"))
        return {"status":"BLOCKED", "identity":identity, "schema_family":"UNKNOWN", "inspection":None, "issues":issues}
    if inspection["integrity"] != ["ok"]:
        issues.append(issue("SQLITE_INTEGRITY_FAILURE","CRITICAL",expected=["ok"],observed=inspection["integrity"],repair="MANUAL_REVIEW",evidence=inspection["integrity"],action="recover SQLite database before repair"))
    revs=inspection["alembic_revisions"]
    if revs and revs != [CURRENT_REVISION]:
        issues.append(issue("ALEMBIC_REVISION_UNKNOWN","ERROR",expected=CURRENT_REVISION,observed=revs,repair="SAFE_AUTO_REPAIR",evidence=revs,action="plan and apply migration to head"))
    if TABLE not in inspection["tables"]:
        issues.append(issue("LIFECYCLE_TABLE_MISSING","CRITICAL",expected=TABLE,observed=None,repair="MANUAL_REVIEW",evidence=inspection["tables"],action="restore or migrate the correct database",table=TABLE))
    else:
        cols={c["name"]:c for c in inspection["columns"]}; sql=(inspection["create_sql"] or "").upper()
        idcol=cols.get("id")
        if not idcol or idcol["type"].upper() != "INTEGER" or idcol["pk"] != 1:
            issues.append(issue("LIFECYCLE_PK_NOT_SQLITE_ROWID_COMPATIBLE","CRITICAL",expected="INTEGER PRIMARY KEY AUTOINCREMENT",observed=idcol,repair="REBUILD_REQUIRED",evidence=inspection["create_sql"],action="back up and rebuild lifecycle table",table=TABLE,column="id"))
        for name in sorted(WRITER_COLUMNS):
            if name not in cols:
                issues.append(issue("LIFECYCLE_REQUIRED_COLUMN_MISSING","ERROR",expected="column present",observed=None,repair="REBUILD_REQUIRED",evidence=sorted(cols),action="rebuild to canonical contract",table=TABLE,column=name))
            else:
                expected = "TEXT" if name in TEXT_COLUMNS else ("REAL" if name in REAL_COLUMNS else "INTEGER")
                observed = str(cols[name]["type"] or "").upper()
                # Compare declared types rather than broad SQLite affinity: the
                # contract is deliberately deterministic and separately checks PK rowid semantics.
                if observed != expected:
                    issues.append(issue("LIFECYCLE_TYPE_MISMATCH","ERROR",expected=expected,observed=observed,repair="REBUILD_REQUIRED",evidence=cols[name],action="rebuild to canonical declared type",table=TABLE,column=name))
        for name,c in cols.items():
            if c["notnull"] and name not in WRITER_COLUMNS and name != "id":
                issues.append(issue("LIFECYCLE_NOT_NULL_WRITER_CONFLICT","CRITICAL",expected="nullable legacy column",observed=c,repair="REBUILD_REQUIRED",evidence=c,action="rebuild without obsolete writer requirement",table=TABLE,column=name))
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
            targets={tuple(x[2] for x in conn.execute(f'PRAGMA index_info("{r[1]}")')) for r in conn.execute(f"PRAGMA index_list({TABLE})") if r[2]}
            for ident in UNIQUE_IDENTITIES:
                if ident not in targets:
                    issues.append(issue("LIFECYCLE_CONFLICT_TARGET_MISSING","ERROR",expected=ident,observed=sorted(targets),repair="REBUILD_REQUIRED",evidence=sorted(targets),action="create canonical unique target",table=TABLE))
                where=" AND ".join(f'"{c}" IS NOT NULL' for c in ident); names=",".join(ident)
                duplicates=conn.execute(f"SELECT {names},COUNT(*) FROM {TABLE} WHERE {where} GROUP BY {names} HAVING COUNT(*)>1 LIMIT 20").fetchall()
                if duplicates:
                    code="LIFECYCLE_DUPLICATE_EVENT_ID" if ident == ("event_id",) else "LIFECYCLE_DUPLICATE_SIGNAL_TIME_STATE"
                    issues.append(issue(code,"CRITICAL",expected="unique identity",observed=duplicates,repair="MANUAL_REVIEW",evidence=duplicates,action="review duplicate evidence; no rows will be deleted",table=TABLE))
    family="CANONICAL" if not issues else ("LEGACY_ALEMBIC" if TABLE in inspection["tables"] else "UNKNOWN")
    return {"status":"HEALTHY" if not issues else "BLOCKED", "identity":identity,"schema_family":family,"inspection":inspection,"issues":issues}
