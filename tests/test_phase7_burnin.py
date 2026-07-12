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


def test_entry_fill_does_not_create_closed_burnin_outcome(tmp_path: Path):
    import asyncio
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from alphaforge.ai_brain import AIBrain
    from alphaforge.persistence import init_db
    from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
    db=tmp_path/"rt.sqlite"
    engine=init_db(f"sqlite+pysqlite:///{db}")
    orch=RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.PAPER), ai_brain=AIBrain(Session(engine)), market_scanner=lambda: asyncio.sleep(0, result=[]), persistence_engine=engine, scanner_source="PAPER_RUNTIME")
    orch.metrics.persistence_enabled=True
    orch._start_or_resume_burnin_run()
    orch._persist_burnin_decision({"signal_id":"s1","symbol":"BTCUSDT","decision":"ACCEPTED","execution_ctx":{"spread_pct":.01,"expected_slippage_pct":.01,"fee_pct":.001,"funding_rate_pct":.0,"market_data_latency_ms":10}}, lifecycle_state="ORDER_PLACED")
    asyncio.run(orch._execute("BTCUSDT", {"order_type":"MARKET"}, {"entry":100,"rr":2,"execution_ctx":{"spread_pct":.01,"expected_slippage_pct":.01,"fee_pct":.001,"funding_rate_pct":.0,"market_data_latency_ms":10}}))
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM burnin_trade_outcomes")).scalar_one() == 0
        row=c.execute(text("SELECT closed_trade_count, open_trade_count FROM burnin_runs WHERE burnin_run_id=:bid"), {"bid": orch._burnin_run_id}).first()
    assert row[0] == 0
    assert row[1] == 1


def test_position_closed_creates_one_realized_burnin_outcome(tmp_path: Path):
    import asyncio
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from alphaforge.ai_brain import AIBrain
    from alphaforge.persistence import init_db
    from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
    db=tmp_path/"rt2.sqlite"
    engine=init_db(f"sqlite+pysqlite:///{db}")
    orch=RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.PAPER), ai_brain=AIBrain(Session(engine)), market_scanner=lambda: asyncio.sleep(0, result=[]), persistence_engine=engine, scanner_source="PAPER_RUNTIME")
    orch.metrics.persistence_enabled=True
    orch._start_or_resume_burnin_run()
    orch._persist_burnin_decision({"signal_id":"s1","symbol":"BTCUSDT","decision":"ACCEPTED","execution_ctx":{}}, lifecycle_state="ORDER_PLACED")
    orch._persist_burnin_closed_trade_from_lifecycle("BTCUSDT", {"trade_id":"t1","gross_pnl":10,"gross_r":1.5,"entry_spread_cost":.01,"entry_slippage_cost":.02,"exit_slippage_cost":.03,"fee_cost":.01,"funding_cost":.0,"latency_cost":.001,"net_pnl":9.929,"net_r":1.429,"mfe":2.0,"mae":-.5,"hold_duration_seconds":300,"exit_reason":"TP_HIT"})
    with engine.connect() as c:
        rows=c.execute(text("SELECT gross_r, net_r, exit_reason FROM burnin_trade_outcomes")).fetchall()
        run=c.execute(text("SELECT closed_trade_count, open_trade_count FROM burnin_runs WHERE burnin_run_id=:bid"), {"bid": orch._burnin_run_id}).first()
    assert len(rows)==1
    assert rows[0][0] == 1.5 and rows[0][1] == 1.429 and rows[0][2] == "TP_HIT"
    assert run[0] == 1 and run[1] == 0
