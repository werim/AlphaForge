"""Add immutable canonical universe-selection evidence.

Revision ID: 0010_canonical_universe_selection
Revises: 0009_timestamp_bounded_expectancy_evidence
"""
from alembic import op
import sqlalchemy as sa


revision = "0010_canonical_universe_selection"
down_revision = "0009_timestamp_bounded_expectancy_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("universe_selection_cycles"):
        op.create_table(
            "universe_selection_cycles",
            sa.Column("cycle_id", sa.Text(), primary_key=True),
            sa.Column("decision_timestamp", sa.Float(), nullable=False),
            sa.Column("execution_mode", sa.Text(), nullable=False),
            sa.Column("selected_symbols_json", sa.Text(), nullable=False),
            sa.Column("candidate_count", sa.Integer(), nullable=False),
            sa.Column("config_hash", sa.Text(), nullable=False),
            sa.Column("strategy_config_hash", sa.Text()),
            sa.Column("universe_hash", sa.Text(), nullable=False),
            sa.Column("evidence_hash", sa.Text(), nullable=False),
            sa.Column("git_sha", sa.Text(), nullable=False),
            sa.Column("source_provenance_json", sa.Text(), nullable=False),
            sa.Column("ranking_version", sa.Text(), nullable=False),
            sa.Column("schema_version", sa.Text(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    inspector = sa.inspect(bind)
    if not inspector.has_table("universe_selection_candidates"):
        op.create_table(
            "universe_selection_candidates",
            sa.Column("cycle_id", sa.Text(), sa.ForeignKey("universe_selection_cycles.cycle_id"), primary_key=True),
            sa.Column("candidate_index", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.Text(), nullable=False),
            sa.Column("eligibility_state", sa.Text(), nullable=False),
            sa.Column("exclusion_reasons_json", sa.Text(), nullable=False),
            sa.Column("observed_inputs_json", sa.Text(), nullable=False),
            sa.Column("ranking_components_json", sa.Text(), nullable=False),
            sa.Column("ranking_score", sa.Float()),
            sa.Column("ranking_order", sa.Integer()),
            sa.Column("selected", sa.Integer(), nullable=False),
            sa.Column("evidence_availability_json", sa.Text(), nullable=False),
        )
    inspector = sa.inspect(bind)
    cycle_indexes = {item["name"] for item in inspector.get_indexes("universe_selection_cycles")}
    if "ix_universe_selection_cycles_time" not in cycle_indexes:
        op.create_index("ix_universe_selection_cycles_time", "universe_selection_cycles", ["decision_timestamp", "cycle_id"])
    candidate_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("universe_selection_candidates")}
    if "ix_universe_selection_candidates_symbol" not in candidate_indexes:
        op.create_index("ix_universe_selection_candidates_symbol", "universe_selection_candidates", ["symbol", "cycle_id"])

    if bind.dialect.name == "postgresql":
        for table in ("universe_selection_cycles", "universe_selection_candidates"):
            op.execute(f"CREATE OR REPLACE FUNCTION {table}_immutable_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '{table} is append-only'; END; $$;")
            op.execute(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_{table}_no_update' AND tgrelid = '{table}'::regclass) "
                f"THEN CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION {table}_immutable_fn(); END IF; END $$;"
            )
    else:
        for table in ("universe_selection_cycles", "universe_selection_candidates"):
            op.execute(f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'universe selection evidence is immutable'); END;")
            op.execute(f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'universe selection evidence is immutable'); END;")


def downgrade() -> None:
    # Canonical decision evidence is intentionally retained.
    pass
