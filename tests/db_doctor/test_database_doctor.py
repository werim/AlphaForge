from __future__ import annotations
import sqlite3
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from alphaforge.db_doctor import certify, diagnose, repair
from alphaforge.persistence import init_db, save_trade_lifecycle_event
from alphaforge.db_doctor.backup import snapshot_database
from alphaforge.db_doctor.writer_probes import run_writer_probes

ROOT=Path(__file__).resolve().parents[2]
def config(db):
    c=Config(str(ROOT/"alembic.ini")); c.set_main_option("script_location",str(ROOT/"alembic")); c.set_main_option("sqlalchemy.url",f"sqlite+pysqlite:///{db}"); return c

def broken_current_database(db):
    init_db(f"sqlite+pysqlite:///{db}").dispose()
    with sqlite3.connect(db) as conn:
        sql=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='trade_lifecycle_events'").fetchone()[0]
        conn.execute("ALTER TABLE trade_lifecycle_events RENAME TO trade_lifecycle_events_old")
        conn.execute(sql.replace("CREATE TABLE", "CREATE TABLE").replace("trade_lifecycle_events", "trade_lifecycle_events", 1).replace("id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGINT PRIMARY KEY"))
        conn.execute("INSERT INTO trade_lifecycle_events SELECT * FROM trade_lifecycle_events_old")
        conn.execute("DROP TABLE trade_lifecycle_events_old")
        conn.execute("CREATE UNIQUE INDEX ux_trade_lifecycle_event_id ON trade_lifecycle_events(event_id)")
        conn.execute("CREATE UNIQUE INDEX ux_lifecycle_signal_event_ts_state ON trade_lifecycle_events(signal_id,event_ts,lifecycle_state)")
        conn.execute("CREATE TABLE alembic_version(version_num VARCHAR(32) PRIMARY KEY)")
        conn.execute("INSERT INTO alembic_version VALUES('0007_repair_runtime_lifecycle_schema')")

def test_diagnose_missing_is_read_only(tmp_path):
    db=tmp_path/"absent.db"; result=diagnose(db)
    assert result["status"] == "BLOCKED" and not db.exists()
    assert result["issues"][0]["code"] == "DATABASE_IDENTITY_UNVERIFIED"

def test_fresh_bootstrap_is_structurally_healthy(tmp_path):
    db=tmp_path/"fresh.db"; init_db(f"sqlite+pysqlite:///{db}").dispose()
    assert diagnose(db)["status"] == "HEALTHY"

def test_exact_0001_failure_and_head_repair_preserve_legacy_row(tmp_path):
    db=tmp_path/"historical.db"; command.upgrade(config(db),"0001_phase1_init")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO exchange_symbols(id,venue,market_type,symbol,pair,contract_type,base_asset,quote_asset,margin_asset,status,price_precision,quantity_precision,tick_size,step_size,min_qty,min_notional,contract_size,raw_exchange_info_json) VALUES(1,'X','USDT_M','BTCUSDT','BTCUSDT','P','BTC','USDT','USDT','TRADING',1,1,1,1,1,1,1,'{}')")
        conn.execute("INSERT INTO selector_decisions(id,strategy_signal_id,decision,reason) VALUES(1,1,'ALLOW','legacy')")
        # Foreign keys are off in sqlite3 by default; this is preserved historical evidence.
        conn.execute("INSERT INTO order_intents(id,selector_decision_id,symbol_id,side,quantity) VALUES(1,1,1,'BUY',1)")
        conn.execute("INSERT INTO trade_lifecycle_events(id,order_intent_id,event_type,event_payload) VALUES(7,1,'LEGACY','{\"source\":\"0001\"}')")
    command.upgrade(config(db),"head")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT id,order_intent_id,event_type,event_payload FROM trade_lifecycle_events WHERE id=7").fetchone() == (7,1,"LEGACY",'{"source":"0001"}')
        assert "INTEGER PRIMARY KEY AUTOINCREMENT" in conn.execute("SELECT sql FROM sqlite_master WHERE name='trade_lifecycle_events'").fetchone()[0]
    engine=create_engine(f"sqlite+pysqlite:///{db}")
    with Session(engine) as session:
        assert save_trade_lifecycle_event(session,event_id="works",signal_id="works",symbol="BTCUSDT",mode="PAPER",lifecycle_state="SIGNAL_CREATED") is True

def test_0007_reproduces_bigint_primary_key_runtime_failure(tmp_path):
    db=tmp_path/"production-shape.db"; command.upgrade(config(db),"0007_repair_runtime_lifecycle_schema")
    before=diagnose(db); assert {i["code"] for i in before["issues"]} >= {"LIFECYCLE_PK_NOT_SQLITE_ROWID_COMPATIBLE","LIFECYCLE_NOT_NULL_WRITER_CONFLICT"}
    with Session(create_engine(f"sqlite+pysqlite:///{db}")) as session:
        with pytest.raises(RuntimeError,match="NOT NULL constraint failed: trade_lifecycle_events.id"):
            save_trade_lifecycle_event(session,event_id="fails",signal_id="fails",symbol="BTCUSDT",mode="PAPER",lifecycle_state="SIGNAL_CREATED")

