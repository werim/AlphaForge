from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import threading
import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from alphaforge.burnin import BurnInRun, bootstrap_burnin_schema, persist_burnin_run
from alphaforge.burnin_campaign import bootstrap_campaign_schema, build_phase8_campaign_identity
from alphaforge.config import load_config_from_env
from alphaforge.persistence import init_db
from alphaforge.runtime_heartbeat import save_runtime_heartbeat, fetch_latest_runtime_heartbeat
from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot, latest_runtime_state_snapshot
from alphaforge.sqlite_safety import SQLiteBusyExhausted, run_sqlite_write_with_retry
from alphaforge.storage_policy import (
    StorageController, StoragePolicy, archive_location, campaign_eligibility, checkpoint,
    disk_budget, inventory, plan_telemetry, prune_telemetry, readonly,
    remove_archived_batch, verified_archive, verify_manifest,
)
from alphaforge.universe_evidence import UNIVERSE_SELECTION_DDL

CID = 'camp_6200000000000001'
RUN = CID + '_run_0000'


def policy(**changes):
    return replace(StoragePolicy.from_config(load_config_from_env(env={}).runtime), **changes)


@pytest.fixture
def db(tmp_path):
    engine = init_db(f'sqlite+pysqlite:///{tmp_path / "retention.db"}')
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(mode='PAPER', requested_mode='PAPER', actual_mode='PAPER', runtime_status='STOPPED', instance_id='schema'))
    yield engine
    engine.dispose()


def heartbeats(engine, count=30, *, instance='standalone', state='OPERATING', payload=None):
    for i in range(count):
        save_runtime_heartbeat(engine, runtime_instance_id=instance, execution_mode='PAPER', scanner_source='EXCHANGE_PUBLIC_MARKET_DATA', runtime_state=state, heartbeat_ts=f'2020-01-01T00:00:{i % 60:02d}Z', active_positions_count=0, pending_orders_count=0, payload=payload or {})


def terminal(engine, *, cycles=3):
    with engine.begin() as conn:
        bootstrap_burnin_schema(conn)
        bootstrap_campaign_schema(conn)
        persist_burnin_run(conn, BurnInRun(burnin_run_id=RUN, release_id='issue620', execution_mode='PAPER', phase='PHASE8', status='COMPLETED', git_commit='sha620', config_hash='cfg620', strategy_config_hash='strategy620', universe_hash='universe620', source_provenance={'provider':'BINANCE'}, symbols=['BTCUSDT'], intervals=['1m']))
        conn.execute(text("""INSERT INTO burnin_campaigns(campaign_id,release_id,campaign_status,created_at,completed_at,active_run_id,config_hash,strategy_config_hash,universe_hash,git_commit,source_provenance_json,symbols_json,intervals_json,schema_version) VALUES (:cid,'issue620','COMPLETED','2020-01-01','2020-01-02',:run,'cfg620','strategy620','universe620','sha620','{}','["BTCUSDT"]','["1m"]','phase8_campaign_v1')"""), {'cid':CID,'run':RUN})
        conn.execute(text("""INSERT INTO burnin_campaign_runs(campaign_id,burnin_run_id,continuation_sequence,status,started_at,ended_at,created_at,schema_version) VALUES (:cid,:run,0,'COMPLETED','2020-01-01','2020-01-02','2020-01-01','phase8_campaign_v1')"""), {'cid':CID,'run':RUN})
        for sql in UNIVERSE_SELECTION_DDL:
            conn.exec_driver_sql(sql)
        for i in range(cycles):
            c = f'cycle-{i}'
            conn.exec_driver_sql("""INSERT INTO universe_selection_cycles(cycle_id,decision_timestamp,execution_mode,selected_symbols_json,candidate_count,config_hash,universe_hash,evidence_hash,git_sha,source_provenance_json,ranking_version,schema_version,payload_json) VALUES (?,1,'PAPER','["BTCUSDT"]',1,'cfg620','universe620',?,'sha620','[]','v1','v1',?)""", (c, f'hash-{i}', json.dumps({'cycle':c,'padding':'x' * 30000})))
            conn.exec_driver_sql("INSERT INTO universe_selection_candidates VALUES (?,0,'BTCUSDT','ELIGIBLE','[]','{}','{}',1,1,1,'{}')", (c,))
            conn.exec_driver_sql("INSERT INTO burnin_universe_selection_links(link_id,campaign_id,burnin_run_id,cycle_id,decision_timestamp,schema_version) VALUES (?,?,?,?,1,'v1')", (f'link-{i}',CID,RUN,c))
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(mode='PAPER', requested_mode='PAPER', actual_mode='PAPER', runtime_status='STOPPED', instance_id='terminal', campaign_id=CID, burnin_run_id=RUN, unknown_exchange_state=False))


