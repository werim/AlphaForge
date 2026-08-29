from __future__ import annotations
import sqlite3
import pytest
from alphaforge.db_doctor.diagnostics import diagnose
from alphaforge.db_doctor.targets import parse_target,resolve_database_targets
from alphaforge.persistence import init_db

@pytest.mark.parametrize("values",[
    (r"C:\\data\\alpha.db","sqlite:///C:/data/alpha.db","sqlite+pysqlite:///C:/data/alpha.db"),
    ("/tmp/../tmp/alpha.db","sqlite:////tmp/alpha.db","sqlite+pysqlite:////tmp/alpha.db"),
])
def test_equivalent_paths_share_identity(values):
    assert len({parse_target(v)["canonical_identity"] for v in values}) == 1

def test_sqlite_postgres_target_conflict_preserves_both():
    result=resolve_database_targets("sqlite:///alpha.db",{"ALPHAFORGE_DATABASE_URL":"postgresql://db/alpha"})
    assert result["conflict"] and {c["dialect"] for c in result["candidates"]}=={"sqlite","postgresql"}
    assert all({"source","raw_value","dialect","canonical_identity"} <= set(c) for c in result["candidates"])

def test_missing_database_has_complete_shape(tmp_path):
    result=diagnose(tmp_path/"missing.db")
    for key in ("database_target_resolution","dialect","SQLite_features","schema_contracts","schema_ownership","ORM_alignment","exposure","writer_compatibility","unsafe_data","recommended_repairs"):
        assert key in result

def test_exposure_never_zero_and_unknown_evidence_preserved(tmp_path):
    db=tmp_path/"split.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("CREATE TABLE positions(id,position_id,symbol,qty,status);CREATE TABLE orders(id,order_id,symbol,status);CREATE TABLE runtime_positions(id,symbol,qty,status);CREATE TABLE runtime_orders(id,order_id,symbol,status);CREATE TABLE deployed_evidence(payload);INSERT INTO positions VALUES(1,'p','BTC',1,'OPEN');INSERT INTO runtime_positions VALUES(1,'ETH',2,'OPEN');INSERT INTO deployed_evidence VALUES('preserve');")
    before=db.read_bytes(); result=diagnose(db)
    assert result["exposure"]["classification"]=="CONFLICTING_EXPOSURE" and result["exposure"]["unknown_is_zero"] is False
    assert "EXPOSURE_MULTIPLE_ACTIVE_SOURCES" in {i["code"] for i in result["issues"]}
    assert db.read_bytes()==before

def test_json_capability_failure_is_fail_closed(monkeypatch,tmp_path):
    import alphaforge.db_doctor.diagnostics as module
    db=tmp_path/"db.sqlite"; sqlite3.connect(db).close()
    real=module.inspect_database
    def failed(path):
        value=real(path); value["json1"]=False; return value
    monkeypatch.setattr(module,"inspect_database",failed)
    assert "SQLITE_JSON1_UNAVAILABLE" in {i["code"] for i in diagnose(db)["issues"]}

def test_init_db_reports_real_orm_and_owner_drift(tmp_path):
    db=tmp_path/"init.db"; init_db(f"sqlite+pysqlite:///{db}").dispose()
    result=diagnose(db)
    assert result["ORM_alignment"]["autogenerate_safe"] is False
    assert any(m["table"]=="exchange_symbols" for m in result["ORM_alignment"]["mismatches"])
    assert "ORM_ALEMBIC_CONTRACT_MISMATCH" in {i["code"] for i in result["issues"]}
    assert "INCOMPATIBLE_OWNER_CONTRACTS" in {i["code"] for i in result["issues"]}
    assert result["status"]=="HEALTHY" and not result["runtime_blockers"]
    assert "MULTIPLE_SCHEMA_OWNERS" in {i["code"] for i in result["repository_findings"]}
    orm_issue=next(i for i in result["issues"] if i["code"]=="ORM_ALEMBIC_CONTRACT_MISMATCH")
    assert orm_issue["blocks"]==["alembic_autogenerate"]

def test_architecture_findings_do_not_skip_writer_probe(monkeypatch,tmp_path):
    import alphaforge.db_doctor.verifier as verifier
    db=tmp_path/"init.db"; init_db(f"sqlite+pysqlite:///{db}").dispose(); called=[]
    monkeypatch.setattr(verifier,"run_writer_probes",lambda path:(called.append(path) or {"passed":True,"checks":[],"error":None}))
    result=verifier.certify(db)
    assert called==[db] and result["runtime_certification"]["status"]=="DATABASE_CERTIFIED"
    assert result["repository_audit"]["status"]=="FINDINGS"

def test_target_conflict_blocks_certification(monkeypatch,tmp_path):
    import alphaforge.db_doctor.verifier as verifier
    db=tmp_path/"init.db"; init_db(f"sqlite+pysqlite:///{db}").dispose()
    monkeypatch.setenv("ALPHAFORGE_DATABASE_URL","postgresql://db/alpha")
    monkeypatch.setattr(verifier,"run_writer_probes",lambda _path:pytest.fail("probe must not run"))
    result=verifier.certify(db)
    assert result["status"]=="NOT_CERTIFIED"
    assert "DATABASE_TARGET_CONFLICT" in {i["code"] for i in result["runtime_certification"]["blockers"]}
