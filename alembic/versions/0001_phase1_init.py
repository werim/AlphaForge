"""phase1 init

Revision ID: 0001_phase1
Revises: 
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_phase1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    market_type = sa.Enum("USDT_M", "COIN_M", name="markettype")
    market_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "exchange_symbols",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("base_asset", sa.String(length=32), nullable=False),
        sa.Column("quote_asset", sa.String(length=32), nullable=False),
        sa.Column("market_type", market_type, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("exchange", "symbol", "market_type", name="uq_exchange_symbol_market"),
    )

    op.create_table(
        "config_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )

    op.create_table(
        "symbol_discovery_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("market_type", market_type, nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )

    op.execute("""
    CREATE TRIGGER trg_config_snapshots_no_update
    BEFORE UPDATE ON config_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'config_snapshots is append-only');
    END;
    """)
    op.execute("""
    CREATE TRIGGER trg_config_snapshots_no_delete
    BEFORE DELETE ON config_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'config_snapshots is append-only');
    END;
    """)
    op.execute("""
    CREATE TRIGGER trg_symbol_discovery_audit_no_update
    BEFORE UPDATE ON symbol_discovery_audit
    BEGIN
        SELECT RAISE(ABORT, 'symbol_discovery_audit is append-only');
    END;
    """)
    op.execute("""
    CREATE TRIGGER trg_symbol_discovery_audit_no_delete
    BEFORE DELETE ON symbol_discovery_audit
    BEGIN
        SELECT RAISE(ABORT, 'symbol_discovery_audit is append-only');
    END;
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_symbol_discovery_audit_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_symbol_discovery_audit_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_config_snapshots_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_config_snapshots_no_update")
    op.drop_table("symbol_discovery_audit")
    op.drop_table("config_snapshots")
    op.drop_table("exchange_symbols")
