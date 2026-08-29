"""Rebuild the SQLite lifecycle table with rowid-compatible PK semantics.

Revision ID: 0008_database_doctor_lifecycle_contract
Revises: 0007_repair_runtime_lifecycle_schema
"""
from alembic import op
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


def _duplicates(bind, columns):
    names = ", ".join(columns)
    present = " AND ".join(f'"{name}" IS NOT NULL' for name in columns)
    return list(bind.execute(sa.text(
        f'SELECT {names}, COUNT(*) FROM trade_lifecycle_events WHERE {present} '
        f'GROUP BY {names} HAVING COUNT(*) > 1 LIMIT 20'
    )).fetchall())


def _sqlite_upgrade(bind):
    inspector = sa.inspect(bind)
    if not inspector.has_table("trade_lifecycle_events"):
        raise RuntimeError("lifecycle schema repair blocked: trade_lifecycle_events is missing")
    for identity in (("event_id",), ("signal_id", "event_ts", "lifecycle_state")):
        duplicates = _duplicates(bind, identity)
        if duplicates:
            raise RuntimeError(f"unsafe lifecycle rebuild blocked; duplicate identity {identity}: {duplicates}")

    old_count = int(bind.execute(sa.text("SELECT COUNT(*) FROM trade_lifecycle_events")).scalar_one())
    old_columns = {column["name"] for column in inspector.get_columns("trade_lifecycle_events")}
    bind.execute(sa.text("DROP TABLE IF EXISTS trade_lifecycle_events__doctor_new"))
    definitions = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
    definitions += [f'"{name}" TEXT' for name in TEXT_COLUMNS]
    definitions += [f'"{name}" REAL' for name in REAL_COLUMNS]
    definitions += [f'"{name}" INTEGER' for name in INTEGER_COLUMNS]
    bind.execute(sa.text("CREATE TABLE trade_lifecycle_events__doctor_new (" + ",".join(definitions) + ")"))
    copy_columns = ["id"] + [c for c in (*TEXT_COLUMNS, *REAL_COLUMNS, *INTEGER_COLUMNS) if c in old_columns]
    quoted = ",".join(f'"{c}"' for c in copy_columns)
    bind.execute(sa.text(
        f"INSERT INTO trade_lifecycle_events__doctor_new ({quoted}) SELECT {quoted} FROM trade_lifecycle_events"
    ))
    new_count = int(bind.execute(sa.text("SELECT COUNT(*) FROM trade_lifecycle_events__doctor_new")).scalar_one())
    if new_count != old_count:
        raise RuntimeError(f"lifecycle row count verification failed: old={old_count} new={new_count}")
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