def test_duplicate_identity_blocks_migration_without_loss(tmp_path):
    db=tmp_path/"dupe.db"; command.upgrade(config(db),"0007_repair_runtime_lifecycle_schema")
    with sqlite3.connect(db) as conn:
        conn.execute("DROP INDEX ux_trade_lifecycle_event_id")
        conn.execute("INSERT INTO trade_lifecycle_events(id,event_id,signal_id,event_type,event_payload,order_intent_id) VALUES(1,'same','a','X','{}',1),(2,'same','b','X','{}',2)")
    with pytest.raises(Exception,match="duplicate"):
        command.upgrade(config(db),"head")
    with sqlite3.connect(db) as conn: assert conn.execute("SELECT COUNT(*) FROM trade_lifecycle_events").fetchone()[0] == 2

def test_repair_backs_up_and_certifies_real_writers(tmp_path):
    db=tmp_path/"repair.db"; broken_current_database(db)
    result=repair(db); assert result["status"] == "REPAIRED" and Path(result["backup_path"]).is_file()
    assert result["writer_probes"]["passed"] is True
    assert {x["name"] for x in result["writer_probes"]["checks"]} >= {"save_trade_lifecycle_event_signal_created","save_trade_lifecycle_event_upsert","save_trade_lifecycle_event_signal_rejected","save_order_decision","runtime_heartbeat","runtime_state_snapshot"}

def test_backup_failure_prevents_source_mutation(monkeypatch,tmp_path):
    import alphaforge.db_doctor.repairs as repairs
    db=tmp_path/"no-backup.db"; command.upgrade(config(db),"0007_repair_runtime_lifecycle_schema")
    before=db.read_bytes()
    monkeypatch.setattr(repairs,"create_backup",lambda _path: (_ for _ in ()).throw(RuntimeError("backup unavailable")))
    with pytest.raises(RuntimeError,match="backup unavailable"): repairs.repair(db)
    assert db.read_bytes() == before

def test_failed_migration_retains_validated_backup(monkeypatch,tmp_path):
    import alphaforge.db_doctor.repairs as repairs
    db=tmp_path/"failed.db"; command.upgrade(config(db),"0007_repair_runtime_lifecycle_schema")
    monkeypatch.setattr(repairs.command,"upgrade",lambda *_args,**_kwargs: (_ for _ in ()).throw(RuntimeError("migration failed")))
    result=repairs.repair(db)
    assert result["status"] == "REPAIR_FAILED" and Path(result["backup_path"]).is_file()
    with sqlite3.connect(result["backup_path"]) as conn: assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)

def test_certification_fails_when_real_probe_fails(monkeypatch,tmp_path):
    import alphaforge.db_doctor.verifier as verifier
    db=tmp_path/"structural.db"; init_db(f"sqlite+pysqlite:///{db}").dispose()
    monkeypatch.setattr(verifier,"run_writer_probes",lambda _path:{"passed":False,"checks":[],"error":"injected actual writer failure"})
    result=verifier.certify(db)
    assert result["status"] == "NOT_CERTIFIED"
    assert result["diagnosis"]["issues"][-1]["code"] == "WRITER_PROBE_FAILED"

def test_writer_probe_uses_online_backup_with_committed_wal_and_never_contaminates_source(tmp_path):
    db=tmp_path/"wal.db"; init_db(f"sqlite+pysqlite:///{db}").dispose()
    keeper=sqlite3.connect(db)
    keeper.execute("PRAGMA journal_mode=WAL"); keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute("CREATE TRIGGER wal_probe_seen BEFORE INSERT ON trade_lifecycle_events WHEN NEW.symbol='DBDOCTOR' BEGIN SELECT RAISE(ABORT,'committed WAL trigger observed'); END")
    keeper.commit()
    assert Path(str(db)+"-wal").stat().st_size > 0
    before=keeper.execute("SELECT COUNT(*) FROM trade_lifecycle_events").fetchone()[0]
    result=run_writer_probes(db)
    assert result["passed"] is False and "committed WAL trigger observed" in result["error"]
    assert keeper.execute("SELECT COUNT(*) FROM trade_lifecycle_events").fetchone()[0] == before
    keeper.close()

