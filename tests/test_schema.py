from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphaforge.models.schema import ExchangeSymbol, MarketType


def test_tables_create_from_metadata() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    from alphaforge.db.base import Base
    from alphaforge.models import schema  # noqa: F401

    Base.metadata.create_all(engine)
    names = set(inspect(engine).get_table_names())
    assert {"exchange_symbols", "config_snapshots", "symbol_discovery_audit"}.issubset(names)


def test_exchange_symbol_unique_constraint() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    from alphaforge.db.base import Base
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(ExchangeSymbol(exchange="BINANCE", symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", market_type=MarketType.USDT_M, status="ACTIVE"))
        session.commit()
        session.add(ExchangeSymbol(exchange="BINANCE", symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", market_type=MarketType.USDT_M, status="ACTIVE"))
        try:
            session.commit()
            assert False, "Expected IntegrityError"
        except IntegrityError:
            session.rollback()


def test_append_only_trigger_like_policy_sqlite() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE config_snapshots (id INTEGER PRIMARY KEY, component TEXT, version TEXT, payload TEXT)"))
        conn.execute(text("INSERT INTO config_snapshots (component, version, payload) VALUES ('core', '1', '{}')"))
        conn.execute(text("CREATE TRIGGER trg_config_snapshots_no_update BEFORE UPDATE ON config_snapshots BEGIN SELECT RAISE(ABORT, 'config_snapshots is append-only'); END;"))

        try:
            conn.execute(text("UPDATE config_snapshots SET version='2' WHERE id=1"))
            assert False, "Expected update failure"
        except Exception as exc:  # sqlite raises OperationalError wrapper
            assert "append-only" in str(exc)
