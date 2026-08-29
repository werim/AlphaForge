from __future__ import annotations
import sqlite3
from pathlib import Path
from .contracts import CONTRACTS,OWNER_MAP,WRITER_READER_MATRIX,UNIQUE_REQUIREMENTS,INDEX_REQUIREMENTS,CURRENT_REVISION, INTEGER_COLUMNS, REAL_COLUMNS, TABLE, TEXT_COLUMNS, UNIQUE_IDENTITIES, WRITER_COLUMNS
from .identity import collect_identity
from .inspector import inspect_database
from .targets import resolve_database_targets
from .orm_audit import audit_orm
from .dialect import audit_runtime_sql

BLOCKS = {
    "DATABASE_TARGET_CONFLICT": ("repair", "paper_certification"),
    "PAPER_DIALECT_UNSUPPORTED": ("repair", "paper_certification"),
    "DATABASE_IDENTITY_UNVERIFIED": ("repair", "paper_certification"),
    "SQLITE_INTEGRITY_FAILURE": ("repair", "paper_certification", "migration"),
    "LIFECYCLE_UNSUPPORTED_SCHEMA_OBJECT": ("repair", "paper_certification", "migration"),
    "LIFECYCLE_DUPLICATE_EVENT_ID": ("repair", "paper_certification", "migration"),
    "LIFECYCLE_DUPLICATE_SIGNAL_TIME_STATE": ("repair", "paper_certification", "migration"),
    "EXPOSURE_SOURCE_AMBIGUOUS": ("paper_certification", "exposure_operations"),
    "EXPOSURE_MULTIPLE_ACTIVE_SOURCES": ("paper_certification", "exposure_operations"),
    "EXPOSURE_SCHEMA_UNSUPPORTED": ("paper_certification", "exposure_operations"),
    "ADAPTIVE_SCHEMA_GENERATION_CONFLICT": ("paper_certification",),
    "ORM_ALEMBIC_CONTRACT_MISMATCH": ("alembic_autogenerate",),
    "MULTIPLE_SCHEMA_OWNERS": ("alembic_autogenerate", "schema_consolidation"),
    "INCOMPATIBLE_OWNER_CONTRACTS": ("alembic_autogenerate", "schema_consolidation"),
}
PAPER_CONTRACT_CODES = {"LIFECYCLE_TABLE_MISSING", "LIFECYCLE_PK_NOT_SQLITE_ROWID_COMPATIBLE",
    "LIFECYCLE_REQUIRED_COLUMN_MISSING", "LIFECYCLE_TYPE_MISMATCH", "LIFECYCLE_NOT_NULL_WRITER_CONFLICT",
    "LIFECYCLE_CONFLICT_TARGET_MISSING", "RUNTIME_REQUIRED_TABLE_MISSING", "RUNTIME_REQUIRED_COLUMN_MISSING",
    "SQLITE_PK_SEMANTIC_MISMATCH", "MISSING_UNIQUE_CONSTRAINT", "NOT_NULL_WRITER_CONFLICT",
    "SQLITE_JSON1_UNAVAILABLE"}

def issue(code, severity, *, expected, observed, repair, evidence, action, table=None, column=None, blocks=None):
    gates = tuple(blocks if blocks is not None else BLOCKS.get(code, ("paper_certification",) if code in PAPER_CONTRACT_CODES else ()))
    return {"code": code, "severity": severity, "table": table, "column": column, "expected": expected,
            "observed": observed, "repair_classification": repair, "blocks": list(gates),
            "evidence": evidence, "recommended_action": action}

def blockers(report: dict, operation: str) -> list[dict]:
    return [finding for finding in report.get("issues", ()) if operation in finding.get("blocks", ())]

def _early(identity, targets, issues):
    return {"status":"BLOCKED","identity":identity,"database_target_resolution":targets,
            "dialect":{"target":None,"paper_runtime":"SQLITE_ONLY","supported_for_certification":False,"runtime_sql_surfaces":{}},
            "SQLite_features":{},"migration_state":{},"schema_family":"UNKNOWN","schema_contracts":{},
            "schema_ownership":OWNER_MAP,"ORM_alignment":{"tables_audited":[],"mismatches":[],"autogenerate_safe":False},
            "lifecycle":None,"exposure":{"classification":"UNKNOWN_EXPOSURE","unknown_is_zero":False},
            "runtime":{},"burnin":{},"campaign":{},"adaptive_expectancy":{},"readiness_release":{},
            "writer_compatibility":{},"runtime_blockers":issues,"repository_findings":[],"unsafe_data":issues,"issues":issues,"recommended_repairs":[],"inspection":None}

