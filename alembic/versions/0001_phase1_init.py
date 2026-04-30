"""phase1 init full

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


def _json_type(bind):
    return sa.dialects.postgresql.JSONB() if bind.dialect.name == "postgresql" else sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    json_t = _json_type(bind)
    market_type = sa.Enum("USDT_M", "COIN_M", name="markettype")
    decision = sa.Enum("ALLOW", "BLOCK", name="decision")
    side = sa.Enum("BUY", "SELL", name="side")
    market_type.create(bind, checkfirst=True)
    decision.create(bind, checkfirst=True)
    side.create(bind, checkfirst=True)

    op.create_table("exchange_symbols", sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("venue", sa.String(32), nullable=False), sa.Column("market_type", market_type, nullable=False), sa.Column("symbol", sa.String(64), nullable=False), sa.Column("pair", sa.String(64), nullable=False), sa.Column("contract_type", sa.String(32), nullable=False), sa.Column("base_asset", sa.String(32), nullable=False), sa.Column("quote_asset", sa.String(32), nullable=False), sa.Column("margin_asset", sa.String(32), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("onboard_date", sa.DateTime(timezone=True)), sa.Column("delivery_date", sa.DateTime(timezone=True)), sa.Column("price_precision", sa.Integer(), nullable=False), sa.Column("quantity_precision", sa.Integer(), nullable=False), sa.Column("tick_size", sa.Numeric(20,10), nullable=False), sa.Column("step_size", sa.Numeric(20,10), nullable=False), sa.Column("min_qty", sa.Numeric(20,10), nullable=False), sa.Column("min_notional", sa.Numeric(20,10), nullable=False), sa.Column("contract_size", sa.Numeric(20,10), nullable=False), sa.Column("last_synced_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False), sa.Column("raw_exchange_info_json", json_t, nullable=False), sa.UniqueConstraint("venue", "market_type", "symbol", name="uq_exchange_symbol"), sa.CheckConstraint("price_precision >= 0"), sa.CheckConstraint("quantity_precision >= 0"))

    tables = [
        ("candles", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("symbol_id", sa.BigInteger(), sa.ForeignKey("exchange_symbols.id"), nullable=False), sa.Column("timeframe", sa.String(16), nullable=False), sa.Column("open_time", sa.DateTime(timezone=True), nullable=False), sa.Column("close_time", sa.DateTime(timezone=True), nullable=False), sa.Column("open", sa.Numeric(20,10), nullable=False), sa.Column("high", sa.Numeric(20,10), nullable=False), sa.Column("low", sa.Numeric(20,10), nullable=False), sa.Column("close", sa.Numeric(20,10), nullable=False), sa.Column("volume", sa.Numeric(28,10), nullable=False), sa.UniqueConstraint("symbol_id","timeframe","open_time", name="uq_candles_symbol_time")]),
        ("indicator_snapshots", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("candle_id", sa.BigInteger(), sa.ForeignKey("candles.id"), nullable=False), sa.Column("indicators", json_t, nullable=False)]),
        ("regime_states", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("symbol_id", sa.BigInteger(), sa.ForeignKey("exchange_symbols.id"), nullable=False), sa.Column("regime", sa.String(32), nullable=False), sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False)]),
        ("strategy_signals", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("symbol_id", sa.BigInteger(), sa.ForeignKey("exchange_symbols.id"), nullable=False), sa.Column("regime_state_id", sa.BigInteger(), sa.ForeignKey("regime_states.id")), sa.Column("signal", sa.String(32), nullable=False), sa.Column("signal_payload", json_t, nullable=False)]),
        ("selector_decisions", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("strategy_signal_id", sa.BigInteger(), sa.ForeignKey("strategy_signals.id"), nullable=False), sa.Column("decision", decision, nullable=False), sa.Column("reason", sa.Text(), nullable=False)]),
        ("order_intents", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("selector_decision_id", sa.BigInteger(), sa.ForeignKey("selector_decisions.id"), nullable=False), sa.Column("symbol_id", sa.BigInteger(), sa.ForeignKey("exchange_symbols.id"), nullable=False), sa.Column("side", side, nullable=False), sa.Column("quantity", sa.Numeric(20,10), nullable=False), sa.Column("price", sa.Numeric(20,10))]),
        ("risk_decisions", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("order_intent_id", sa.BigInteger(), sa.ForeignKey("order_intents.id"), nullable=False), sa.Column("decision", decision, nullable=False), sa.Column("reason", sa.Text(), nullable=False)]),
        ("trade_lifecycle_events", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("order_intent_id", sa.BigInteger(), sa.ForeignKey("order_intents.id"), nullable=False), sa.Column("event_type", sa.String(32), nullable=False), sa.Column("event_payload", json_t, nullable=False)]),
        ("positions", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("symbol_id", sa.BigInteger(), sa.ForeignKey("exchange_symbols.id"), nullable=False), sa.Column("side", side, nullable=False), sa.Column("size", sa.Numeric(20,10), nullable=False)]),
        ("orders", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("order_intent_id", sa.BigInteger(), sa.ForeignKey("order_intents.id"), nullable=False), sa.Column("external_order_id", sa.String(128)), sa.Column("status", sa.String(24), nullable=False)]),
        ("closed_trades", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("position_id", sa.BigInteger(), sa.ForeignKey("positions.id"), nullable=False), sa.Column("pnl", sa.Numeric(20,10), nullable=False)]),
        ("rejection_audit", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("order_intent_id", sa.BigInteger(), sa.ForeignKey("order_intents.id"), nullable=False), sa.Column("reason", sa.Text(), nullable=False), sa.Column("payload", json_t, nullable=False)]),
        ("order_decision_audit", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("risk_decision_id", sa.BigInteger(), sa.ForeignKey("risk_decisions.id"), nullable=False), sa.Column("payload", json_t, nullable=False)]),
        ("config_snapshots", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("component", sa.String(64), nullable=False), sa.Column("version", sa.String(64), nullable=False), sa.Column("payload", json_t, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False)]),
        ("strategy_performance", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("strategy_name", sa.String(64), nullable=False), sa.Column("metrics", json_t, nullable=False)]),
        ("regime_performance", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("regime", sa.String(32), nullable=False), sa.Column("metrics", json_t, nullable=False)]),
        ("optimizer_trials", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("trial_key", sa.String(128), unique=True, nullable=False), sa.Column("params", json_t, nullable=False)]),
        ("optimizer_results", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("trial_id", sa.BigInteger(), sa.ForeignKey("optimizer_trials.id"), nullable=False), sa.Column("result", json_t, nullable=False)]),
        ("runtime_state", [sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("key", sa.String(128), unique=True, nullable=False), sa.Column("value", json_t, nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False)]),
    ]
    for name, cols in tables:
        op.create_table(name, *cols)

    if bind.dialect.name == "postgresql":
        for table in ["config_snapshots", "rejection_audit", "order_decision_audit"]:
            op.execute(f"CREATE FUNCTION {table}_immutable_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '{table} is append-only'; END; $$;")
            op.execute(f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION {table}_immutable_fn();")
    else:
        for table in ["config_snapshots", "rejection_audit", "order_decision_audit"]:
            op.execute(f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;")
            op.execute(f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;")


def downgrade() -> None:
    pass
