from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.persistence import fetch_expectancy_stat, init_db, upsert_expectancy_stats


def test_fetch_expectancy_missing_table_returns_none():
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        assert fetch_expectancy_stat(s, "invalid_table", "setup", "pullback") is None


def test_fetch_expectancy_missing_row_returns_none():
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        assert fetch_expectancy_stat(s, "setup_expectancy_stats", "setup", "missing") is None


def test_fetch_expectancy_existing_row_returns_float():
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        upsert_expectancy_stats(s, "setup_expectancy_stats", "setup", "pullback", 9.0)
        upsert_expectancy_stats(s, "setup_expectancy_stats", "setup", "pullback", 3.0)
        got = fetch_expectancy_stat(s, "setup_expectancy_stats", "setup", "pullback")
        assert isinstance(got, float)
        assert got == 6.0


def test_fetch_expectancy_missing_expectancy_value_returns_none():
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        s.execute(text("UPDATE setup_expectancy_stats SET samples = 0, total_pnl = 0 WHERE setup = 'missing'"))
        s.commit()
        assert fetch_expectancy_stat(s, "setup_expectancy_stats", "setup", "missing") is None