def archive_policy(tmp_path, **changes):
    directory = tmp_path / 'archives'
    directory.mkdir(exist_ok=True)
    return policy(archive_dir=str(directory), **changes)


def test_config_is_validated_and_identity_bound():
    default = load_config_from_env(env={}).runtime
    assert default.storage_high_bytes == 1024 ** 3
    assert default.storage_low_bytes == 700 * 1024 ** 2
    with pytest.raises(ValueError, match='high_bytes'):
        load_config_from_env(env={'ALPHAFORGE_STORAGE_HIGH_BYTES':'100','ALPHAFORGE_STORAGE_LOW_BYTES':'100'})
    with pytest.raises(ValueError):
        load_config_from_env(env={'ALPHAFORGE_STORAGE_BATCH_ROWS':'0'})
    a = build_phase8_campaign_identity(default, ['BTCUSDT'], ['1m'])
    b = build_phase8_campaign_identity(replace(default, storage_batch_rows=22), ['BTCUSDT'], ['1m'])
    assert a['config_hash'] != b['config_hash']
    assert a['strategy_config_hash'] == b['strategy_config_hash']
    assert a['execution_cost_config_hash'] == b['execution_cost_config_hash']


def test_inventory_is_readonly_and_physical_accounting_is_truthful(db):
    heartbeats(db)
    path = Path(db.url.database)
    with readonly(path) as conn:
        before = conn.execute('SELECT COUNT(*) FROM runtime_heartbeats').fetchone()[0]
        schema = conn.execute('PRAGMA schema_version').fetchone()[0]
    report = inventory(path, policy())
    assert report['physical_bytes'] == report['main_bytes'] + report['wal_bytes']
    assert report['reusable_bytes'] == report['freelist_count'] * report['page_size']
    assert report['database_path'] == str(path.resolve())
    assert report['allocations'] or report['allocation_unavailable_reason']
    with readonly(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM runtime_heartbeats').fetchone()[0] == before
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == schema


def test_below_threshold_never_deletes(db):
    heartbeats(db)
    report = StorageController(db, policy()).check(force=True)
    assert report['rows_removed'] == 0
    assert report['status'] == 'TARGET_REACHED'
    assert fetch_latest_runtime_heartbeat(db)['id'] == 30


def test_threshold_cleanup_is_bounded_and_latest_recovery_survives(db):
    heartbeats(db)
    heartbeats(db, 1, state='RECOVERY_REQUIRED', instance='unsafe')
    controller = StorageController(db, policy(high_bytes=100000,low_bytes=1,batch_rows=5,batch_seconds=2))
    before = latest_runtime_state_snapshot(db)
    result = controller.check(force=True)
    assert result['rows_removed'] == 5
    with db.connect() as conn:
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM runtime_heartbeats').scalar_one() == 26
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM runtime_heartbeats WHERE runtime_state='RECOVERY_REQUIRED'").scalar_one() == 1
    assert latest_runtime_state_snapshot(db) == before
    assert fetch_latest_runtime_heartbeat(db)['runtime_instance_id'] == 'unsafe'


def test_campaign_bound_heartbeat_and_safety_rows_unchanged(db):
    heartbeats(db, instance='bound')
    save_runtime_state_snapshot(db, RuntimeStateSnapshot(mode='PAPER',requested_mode='PAPER',actual_mode='PAPER',runtime_status='RECOVERY_REQUIRED',instance_id='bound',campaign_id=CID,recovery_action_required=True))
    with db.begin() as conn:
        conn.exec_driver_sql("INSERT INTO positions(position_id,status) VALUES ('open','OPEN')")
        conn.exec_driver_sql("INSERT INTO orders(order_id,status) VALUES ('pending','NEW')")
    result = prune_telemetry(db, policy(batch_seconds=2))
    assert result['rows_removed'] == 0
    with db.connect() as conn:
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM runtime_heartbeats').scalar_one() == 30
        assert conn.exec_driver_sql('SELECT status FROM positions').scalar_one() == 'OPEN'
        assert conn.exec_driver_sql('SELECT status FROM orders').scalar_one() == 'NEW'


def test_reusable_capacity_prevents_delete_churn(db, monkeypatch):
    heartbeats(db)
    p = policy(high_bytes=100000, low_bytes=1, batch_rows=100, batch_seconds=2)
    prune_telemetry(db, p)
    with db.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE retained_capacity_fixture(payload BLOB)")
        conn.exec_driver_sql("INSERT INTO retained_capacity_fixture VALUES (zeroblob(2097152))")
        conn.exec_driver_sql("DROP TABLE retained_capacity_fixture")
    checkpoint(db)
    measured = inventory(db.url.database, p, detailed=False)
    p = replace(p, low_bytes=measured['allocated_live_bytes'] + 1, high_bytes=measured['allocated_live_bytes'] + 2)
    monkeypatch.setattr('alphaforge.storage_policy.prune_telemetry', lambda *a: pytest.fail('unexpected repeated deletion'))
    result = StorageController(db,p).check(force=True)
    assert result['rows_removed'] == 0
    assert not result['blocked']
    assert result['status'] == 'REUSABLE_CAPACITY'


def test_archive_is_verified_before_bounded_removal_and_replayable(db,tmp_path):
    terminal(db)
    p = archive_policy(tmp_path,batch_rows=3,batch_seconds=2)
    before = inventory(db.url.database,p,detailed=False)
    m = verified_archive(db,p,CID)
    assert verify_manifest(m) == Path(m['archive_path'])
    result = remove_archived_batch(db,p,m)
    assert result['rows_removed'] == 3
    assert result['state'] == 'REMOVING'
    with readonly(Path(db.url.database)) as conn:
        location = archive_location(conn,CID)
        assert location['status'] == 'ARCHIVED_EVIDENCE_REQUIRES_REPLAY'
        assert conn.execute('SELECT COUNT(*) FROM universe_selection_cycles').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM storage_archive_authorizations').fetchone()[0] == 0
    with readonly(Path(location['archive_path'])) as conn:
        assert conn.execute('SELECT COUNT(*) FROM universe_selection_cycles').fetchone()[0] == 3
        assert conn.execute('SELECT COUNT(*) FROM burnin_runs').fetchone()[0] == 1
    # Restart uses the already verified durable snapshot; no overwrite/archive
    # duplication and no second deletion of a previously removed cycle.
    m2 = verified_archive(db,p,CID)
    assert m2 == m
    assert remove_archived_batch(db,p,m)['rows_removed'] == 3
    assert remove_archived_batch(db,p,m)['rows_removed'] == 3
    assert remove_archived_batch(db,p,m)['rows_removed'] == 0
    after = inventory(db.url.database,p,detailed=False)
    assert after['reusable_bytes'] > before['reusable_bytes']
    print(json.dumps({'before_live_bytes':before['allocated_live_bytes'],'after_live_bytes':after['allocated_live_bytes'],'before_reusable_bytes':before['reusable_bytes'],'after_reusable_bytes':after['reusable_bytes'],'rows_removed':9}))


def test_ordinary_universe_deletes_updates_and_forged_authorizations_fail(db,tmp_path):
    terminal(db)
    m = verified_archive(db,archive_policy(tmp_path),CID)
    for table in ('universe_selection_cycles','universe_selection_candidates','burnin_universe_selection_links'):
        with pytest.raises(IntegrityError,match='immutable'):
            with db.begin() as conn:
                conn.exec_driver_sql(f'DELETE FROM {table}')
        with pytest.raises(IntegrityError,match='immutable'):
            with db.begin() as conn:
                conn.exec_driver_sql(f"UPDATE {table} SET cycle_id='forged'")
    with pytest.raises((OperationalError,IntegrityError)):
        with db.begin() as conn:
            conn.exec_driver_sql("INSERT INTO storage_archive_authorizations VALUES ('forged',?,'cycle-0','hash-0')",(CID,))
    assert Path(m['archive_path']).exists()


def test_partial_or_corrupt_archive_never_allows_deletion(db,tmp_path):
    terminal(db)
    p = archive_policy(tmp_path)
    m = verified_archive(db,p,CID)
    Path(m['archive_path']).write_bytes(b'partial')
    with pytest.raises(ValueError,match='CHECKSUM'):
        remove_archived_batch(db,p,m)
    with readonly(Path(db.url.database)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM universe_selection_cycles').fetchone()[0] == 3
        assert archive_location(conn,CID)['status'] == 'ARCHIVED_EVIDENCE_UNAVAILABLE'


@pytest.mark.parametrize('mutation,reason',[
    ("UPDATE burnin_campaigns SET campaign_status='FAILED'",'UNFINISHED'),
    ("UPDATE burnin_campaigns SET worker_pid=1234",'WORKER'),
    ("UPDATE burnin_runs SET open_trade_count=1",'OPEN_RUN'),
    ("INSERT INTO positions(position_id,status) VALUES ('open','OPEN')",'SAFETY_STATE'),
    ("CREATE TABLE unknown_dependency(cycle_id TEXT)",'UNKNOWN_UNIVERSE'),
    ("UPDATE runtime_state_snapshots SET recovery_action_required=1 WHERE campaign_id IS NOT NULL",'QUIESCENT'),
])
def test_unknown_active_failed_or_recovery_state_blocks_archive(db,tmp_path,mutation,reason):
    terminal(db)
    with db.begin() as conn:
        conn.exec_driver_sql(mutation)
    with pytest.raises(ValueError,match=reason):
        verified_archive(db,archive_policy(tmp_path),CID)
    with db.connect() as conn:
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM universe_selection_cycles').scalar_one() == 3


def test_missing_destination_and_archive_disk_full_fail_before_source_deletion(db,tmp_path,monkeypatch):
    terminal(db)
    with pytest.raises(ValueError,match='DESTINATION_MISSING'):
        verified_archive(db,policy(),CID)
    usage = __import__('shutil').disk_usage(tmp_path)
    monkeypatch.setattr('alphaforge.storage_policy.shutil.disk_usage',lambda p: usage._replace(free=1))
    with pytest.raises(ValueError,match='CAPACITY'):
        verified_archive(db,archive_policy(tmp_path),CID)
    assert disk_budget(Path(db.url.database),policy())['status'] != 'PASS'


def test_checkpoint_long_reader_reports_backlog_without_unlink(db):
    reader = sqlite3.connect(db.url.database)
    reader.execute('BEGIN')
    reader.execute('SELECT * FROM runtime_state_snapshots').fetchall()
    heartbeats(db)
    wal = Path(str(db.url.database)+'-wal')
    assert wal.exists()
    report = checkpoint(db)
    assert report['busy']
    assert report['wal_frames'] > report['checkpointed_frames']
    assert wal.exists()
    reader.rollback()
    reader.close()
    assert not checkpoint(db)['busy']


def test_held_external_writer_is_bounded_and_nontransient_errors_remain_fatal(db):
    heartbeats(db)
    held = sqlite3.connect(db.url.database)
    held.execute('BEGIN IMMEDIATE')
    started=time.monotonic()
    try:
        with pytest.raises(SQLiteBusyExhausted):
            prune_telemetry(db,policy(batch_seconds=2))
        assert time.monotonic()-started < 2
    finally:
        held.rollback(); held.close()
    with db.begin() as conn:
        conn.exec_driver_sql('DROP TABLE runtime_heartbeats')
        conn.exec_driver_sql('CREATE TABLE runtime_heartbeats(id INTEGER PRIMARY KEY)')
    assert prune_telemetry(db,policy())['rows_removed'] == 0


def test_concurrent_retention_and_production_heartbeat_converge(db):
    heartbeats(db)
    errors=[]
    def retention():
        try:
            prune_telemetry(db,policy(batch_rows=10,batch_seconds=2))
        except BaseException as e:
            errors.append(e)
    def writer():
        try:
            heartbeats(db,3,instance='concurrent')
        except BaseException as e:
            errors.append(e)
    threads=[threading.Thread(target=f) for f in (retention,retention,writer)]
    for t in threads:t.start()
    for t in threads:t.join(5)
    assert not errors
    assert not any(t.is_alive() for t in threads)
    with db.connect() as conn:
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM runtime_heartbeats').scalar_one() == 13
        assert conn.exec_driver_sql('PRAGMA busy_timeout').scalar_one() == 30000
        assert conn.exec_driver_sql('PRAGMA journal_mode').scalar_one() == 'wal'
        assert conn.exec_driver_sql('PRAGMA synchronous').scalar_one() == 1
        assert conn.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1


def runtime(db):
    from alphaforge.runtime import RuntimeConfig,RuntimeOrchestrator
    r = RuntimeOrchestrator(config=RuntimeConfig(),market_scanner=AsyncMock(return_value=[]),ai_brain=type("Brain",(),{"session":None})(),persistence_engine=db)
    r.metrics.persistence_enabled = True
    return r


def test_empty_startup_refuses_insufficient_reserve(db,tmp_path,monkeypatch):
    usage = __import__('shutil').disk_usage(tmp_path)
    monkeypatch.setattr('alphaforge.storage_policy.shutil.disk_usage',lambda p:usage._replace(free=139*1024**2))
    r=runtime(db)
    with pytest.raises(RuntimeError,match='STORAGE_DISK_RESERVE'):
        asyncio.run(r.start())
    r.market_scanner.assert_not_called()


def test_open_position_pressure_keeps_management_alive_and_blocks_scanner(db,tmp_path,monkeypatch):
    usage=__import__('shutil').disk_usage(tmp_path)
    monkeypatch.setattr('alphaforge.storage_policy.shutil.disk_usage',lambda p:usage._replace(free=139*1024**2))
    r=runtime(db)
    r._active_positions['BTCUSDT']={'trade_id':'active'}
    asyncio.run(r._scan_once())
    assert r._storage_pressure and r._recovery_required
    assert not r._stop_event.is_set()
    assert 'BTCUSDT' in r._active_positions
    r.market_scanner.assert_not_called()
    assert latest_runtime_state_snapshot(db)['recovery_action_required'] == 1


def test_crash_before_manifest_never_enables_cleanup_and_restart_reuses_safely(db,tmp_path,monkeypatch):
    import alphaforge.storage_policy as storage
    terminal(db)
    p=archive_policy(tmp_path)
    publish=storage._atomic_json
    def crash(*args):raise RuntimeError('crash-before-manifest')
    monkeypatch.setattr(storage,'_atomic_json',crash)
    with pytest.raises(RuntimeError,match='crash-before-manifest'):
        verified_archive(db,p,CID)
    with readonly(Path(db.url.database)) as conn:
        assert 'storage_archives' not in storage.tables(conn)
        assert conn.execute('SELECT COUNT(*) FROM universe_selection_cycles').fetchone()[0] == 3
    monkeypatch.setattr(storage,'_atomic_json',publish)
    m=verified_archive(db,p,CID)
    assert verify_manifest(m)
    assert remove_archived_batch(db,p,m)['rows_removed'] == 9


def test_crash_during_delete_rolls_back_entire_batch_and_scope_capability(db,tmp_path):
    terminal(db)
    p=archive_policy(tmp_path,batch_rows=3,batch_seconds=2)
    m=verified_archive(db,p,CID)
    with db.begin() as conn:
        conn.exec_driver_sql("CREATE TRIGGER inject_crash BEFORE DELETE ON universe_selection_candidates BEGIN SELECT RAISE(ABORT,'injected crash'); END")
    with pytest.raises(IntegrityError,match='injected crash'):
        remove_archived_batch(db,p,m)
    with db.connect() as conn:
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM universe_selection_cycles').scalar_one() == 3
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM burnin_universe_selection_links').scalar_one() == 3
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM storage_archive_authorizations').scalar_one() == 0
        assert conn.exec_driver_sql('SELECT state FROM storage_archives').scalar_one() == 'VERIFIED'
    with db.begin() as conn:conn.exec_driver_sql('DROP TRIGGER inject_crash')
    assert remove_archived_batch(db,p,m)['rows_removed'] == 3


def test_concurrent_archive_attempts_publish_one_archive_and_do_not_duplicate_deletes(db,tmp_path):
    terminal(db)
    p=archive_policy(tmp_path,batch_rows=3,batch_seconds=2)
    results=[]; errors=[]
    def run():
        try:
            m=verified_archive(db,p,CID)
            results.append(remove_archived_batch(db,p,m))
        except ValueError as exc:
            errors.append(str(exc))
    threads=[threading.Thread(target=run) for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join(10)
    assert not any(t.is_alive() for t in threads)
    assert results
    assert all('ALREADY_IN_PROGRESS' in e for e in errors)
    assert len(list(Path(p.archive_dir).glob('*.archive.db'))) == 1
    with db.connect() as conn:
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM universe_selection_cycles').scalar_one() == 3-len(results)
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM storage_archives').scalar_one() == 1


def test_recent_terminal_campaign_and_missing_quiescence_evidence_stay_protected(db,tmp_path):
    from datetime import datetime,timezone
    terminal(db)
    with db.begin() as conn:
        conn.exec_driver_sql('UPDATE burnin_campaigns SET completed_at=?',(datetime.now(timezone.utc).isoformat(),))
    with pytest.raises(ValueError,match='TOO_RECENT'):
        verified_archive(db,archive_policy(tmp_path),CID)
    with db.begin() as conn:
        conn.exec_driver_sql("UPDATE burnin_campaigns SET completed_at='2020-01-01'")
        conn.exec_driver_sql('DELETE FROM runtime_state_snapshots WHERE campaign_id=?',(CID,))
    with pytest.raises(ValueError,match='QUIESCENCE'):
        verified_archive(db,archive_policy(tmp_path),CID)


def test_source_scope_changes_after_archive_do_not_allow_removal(db,tmp_path):
    terminal(db)
    p=archive_policy(tmp_path)
    m=verified_archive(db,p,CID)
    with db.begin() as conn:
        conn.exec_driver_sql("INSERT INTO orders(order_id,status) VALUES ('new-order','NEW')")
    with pytest.raises(ValueError,match='SAFETY_STATE'):
        remove_archived_batch(db,p,m)
    with db.connect() as conn:
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM universe_selection_cycles').scalar_one() == 3


def test_archived_scope_cannot_resume_or_materialize_qualification(db,tmp_path):
    from alphaforge.burnin_campaign import start_or_resume_campaign,qualify_campaign
    terminal(db)
    m=verified_archive(db,archive_policy(tmp_path),CID)
    remove_archived_batch(db,archive_policy(tmp_path),m)
    with pytest.raises(RuntimeError,match='ARCHIVED_CAMPAIGN'):
        with db.begin() as conn:start_or_resume_campaign(conn,CID,resume=True)
    with pytest.raises(RuntimeError,match='ARCHIVED_CAMPAIGN'):
        qualify_campaign(db,CID)


def test_empty_runtime_pressure_uses_supervised_shutdown_after_durable_state(db,tmp_path,monkeypatch):
    usage=__import__('shutil').disk_usage(tmp_path)
    monkeypatch.setattr('alphaforge.storage_policy.shutil.disk_usage',lambda p:usage._replace(free=139*1024**2))
    r=runtime(db)
    r._unknown_exchange_state=False
    r._reconciliation_status="CLEAN"
    asyncio.run(r._scan_once())
    assert r._stop_event.is_set()
    snapshot=latest_runtime_state_snapshot(db)
    assert snapshot['recovery_action_required'] == 1
    assert snapshot['fail_closed_reason'] == 'STORAGE_PRESSURE_RECOVERY_REQUIRED'
    r.market_scanner.assert_not_called()


def test_automatic_terminal_archive_and_telemetry_under_threshold_crossing(db,tmp_path):
    terminal(db)
    p=archive_policy(tmp_path,high_bytes=100000,low_bytes=1,batch_rows=3,batch_seconds=2)
    report=StorageController(db,p).check(force=True)
    assert report['archives_verified'] == 1
    assert report['rows_removed'] == 3
    assert report['status'] == 'CLEANUP_IN_PROGRESS'


def test_pending_rejects_and_resolver_backlog_are_preserved(db,tmp_path):
    terminal(db)
    with db.begin() as conn:
        conn.exec_driver_sql("""INSERT INTO burnin_pending_reject_labels(pending_label_id,campaign_id,burnin_run_id,reject_decision_id,symbol,side,decision_timestamp,execution_cost_assumptions_json,source_provenance_json,due_at,status,created_at,schema_version) VALUES ('pending',?,?,'reject','BTCUSDT','LONG','2020-01-01','{}','{}','2020-01-01','RESOLVING','2020-01-01','v1')""",(CID,RUN))
    with pytest.raises(ValueError,match='UNRESOLVED_STATE'):
        verified_archive(db,archive_policy(tmp_path),CID)
    with db.connect() as conn:
        assert conn.exec_driver_sql('SELECT status FROM burnin_pending_reject_labels').scalar_one() == 'RESOLVING'
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM universe_selection_cycles').scalar_one() == 3


def test_disk_pressure_preflight_refuses_before_bootstrap_or_artifact_creation(db,tmp_path,monkeypatch):
    import alphaforge.burnin_ops as ops
    usage=__import__('shutil').disk_usage(tmp_path)
    monkeypatch.setattr('alphaforge.storage_policy.shutil.disk_usage',lambda p:usage._replace(free=139*1024**2))
    monkeypatch.setattr(ops,'_connect',lambda p:pytest.fail('disk pressure must precede bootstrap/write probes'))
    output=tmp_path/'preflight'
    result=ops.preflight(db.url.database,'issue620',['BTCUSDT'],['1m'],output_dir=output)
    assert result['status'] == 'FAIL_CLOSED'
    assert result['blockers'] == ['disk_space_sufficient']
    assert not output.exists()


def test_growth_duration_adds_configured_budget_without_fabricated_rate():
    p=policy(growth_budget_bytes_per_sec=1000)
    assert p.reserve(duration_seconds=86400)-p.reserve() == 86400000
    assert policy().reserve(duration_seconds=86400) == policy().reserve()


def test_missing_immutable_domain_data_is_not_archivable(db,tmp_path):
    terminal(db)
    # A missing source candidate is not a legitimate retention request. Model
    # a legacy bad DB by replacing its schema before the archival lifecycle.
    with db.begin() as conn:
        conn.exec_driver_sql('DROP TRIGGER trg_universe_selection_candidates_no_delete')
        conn.exec_driver_sql("DELETE FROM universe_selection_candidates WHERE cycle_id='cycle-0'")
    with pytest.raises(ValueError,match='DOMAIN_OR_REPLAY_INCOMPLETE'):
        verified_archive(db,archive_policy(tmp_path),CID)


def test_unknown_exposure_does_not_authorize_empty_shutdown(db,tmp_path,monkeypatch):
    usage=__import__('shutil').disk_usage(tmp_path)
    monkeypatch.setattr('alphaforge.storage_policy.shutil.disk_usage',lambda p:usage._replace(free=139*1024**2))
    r=runtime(db)
    r._unknown_exchange_state=True
    asyncio.run(r._scan_once())
    assert r._storage_pressure and not r._stop_event.is_set()
    assert r._execution_reconciliation_blocked()


def test_verified_archive_restart_does_not_require_capacity_for_another_backup(db,tmp_path,monkeypatch):
    terminal(db)
    p=archive_policy(tmp_path,batch_rows=3,batch_seconds=2)
    m=verified_archive(db,p,CID)
    usage=__import__('shutil').disk_usage(tmp_path)
    monkeypatch.setattr('alphaforge.storage_policy.shutil.disk_usage',lambda location:usage._replace(free=1))
    resumed=verified_archive(db,p,CID)
    assert resumed == m
    assert remove_archived_batch(db,p,resumed)['rows_removed'] == 3


def test_archive_sqlite_full_is_an_explicit_pressure_blocker_not_scanner_death(db,tmp_path,monkeypatch):
    import alphaforge.storage_policy as storage
    terminal(db)
    failure=sqlite3.OperationalError('database or disk is full')
    failure.sqlite_errorcode=sqlite3.SQLITE_FULL
    def full(*args,**kwargs):raise failure
    monkeypatch.setattr(storage,'verified_archive',full)
    p=archive_policy(tmp_path,high_bytes=100000,low_bytes=1,batch_seconds=2)
    result=StorageController(db,p).check(force=True)
    assert result['blocked']
    assert result['status'] == 'STORAGE_DISK_FULL_RECOVERY_REQUIRED'
    assert 'STORAGE_ARCHIVE_OR_MAINTENANCE_DISK_FULL' in result['blockers']
    with db.connect() as conn:
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM universe_selection_cycles').scalar_one() == 3


def test_failed_disk_full_recovery_persistence_is_explicit_and_does_not_stop_exposure(db,tmp_path,monkeypatch):
    from alphaforge.runtime import RuntimeOrchestrator
    usage=__import__('shutil').disk_usage(tmp_path)
    monkeypatch.setattr('alphaforge.storage_policy.shutil.disk_usage',lambda p:usage._replace(free=139*1024**2))
    failure=sqlite3.OperationalError('database or disk is full')
    failure.sqlite_errorcode=sqlite3.SQLITE_FULL
    def full(*args,**kwargs):raise OperationalError('snapshot',{},failure)
    monkeypatch.setattr(RuntimeOrchestrator,'_persist_runtime_state_snapshot',full)
    r=runtime(db)
    r._active_positions['BTCUSDT']=100.0
    asyncio.run(r._scan_once())
    assert r._storage_report['recovery_state_persisted'] is False
    assert r._burnin_evidence_incomplete
    assert not r._stop_event.is_set()
    assert r._execution_reconciliation_blocked()
    assert r._active_positions['BTCUSDT'] == 100.0


def test_nontransient_maintenance_schema_errors_remain_fatal(db,monkeypatch):
    import alphaforge.storage_policy as storage
    heartbeats(db)
    def schema_failure(*args):raise OperationalError('DELETE',{},sqlite3.OperationalError('no such column: corrupt'))
    monkeypatch.setattr(storage,'prune_telemetry',schema_failure)
    with pytest.raises(OperationalError,match='no such column'):
        StorageController(db,policy(high_bytes=100000,low_bytes=1)).check(force=True)


def test_concurrent_resolver_reconciliation_and_retention_preserve_pending_evidence(db):
    from alphaforge.burnin_campaign import _with_fresh_lock_retry
    from alphaforge.burnin_resolver import resolve_campaign_batch
    from alphaforge.runtime_state import persist_reconciliation_cycle
    terminal(db)
    heartbeats(db)
    with db.begin() as conn:
        conn.exec_driver_sql("""INSERT INTO burnin_pending_reject_labels(pending_label_id,campaign_id,burnin_run_id,reject_decision_id,symbol,side,decision_timestamp,execution_cost_assumptions_json,source_provenance_json,due_at,status,created_at,schema_version) VALUES ('pending',?,?,'reject','BTCUSDT','LONG','2020-01-01','{}','{}','2999-01-01','PENDING','2020-01-01','v1')""",(CID,RUN))
    errors=[]
    def invoke(operation):
        try:operation()
        except BaseException as exc:errors.append(exc)
    state=RuntimeStateSnapshot(mode='PAPER',requested_mode='PAPER',actual_mode='PAPER',runtime_status='OPERATING',instance_id='reconciler',unknown_exchange_state=False,reconciliation_status='CLEAN')
    operations=[lambda:prune_telemetry(db,policy(batch_rows=5,batch_seconds=2)),
                lambda:_with_fresh_lock_retry(db,lambda conn:resolve_campaign_batch(conn,CID,{},now='2020-01-02T00:00:00Z')),
                lambda:persist_reconciliation_cycle(db,cycle_id='issue620-reconciliation',findings=[],snapshot=state,diagnostics={'evidence_status':'COMPLETE'},pending_failures=[])]
    threads=[threading.Thread(target=invoke,args=(operation,)) for operation in operations]
    for thread in threads:thread.start()
    for thread in threads:thread.join(5)
    assert not errors and not any(thread.is_alive() for thread in threads)
    with db.connect() as conn:
        assert conn.exec_driver_sql("SELECT status FROM burnin_pending_reject_labels WHERE pending_label_id='pending'").scalar_one() == 'PENDING'
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM universe_selection_cycles').scalar_one() == 3
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM exchange_reconciliation_events WHERE cycle_id='issue620-reconciliation'").scalar_one() == 1
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM runtime_heartbeats').scalar_one() == 25
