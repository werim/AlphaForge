from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import selectors
from pathlib import Path
import sqlite3
import sys
import subprocess
import time

import pytest

from alphaforge.burnin import canonical_hash
from alphaforge import storage_calibration as cal


@pytest.fixture
def plan(tmp_path):
    result = cal.example({"ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED": "true"}, "a" * 40,
                       duration=2, candidate_count=3)
    result["environment_scope"]["filesystem"] = cal.filesystem_identity(tmp_path)
    return result


def source():
    return {"sha": "a" * 40, "dirty": False, "tree": "b" * 40, "modules": {}}


def test_atomic_attempt_race_and_reentry_precede_disk_preparation(tmp_path):
    out = tmp_path / "new-lab"
    def attempt(_):
        try:
            cal.reservation(out, "digest")
            return "reserved"
        except FileExistsError:
            return "refused"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert results.count("reserved") == 1
    assert results.count("refused") == 15
    assert not out.exists()
    assert (tmp_path / ".new-lab.attempt.json").stat().st_mode & 0o777 == 0o600


def invoke(monkeypatch, tmp_path, plan):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    monkeypatch.setattr(cal, "source_identity", source)
    return path, cal.run(path, tmp_path / "lab", cal.file_hash(path))


def test_preparation_failure_is_durable_and_attempt_consumed(monkeypatch, tmp_path, plan):
    # Even a failed mkdir/collision cannot recycle the same attempt.
    (tmp_path / "lab").mkdir()
    path, result = invoke(monkeypatch, tmp_path, plan)
    assert result["status"] == "FAILED"
    assert "FRESH_OUTPUT_COLLISION" in result["reason"]
    assert json.loads((tmp_path / ".lab.result.json").read_text()) == result
    assert result["recommended_growth_budget_bytes_per_sec"] is None
    saved_claim = (tmp_path / ".lab.attempt.json").read_bytes()
    with pytest.raises(ValueError, match="RESULT_ALREADY_EXISTS"):
        cal.run(path, tmp_path / "lab", cal.file_hash(path))
    assert (tmp_path / ".lab.attempt.json").read_bytes() == saved_claim
    assert json.loads((tmp_path / ".lab.result.json").read_text()) == result


def test_source_config_workload_scope_fail_before_attempt(tmp_path, plan):
    for key, value in [("code_sha", "wrong"), ("policy_hash", "wrong"), ("workload_hash", "wrong")]:
        changed = deepcopy(plan)
        changed[key] = value
        with pytest.raises(ValueError):
            cal.validate(changed, source())
    changed = deepcopy(plan)
    changed["config_env"]["ALPHAFORGE_STORAGE_STATS_ONLY_ENABLED"] = "true"
    with pytest.raises(ValueError):
        cal.validate(changed, source())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", ["empty", "missing_provenance", "short", "out_of_order", "nan"])
def test_incomplete_workload_never_validates(plan, change):
    if change == "empty":
        plan["workload"]["events"] = []
    elif change == "missing_provenance":
        plan["workload"]["provenance"] = {}
    elif change == "short":
        plan["workload"]["events"][-1]["at"] = 1
    elif change == "nan":
        plan["workload"]["events"][-1]["at"] = float("nan")
    else:
        plan["workload"]["events"].reverse()
    plan["workload_hash"] = canonical_hash(plan["workload"])
    with pytest.raises(ValueError):
        cal.validate(plan, source())


def limits(**changes):
    return {"timeout_seconds": 1, "max_bytes": 16 * 1024**2,
            "host_reserve_bytes": 1, "max_log_bytes": 4096, **changes}


def test_timeout_kills_only_owned_child_and_captures_redacted_logs(tmp_path):
    result = cal.supervise(
        [sys.executable, "-c", "import time; print('token=private-value',flush=True); time.sleep(20)"],
        tmp_path, limits(), {}, (),
    )
    assert result["abort_reason"] == "TIMEOUT"
    assert result["returncode"] != 0
    assert "private-value" not in (tmp_path / "stdout.log").read_text()
    assert "[REDACTED]" in (tmp_path / "stdout.log").read_text()


def test_failed_child_returncode_stderr_and_output_bound(tmp_path):
    result = cal.supervise(
        [sys.executable, "-c", "import sys; print('password=hidden',file=sys.stderr); sys.exit(7)"],
        tmp_path, limits(), {}, (),
    )
    assert result["returncode"] == 7
    assert "hidden" not in (tmp_path / "stderr.log").read_text()


