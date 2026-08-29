"""Rebuild the SQLite lifecycle table with rowid-compatible PK semantics.

Revision ID: 0008_database_doctor_lifecycle_contract
Revises: 0007_repair_runtime_lifecycle_schema
"""
from alembic import op
import hashlib
import json
import sqlalchemy as sa

revision = "0008_database_doctor_lifecycle_contract"
down_revision = "0007_repair_runtime_lifecycle_schema"
branch_labels = None
depends_on = None

TEXT_COLUMNS = (
    "event_id", "signal_id", "order_id", "symbol", "mode", "trade_id",
    "lifecycle_state", "state", "event_type", "payload", "decision",
    "reject_reason", "expectancy_bucket", "execution_ctx", "event_ts",
    "created_at", "cancel_reason", "lifecycle_id", "failure_reason",
    "reconciliation_reason", "incident_payload", "event_payload",
)
REAL_COLUMNS = ("score", "rr", "effective_rr")
INTEGER_COLUMNS = ("execution_ctx_missing", "lifecycle_seq", "order_intent_id")
KNOWN_COLUMNS = {"id", *TEXT_COLUMNS, *REAL_COLUMNS, *INTEGER_COLUMNS}
KNOWN_INDEXES = {
    "ux_trade_lifecycle_event_id": ("event_id",),
    "ux_lifecycle_signal_event_ts_state": ("signal_id", "event_ts", "lifecycle_state"),
}


def _duplicates(bind, columns):
    names = ", ".join(columns)
    present = " AND ".join(f'"{name}" IS NOT NULL' for name in columns)
    return list(bind.execute(sa.text(
        f'SELECT {names}, COUNT(*) FROM trade_lifecycle_events WHERE {present} '
        f'GROUP BY {names} HAVING COUNT(*) > 1 LIMIT 20'
    )).fetchall())


def _evidence_digest(columns, rows):
    payload = {"columns": list(columns), "rows": [list(row) for row in rows]}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


def _require_understood_schema(bind, inspector, columns):
    unknown_columns = sorted(set(columns) - KNOWN_COLUMNS)
    if unknown_columns:
        raise RuntimeError(f"unsupported lifecycle schema blocked before mutation; unknown_columns={unknown_columns}")
    reflected_columns = {item["name"]: item for item in inspector.get_columns("trade_lifecycle_events")}
    unknown_defaults = {name: item.get("default") for name, item in reflected_columns.items() if item.get("default") is not None}
    allowed_legacy_not_null = {"order_intent_id", "event_type", "event_payload"}
    unknown_not_null = sorted(name for name, item in reflected_columns.items()
                              if item.get("nullable") is False and name != "id" and name not in allowed_legacy_not_null)
    primary_key = tuple(inspector.get_pk_constraint("trade_lifecycle_events").get("constrained_columns") or ())
    if unknown_defaults or unknown_not_null or primary_key != ("id",):
        raise RuntimeError("unsupported lifecycle schema blocked before mutation; "
                           f"defaults={unknown_defaults}; not_null={unknown_not_null}; primary_key={primary_key}")

    unknown_indexes = []
    for row in bind.execute(sa.text("PRAGMA index_list('trade_lifecycle_events')")).fetchall():
        _, name, unique, origin, partial = row
        index_columns = tuple(item[2] for item in bind.execute(sa.text(f'PRAGMA index_info("{name}")')).fetchall())
        if origin == "pk":
            continue
        xinfo = bind.execute(sa.text(f'PRAGMA index_xinfo("{name}")')).fetchall()
        noncanonical_order_or_collation = any(item[5] and (item[3] != 0 or str(item[4]).upper() != "BINARY") for item in xinfo)
        if (origin == "u" and name.startswith("sqlite_autoindex_") and index_columns in KNOWN_INDEXES.values()
                and not partial and not noncanonical_order_or_collation):
            # Canonical inline UNIQUE(event_id), recreated as the named canonical index.
            continue
        if (name not in KNOWN_INDEXES or not unique or partial or noncanonical_order_or_collation
                or index_columns != KNOWN_INDEXES[name]):
            unknown_indexes.append({"name": name, "unique": bool(unique), "columns": index_columns,
                                    "origin": origin, "partial": bool(partial), "xinfo": [tuple(item) for item in xinfo]})
    if unknown_indexes:
        raise RuntimeError(f"unsupported lifecycle schema blocked before mutation; unknown_indexes={unknown_indexes}")

    triggers = [tuple(row) for row in bind.execute(sa.text(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='trade_lifecycle_events' ORDER BY name"
    )).fetchall()]
    if triggers:
        raise RuntimeError(f"unsupported lifecycle schema blocked before mutation; triggers={triggers}")

    foreign_keys = inspector.get_foreign_keys("trade_lifecycle_events")
    understood_fk = lambda fk: (tuple(fk.get("constrained_columns") or ()) == ("order_intent_id",)
        and fk.get("referred_table") == "order_intents" and tuple(fk.get("referred_columns") or ()) == ("id",))
    unknown_fks = [fk for fk in foreign_keys if not understood_fk(fk)]
    if unknown_fks:
        raise RuntimeError(f"unsupported lifecycle schema blocked before mutation; foreign_keys={unknown_fks}")
    checks = inspector.get_check_constraints("trade_lifecycle_events")
    uniques = inspector.get_unique_constraints("trade_lifecycle_events")
    unknown_uniques = [item for item in uniques if tuple(item.get("column_names") or ()) not in KNOWN_INDEXES.values()]
    if checks or unknown_uniques:
        raise RuntimeError(f"unsupported lifecycle schema blocked before mutation; checks={checks}; unique_constraints={unknown_uniques}")
    return bool(foreign_keys)


