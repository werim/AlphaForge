"""Evidence boundaries: complete replay is not real-runtime capacity proof."""
from copy import deepcopy
from dataclasses import replace

import pytest

from alphaforge import storage_calibration as cal
from alphaforge.burnin import canonical_hash
from alphaforge.storage_policy import StoragePolicy, full_replay_duration_budget


@pytest.mark.parametrize("kind", ["SYNTHETIC", "RECORDED_REPLAY"])
def test_complete_replay_cannot_bootstrap_normal_admission(tmp_path, kind):
    # Tiny fixture with a six-hour *logical* span, not a calibration. A caller's
    # recorded label cannot confer authority, even when all adapters complete.
    env = {"ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED": "true",
           "ALPHAFORGE_REJECT_FORWARD_HORIZON_BARS": "1",
           "ALPHAFORGE_SCAN_INTERVAL_SEC": "21600"}
    source = {"sha": "a" * 40, "dirty": False}
    plan = cal.example(env, source["sha"], duration=21600, candidate_count=1)
    plan["workload"]["provenance"] = {"kind": kind, "source": "test declaration only"}
    plan["workload_hash"] = canonical_hash(plan["workload"])
    result = cal.replay(plan, tmp_path, source)
    assert result["complete"] is True
    assert result["missing_surfaces"] == []
    assert result["sampled_peaks"]["wal_bytes"] > 0
    assert result["integrity"] == {"integrity": "ok", "foreign_key_violations": 0}
    assert result["admission_eligible"] is False
    assert result["recommended_growth_budget_bytes_per_sec"] is None
    cfg, _ = cal.configuration(env)
    budget = full_replay_duration_budget(
        tmp_path / "laboratory.db", StoragePolicy.from_config(cfg.runtime), duration_seconds=21600)
    assert budget["current_physical_bytes"] > 0
    assert budget["status"] == "STORAGE_GROWTH_BUDGET_UNMEASURED"


def test_prospective_operator_rate_has_new_identity_and_preserves_economics():
    # 123 is an arbitrary test value, never a proposed production budget.
    env = {"ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED": "true"}
    cfg, before = cal.configuration(env)
    prospective = deepcopy(env)
    prospective["ALPHAFORGE_STORAGE_GROWTH_BUDGET_BYTES_PER_SEC"] = "123"
    after_cfg, after = cal.configuration(prospective)
    assert before["config_hash"] != after["config_hash"]
    for key in ("strategy_config_hash", "universe_hash", "execution_cost_config_hash"):
        assert before[key] == after[key]
    left = deepcopy(before["config_payload"])
    right = deepcopy(after["config_payload"])
    assert left["storage_policy"]["growth_budget_bytes_per_sec"] == 0
    assert right["storage_policy"]["growth_budget_bytes_per_sec"] == 123
    left["storage_policy"].pop("growth_budget_bytes_per_sec")
    right["storage_policy"].pop("growth_budget_bytes_per_sec")
    assert left == right
    policy = StoragePolicy.from_config(cfg.runtime)
    measured = StoragePolicy.from_config(after_cfg.runtime)
    assert measured == replace(policy, growth_budget_bytes_per_sec=123)
    assert policy.identity_hash() != measured.identity_hash()
    assert measured.reserve(456, 21600) - policy.reserve(456, 21600) == 123 * 21600


def test_worker_exits_after_supervisor_death_and_keeps_consumed_attempt(tmp_path):
    import json
    import os
    import signal
    import selectors
    import subprocess
    import sys
    import time

    # A tiny process fixture, no DB or market workload. The parent is the only
    # process killed by this test; the watched worker must stop itself.
    worker = '''
import os, sys, time
from pathlib import Path
from alphaforge.storage_calibration import parent_watchdog
out = Path(sys.argv[1])
parent_watchdog(os.getppid(), out)
(out / "ready").write_text(str(os.getpid()))
time.sleep(20)
(out / "survived").write_text("incorrect")
'''
    parent = '''
import subprocess, sys, time
from pathlib import Path
from alphaforge.storage_calibration import reservation
out = Path(sys.argv[1])
reservation(out / "lab", "fixture-only")
p = subprocess.Popen([sys.executable, "-c", sys.argv[2], str(out)])
(out / "child.pid").write_text(str(p.pid))
try:
    p.wait(timeout=25)
finally:
    if p.poll() is None:
        p.kill()
'''
    proc = subprocess.Popen([sys.executable, "-c", parent, str(tmp_path), worker],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / "ready").exists()
        child_pid = int((tmp_path / "ready").read_text())
        claim = (tmp_path / ".lab.attempt.json").read_bytes()
        proc.kill()
        proc.wait(timeout=3)
        # The child inherited this pipe. EOF after parent death proves that
        # it exited rather than merely writing a failure note and continuing.
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), "orphaned worker kept its output pipe open"
            assert proc.stdout.read(1) == b""
        child_pid = None  # no PID signal after observing worker exit
        note = tmp_path / "worker-parent-loss.json"
        deadline = time.monotonic() + 5
        while not note.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert json.loads(note.read_text())["reason"] == "SUPERVISOR_LOST"
        assert json.loads(note.read_text())["admission_eligible"] is False
        assert (tmp_path / ".lab.attempt.json").read_bytes() == claim
        assert not (tmp_path / "survived").exists()
        assert not (tmp_path / ".lab.result.json").exists()
        assert not (tmp_path / "worker-result.json").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.stdout.close()
        proc.stderr.close()


def test_parent_loss_still_exits_if_failure_note_cannot_be_written(tmp_path):
    import subprocess
    import sys

    # ENOTDIR is deterministic even for privileged test runners. Loss of the
    # diagnostic output path must not leave the workload running without a note.
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("fixture")
    script = '''
import os, sys, time
from pathlib import Path
from alphaforge.storage_calibration import parent_watchdog
parent_watchdog(os.getppid() + 1000000, Path(sys.argv[1]))
time.sleep(10)
raise SystemExit(99)
'''
    result = subprocess.run([sys.executable, "-c", script, str(blocked)],
                            capture_output=True, timeout=5)
    assert result.returncode == 74
    assert blocked.read_text() == "fixture"