def test_log_flood_has_bounded_capture(tmp_path):
    result = cal.supervise([sys.executable, "-c", "print('x'*100000)"], tmp_path,
                           limits(max_log_bytes=128), {}, ())
    assert result["abort_reason"] == "LOG_BOUND_EXCEEDED"
    assert (tmp_path / "stdout.log").stat().st_size <= 128


def test_missing_success_evidence_does_not_create_positive_budget(monkeypatch, tmp_path, plan):
    monkeypatch.setattr(cal, "supervise", lambda *a: {"returncode": 0, "abort_reason": None})
    _, result = invoke(monkeypatch, tmp_path, plan)
    assert result["status"] == "FAILED"
    assert result["admission_eligible"] is False
    assert result["recommended_growth_budget_bytes_per_sec"] is None


def test_canonical_writers_wal_and_controller_not_synthetic_sql(tmp_path, plan):
    result = cal.replay(plan, tmp_path, source())
    assert result["replay_finished"] is True
    assert result["complete"] is False  # 2-second partial fixture, never six-hour evidence
    assert result["admission_eligible"] is False
    assert result["recommended_growth_budget_bytes_per_sec"] is None
    universe_events = [e for e in plan["workload"]["events"] if e["kind"] == "universe"]
    expected_rows = sum(len(e["data"]["candidates"]) for e in universe_events)
    assert result["canonical_input_budget"]["status"] == "STORAGE_GROWTH_BUDGET_UNMEASURED"
    assert result["growth_diagnostics"]["largest_committed_db_wal_burst_bytes"] > 0
    assert result["counts"]["candidate_rows"] == expected_rows
    assert result["counts"]["universe"] == len(universe_events)
    assert result["sampled_peaks"]["wal_bytes"] > 0
    assert result["controller_reports"]
    assert result["integrity"] == {"integrity": "ok", "foreign_key_violations": 0}
    with sqlite3.connect(tmp_path / "laboratory.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM universe_selection_candidates").fetchone()[0] == expected_rows
        assert conn.execute("SELECT COUNT(*) FROM burnin_universe_selection_links").fetchone()[0] == len(universe_events)
    samples = [json.loads(x) for x in (tmp_path / "samples.jsonl").read_text().splitlines()]
    assert all(x["pragmas"]["journal_mode"] == "wal" for x in samples)


def test_peak_does_not_disappear_after_wal_checkpoint():
    assert cal.peaks([{"wal_bytes": 9000, "main_bytes": 1000},
                      {"wal_bytes": 0, "main_bytes": 5000}])["wal_bytes"] == 9000


def test_archive_shm_and_allocated_space_are_counted_without_following_links(tmp_path):
    (tmp_path / "laboratory.db").write_bytes(b"x" * 16)
    (tmp_path / "laboratory.db-wal").write_bytes(b"x" * 32)
    (tmp_path / "laboratory.db-shm").write_bytes(b"x" * 8)
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "snapshot.db").write_bytes(b"x" * 64)
    result = cal.footprint(tmp_path)
    assert result["physical_file_bytes"] == 120
    assert result["archive_bytes"] == 64 and result["shm_bytes"] == 8
    (tmp_path / "link").symlink_to(tmp_path / "laboratory.db")
    with pytest.raises(ValueError, match="SYMLINK"):
        cal.footprint(tmp_path)


