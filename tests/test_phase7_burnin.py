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


def test_attached_paper_execution_persists_guided_pending_position(tmp_path: Path):
    import asyncio, json
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from alphaforge.ai_brain import AIBrain
    from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
    from alphaforge.persistence import init_db
    from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator

    engine=init_db(f"sqlite+pysqlite:///{tmp_path / 'attached.sqlite'}")
    with engine.begin() as conn:
        campaign=create_campaign(conn,release_id='guided',duration_days=7,symbols=['BTCUSDT'],intervals=['1h','15m','1m'])
        run=start_or_resume_campaign(conn,campaign.campaign_id)
    orch=RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),ai_brain=AIBrain(Session(engine)),market_scanner=lambda:asyncio.sleep(0,result=[]),persistence_engine=engine,scanner_source='EXCHANGE_PUBLIC_MARKET_DATA')
    orch._campaign_id=campaign.campaign_id; orch._burnin_run_id=run['burnin_run_id']
    execution_ctx={'spread_pct':.0002,'expected_slippage_pct':.0002,'fee_pct':.0004,'funding_rate_pct':.0,'latency_ms':10,'liquidity_score':.9,'volatility_regime':'normal'}
    mtf={'regime':{'regime':'LONG'},'setup':{'phase':'PULLBACK'},'execution':{'direction':'LONG'}}
    asyncio.run(orch._execute('BTCUSDT',{'signal_id':'guided-signal','order_type':'MARKET'},{'source_exchange':'binance','side':'LONG','entry':100,'sl':99,'tp':102,'rr':2,'execution_ctx':execution_ctx,'mtf':mtf}))
    with engine.connect() as conn:
        row=conn.execute(text("SELECT signal_id,side,stop,target,source_provenance_json,status FROM burnin_pending_position_outcomes")).mappings().one()
    provenance=json.loads(row['source_provenance_json'])
    assert row['signal_id']=='guided-signal' and row['side']=='LONG' and row['status']=='OPEN'
    assert (row['stop'],row['target'])==(99,102)
    assert provenance['setup_phase']=='PULLBACK' and provenance['execution_direction']=='LONG'


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


def test_persist_burnin_run_rejects_duplicate_run_id(tmp_path: Path):
    import pytest
    import sqlite3
    conn=sqlite3.connect(tmp_path/"dup.db")
    bootstrap_burnin_schema(conn)
    run=BurnInRun(burnin_run_id="rdup",release_id="rel",execution_mode="PAPER",git_commit="abc",config_hash=config_hash({"a":1}),strategy_config_hash=config_hash({"s":1}),universe_hash=universe_hash(["BTCUSDT"],["5m"]),source_provenance={"provider":"BINANCE_READONLY"})
    persist_burnin_run(conn, run)
    with pytest.raises(sqlite3.IntegrityError):
        persist_burnin_run(conn, run)


def test_continuation_sequence_allocates_without_overwriting(tmp_path: Path):
    import sqlite3
    from alphaforge.burnin import next_burnin_continuation_sequence
    conn=sqlite3.connect(tmp_path/"seq.db")
    bootstrap_burnin_schema(conn)
    for expected in [0,1]:
        seq=next_burnin_continuation_sequence(conn, release_id="rel", execution_mode="PAPER")
        assert seq == expected
        run=BurnInRun(burnin_run_id=f"phase7:rel:PAPER:{seq}",release_id="rel",execution_mode="PAPER",continuation_sequence=seq,git_commit="abc",config_hash=config_hash({"a":1}),strategy_config_hash=config_hash({"s":1}),universe_hash=universe_hash(["BTCUSDT"],["5m"]),source_provenance={"provider":"BINANCE_READONLY"},sample_count=99 if seq==0 else 0,end_time="2026-01-01T00:00:00Z" if seq==0 else None)
        persist_burnin_run(conn, run)
    rows=conn.execute("SELECT burnin_run_id, continuation_sequence, sample_count, end_time FROM burnin_runs ORDER BY continuation_sequence").fetchall()
    assert rows[0][0] == "phase7:rel:PAPER:0"
    assert rows[0][1] == 0 and rows[0][2] == 99 and rows[0][3] == "2026-01-01T00:00:00Z"
    assert rows[1][0] == "phase7:rel:PAPER:1"


