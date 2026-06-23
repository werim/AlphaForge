"""bootstrap runtime evidence tables

Revision ID: 0003_sqlite_bootstrap_runtime_tables
Revises: 0002_adaptive_learning_lifecycle
Create Date: 2026-06-23
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_sqlite_bootstrap_runtime_tables"
down_revision = "0002_adaptive_learning_lifecycle"
branch_labels = None
depends_on = None


def _json_type(bind):
    return sa.dialects.postgresql.JSONB() if bind.dialect.name == "postgresql" else sa.JSON()


def _has_table(bind, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _ensure_config_snapshot_triggers(bind) -> None:
    if bind.dialect.name == "postgresql":
        return
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_config_snapshots_no_update "
        "BEFORE UPDATE ON config_snapshots "
        "BEGIN SELECT RAISE(ABORT, 'config_snapshots is append-only'); END;"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_config_snapshots_no_delete "
        "BEFORE DELETE ON config_snapshots "
        "BEGIN SELECT RAISE(ABORT, 'config_snapshots is append-only'); END;"
    )


def upgrade() -> None:
    bind = op.get_bind()
    json_t = _json_type(bind)

    # Defensive repair for partially-applied legacy SQLite databases that failed
    # before the append-only config snapshot table was created. Fresh databases
    # already get this table from 0001; this keeps upgrade head idempotently safe
    # without dropping or rewriting existing rows.
    if not _has_table(bind, "config_snapshots"):
        op.create_table(
            "config_snapshots",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("component", sa.String(64), nullable=False),
            sa.Column("version", sa.String(64), nullable=False),
            sa.Column("payload", json_t, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )

    _ensure_config_snapshot_triggers(bind)

    if not _has_table(bind, "timesfm_forecast_evidence"):
        op.create_table(
            "timesfm_forecast_evidence",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("forecast_id", sa.Text(), nullable=False, unique=True),
            sa.Column("timestamp", sa.Integer(), nullable=False),
            sa.Column("symbol", sa.Text(), nullable=False),
            sa.Column("timeframe", sa.Text(), nullable=False),
            sa.Column("horizon", sa.Integer()),
            sa.Column("current_price", sa.Float()),
            sa.Column("forecast_p10", sa.Float()),
            sa.Column("forecast_p50", sa.Float()),
            sa.Column("forecast_p90", sa.Float()),
            sa.Column("side", sa.Text(), nullable=False),
            sa.Column("expected_rr", sa.Float()),
            sa.Column("rejection_reason", sa.Text()),
            sa.Column("mode", sa.Text(), nullable=False),
            sa.Column("model_provider", sa.Text()),
            sa.Column("model_name", sa.Text()),
            sa.Column("model_version", sa.Text()),
            sa.Column("no_lookahead_input_end_ts", sa.Integer(), nullable=False),
            sa.Column("payload_json", sa.Text()),
            sa.Column("created_at", sa.Text()),
            sa.Column("updated_at", sa.Text()),
        )

    if not _has_table(bind, "timesfm_forward_outcome_labels"):
        op.create_table(
            "timesfm_forward_outcome_labels",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("forecast_id", sa.Text(), nullable=False, unique=True),
            sa.Column("outcome", sa.Text(), nullable=False),
            sa.Column("mfe", sa.Float()),
            sa.Column("mae", sa.Float()),
            sa.Column("expected_r", sa.Float()),
            sa.Column("realized_r", sa.Float()),
            sa.Column("labeled_at", sa.Text()),
            sa.Column("payload_json", sa.Text()),
        )

    op.create_index(
        "ix_timesfm_evidence_symbol_timeframe_ts",
        "timesfm_forecast_evidence",
        ["symbol", "timeframe", "timestamp"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_timesfm_evidence_symbol_timeframe_ts", table_name="timesfm_forecast_evidence", if_exists=True)
    op.drop_table("timesfm_forward_outcome_labels", if_exists=True)
    op.drop_table("timesfm_forecast_evidence", if_exists=True)