def test_lifecycle_shadow_and_resolvers_use_isolated_canonical_scope(tmp_path, plan):
    from alphaforge.storage_workload import iso
    start = plan["workload"]["start_timestamp"]
    at = iso(start)
    costs = {k: 0.0 for k in ["spread_penalty", "slippage_penalty", "fee_penalty", "funding_penalty",
                             "latency_penalty", "volatility_penalty", "liquidity_penalty"]}
    position = dict(trade_id="lab-trade", signal_id="lab-signal", decision_time=at,
                    source_decision_id="lab-decision", symbol="BTCUSDT", side="LONG",
                    setup_type="PULLBACK", entry_time=at, planned_entry=100.0,
                    simulated_fill=100.0, stop=90.0, target=120.0, quantity=1.0,
                    notional=100.0, entry_spread=0.0, entry_slippage=0.0, entry_fee=0.0,
                    regime="TRENDING", source_provenance={
                        "execution_cost_unit": "USD", "execution_cost_model_unit": "USD",
                        "execution_cost_model": costs, "target_policy": {"trailing_allowed": False},
                    })
    candle = {"timestamp": iso(start + 60), "high": 105.0, "low": 98.0, "close": 104.0,
              "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"}}
    reject = dict(reject_decision_id="lab-reject", signal_id="lab-reject-signal", symbol="BTCUSDT",
                  side="LONG", decision_timestamp=at, entry=100.0, stop=90.0, target=120.0,
                  horizon_bars=1, timeframe="1m", execution_cost_assumptions=costs,
                  regime="TRENDING", reject_reason="FIXTURE_ONLY", source_provenance={"source": "SYNTHETIC"})
    events = plan["workload"]["events"][:2]
    events.extend([
        {"id": "observation", "kind": "observation", "at": 0, "data": {"symbol": "BTCUSDT", "decision": "REJECTED", "metrics": {"reject_decision_id": "lab-reject"}}},
        {"id": "position", "kind": "position", "at": 0, "data": position},
        {"id": "reject", "kind": "reject", "at": 0, "data": reject},
        {"id": "snapshot", "kind": "snapshot", "at": 0, "data": {"fixture": True}},
        {"id": "shadow", "kind": "shadow", "at": 150, "data": {"trade_id": "lab-trade", "candles": [candle]}},
        {"id": "resolve", "kind": "resolve", "at": 210, "data": {"candles_by_symbol": {"BTCUSDT": [
            candle, {**candle, "timestamp": iso(start + 120), "high": 121.0, "low": 103.0, "close": 119.0},
        ]}}},
    ])
    plan["workload"].update(events=events, duration_seconds=210)
    plan["workload_hash"] = canonical_hash(plan["workload"])
    result = cal.replay(plan, tmp_path, source())
    assert result["missing_surfaces"] == []
    assert not result["complete"]  # all adapters, but still not six-hour/market proof
    with sqlite3.connect(tmp_path / "laboratory.db") as conn:
        assert conn.execute("SELECT count(*) FROM burnin_position_management_shadow_proposals").fetchone()[0] == 4
        assert conn.execute("SELECT exit_reason FROM burnin_pending_position_outcomes").fetchone()[0] == "TP_HIT"
        assert conn.execute("SELECT count(*) FROM burnin_position_management_events").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM burnin_pending_reject_labels").fetchone()[0] == 1


def test_canonical_storage_high_watermark_abort_retains_evidence(tmp_path, plan):
    from alphaforge.config import load_config_from_env
    from alphaforge.burnin_campaign import build_phase8_campaign_identity
    from alphaforge.storage_policy import StoragePolicy
    env = {**plan["config_env"], "ALPHAFORGE_STORAGE_HIGH_BYTES": "1024", "ALPHAFORGE_STORAGE_LOW_BYTES": "512"}
    cfg = load_config_from_env(env=env)
    i = build_phase8_campaign_identity(cfg.runtime, [], ["1h", "15m", "1m"], dynamic_universe=True)
    plan.update(config_env=env, config_hashes={k: i[k] for k in plan["config_hashes"]},
                policy_hash=StoragePolicy.from_config(cfg.runtime).identity_hash())
    with pytest.raises(ValueError, match="CANONICAL_STORAGE_BLOCKED"):
        cal.replay(plan, tmp_path, source())
    with sqlite3.connect(tmp_path / "laboratory.db") as conn:
        assert conn.execute("SELECT count(*) FROM universe_selection_candidates").fetchone()[0] == 3


def test_disk_and_host_bound_abort_owned_process(tmp_path):
    result = cal.supervise([sys.executable, "-c", "import time; time.sleep(20)"], tmp_path,
                           limits(host_reserve_bytes=10**30), {}, ())
    assert result["abort_reason"] == "HOST_RESERVE_VIOLATION"
    assert result["returncode"] != 0


def test_real_checkpoint_wal_peak_is_retained(tmp_path, plan):
    from alphaforge.storage_policy import checkpoint
    from alphaforge.storage_workload import Workload
    cfg, ident = cal.validate(plan, source())
    workload = Workload(tmp_path, cfg, ident, source()["sha"], "checkpoint-fixture")
    try:
        workload.apply(plan["workload"]["events"][0], plan["workload"]["start_timestamp"])
        before = cal.footprint(tmp_path)
        assert before["wal_bytes"] > 0
        result = checkpoint(workload.engine, truncate=True)
        after = cal.footprint(tmp_path)
        assert result["mode"] == "TRUNCATE" and not result["busy"]
        assert after["wal_bytes"] == 0
        assert cal.peaks([before, after])["wal_bytes"] == before["wal_bytes"]
    finally:
        workload.close()


