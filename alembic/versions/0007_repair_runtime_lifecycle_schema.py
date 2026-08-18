"""Repair the additive normalized runtime lifecycle contract.

Revision ID: 0007_repair_runtime_lifecycle_schema
Revises: 0006_reject_label_identity_timeframe
"""
from alembic import op
import sqlalchemy as sa


revision = "0007_repair_runtime_lifecycle_schema"
down_revision = "0006_reject_label_identity_timeframe"
branch_labels = None
depends_on = None


LIFECYCLE_COLUMNS = (
    ("event_id", sa.Text()), ("signal_id", sa.Text()), ("trade_id", sa.Text()),
    ("order_id", sa.Text()), ("symbol", sa.Text()), ("mode", sa.Text()),
    ("lifecycle_state", sa.Text()), ("state", sa.Text()), ("event_type", sa.Text()),
    ("payload", sa.Text()), ("decision", sa.Text()), ("reject_reason", sa.Text()),
    ("score", sa.Float()), ("rr", sa.Float()), ("effective_rr", sa.Float()),
    ("expectancy_bucket", sa.Text()), ("execution_ctx", sa.Text()),
    ("execution_ctx_missing", sa.Integer()), ("event_ts", sa.Text()),
    ("created_at", sa.Text()), ("lifecycle_seq", sa.Integer()),
    ("cancel_reason", sa.Text()), ("lifecycle_id", sa.Text()),
    ("failure_reason", sa.Text()), ("reconciliation_reason", sa.Text()),
    ("incident_payload", sa.Text()),
)

CANONICAL_STATES = (
    "SIGNAL_CREATED", "SIGNAL_REJECTED", "SYMBOL_REJECTED",
    "WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED", "ORDER_PLACED",
    "ORDER_REJECTED", "POSITION_OPENED", "POSITION_CLOSED",
    "ENTRY_TIMEOUT", "CANCELLED",
)


def _duplicate_diagnostics(bind, columns: tuple[str, ...]) -> list[tuple]:
    names = ", ".join(columns)
    not_null = " AND ".join(f"{name} IS NOT NULL" for name in columns)
    return list(bind.execute(sa.text(
        f"SELECT {names}, COUNT(*) AS duplicate_count "
        f"FROM trade_lifecycle_events WHERE {not_null} "
        f"GROUP BY {names} HAVING COUNT(*) > 1 ORDER BY duplicate_count DESC LIMIT 20"
    )).fetchall())


def _require_unique_evidence(bind, columns: tuple[str, ...]) -> None:
    null_predicate = " OR ".join(f"{name} IS NULL" for name in columns)
    null_count = int(bind.execute(sa.text(
        f"SELECT COUNT(*) FROM trade_lifecycle_events WHERE {null_predicate}"
    )).scalar_one())
    duplicates = _duplicate_diagnostics(bind, columns)
    if duplicates:
        rendered = [tuple(row) for row in duplicates]
        raise RuntimeError(
            "unsafe lifecycle uniqueness repair blocked for "
            f"trade_lifecycle_events({', '.join(columns)}); "
            f"null_identity_rows_preserved={null_count}; duplicates={rendered}"
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("trade_lifecycle_events"):
        raise RuntimeError("lifecycle schema repair blocked: trade_lifecycle_events is missing")

    existing = {column["name"] for column in inspector.get_columns("trade_lifecycle_events")}
    for name, column_type in LIFECYCLE_COLUMNS:
        if name not in existing:
            op.add_column("trade_lifecycle_events", sa.Column(name, column_type, nullable=True))
            existing.add(name)

    # Only exact canonical state evidence is safe to promote. Unknown/legacy
    # spellings remain NULL for operator review; timestamps and metrics are never invented.
    placeholders = ",".join(f"'{state}'" for state in CANONICAL_STATES)
    bind.execute(sa.text(
        "UPDATE trade_lifecycle_events SET lifecycle_state=UPPER(TRIM(state)) "
        f"WHERE lifecycle_state IS NULL AND UPPER(TRIM(state)) IN ({placeholders})"
    ))

    # NULL event identifiers are preserved because SQLite UNIQUE permits them.
    # Non-NULL duplicate evidence fails the migration transaction closed.
    _require_unique_evidence(bind, ("event_id",))
    _require_unique_evidence(bind, ("signal_id", "event_ts", "lifecycle_state"))
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_trade_lifecycle_event_id "
        "ON trade_lifecycle_events(event_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_lifecycle_signal_event_ts_state "
        "ON trade_lifecycle_events(signal_id, event_ts, lifecycle_state)"
    )


def downgrade() -> None:
    # Deliberately non-destructive: legacy rows and additive columns are evidence.
    pass
