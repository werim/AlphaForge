"""Add append-only, resolved timestamp-bounded expectancy evidence.

Revision ID: 0009_timestamp_bounded_expectancy_evidence
Revises: 0008_database_doctor_lifecycle_contract
"""
from alembic import op
import sqlalchemy as sa


revision = "0009_timestamp_bounded_expectancy_evidence"
down_revision = "0008_database_doctor_lifecycle_contract"
branch_labels = None
depends_on = None


def _add(table: str, name: str, column: sa.Column) -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(table) and name not in {item["name"] for item in inspector.get_columns(table)}:
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    _add("burnin_pending_position_outcomes", "source_decision_id", sa.Column("source_decision_id", sa.Text()))
    _add("burnin_pending_position_outcomes", "decision_time", sa.Column("decision_time", sa.Text()))
    _add("burnin_pending_position_outcomes", "setup_type", sa.Column("setup_type", sa.Text()))
    inspector = sa.inspect(bind)
    if not inspector.has_table("expectancy_evidence"):
        op.create_table(
            "expectancy_evidence",
            sa.Column("evidence_id", sa.Text(), primary_key=True),
            sa.Column("source_decision_id", sa.Text(), nullable=False),
            sa.Column("evidence_type", sa.Text(), nullable=False),
            sa.Column("decision_time", sa.Text(), nullable=False),
            sa.Column("resolved_at", sa.Text(), nullable=False),
            sa.Column("symbol", sa.Text(), nullable=False),
            sa.Column("side", sa.Text()), sa.Column("setup_type", sa.Text()),
            sa.Column("regime", sa.Text()), sa.Column("reject_reason", sa.Text()),
            sa.Column("net_r", sa.Float(), nullable=False),
            sa.Column("run_id", sa.Text()), sa.Column("campaign_id", sa.Text()),
            sa.Column("release_id", sa.Text()),
            sa.Column("evidence_complete", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.Text(), nullable=False),
        )
        inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("expectancy_evidence")}
    if "ix_expectancy_evidence_as_of" not in indexes:
        op.create_index("ix_expectancy_evidence_as_of", "expectancy_evidence", ["resolved_at", "decision_time"])
    if "ix_expectancy_evidence_symbol_as_of" not in indexes:
        op.create_index("ix_expectancy_evidence_symbol_as_of", "expectancy_evidence", ["symbol", "resolved_at", "decision_time"])


def downgrade() -> None:
    # Evidence and additive linkage columns are intentionally preserved.
    pass