@pytest.mark.parametrize("object_kind",["column","index","trigger"])
def test_unknown_lifecycle_schema_blocks_before_destructive_mutation(tmp_path,object_kind):
    db=tmp_path/f"unknown-{object_kind}.db"; command.upgrade(config(db),"0007_repair_runtime_lifecycle_schema")
    with sqlite3.connect(db) as conn:
        if object_kind == "column":
            conn.execute("ALTER TABLE trade_lifecycle_events ADD COLUMN deployed_evidence TEXT")
            conn.execute("INSERT INTO trade_lifecycle_events(id,order_intent_id,event_type,event_payload,deployed_evidence) VALUES(9,1,'LEGACY','{}','preserve-me')")
        elif object_kind == "index": conn.execute("CREATE INDEX deployed_semantic_index ON trade_lifecycle_events(event_type)")
        else: conn.execute("CREATE TRIGGER deployed_audit_trigger AFTER INSERT ON trade_lifecycle_events BEGIN SELECT 1; END")
    with pytest.raises(Exception,match="unsupported lifecycle schema blocked before mutation"):
        command.upgrade(config(db),"head")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0007_repair_runtime_lifecycle_schema"
        if object_kind == "column": assert conn.execute("SELECT deployed_evidence FROM trade_lifecycle_events WHERE id=9").fetchone() == ("preserve-me",)
        elif object_kind == "index": assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='deployed_semantic_index'").fetchone() == (1,)
        else: assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='deployed_audit_trigger'").fetchone() == (1,)

def test_value_level_legacy_evidence_survives_rebuild(tmp_path):
    db=tmp_path/"values.db"; command.upgrade(config(db),"0007_repair_runtime_lifecycle_schema")
    with sqlite3.connect(db) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute("INSERT INTO trade_lifecycle_events(id,order_intent_id,event_type,event_payload,event_id,signal_id,payload,score,rr) VALUES(17,42,'LEGACY_RAW','{\"bytes\":\"00ff\"}','evt-17','sig-17','{\"nested\":[null,1]}',7.125,1.375)")
        before=dict(conn.execute("SELECT * FROM trade_lifecycle_events WHERE id=17").fetchone())
    command.upgrade(config(db),"head")
    with sqlite3.connect(db) as conn:
        conn.row_factory=sqlite3.Row
        assert dict(conn.execute("SELECT * FROM trade_lifecycle_events WHERE id=17").fetchone()) == before

def test_repair_fails_closed_when_post_migration_writer_probe_fails(monkeypatch,tmp_path):
    import alphaforge.db_doctor.repairs as repairs
    db=tmp_path/"probe-fail.db"; broken_current_database(db)
    monkeypatch.setattr(repairs,"run_writer_probes",lambda _path:{"passed":False,"checks":[],"error":"injected"})
    result=repairs.repair(db)
    assert result["status"] == "VERIFICATION_FAILED" and Path(result["backup_path"]).is_file()
    assert result["writer_probes"]["passed"] is False and result["recommended_action"]

def test_orm_findings_do_not_block_unrelated_lifecycle_repair(tmp_path):
    db=tmp_path/"orm-warning-repair.db"; broken_current_database(db)
    before=diagnose(db)
    assert "ORM_ALEMBIC_CONTRACT_MISMATCH" in {i["code"] for i in before["repository_findings"]}
    assert all("repair" not in i["blocks"] for i in before["repository_findings"] if i["code"]=="ORM_ALEMBIC_CONTRACT_MISMATCH")
    assert repair(db)["status"]=="REPAIRED"

def test_duplicate_lifecycle_evidence_is_explicit_repair_blocker(tmp_path):
    db=tmp_path/"duplicate-blocker.db"; command.upgrade(config(db),"0007_repair_runtime_lifecycle_schema")
    with sqlite3.connect(db) as conn:
        conn.execute("DROP INDEX ux_trade_lifecycle_event_id")
        conn.execute("INSERT INTO trade_lifecycle_events(id,event_id,signal_id,event_type,event_payload,order_intent_id) VALUES(1,'same','a','X','{}',1),(2,'same','b','X','{}',2)")
    result=repair(db)
    assert result["status"]=="BLOCKED_MANUAL_REVIEW" and result["backup_path"] is None
    assert any(i["code"]=="LIFECYCLE_DUPLICATE_EVENT_ID" and "repair" in i["blocks"] for i in result["before"]["issues"])

def test_target_conflict_blocks_repair_before_backup(monkeypatch,tmp_path):
    import alphaforge.db_doctor.repairs as repairs
    db=tmp_path/"target-conflict.db"; init_db(f"sqlite+pysqlite:///{db}").dispose()
    monkeypatch.setenv("ALPHAFORGE_DATABASE_URL","postgresql://db/other")
    monkeypatch.setattr(repairs,"create_backup",lambda _path:pytest.fail("backup must not run"))
    result=repairs.repair(db)
    assert result["status"]=="BLOCKED_MANUAL_REVIEW"
    assert any(i["code"]=="DATABASE_TARGET_CONFLICT" and "repair" in i["blocks"] for i in result["before"]["issues"])
