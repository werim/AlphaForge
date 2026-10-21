"""Bounded diagnostic full-replay laboratory harness. No admission authority.

`example` writes an explicitly synthetic workload; `run` is a single-use,
process-isolated replay. Neither command starts PAPER or changes a config.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import threading
from typing import Any

from alphaforge.burnin import canonical_hash
from alphaforge.burnin_campaign import build_phase8_campaign_identity
from alphaforge.config import load_config_from_env
from alphaforge.config_registry import CONFIG_REGISTRY
from alphaforge.storage_policy import StoragePolicy, full_replay_duration_budget, disk_budget
from alphaforge.storage_workload import ACTIONS, Workload, iso

VERSION = "storage-calibration.v1"
MAX_PLAN_BYTES = 256 * 1024**2
SECRET = re.compile(r"(?i)(secret|token|password|api[_-]?key|authorization|webhook)")


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024**2), b""):
            h.update(block)
    return h.hexdigest()


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, sort_keys=True, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def source_identity() -> dict:
    root = Path(__file__).resolve().parents[2]
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True, timeout=10).strip()
    return {"sha": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
            "dirty": bool(git("status", "--porcelain")),
            "modules": {name: file_hash(root / "src/alphaforge" / name) for name in (
                "storage_calibration.py", "storage_workload.py", "storage_policy.py",
                "universe_evidence.py", "burnin_resolver.py", "position_management_shadow.py",
            )}}


def filesystem_identity(path: Path) -> dict:
    st, fs = path.stat(), os.statvfs(path)
    return {"device": st.st_dev, "fsid": fs.f_fsid,
            "fragment_size": fs.f_frsize, "block_size": fs.f_bsize}


def environment_identity(path: Path) -> dict:
    return {"python_binary_sha256": file_hash(Path(sys.executable)), "python_version": sys.version,
            "sqlite_version": sqlite3.sqlite_version, "platform": platform.platform(),
            "host_hash": hashlib.sha256(platform.node().encode()).hexdigest(),
            "filesystem": filesystem_identity(path)}


def configuration(env: dict) -> tuple[Any, dict]:
    # No dotenv, account, inherited process config or credentials in this path.
    accepted = {name: setting for setting in CONFIG_REGISTRY for name in (setting.env_name, *setting.deprecated_aliases)}
    for key in env:
        if key not in accepted or accepted[key].secret or SECRET.search(key):
            raise ValueError("CONFIG_KEY_NOT_CANONICAL_OR_SECRET")
    cfg = load_config_from_env(env=env)
    if cfg.runtime.execution_mode != "PAPER" or cfg.runtime.live_enabled:
        raise ValueError("LAB_PAPER_NONLIVE_REQUIRED")
    if not cfg.runtime.paper_position_management_shadow_enabled:
        raise ValueError("TARGET_SHADOW_CONFIG_REQUIRED")
    if [cfg.runtime.regime_timeframe, cfg.runtime.setup_timeframe, cfg.runtime.execution_timeframe] != ["1h", "15m", "1m"]:
        raise ValueError("TARGET_MTF_SCOPE_REQUIRED")
    if cfg.runtime.storage_stats_only_enabled or not cfg.runtime.storage_enabled:
        raise ValueError("FULL_REPLAY_STORAGE_REQUIRED")
    ident = build_phase8_campaign_identity(cfg.runtime, [], ["1h", "15m", "1m"], dynamic_universe=True)
    return cfg, ident


def validate(plan: dict, source: dict) -> tuple[Any, dict]:
    if plan.get("version") != VERSION or plan.get("code_sha") != source["sha"] or source["dirty"]:
        raise ValueError("SOURCE_SCOPE_MISMATCH_OR_DIRTY")
    expected_env = plan.get("environment_scope", {})
    observed_env = environment_identity(Path(__file__).resolve().parent)
    if {k: v for k, v in expected_env.items() if k != "filesystem"} != {k: v for k, v in observed_env.items() if k != "filesystem"}:
        raise ValueError("INTERPRETER_SQLITE_HOST_SCOPE_MISMATCH")
    cfg, ident = configuration(plan["config_env"])
    policy = StoragePolicy.from_config(cfg.runtime)
    if plan.get("config_hashes") != {k: ident[k] for k in (
        "config_hash", "strategy_config_hash", "universe_hash", "execution_cost_config_hash",
    )} or plan.get("policy_hash") != policy.identity_hash():
        raise ValueError("CONFIG_POLICY_SCOPE_MISMATCH")
    if plan.get("workload_hash") != canonical_hash(plan["workload"]):
        raise ValueError("WORKLOAD_HASH_MISMATCH")
    workload = plan["workload"]
    if workload.get("provenance", {}).get("kind") not in {"SYNTHETIC", "RECORDED_REPLAY"}:
        raise ValueError("WORKLOAD_PROVENANCE_MISSING")
    if not workload["provenance"].get("source") or workload.get("intervals") != ["1h", "15m", "1m"]:
        raise ValueError("WORKLOAD_SCOPE_MISMATCH")
    duration = workload.get("duration_seconds")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("DURATION_INVALID")
    if type(workload.get("start_timestamp")) not in (int, float) or not math.isfinite(workload["start_timestamp"]):
        raise ValueError("START_TIMESTAMP_INVALID")
    events = workload.get("events")
    if not isinstance(events, list) or not events or len(events) > 100000:
        raise ValueError("WORKLOAD_EVIDENCE_MISSING")
    ids, previous = set(), -1.0
    for event in events:
        at = event.get("at")
        if (type(at) not in (int, float) or not math.isfinite(at) or
                at < previous or at > duration or at < 0 or event.get("kind") not in ACTIONS or
                not event.get("id") or event["id"] in ids or not isinstance(event.get("data", {}), dict)):
            raise ValueError("WORKLOAD_EVENT_SCOPE_INVALID")
        ids.add(event["id"])
        previous = at
    if previous != duration or events[0]["at"] != 0:
        raise ValueError("WORKLOAD_WINDOW_INCOMPLETE")
    for key in ("timeout_seconds", "max_bytes", "host_reserve_bytes", "max_log_bytes"):
        v = plan["limits"].get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ValueError("RESOURCE_BOUND_INVALID")
    return cfg, ident


def footprint(directory: Path) -> dict:
    totals = {"main_bytes": 0, "wal_bytes": 0, "shm_bytes": 0, "archive_bytes": 0,
              "artifact_bytes": 0, "allocated_bytes": 0}
    if directory.exists():
        for p in directory.rglob("*"):
            if p.is_symlink():
                raise ValueError("OUTPUT_SYMLINK_FORBIDDEN")
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except FileNotFoundError:
                continue  # an atomic diagnostic publication completed
            if st.st_nlink != 1:
                raise ValueError("OUTPUT_HARDLINK_FORBIDDEN")
            if "archive" in p.relative_to(directory).parts:
                key = "archive_bytes"
            elif p.name == "laboratory.db":
                key = "main_bytes"
            elif p.name.endswith("-wal"):
                key = "wal_bytes"
            elif p.name.endswith("-shm"):
                key = "shm_bytes"
            else:
                key = "artifact_bytes"
            totals[key] += st.st_size
            totals["allocated_bytes"] += st.st_blocks * 512
    totals["db_wal_bytes"] = totals["main_bytes"] + totals["wal_bytes"]
    totals["physical_file_bytes"] = sum(totals[k] for k in (
        "main_bytes", "wal_bytes", "shm_bytes", "archive_bytes", "artifact_bytes",
    ))
    totals["host_free_bytes"] = shutil.disk_usage(directory if directory.exists() else directory.parent).free
    totals["filesystem"] = filesystem_identity(directory if directory.exists() else directory.parent)
    return totals


def peaks(samples: list[dict]) -> dict:
    # Keep actual sampled peaks; final WAL shrinkage never subtracts a peak.
    names = ("db_wal_bytes", "main_bytes", "wal_bytes", "shm_bytes", "archive_bytes", "physical_file_bytes", "allocated_bytes")
    return {name: max((s.get(name, 0) for s in samples), default=0) for name in names}


def redact(value: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[REDACTED]")
            # A bounded raw capture may end halfway through a bare secret.
            for length in range(min(len(secret) - 1, len(value)), 3, -1):
                if value.endswith(secret[:length]):
                    value = value[:-length] + "[REDACTED_TRUNCATED]"
                    break
    return re.sub(r"(?i)((?:secret|token|password|api[_-]?key|authorization|signature|x-mbx-apikey)[\"']?\s*[:=]\s*[\"']?)[^\s,;}&\"']+",
                  r"\1[REDACTED]", value)


def reservation(output: Path, plan_hash: str, content_hash: str = "") -> Path:
    # Reservation lives in the existing control parent, BEFORE output/DB/archive
    # preparation. Never delete this file, even if preparation fails.
    if not output.is_absolute() or not output.parent.is_dir() or output.parent.resolve() != output.parent:
        raise ValueError("EXPLICIT_EXISTING_REAL_CONTROL_PARENT_REQUIRED")
    claim = output.with_name("." + output.name + ".attempt.json")
    fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"plan_hash": plan_hash, "parent_pid": os.getpid(), "output": str(output), "content_hash": content_hash}, f)
        f.flush()
        os.fsync(f.fileno())
    fsync_directory(claim.parent)
    return claim


def supervise(command: list[str], output: Path, limits: dict, env: dict, secrets: tuple[str, ...]) -> dict:
    """Bounded supervision of the owned child, including pre-replay failures."""
    logs = {name: bytearray() for name in ("stdout", "stderr")}
    observed_peaks, minimum_free, reason = {}, None, None
    started = time.monotonic()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, env=env, cwd=Path(__file__).resolve().parents[2])
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stderr is not None
    for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        selector.register(stream, selectors.EVENT_READ, name)
    try:
        while selector.get_map() or proc.poll() is None:
            if time.monotonic() - started >= limits["timeout_seconds"]:
                reason = reason or "TIMEOUT"
            try:
                sample = footprint(output)
                observed_peaks = peaks([observed_peaks, sample])
                minimum_free = min(minimum_free, sample["host_free_bytes"]) if minimum_free is not None else sample["host_free_bytes"]
                if max(sample["physical_file_bytes"], sample["allocated_bytes"]) > limits["max_bytes"]:
                    reason = reason or "DISK_BOUND_EXCEEDED"
                if sample["host_free_bytes"] < limits["host_reserve_bytes"]:
                    reason = reason or "HOST_RESERVE_VIOLATION"
            except Exception:
                reason = reason or "RESOURCE_EVIDENCE_UNAVAILABLE"
            if reason and proc.poll() is None:
                proc.kill()
            for key, _ in selector.select(timeout=0.05):
                block = os.read(key.fileobj.fileno(), 4096)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                log = logs[key.data]
                room = limits["max_log_bytes"] - len(log)
                log.extend(block[:max(0, room)])
                if len(block) > room:
                    reason = reason or "LOG_BOUND_EXCEEDED"
        returncode = proc.wait(timeout=1)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        selector.close()
        proc.stdout.close()
        proc.stderr.close()
        for name, data in logs.items():
            text = redact(data.decode("utf-8", errors="replace"), secrets)
            fd = os.open(output / (name + ".log"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text.encode("utf-8")[:limits["max_log_bytes"]].decode("utf-8", errors="ignore"))
    return {"returncode": returncode, "abort_reason": reason,
            "wall_seconds": time.monotonic() - started, "sampled_peaks": observed_peaks,
            "minimum_observed_host_free_bytes": minimum_free,
            "resource_observation_interval_seconds": 0.05}


def parent_watchdog(parent_pid: int, output: Path, *, interval_seconds: float = 0.1) -> threading.Event:
    """Stop this disposable worker after supervisor loss; never continue a claim.

    This child-side liveness guard is not an OS quota or a hard real-time lease.
    The durable parent claim remains authoritative when final evidence is absent.
    """
    stopped = threading.Event()

    def watch():
        while not stopped.is_set():
            if os.getppid() != parent_pid:
                try:
                    write_json(output / "worker-parent-loss.json", {
                        "status": "FAILED", "reason": "SUPERVISOR_LOST",
                        "admission_eligible": False,
                        "recommended_growth_budget_bytes_per_sec": None,
                        "expected_parent_pid": parent_pid,
                    })
                finally:
                    # No application exception handler can resume an orphaned
                    # workload. If disk-full prevents the note, the persistent
                    # claim with no parent result still means INCOMPLETE.
                    os._exit(74)
            stopped.wait(interval_seconds)

    stopped.worker_thread = threading.Thread(target=watch, name="storage-calibration-parent-watch", daemon=True)
    stopped.worker_thread.start()
    return stopped


def run(plan_path: Path, output: Path, expected_digest: str) -> dict:
    result_path = output.with_name("." + output.name + ".result.json")
    with plan_path.open("rb") as stream:
        raw = stream.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise ValueError("PLAN_TOO_LARGE")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_digest:
        raise ValueError("PLAN_DIGEST_MISMATCH")
    plan = json.loads(raw)
    source = source_identity()
    validate(plan, source)
    if result_path.exists():
        raise ValueError("RESULT_ALREADY_EXISTS")
    if plan["environment_scope"]["filesystem"] != filesystem_identity(output.parent):
        raise ValueError("FILESYSTEM_SCOPE_MISMATCH")
    claim = reservation(output, digest, canonical_hash(plan))
    result = {"status": "FAILED", "admission_eligible": False,
              "recommended_growth_budget_bytes_per_sec": None,
              "plan_hash": digest, "code_identity": source, "claim": str(claim),
              "config_hashes": plan["config_hashes"], "policy_hash": plan["policy_hash"],
              "workload_hash": plan["workload_hash"], "environment_scope": plan["environment_scope"],
              "reason": "PREPARATION_INCOMPLETE", "returncode": None}
    try:
        if output.exists():
            raise ValueError("FRESH_OUTPUT_COLLISION")
        if shutil.disk_usage(output.parent).free < plan["limits"]["max_bytes"] + plan["limits"]["host_reserve_bytes"]:
            raise ValueError("PREPARATION_HOST_RESERVE_INSUFFICIENT")
        output.mkdir(mode=0o700)
        # Freeze the exact already-validated bytes for the child; input path
        # edits after admission cannot replace the selected workload.
        write_json(output / "input.json", plan)
        clean_env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")}
        clean_env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        secrets = tuple(v for k, v in os.environ.items() if SECRET.search(k) and len(v) >= 4)
        outcome = supervise(
            [sys.executable, "-m", "alphaforge.storage_calibration", "_worker",
             "--plan", str(output / "input.json"), "--output", str(output), "--claim", str(claim)],
            output, plan["limits"], clean_env, secrets,
        )
        result.update(outcome)
        worker_path = output / "worker-result.json"
        if outcome["returncode"] == 0 and not outcome["abort_reason"] and worker_path.is_file():
            worker = json.loads(worker_path.read_text())
            if worker.get("complete") is True and worker.get("plan_hash") == canonical_hash(plan):
                result.update(status="DIAGNOSTIC_COMPLETE", reason="OFFLINE_REPLAY_NOT_ADMISSION_AUTHORITY", worker=worker)
            else:
                result.update(reason="WORKLOAD_COVERAGE_INCOMPLETE" if worker.get("plan_hash") == canonical_hash(plan) else "WORKER_EVIDENCE_CROSS_SCOPED", worker=worker)
        else:
            result["reason"] = outcome["abort_reason"] or "WORKER_FAILED_OR_EVIDENCE_MISSING"
    except Exception as exc:
        result["reason"] = type(exc).__name__ + ":" + redact(str(exc))[:1024]
    finally:
        # Persistent even when no output directory could be prepared. An
        # unwriteable control filesystem is an explicit failure, never PASS.
        write_json(result_path, result)
    return result


def replay(plan: dict, output: Path, source: dict) -> dict:
    cfg, ident = validate(plan, source)
    policy = StoragePolicy.from_config(cfg.runtime)
    if policy.archive_dir and Path(policy.archive_dir).resolve() != output / "archive":
        raise ValueError("ARCHIVE_DESTINATION_MUST_BE_LAB_LOCAL")
    # These checks are measurement isolation, not exchange security changes.
    def no_network(*args, **kwargs):
        raise RuntimeError("OFFLINE_WORKLOAD_NETWORK_FORBIDDEN")
    original_connect = socket.socket.connect
    socket.socket.connect = no_network
    workload = None
    observed_peaks, reports, report_count = {}, [], 0
    first_filesystem = filesystem_identity(output)
    previous_sample = None
    growth = {"positive_db_wal_delta_bytes": 0, "largest_committed_db_wal_burst_bytes": 0,
              "largest_observed_interval_bytes_per_operation_second": 0.0,
              "interpretation": "EMPIRICAL_SAMPLED_DIAGNOSTICS_NOT_AN_UPPER_BOUND_OR_OPERATOR_BUDGET"}
    try:
        if policy.archive_dir:
            (output / "archive").mkdir(mode=0o700)
        workload = Workload(output, cfg, ident, source["sha"], canonical_hash(plan)[:16])
        w = plan["workload"]
        def sample(at):
            nonlocal observed_peaks, previous_sample
            value = {**footprint(output), **workload.sample(), "operation_seconds": at}
            observed_peaks = peaks([observed_peaks, value])
            if previous_sample is not None:
                delta = max(0, value["db_wal_bytes"] - previous_sample["db_wal_bytes"])
                growth["positive_db_wal_delta_bytes"] += delta
                growth["largest_committed_db_wal_burst_bytes"] = max(growth["largest_committed_db_wal_burst_bytes"], delta)
                elapsed = at - previous_sample["operation_seconds"]
                if elapsed > 0:
                    growth["largest_observed_interval_bytes_per_operation_second"] = max(growth["largest_observed_interval_bytes_per_operation_second"], delta / elapsed)
            previous_sample = value
            with (output / "samples.jsonl").open("a") as f:
                f.write(json.dumps(value, sort_keys=True) + "\n")
            if max(value["physical_file_bytes"], value["allocated_bytes"]) > plan["limits"]["max_bytes"]:
                raise ValueError("DISK_BOUND_EXCEEDED")
            if value["host_free_bytes"] < max(policy.reserve(value["wal_bytes"], w["duration_seconds"]), plan["limits"]["host_reserve_bytes"]):
                raise ValueError("HOST_RESERVE_VIOLATION")
        sample(0)
        for event in w["events"]:
            workload.apply(event, w["start_timestamp"] + event["at"])
            sample(event["at"])  # before canonical maintenance can shrink WAL
            report = workload.maintain(event["at"])
            if report is not None:
                report_count += 1
                reports.append(report)
                reports = reports[-100:]
                with (output / "maintenance.jsonl").open("a") as log:
                    log.write(json.dumps({"operation_seconds": event["at"], **report}) + "\n")
            sample(event["at"])
            if report is not None and report["blocked"]:
                raise ValueError("CANONICAL_STORAGE_BLOCKED")
        integrity = workload.integrity()
        if integrity != {"integrity": "ok", "foreign_key_violations": 0}:
            raise ValueError("LAB_INTEGRITY_FAILED")
        missing = sorted(ACTIONS - workload.counts.keys())
        return {"complete": not missing and w["duration_seconds"] >= 21600, "replay_finished": True, "plan_hash": canonical_hash(plan), "source": source,
                "config_hashes": plan["config_hashes"], "policy_hash": plan["policy_hash"],
                "workload_hash": plan["workload_hash"], "provenance": w["provenance"],
                "duration_seconds": w["duration_seconds"], "counts": dict(workload.counts),
                "missing_surfaces": missing, "sampled_peaks": observed_peaks,
                "growth_diagnostics": growth,
                "canonical_input_budget": full_replay_duration_budget(workload.path, policy, duration_seconds=w["duration_seconds"]),
                "canonical_disk_reserve": disk_budget(workload.path, policy, duration_seconds=w["duration_seconds"]),
                "controller_report_count": report_count, "controller_reports": reports, "integrity": integrity,
                "interpreter": {"version": sys.version, "binary_sha256": file_hash(Path(sys.executable)),
                                "sqlalchemy": importlib.metadata.version("sqlalchemy")},
                "sqlite_version": sqlite3.sqlite_version, "platform": platform.platform(),
                "filesystem": first_filesystem,
                "admission_eligible": False, "recommended_growth_budget_bytes_per_sec": None,
                "limits": ["OFFLINE_NOT_FUTURE_WORKLOAD_UPPER_BOUND", "SAMPLED_PEAK_NOT_KERNEL_QUOTA",
                           "NO_AUTHENTICATED_RUNTIME_RECONCILIATION", "NO_AUTOMATIC_OPERATOR_BUDGET"]}
    finally:
        if workload is not None:
            workload.close()
        socket.socket.connect = original_connect


def example(env: dict, sha: str, duration: int = 21600, candidate_count: int = 32) -> dict:
    cfg, ident = configuration(env)
    start = 1791288000.0
    cadence = float(cfg.runtime.scan_interval_sec)
    cycles = math.ceil(duration / cadence)
    if cycles * 2 + 2 > 100000:
        raise ValueError("EXAMPLE_EXCEEDS_EVENT_RESOURCE_BOUND")
    events = []
    for at in sorted(set([*(min(i * cadence, duration) for i in range(cycles)), duration])):
        candidates = [{"symbol": f"S{i:04d}USDT", "volume_24h_usd": 10000000,
                       "source_exchange": "binance", "observed_at": start + at,
                       "market_data_source": "SYNTHETIC_OFFLINE_FIXTURE"} for i in range(candidate_count)]
        events.extend([{"id": f"universe-{at}", "kind": "universe", "at": at, "data": {"candidates": candidates}},
                       {"id": f"heartbeat-{at}", "kind": "heartbeat", "at": at, "data": {"calibration": True}}])
    # A declared synthetic lifecycle, not invented historical market evidence.
    # Small windows remain intentionally partial instead of leaking future bars.
    resolve_at = max(180, cfg.runtime.reject_forward_horizon_bars * 60 + 60)
    if duration >= resolve_at:
        costs = {name: 0.0 for name in ("spread_penalty", "slippage_penalty", "fee_penalty",
                 "funding_penalty", "latency_penalty", "volatility_penalty", "liquidity_penalty")}
        candles = [{"timestamp": iso(start + minute), "high": 105.0, "low": 98.0, "close": 104.0,
                    "source_provenance": {"provider": "SYNTHETIC_OFFLINE_FIXTURE", "interval": "1m"}}
                   for minute in range(60, resolve_at + 1, 60)]
        candles[1].update(high=121.0, low=103.0, close=119.0)
        events.extend([
            {"id": "lab-observation", "kind": "observation", "at": 0,
             "data": {"symbol": "BTCUSDT", "decision": "REJECTED", "metrics": {"reject_decision_id": "example-reject"}}},
            {"id": "lab-position", "kind": "position", "at": 0, "data": {
                "trade_id": "example-position", "signal_id": "example-signal", "source_decision_id": "example-decision",
                "decision_time": iso(start), "entry_time": iso(start), "symbol": "BTCUSDT", "side": "LONG",
                "setup_type": "PULLBACK", "regime": "TRENDING", "planned_entry": 100.0, "simulated_fill": 100.0,
                "stop": 90.0, "target": 120.0, "quantity": 1.0, "notional": 100.0,
                "entry_spread": 0.0, "entry_slippage": 0.0, "entry_fee": 0.0,
                "source_provenance": {"source": "SYNTHETIC_OFFLINE_FIXTURE", "execution_cost_unit": "USD",
                    "execution_cost_model_unit": "USD", "execution_cost_model": costs,
                    "target_policy": {"trailing_allowed": False}},
             }},
            {"id": "lab-reject", "kind": "reject", "at": 0, "data": {
                "reject_decision_id": "example-reject", "signal_id": "example-reject-signal", "symbol": "BTCUSDT",
                "side": "LONG", "decision_timestamp": iso(start), "entry": 100.0, "stop": 90.0, "target": 120.0,
                "horizon_bars": cfg.runtime.reject_forward_horizon_bars, "timeframe": "1m",
                "execution_cost_assumptions": {name: 0.0 for name in ("spread_cost", "entry_slippage_cost",
                    "exit_slippage_cost", "fee_cost", "funding_cost", "latency_cost")},
                "regime": "TRENDING", "reject_reason": "SYNTHETIC_ONLY",
                "source_provenance": {"source": "SYNTHETIC_OFFLINE_FIXTURE"},
            }},
            {"id": "lab-snapshot", "kind": "snapshot", "at": 0, "data": {"synthetic": True}},
            {"id": "lab-shadow", "kind": "shadow", "at": 150,
             "data": {"trade_id": "example-position", "candles": candles[:1]}},
            {"id": "lab-resolve", "kind": "resolve", "at": resolve_at,
             "data": {"candles_by_symbol": {"BTCUSDT": candles}}},
        ])
        events.sort(key=lambda e: e["at"])
    workload = {"provenance": {"kind": "SYNTHETIC", "source": "explicit illustrative fixture; not captured market evidence"},
                "intervals": ["1h", "15m", "1m"], "start_timestamp": start,
                "duration_seconds": duration, "events": events,
                "representativeness": "Synthetic cadence/candidate load plus one explicit lifecycle. No market, concurrency or workload upper-bound authority."}
    return {"version": VERSION, "code_sha": sha, "config_env": env,
            "environment_scope": environment_identity(Path(__file__).resolve().parent),
            "config_hashes": {k: ident[k] for k in ("config_hash", "strategy_config_hash", "universe_hash", "execution_cost_config_hash")},
            "policy_hash": StoragePolicy.from_config(cfg.runtime).identity_hash(), "workload": workload,
            "workload_hash": canonical_hash(workload),
            "limits": {"timeout_seconds": 600, "max_bytes": 1024**3, "host_reserve_bytes": 608174080, "max_log_bytes": 65536}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("example")
    ex.add_argument("--config-input", type=Path, required=True, help="Nonsecret canonical env mapping JSON (or {env: mapping})")
    ex.add_argument("--output", type=Path, required=True)
    ex.add_argument("--candidate-count", type=int, default=32)
    r = sub.add_parser("run")
    r.add_argument("--plan", type=Path, required=True)
    r.add_argument("--plan-sha256", required=True)
    r.add_argument("--output", type=Path, required=True)
    w = sub.add_parser("_worker", help=argparse.SUPPRESS)
    w.add_argument("--plan", type=Path, required=True)
    w.add_argument("--output", type=Path, required=True)
    w.add_argument("--claim", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "example":
        if args.output.exists() or args.candidate_count <= 0:
            raise ValueError("FRESH_EXAMPLE_PATH_AND_POSITIVE_CANDIDATE_COUNT_REQUIRED")
        raw = json.loads(args.config_input.read_text())
        env = raw.get("env", raw)
        plan = example(env, source_identity()["sha"], candidate_count=args.candidate_count)
        plan["environment_scope"] = environment_identity(args.output.parent)
        write_json(args.output, plan)
        print(file_hash(args.output))
        return 0
    if args.command == "_worker":
        claim = json.loads(args.claim.read_text())
        plan = json.loads(args.plan.read_text())
        if claim["parent_pid"] != os.getppid() or claim["output"] != str(args.output) or claim["content_hash"] != canonical_hash(plan):
            raise ValueError("WORKER_RESERVATION_SCOPE_MISMATCH")
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE, (plan["limits"]["max_bytes"], plan["limits"]["max_bytes"]))
        fd = os.open(args.output / "worker.attempt", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        watchdog = parent_watchdog(claim["parent_pid"], args.output)
        try:
            before = source_identity()
            result = replay(plan, args.output, before)
            if source_identity() != before:
                raise ValueError("SOURCE_CHANGED_DURING_REPLAY")
            write_json(args.output / "worker-result.json", result)
            return 0
        finally:
            watchdog.set()
    result = run(args.plan.resolve(), args.output, args.plan_sha256)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "DIAGNOSTIC_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
