from __future__ import annotations

import sqlite3
from pathlib import Path

from alphaforge.burnin import BurnInRun, bootstrap_burnin_schema, config_hash, persist_burnin_run, universe_hash, latest_burnin_snapshot


def test_phase7_schema_additive_and_read_no_evidence(tmp_path: Path):
    db=tmp_path/"b.db"
    conn=sqlite3.connect(db)
    bootstrap_burnin_schema(conn)
    conn.commit()
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"burnin_runs","burnin_trade_outcomes","burnin_qualification_snapshots","burnin_suspension_events"}.issubset(tables)
    assert latest_burnin_snapshot(conn)["status"] == "UNAVAILABLE"
    conn.close()


def test_burnin_run_rejects_forbidden_modes_and_missing_provenance():
    run=BurnInRun(burnin_run_id="r", release_id="rel", execution_mode="BACKTEST")
    try:
        run.validate()
    except ValueError as exc:
        assert "PAPER or LIVE_PRECHECK" in str(exc)
    else:
        raise AssertionError("BACKTEST evidence must not validate")


def test_persist_burnin_run_with_deterministic_hashes(tmp_path: Path):
    conn=sqlite3.connect(tmp_path/"b.db")
    bootstrap_burnin_schema(conn)
    run=BurnInRun(burnin_run_id="r1",release_id="rel",execution_mode="PAPER",git_commit="abc",config_hash=config_hash({"b":2,"a":1}),strategy_config_hash=config_hash({"x":1}),universe_hash=universe_hash(["ETHUSDT","BTCUSDT"],["5m"]),source_provenance={"provider":"BINANCE_READONLY"},symbols=["BTCUSDT","ETHUSDT"],intervals=["5m"])
    persist_burnin_run(conn,run)
    row=conn.execute("SELECT config_hash, universe_hash FROM burnin_runs WHERE burnin_run_id='r1'").fetchone()
    assert row[0] == config_hash({"a":1,"b":2})
    assert row[1] == universe_hash(["BTCUSDT","ETHUSDT"],["5m"])
