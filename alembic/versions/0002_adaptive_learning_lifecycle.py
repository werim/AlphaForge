"""adaptive learning lifecycle

Revision ID: 0002_adaptive_learning_lifecycle
Revises: 0001_phase1_init
Create Date: 2026-05-06
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_adaptive_learning_lifecycle"
down_revision = "0001_phase1_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "adaptive_threshold_stats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("window_size", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("consecutive_sl_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_tp_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rolling_winrate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("rolling_expectancy", sa.Float(), nullable=False, server_default="0"),
        sa.Column("min_score", sa.Float(), nullable=False, server_default="7.5"),
        sa.Column("min_rr", sa.Float(), nullable=False, server_default="1.3"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "expectancy_stats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("setup_type", sa.String(length=128), nullable=False),
        sa.Column("regime", sa.String(length=128), nullable=False),
        sa.Column("bucket", sa.String(length=32), nullable=False),
        sa.Column("trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("winrate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("avg_rr", sa.Float(), nullable=False, server_default="0"),
        sa.Column("expectancy", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("setup_type", "regime", "bucket", name="uq_expectancy_stats_key"),
    )


def downgrade() -> None:
    op.drop_table("expectancy_stats")
    op.drop_table("adaptive_threshold_stats")