def test_unique_release_mode_sequence_blocks_duplicate_sequence(tmp_path: Path):
    import pytest
    import sqlite3
    conn=sqlite3.connect(tmp_path/"uniq.db")
    bootstrap_burnin_schema(conn)
    base={"release_id":"rel","execution_mode":"LIVE_PRECHECK","continuation_sequence":0,"git_commit":"abc","config_hash":config_hash({"a":1}),"strategy_config_hash":config_hash({"s":1}),"universe_hash":universe_hash(["BTCUSDT"],["5m"]),"source_provenance":{"provider":"BINANCE_READONLY"},"parent_burnin_run_id":"paper0","parent_qualification_id":"q0"}
    persist_burnin_run(conn, BurnInRun(burnin_run_id="lp0", **base))
    with pytest.raises(sqlite3.IntegrityError):
        persist_burnin_run(conn, BurnInRun(burnin_run_id="lp0-other", **base))


def test_runtime_second_paper_startup_creates_sequence_one(tmp_path: Path):
    import asyncio
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from alphaforge.ai_brain import AIBrain
    from alphaforge.persistence import init_db
    from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
    db=tmp_path/"paperseq.sqlite"
    engine=init_db(f"sqlite+pysqlite:///{db}")
    for _ in range(2):
        orch=RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, phase7_burnin_release_id="rel"), ai_brain=AIBrain(Session(engine)), market_scanner=lambda: asyncio.sleep(0, result=[]), persistence_engine=engine, scanner_source="PAPER_RUNTIME")
        orch._start_or_resume_burnin_run()
    with engine.connect() as c:
        rows=c.execute(text("SELECT burnin_run_id, continuation_sequence FROM burnin_runs WHERE release_id='rel' AND execution_mode='PAPER' ORDER BY continuation_sequence")).fetchall()
    assert [r[1] for r in rows] == [0,1]


def test_runtime_live_precheck_second_startup_sequence_one_and_parent_immutable(tmp_path: Path):
    import asyncio, json
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from alphaforge.ai_brain import AIBrain
    from alphaforge.persistence import init_db
    from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
    db=tmp_path/"lpseq.sqlite"
    engine=init_db(f"sqlite+pysqlite:///{db}")
    # Seed immutable qualified PAPER lineage.
    with engine.begin() as c:
        bootstrap_burnin_schema(c)
        paper=BurnInRun(burnin_run_id="phase7:rel:PAPER:0",release_id="rel",execution_mode="PAPER",continuation_sequence=0,git_commit="abc",config_hash=config_hash({"a":1}),strategy_config_hash=config_hash({"s":1}),universe_hash=universe_hash(["BTCUSDT"],["5m"]),source_provenance={"provider":"BINANCE_READONLY"},sample_count=77,end_time="2026-01-01T00:00:00Z")
        persist_burnin_run(c, paper)
        c.execute(text("INSERT INTO burnin_qualification_snapshots(qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,expectancy_status,execution_status,regime_status,reject_quality_status,calibration_status,drawdown_status,concentration_status,reconciliation_status,evidence_completeness_status,blockers_json,warnings_json,thresholds_json,metrics_json,evidence_hash,schema_version) VALUES ('q0','phase7:rel:PAPER:0','rel','now','CANARY_QUALIFIED','PASS','PASS','PASS','PASS','PASS','PASS','PASS','PASS','PASS','PASS','[]','[]','{}','{}','h','v')"))
    for _ in range(2):
        orch=RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.LIVE_PRECHECK, phase7_burnin_release_id="rel"), ai_brain=AIBrain(Session(engine)), market_scanner=lambda: asyncio.sleep(0, result=[]), persistence_engine=engine, scanner_source="EXCHANGE_PUBLIC_MARKET_DATA")
        orch._start_or_resume_burnin_run()
    with engine.connect() as c:
        lp=c.execute(text("SELECT continuation_sequence,parent_burnin_run_id,parent_qualification_id FROM burnin_runs WHERE release_id='rel' AND execution_mode='LIVE_PRECHECK' ORDER BY continuation_sequence")).fetchall()
        paper_after=c.execute(text("SELECT sample_count,end_time FROM burnin_runs WHERE burnin_run_id='phase7:rel:PAPER:0'")).first()
    assert [r[0] for r in lp] == [0,1]
    assert all(r[1] == "phase7:rel:PAPER:0" and r[2] == "q0" for r in lp)
    assert paper_after[0] == 77 and paper_after[1] == "2026-01-01T00:00:00Z"
