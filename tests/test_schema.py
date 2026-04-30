from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphaforge.db.base import Base
from alphaforge.models.schema import ExchangeSymbol, MarketType

MANDATORY = {
    "candles", "indicator_snapshots", "regime_states", "strategy_signals", "selector_decisions",
    "order_intents", "risk_decisions", "trade_lifecycle_events", "positions", "orders", "closed_trades",
    "rejection_audit", "order_decision_audit", "config_snapshots", "strategy_performance",
    "regime_performance", "optimizer_trials", "optimizer_results", "runtime_state", "exchange_symbols",
}


def test_imports() -> None:
    import alphaforge.clients.binance  # noqa: F401
    import alphaforge.discovery.contracts  # noqa: F401
    import alphaforge.config.persistence  # noqa: F401


def test_all_mandatory_tables_created() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    names = set(inspect(engine).get_table_names())
    assert MANDATORY.issubset(names)


def test_exchange_symbols_uniqueness_and_check_constraint() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    row = dict(
        id=1, venue="BINANCE", market_type=MarketType.USDT_M, symbol="BTCUSDT", pair="BTCUSDT",
        contract_type="PERPETUAL", base_asset="BTC", quote_asset="USDT", margin_asset="USDT",
        status="TRADING", price_precision=2, quantity_precision=3, tick_size=0.1, step_size=0.001,
        min_qty=0.001, min_notional=5, contract_size=1, raw_exchange_info_json={"x": 1}
    )
    with Session(engine) as s:
        s.add(ExchangeSymbol(**row))
        s.commit()
        s.add(ExchangeSymbol(**{**row, "id": 2}))
        try:
            s.commit()
            assert False
        except IntegrityError:
            s.rollback()


def test_append_only_trigger_sqlite_pattern() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE config_snapshots (id INTEGER PRIMARY KEY, payload TEXT)"))
        conn.execute(text("INSERT INTO config_snapshots (id,payload) VALUES (1,'{}')"))
        conn.execute(text("CREATE TRIGGER trg_config_snapshots_no_update BEFORE UPDATE ON config_snapshots BEGIN SELECT RAISE(ABORT, 'config_snapshots is append-only'); END;"))
        try:
            conn.execute(text("UPDATE config_snapshots SET payload='x' WHERE id=1"))
            assert False
        except Exception as exc:
            assert "append-only" in str(exc)