def diagnose(path: Path) -> dict:
    identity = collect_identity(path); issues=[]; targets=resolve_database_targets(path)
    if targets["conflict"]: issues.append(issue("DATABASE_TARGET_CONFLICT","CRITICAL",expected="one canonical target",observed=targets,repair="MANUAL_REVIEW",evidence=targets,action="remove conflicting targets"))
    unsupported=sorted(d for d in targets["dialects"] if d != "sqlite")
    if unsupported: issues.append(issue("PAPER_DIALECT_UNSUPPORTED","CRITICAL",expected="sqlite",observed=unsupported,repair="MANUAL_REVIEW",evidence=targets,action="select a SQLite target for PAPER certification"))
    if not identity["exists"]:
        issues.append(issue("DATABASE_IDENTITY_UNVERIFIED", "CRITICAL", expected="existing SQLite file", observed="missing", repair="MANUAL_REVIEW", evidence=identity, action="verify --db path"))
        return _early(identity,targets,issues)
    try: inspection=inspect_database(path)
    except Exception as exc:
        issues.append(issue("DATABASE_IDENTITY_UNVERIFIED", "CRITICAL", expected="readable SQLite database", observed=repr(exc), repair="MANUAL_REVIEW", evidence=identity, action="verify database identity and permissions"))
        return _early(identity,targets,issues)
    if inspection["integrity"] != ["ok"]:
        issues.append(issue("SQLITE_INTEGRITY_FAILURE","CRITICAL",expected=["ok"],observed=inspection["integrity"],repair="MANUAL_REVIEW",evidence=inspection["integrity"],action="recover SQLite database before repair"))
    revs=inspection["alembic_revisions"]
    if revs and revs != [CURRENT_REVISION]:
        issues.append(issue("ALEMBIC_REVISION_UNKNOWN","ERROR",expected=CURRENT_REVISION,observed=revs,repair="SAFE_AUTO_REPAIR",evidence=revs,action="plan and apply migration to head"))
    if TABLE not in inspection["tables"]:
        issues.append(issue("LIFECYCLE_TABLE_MISSING","CRITICAL",expected=TABLE,observed=None,repair="MANUAL_REVIEW",evidence=inspection["tables"],action="restore or migrate the correct database",table=TABLE))
    else:
        cols={c["name"]:c for c in inspection["columns"]}; sql=(inspection["create_sql"] or "").upper()
        understood_columns={"id",*TEXT_COLUMNS,*REAL_COLUMNS,*INTEGER_COLUMNS}
        unknown_columns=sorted(set(cols)-understood_columns)
        lifecycle_objects=[o for o in inspection["objects"] if o["table"] == TABLE]
        unknown_objects=[o for o in lifecycle_objects if
                         (o["type"] == "trigger" or (o["type"] == "index" and
                          not o["name"].startswith("sqlite_autoindex_") and
                          o["name"] not in {"ux_trade_lifecycle_event_id","ux_lifecycle_signal_event_ts_state"}))]
        if unknown_columns or unknown_objects:
            issues.append(issue("LIFECYCLE_UNSUPPORTED_SCHEMA_OBJECT","CRITICAL",expected="explicitly understood lifecycle schema",observed={"columns":unknown_columns,"objects":unknown_objects},repair="MANUAL_REVIEW",evidence={"columns":unknown_columns,"objects":unknown_objects},action="review and explicitly preserve the deployed schema object before rebuild",table=TABLE))
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
    contracts={}
    for name,c in CONTRACTS.items():
        schema=inspection["schemas"].get(name); present={x["name"] for x in schema["columns"]} if schema else set(); missing=sorted(set(c.columns)-present)
        contracts[name]={"surface":c.surface,"requirement":c.requirement,"role":c.role,"present":bool(schema),"compatible":bool(schema) and not missing,"missing_columns":missing}
        if c.requirement=="REQUIRED" and not schema: issues.append(issue("RUNTIME_REQUIRED_TABLE_MISSING","CRITICAL",expected="present",observed=None,repair="REBUILD_REQUIRED",evidence=name,action="migrate explicitly",table=name))
        elif schema and missing:
            issues.append(issue("RUNTIME_REQUIRED_COLUMN_MISSING","ERROR",expected=c.columns,observed=sorted(present),repair="REBUILD_REQUIRED",evidence=missing,action="migrate explicitly",table=name))
            if len(c.owners)>1: issues.append(issue("INCOMPATIBLE_OWNER_CONTRACTS","ERROR",expected=OWNER_MAP[name],observed=missing,repair="MANUAL_REVIEW",evidence=OWNER_MAP[name],action="compare independent definitions",table=name))
        if schema:
            idcol=next((x for x in schema["columns"] if x["name"]=="id"),None)
            if "id" in c.columns and (not idcol or str(idcol["type"]).upper()!="INTEGER" or idcol["pk"]!=1): issues.append(issue("SQLITE_PK_SEMANTIC_MISMATCH","ERROR",expected="INTEGER PRIMARY KEY rowid semantics",observed=idcol,repair="REBUILD_REQUIRED",evidence=schema["sql"],action="rebuild explicitly",table=name,column="id"))
            unique={tuple(i["columns"]) for i in schema["indexes"] if i["unique"]}
            indexed={tuple(i["columns"]) for i in schema["indexes"]}
            for wanted in UNIQUE_REQUIREMENTS.get(name,()):
                if wanted not in unique: issues.append(issue("MISSING_UNIQUE_CONSTRAINT","ERROR",expected=wanted,observed=sorted(unique),repair="REBUILD_REQUIRED",evidence=schema["indexes"],action="review duplicates before explicit migration",table=name))
            for wanted in INDEX_REQUIREMENTS.get(name,()):
                if not any(tuple(cols[:len(wanted)])==wanted for cols in indexed): issues.append(issue("MISSING_REQUIRED_INDEX","ERROR",expected=wanted,observed=sorted(indexed),repair="SAFE_AUTO_REPAIR",evidence=schema["indexes"],action="create non-destructive index",table=name))

    features={"capabilities":{"integrity":inspection["integrity"],"json_functions":inspection["json1"],"foreign_keys_supported":True,"sqlite_version":sqlite3.sqlite_version},"doctor_connection_observation":{"foreign_keys_enabled":inspection["foreign_keys_enabled"],"journal_mode":inspection["journal_mode"],"busy_timeout_ms":inspection["busy_timeout_ms"],"certification_evidence":False},"runtime_configuration_contract":{"foreign_keys":"ON","journal_mode":"WAL","busy_timeout_ms":30000,"source":"src/alphaforge/persistence.py:init_db connection hook"}}
    if not inspection["json1"]: issues.append(issue("SQLITE_JSON1_UNAVAILABLE","CRITICAL",expected=True,observed=False,repair="MANUAL_REVIEW",evidence=features["capabilities"],action="install JSON-capable SQLite"))
    schemas=inspection["schemas"]; domain=all(n in schemas for n in ("positions","orders")); adapter=all(n in schemas for n in ("runtime_positions","runtime_orders")); dr=sum(schemas.get(n,{}).get("row_count",0) for n in ("positions","orders")); ar=sum(schemas.get(n,{}).get("row_count",0) for n in ("runtime_positions","runtime_orders"))
    exposure={"classification":"CONFLICTING_EXPOSURE" if domain and adapter and dr and ar else "ADAPTER_EXPOSURE" if domain and adapter else "RUNTIME_EXPOSURE" if adapter else "LEGACY_DOMAIN_EXPOSURE" if domain else "UNKNOWN_EXPOSURE","domain_rows":dr,"adapter_rows":ar,"multiple_active":bool(dr and ar),"unknown_is_zero":False}
    if exposure["classification"] in ("UNKNOWN_EXPOSURE","CONFLICTING_EXPOSURE"): issues.append(issue("EXPOSURE_SOURCE_AMBIGUOUS","CRITICAL",expected="one exposure source",observed=exposure,repair="MANUAL_REVIEW",evidence=exposure,action="identify authority; never infer zero"))
    if exposure["multiple_active"]: issues.append(issue("EXPOSURE_MULTIPLE_ACTIVE_SOURCES","CRITICAL",expected="one populated source",observed=exposure,repair="MANUAL_REVIEW",evidence=exposure,action="reconcile externally"))
    current=[n for n in ("adaptive_stats","setup_expectancy_stats","regime_expectancy_stats","symbol_expectancy_stats") if n in schemas]; legacy=[n for n in ("adaptive_threshold_stats","expectancy_stats") if n in schemas]; cr=sum(schemas[n]["row_count"] for n in current); lr=sum(schemas[n]["row_count"] for n in legacy); adaptive={"current_tables":current,"historical_tables":legacy,"current_rows":cr,"historical_rows":lr,"conflict":bool(cr and lr)}
    if adaptive["conflict"]: issues.append(issue("ADAPTIVE_SCHEMA_GENERATION_CONFLICT","CRITICAL",expected="one active generation",observed=adaptive,repair="MANUAL_REVIEW",evidence=adaptive,action="preserve and prove ownership"))
    for name, ownership in OWNER_MAP.items():
        if name in schemas and len(ownership["owners"]) > 1:
            issues.append(issue("MULTIPLE_SCHEMA_OWNERS","WARNING",expected="one canonical schema owner",observed=ownership,repair="MANUAL_REVIEW",evidence=ownership,action="audit owner contracts before autogenerate or consolidation",table=name))
    orm=audit_orm(schemas)
    for mismatch in orm["mismatches"]:
        issues.append(issue("ORM_ALEMBIC_CONTRACT_MISMATCH","ERROR",expected="ORM metadata matches deployed/Alembic schema",observed=mismatch,repair="MANUAL_REVIEW",evidence=mismatch,action="do not run alembic autogenerate",table=mismatch["table"]))
        if mismatch["table"] in OWNER_MAP and len(OWNER_MAP[mismatch["table"]]["owners"])>1: issues.append(issue("INCOMPATIBLE_OWNER_CONTRACTS","ERROR",expected=OWNER_MAP[mismatch["table"]],observed=mismatch,repair="MANUAL_REVIEW",evidence=mismatch,action="compare independent owner contracts",table=mismatch["table"]))
    dialect_surfaces=audit_runtime_sql()
    family="ALEMBIC_HEAD" if inspection["alembic_revisions"]==[CURRENT_REVISION] else "LEGACY_ALEMBIC" if inspection["alembic_revisions"] else "INIT_DB" if TABLE in inspection["tables"] else "UNKNOWN"
    paper_blockers=[x for x in issues if "paper_certification" in x["blocks"]]
    return {"status":"HEALTHY" if not paper_blockers else "BLOCKED","identity":identity,"database_target_resolution":targets,"dialect":{"target":"sqlite","paper_runtime":"SQLITE_ONLY","supported_for_certification":True,"runtime_sql_surfaces":dialect_surfaces},"SQLite_features":features,"migration_state":{"alembic_revisions":inspection["alembic_revisions"],"schema_migrations":inspection["schema_migrations"]},"schema_family":family,"schema_contracts":contracts,"schema_ownership":OWNER_MAP,"ORM_alignment":orm,"lifecycle":contracts.get(TABLE),"exposure":exposure,"runtime":{n:v for n,v in contracts.items() if v["surface"]=="runtime"},"burnin":{n:v for n,v in contracts.items() if v["surface"]=="burnin"},"campaign":{n:v for n,v in contracts.items() if v["surface"]=="campaign"},"adaptive_expectancy":adaptive,"readiness_release":{n:v for n,v in contracts.items() if v["surface"]=="readiness_release"},"writer_compatibility":{w:{"tables":d,"compatible":all(contracts.get(t,{}).get("compatible",False) for t in d)} for w,d in WRITER_READER_MATRIX.items()},"runtime_blockers":paper_blockers,"repository_findings":[x for x in issues if "paper_certification" not in x["blocks"]],"unsafe_data":[x for x in issues if x["repair_classification"]=="MANUAL_REVIEW"],"issues":issues,"recommended_repairs":[x for x in issues if x["repair_classification"]=="SAFE_AUTO_REPAIR"],"inspection":inspection}
