"""normalize core lifecycle identifiers

Revision ID: 0005_core_identifier_normalization
Revises: 0004_align_init_db_baseline_tables
Create Date: 2026-06-23
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_core_identifier_normalization"
down_revision = "0004_align_init_db_baseline_tables"
branch_labels = None
depends_on = None


CORE_IDENTIFIER_COLUMNS = {
    "signals": [("signal_id", sa.Text()), ("symbol", sa.Text()), ("timeframe", sa.Text()), ("mode", sa.Text()), ("created_at", sa.Text()), ("updated_at", sa.Text())],
    "order_decisions": [("decision_id", sa.Text()), ("signal_id", sa.Text()), ("symbol", sa.Text()), ("timeframe", sa.Text()), ("mode", sa.Text()), ("created_at", sa.Text()), ("updated_at", sa.Text())],
    "signal_id_state": [("signal_id", sa.Text()), ("symbol", sa.Text()), ("timeframe", sa.Text()), ("mode", sa.Text()), ("created_at", sa.Text()), ("updated_at", sa.Text())],
    "orders": [("order_id", sa.Text()), ("signal_id", sa.Text()), ("position_id", sa.Text()), ("symbol", sa.Text()), ("timeframe", sa.Text()), ("mode", sa.Text()), ("created_at", sa.Text()), ("updated_at", sa.Text())],
    "positions": [("position_id", sa.Text()), ("signal_id", sa.Text()), ("symbol", sa.Text()), ("timeframe", sa.Text()), ("mode", sa.Text()), ("created_at", sa.Text()), ("updated_at", sa.Text())],
    "fills": [("order_id", sa.Text()), ("position_id", sa.Text()), ("signal_id", sa.Text()), ("symbol", sa.Text()), ("created_at", sa.Text())],
    "paper_events": [("event_id", sa.Text()), ("signal_id", sa.Text()), ("order_id", sa.Text()), ("position_id", sa.Text()), ("symbol", sa.Text()), ("timeframe", sa.Text()), ("mode", sa.Text()), ("created_at", sa.Text())],
    "backtest_runs": [("run_id", sa.Text()), ("mode", sa.Text()), ("created_at", sa.Text()), ("updated_at", sa.Text())],
    "backtest_events": [("event_id", sa.Text()), ("run_id", sa.Text()), ("signal_id", sa.Text()), ("order_id", sa.Text()), ("position_id", sa.Text()), ("symbol", sa.Text()), ("timeframe", sa.Text()), ("mode", sa.Text()), ("created_at", sa.Text())],
    "symbol_snapshots": [("run_id", sa.Text()), ("symbol", sa.Text()), ("timeframe", sa.Text()), ("mode", sa.Text()), ("created_at", sa.Text())],
    "timesfm_forecast_evidence": [("symbol", sa.Text()), ("timeframe", sa.Text()), ("timestamp", sa.Integer()), ("created_at", sa.Text())],
    "calibration_labels": [("signal_id", sa.Text()), ("run_id", sa.Text()), ("symbol", sa.Text()), ("timeframe", sa.Text()), ("mode", sa.Text()), ("created_at", sa.Text())],
    "optimizer_runs": [("run_id", sa.Text()), ("created_at", sa.Text()), ("updated_at", sa.Text())],
}

CORE_IDENTIFIER_INDEXES = [
    ("ix_signals_signal_id", "signals", "signal_id"),
    ("ix_order_decisions_decision_id", "order_decisions", "decision_id"),
    ("ix_order_decisions_signal_id", "order_decisions", "signal_id"),
    ("ix_orders_order_id", "orders", "order_id"),
    ("ix_orders_signal_id", "orders", "signal_id"),
    ("ix_orders_position_id", "orders", "position_id"),
    ("ix_positions_position_id", "positions", "position_id"),
    ("ix_positions_signal_id", "positions", "signal_id"),
    ("ix_fills_order_id", "fills", "order_id"),
    ("ix_fills_position_id", "fills", "position_id"),
    ("ix_paper_events_signal_id", "paper_events", "signal_id"),
    ("ix_paper_events_position_id", "paper_events", "position_id"),
    ("ix_backtest_events_run_id", "backtest_events", "run_id"),
    ("ix_backtest_events_signal_id", "backtest_events", "signal_id"),
    ("ix_calibration_labels_signal_id", "calibration_labels", "signal_id"),
    ("ix_optimizer_runs_run_id", "optimizer_runs", "run_id"),
]


def _columns(bind, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for table_name, columns in CORE_IDENTIFIER_COLUMNS.items():
        if table_name not in existing_tables:
            continue
        existing_columns = _columns(bind, table_name)
        for column_name, column_type in columns:
            if column_name not in existing_columns:
                op.add_column(table_name, sa.Column(column_name, column_type, nullable=True))
                existing_columns.add(column_name)
    for index_name, table_name, column_name in CORE_IDENTIFIER_INDEXES:
        if table_name in existing_tables and column_name in _columns(bind, table_name):
            op.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name}({column_name})")


def downgrade() -> None:
    pass
