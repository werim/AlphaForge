"""Bounded real PAPER storage experiment; never normal PAPER admission.

Requires a preprovisioned APFS quota volume AND an actual capacity/sandbox proof.
No command provisions storage. Review is separate from measurement completion.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import hashlib
import importlib.metadata
import math
import os
from pathlib import Path
import selectors
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

from alphaforge.burnin import canonical_hash
from alphaforge.storage_calibration import (configuration, source_identity, environment_identity,
    file_hash, write_json, footprint, peaks, redact, parent_watchdog)
from alphaforge.storage_policy import StoragePolicy, full_replay_duration_budget
from alphaforge import storage_volume as volume

VERSION = "real-storage-calibration.v1"
DURATION = 21600
LEASE_SECONDS = 5
MAX_AGE = 86400
PURPOSE = "STORAGE_CALIBRATION_ONLY"
MODULE = "alphaforge.storage_measurement"
if __name__ == "__main__":
    # Shared permit type must be identical when -m entry calls other authorities.
    sys.modules[MODULE] = sys.modules[__name__]
HASH_KEYS = ("config_hash", "strategy_config_hash", "universe_hash", "execution_cost_config_hash")


def read_json(path: Path, limit=1024 * 1024):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("EVIDENCE_SIZE_BOUND")
    return json.loads(raw)


def exclusive_json(path: Path, value: dict):
    raw=json.dumps(value,sort_keys=True,allow_nan=False).encode()
    if len(raw)>1024*1024: raise ValueError('CONTROL_JSON_BOUND')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f:
        f.write(raw);f.flush();os.fsync(f.fileno())
    from alphaforge.storage_calibration import fsync_directory
    fsync_directory(path.parent)


def measurement_environment(mount):
    dependencies={}
    for distribution in importlib.metadata.distributions():
        name=distribution.metadata.get('Name')
        if name:
            normalized=name.lower().replace('_','-')
            if normalized in dependencies and dependencies[normalized]!=distribution.version:
                raise ValueError('AMBIGUOUS_DEPENDENCY_VERSION')
            dependencies[normalized]=distribution.version
    return {**environment_identity(mount),'dependencies':dependencies}


def hashes(env):
    cfg, ident = configuration(env)
    return cfg, {k: ident[k] for k in HASH_KEYS}


def validate_plan(plan):
    source = source_identity()
    cfg, identity = hashes(plan["config_env"])
    if (plan.get("version") != VERSION or plan.get("purpose") != PURPOSE or source["dirty"]
            or plan.get("source") != source or plan.get("config_hashes") != identity
            or plan.get("policy_hash") != StoragePolicy.from_config(cfg.runtime).identity_hash()
            or cfg.runtime.storage_growth_budget_bytes_per_sec != 0
            or plan.get("wall_limit_seconds") != DURATION + 660
            or plan.get("shutdown_grace_seconds") != 60 or plan.get("max_log_bytes") != 65536
            or plan.get("host_reserve_bytes") != StoragePolicy.from_config(cfg.runtime).reserve()
            or plan.get("duration_seconds") != DURATION
            or not 0 <= time.time() - plan.get("created_at", 0) <= MAX_AGE):
        raise ValueError("REAL_MEASUREMENT_SOURCE_CONFIG_TIME_SCOPE")
    mount = Path(plan["backend"]["mount"])
    if cfg.runtime.storage_archive_dir and Path(cfg.runtime.storage_archive_dir).resolve() != mount / "experiment/archive":
        raise ValueError("ARCHIVE_MUST_BE_QUOTA_LOCAL")
    actual = volume.verify(mount, plan["backend"]["cap_bytes"], plan["proof_hash"], source["sha"])
    if actual != plan["backend"] or measurement_environment(mount) != plan["environment"]:
        raise ValueError("REAL_MEASUREMENT_HOST_FILESYSTEM_SCOPE")
    return cfg


@dataclass(frozen=True)
class CalibrationPermit:
    plan: dict
    control_path: Path

    def assess(self, db: Path, cfg, duration: float):
        expected = validate_plan(self.plan)
        from alphaforge.burnin_campaign import build_phase8_campaign_identity
        current_identity = build_phase8_campaign_identity(cfg.runtime, [], ["1h", "15m", "1m"], dynamic_universe=True)
        root = Path(self.plan["backend"]["mount"])
        control = volume.ControlFile(self.control_path)
        try:
            claim = control.get(0)
        finally:
            control.close()
        if (self.control_path != root / ".af-real-calibration.control"
                or claim.get("plan_hash") != canonical_hash(self.plan)
                or db.resolve() != root / "experiment" / "laboratory.db"
                or read_json(root / "experiment/guardian.json").get("supervisor_pid") != claim.get("supervisor_pid")
                or read_json(root / "experiment/guardian.json").get("guardian_pid") != os.getppid()
                or read_json(root / "experiment/worker.attempt") != {"plan_hash":canonical_hash(self.plan),"pid":os.getpid()}
                or duration != DURATION
                or {k: current_identity[k] for k in HASH_KEYS} != self.plan["config_hashes"]
                or StoragePolicy.from_config(cfg.runtime) != StoragePolicy.from_config(expected.runtime)):
            raise ValueError("CALIBRATION_PERMIT_SCOPE")
        return {"status": "PASS", "purpose": PURPOSE, "normal_admission": False,
                "capacity_authority": volume.BACKEND, "cap_bytes": self.plan["backend"]["cap_bytes"]}


def prepare(env: dict, mount: Path, cap: int, proof_hash: str):
    source = source_identity()
    cfg, identity = hashes(env)
    policy = StoragePolicy.from_config(cfg.runtime)
    backend = volume.verify(mount, cap, proof_hash, source["sha"])
    if backend.get("proof_expires_at",time.time()+MAX_AGE) - time.time() < DURATION + 660:
        raise ValueError("PHYSICAL_PROOF_WILL_EXPIRE_DURING_MEASUREMENT")
    plan = {"version": VERSION, "purpose": PURPOSE, "created_at": time.time(),
            "source": source, "config_env": env, "config_hashes": identity,
            "policy_hash": policy.identity_hash(), "backend": backend, "proof_hash": proof_hash,
            "environment": measurement_environment(mount), "duration_seconds": DURATION,
            "wall_limit_seconds": DURATION + 660, "shutdown_grace_seconds": 60,
            "max_log_bytes": 65536, "host_reserve_bytes": policy.reserve()}
    validate_plan(plan)
    return plan


def scoped_coverage(db: Path, campaign_id: str, run_id: str) -> dict:
    """SELECT only; unknown tables/columns are evidence errors, never zero."""
    from alphaforge.storage_policy import readonly
    with readonly(db) as c:
        def count(table, extra="", params=()):
            return c.execute(f"SELECT COUNT(*) FROM {table} WHERE campaign_id=? AND burnin_run_id=? {extra}",
                             (campaign_id, run_id, *params)).fetchone()[0]
        instances = [r[0] for r in c.execute("SELECT DISTINCT instance_id FROM runtime_state_snapshots WHERE campaign_id=? AND burnin_run_id=?", (campaign_id, run_id))]
        if len(instances) != 1:
            raise ValueError("RUNTIME_INSTANCE_SCOPE_MISSING_OR_CHANGED")
        heartbeats = c.execute("SELECT heartbeat_ts,scanner_source,runtime_state FROM runtime_heartbeats WHERE runtime_instance_id=? AND execution_mode='PAPER' ORDER BY heartbeat_ts", (instances[0],)).fetchall()
        from datetime import datetime
        stamps = [datetime.fromisoformat(r[0].replace('Z', '+00:00')).timestamp() for r in heartbeats if r[2]=="OPERATING"]
        cycle_counts = [r[0] for r in c.execute("SELECT COUNT(*) FROM universe_selection_candidates u JOIN burnin_universe_selection_links l ON l.cycle_id=u.cycle_id WHERE l.campaign_id=? AND l.burnin_run_id=? GROUP BY l.cycle_id", (campaign_id, run_id))]
        eligibility = dict(c.execute("SELECT eligibility_status,COUNT(*) FROM burnin_position_management_shadow_proposals WHERE campaign_id=? AND burnin_run_id=? GROUP BY eligibility_status",(campaign_id,run_id)))
        actions = dict(c.execute("SELECT proposed_action,COUNT(*) FROM burnin_position_management_shadow_proposals WHERE campaign_id=? AND burnin_run_id=? GROUP BY proposed_action", (campaign_id, run_id)))
        failures = c.execute("SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=? AND (event_type LIKE '%FAILED%' OR event_type LIKE '%FAILURE%' OR event_type LIKE '%DEFERRED%')", (campaign_id,)).fetchone()[0]
        resolver = c.execute("SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=? AND event_type='RESOLVER_BATCH'", (campaign_id,)).fetchone()[0]
        reconciliations = c.execute("SELECT COUNT(*) FROM exchange_reconciliation_events WHERE instance_id=? AND mode='PAPER' AND status='CLEAN' AND exchange_read_only_status='AVAILABLE'", (instances[0],)).fetchone()[0]
        return {"runtime_instance_id": instances[0], "wall_heartbeat_span": stamps[-1] - stamps[0] if len(stamps) > 1 else 0,
                "heartbeat_max_gap": max((b-a for a,b in zip(stamps, stamps[1:])), default=0),
                "heartbeat_sources": sorted({r[1] for r in heartbeats}),
                "unhealthy_heartbeats": sum(r[2] not in {"OPERATING", "STOPPING"} for r in heartbeats),
                "scanner_cycles": len(cycle_counts), "max_candidates_per_cycle": max(cycle_counts, default=0),
                "candidate_rows": sum(cycle_counts), "observations": c.execute("SELECT COUNT(*) FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id=? AND o.burnin_run_id=?", (campaign_id, run_id)).fetchone()[0],
                "rejects": count("burnin_pending_reject_labels"), "positions": count("burnin_pending_position_outcomes"),
                "closed_positions": count("burnin_pending_position_outcomes", "AND status='CLOSED' AND evidence_complete=1"),
                "shadow_actions": actions, "shadow_eligibility": eligibility, "snapshots": count("runtime_state_snapshots"),
                "resolver_batches": resolver, "clean_reconciliations": reconciliations, "failures": failures,
                "integrity": c.execute('PRAGMA integrity_check').fetchone()[0],
                "foreign_key_violations": len(c.execute('PRAGMA foreign_key_check').fetchall()),
                "journal_mode": c.execute('PRAGMA journal_mode').fetchone()[0],
                "page_size": c.execute('PRAGMA page_size').fetchone()[0],
                "page_count": c.execute('PRAGMA page_count').fetchone()[0],
                "freelist_count": c.execute('PRAGMA freelist_count').fetchone()[0]}


def coverage_gaps(c: dict, runtime, maintenance_count: int):
    from alphaforge.position_management_shadow import SHADOW_ACTIONS
    required_actions = set(SHADOW_ACTIONS)
    gaps = [name for name in ("observations", "rejects", "positions", "closed_positions", "snapshots",
                              "resolver_batches", "clean_reconciliations", "candidate_rows") if c.get(name, 0) <= 0]
    if c.get("wall_heartbeat_span", 0) < DURATION:
        gaps.append("REAL_WALL_WINDOW_INCOMPLETE")
    if c.get("scanner_cycles", 0) < math.floor(DURATION / runtime.scan_interval_sec):
        gaps.append("SCANNER_CADENCE_INCOMPLETE")
    if c.get("heartbeat_max_gap", math.inf) > runtime.stale_market_data_sec:
        gaps.append("HEARTBEAT_GAP")
    if c.get("heartbeat_sources") != ["EXCHANGE_PUBLIC_MARKET_DATA"]:
        gaps.append("REAL_PROVIDER_SOURCE_MISSING")
    if not required_actions <= c.get("shadow_actions", {}).keys():
        gaps.append("SHADOW_ACTION_COVERAGE_INCOMPLETE")
    if c.get("unhealthy_heartbeats") != 0:
        gaps.append("UNHEALTHY_RUNTIME_HEARTBEAT")
    if c.get("controller_checkpoints",0) <= 0:
        gaps.append("CHECKPOINT_BEHAVIOR_NOT_OBSERVED")
    if (c.get("failures") != 0 or c.get("integrity") != "ok" or c.get("foreign_key_violations") != 0
            or c.get("journal_mode") != "wal" or maintenance_count <= 0):
        gaps.append("FAILED_OR_INCOMPLETE_CANONICAL_EVIDENCE")
    return gaps


def heartbeat_window(db, campaign_id, run_id):
    from datetime import datetime
    from alphaforge.storage_policy import readonly
    with readonly(db) as c:
        row=c.execute("""SELECT MIN(heartbeat_ts),MAX(heartbeat_ts) FROM runtime_heartbeats
            WHERE runtime_instance_id IN (SELECT DISTINCT instance_id FROM runtime_state_snapshots
            WHERE campaign_id=? AND burnin_run_id=?) AND execution_mode='PAPER' AND runtime_state='OPERATING'""",
            (campaign_id,run_id)).fetchone()
    if not row[0] or not row[1]: return 0
    return (datetime.fromisoformat(row[1].replace('Z','+00:00'))-datetime.fromisoformat(row[0].replace('Z','+00:00'))).total_seconds()


async def real_worker(plan, control_path: Path, guard):
    """Actual runtime, canonical scanner/providers, writers/resolvers/controller."""
    from sqlalchemy import create_engine
    from alphaforge import burnin_ops as ops
    from alphaforge.burnin_campaign import (create_campaign, start_or_resume_campaign,
        BurnInCampaignRunner, BinanceReadOnlyCandleProvider, pause_campaign, event)
    from alphaforge.position_management_shadow import PaperPositionManagementShadowProvider
    cfg = validate_plan(plan)
    mount = Path(plan["backend"]["mount"])
    root, db = mount / "experiment", mount / "experiment/laboratory.db"
    release = "storage-calibration-" + canonical_hash(plan)[:16]
    # For a feature checkout use the existing canonical source fingerprint token.
    if ops._git_branch().startswith("feature/"):
        release += "-wt-" + ops._git_worktree_fingerprint()[:12]
    previous_environment = {k: os.environ.get(k) for k in ("ALPHAFORGE_RELEASE_ID", "ALPHAFORGE_BURNIN_DATABASE_PATH")}
    try:
        os.environ["ALPHAFORGE_RELEASE_ID"] = release
        os.environ["ALPHAFORGE_BURNIN_DATABASE_PATH"] = str(db)
        pf = ops._preflight(str(db), release, [], ["1h", "15m", "1m"], dynamic_universe=True,
                            output_dir=root / "preflight", expected_duration_seconds=DURATION,
                            calibration_permit=CalibrationPermit(plan, control_path))
        if pf["status"] != "CALIBRATION_READY":
            raise ValueError("CALIBRATION_PREFLIGHT_FAILED")
        conn = ops._connect(str(db))
        try:
            campaign = create_campaign(conn, release_id=release, duration_days=DURATION / 86400,
                symbols=[], intervals=["1h", "15m", "1m"], runtime_config=cfg.runtime,
                source_provenance={"purpose": PURPOSE, "provider": "BINANCE_READ_ONLY_KLINES", "exchange": "binance", "mode": "PAPER"}, dynamic_universe=True)
            start = start_or_resume_campaign(conn, campaign.campaign_id)
            event(conn, campaign.campaign_id, "STORAGE_CALIBRATION_STARTED", burnin_run_id=start["burnin_run_id"], details={"plan_hash": canonical_hash(plan)})
            conn.commit()
        finally:
            conn.close()
        engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
        runner = BurnInCampaignRunner(engine, campaign.campaign_id,
            BinanceReadOnlyCandleProvider(base_url=cfg.binance.market_data_base_url),
            position_management_shadow_provider=PaperPositionManagementShadowProvider(max_evidence_age_seconds=cfg.runtime.stale_market_data_sec),
            provider_transient_outage_grace_seconds=cfg.runtime.provider_transient_outage_grace_seconds,
            storage_calibration_permit=CalibrationPermit(plan,control_path))
        maintenance, observed_peaks, previous, positive_growth = [], {}, 0, 0
        sample_lock = threading.Lock()
        from alphaforge import storage_policy as storage
        controller_class = storage.StorageController
        original_check = controller_class.check
        original_checkpoint, original_archive = storage.checkpoint, storage.verified_archive
        checkpoints = []
        def sample():
            nonlocal observed_peaks, previous, positive_growth
            with sample_lock:
                value = {**footprint(root), **volume.usage(plan["backend"])}
                value["control_files_bytes"] = sum(p.stat().st_size for p in mount.iterdir() if p.is_file())
                observed_peaks = peaks([observed_peaks, value])
                positive_growth += max(0, value["db_wal_bytes"] - previous)
                previous = value["db_wal_bytes"]
                with (root / 'samples.jsonl').open('a') as f:
                    f.write(json.dumps({"time": time.time(), **value}) + '\n')
                return value
        def check(controller, *args, **kwargs):
            if not kwargs.get("force") and time.monotonic() < controller.next_check:
                return original_check(controller, *args, **kwargs)
            sample()
            report = original_check(controller, *args, **kwargs)
            sample()
            with sample_lock:
                maintenance.append({"blocked": report.get("blocked"), "status": report.get("status"), "checkpoint": report.get("checkpoint"), "archives_verified": report.get("archives_verified",0), "blockers": report.get("blockers")})
                with (root / 'maintenance.jsonl').open('a') as f:
                    f.write(json.dumps({"time": time.time(), "report": report}, default=str) + '\n')
            return report
        def measured_checkpoint(*args, **kwargs):
            sample()
            report = original_checkpoint(*args, **kwargs)
            sample()
            with sample_lock:
                checkpoints.append(report)
                with (root / 'checkpoints.jsonl').open('a') as f:
                    f.write(json.dumps({"time":time.time(),"report":report},default=str)+'\n')
            return report
        def measured_archive(*args, **kwargs):
            sample()
            report = original_archive(*args, **kwargs)
            sample()  # verified archive exists before canonical source pruning
            return report
        storage.checkpoint, storage.verified_archive = measured_checkpoint, measured_archive
        controller_class.check = check
        deadline = time.monotonic() + DURATION + 600
        async def stop_window():
            while time.monotonic() < deadline:
                if guard.is_set() or not guard.worker_thread.is_alive():
                    os._exit(75)
                sample()
                try:
                    span = await asyncio.to_thread(heartbeat_window, db, campaign.campaign_id, start["burnin_run_id"])
                except (sqlite3.OperationalError, ValueError):
                    await asyncio.sleep(1)
                    continue  # startup incomplete, never counted as healthy coverage
                if span >= DURATION:
                    with engine.begin() as c:
                        pause_campaign(c, campaign.campaign_id)
                    return
                await asyncio.sleep(1)
            raise TimeoutError("REAL_WINDOW_DEADLINE")
        work = asyncio.create_task(runner.run_foreground())
        timer = asyncio.create_task(stop_window())
        try:
            done, _ = await asyncio.wait([work, timer], return_when=asyncio.FIRST_COMPLETED)
            if timer in done:
                timer.result()
                await asyncio.wait_for(work, plan['shutdown_grace_seconds'])
            else:
                work.result()
            coverage = scoped_coverage(db, campaign.campaign_id, start["burnin_run_id"])
            sample()
            coverage["controller_checkpoints"] = len(checkpoints)
            coverage["archives_verified"] = sum(r["archives_verified"] for r in maintenance)
            gaps = coverage_gaps(coverage, cfg.runtime, len(maintenance))
            if cfg.runtime.storage_archive_dir and not coverage["archives_verified"]:
                gaps.append("ARCHIVE_BEHAVIOR_NOT_OBSERVED")
            if any(r["blocked"] or r["blockers"] for r in maintenance):
                gaps.append("CANONICAL_STORAGE_BLOCKED")
            return {"version": VERSION, "kind": "REAL_RUNTIME", "complete": not gaps,
                    "admission_eligible": False, "plan_hash": canonical_hash(plan), "coverage": coverage,
                    "gaps": gaps, "campaign_id": campaign.campaign_id, "burnin_run_id": start["burnin_run_id"],
                    "sampled_peaks": observed_peaks, "positive_db_wal_growth_bytes": positive_growth,
                    "maintenance_count": len(maintenance), "peak_limit_kind": "SAMPLED_LOWER_BOUND_WITH_KERNEL_QUOTA_CAP",
                    "representativeness":"OBSERVED_SCOPE_ONLY_REQUIRES_OPERATOR_REVIEW",
                    "runtime_returned": True,"worker_pid":os.getpid(),"guardian_pid":os.getppid()}
        finally:
            for task in (work, timer):
                if not task.done(): task.cancel()
            await asyncio.gather(work, timer, return_exceptions=True)
            guard.set()
            controller_class.check = original_check
            storage.checkpoint, storage.verified_archive = original_checkpoint, original_archive
            engine.dispose()

    finally:
        for key,value in previous_environment.items():
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value


def worker_argv(plan_path):
    return [sys.executable, '-m', MODULE, '_worker', '--plan', str(plan_path)]


def guardian(plan, control_path: Path):
    """Independent watchdog process: stdin EOF/lease expiry kills its owned worker."""
    parent_pid = os.getppid()
    exclusive_json(control_path.parent / "experiment/guardian.json", {"supervisor_pid": parent_pid, "guardian_pid": os.getpid()})
    child = subprocess.Popen(worker_argv(control_path.parent / 'experiment/plan.json'), stdin=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin.buffer, selectors.EVENT_READ)
    last = time.monotonic()
    try:
        while child.poll() is None:
            if os.getppid() != parent_pid or time.monotonic() - last > LEASE_SECONDS:
                raise RuntimeError('SUPERVISOR_LEASE_LOST')
            for key, _ in selector.select(timeout=0.1):
                if not os.read(key.fd, 1024):
                    raise RuntimeError('SUPERVISOR_PIPE_CLOSED')
                last = time.monotonic()
        return child.returncode
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        selector.close()


def run(plan_path: Path, digest: str):
    os.environ['GIT_OPTIONAL_LOCKS']='0'
    with plan_path.open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('PLAN_DIGEST_MISMATCH')
    plan = json.loads(raw)
    cfg = validate_plan(plan)
    mount = Path(plan['backend']['mount'])
    if (mount / 'experiment').exists():
        raise ValueError('CALIBRATION_NO_RESTART')
    # Reservation precedes output preparation, preflight, network and worker.
    control = volume.ControlFile(mount / '.af-real-calibration.control',
        {'plan_hash': canonical_hash(plan), 'supervisor_pid': os.getpid(), 'created_at': time.time()})
    result = {'version': VERSION, 'status': 'FAILED', 'kind': 'REAL_RUNTIME',
              'admission_eligible': False, 'plan': plan, 'completed_at': None, 'returncode': None}
    logs = {'stdout': bytearray(), 'stderr': bytearray()}
    proc = None
    selector = selectors.DefaultSelector()
    secrets = tuple(v for k, v in os.environ.items() if any(w in k.upper() for w in ('SECRET','TOKEN','KEY','PASSWORD')) and len(v) >= 4)
    try:
        if volume.container_free(plan['backend']) < plan['backend']['cap_bytes'] + plan['host_reserve_bytes']:
            raise ValueError('HOST_BACKING_RESERVE_INSUFFICIENT')
        root = mount / 'experiment'
        root.mkdir(mode=0o700)
        (root / 'tmp').mkdir(mode=0o700)
        write_json(root / 'plan.json', plan)
        source_root = Path(__file__).resolve().parents[2]
        git_dir = subprocess.check_output(['git','-C',str(source_root),'rev-parse','--absolute-git-dir'], text=True).strip()
        env = {k: v for k, v in os.environ.items() if k in ('PATH','LANG','LC_ALL','SYSTEMROOT','BINANCE_API_KEY','BINANCE_API_SECRET')}
        env.update({k: str(v) for k,v in plan['config_env'].items()})
        env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(source_root/'src'), TMPDIR=str(root/'tmp'),
                   GIT_DIR=git_dir, GIT_WORK_TREE=str(source_root), GIT_OPTIONAL_LOCKS='0', ALPHAFORGE_DATABASE_URL=f'sqlite+pysqlite:///{root / "laboratory.db"}',
                   ALPHAFORGE_AGENT_GRAPH_DATABASE_URL=f'sqlite+pysqlite:///{root / "agent-shadow.db"}')
        proc = subprocess.Popen(['/usr/bin/sandbox-exec','-p',volume.sandbox_profile(mount),sys.executable,
            '-m',MODULE,'_guardian','--plan',str(root/'plan.json')], cwd=root, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for name, stream in (('stdout',proc.stdout),('stderr',proc.stderr)):
            selector.register(stream, selectors.EVENT_READ, name)
        started = time.monotonic()
        last_resource = 0.0
        while selector.get_map() or proc.poll() is None:
            if time.monotonic() - started > plan['wall_limit_seconds']:
                raise TimeoutError('REAL_CALIBRATION_TIMEOUT')
            if proc.poll() is None:
                try:
                    proc.stdin.write(b'.')
                    proc.stdin.flush()
                except BrokenPipeError:
                    pass  # final worker returncode/evidence must still validate
            if time.monotonic() - last_resource > 1:
                if volume.container_free(plan['backend']) < plan['host_reserve_bytes']:
                    raise ValueError('HOST_RESERVE_VIOLATION')
                last_resource = time.monotonic()
            for key, _ in selector.select(timeout=0.1):
                block = os.read(key.fd,4096)
                if not block:
                    selector.unregister(key.fileobj)
                else:
                    log = logs[key.data]
                    room = plan['max_log_bytes'] - len(log)
                    log.extend(block[:max(0,room)])
                    if len(block) > room:
                        raise ValueError('LOG_BOUND_EXCEEDED')
            # Persist bounded redacted output even before a crash/deadline.
            for index,name in ((2,'stdout'),(3,'stderr')):
                control.put(index, {'text': redact(logs[name].decode(errors='replace'),secrets)})
        result['returncode'] = proc.wait(timeout=5)
        validate_plan(plan)  # dependency/backend/environment drift during the window also fails
        worker = read_json(root / 'worker-result.json')
        if (worker.get('plan_hash') != canonical_hash(plan)
                or source_identity() != plan['source']):
            raise ValueError('WORKER_OR_SOURCE_EVIDENCE_FAILED')
        if proc.returncode != 0 or worker.get('status') == 'FAILED':
            if (proc.returncode != 0 and worker.get('version') == VERSION and worker.get('kind') == 'REAL_RUNTIME'
                    and worker.get('status') == 'FAILED' and worker.get('complete') is False
                    and worker.get('admission_eligible') is False
                    and isinstance(worker.get('reason'), str) and 0 < len(worker['reason']) <= 512):
                # Preserve attributable failure evidence without making it measurable.
                worker['reason'] = redact(worker['reason'], secrets)
                result['worker'] = worker
                raise RuntimeError('REAL_WORKER_FAILED:' + worker['reason'])
            raise ValueError('WORKER_OR_SOURCE_EVIDENCE_FAILED')
        result.update(artifacts=artifact_manifest(root), worker=worker, status='MEASURED' if worker.get('complete') is True else 'INCOMPLETE')
    except Exception as exc:
        result['reason'] = type(exc).__name__ + ':' + redact(str(exc),secrets)[:512]
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            result['returncode'] = proc.wait(timeout=5)
            for stream in (proc.stdin,proc.stdout,proc.stderr): stream.close()
        selector.close()
        result['completed_at'] = time.time()
        try:
            control.put(1,result)
        finally:
            control.close()
    return result


def artifact_manifest(root: Path):
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('MEASUREMENT_SYMLINK_FORBIDDEN')
        if path.is_file():
            files[str(path.relative_to(root))] = file_hash(path)
    return files


def reviewed_measurement(control_path: Path):
    control = volume.ControlFile(control_path)
    try:
        claim, result = control.get(0), control.get(1)
    finally:
        control.close()
    plan, worker = result['plan'], result.get('worker',{})
    cfg = validate_plan(plan)
    if (claim.get('plan_hash') != canonical_hash(plan) or result.get('status') != 'MEASURED'
            or result.get('kind') != 'REAL_RUNTIME'
            or result.get('artifacts') != artifact_manifest(Path(plan['backend']['mount'])/'experiment')
            or result.get('returncode') != 0 or worker.get('kind') != 'REAL_RUNTIME'
            or worker.get('complete') is not True or worker.get('plan_hash') != canonical_hash(plan)
            or worker.get('gaps') != [] or coverage_gaps(worker.get('coverage',{}),cfg.runtime,worker.get('maintenance_count',0))
            or not 0 <= time.time() - result.get('completed_at',0) <= MAX_AGE):
        raise ValueError('MEASUREMENT_INCOMPLETE_FAILED_STALE_OR_NOT_REAL')
    return result


def review(control_path: Path, rate: int, justification: str):
    result = reviewed_measurement(control_path)
    plan = result['plan']
    if type(rate) is not int or rate <= 0 or len(justification.strip()) < 32:
        raise ValueError('EXPLICIT_OPERATOR_RATE_AND_SCOPE_JUSTIFICATION_REQUIRED')
    # Necessary empirical floor only, never a claimed universal upper bound.
    if rate * DURATION < result['worker']['positive_db_wal_growth_bytes']:
        raise ValueError('RATE_BELOW_OBSERVED_POSITIVE_GROWTH')
    prospective = dict(plan['config_env'], ALPHAFORGE_STORAGE_GROWTH_BUDGET_BYTES_PER_SEC=str(rate))
    cfg, identity = hashes(prospective)
    budget = full_replay_duration_budget(Path(plan['backend']['mount'])/'prospective-empty.db', StoragePolicy.from_config(cfg.runtime), duration_seconds=DURATION)
    if budget['status'] != 'PASS':
        raise ValueError('REVIEWED_BUDGET_CANNOT_FIT_TARGET_POLICY')
    return {'version':VERSION, 'kind':'OPERATOR_SCOPED_EMPIRICAL_BUDGET','admission_eligible':True,
            'control':str(control_path.resolve()), 'result_hash':canonical_hash(result),
            'prospective_config_hashes':identity,'rate':rate,'justification':justification,
            'expires_at':min(result['completed_at']+MAX_AGE,plan.get('created_at',result['completed_at'])+MAX_AGE,plan['backend'].get('proof_expires_at',result['completed_at']+MAX_AGE)),'universal_upper_bound':False}


def admission_evidence(path, cfg, db: Path, duration, *, intervals=("1h","15m","1m")):
    if sorted(intervals) != ["15m","1h","1m"]:
        raise ValueError("STORAGE_EVIDENCE_INTERVAL_SCOPE")
    if path is None:
        raise ValueError('STORAGE_ADMISSION_EVIDENCE_REQUIRED')
    certificate = read_json(Path(path))
    if (certificate.get('version') != VERSION or certificate.get('admission_eligible') is not True
            or certificate.get('kind') != 'OPERATOR_SCOPED_EMPIRICAL_BUDGET'
            or certificate.get('universal_upper_bound') is not False):
        raise ValueError('INVALID_STORAGE_ADMISSION_CERTIFICATE')
    result = reviewed_measurement(Path(certificate['control']))
    expected = review(Path(certificate['control']),certificate['rate'],certificate['justification'])
    if certificate != expected or canonical_hash(result) != certificate['result_hash']:
        raise ValueError('STORAGE_REVIEW_OR_MEASUREMENT_TAMPERED')
    if not 0 < duration <= DURATION or time.time() > certificate['expires_at']:
        raise ValueError('STORAGE_EVIDENCE_DURATION_OR_EXPIRY')
    from alphaforge.burnin_campaign import build_phase8_campaign_identity
    identity = build_phase8_campaign_identity(cfg.runtime,[],['1h','15m','1m'],dynamic_universe=True)
    if ({k:identity[k] for k in HASH_KEYS} != certificate['prospective_config_hashes']
            or cfg.runtime.storage_growth_budget_bytes_per_sec != certificate['rate']):
        raise ValueError('STORAGE_EVIDENCE_PROSPECTIVE_CONFIG_MISMATCH')
    mount = Path(result['plan']['backend']['mount'])
    if (not db.resolve().is_relative_to(mount) or db.exists() or db.is_symlink()
            or any(Path(str(db)+suffix).exists() for suffix in ('-wal','-shm'))):
        raise ValueError('FRESH_DATABASE_ON_MEASURED_FILESYSTEM_REQUIRED')
    existing_parent=db.resolve().parent
    while not existing_parent.exists(): existing_parent=existing_parent.parent
    if volume.filesystem_identity(existing_parent)!=result['plan']['backend']['filesystem']:
        raise ValueError('STORAGE_EVIDENCE_TARGET_FILESYSTEM_MISMATCH')
    if volume.container_free(result['plan']['backend']) < StoragePolicy.from_config(cfg.runtime).reserve(0,duration):
        raise ValueError('STORAGE_EVIDENCE_HOST_CONTAINER_RESERVE_INSUFFICIENT')
    # Existing measurement files are retained; free-space check counts them.
    if shutil.disk_usage(mount).free < StoragePolicy.from_config(cfg.runtime).reserve(0,duration):
        raise ValueError('STORAGE_EVIDENCE_RETAINED_FILES_LEAVE_INSUFFICIENT_RESERVE')
    return {'status':'PASS','kind':certificate['kind'],'result_hash':certificate['result_hash'],
            'scope':'SAME_SHA_CONFIG_HOST_QUOTA_FILESYSTEM_UP_TO_SIX_HOURS','universal_upper_bound':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='cmd',required=True)
    for command in ('inspect','sandbox-preflight','probe','prepare'):
        p=sub.add_parser(command);p.add_argument('--mount',type=Path,required=True);p.add_argument('--cap-bytes',type=int,required=True)
        if command=='probe':
            p.add_argument('--host-reserve-bytes',type=int,required=True)
            p.add_argument('--attempt-id',help='Explicitly authorized successor; preserves and locks original evidence')
        if command=='prepare':
            p.add_argument('--proof-hash',required=True);p.add_argument('--config-input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('run');p.add_argument('--plan',type=Path,required=True);p.add_argument('--plan-sha256',required=True)
    p=sub.add_parser('review');p.add_argument('--control',type=Path,required=True);p.add_argument('--rate',type=int,required=True);p.add_argument('--justification',required=True);p.add_argument('--output',type=Path,required=True)
    for command in ('_guardian','_worker'):
        p=sub.add_parser(command,help=argparse.SUPPRESS);p.add_argument('--plan',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.cmd=='inspect':
        result={'geometry':volume.inspect_volume(args.mount,args.cap_bytes),'backend_verification':'UNVERIFIED_WITHOUT_PHYSICAL_PROOF','ready':False}
    elif args.cmd=='sandbox-preflight':
        source=source_identity()
        if source['dirty']: raise ValueError('DIRTY_SOURCE')
        identity=volume.inspect_volume(args.mount,args.cap_bytes)
        result=volume.sandbox_preflight(args.mount,source['sha'])
        # Full bounded diagnostics, not a physical proof or admission certificate.
        print(json.dumps({'command':args.cmd,'identity':identity,'verification':result,
                          'admission_eligible':False,'physical_acceptance':'NOT_RUN'},sort_keys=True))
        return 0 if volume.sandbox_verified(result,args.mount,source['sha']) else 3
    elif args.cmd=='probe':
        source=source_identity()
        if source['dirty']: raise ValueError('DIRTY_SOURCE')
        try:
            result=volume.probe(args.mount,args.cap_bytes,source['sha'],args.host_reserve_bytes,attempt_id=args.attempt_id)
        except volume.SandboxPreflightError as exc:
            print(json.dumps({'command':args.cmd,'status':'FAILED','reason':str(exc),
                              'sandbox_verification':exc.evidence,'admission_eligible':False,
                              'physical_attempt_started':False},sort_keys=True))
            return 3
    elif args.cmd=='prepare':
        env=read_json(args.config_input);env=env.get('env',env)
        result=prepare(env,args.mount,args.cap_bytes,args.proof_hash)
        if not args.output.resolve().is_relative_to(args.mount.resolve()): raise ValueError('PLAN_MUST_BE_QUOTA_LOCAL')
        exclusive_json(args.output,result)
    elif args.cmd=='run': result=run(args.plan,args.plan_sha256)
    elif args.cmd=='review':
        result=review(args.control,args.rate,args.justification)
        if not args.output.resolve().is_relative_to(args.control.parent.resolve()): raise ValueError('REVIEW_MUST_BE_QUOTA_LOCAL')
        exclusive_json(args.output,result)
    else:
        plan=read_json(args.plan);validate_plan(plan)
        control_path=Path(plan['backend']['mount'])/'.af-real-calibration.control'
        control=volume.ControlFile(control_path)
        try: claim=control.get(0)
        finally: control.close()
        if claim.get('plan_hash')!=canonical_hash(plan): raise ValueError('WORKER_CLAIM_SCOPE')
        if args.cmd=='_guardian':
            if claim['supervisor_pid']!=os.getppid(): raise ValueError('GUARDIAN_PARENT_SCOPE')
            return guardian(plan,control_path)
        guardian_note=read_json(args.plan.parent/'guardian.json')
        if guardian_note != {'supervisor_pid':claim['supervisor_pid'],'guardian_pid':os.getppid()}:
            raise ValueError('WORKER_GUARDIAN_SCOPE')
        exclusive_json(args.plan.parent/'worker.attempt',{'plan_hash':canonical_hash(plan),'pid':os.getpid()})
        guard=parent_watchdog(os.getppid(),args.plan.parent)
        try:
            result=asyncio.run(real_worker(plan,control_path,guard))
        except Exception as exc:
            secrets = tuple(v for k, v in os.environ.items() if any(w in k.upper() for w in ('SECRET','TOKEN','KEY','PASSWORD')) and len(v) >= 4)
            exclusive_json(args.plan.parent/'worker-result.json', {
                'version': VERSION, 'kind': 'REAL_RUNTIME', 'status': 'FAILED',
                'complete': False, 'admission_eligible': False, 'plan_hash': canonical_hash(plan),
                'reason': (type(exc).__name__ + ':' + redact(str(exc), secrets))[:512]})
            raise  # retain the original traceback and nonzero process exit
        exclusive_json(args.plan.parent/'worker-result.json',result)
        return 0
    print(json.dumps({'command':args.cmd,'status':result.get('status','PREPARED'),'admission_eligible':result.get('admission_eligible',False),'hash':canonical_hash(result)},sort_keys=True))
    return 0 if args.cmd not in ('run','probe') or result.get('status') in ('MEASURED','PHYSICALLY_VERIFIED') else 3


if __name__=='__main__':
    raise SystemExit(main())