def _sqlite_upgrade(bind):
    inspector = sa.inspect(bind)
    if not inspector.has_table("trade_lifecycle_events"):
        raise RuntimeError("lifecycle schema repair blocked: trade_lifecycle_events is missing")
    old_column_list = [column["name"] for column in inspector.get_columns("trade_lifecycle_events")]
    preserve_order_intent_fk = _require_understood_schema(bind, inspector, old_column_list)
    null_ids = int(bind.execute(sa.text("SELECT COUNT(*) FROM trade_lifecycle_events WHERE id IS NULL")).scalar_one())
    if null_ids:
        raise RuntimeError(f"unsafe lifecycle rebuild blocked; null surrogate ids would require invented values: {null_ids}")
    for identity in (("event_id",), ("signal_id", "event_ts", "lifecycle_state")):
        duplicates = _duplicates(bind, identity)
        if duplicates:
            raise RuntimeError(f"unsafe lifecycle rebuild blocked; duplicate identity {identity}: {duplicates}")

    old_count = int(bind.execute(sa.text("SELECT COUNT(*) FROM trade_lifecycle_events")).scalar_one())
    old_columns = set(old_column_list)
    bind.execute(sa.text("DROP TABLE IF EXISTS trade_lifecycle_events__doctor_new"))
    definitions = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
    definitions += [f'"{name}" TEXT' for name in TEXT_COLUMNS]
    definitions += [f'"{name}" REAL' for name in REAL_COLUMNS]
    definitions += [f'"{name}" INTEGER' for name in INTEGER_COLUMNS]
    if preserve_order_intent_fk:
        definitions += ['FOREIGN KEY("order_intent_id") REFERENCES "order_intents"("id")']
    bind.execute(sa.text("CREATE TABLE trade_lifecycle_events__doctor_new (" + ",".join(definitions) + ")"))
    copy_columns = ["id"] + [c for c in (*TEXT_COLUMNS, *REAL_COLUMNS, *INTEGER_COLUMNS) if c in old_columns]
    quoted = ",".join(f'"{c}"' for c in copy_columns)
    bind.execute(sa.text(
        f"INSERT INTO trade_lifecycle_events__doctor_new ({quoted}) SELECT {quoted} FROM trade_lifecycle_events"
    ))
    new_count = int(bind.execute(sa.text("SELECT COUNT(*) FROM trade_lifecycle_events__doctor_new")).scalar_one())
    if new_count != old_count:
        raise RuntimeError(f"lifecycle row count verification failed: old={old_count} new={new_count}")
    evidence_columns = old_column_list
    evidence_sql = ",".join(f'"{name}"' for name in evidence_columns)
    old_rows = [tuple(row) for row in bind.execute(sa.text(
        f"SELECT {evidence_sql} FROM trade_lifecycle_events ORDER BY id"
    )).fetchall()]
    new_rows = [tuple(row) for row in bind.execute(sa.text(
        f"SELECT {evidence_sql} FROM trade_lifecycle_events__doctor_new ORDER BY id"
    )).fetchall()]
    old_digest = _evidence_digest(evidence_columns, old_rows)
    new_digest = _evidence_digest(evidence_columns, new_rows)
    if old_rows != new_rows or old_digest != new_digest:
        raise RuntimeError(f"lifecycle value-level evidence verification failed: old_sha256={old_digest} new_sha256={new_digest}")
    bind.execute(sa.text("DROP TABLE trade_lifecycle_events"))
    bind.execute(sa.text("ALTER TABLE trade_lifecycle_events__doctor_new RENAME TO trade_lifecycle_events"))
    bind.execute(sa.text("CREATE UNIQUE INDEX ux_trade_lifecycle_event_id ON trade_lifecycle_events(event_id)"))
    bind.execute(sa.text("CREATE UNIQUE INDEX ux_lifecycle_signal_event_ts_state ON trade_lifecycle_events(signal_id,event_ts,lifecycle_state)"))


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _sqlite_upgrade(bind)
        return
    if bind.dialect.name == "postgresql":
        # PostgreSQL BIGINT primary keys require an explicit sequence/default too.
        op.execute("CREATE SEQUENCE IF NOT EXISTS trade_lifecycle_events_id_seq OWNED BY trade_lifecycle_events.id")
        op.execute("ALTER TABLE trade_lifecycle_events ALTER COLUMN id SET DEFAULT nextval('trade_lifecycle_events_id_seq')")
        op.execute("SELECT setval('trade_lifecycle_events_id_seq', COALESCE((SELECT MAX(id) FROM trade_lifecycle_events), 0) + 1, false)")
        return
    raise RuntimeError(f"unsupported lifecycle migration dialect: {bind.dialect.name}")


def downgrade():
    # Evidence-preserving migration is intentionally irreversible.
    pass
