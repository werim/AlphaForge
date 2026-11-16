"""Only tiny fixtures. None is an actual capacity or six-hour measurement."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import errno
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from alphaforge import storage_measurement as m, storage_volume as v
from alphaforge.burnin import canonical_hash
from alphaforge.position_management_shadow import SHADOW_ACTIONS


@pytest.fixture
def cfg():
    return m.configuration({'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED':'true'})[0]


def good_coverage():
    return dict(wall_heartbeat_span=21600,heartbeat_max_gap=1,heartbeat_sources=['EXCHANGE_PUBLIC_MARKET_DATA'],
        scanner_cycles=21600,max_candidates_per_cycle=5,candidate_rows=100,observations=5,rejects=1,
        positions=1,closed_positions=1,snapshots=10,resolver_batches=2,clean_reconciliations=2,
        failures=0,unhealthy_heartbeats=0,controller_checkpoints=1,integrity='ok',foreign_key_violations=0,journal_mode='wal',shadow_evidence_status='AVAILABLE',shadow_actions=dict.fromkeys(SHADOW_ACTIONS,1))


def test_atomic_control_race_and_torn_completion(tmp_path):
    path=tmp_path/'control'
    def reserve(_):
        try:
            c=v.ControlFile(path,{'attempt':'fixture'});c.close();return True
        except FileExistsError:return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve,range(16)))==1
    c=v.ControlFile(path)
    c.put(1,{'status':'MEASURED'})
    assert c.get(0)=={'attempt':'fixture'} and c.get(1)['status']=='MEASURED'
    with pytest.raises(ValueError,match='BOUND'):c.put(1,{'huge':'x'*v.SLOT})
    os.pwrite(c.fd,b'changed',v.SLOT+40)
    with pytest.raises(ValueError,match='TORN'):c.get(1)
    c.close()
    assert path.stat().st_size==v.SLOT*v.SLOTS


def test_control_short_write_is_failure_and_consumes_attempt(tmp_path,monkeypatch):
    original=os.pwrite
    monkeypatch.setattr(v.os,'pwrite',lambda fd,data,offset: original(fd,data[:1],offset))
    with pytest.raises(OSError,match='SHORT_WRITE'):v.ControlFile(tmp_path/'attempt',{'fixture':True})
    assert (tmp_path/'attempt').exists()
    with pytest.raises(FileExistsError):v.ControlFile(tmp_path/'attempt',{'fixture':True})


@pytest.mark.parametrize('mutation',['duration','source','policy','config','future','limits'])
def test_plan_drift_fails_before_backend_or_claim(tmp_path,monkeypatch,cfg,mutation):
    env={'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED':'true'}
    _,ids=m.hashes(env)
    source={'sha':'a'*40,'dirty':False}
    plan=dict(version=m.VERSION,purpose=m.PURPOSE,source=source,config_env=env,config_hashes=ids,
              policy_hash=m.StoragePolicy.from_config(cfg.runtime).identity_hash(),duration_seconds=21600,
              wall_limit_seconds=22260,shutdown_grace_seconds=60,max_log_bytes=65536,
              host_reserve_bytes=m.StoragePolicy.from_config(cfg.runtime).reserve(),created_at=time.time())
    monkeypatch.setattr(m,'source_identity',lambda:source)
    monkeypatch.setattr(v,'verify',lambda *a:pytest.fail('invalid scope must fail before physical proof'))
    if mutation=='duration':plan['duration_seconds']=1
    if mutation=='source':plan['source']={'sha':'wrong'}
    if mutation=='policy':plan['policy_hash']='wrong'
    if mutation=='config':plan['config_hashes']={}
    if mutation=='future':plan['created_at']=time.time()+100
    if mutation=='limits':plan['wall_limit_seconds']=999999
    with pytest.raises(ValueError):m.validate_plan(plan)
    assert list(tmp_path.iterdir())==[]


def test_quota_metadata_requires_exact_dedicated_volume(tmp_path,monkeypatch):
    real_mount=Path.is_mount
    monkeypatch.setattr(Path,'is_mount',lambda p: p==tmp_path or real_mount(p))
    monkeypatch.setattr(v.platform,'system',lambda:'Darwin')
    original=Path.is_file
    monkeypatch.setattr(Path,'is_file',lambda p: str(p)=='/usr/bin/sandbox-exec' or original(p))
    monkeypatch.setattr(v,'file_hash',lambda p:'binary-sha')
    volume={'FilesystemType':'apfs','MountPoint':str(tmp_path),'WritableVolume':True,'WritableMedia':True,
            'VolumeUUID':'uuid','DeviceIdentifier':'disk99s1'}
    entry={'APFSVolumeUUID':'uuid','DeviceIdentifier':'disk99s1','CapacityQuota':10000000,'Roles':[]}
    container={'APFSContainerUUID':'container','Volumes':[entry]}
    monkeypatch.setattr(v,'plist',lambda argv: volume if 'info' in argv else {'Containers':[container]})
    monkeypatch.setattr(v,'host_identity',lambda: 'canonical-host-hash')
    observed=v.inspect_volume(tmp_path,10000000)
    assert observed['backend']==v.BACKEND and observed['cap_bytes']==10000000
    assert observed['host']=='canonical-host-hash'
    for field,value in [('CapacityQuota',0),('CapacityQuota',999),('Roles',['Data']),('DeviceIdentifier','wrong')]:
        before=deepcopy(entry);entry[field]=value
        with pytest.raises(ValueError):v.inspect_volume(tmp_path,10000000)
        entry.clear();entry.update(before)


def sandbox_record(mount, sha='a'*40):
    import hashlib
    nonce = 'f'*32
    return dict(schema=v.SANDBOX_SCHEMA, status='SANDBOX_VERIFIED', source_sha=sha,
        profile_sha256=hashlib.sha256(v.sandbox_profile(mount).encode()).hexdigest(),
        script_sha256=hashlib.sha256(v._SANDBOX_CHECK.encode()).hexdigest(),
        executable=v.SANDBOX_EXEC, returncode=0, inside_write=True, outside_absent=True,
        outside_errno=errno.EPERM, nonce=nonce,
        stdout=json.dumps({'nonce':nonce,'outside_errno':errno.EPERM}), stderr='')


def sandbox_success(command, **kwargs):
    assert command[0] == v.SANDBOX_EXEC and command[1] == '-p'
    assert command[5] == v._SANDBOX_CHECK
    nonce, inside, outside = command[-3:]
    Path(inside).write_text(nonce)
    assert not Path(outside).exists()
    return SimpleNamespace(returncode=0, stdout=json.dumps({'nonce':nonce,'outside_errno':errno.EPERM}).encode(), stderr=b'')


def physical_fixture(tmp_path,monkeypatch):
    identity={'mount':str(tmp_path),'cap_bytes':10000000,'filesystem':{'fsid':9},'host':'host','backend':v.BACKEND}
    monkeypatch.setattr(v,'inspect_volume',lambda *a:identity)
    c=v.ControlFile(tmp_path/'.af-capacity-proof.control',{'identity':identity,'source_sha':'a'*40,'host_reserve_bytes':100})
    result={'status':'PHYSICALLY_VERIFIED','identity':identity,'aggregate_enospc':True,'sandbox_denial':True,
            'files_written':4,'observed_capacity_bytes':9000000,'capacity_errno':errno.ENOSPC,
            'container_free_at_error':1000,'completed_at':time.time(),
            'sandbox_verification':sandbox_record(tmp_path), 'sandbox_returncode':0}
    c.put(1,result);c.close()
    return identity,result


@pytest.mark.parametrize('field,value',[('status','FAILED'),('aggregate_enospc',False),('sandbox_denial',False),
    ('files_written',1),('observed_capacity_bytes',1),('capacity_errno',errno.EFBIG),('container_free_at_error',0),('completed_at',1)])
def test_physical_proof_never_accepts_sampled_per_file_or_stale_result(tmp_path,monkeypatch,field,value):
    _,result=physical_fixture(tmp_path,monkeypatch)
    result[field]=value
    c=v.ControlFile(tmp_path/'.af-capacity-proof.control');c.put(1,result);c.close()
    with pytest.raises(ValueError,match='PHYSICAL_BACKEND'):v.verify(tmp_path,10000000,canonical_hash(result),'a'*40)


def test_backend_inspection_without_real_probe_is_not_verified(tmp_path,monkeypatch):
    monkeypatch.setattr(v,'inspect_volume',lambda *a:{'mount':str(tmp_path)})
    with pytest.raises(FileNotFoundError):v.verify(tmp_path,10000000,'not-a-proof','a'*40)
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('field,value',[('wall_heartbeat_span',21599),('failures',1),('candidate_rows',0),
    ('shadow_actions',{}),('heartbeat_sources',['SYNTHETIC']),('integrity','corrupt'),('journal_mode','delete'),
    ('clean_reconciliations',0),('scanner_cycles',0),('heartbeat_max_gap',99999)])
def test_elapsed_time_alone_and_missing_runtime_surfaces_are_not_complete(cfg,field,value):
    coverage=good_coverage();assert m.coverage_gaps(coverage,cfg.runtime,1)==[]
    coverage[field]=value
    assert m.coverage_gaps(coverage,cfg.runtime,1)


def measurement_fixture(tmp_path,monkeypatch,cfg,env=None):
    if env is None:
        env={'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED':'true'}
    plan={'config_env':env,'backend':{'mount':str(tmp_path),'filesystem':v.filesystem_identity(tmp_path)},'source':{'sha':'a'*40}}
    worker={'kind':'REAL_RUNTIME','complete':True,'gaps':[],'plan_hash':canonical_hash(plan),
            'coverage':good_coverage(),'maintenance_count':2,'positive_db_wal_growth_bytes':1000}
    result={'version':m.VERSION,'kind':'REAL_RUNTIME','status':'MEASURED','returncode':0,
            'plan':plan,'worker':worker,'completed_at':time.time()}
    root=tmp_path/'experiment';root.mkdir();(root/'evidence').write_text('fixture')
    result['artifacts']=m.artifact_manifest(root)
    control_path=tmp_path/'.af-real-calibration.control'
    c=v.ControlFile(control_path,{'plan_hash':canonical_hash(plan)});c.put(1,result);c.close()
    monkeypatch.setattr(m,'validate_plan',lambda p:cfg)
    monkeypatch.setattr(v,'container_free',lambda p:999999999999)
    return control_path,result


@pytest.mark.parametrize('mutation',['failed','short','synthetic','stale','tampered','wrong_plan','unavailable_shadow'])
def test_review_rejects_failed_short_synthetic_stale_or_changed_measurements(tmp_path,monkeypatch,cfg,mutation):
    path,result=measurement_fixture(tmp_path,monkeypatch,cfg)
    if mutation=='failed':result['status']='FAILED'
    if mutation=='short':result['worker']['coverage']['wall_heartbeat_span']=10
    if mutation=='synthetic':result['kind']='SYNTHETIC'
    if mutation=='stale':result['completed_at']=1
    if mutation=='tampered':(tmp_path/'experiment/evidence').write_text('modified')
    if mutation=='wrong_plan':result['worker']['plan_hash']='wrong'
    if mutation=='unavailable_shadow':result['worker']['coverage'].pop('shadow_evidence_status')
    c=v.ControlFile(path);c.put(1,result);c.close()
    with pytest.raises(ValueError):m.review(path,10,'Fixture operator scope justification only; not a measured rate.')


def test_admission_consumes_review_and_rejects_config_filesystem_and_artifact_drift(tmp_path,monkeypatch,cfg):
    path,_=measurement_fixture(tmp_path,monkeypatch,cfg)
    cert=m.review(path,10,'Fixture operator scope justification only; not a measured rate.')
    certificate=tmp_path/'review.json';certificate.write_text(json.dumps(cert))
    prospective,_=m.configuration({'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED':'true',
                                   'ALPHAFORGE_STORAGE_GROWTH_BUDGET_BYTES_PER_SEC':'10'})
    assert m.admission_evidence(certificate,prospective,tmp_path/'fresh.db',21600)['status']=='PASS'
    with pytest.raises(ValueError,match='CONFIG'):m.admission_evidence(certificate,cfg,tmp_path/'fresh.db',21600)
    with pytest.raises(ValueError,match='FILESYSTEM'):m.admission_evidence(certificate,prospective,tmp_path.parent/'elsewhere.db',21600)
    (tmp_path/'fresh.db').touch()
    with pytest.raises(ValueError,match='FRESH_DATABASE'):m.admission_evidence(certificate,prospective,tmp_path/'fresh.db',21600)
    cert['rate']=1;certificate.write_text(json.dumps(cert))
    with pytest.raises(ValueError):m.admission_evidence(certificate,prospective,tmp_path/'new.db',21600)


def test_real_calibration_lineage_cannot_qualify_complete_or_resume(tmp_path,cfg):
    from alphaforge.persistence import init_db
    from alphaforge.burnin_campaign import create_campaign,start_or_resume_campaign,qualify_campaign,check_campaign_completion
    e=init_db(f'sqlite+pysqlite:///{tmp_path/"fixture.db"}')
    with e.begin() as c:
        campaign=create_campaign(c,release_id='calibration-fixture',duration_days=.25,symbols=[],intervals=['1h','15m','1m'],
            runtime_config=cfg.runtime,dynamic_universe=True,source_provenance={'purpose':m.PURPOSE,'exchange':'binance'})
        start_or_resume_campaign(c,campaign.campaign_id)
        assert check_campaign_completion(c,campaign.campaign_id)['complete'] is False
        with pytest.raises(RuntimeError,match='NO_RESUME'):start_or_resume_campaign(c,campaign.campaign_id,resume=True)
        with pytest.raises(RuntimeError,match='NO_RESUME'):start_or_resume_campaign(c,campaign.campaign_id)
    import asyncio
    from alphaforge.burnin_campaign import BurnInCampaignRunner
    runner=BurnInCampaignRunner(e,campaign.campaign_id,object())
    with pytest.raises(RuntimeError,match='OWNED_WORKER_REQUIRED'):asyncio.run(runner.run_foreground())
    from alphaforge.runtime import RuntimeOrchestrator
    bare=SimpleNamespace(_resolve_persistence_engine=lambda:e)
    with pytest.raises(RuntimeError,match='OWNED_WORKER_REQUIRED'):
        RuntimeOrchestrator._attach_phase8_campaign(bare,campaign.campaign_id)
    from alphaforge.burnin_qualification import BurnInQualificationEngine
    with e.connect() as c:run_id=c.execute(text('SELECT active_run_id FROM burnin_campaigns')).scalar_one()
    direct=BurnInQualificationEngine(e).evaluate(run_id)
    assert 'STORAGE_CALIBRATION_NOT_QUALIFICATION_ELIGIBLE' in direct.blockers
    verdict=qualify_campaign(e,campaign.campaign_id)
    assert verdict['materialized'] is False and 'CALIBRATION' in verdict['verdict']
    with e.connect() as c:assert c.execute(text('SELECT COUNT(*) FROM burnin_qualification_snapshots')).scalar()==0
    e.dispose()


@pytest.mark.parametrize('loss',['supervisor','guardian','worker','lease'])
def test_guardian_process_chain_has_no_automatic_survivor_or_retry(tmp_path,loss):
    root=tmp_path/'experiment';root.mkdir()
    worker='''
import os,sys,time
from pathlib import Path
from alphaforge.storage_calibration import parent_watchdog
p=Path(sys.argv[1]);parent_watchdog(os.getppid(),p)
(p/'worker.pid').write_text(str(os.getpid()))
time.sleep(20)
(p/'survived').touch()
'''
    script='''
import sys
from pathlib import Path
from alphaforge import storage_measurement as m
root=Path(sys.argv[1]);m.LEASE_SECONDS=.3
m.worker_argv=lambda p:[sys.executable,'-c',sys.argv[2],str(root/'experiment')]
raise SystemExit(m.guardian({},root/'control'))
'''
    env={**os.environ,'PYTHONPATH':str(Path(m.__file__).resolve().parents[1]),'PYTHONDONTWRITEBYTECODE':'1'}
    p=subprocess.Popen([sys.executable,'-c',script,str(tmp_path),worker],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
    child_pid=None
    try:
        deadline=time.monotonic()+5
        while not (root/'worker.pid').exists() and time.monotonic()<deadline:
            if p.poll() is None:p.stdin.write(b'.');p.stdin.flush()
            time.sleep(.02)
        assert (root/'worker.pid').exists()
        child_pid=int((root/'worker.pid').read_text())
        if loss=='supervisor':p.stdin.close()
        elif loss=='guardian':p.kill()
        elif loss=='worker':os.kill(child_pid,9)
        # lease: intentionally leave stdin open but stop heartbeats
        p.wait(timeout=5)
        with selectors.DefaultSelector() as selector:
            selector.register(p.stdout,selectors.EVENT_READ)
            assert selector.select(timeout=5)
            assert p.stdout.read(1)==b''
        child_pid=None
        assert not (root/'survived').exists()
        assert len(list(root.glob('worker.pid')))==1
    finally:
        if p.poll() is None:p.kill();p.wait(timeout=3)
        if child_pid is not None:
            try:os.kill(child_pid,9)
            except ProcessLookupError:pass
        for stream in (p.stdin,p.stdout,p.stderr):
            if not stream.closed:stream.close()


def test_coverage_reads_real_canonical_fixture_tables_without_assuming_unknown_zero(tmp_path):
    from alphaforge import storage_calibration as cal
    import sqlite3
    env={'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED':'true',
         'ALPHAFORGE_REJECT_FORWARD_HORIZON_BARS':'1','ALPHAFORGE_SCAN_INTERVAL_SEC':'60'}
    source={'sha':'a'*40,'dirty':False}
    plan=cal.example(env,source['sha'],duration=210,candidate_count=1)
    cal.replay(plan,tmp_path,source)
    with sqlite3.connect(tmp_path/'laboratory.db') as c:
        cid,run=c.execute('SELECT campaign_id,active_run_id FROM burnin_campaigns').fetchone()
    coverage=m.scoped_coverage(tmp_path/'laboratory.db',cid,run)
    assert coverage['positions']==1 and coverage['closed_positions']==1
    assert coverage['rejects']==1 and coverage['observations']>=1
    assert coverage['candidate_rows']>0 and coverage['shadow_actions']
    assert coverage['clean_reconciliations']==0
    assert m.coverage_gaps(coverage,m.configuration(env)[0].runtime,1)  # offline != real evidence


def coverage_database(tmp_path):
    from alphaforge import storage_calibration as cal
    import sqlite3
    env = {'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED': 'true',
           'ALPHAFORGE_REJECT_FORWARD_HORIZON_BARS': '1', 'ALPHAFORGE_SCAN_INTERVAL_SEC': '60'}
    cal.replay(cal.example(env, 'a' * 40, duration=210, candidate_count=1), tmp_path,
               {'sha': 'a' * 40, 'dirty': False})
    db = tmp_path / 'laboratory.db'
    with sqlite3.connect(db) as c:
        cid, run = c.execute('SELECT campaign_id,active_run_id FROM burnin_campaigns').fetchone()
    return db, cid, run


@pytest.mark.parametrize('state', ['absent', 'empty', 'other_scope'])
def test_shadow_coverage_distinguishes_unavailable_from_known_empty(tmp_path, state, cfg):
    import sqlite3
    db, cid, run = coverage_database(tmp_path)
    baseline = m.scoped_coverage(db, cid, run)
    with sqlite3.connect(db) as c:
        if state == 'absent':
            c.execute('DROP TABLE burnin_position_management_shadow_proposals')
        elif state == 'empty':
            c.execute('DELETE FROM burnin_position_management_shadow_proposals')
        else:
            c.execute("UPDATE burnin_position_management_shadow_proposals SET campaign_id='other'")
    observed = m.scoped_coverage(db, cid, run)
    assert observed['positions'] == baseline['positions'] == 1
    assert observed['closed_positions'] == 1 and observed['candidate_rows'] == baseline['candidate_rows']
    assert observed['shadow_evidence_status'] == ('UNAVAILABLE' if state == 'absent' else 'AVAILABLE')
    assert observed['shadow_actions'] == (None if state == 'absent' else {})
    assert observed['shadow_eligibility'] == (None if state == 'absent' else {})
    gaps = m.coverage_gaps(observed, cfg.runtime, 1)
    assert 'SHADOW_ACTION_COVERAGE_INCOMPLETE' in gaps
    assert ('SHADOW_EVIDENCE_UNAVAILABLE' in gaps) == (state == 'absent')
    with sqlite3.connect(db) as c:
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='burnin_position_management_shadow_proposals'").fetchone()
    assert bool(exists) == (state != 'absent')  # Reader never bootstraps missing evidence.


def test_shadow_coverage_does_not_hide_malformed_existing_schema(tmp_path):
    import sqlite3
    db, cid, run = coverage_database(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute('DROP TABLE burnin_position_management_shadow_proposals')
        c.execute('CREATE TABLE burnin_position_management_shadow_proposals(campaign_id TEXT,burnin_run_id TEXT)')
    with pytest.raises(sqlite3.OperationalError, match='no such column'):
        m.scoped_coverage(db, cid, run)


@pytest.mark.parametrize('status', [None, 'UNAVAILABLE', 'UNKNOWN'])
def test_complete_action_keys_do_not_promote_unavailable_shadow_evidence(cfg, status):
    coverage = good_coverage()
    if status is None:
        coverage.pop('shadow_evidence_status')
    else:
        coverage['shadow_evidence_status'] = status
    assert 'SHADOW_EVIDENCE_UNAVAILABLE' in m.coverage_gaps(coverage, cfg.runtime, 1)


@pytest.mark.parametrize('actions', [None, [], 'unavailable'])
def test_invalid_shadow_action_shape_is_incomplete(cfg, actions):
    coverage = good_coverage()
    coverage['shadow_actions'] = actions
    assert 'SHADOW_ACTION_COVERAGE_INCOMPLETE' in m.coverage_gaps(coverage, cfg.runtime, 1)


def test_real_worker_preserves_partial_coverage_when_shadow_table_was_never_created(tmp_path, monkeypatch, cfg):
    import asyncio
    import sqlite3
    from alphaforge import burnin_ops as ops, burnin_campaign as campaigns
    from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot
    from alphaforge.runtime_heartbeat import save_runtime_heartbeat
    root = tmp_path / 'experiment'
    root.mkdir()
    db, _, _ = coverage_database(root)
    with sqlite3.connect(db) as c:
        c.execute('DROP TABLE burnin_position_management_shadow_proposals')
    plan = {'backend': {'mount': str(tmp_path)}, 'shutdown_grace_seconds': 1}
    monkeypatch.setattr(m, 'validate_plan', lambda p: cfg)
    monkeypatch.setattr(ops, '_preflight', lambda *a, **kw: {'status': 'CALIBRATION_READY'})
    monkeypatch.setattr(ops, '_git_branch', lambda: 'dev')
    monkeypatch.setattr(v, 'usage', lambda p: {'quota_used_bytes': 0, 'host_container_free_bytes': 9999999999})
    async def finish_early(runner):
        with runner.engine.connect() as c:
            run = c.execute(text('SELECT active_run_id FROM burnin_campaigns WHERE campaign_id=:cid'),
                            {'cid': runner.campaign_id}).scalar_one()
        save_runtime_state_snapshot(runner.engine, RuntimeStateSnapshot(
            mode='PAPER', requested_mode='PAPER', actual_mode='PAPER', runtime_status='OPERATING',
            instance_id='synthetic-661', campaign_id=runner.campaign_id, burnin_run_id=run))
        save_runtime_heartbeat(runner.engine, runtime_instance_id='synthetic-661', execution_mode='PAPER',
                               scanner_source='SYNTHETIC', runtime_state='OPERATING')
        return {'status': 'synthetic early stop'}
    monkeypatch.setattr(campaigns.BurnInCampaignRunner, 'run_foreground', finish_early)
    guard = SimpleNamespace(is_set=lambda: False, set=lambda: None,
                            worker_thread=SimpleNamespace(is_alive=lambda: True))
    result = asyncio.run(m.real_worker(plan, tmp_path / 'control', guard))
    assert result['complete'] is False and result['admission_eligible'] is False
    assert result['coverage']['runtime_instance_id'] == 'synthetic-661'
    assert result['coverage']['shadow_evidence_status'] == 'UNAVAILABLE'
    assert result['coverage']['shadow_actions'] is None
    assert {'SHADOW_EVIDENCE_UNAVAILABLE', 'SHADOW_ACTION_COVERAGE_INCOMPLETE',
            'REAL_WALL_WINDOW_INCOMPLETE', 'REAL_PROVIDER_SOURCE_MISSING'} <= set(result['gaps'])


def run_fixture_plan(tmp_path,monkeypatch,cfg):
    plan={'config_env':{'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED':'true'},
          'backend':{'mount':str(tmp_path),'cap_bytes':10000000},'source':{'sha':'a'*40},
          'host_reserve_bytes':100,'wall_limit_seconds':.15,'max_log_bytes':65536}
    monkeypatch.setattr(m,'validate_plan',lambda p:cfg)
    monkeypatch.setattr(v,'container_free',lambda p:9999999999)
    path=tmp_path/'selected-plan.json';path.write_text(json.dumps(plan))
    return path


def test_preparation_failure_is_durable_and_never_restarts(tmp_path,monkeypatch,cfg):
    plan=run_fixture_plan(tmp_path,monkeypatch,cfg)
    original=Path.mkdir
    def fail(path,*a,**kw):
        if path==tmp_path/'experiment':raise PermissionError('fixture preparation denied')
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,'mkdir',fail)
    result=m.run(plan,m.file_hash(plan))
    assert result['status']=='FAILED' and 'PermissionError' in result['reason']
    c=v.ControlFile(tmp_path/'.af-real-calibration.control');saved=c.get(1);c.close()
    assert saved==result and saved['admission_eligible'] is False
    with pytest.raises(FileExistsError):m.run(plan,m.file_hash(plan))


def test_real_supervisor_timeout_keeps_redacted_output_and_returncode(tmp_path,monkeypatch,cfg):
    plan=run_fixture_plan(tmp_path,monkeypatch,cfg)
    original=subprocess.Popen
    monkeypatch.setenv('BINANCE_API_SECRET','fixture-private-key')
    def owned_child(command,**kwargs):
        if command[0]!='/usr/bin/sandbox-exec':return original(command,**kwargs)
        assert command[:2]==['/usr/bin/sandbox-exec','-p']
        return original([sys.executable,'-c',"import time;print('token=fixture-private-key',flush=True);time.sleep(10)"],**kwargs)
    monkeypatch.setattr(m.subprocess,'Popen',owned_child)
    result=m.run(plan,m.file_hash(plan))
    assert result['status']=='FAILED' and 'TIMEOUT' in result['reason']
    assert result['returncode']!=0 and result['admission_eligible'] is False
    c=v.ControlFile(tmp_path/'.af-real-calibration.control')
    assert 'fixture-private-key' not in c.get(2)['text']
    assert c.get(1)['returncode']==result['returncode'];c.close()


def test_evidence_disk_full_cannot_create_favorable_result(tmp_path,monkeypatch,cfg):
    plan=run_fixture_plan(tmp_path,monkeypatch,cfg)
    original=v.ControlFile.put
    def no_completion(c,index,value,**kw):
        if index==1:raise OSError(errno.ENOSPC,'fixture disk full')
        return original(c,index,value,**kw)
    monkeypatch.setattr(v.ControlFile,'put',no_completion)
    monkeypatch.setattr(m.volume,'container_free',lambda p:0)  # fail before process startup
    with pytest.raises(OSError):m.run(plan,m.file_hash(plan))
    c=v.ControlFile(tmp_path/'.af-real-calibration.control')
    assert c.get(0)['plan_hash']
    with pytest.raises(ValueError):c.get(1)
    c.close()


def test_normal_positive_rate_requires_real_evidence_before_bootstrap(tmp_path,monkeypatch):
    from alphaforge import burnin_ops as ops
    monkeypatch.setenv('ALPHAFORGE_STORAGE_GROWTH_BUDGET_BYTES_PER_SEC','1')
    monkeypatch.setenv('ALPHAFORGE_STORAGE_STATS_ONLY_ENABLED','false')
    monkeypatch.setattr(ops,'_connect',lambda p:pytest.fail('missing evidence must fail before canonical writes'))
    result=ops.preflight(str(tmp_path/'fresh.db'),'storage-evidence-fixture',[],['1h','15m','1m'],
        dynamic_universe=True,expected_duration_seconds=21600,output_dir=tmp_path/'preflight')
    assert result['status']=='FAIL_CLOSED'
    assert result['evidence_locations']['unavailable_reason']=='STORAGE_ADMISSION_EVIDENCE_REJECTED'
    assert not (tmp_path/'fresh.db').exists() and not (tmp_path/'preflight').exists()


@pytest.fixture
def bound_plan(tmp_path,monkeypatch,cfg):
    source={'sha':'a'*40,'dirty':False}
    backend={'mount':str(tmp_path),'cap_bytes':10000000,'host':'fixture-host','filesystem':{'id':1}}
    monkeypatch.setattr(m,'source_identity',lambda:source)
    monkeypatch.setattr(v,'verify',lambda *a:deepcopy(backend))
    monkeypatch.setattr(m,'environment_identity',lambda p:{'host':'fixture-host','filesystem':{'id':1}})
    return m.prepare({'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED':'true'},tmp_path,10000000,'fixture-proof')


@pytest.mark.parametrize('drift',['host','filesystem','sha','strategy','mode'])
def test_validated_real_plan_rejects_actual_scope_drift_before_attempt(tmp_path,monkeypatch,bound_plan,drift):
    plan=deepcopy(bound_plan)
    if drift=='host':plan['environment']['host']='elsewhere'
    if drift=='filesystem':plan['backend']['filesystem']['id']=999
    if drift=='sha':plan['source']['sha']='b'*40
    if drift=='strategy':plan['config_env']['ALPHAFORGE_REJECT_FORWARD_HORIZON_BARS']='1'
    if drift=='mode':plan['config_env']['ALPHAFORGE_EXECUTION_MODE']='LIVE'
    with pytest.raises(ValueError):m.validate_plan(plan)
    assert not (tmp_path/'.af-real-calibration.control').exists()


def test_separate_capacity_contract_breaks_bootstrap_cycle_without_normal_gate_override(tmp_path,bound_plan,cfg):
    root=tmp_path/'experiment';root.mkdir()
    m.write_json(root/'guardian.json',{'supervisor_pid':123,'guardian_pid':os.getppid()})
    m.exclusive_json(root/'worker.attempt',{'plan_hash':canonical_hash(bound_plan),'pid':os.getpid()})
    path=tmp_path/'.af-real-calibration.control'
    c=v.ControlFile(path,{'plan_hash':canonical_hash(bound_plan),'supervisor_pid':123});c.close()
    permit=m.CalibrationPermit(bound_plan,path)
    policy=m.StoragePolicy.from_config(cfg.runtime)
    assert m.full_replay_duration_budget(root/'laboratory.db',policy,duration_seconds=21600)['status']=='STORAGE_GROWTH_BUDGET_UNMEASURED'
    result=permit.assess(root/'laboratory.db',cfg,21600)
    assert result['status']=='PASS' and result['normal_admission'] is False
    with pytest.raises(ValueError):permit.assess(tmp_path/'wrong.db',cfg,21600)
    with pytest.raises(ValueError):permit.assess(root/'laboratory.db',cfg,1)
    from alphaforge import burnin_ops as ops
    with pytest.raises(TypeError):ops.preflight(str(root/'laboratory.db'),'fixture',[],['1h','15m','1m'],calibration_permit=permit)


def test_backend_probe_requires_aggregate_capacity_not_single_file_or_sampled_bytes(tmp_path,monkeypatch):
    # Deliberately injected OS ENOSPC fixture, NOT a physical verification result.
    # Unmocked verify() rejects this ordinary temporary directory as a backend.
    identity={'mount':str(tmp_path),'cap_bytes':10000000,'backend':v.BACKEND}
    monkeypatch.setattr(v,'inspect_volume',lambda *a:identity)
    monkeypatch.setattr(v,'container_free',lambda p:999999999)
    def deny(command,**kw):
        return sandbox_success(command, **kw)
    monkeypatch.setattr(v.subprocess,'run',deny)
    real_write=os.write;written=0
    def quota(fd,data):
        nonlocal written
        if written>7500000:raise OSError(errno.ENOSPC,'fixture injected quota')
        n=real_write(fd,data);written+=n;return n
    monkeypatch.setattr(v.os,'write',quota)
    result=v.probe(tmp_path,10000000,'a'*40,100)
    assert result['status']=='PHYSICALLY_VERIFIED' and result['files_written']>=2
    assert result['aggregate_enospc'] and result['sandbox_denial']
    assert not list(tmp_path.glob('.af-fill-*'))
    assert (tmp_path/'.af-capacity-proof.control').exists()
    with pytest.raises(ValueError,match='EMPTY_DEDICATED'):v.probe(tmp_path,10000000,'a'*40,100)


def test_real_worker_selects_canonical_runner_and_never_confuses_early_exit_with_measurement(tmp_path,monkeypatch,cfg):
    """Real DB/campaign/controller; only the long-running runtime is a fixture.

    No scanner/network/real calibration is started. Early return must fail on
    absent runtime evidence; canonical orchestration selection is asserted.
    """
    import asyncio
    from alphaforge import burnin_ops as ops, burnin_campaign as campaign_module
    root=tmp_path/'experiment';root.mkdir()
    plan={'backend':{'mount':str(tmp_path)},'shutdown_grace_seconds':1}
    monkeypatch.setattr(m,'validate_plan',lambda p:cfg)
    monkeypatch.setattr(ops,'_preflight',lambda *a,**kw:{'status':'CALIBRATION_READY'})
    monkeypatch.setattr(ops,'_git_branch',lambda:'dev')
    monkeypatch.setattr(v,'usage',lambda p:{'quota_used_bytes':0,'host_container_free_bytes':9999999999})
    selected=[]
    async def finish_early(runner):
        selected.append((type(runner.candle_provider).__name__,type(runner.position_management_shadow_provider).__name__))
        from alphaforge.storage_policy import StorageController,StoragePolicy
        StorageController(runner.engine,StoragePolicy.from_config(cfg.runtime)).check(force=True)
        return {'status':'fixture early stop'}
    monkeypatch.setattr(campaign_module.BurnInCampaignRunner,'run_foreground',finish_early)
    guard=SimpleNamespace(is_set=lambda:False,set=lambda:None,worker_thread=SimpleNamespace(is_alive=lambda:True))
    with pytest.raises(ValueError,match='RUNTIME_INSTANCE_SCOPE'):
        asyncio.run(m.real_worker(plan,tmp_path/'control',guard))
    assert selected==[('BinanceReadOnlyCandleProvider','PaperPositionManagementShadowProvider')]
    assert (root/'maintenance.jsonl').exists()
    assert not (root/'worker-result.json').exists()


def test_scope_rejects_different_campaign_intervals_before_any_evidence_read(tmp_path,cfg):
    with pytest.raises(ValueError,match='INTERVAL_SCOPE'):
        m.admission_evidence(tmp_path/'missing',cfg,tmp_path/'fresh.db',21600,intervals=['4h'])


def test_private_worker_reentry_and_result_overwrite_are_refused(tmp_path,monkeypatch,cfg):
    # Exercise the actual worker entrypoint with no runtime: result stays partial.
    root=tmp_path/'experiment';root.mkdir()
    plan={'backend':{'mount':str(tmp_path)}}
    path=root/'plan.json';path.write_text(json.dumps(plan))
    control=v.ControlFile(tmp_path/'.af-real-calibration.control',{'plan_hash':canonical_hash(plan),'supervisor_pid':123});control.close()
    m.exclusive_json(root/'guardian.json',{'supervisor_pid':123,'guardian_pid':os.getppid()})
    monkeypatch.setattr(m,'validate_plan',lambda p:cfg)
    async def partial(plan,control,guard):
        guard.set()
        return {'complete':False,'admission_eligible':False}
    monkeypatch.setattr(m,'real_worker',partial)
    assert m.main(['_worker','--plan',str(path)])==0
    result=(root/'worker-result.json').read_bytes()
    with pytest.raises(FileExistsError):m.main(['_worker','--plan',str(path)])
    assert (root/'worker-result.json').read_bytes()==result
    with pytest.raises(FileExistsError):m.exclusive_json(root/'worker-result.json',{'complete':True})


def test_module_entrypoint_uses_one_canonical_permit_type():
    script='''
import argparse,runpy,sys

def inspect_before_any_action(*a,**kw):
    import alphaforge.storage_measurement as canonical
    assert canonical.CalibrationPermit is sys.modules['__main__'].CalibrationPermit
    raise SystemExit(0)
argparse.ArgumentParser.parse_args=inspect_before_any_action
runpy.run_module('alphaforge.storage_measurement',run_name='__main__',alter_sys=True)
'''
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,timeout=10,
        env={**os.environ,'PYTHONPATH':str(Path(m.__file__).resolve().parents[1]),'PYTHONDONTWRITEBYTECODE':'1'})
    assert result.returncode==0,result.stderr.decode()


@pytest.mark.parametrize('duration',[0,-1,float('nan'),float('inf')])
def test_normal_launch_cannot_use_zero_or_nonfinite_duration_to_evade_capacity(tmp_path,monkeypatch,duration):
    from alphaforge import burnin_ops as ops
    monkeypatch.setattr(ops,'preflight',lambda *a,**kw:pytest.fail('invalid duration must not start preflight'))
    result=ops.launch_campaign(str(tmp_path/'fresh.db'),'fixture',duration,[],['1h','15m','1m'],dynamic_universe=True)
    assert result=={'status':'FAILED_CLOSED','reason':'INVALID_DURATION'}
    assert not (tmp_path/'fresh.db').exists()


def test_control_fsync_failure_invalidates_favorable_slot(tmp_path,monkeypatch):
    control=v.ControlFile(tmp_path/'control',{'fixture':True})
    original=os.fsync;calls=0
    def fail_completion(fd):
        nonlocal calls
        calls+=1
        if calls==2:raise OSError(errno.EIO,'fixture commit fsync failed')
        return original(fd)
    monkeypatch.setattr(v.os,'fsync',fail_completion)
    with pytest.raises(OSError):control.put(1,{'status':'MEASURED'})
    with pytest.raises(ValueError):control.get(1)
    control.close()


def test_approved_digest_and_validated_plan_are_the_same_bytes(tmp_path,monkeypatch):
    import hashlib,io
    path=tmp_path/'plan.json'
    selected=b'{"selected":"not approved"}'
    approved=b'{"selected":"approved"}'
    path.write_bytes(selected)
    original=Path.open;reads=0
    def swap_on_read(p,*args,**kwargs):
        nonlocal reads
        if p==path:
            reads+=1
            return io.BytesIO(selected if reads==1 else approved)
        return original(p,*args,**kwargs)
    monkeypatch.setattr(Path,'open',swap_on_read)
    monkeypatch.setattr(m,'validate_plan',lambda p:pytest.fail('unapproved selected bytes must not reach admission'))
    with pytest.raises(ValueError,match='PLAN_DIGEST'):m.run(path,hashlib.sha256(approved).hexdigest())
    assert not (tmp_path/'.af-real-calibration.control').exists()


def test_runtime_redaction_covers_signed_urls_and_json_auth_fields():
    from alphaforge.storage_calibration import redact
    value='GET /?signature=private-hmac&timestamp=123 X-MBX-APIKEY: private-api {"signature": "json-hmac"}'
    result=redact(value)
    assert all(secret not in result for secret in ('private-hmac','private-api','json-hmac'))
    assert 'timestamp=123' in result


def test_four_gib_probe_reaches_native_capacity_with_bounded_inventory_latency(tmp_path,monkeypatch):
    # Simulated Mini inventory latency, NOT physical quota/admission evidence.
    cap=4*1024**3
    identity={'mount':str(tmp_path),'cap_bytes':cap,'backend':v.BACKEND}
    clock=[0.0];written=[0]
    monkeypatch.setattr(v,'inspect_volume',lambda *a:identity)
    def free(_):
        clock[0]+=.16
        return 100*1024**3
    monkeypatch.setattr(v,'container_free',free)
    monkeypatch.setattr(v.time,'monotonic',lambda:clock[0])
    def deny(command,**kw):
        return sandbox_success(command, **kw)
    monkeypatch.setattr(v.subprocess,'run',deny)
    def write(fd,data):
        if written[0]+len(data)>cap-8*1024**2:
            raise OSError(errno.ENOSPC,'fixture aggregate quota')
        written[0]+=len(data)
        return len(data)
    monkeypatch.setattr(v.os,'write',write)
    result=v.probe(tmp_path,cap,'a'*40,608174080)
    assert result['status']=='PHYSICALLY_VERIFIED',result
    assert result['aggregate_enospc'] and result['files_written']>=2
    assert result['wall_seconds']<600


def test_probe_reserves_headroom_for_entire_next_write(tmp_path, monkeypatch):
    # A below-reserve result must not be caused by the probe's own next write.
    cap, reserve = 4 * 1024**3, 608174080
    identity = {'mount': str(tmp_path), 'cap_bytes': cap, 'backend': v.BACKEND}
    monkeypatch.setattr(v, 'inspect_volume', lambda *a: identity)
    reads = iter([cap + reserve, reserve + 1])
    monkeypatch.setattr(v, 'container_free', lambda _: next(reads))
    def deny(command, **kw):
        return sandbox_success(command, **kw)
    monkeypatch.setattr(v.subprocess, 'run', deny)
    monkeypatch.setattr(v.os, 'write', lambda *a: pytest.fail('reserve must be checked before writing'))
    result = v.probe(tmp_path, cap, 'a' * 40, reserve)
    assert result['status'] == 'FAILED'
    assert result['aggregate_enospc'] is False
    assert result['reason'] == 'ValueError'
    assert not list(tmp_path.glob('.af-fill-*'))


def test_new_physical_attempt_preserves_failed_claim_and_is_consumed(tmp_path, monkeypatch):
    identity, failed = physical_fixture(tmp_path, monkeypatch)
    failed.update(status='FAILED', aggregate_enospc=False, reason='TimeoutError')
    old = tmp_path / '.af-capacity-proof.control'
    c = v.ControlFile(old); c.put(1, failed); c.close()
    before = old.read_bytes()
    monkeypatch.setattr(v, 'container_free', lambda _: 10**12)
    def deny(command, **kw):
        return sandbox_success(command, **kw)
    monkeypatch.setattr(v.subprocess, 'run', deny)
    written = 0
    def write(fd, data):
        nonlocal written
        if written + len(data) > 8_000_000:
            raise OSError(errno.ENOSPC, 'fixture aggregate cap')
        written += len(data)
        return len(data)
    monkeypatch.setattr(v.os, 'write', write)
    result = v.probe(tmp_path, 10_000_000, 'b'*40, 100, attempt_id='second')
    assert result['status'] == 'PHYSICALLY_VERIFIED'
    assert old.read_bytes() == before
    assert result['attempt_id'] == 'second'
    assert v.verify(tmp_path, 10_000_000, canonical_hash(result), 'b'*40)['host'] == 'host'
    with pytest.raises(ValueError, match='PHYSICAL_BACKEND'):
        v.verify(tmp_path, 10_000_000, canonical_hash(result), 'a'*40)
    with pytest.raises(FileExistsError):
        v.probe(tmp_path, 10_000_000, 'b'*40, 100, attempt_id='second')
    assert old.read_bytes() == before
    # A later change even to unused bytes of prior evidence breaks the chain.
    with old.open('r+b') as f:
        f.seek(-1, 2); f.write(b'x')
    with pytest.raises(ValueError, match='HISTORY'):
        v.verify(tmp_path, 10_000_000, canonical_hash(result), 'b'*40)


@pytest.mark.parametrize('attempt_id', ['', '../escape', 'bad/name', 'a'*65])
def test_physical_attempt_id_cannot_select_another_path(tmp_path, attempt_id):
    with pytest.raises(ValueError, match='ATTEMPT_ID'):
        v.probe(tmp_path, 10_000_000, 'a'*40, 100, attempt_id=attempt_id)
    assert list(tmp_path.iterdir()) == []


def test_successor_probe_serializes_without_modifying_original_inode(tmp_path, monkeypatch):
    physical_fixture(tmp_path, monkeypatch)
    path = tmp_path / '.af-capacity-proof.control'
    before = path.read_bytes()
    c = v.ControlFile(path)
    try:
        v.fcntl.flock(c.fd, v.fcntl.LOCK_EX | v.fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            v.probe(tmp_path, 10_000_000, 'b'*40, 100, attempt_id='racing')
    finally:
        c.close()
    assert path.read_bytes() == before
    assert not (tmp_path / '.af-capacity-proof.racing.control').exists()


@pytest.mark.parametrize('unowned', ['file', 'incomplete', 'symlink'])
def test_new_probe_does_not_hide_unknown_or_incomplete_evidence(tmp_path, monkeypatch, unowned):
    physical_fixture(tmp_path, monkeypatch)
    if unowned == 'file':
        (tmp_path / 'unowned.data').touch()
    elif unowned == 'incomplete':
        c = v.ControlFile(tmp_path / '.af-capacity-proof.unfinished.control', {'identity': {}}); c.close()
    else:
        (tmp_path / '.af-capacity-proof.link.control').symlink_to(tmp_path / '.af-capacity-proof.control')
    with pytest.raises(ValueError):
        v.probe(tmp_path, 10_000_000, 'b'*40, 100, attempt_id='not-started')
    assert not (tmp_path / '.af-capacity-proof.not-started.control').exists()


@pytest.mark.parametrize('publish_fails', [False, True])
def test_worker_exception_persists_redacted_failed_result_without_retry(tmp_path, monkeypatch, cfg, publish_fails):
    root = tmp_path / 'experiment'
    root.mkdir()
    plan = {'backend': {'mount': str(tmp_path)}}
    path = root / 'plan.json'
    path.write_text(json.dumps(plan))
    control = v.ControlFile(tmp_path / '.af-real-calibration.control',
                            {'plan_hash': canonical_hash(plan), 'supervisor_pid': 123})
    control.close()
    m.exclusive_json(root / 'guardian.json',
                     {'supervisor_pid': 123, 'guardian_pid': os.getppid()})
    monkeypatch.setattr(m, 'validate_plan', lambda p: cfg)
    monkeypatch.setenv('BINANCE_API_SECRET', 'fixture-sensitive-key')
    async def failed(plan, control, guard):
        guard.set()
        raise RuntimeError('STORAGE_PRESSURE_RECOVERY_REQUIRED fixture-sensitive-key ' + 'x' * 1000)
    monkeypatch.setattr(m, 'real_worker', failed)
    if publish_fails:
        original = m.exclusive_json
        def full(path, value):
            if path.name == 'worker-result.json': raise OSError(errno.ENOSPC, 'fixture full')
            return original(path, value)
        monkeypatch.setattr(m, 'exclusive_json', full)
        with pytest.raises(OSError, match='fixture full'):
            m.main(['_worker', '--plan', str(path)])
        assert not (root / 'worker-result.json').exists()
        with pytest.raises(FileExistsError):
            m.main(['_worker', '--plan', str(path)])
        return
    with pytest.raises(RuntimeError, match='STORAGE_PRESSURE_RECOVERY_REQUIRED'):
        m.main(['_worker', '--plan', str(path)])
    result = json.loads((root / 'worker-result.json').read_text())
    assert result['status'] == 'FAILED' and result['complete'] is False
    assert result['admission_eligible'] is False and result['plan_hash'] == canonical_hash(plan)
    assert result['reason'].startswith('RuntimeError:STORAGE_PRESSURE_RECOVERY_REQUIRED')
    assert len(result['reason']) <= 512 and 'fixture-sensitive-key' not in result['reason']
    before = (root / 'worker-result.json').read_bytes()
    with pytest.raises(FileExistsError):
        m.main(['_worker', '--plan', str(path)])
    assert (root / 'worker-result.json').read_bytes() == before


@pytest.mark.parametrize('mutation', ['none', 'wrong_plan', 'complete', 'missing_reason', 'missing',
                                     'zero_exit', 'wrong_version', 'admitted', 'secret'])
def test_supervisor_preserves_only_scoped_failed_worker_cause(tmp_path, monkeypatch, cfg, mutation):
    path = run_fixture_plan(tmp_path, monkeypatch, cfg)
    plan = json.loads(path.read_text())
    plan['wall_limit_seconds'] = 5  # allow bounded child startup on slow CI hosts
    path.write_text(json.dumps(plan))
    monkeypatch.setattr(m, 'source_identity', lambda: plan['source'])
    original = subprocess.Popen
    failure = {'version': m.VERSION, 'kind': 'REAL_RUNTIME', 'status': 'FAILED',
               'complete': False, 'admission_eligible': False,
               'plan_hash': canonical_hash(plan), 'reason': 'RuntimeError:STORAGE_PRESSURE_RECOVERY_REQUIRED'}
    if mutation == 'wrong_plan': failure['plan_hash'] = 'wrong'
    if mutation == 'complete': failure['complete'] = True
    if mutation == 'missing_reason': failure.pop('reason')
    if mutation == 'wrong_version': failure['version'] = 'wrong'
    if mutation == 'admitted': failure['admission_eligible'] = True
    if mutation == 'secret':
        monkeypatch.setenv('BINANCE_API_SECRET', 'fixture-sensitive-key')
        failure['reason'] += ' fixture-sensitive-key'
    def child(command, **kwargs):
        if command[0] != '/usr/bin/sandbox-exec': return original(command, **kwargs)
        script = 'import pathlib,sys; '
        if mutation != 'missing':
            script += 'pathlib.Path("worker-result.json").write_text(' + repr(json.dumps(failure)) + '); '
        script += 'sys.exit(' + ('0' if mutation == 'zero_exit' else '1') + ')'
        return original([sys.executable, '-c', script], **kwargs)
    monkeypatch.setattr(m.subprocess, 'Popen', child)
    result = m.run(path, m.file_hash(path))
    assert result['status'] == 'FAILED' and result['admission_eligible'] is False
    assert result['returncode'] == (0 if mutation == 'zero_exit' else 1)
    if mutation == 'none':
        assert result['worker'] == failure
        assert 'STORAGE_PRESSURE_RECOVERY_REQUIRED' in result['reason']
    elif mutation == 'secret':
        assert 'STORAGE_PRESSURE_RECOVERY_REQUIRED' in result['reason']
        assert 'fixture-sensitive-key' not in json.dumps(result)
    else:
        assert 'worker' not in result
        assert 'STORAGE_PRESSURE_RECOVERY_REQUIRED' not in result['reason']
    control = v.ControlFile(tmp_path / '.af-real-calibration.control')
    try: assert control.get(1) == result
    finally: control.close()


@pytest.mark.parametrize('setting,value', [
    ('ALPHAFORGE_SCAN_INTERVAL_SEC', '0.1'),
    ('ALPHAFORGE_HEARTBEAT_INTERVAL_SEC', '5'),
    ('ALPHAFORGE_RECONCILIATION_INTERVAL_SEC', '1'),
    ('ALPHAFORGE_STORAGE_CHECK_INTERVAL_SEC', '10'),
])
def test_admission_rejects_workload_drift_even_when_strategy_hashes_match(tmp_path, monkeypatch, cfg, setting, value):
    path, _ = measurement_fixture(tmp_path, monkeypatch, cfg)
    cert = m.review(path, 10, 'Fixture explicit operator scope only; not a measured future upper bound.')
    certificate = tmp_path / 'review.json'
    certificate.write_text(json.dumps(cert))
    env = {'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED': 'true',
           'ALPHAFORGE_STORAGE_GROWTH_BUDGET_BYTES_PER_SEC': '10'}
    prospective, identity = m.hashes(env)
    changed, changed_identity = m.hashes({**env, setting: value})
    if setting == 'ALPHAFORGE_STORAGE_CHECK_INTERVAL_SEC':
        assert identity != changed_identity  # already bound by the existing policy identity
    else:
        assert identity == changed_identity  # economic identity omits workload cadence
    assert m.admission_evidence(certificate, prospective, tmp_path / 'fresh.db', 21600)['status'] == 'PASS'
    with pytest.raises(ValueError, match='CONFIG'):
        m.admission_evidence(certificate, changed, tmp_path / 'changed.db', 21600)


@pytest.mark.parametrize('field,value', [('scan_interval_sec', 0.1), ('heartbeat_interval_sec', 5.0),
                                         ('reconciliation_interval_sec', 1.0)])
def test_calibration_permit_binds_full_runtime_cadence(tmp_path, bound_plan, cfg, field, value):
    from dataclasses import replace
    root = tmp_path / 'experiment'
    root.mkdir()
    m.write_json(root / 'guardian.json', {'supervisor_pid': 123, 'guardian_pid': os.getppid()})
    m.exclusive_json(root / 'worker.attempt', {'plan_hash': canonical_hash(bound_plan), 'pid': os.getpid()})
    path = tmp_path / '.af-real-calibration.control'
    control = v.ControlFile(path, {'plan_hash': canonical_hash(bound_plan), 'supervisor_pid': 123})
    control.close()
    permit = m.CalibrationPermit(bound_plan, path)
    assert permit.assess(root / 'laboratory.db', cfg, 21600)['status'] == 'PASS'
    changed = replace(cfg, runtime=replace(cfg.runtime, **{field: value}))
    with pytest.raises(ValueError, match='CALIBRATION_PERMIT_SCOPE'):
        permit.assess(root / 'laboratory.db', changed, 21600)
    routed = replace(cfg, runtime=replace(cfg.runtime, agent_graph_database_url=f'sqlite+pysqlite:///{root / "agent-shadow.db"}'))
    assert permit.assess(root / 'laboratory.db', routed, 21600)['status'] == 'PASS'
    outside = replace(cfg, runtime=replace(cfg.runtime, agent_graph_database_url='sqlite+pysqlite:////outside.db'))
    with pytest.raises(ValueError, match='CALIBRATION_PERMIT_SCOPE'):
        permit.assess(root / 'laboratory.db', outside, 21600)


@pytest.mark.parametrize('mutation', ['missing', 'forged'])
def test_full_runtime_certificate_binding_is_mandatory(tmp_path, monkeypatch, cfg, mutation):
    path, _ = measurement_fixture(tmp_path, monkeypatch, cfg)
    cert = m.review(path, 10, 'Fixture explicit operator scope only; not a future capacity guarantee.')
    if mutation == 'missing': cert.pop('prospective_workload_hash')
    else: cert['prospective_workload_hash'] = '0' * 64
    certificate = tmp_path / 'review.json'
    certificate.write_text(json.dumps(cert))
    prospective, _ = m.configuration({'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED': 'true',
                                      'ALPHAFORGE_STORAGE_GROWTH_BUDGET_BYTES_PER_SEC': '10'})
    with pytest.raises(ValueError, match='TAMPERED'):
        m.admission_evidence(certificate, prospective, tmp_path / 'fresh.db', 21600)


@pytest.mark.parametrize('target', ['fresh', 'existing', 'outside', 'shared', 'memory', 'postgres'])
def test_enabled_agent_database_is_fresh_and_on_measured_backend(tmp_path, monkeypatch, target):
    main = tmp_path / 'fresh.db'
    agent = tmp_path / 'agent.db'
    if target == 'outside': agent = tmp_path.parent / 'outside-agent.db'
    if target == 'shared': agent = main
    url = f'sqlite+pysqlite:///{agent}'
    if target == 'memory': url = 'sqlite+pysqlite:///:memory:'
    if target == 'postgres': url = 'postgresql://localhost/fixture'
    env = {'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED': 'true',
           'ALPHAFORGE_AGENT_GRAPH_ENABLED': 'true', 'ALPHAFORGE_AGENT_GRAPH_DATABASE_URL': url}
    cfg, _ = m.configuration(env)
    path, _ = measurement_fixture(tmp_path, monkeypatch, cfg, env)
    cert = m.review(path, 10, 'Fixture exact agent-storage scope only; no actual capacity claim.')
    certificate = tmp_path / 'review.json'
    certificate.write_text(json.dumps(cert))
    prospective, _ = m.configuration({**env, 'ALPHAFORGE_STORAGE_GROWTH_BUDGET_BYTES_PER_SEC': '10'})
    if target == 'existing': agent.touch()
    if target == 'fresh':
        assert m.admission_evidence(certificate, prospective, main, 21600)['status'] == 'PASS'
    else:
        with pytest.raises(ValueError, match='DATABASE'):
            m.admission_evidence(certificate, prospective, main, 21600)
    assert not main.exists()


@pytest.mark.parametrize('setting,value', [
    ('ALPHAFORGE_BINANCE_MARKET_DATA_BASE_URL', 'https://example.invalid'),
    ('HYPERLIQUID_ENABLED', 'false'),
    ('ALPHAFORGE_LOG_LEVEL', 'DEBUG'),
])
def test_provider_and_effective_config_cannot_inherit_other_profile_measurement(tmp_path, monkeypatch, cfg, setting, value):
    path, _ = measurement_fixture(tmp_path, monkeypatch, cfg)
    cert = m.review(path, 10, 'Fixture public-provider exact scope only; no live or market capacity claim.')
    certificate = tmp_path / 'review.json'
    certificate.write_text(json.dumps(cert))
    env = {'ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED': 'true',
           'ALPHAFORGE_STORAGE_GROWTH_BUDGET_BYTES_PER_SEC': '10', setting: value}
    changed, _ = m.configuration(env)
    with pytest.raises(ValueError, match='CONFIG'):
        m.admission_evidence(certificate, changed, tmp_path / 'fresh.db', 21600)
    assert not (tmp_path / 'fresh.db').exists()


def test_workload_scope_excludes_only_secret_values_and_owned_primary_db_location(cfg):
    from dataclasses import replace
    original = m.workload_config_scope(cfg)
    rotated = replace(cfg, notifications=replace(cfg.notifications, telegram_bot_token='fixture-sensitive-key'),
                      persistence=replace(cfg.persistence, database_url='sqlite+pysqlite:////fresh-owned.db'))
    assert m.workload_config_scope(rotated) == original
    assert 'telegram_bot_token' not in original['notifications']
    assert 'database_url' not in original['persistence']
    assert 'agent_graph_database_url' in original['runtime']
    assert cfg.notifications.telegram_bot_token == ''