def test_interpreter_and_filesystem_mismatch_are_not_relabelled(monkeypatch, tmp_path, plan):
    changed = deepcopy(plan)
    changed["environment_scope"]["sqlite_version"] = "unrelated"
    with pytest.raises(ValueError, match="INTERPRETER_SQLITE_HOST"):
        cal.validate(changed, source())
    plan["environment_scope"]["filesystem"]["device"] = -1
    monkeypatch.setattr(cal, "source_identity", source)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="FILESYSTEM_SCOPE"):
        cal.run(path, tmp_path / "lab", cal.file_hash(path))
    assert not (tmp_path / ".lab.attempt.json").exists()


def test_redaction_covers_known_secret_truncated_at_capture_boundary():
    assert "super-" not in cal.redact("prefix super-", ("super-private-token",))
    assert "[REDACTED_TRUNCATED]" in cal.redact("prefix super-", ("super-private-token",))


@pytest.mark.parametrize("value", ['{"token": "hidden"}', 'signature=hidden&next=ok',
                                   'x-mbx-apikey: hidden', "{'password': 'hidden'}"])
def test_structured_and_signed_diagnostics_do_not_expose_credentials(value):
    assert "hidden" not in cal.redact(value)
    assert "[REDACTED]" in cal.redact(value)


@pytest.mark.parametrize("note_failure", [False, True])
def test_real_worker_stops_after_supervisor_death_without_resume(tmp_path, note_failure):
    # Exercise the actual _worker entrypoint, with a blocked disposable replay.
    # Inherited pipe EOF proves the orphan exited without process-list access.
    worker = '''
import os,sys,time
from pathlib import Path
from alphaforge import storage_calibration as c
root=Path(sys.argv[1])
c.source_identity=lambda: {}
def replay(*args):
    (root/'ready').write_text(str(os.getpid()))
    time.sleep(20)
    (root/'survived').touch()
    return {}
c.replay=replay
if sys.argv[2]=='True':
    def failed_note(*args): raise OSError('injected evidence publication failure')
    c.write_json=failed_note
c.main(['_worker','--plan',str(root/'plan.json'),'--output',str(root),'--claim',str(root/'claim.json')])
'''
    supervisor = '''
import json,os,subprocess,sys,time
from pathlib import Path
from alphaforge.burnin import canonical_hash
root=Path(sys.argv[1]);plan={'limits':{'max_bytes':16777216}}
(root/'plan.json').write_text(json.dumps(plan))
(root/'claim.json').write_text(json.dumps({'parent_pid':os.getpid(),'output':str(root),'content_hash':canonical_hash(plan)}))
subprocess.Popen([sys.executable,'-c',sys.argv[2],str(root),sys.argv[3]])
time.sleep(20)
'''
    env = {**os.environ, "PYTHONPATH": str(Path(cal.__file__).resolve().parents[1]),
           "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.Popen([sys.executable, "-c", supervisor, str(tmp_path), worker, str(note_failure)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (tmp_path / "ready").exists()
        child_pid = int((tmp_path / "ready").read_text())
        claim = (tmp_path / "claim.json").read_bytes()
        proc.kill(); proc.wait(timeout=3)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=3), "worker survived supervisor death"
            assert proc.stdout.read(1) == b""
        child_pid = None
        assert not (tmp_path / "survived").exists()
        assert not (tmp_path / "worker-result.json").exists()
        assert (tmp_path / "claim.json").read_bytes() == claim
        assert (tmp_path / "worker.attempt").exists()
        if not note_failure:
            note = json.loads((tmp_path / "worker-parent-loss.json").read_text())
            assert note["reason"] == "SUPERVISOR_LOST"
            assert note["admission_eligible"] is False
            assert note["recommended_growth_budget_bytes_per_sec"] is None
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=3)
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
        proc.stdout.close(); proc.stderr.close()


def test_generated_workload_exercises_every_adapter_without_future_evidence(tmp_path):
    env = {"ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED": "true",
           "ALPHAFORGE_REJECT_FORWARD_HORIZON_BARS": "1", "ALPHAFORGE_SCAN_INTERVAL_SEC": "60"}
    p = cal.example(env, source()["sha"], duration=210, candidate_count=1)
    p["environment_scope"]["filesystem"] = cal.filesystem_identity(tmp_path)
    shadow = next(e for e in p["workload"]["events"] if e["kind"] == "shadow")
    assert len(shadow["data"]["candles"]) == 1
    result = cal.replay(p, tmp_path, source())
    assert result["missing_surfaces"] == []
    assert not result["complete"] and not result["admission_eligible"]
    assert result["recommended_growth_budget_bytes_per_sec"] is None
