"""align init_db baseline runtime tables

Revision ID: 0004_align_init_db_baseline_tables
Revises: 0003_sqlite_bootstrap_runtime_tables
Create Date: 2026-06-23
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_align_init_db_baseline_tables"
down_revision = "0003_sqlite_bootstrap_runtime_tables"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _create_if_missing(bind, name: str, *columns) -> None:
    if not _has_table(bind, name):
        op.create_table(name, *columns)


def upgrade() -> None:
    bind = op.get_bind()
    _create_if_missing(bind, "signals", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("signal_id", sa.Text(), unique=True), sa.Column("symbol", sa.Text()), sa.Column("side", sa.Text()), sa.Column("timeframe", sa.Text()), sa.Column("mode", sa.Text()), sa.Column("score", sa.Float()), sa.Column("rr", sa.Float()), sa.Column("effective_rr", sa.Float()), sa.Column("expectancy_bucket", sa.Text()), sa.Column("created_at", sa.Text()), sa.Column("updated_at", sa.Text()))
    _create_if_missing(bind, "order_decisions", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("decision_id", sa.Text(), unique=True), sa.Column("signal_id", sa.Text()), sa.Column("order_id", sa.Text()), sa.Column("symbol", sa.Text()), sa.Column("mode", sa.Text()), sa.Column("decision", sa.Text()), sa.Column("reject_reason", sa.Text()), sa.Column("score", sa.Float()), sa.Column("rr", sa.Float()), sa.Column("effective_rr", sa.Float()), sa.Column("expectancy_bucket", sa.Text()), sa.Column("payload", sa.Text()), sa.Column("execution_ctx", sa.Text()), sa.Column("execution_ctx_missing", sa.Integer()), sa.Column("created_at", sa.Text()), sa.Column("updated_at", sa.Text()))
    _create_if_missing(bind, "signal_id_state", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("scope", sa.Text(), nullable=False, unique=True), sa.Column("last_signal_id", sa.Text()), sa.Column("updated_at", sa.Text()))
    _create_if_missing(bind, "positions", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("position_id", sa.Text(), unique=True), sa.Column("symbol", sa.Text()), sa.Column("side", sa.Text()), sa.Column("qty", sa.Float()), sa.Column("entry_price", sa.Float()), sa.Column("status", sa.Text()), sa.Column("created_at", sa.Text()), sa.Column("updated_at", sa.Text()))
    _create_if_missing(bind, "orders", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("order_id", sa.Text(), unique=True), sa.Column("signal_id", sa.Text()), sa.Column("symbol", sa.Text()), sa.Column("side", sa.Text()), sa.Column("status", sa.Text()), sa.Column("created_at", sa.Text()), sa.Column("updated_at", sa.Text()))
    _create_if_missing(bind, "fills", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("fill_id", sa.Text(), unique=True), sa.Column("order_id", sa.Text()), sa.Column("symbol", sa.Text()), sa.Column("side", sa.Text()), sa.Column("qty", sa.Float()), sa.Column("price", sa.Float()), sa.Column("fee", sa.Float()), sa.Column("filled_at", sa.Text()), sa.Column("created_at", sa.Text()))
    _create_if_missing(bind, "paper_events", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("event_id", sa.Text(), unique=True), sa.Column("event_type", sa.Text()), sa.Column("symbol", sa.Text()), sa.Column("payload_json", sa.Text()), sa.Column("created_at", sa.Text()))
    _create_if_missing(bind, "backtest_runs", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("run_id", sa.Text(), unique=True), sa.Column("started_at", sa.Text()), sa.Column("completed_at", sa.Text()), sa.Column("payload_json", sa.Text()))
    _create_if_missing(bind, "backtest_events", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("run_id", sa.Text()), sa.Column("event_type", sa.Text()), sa.Column("symbol", sa.Text()), sa.Column("payload_json", sa.Text()), sa.Column("created_at", sa.Text()))
    _create_if_missing(bind, "symbol_snapshots", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("symbol", sa.Text(), nullable=False), sa.Column("timeframe", sa.Text()), sa.Column("snapshot_ts", sa.Text()), sa.Column("payload_json", sa.Text()), sa.Column("created_at", sa.Text()))
    _create_if_missing(bind, "runtime_control_state", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("mode_requested", sa.Text(), nullable=False), sa.Column("mode_running", sa.Text()), sa.Column("kill_switch_active", sa.Integer(), nullable=False, server_default="0"), sa.Column("kill_switch_source", sa.Text()), sa.Column("kill_switch_updated_at", sa.Text()), sa.Column("runtime_status", sa.Text(), nullable=False), sa.Column("last_error", sa.Text()), sa.Column("updated_at", sa.Text(), nullable=False))
    _create_if_missing(bind, "calibration_labels", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("signal_id", sa.Text()), sa.Column("label", sa.Text()), sa.Column("payload_json", sa.Text()), sa.Column("created_at", sa.Text()))
    _create_if_missing(bind, "optimizer_runs", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("run_id", sa.Text(), unique=True), sa.Column("status", sa.Text()), sa.Column("payload_json", sa.Text()), sa.Column("created_at", sa.Text()), sa.Column("updated_at", sa.Text()))
    op.execute("CREATE INDEX IF NOT EXISTS ix_timesfm_evidence_symbol_timeframe_ts ON timesfm_forecast_evidence(symbol, timeframe, timestamp)")


def downgrade() -> None:
    pass
