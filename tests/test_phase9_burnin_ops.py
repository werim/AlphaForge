from __future__ import annotations

import json, sqlite3, subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import hashlib

from alphaforge.burnin import config_hash, persist_burnin_observation, persist_burnin_reject_outcome, persist_burnin_trade_outcome, utc_now
from alphaforge.burnin_campaign import build_phase8_campaign_identity, create_campaign, event, start_or_resume_campaign, update_campaign_heartbeat, aggregate_campaign, get_campaign, fail_active_campaign_run, mark_attached_campaign_operational
from alphaforge.burnin_ops import (
    audit_payload,
    bootstrap_ops_schema,
    finalize,
    health_payload,
    launch_campaign,
    preflight,
    recovery_drill,
    verify_worker_attachment,
    watch_once,
    clock_skew_check,
    database_diagnosis,
    _readonly_sqlite_uri,
    main as burnin_ops_main,
)


def _conn(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "ops.db"
    from alphaforge.persistence import init_db
    init_db(f"sqlite+pysqlite:///{db}").dispose()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    bootstrap_ops_schema(conn)
    return db, conn


def _campaign(conn, *, release="rel", targets_zero=True):
    camp = create_campaign(
        conn,
        release_id=release,
        duration_days=0 if targets_zero else 1,
        symbols=["BTCUSDT"],
        intervals=["1h"],
        target_decisions=0 if targets_zero else 500,
        target_closed_trades=0 if targets_zero else 30,
        target_reject_forward_outcomes=0 if targets_zero else 50,
    )
    run = start_or_resume_campaign(conn, camp.campaign_id)["burnin_run_id"]
    conn.commit()
    return camp, run


def test_database_diagnosis_is_read_only_and_fails_closed_on_unknown_exposure(tmp_path):
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_campaigns SET worker_pid=NULL,last_heartbeat_at='2020-01-01T00:00:00Z' WHERE campaign_id=?", (camp.campaign_id,))
    conn.commit()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    sidecars_before = {suffix: db.with_name(db.name + suffix).exists() for suffix in ("-wal", "-shm")}
    schema_before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    rows_before = conn.execute("SELECT COUNT(*) FROM burnin_campaigns").fetchone()[0]
    payload = database_diagnosis(str(db), max_heartbeat_age=1)
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert before == after
    assert payload["read_only"] is True
    state = payload["campaigns"][0]
    assert state["active_continuation"]["burnin_run_id"] == run
    assert state["stale_continuation"] is True
    assert state["runtime_pending_orders"] is None
    assert state["runtime_pending_orders_available"] is False
    assert payload["cleanup_plan"][0]["classification"] == "MANUAL_REVIEW"
    assert payload["cleanup_plan"][0]["convert_unknown_exposure_to_zero"] is False
    # Diagnosis must not change WAL sidecar presence; canonical runtime setup
    # may already have created them while another connection remains open.
    assert {suffix: db.with_name(db.name + suffix).exists() for suffix in ("-wal", "-shm")} == sidecars_before
    assert conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == schema_before
    assert conn.execute("SELECT COUNT(*) FROM burnin_campaigns").fetchone()[0] == rows_before
    conn.close()


def _runtime_table(conn, camp, run, *, omit=(), pending_orders="[]", campaign_id=None, timestamp=None,
                   reconciliation_status="CLEAN", unknown_exchange_state=0):
    definitions = {
        "id": "INTEGER PRIMARY KEY", "campaign_id": "TEXT", "burnin_run_id": "TEXT", "release_id": "TEXT",
        "active_position_count": "INTEGER", "active_positions": "TEXT", "pending_order_count": "INTEGER", "pending_orders": "TEXT",
        "orphan_position_count": "INTEGER", "orphan_order_count": "INTEGER", "unknown_exchange_state": "INTEGER",
        "recovery_action_required": "INTEGER", "reconciliation_status": "TEXT", "exchange_read_only_status": "TEXT",
        "diagnostics_json": "TEXT", "timestamp": "TEXT",
    }
    columns = [name for name in definitions if name not in omit]
    conn.execute("DROP TABLE IF EXISTS runtime_state_snapshots")
    conn.execute("CREATE TABLE runtime_state_snapshots(" + ",".join(f"{name} {definitions[name]}" for name in columns) + ")")
    values = {"id": 1, "campaign_id": campaign_id or camp.campaign_id, "burnin_run_id": run, "release_id": camp.release_id,
              "active_position_count": 0, "active_positions": "[]", "pending_order_count": 0, "pending_orders": pending_orders,
              "orphan_position_count": 0, "orphan_order_count": 0, "unknown_exchange_state": unknown_exchange_state,
              "recovery_action_required": 0, "reconciliation_status": reconciliation_status, "exchange_read_only_status": "AVAILABLE",
              "diagnostics_json": json.dumps({"evidence_status": "COMPLETE", "input_source": "AUTHENTICATED_EXCHANGE_SNAPSHOT"}),
              "timestamp": timestamp or utc_now()}
    conn.execute(f"INSERT INTO runtime_state_snapshots({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [values[c] for c in columns])
    conn.commit()


@pytest.mark.parametrize("case", ["absent", "historical", "missing_counter", "malformed_json", "other_campaign", "stale", "local_only", "unknown_exchange"])
def test_database_diagnosis_historical_runtime_compatibility(tmp_path, case):
    db, conn = _conn(tmp_path / case)
    camp, run = _campaign(conn)
    if case == "absent":
        conn.execute("DROP TABLE IF EXISTS runtime_state_snapshots"); conn.commit()
    elif case == "historical":
        _runtime_table(conn, camp, run, omit={"campaign_id", "burnin_run_id", "release_id", "diagnostics_json"})
    elif case == "missing_counter":
        _runtime_table(conn, camp, run, omit={"orphan_order_count"})
    elif case == "malformed_json":
        _runtime_table(conn, camp, run, pending_orders="not-json")
    elif case == "other_campaign":
        _runtime_table(conn, camp, run, campaign_id="another-campaign")
    elif case == "stale":
        _runtime_table(conn, camp, run, timestamp="2020-01-01T00:00:00Z")
    elif case == "local_only":
        _runtime_table(conn, camp, run, reconciliation_status="LOCAL_ONLY_DIAGNOSTIC")
    else:
        _runtime_table(conn, camp, run, unknown_exchange_state=1)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    out = database_diagnosis(str(db), max_heartbeat_age=120)
    assert out["status"] == "COMPLETE"
    assert out["cleanup_plan"][0]["classification"] == "MANUAL_REVIEW"
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    conn.close()


@pytest.mark.parametrize("table,availability,value", [
    ("burnin_pending_position_outcomes", "campaign_open_positions_available", "campaign_open_positions"),
    ("burnin_pending_reject_labels", "pending_reject_labels_available", "pending_reject_labels"),
])
def test_database_diagnosis_missing_local_evidence_table_is_unavailable(tmp_path, table, availability, value):
    db, conn = _conn(tmp_path / table)
    _campaign(conn)
    conn.execute(f"DROP TABLE {table}"); conn.commit()
    out = database_diagnosis(str(db))
    assert out["campaigns"][0][availability] is False
    assert out["campaigns"][0][value] is None
    assert out["cleanup_plan"][0]["classification"] == "MANUAL_REVIEW"
    conn.close()


def test_database_diagnosis_query_failure_is_structured(monkeypatch, tmp_path):
    db, conn = _conn(tmp_path)
    _campaign(conn); conn.close()
    real = __import__("alphaforge.burnin_ops", fromlist=["_connect_readonly"])._connect_readonly

    class Proxy:
        def __init__(self, wrapped): self.wrapped = wrapped
        def execute(self, sql, params=()):
            if sql.startswith("SELECT pending_position_id"):
                raise sqlite3.OperationalError("injected read failure")
            return self.wrapped.execute(sql, params)
        def close(self): self.wrapped.close()

    monkeypatch.setattr("alphaforge.burnin_ops._connect_readonly", lambda path: Proxy(real(path)))
    out = database_diagnosis(str(db))
    state = out["campaigns"][0]
    assert state["campaign_open_positions_available"] is False
    assert state["campaign_open_positions"] is None
    assert any("injected read failure" in row.get("query_error", "") for row in state["query_errors"])


def test_database_diagnosis_missing_database_has_structured_cli_failure(tmp_path, capsys):
    missing = tmp_path / "missing.db"
    assert burnin_ops_main(["--db", str(missing), "diagnose-db"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "BLOCKED_DATABASE_STATE"
    assert "Traceback" not in json.dumps(payload)


def test_readonly_sqlite_uri_cross_platform_paths():
    assert _readonly_sqlite_uri("C:\\Alpha Forge\\burnin.db", platform="nt") == "file:C:/Alpha%20Forge/burnin.db?mode=ro"
    assert _readonly_sqlite_uri("/var/lib/alpha forge/burnin.db", platform="posix") == "file:/var/lib/alpha%20forge/burnin.db?mode=ro"


def test_phase9_preflight_rejects_non_paper(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "LIVE")
    out = preflight(str(tmp_path / "pf.db"), "rel", ["BTCUSDT"], ["1h"], require_market_data=False)
    assert out["status"] == "FAIL_CLOSED"
    assert "execution_mode_paper" in out["blockers"]


def test_preflight_cannot_pass_unverified_critical_check(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    monkeypatch.setattr(ops, "_git_clean", lambda: True)
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "dev\n")
    monkeypatch.setattr(ops, "_actual_runtime_identity", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unverified")))
    out = ops.preflight(str(tmp_path / "pf.db"), "rel", ["BTCUSDT"], ["1h"], require_market_data=False)
    assert out["status"] == "FAIL_CLOSED"
    assert "runtime_identity_matches_campaign_identity" in out["blockers"]
    assert next(c for c in out["checks"] if c["name"] == "runtime_identity_matches_campaign_identity")["status"] == "UNAVAILABLE"


def test_phase9_paper_candidate_and_runtime_identity_share_exact_config_payload(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("ALPHAFORGE_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}")
    # Non-default values cover the previously dropped RuntimeConfig fields.
    monkeypatch.setenv("ALPHAFORGE_MIN_SL_PCT", "0.31")
    monkeypatch.setenv("ALPHAFORGE_STOP_TOO_WIDE_SOFT_SCORE_MIN", "8.4")
    monkeypatch.setenv("ALPHAFORGE_MAX_TRADES_GLOBAL_PER_DAY", "7")
    candidate = ops._candidate_identity("rel", ["BTCUSDT"], ["1h"])
    runtime = ops._actual_runtime_identity("rel", ["BTCUSDT"], ["1h"])

    assert candidate["config_payload"] == runtime["config_payload"]
    assert candidate["config_hash"] == runtime["config_hash"]
    assert candidate["config_payload"]["RUNTIME_LIMITS_ACTIVE"] is True


def test_phase8_identity_is_mode_aware_and_component_hashes_are_deterministic():
    from alphaforge.config import RuntimeSettings

    paper = RuntimeSettings(execution_mode="PAPER")
    backtest = RuntimeSettings(execution_mode="BACKTEST")
    paper_identity = build_phase8_campaign_identity(paper, ["ETHUSDT", "BTCUSDT"], ["5m", "1h"], release_id="rel")
    paper_repeat = build_phase8_campaign_identity(paper, ["BTCUSDT", "ETHUSDT"], ["1h", "5m"], release_id="rel")
    backtest_identity = build_phase8_campaign_identity(backtest, ["BTCUSDT", "ETHUSDT"], ["1h", "5m"], release_id="rel")

    assert paper_identity["config_hash"] == paper_repeat["config_hash"]
    assert paper_identity["strategy_config_hash"] == paper_repeat["strategy_config_hash"]
    assert paper_identity["universe_hash"] == paper_repeat["universe_hash"]
    assert paper_identity["execution_cost_config_hash"] == paper_repeat["execution_cost_config_hash"]
    assert paper_identity["config_payload"]["RUNTIME_LIMITS_ACTIVE"] is True
    assert backtest_identity["config_payload"]["RUNTIME_LIMITS_ACTIVE"] is False
    assert paper_identity["config_hash"] != backtest_identity["config_hash"]


def test_preflight_passes_with_matching_runtime_identity_and_records_payloads(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("BINANCE_API_KEY", "valid-readonly-key-abcdef123456")
    monkeypatch.setenv("BINANCE_API_SECRET", "valid-readonly-secret-abcdef123456")
    monkeypatch.setattr(ops, "_git_clean", lambda: True)
    monkeypatch.setattr(ops, "_git_commit", lambda: "commit")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "dev\n")
    monkeypatch.setattr(ops, "clock_skew_check", lambda: {"status": "PASS"})
    monkeypatch.setattr(ops, "_actual_runtime_identity", lambda release, symbols, intervals: {**ops._candidate_identity(release, symbols, intervals), "execution_mode": "PAPER"})

    provider = type("Provider", (), {"snapshot": lambda self: {"evidence_status": "COMPLETE", "authenticated": True, "input_source": "AUTHENTICATED_EXCHANGE_SNAPSHOT", "orders": [], "positions": []}})()
    out = ops.preflight(str(tmp_path / "pf.db"), "rel", ["BTCUSDT"], ["1h"], require_market_data=False, reconciliation_provider=provider)
    check = next(c for c in out["checks"] if c["name"] == "runtime_identity_matches_campaign_identity")
    assert out["status"] == "PASS"
    assert check["status"] == "PASS"
    assert check["details"]["config_payload_differences"] == {}


def test_preflight_fails_closed_for_derived_runtime_config_drift(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    monkeypatch.setattr(ops, "_git_clean", lambda: True)
    monkeypatch.setattr(ops, "_git_commit", lambda: "commit")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "dev\n")
    monkeypatch.setattr(ops, "clock_skew_check", lambda: {"status": "PASS"})

    def drifted_runtime(release, symbols, intervals):
        identity = {**ops._candidate_identity(release, symbols, intervals), "execution_mode": "PAPER"}
        payload = {**identity["config_payload"], "MAX_TRADES_GLOBAL_PER_DAY": 999}
        return {**identity, "config_payload": payload, "config_hash": config_hash(payload)}

    monkeypatch.setattr(ops, "_actual_runtime_identity", drifted_runtime)
    out = ops.preflight(str(tmp_path / "pf.db"), "rel", ["BTCUSDT"], ["1h"], require_market_data=False)
    check = next(c for c in out["checks"] if c["name"] == "runtime_identity_matches_campaign_identity")
    assert out["status"] == "FAIL_CLOSED"
    assert "runtime_identity_matches_campaign_identity" in out["blockers"]
    assert check["details"]["config_payload_differences"]["MAX_TRADES_GLOBAL_PER_DAY"] == {"candidate": 10, "runtime": 999}


def test_phase9_health_detects_running_without_worker_and_sql_counters(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn, targets_zero=False)
    persist_burnin_observation(conn, observation_id="o1", burnin_run_id=run, release_id="rel", observed_at="2026-01-01T00:00:00Z", execution_mode="PAPER", symbol="BTCUSDT", regime="TREND", decision="ACCEPTED", lifecycle_state="FILLED", metrics={}, source_provenance={"t": "x"})
    conn.commit()
    h = health_payload(conn, camp.campaign_id, max_heartbeat_age=999999)
    assert h["total_decisions"] == 1 and h["accepted_decisions"] == 1
    assert "RUNNING_WITHOUT_WORKER" in h["unhealthy_reasons"]
    w = watch_once(conn, camp.campaign_id)
    assert w["status"] == "RECOVERY_REQUIRED"


def test_watchdog_detects_backlog_growth_and_provider_failures(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED', last_heartbeat_at=? WHERE campaign_id=?", (utc_now(), camp.campaign_id))
    health_payload(conn, camp.campaign_id, max_heartbeat_age=999999)
    conn.execute("INSERT INTO burnin_pending_reject_labels(pending_label_id,campaign_id,burnin_run_id,reject_decision_id,signal_id,symbol,side,decision_timestamp,entry,stop,target,horizon_seconds,execution_cost_assumptions_json,regime,reject_reason,source_provenance_json,due_at,status,created_at,schema_version) VALUES ('p1',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (camp.campaign_id, run, "r1", "s", "BTCUSDT", "LONG", utc_now(), 1, 0.9, 1.2, 3600, "{}", "TREND", "LOW", "{}", utc_now(), "PENDING", utc_now(), "sv"))
    for i in range(3):
        event(conn, camp.campaign_id, "RESOLVER_BATCH_FAILED", details={"error": "PROVIDER_FAILURE", "n": i})
    conn.commit()
    h = health_payload(conn, camp.campaign_id, max_heartbeat_age=999999)
    assert "RESOLVER_BACKLOG_GROWTH" in h["unhealthy_reasons"]
    assert "REPEATED_PROVIDER_FAILURES" in h["unhealthy_reasons"]


def test_detached_launch_waits_for_attach_and_fails_without_attach(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    started = utc_now()
    worker_started = utc_now()
    conn.execute("UPDATE burnin_campaigns SET worker_pid=123, worker_started_at=?, last_heartbeat_at=? WHERE campaign_id=?", (worker_started, utc_now(), camp.campaign_id))
    event(conn, camp.campaign_id, "PHASE8_CAMPAIGN_ATTACHED", burnin_run_id=run, details={"runtime_instance_id": "rt", "active_run_id": run})
    update_campaign_heartbeat(conn, camp.campaign_id)
    conn.commit()
    import alphaforge.burnin_ops as ops
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: True)
    ok = verify_worker_attachment(conn, camp.campaign_id, worker_started_at=worker_started, launch_started_at=started, timeout_seconds=0.01)
    assert ok["status"] == "ATTACHED"

    camp2, run2 = _campaign(conn, release="rel2")
    conn.execute("UPDATE burnin_campaigns SET worker_pid=124, worker_started_at=? WHERE campaign_id=?", (utc_now(), camp2.campaign_id))
    conn.commit()
    failed = verify_worker_attachment(conn, camp2.campaign_id, worker_started_at=utc_now(), launch_started_at=utc_now(), timeout_seconds=0.01)
    assert failed["status"] == "FAILED"


def test_foreground_launch_invokes_runner(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db = str(tmp_path / "launch.db")
    monkeypatch.setattr(ops, "preflight", lambda *a, **k: {"status": "PASS", "evidence_locations": {}})

    class FakeRunner:
        called = False
        def __init__(self, *a, **k): pass
        async def run_foreground(self):
            FakeRunner.called = True
            return {"status": "STOPPED"}

    monkeypatch.setattr(ops, "BurnInCampaignRunner", FakeRunner)
    out = launch_campaign(db, "rel", 0, ["BTCUSDT"], ["1h"], detach=False)
    assert out["status"] == "FOREGROUND_STOPPED"
    assert FakeRunner.called


def test_worker_launch_uses_persisted_campaign_release_not_shell_release(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    db, conn = _conn(tmp_path)
    camp, _ = _campaign(conn, release="phase9_trial")
    conn.close()
    captured = {}

    class P:
        pid = 123

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs["env"])
        return P()

    monkeypatch.setenv("ALPHAFORGE_RELEASE_ID", "wrong_shell_release")
    monkeypatch.setattr(ops.subprocess, "Popen", fake_popen)
    ops._launch_worker(str(db), camp.campaign_id)
    assert captured["ALPHAFORGE_RELEASE_ID"] == "phase9_trial"
    assert captured["ALPHAFORGE_EXECUTION_MODE"] == captured["EXECUTION_MODE"] == "PAPER"


def test_failed_zero_sample_run_is_terminal_and_excluded_from_aggregate(tmp_path):
    db, conn = _conn(tmp_path)
    camp, failed_run = _campaign(conn, release="phase9_trial")
    fail_active_campaign_run(conn, camp.campaign_id, "PHASE8_CAMPAIGN_RELEASE_MISMATCH")
    resumed = start_or_resume_campaign(conn, camp.campaign_id, resume=True)
    conn.commit()

    rows = conn.execute("SELECT burnin_run_id,status,ended_at FROM burnin_campaign_runs WHERE campaign_id=? ORDER BY continuation_sequence", (camp.campaign_id,)).fetchall()
    assert rows[0]["burnin_run_id"] == failed_run and rows[0]["status"] == "FAILED" and rows[0]["ended_at"]
    assert resumed["continuation_sequence"] == 1
    assert aggregate_campaign(conn, camp.campaign_id)["metrics"]["source_run_ids"] == [resumed["burnin_run_id"]]


def test_campaign_run_identity_mismatch_blocks_worker_spawn_and_is_idempotent(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn, release="phase9_trial")
    conn.execute("UPDATE burnin_runs SET release_id='stale_release' WHERE burnin_run_id=?", (run,)); conn.commit()
    monkeypatch.setattr(ops.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")))
    with pytest.raises(RuntimeError, match="PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH"):
        ops._launch_worker(str(db), camp.campaign_id)
    with pytest.raises(RuntimeError, match="PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH"):
        ops._launch_worker(str(db), camp.campaign_id)
    state = conn.execute("SELECT campaign_status,last_error FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()
    rows = conn.execute("SELECT status,ended_at FROM burnin_campaign_runs WHERE burnin_run_id=?", (run,)).fetchone()
    events = conn.execute("SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=? AND event_type='PHASE8_CAMPAIGN_ATTACH_FAILED'", (camp.campaign_id,)).fetchone()[0]
    assert tuple(state) == ("PAUSED", "PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH")
    assert tuple(rows)[0] == "FAILED" and tuple(rows)[1] and events == 1


def test_worker_spawn_failure_terminalizes_created_continuation(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    db = str(tmp_path / "spawn.db")
    monkeypatch.setattr(ops, "preflight", lambda *a, **k: {"status": "PASS", "evidence_locations": {}})
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: (_ for _ in ()).throw(OSError("spawn failed")))
    out = launch_campaign(db, "rel", 0, ["BTCUSDT"], ["1h"], detach=True)
    conn = sqlite3.connect(db); row = conn.execute("SELECT r.status, cr.status FROM burnin_runs r JOIN burnin_campaign_runs cr ON cr.burnin_run_id=r.burnin_run_id").fetchone(); conn.close()
    assert out["reason"] == "PHASE9_WORKER_SPAWN_FAILED" and row == ("FAILED", "FAILED")


def test_recovery_drill_starts_new_worker_and_preserves_exact_pending_ids(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("INSERT INTO burnin_pending_reject_labels(pending_label_id,campaign_id,burnin_run_id,reject_decision_id,signal_id,symbol,side,decision_timestamp,entry,stop,target,horizon_seconds,execution_cost_assumptions_json,regime,reject_reason,source_provenance_json,due_at,status,created_at,schema_version) VALUES ('p_keep',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (camp.campaign_id, run, "r_keep", "s", "BTCUSDT", "LONG", utc_now(), 1, 0.9, 1.2, 3600, "{}", "TREND", "LOW", "{}", utc_now(), "PENDING", utc_now(), "sv"))
    conn.execute("UPDATE burnin_campaigns SET worker_pid=500, worker_started_at=? WHERE campaign_id=?", (utc_now(), camp.campaign_id))
    conn.commit()
    monkeypatch.setattr(ops, "_stop_worker", lambda pid, timeout=10.0: True)
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: True)

    class P(SimpleNamespace):
        pid = 501
    monkeypatch.setattr(ops, "_launch_worker", lambda db, cid: P())
    monkeypatch.setattr(ops, "verify_worker_attachment", lambda *a, **k: {"status": "ATTACHED", "runtime_instance_id": "rt"})
    monkeypatch.setattr(ops, "qualify_campaign", lambda *a, **k: {"status": "stub"})
    out = recovery_drill(conn, camp.campaign_id, attach_timeout_seconds=0.01)
    assert out["checks"]["exactly_one_new_continuation"]
    assert out["checks"]["pending_reject_ids_preserved_exactly"]
    assert out["checks"]["restart_count_incremented_once"]


def test_recovery_drill_recovers_dead_pidless_running_continuation_with_evidence(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    db, conn = _conn(tmp_path)
    camp, old_run = _campaign(conn)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED', worker_pid=NULL, last_heartbeat_at='2020-01-01T00:00:00Z' WHERE campaign_id=?", (camp.campaign_id,))
    conn.commit()
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: int(pid or 0) == 501)
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: SimpleNamespace(pid=501))
    monkeypatch.setattr(ops, "verify_worker_attachment", lambda *a, **k: {"status": "ATTACHED", "runtime_instance_id": "rt"})
    out = recovery_drill(conn, camp.campaign_id, attach_timeout_seconds=0.01)
    assert out["status"] == "PASS"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (old_run,)).fetchone()[0] == "RECOVERY_REQUIRED"
    assert conn.execute("SELECT status FROM burnin_campaign_runs WHERE burnin_run_id=?", (old_run,)).fetchone()[0] == "RECOVERY_REQUIRED"
    assert out["checks"]["exactly_one_new_continuation"] is True
    assert out["after"]["resume"] is not None
    assert out["after"]["attach"]["status"] == "ATTACHED"
    successor = out["after"]["resume"]["burnin_run_id"]
    assert successor != old_run
    campaign_after = get_campaign(conn, camp.campaign_id)
    assert campaign_after["active_run_id"] == successor
    assert campaign_after["campaign_status"] == "RUNNING"
    assert campaign_after["restart_count"] == 1
    successor_count = conn.execute("SELECT COUNT(*) FROM burnin_campaign_runs WHERE campaign_id=?", (camp.campaign_id,)).fetchone()[0]
    replay = recovery_drill(conn, camp.campaign_id, attach_timeout_seconds=0.01)
    assert replay["status"] == "PASS" and replay["checks"]["idempotent_replay"] is True
    assert conn.execute("SELECT COUNT(*) FROM burnin_campaign_runs WHERE campaign_id=?", (camp.campaign_id,)).fetchone()[0] == successor_count
    details = conn.execute("SELECT details_json FROM burnin_campaign_events WHERE campaign_id=? AND event_type='PHASE9_STALE_CONTINUATION_RECOVERED'", (camp.campaign_id,)).fetchone()[0]
    assert json.loads(details)["transition"] == "RUNNING->RECOVERY_REQUIRED"


@pytest.mark.parametrize("field", ["active_positions", "pending_orders", "orphan_orders", "orphan_positions"])
def test_dead_continuation_with_runtime_exposure_never_resumes(monkeypatch, tmp_path, field):
    import alphaforge.burnin_ops as ops

    _, conn = _conn(tmp_path)
    camp, old_run = _campaign(conn)
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *a: {
        "blocked": True, "reason": "RUNTIME_RECOVERY_REQUIRED",
        "current_exposure_check": {name: int(name == field) for name in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")},
    })
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch")))
    out = recovery_drill(conn, camp.campaign_id)
    assert out["status"] == "FAIL"
    assert out["checks"]["failure_reasons"] == ["UNRESOLVED_RUNTIME_EXPOSURE_OR_RECONCILIATION"]
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (old_run,)).fetchone()[0] == "RUNNING"


def test_audit_detects_pre_decision_candle_hash_mismatch_and_dashboard_mismatch(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    persist_burnin_reject_outcome(conn, reject_outcome_id="rout", burnin_run_id=run, release_id="rel", reject_reason="LOW", symbol="BTCUSDT", decision_time="2026-01-01T01:00:00Z", forward_label="TP_BEFORE_SL", hypothetical_net_r_after_costs=1.0, payload={"candle_timestamps": ["2026-01-01T00:00:00Z"]})
    conn.execute("INSERT INTO burnin_qualification_snapshots(qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,expectancy_status,execution_status,regime_status,reject_quality_status,calibration_status,drawdown_status,concentration_status,reconciliation_status,evidence_completeness_status,blockers_json,warnings_json,thresholds_json,metrics_json,evidence_hash,schema_version,campaign_id,source_run_ids_json,aggregate_evidence_hash) VALUES ('q_bad',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run, "rel", utc_now(), "CANARY_QUALIFIED", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "[]", "[]", "{}", "{}", "hash", "sv", camp.campaign_id, json.dumps([run]), "wrong"))
    conn.commit()
    monkeypatch.setattr(ops, "_dashboard_campaign_snapshot", lambda db, cid: {"decisions": 999, "accepted": 999, "rejected": 999})
    audit = audit_payload(conn, camp.campaign_id)
    assert "rejected_labels_use_post_decision_candles_only" in audit["violations"]
    assert "stored_aggregate_evidence_hash_matches_recomputed" in audit["violations"]
    assert "dashboard_counters_match_sql_counters" in audit["violations"]


def test_source_evidence_immutable_hash_not_hard_coded(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    persist_burnin_observation(conn, observation_id="o1", burnin_run_id=run, release_id="rel", execution_mode="PAPER", symbol="BTCUSDT", decision="REJECTED", source_provenance={"p": "x"})
    conn.commit()
    assert audit_payload(conn, camp.campaign_id)["status"] == "PASS"
    persist_burnin_observation(conn, observation_id="o2", burnin_run_id=run, release_id="rel", execution_mode="PAPER", symbol="BTCUSDT", decision="ACCEPTED", source_provenance={"p": "x"})
    conn.commit()
    audit = audit_payload(conn, camp.campaign_id)
    assert audit["status"] == "PASS"


def test_canary_qualified_only_canonical_verdict_allows_canary_review(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    event(conn, camp.campaign_id, "PHASE8_CAMPAIGN_ATTACHED", burnin_run_id=run, details={"runtime_instance_id": "runtime:canary", "active_run_id": run})
    conn.execute("""CREATE TABLE runtime_heartbeats(
        id INTEGER PRIMARY KEY AUTOINCREMENT, runtime_instance_id TEXT NOT NULL,
        execution_mode TEXT NOT NULL, heartbeat_ts TEXT NOT NULL, scanner_source TEXT,
        runtime_state TEXT NOT NULL, last_scan_ts TEXT, last_decision_ts TEXT,
        active_positions_count INTEGER, pending_orders_count INTEGER,
        evidence_status TEXT NOT NULL, payload_json TEXT)""")
    conn.execute("INSERT INTO runtime_heartbeats(runtime_instance_id,execution_mode,heartbeat_ts,runtime_state,last_scan_ts,evidence_status,payload_json) VALUES (?,?,?,?,?,?,?)", ("runtime:canary", "PAPER", utc_now(), "STOPPING", utc_now(), "MEASURED_RUNTIME_HEARTBEAT", "{}"))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED', evidence_completeness_status='PASS', last_heartbeat_at=? WHERE campaign_id=?", (utc_now(), camp.campaign_id))
    agg_hash = aggregate_campaign(conn, camp.campaign_id).get("evidence_hash")
    conn.execute("INSERT INTO burnin_qualification_snapshots(qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,expectancy_status,execution_status,regime_status,reject_quality_status,calibration_status,drawdown_status,concentration_status,reconciliation_status,evidence_completeness_status,blockers_json,warnings_json,thresholds_json,metrics_json,evidence_hash,schema_version,campaign_id,source_run_ids_json,aggregate_evidence_hash) VALUES ('q_canary',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run, "rel", utc_now(), "CANARY_QUALIFIED", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "[]", "[]", "{}", "{}", agg_hash, "sv", camp.campaign_id, json.dumps([run]), agg_hash))
    conn.execute("UPDATE burnin_campaigns SET latest_qualification_id='q_canary', qualification_status='CANARY_QUALIFIED' WHERE campaign_id=?", (camp.campaign_id,))
    conn.commit()
    out = finalize(conn, str(db), camp.campaign_id, tmp_path / "final_canary")
    assert out["decision"] == "PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW"
    checks = json.loads((tmp_path / "final_canary" / "checksums.json").read_text())
    for rel, digest in checks.items():
        assert __import__("hashlib").sha256((tmp_path / "final_canary" / rel).read_bytes()).hexdigest() == digest

    camp2, run2 = _campaign(conn, release="rel_alias")
    conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED', evidence_completeness_status='PASS', last_heartbeat_at=? WHERE campaign_id=?", (utc_now(), camp2.campaign_id))
    conn.execute("INSERT INTO burnin_qualification_snapshots(qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,expectancy_status,execution_status,regime_status,reject_quality_status,calibration_status,drawdown_status,concentration_status,reconciliation_status,evidence_completeness_status,blockers_json,warnings_json,thresholds_json,metrics_json,evidence_hash,schema_version,campaign_id,source_run_ids_json,aggregate_evidence_hash) VALUES ('q_alias',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run2, "rel_alias", utc_now(), "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "[]", "[]", "{}", "{}", "alias_hash", "sv", camp2.campaign_id, json.dumps([run2]), aggregate_campaign(conn, camp2.campaign_id).get("evidence_hash")))
    conn.execute("UPDATE burnin_campaigns SET latest_qualification_id='q_alias', qualification_status='PASS' WHERE campaign_id=?", (camp2.campaign_id,))
    conn.commit()
    assert finalize(conn, str(db), camp2.campaign_id, tmp_path / "final_alias")["decision"] != "PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW"


def test_running_append_only_allows_growth_but_blocks_mutation_and_delete(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    persist_burnin_observation(conn, observation_id="o1", burnin_run_id=run, release_id="rel", execution_mode="PAPER", symbol="BTCUSDT", decision="REJECTED", source_provenance={"p": "x"})
    conn.commit()
    assert audit_payload(conn, camp.campaign_id)["status"] == "PASS"
    persist_burnin_observation(conn, observation_id="o2", burnin_run_id=run, release_id="rel", execution_mode="PAPER", symbol="BTCUSDT", decision="ACCEPTED", source_provenance={"p": "x"})
    conn.commit()
    assert audit_payload(conn, camp.campaign_id)["status"] == "PASS"
    conn.execute("UPDATE burnin_observations SET decision='MUTATED' WHERE observation_id='o1'")
    conn.commit()
    audit = audit_payload(conn, camp.campaign_id)
    assert "source_run_append_only_or_terminal_immutable" in audit["violations"]

    db2, conn2 = _conn(tmp_path / "delete")
    camp2, run2 = _campaign(conn2)
    persist_burnin_observation(conn2, observation_id="o1", burnin_run_id=run2, release_id="rel", execution_mode="PAPER", symbol="BTCUSDT", decision="REJECTED", source_provenance={"p": "x"})
    conn2.commit()
    assert audit_payload(conn2, camp2.campaign_id)["status"] == "PASS"
    conn2.execute("DELETE FROM burnin_observations WHERE observation_id='o1'")
    conn2.commit()
    audit2 = audit_payload(conn2, camp2.campaign_id)
    assert "source_run_append_only_or_terminal_immutable" in audit2["violations"]


def test_terminal_run_baseline_blocks_later_additions(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    persist_burnin_observation(conn, observation_id="o1", burnin_run_id=run, release_id="rel", execution_mode="PAPER", symbol="BTCUSDT", decision="REJECTED", source_provenance={"p": "x"})
    conn.execute("UPDATE burnin_runs SET status='COMPLETED' WHERE burnin_run_id=?", (run,))
    conn.commit()
    assert audit_payload(conn, camp.campaign_id)["status"] == "PASS"
    persist_burnin_observation(conn, observation_id="o2", burnin_run_id=run, release_id="rel", execution_mode="PAPER", symbol="BTCUSDT", decision="ACCEPTED", source_provenance={"p": "x"})
    conn.commit()
    audit = audit_payload(conn, camp.campaign_id)
    assert "source_run_append_only_or_terminal_immutable" in audit["violations"]


def test_clock_skew_pass_fail_unavailable_paths(monkeypatch):
    base = 1_700_000_000_000
    monkeypatch.setattr("time.time", lambda: base / 1000)
    ok = clock_skew_check(max_skew_ms=1000, provider=lambda: {"provider_utc_ms": base + 500, "provider_provenance": {"provider": "TEST_READ_ONLY"}})
    assert ok["status"] == "PASS" and ok["absolute_skew_ms"] == 500
    bad = clock_skew_check(max_skew_ms=100, provider=lambda: base + 500)
    assert bad["status"] == "FAIL"
    unavailable = clock_skew_check(max_skew_ms=100, provider=lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert unavailable["status"] == "UNAVAILABLE"


def test_failed_worker_termination_creates_no_continuation_or_worker(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_campaigns SET worker_pid=500, worker_started_at=? WHERE campaign_id=?", (utc_now(), camp.campaign_id))
    conn.commit()
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(ops, "_stop_worker", lambda pid, timeout=10.0: False)
    launched = {"called": False}
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: launched.update(called=True))
    before_runs = conn.execute("SELECT COUNT(*) FROM burnin_campaign_runs WHERE campaign_id=?", (camp.campaign_id,)).fetchone()[0]
    before_restart = get_campaign(conn, camp.campaign_id)["restart_count"]
    out = recovery_drill(conn, camp.campaign_id, attach_timeout_seconds=0.01)
    after_runs = conn.execute("SELECT COUNT(*) FROM burnin_campaign_runs WHERE campaign_id=?", (camp.campaign_id,)).fetchone()[0]
    after_campaign = get_campaign(conn, camp.campaign_id)
    assert out["status"] == "FAIL"
    assert before_runs == after_runs
    assert after_campaign["restart_count"] == before_restart
    assert not launched["called"]
    assert after_campaign["campaign_status"] == "RECOVERY_REQUIRED"


def test_null_and_mismatched_aggregate_hash_fail_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("INSERT INTO burnin_qualification_snapshots(qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,expectancy_status,execution_status,regime_status,reject_quality_status,calibration_status,drawdown_status,concentration_status,reconciliation_status,evidence_completeness_status,blockers_json,warnings_json,thresholds_json,metrics_json,evidence_hash,schema_version,campaign_id,source_run_ids_json,aggregate_evidence_hash) VALUES ('q_null',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run, "rel", utc_now(), "CANARY_QUALIFIED", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "[]", "[]", "{}", "{}", "hash", "sv", camp.campaign_id, json.dumps([run]), None))
    conn.execute("INSERT INTO burnin_qualification_snapshots(qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,expectancy_status,execution_status,regime_status,reject_quality_status,calibration_status,drawdown_status,concentration_status,reconciliation_status,evidence_completeness_status,blockers_json,warnings_json,thresholds_json,metrics_json,evidence_hash,schema_version,campaign_id,source_run_ids_json,aggregate_evidence_hash) VALUES ('q_bad_hash',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run, "rel", utc_now(), "CANARY_QUALIFIED", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS", "[]", "[]", "{}", "{}", "hash", "sv", camp.campaign_id, json.dumps([run]), "wrong"))
    conn.commit()
    audit = audit_payload(conn, camp.campaign_id)
    assert "AGGREGATE_EVIDENCE_HASH_MISSING" in audit["violations"]
    assert "AGGREGATE_EVIDENCE_HASH_MISMATCH" in audit["violations"]



def test_phase9_audit_detects_incomplete_outcome_and_finalize_never_live(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    persist_burnin_trade_outcome(conn, outcome_id="t1", burnin_run_id=run, release_id="rel", trade_id="tr1", symbol="BTCUSDT", regime="TREND", closed_at="2026-01-01T01:00:00Z", gross_r=1, gross_pnl=1, costs={"spread_cost": None}, net_r=None, net_pnl=None, hold_duration_seconds=3600, mfe=1, mae=0, exit_reason="TP", payload={})
    conn.commit()
    audit = audit_payload(conn, camp.campaign_id)
    assert audit["status"] == "FAIL"
    assert "no_incomplete_outcomes_counted_complete" in audit["violations"] or "no_missing_cost_fields_in_qualified_outcomes" in audit["violations"]
    out = finalize(conn, str(db), camp.campaign_id, tmp_path / "final")
    assert out["decision"] != "LIVE_READY"
    assert json.loads((tmp_path / "final" / "release_decision.json").read_text())["decision"] in {"PAPER_BURNIN_FAILED", "PAPER_BURNIN_INCOMPLETE"}


def test_watch_cleans_dead_worker_and_terminalizes_both_run_tables(monkeypatch, tmp_path):
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_campaigns SET worker_pid=99999, worker_started_at=? WHERE campaign_id=?", (utc_now(), camp.campaign_id)); conn.commit()
    monkeypatch.setattr("alphaforge.burnin_ops._pid_alive", lambda pid: False)
    result = watch_once(conn, camp.campaign_id)
    assert result["cleaned_dead_worker"] is True
    assert conn.execute("SELECT status,end_time FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()["status"] == "RECOVERY_REQUIRED"
    assert conn.execute("SELECT status,ended_at FROM burnin_campaign_runs WHERE burnin_run_id=?", (run,)).fetchone()["status"] == "RECOVERY_REQUIRED"
    row = conn.execute("SELECT campaign_status,worker_pid,worker_started_at FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()
    assert row["campaign_status"] == "RECOVERY_REQUIRED" and row["worker_pid"] is None and row["worker_started_at"] is None


def _prepare_terminalization_evidence(conn, camp, run):
    _runtime_table(conn, camp, run)
    for definition in ("instance_id TEXT", "startup_id TEXT", "process_id INTEGER", "mode TEXT"):
        try: conn.execute(f"ALTER TABLE runtime_state_snapshots ADD COLUMN {definition}")
        except sqlite3.OperationalError: pass
    conn.execute("UPDATE runtime_state_snapshots SET instance_id='runtime',startup_id='startup',process_id=999999,mode='PAPER',timestamp=?", (utc_now(),))
    event(conn, camp.campaign_id, "PHASE9_DEAD_WORKER_RECOVERY_REQUIRED", burnin_run_id=run, details={"worker_pid": 999999, "evidence_preserved": True})
    conn.commit()


def _clean_runtime_recovery(conn):
    latest = dict(conn.execute("SELECT * FROM runtime_state_snapshots ORDER BY id DESC LIMIT 1").fetchone())
    return {
        "blocked": False, "query_errors": [], "kill_switch_active": False, "reconciliation_status": "CLEAN", "latest": latest,
        "current_exposure_check": {name: 0 for name in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")},
        "availability": {"active_positions_available": True, "pending_orders_available": True, "orphan_evidence_available": True, "kill_switch_available": True},
    }


def test_stale_starting_scanner_with_9990_decisions_terminalizes_only_with_clean_zero_exposure(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops
    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='STARTING',worker_pid=999999 WHERE campaign_id=?", (camp.campaign_id,))
    conn.execute("UPDATE burnin_runs SET status='STARTING' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='STARTING' WHERE burnin_run_id=?", (run,))
    conn.execute("""WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<9990)
        INSERT INTO burnin_observations(observation_id,burnin_run_id,release_id,observed_at,execution_mode,decision,lifecycle_state,evidence_complete,missing_fields_json,metrics_json,source_provenance_json,schema_version)
        SELECT 'stale-'||x,?,?,?,'PAPER','REJECTED','SIGNAL_REJECTED',1,'[]','{}','{}','test' FROM n""",
                 (run, camp.release_id, utc_now()))
    conn.commit()
    _prepare_terminalization_evidence(conn, camp, run)
    monkeypatch.setattr(ops, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *_: _clean_runtime_recovery(conn))

    result = recovery_drill(conn, camp.campaign_id)

    assert result["status"] == "PASS" and result["checks"]["run_decisions"] == 9990
    assert tuple(conn.execute("SELECT campaign_status,worker_pid FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()) == ("FAILED", None)
    assert conn.execute("SELECT COUNT(*) FROM burnin_observations WHERE burnin_run_id=?", (run,)).fetchone()[0] == 9990
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "FAILED"


def test_operational_worker_promotes_all_starting_statuses_atomically(tmp_path):
    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='STARTING' WHERE campaign_id=?", (camp.campaign_id,))
    conn.execute("UPDATE burnin_runs SET status='STARTING' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='STARTING' WHERE burnin_run_id=?", (run,))
    mark_attached_campaign_operational(conn, camp.campaign_id, run, runtime_instance_id="runtime:test")
    conn.commit()
    assert conn.execute("SELECT campaign_status FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()[0] == "RUNNING"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "RUNNING"
    assert conn.execute("SELECT status FROM burnin_campaign_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "RUNNING"


def test_operational_promotion_rejects_partially_running_linkage(tmp_path):
    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_runs SET status='STARTING' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='STARTING' WHERE burnin_run_id=?", (run,))
    conn.commit()

    with pytest.raises(RuntimeError, match="PHASE8_CAMPAIGN_OPERATIONAL_TRANSITION_INCONSISTENT"):
        mark_attached_campaign_operational(conn, camp.campaign_id, run, runtime_instance_id="runtime:test")

    assert get_campaign(conn, camp.campaign_id)["campaign_status"] == "RUNNING"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "STARTING"


def test_explicit_zero_exposure_terminalization_is_atomic_idempotent_and_unblocks(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops
    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED' WHERE campaign_id=? AND burnin_run_id=?", (camp.campaign_id, run))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',worker_pid=NULL WHERE campaign_id=?", (camp.campaign_id,)); conn.commit()
    _prepare_terminalization_evidence(conn, camp, run)
    before = ops._campaign_source_evidence_hash(conn, camp.campaign_id)
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *_: _clean_runtime_recovery(conn))

    result = ops.terminalize_zero_exposure_recovery(conn, camp.campaign_id)
    assert result["status"] == "PASS" and result["idempotent_replay"] is False
    assert conn.execute("SELECT campaign_status FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()[0] == "FAILED"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "FAILED"
    assert conn.execute("SELECT status FROM burnin_campaign_runs WHERE campaign_id=? AND burnin_run_id=?", (camp.campaign_id, run)).fetchone()[0] == "FAILED"
    assert ops._campaign_source_evidence_hash(conn, camp.campaign_id) == before
    assert conn.execute("SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=? AND event_type='PHASE9_MANUAL_ZERO_EXPOSURE_TERMINALIZED'", (camp.campaign_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM burnin_campaigns WHERE campaign_id=? AND campaign_status IN ('CREATED','STARTING','RUNNING','PAUSED','RECOVERY_REQUIRED')", (camp.campaign_id,)).fetchone()[0] == 0
    assert ops.terminalize_zero_exposure_recovery(conn, camp.campaign_id)["idempotent_replay"] is True


@pytest.mark.parametrize("decision_count", [0, 3])
def test_historical_terminalization_persists_fresh_campaign_linked_probe(monkeypatch, tmp_path, decision_count):
    import alphaforge.burnin_ops as ops
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',worker_pid=NULL WHERE campaign_id=?", (camp.campaign_id,))
    _prepare_terminalization_evidence(conn, camp, run)
    conn.execute("UPDATE runtime_state_snapshots SET campaign_id=NULL,burnin_run_id=NULL,timestamp='2020-01-01T00:00:00Z'")
    for index in range(decision_count):
        persist_burnin_observation(conn, observation_id=f"decision-{index}", burnin_run_id=run, release_id=camp.release_id,
                                   execution_mode="PAPER", decision="REJECTED", lifecycle_state="SIGNAL_REJECTED", source_provenance={})
    conn.commit()
    old_id = conn.execute("SELECT MAX(id) FROM runtime_state_snapshots").fetchone()[0]
    recovery = _clean_runtime_recovery(conn)
    recovery.update({
        "blocked": False, "reconciliation_probe_clean": True,
        "reconciliation_probe": {"provider": "test-fixture", "authenticated": True, "input_source": "AUTHENTICATED_EXCHANGE_SNAPSHOT", "retrieved_at": utc_now(), "evidence_status": "COMPLETE", "orders": [], "positions": [], "errors": []},
    })
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *_: recovery)

    result = ops.terminalize_zero_exposure_recovery(conn, camp.campaign_id)

    assert result["status"] == "PASS" and result["terminal_status"] == "FAILED"
    evidence = conn.execute("SELECT * FROM runtime_state_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    assert evidence["id"] > old_id
    assert (evidence["campaign_id"], evidence["burnin_run_id"], evidence["release_id"], evidence["mode"]) == (camp.campaign_id, run, camp.release_id, "PAPER")
    assert evidence["reconciliation_status"] == "CLEAN" and evidence["unknown_exchange_state"] == 0
    details = json.loads(conn.execute("SELECT details_json FROM burnin_campaign_events WHERE event_type='PHASE9_MANUAL_ZERO_EXPOSURE_TERMINALIZED' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert details["runtime_evidence"]["snapshot_id"] == evidence["id"]
    assert details["runtime_evidence"]["snapshot_hash"] == result["runtime_evidence"]["snapshot_hash"]


def test_historical_terminalization_provider_unavailable_persists_no_snapshot(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops
    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',worker_pid=NULL WHERE campaign_id=?", (camp.campaign_id,))
    _prepare_terminalization_evidence(conn, camp, run)
    conn.execute("UPDATE runtime_state_snapshots SET campaign_id=NULL,burnin_run_id=NULL,timestamp='2020-01-01T00:00:00Z'"); conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM runtime_state_snapshots").fetchone()[0]
    recovery = _clean_runtime_recovery(conn)
    recovery.update({"blocked": True, "query_errors": ["reconciliation_probe:provider_unavailable"], "reconciliation_probe_clean": False, "reconciliation_probe": None})
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *_: recovery)

    result = ops.terminalize_zero_exposure_recovery(conn, camp.campaign_id)

    assert result["status"] == "FAIL_CLOSED"
    assert conn.execute("SELECT COUNT(*) FROM runtime_state_snapshots").fetchone()[0] == before
    assert get_campaign(conn, camp.campaign_id)["campaign_status"] == "RECOVERY_REQUIRED"


@pytest.mark.parametrize("provenance", [
    {"authenticated": False, "input_source": "AUTHENTICATED_EXCHANGE_SNAPSHOT"},
    {},
    {"provider": "authenticated-test"},
])
def test_historical_terminalization_rejects_non_authoritative_provenance(monkeypatch, tmp_path, provenance):
    import alphaforge.burnin_ops as ops
    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',worker_pid=NULL WHERE campaign_id=?", (camp.campaign_id,))
    _prepare_terminalization_evidence(conn, camp, run)
    conn.execute("UPDATE runtime_state_snapshots SET campaign_id=NULL,burnin_run_id=NULL,timestamp='2020-01-01T00:00:00Z'"); conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM runtime_state_snapshots").fetchone()[0]
    probe = {"evidence_status": "COMPLETE", "orders": [], "positions": [], "errors": [], **provenance}
    recovery = _clean_runtime_recovery(conn)
    recovery.update({"blocked": False, "reconciliation_probe_clean": True, "reconciliation_probe": probe})
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *_: recovery)

    result = ops.terminalize_zero_exposure_recovery(conn, camp.campaign_id)

    assert result["status"] == "FAIL_CLOSED"
    assert conn.execute("SELECT COUNT(*) FROM runtime_state_snapshots").fetchone()[0] == before
    assert get_campaign(conn, camp.campaign_id)["campaign_status"] == "RECOVERY_REQUIRED"


def test_recovery_required_terminalization_requires_explicit_flag_and_complete_evidence(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',worker_pid=NULL WHERE campaign_id=?", (camp.campaign_id,)); conn.commit()
    monkeypatch.setattr(ops, "recovery_drill", lambda *_: {"status": "FAIL", "failure": "STALE_OR_INVALID_CONTINUATION_REQUIRES_MANUAL_RECOVERY"})
    assert ops.main(["--db", str(db), "recover-runtime", "--campaign-id", camp.campaign_id]) == 1
    assert get_campaign(conn, camp.campaign_id)["campaign_status"] == "RECOVERY_REQUIRED"
    _prepare_terminalization_evidence(conn, camp, run)
    unavailable = _clean_runtime_recovery(conn); unavailable["availability"]["orphan_evidence_available"] = False
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *_: unavailable)
    result = ops.terminalize_zero_exposure_recovery(conn, camp.campaign_id)
    assert result["status"] == "FAIL_CLOSED" and "RUNTIME_EXPOSURE_UNAVAILABLE" in result["failure_reasons"]
    assert get_campaign(conn, camp.campaign_id)["campaign_status"] == "RECOVERY_REQUIRED"


def test_manual_terminalization_event_failure_rolls_back_all_statuses(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops
    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',worker_pid=NULL WHERE campaign_id=?", (camp.campaign_id,)); conn.commit()
    _prepare_terminalization_evidence(conn, camp, run)
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *_: _clean_runtime_recovery(conn))
    monkeypatch.setattr(ops, "event", lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("event failed")))
    with pytest.raises(sqlite3.OperationalError, match="event failed"):
        ops.terminalize_zero_exposure_recovery(conn, camp.campaign_id)
    assert get_campaign(conn, camp.campaign_id)["campaign_status"] == "RECOVERY_REQUIRED"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "RECOVERY_REQUIRED"


@pytest.mark.parametrize(("mutation", "reason"), [
    ("campaign_status", "CAMPAIGN_STATUS_CHANGED"),
    ("active_run", "ACTIVE_RUN_CHANGED"),
    ("continuation_status", "CONTINUATION_STATUS_CHANGED"),
    ("open_position", "OPEN_POSITIONS_NONZERO"),
    ("pending_reject", "PENDING_REJECTS_NONZERO"),
    ("execution", "EXECUTIONS_NONZERO_OR_UNAVAILABLE"),
    ("lifecycle", "LIFECYCLE_EXECUTIONS_NONZERO"),
    ("source", "SOURCE_HASH_CHANGED"),
    ("worker_identity", "WORKER_IDENTITY_CHANGED"),
    ("external_linkage", "EXTERNAL_EVIDENCE_LINKAGE_CHANGED"),
])
def test_terminalization_revalidates_local_state_inside_immediate_transaction(monkeypatch, tmp_path, mutation, reason):
    import alphaforge.burnin_ops as ops
    from alphaforge.burnin_resolver import persist_pending_position, persist_pending_reject_label
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',worker_pid=NULL WHERE campaign_id=?", (camp.campaign_id,)); conn.commit()
    _prepare_terminalization_evidence(conn, camp, run)
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *_: _clean_runtime_recovery(conn))

    def mutate():
        other = sqlite3.connect(db); other.row_factory = sqlite3.Row
        if mutation == "campaign_status": other.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED' WHERE campaign_id=?", (camp.campaign_id,))
        elif mutation == "active_run": other.execute("UPDATE burnin_campaigns SET active_run_id='changed' WHERE campaign_id=?", (camp.campaign_id,))
        elif mutation == "continuation_status": other.execute("UPDATE burnin_campaign_runs SET status='FAILED' WHERE campaign_id=? AND burnin_run_id=?", (camp.campaign_id, run))
        elif mutation == "open_position": persist_pending_position(other, trade_id="race", campaign_id=camp.campaign_id, burnin_run_id=run, signal_id="s", symbol="BTCUSDT", side="LONG", entry_time=utc_now(), planned_entry=1, simulated_fill=1, stop=.9, target=1.2, quantity=1, notional=1, entry_spread=0, entry_slippage=0, entry_fee=0, regime="TREND", source_provenance={})
        elif mutation == "pending_reject": persist_pending_reject_label(other, campaign_id=camp.campaign_id, burnin_run_id=run, reject_decision_id="race", signal_id="s", symbol="BTCUSDT", side="LONG", decision_timestamp=utc_now(), entry=1, stop=.9, target=1.2, horizon_seconds=60, execution_cost_assumptions={}, regime="TREND", reject_reason="LOW_CONFIDENCE", source_provenance={})
        elif mutation == "execution": persist_burnin_trade_outcome(other, outcome_id="race", burnin_run_id=run, release_id=camp.release_id, trade_id="race", symbol="BTCUSDT", regime="TREND", gross_r=0, gross_pnl=0, costs={}, net_r=0, net_pnl=0, exit_reason="CANCELLED")
        elif mutation in {"lifecycle", "source"}: persist_burnin_observation(other, observation_id="race", burnin_run_id=run, release_id=camp.release_id, execution_mode="PAPER", decision="REJECTED", lifecycle_state="ENTRY_TRIGGERED" if mutation == "lifecycle" else "SIGNAL_REJECTED", source_provenance={})
        elif mutation == "worker_identity": other.execute("UPDATE burnin_campaign_events SET details_json=? WHERE campaign_id=? AND event_type='PHASE9_DEAD_WORKER_RECOVERY_REQUIRED'", (json.dumps({"worker_pid": 888888}), camp.campaign_id))
        elif mutation == "external_linkage": other.execute("UPDATE runtime_state_snapshots SET startup_id='changed' WHERE id=1")
        other.commit(); other.close()
    monkeypatch.setattr(ops, "_terminalization_pre_begin_hook", mutate)
    result = ops.terminalize_zero_exposure_recovery(conn, camp.campaign_id)
    assert result["status"] == "FAIL_CLOSED" and reason in result["failure_reasons"]
    assert conn.execute("SELECT campaign_status FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()[0] != "FAILED"


@pytest.mark.parametrize("target", ["run", "campaign"])
def test_terminalization_requires_exactly_one_conditional_update(monkeypatch, tmp_path, target):
    import alphaforge.burnin_ops as ops
    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',worker_pid=NULL WHERE campaign_id=?", (camp.campaign_id,)); conn.commit()
    _prepare_terminalization_evidence(conn, camp, run)
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *_: _clean_runtime_recovery(conn))
    table = "burnin_runs" if target == "run" else "burnin_campaigns"
    column = "status" if target == "run" else "campaign_status"
    conn.execute(f"CREATE TRIGGER ignore_terminal_{target} BEFORE UPDATE OF {column} ON {table} BEGIN SELECT RAISE(IGNORE); END"); conn.commit()
    result = ops.terminalize_zero_exposure_recovery(conn, camp.campaign_id)
    assert result["status"] == "FAIL_CLOSED" and result["failure_reasons"] == ["CONDITIONAL_UPDATE_ROWCOUNT_MISMATCH"]
    assert get_campaign(conn, camp.campaign_id)["campaign_status"] == "RECOVERY_REQUIRED"


def test_unrelated_failed_campaign_is_not_terminalization_replay(tmp_path):
    import alphaforge.burnin_ops as ops
    _, conn = _conn(tmp_path); camp, _ = _campaign(conn)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='FAILED' WHERE campaign_id=?", (camp.campaign_id,)); conn.commit()
    with pytest.raises(RuntimeError, match="NO_MATCHING_TERMINALIZATION_AUDIT"):
        ops.terminalize_zero_exposure_recovery(conn, camp.campaign_id)


def test_pause_is_operator_activity_not_runtime_heartbeat_and_terminalizes_run(tmp_path):
    from alphaforge.burnin_campaign import pause_campaign
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    heartbeat = "2026-01-01T00:00:00+00:00"
    conn.execute("UPDATE burnin_campaigns SET last_heartbeat_at=?,worker_pid=42 WHERE campaign_id=?", (heartbeat, camp.campaign_id))
    pause_campaign(conn, camp.campaign_id); conn.commit()
    row = conn.execute("SELECT last_heartbeat_at,last_operator_activity_at,worker_pid,campaign_status FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()
    assert row["last_heartbeat_at"] == heartbeat and row["last_operator_activity_at"] and row["worker_pid"] == 42 and row["campaign_status"] == "PAUSED"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()["status"] == "PAUSED"
    assert conn.execute("SELECT status FROM burnin_campaign_runs WHERE burnin_run_id=?", (run,)).fetchone()["status"] == "PAUSED"


def test_post_attach_exception_uses_accurate_event_and_terminalizes(tmp_path):
    from alphaforge.burnin_campaign import terminalize_active_campaign_run
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    event(conn, camp.campaign_id, "PHASE8_CAMPAIGN_ATTACHED", burnin_run_id=run, details={"runtime_instance_id": "runtime", "active_run_id": run})
    terminalize_active_campaign_run(conn, camp.campaign_id, run_status="FAILED", campaign_status="FAILED", reason="WORKER_UNCAUGHT_EXCEPTION", event_type="WORKER_UNCAUGHT_EXCEPTION", details={"exception_type": "RuntimeError", "message": "boom", "traceback": "Traceback: boom", "worker_pid": 123, "stdout_log_path": "artifacts/burnin/x/worker.stdout.log", "stderr_log_path": "artifacts/burnin/x/worker.stderr.log"})
    conn.commit()
    events = [dict(r) for r in conn.execute("SELECT event_type,details_json FROM burnin_campaign_events WHERE campaign_id=?", (camp.campaign_id,))]
    crash = next(json.loads(e["details_json"]) for e in events if e["event_type"] == "WORKER_UNCAUGHT_EXCEPTION")
    assert not any(e["event_type"] == "PHASE8_CAMPAIGN_ATTACH_FAILED" for e in events)
    assert crash["traceback"] == "Traceback: boom" and crash["stdout_log_path"].endswith("worker.stdout.log")
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "FAILED"
    assert conn.execute("SELECT status FROM burnin_campaign_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "FAILED"
    state = conn.execute("SELECT campaign_status,last_error,worker_pid FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()
    assert tuple(state) == ("FAILED", "WORKER_UNCAUGHT_EXCEPTION", None)


def test_dead_unrelated_historical_provider_unavailable_recovers_with_local_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION", "false")
    import alphaforge.burnin_ops as ops
    from alphaforge.persistence import init_db
    from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot, latest_runtime_state_snapshot

    db, conn = _conn(tmp_path)
    camp, old_run = _campaign(conn)
    engine = init_db(f"sqlite+pysqlite:///{db}")
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="RECOVERY_REQUIRED", instance_id="old", startup_id="old", process_id=99999999, campaign_id="other", fail_closed_reason="UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED", recovery_action_required=True))
    engine.dispose()
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED', worker_pid=NULL, last_heartbeat_at='2020-01-01T00:00:00Z' WHERE campaign_id=?", (camp.campaign_id,))
    conn.commit()
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: int(pid or 0) == 501)
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: SimpleNamespace(pid=501))
    monkeypatch.setattr(ops, "verify_worker_attachment", lambda *a, **k: {"status": "ATTACHED", "runtime_instance_id": "rt"})

    out = recovery_drill(conn, camp.campaign_id, attach_timeout_seconds=0.01)
    assert out["status"] == "PASS"
    assert out["checks"]["historical_zero_local_fallback"] is True
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (old_run,)).fetchone()[0] == "RECOVERY_REQUIRED"
    details = json.loads(conn.execute("SELECT details_json FROM burnin_campaign_events WHERE campaign_id=? AND event_type='PHASE9_STALE_CONTINUATION_RECOVERED'", (camp.campaign_id,)).fetchone()[0])
    assert details["runtime_recovery"]["fallback_original_runtime_recovery"]["query_errors"]
    latest = latest_runtime_state_snapshot(init_db(f"sqlite+pysqlite:///{db}"))
    assert latest["reconciliation_status"] == "LOCAL_ONLY_DIAGNOSTIC"
    assert latest["runtime_status"] == "LOCAL_DIAGNOSTIC_RECOVERY"
    assert latest["unknown_exchange_state"]
    evt = conn.execute("SELECT diagnostics_json FROM runtime_recovery_events WHERE status='HISTORICAL_RUNTIME_RECOVERED_LOCAL_EVIDENCE'").fetchone()
    assert evt and "read_only_reconciliation_provider_unavailable" in evt[0]


def test_related_provider_unavailable_remains_blocked(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops
    from alphaforge.persistence import init_db
    from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot

    db, conn = _conn(tmp_path)
    camp, old_run = _campaign(conn)
    engine = init_db(f"sqlite+pysqlite:///{db}")
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="RECOVERY_REQUIRED", instance_id="old", startup_id="old", process_id=99999999, campaign_id=camp.campaign_id, fail_closed_reason="UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED", recovery_action_required=True))
    engine.dispose()
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch")))
    out = recovery_drill(conn, camp.campaign_id)
    assert out["status"] == "FAIL"
    assert out["checks"].get("historical_zero_local_fallback") is False
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (old_run,)).fetchone()[0] == "RUNNING"


def test_live_process_remains_blocked(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    _, conn = _conn(tmp_path)
    camp, _old_run = _campaign(conn)
    conn.execute("UPDATE burnin_campaigns SET worker_pid=123 WHERE campaign_id=?", (camp.campaign_id,))
    conn.commit()
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(ops, "_stop_worker", lambda *a, **k: False)
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch")))
    out = recovery_drill(conn, camp.campaign_id)
    assert out["status"] == "FAIL"
    assert out["checks"]["failure_reasons"] == ["RECOVERY_DRILL_WORKER_TERMINATION_FAILED"]


@pytest.mark.parametrize("failed_source,error_key", [
    ("positions", "local_exposure_query_errors"),
    ("orders", "local_exposure_query_errors"),
    ("reconciliation", "reconciliation_storage_errors"),
    ("kill_switch", "kill_switch_query_errors"),
])
def test_provider_unavailable_with_authoritative_query_failure_blocks_fallback(monkeypatch, tmp_path, failed_source, error_key):
    import alphaforge.burnin_ops as ops

    _, conn = _conn(tmp_path)
    camp, old_run = _campaign(conn)
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch")))
    availability = {"active_positions_available": True, "pending_orders_available": True, "orphan_evidence_available": True, "kill_switch_available": True}
    if failed_source == "positions":
        availability["active_positions_available"] = False
    elif failed_source == "orders":
        availability["pending_orders_available"] = False
    elif failed_source == "reconciliation":
        availability["orphan_evidence_available"] = False
    elif failed_source == "kill_switch":
        availability["kill_switch_available"] = False
    provider = "reconciliation_probe:RuntimeError:read_only_reconciliation_provider_unavailable"
    local = f"{failed_source}:OperationalError:simulated"
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *a: {
        "blocked": True,
        "reason": "RECOVERY_EVIDENCE_UNAVAILABLE",
        "scope": "GLOBAL_EXECUTION_RISK",
        "previous_process_alive": False,
        "kill_switch_active": False,
        "query_errors": [provider, local],
        "provider_unavailable_errors": [provider],
        "local_exposure_query_errors": [local] if error_key == "local_exposure_query_errors" else [],
        "reconciliation_storage_errors": [local] if error_key == "reconciliation_storage_errors" else [],
        "kill_switch_query_errors": [local] if error_key == "kill_switch_query_errors" else [],
        "current_exposure_check": {name: 0 for name in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")},
        "availability": availability,
    })
    out = recovery_drill(conn, camp.campaign_id)
    assert out["status"] == "FAIL"
    assert out["checks"]["historical_zero_local_fallback"] is False
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (old_run,)).fetchone()[0] == "RUNNING"


def test_post_fallback_re_evaluation_blocked_prevents_terminalization_and_successor(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    _, conn = _conn(tmp_path)
    camp, old_run = _campaign(conn)
    provider = "reconciliation_probe:RuntimeError:read_only_reconciliation_provider_unavailable"
    first = {
        "blocked": True, "reason": "RECOVERY_EVIDENCE_UNAVAILABLE", "scope": "UNRELATED_HISTORICAL_RUNTIME",
        "previous_process_alive": False, "kill_switch_active": False, "query_errors": [provider],
        "provider_unavailable_errors": [provider], "current_exposure_check": {name: 0 for name in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")},
        "availability": {"active_positions_available": True, "pending_orders_available": True, "orphan_evidence_available": True, "kill_switch_available": True},
        "latest": {"mode": "PAPER"},
    }
    second = {**first, "scope": "GLOBAL_EXECUTION_RISK", "query_errors": [], "provider_unavailable_errors": [], "current_exposure_check": {"active_positions": 1, "pending_orders": 0, "orphan_orders": 0, "orphan_positions": 0}, "blocked": True, "reason": "RUNTIME_RECOVERY_REQUIRED"}
    calls = iter([first, second])
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *a: next(calls))
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch")))
    out = recovery_drill(conn, camp.campaign_id)
    assert out["status"] == "FAIL"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (old_run,)).fetchone()[0] == "RUNNING"
    assert conn.execute("SELECT COUNT(*) FROM burnin_campaign_runs WHERE campaign_id=?", (camp.campaign_id,)).fetchone()[0] == 1


def _patch_launch_preflight(monkeypatch):
    import alphaforge.burnin_ops as ops
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    monkeypatch.setattr(ops, "preflight", lambda *a, **k: {"status": "PASS", "evidence_locations": {}})


def test_launch_keyboard_interrupt_during_attachment_terminalizes_startup(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops
    _patch_launch_preflight(monkeypatch)
    db = str(tmp_path / "ki.db")
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: SimpleNamespace(pid=444, poll=lambda: None))
    monkeypatch.setattr(ops, "verify_worker_attachment", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        ops.launch_campaign(db, "rel", 3, ["BTCUSDT"], ["1h"], detach=True)
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    camp = conn.execute("SELECT campaign_id,campaign_status,worker_pid,last_error,active_run_id FROM burnin_campaigns").fetchone()
    assert camp["campaign_status"] == "FAILED"
    assert camp["worker_pid"] is None
    assert camp["last_error"] == "WORKER_ATTACHMENT_INTERRUPTED"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (camp["active_run_id"],)).fetchone()[0] == "FAILED"
    assert conn.execute("SELECT COUNT(*) FROM burnin_campaign_events WHERE event_type='PHASE9_CAMPAIGN_FAILED' AND details_json LIKE '%KeyboardInterrupt%' ").fetchone()[0] == 1


def test_launch_system_exit_during_attachment_terminalizes_startup(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops
    _patch_launch_preflight(monkeypatch)
    db = str(tmp_path / "se.db")
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: SimpleNamespace(pid=445, poll=lambda: None))
    monkeypatch.setattr(ops, "verify_worker_attachment", lambda *a, **k: (_ for _ in ()).throw(SystemExit(7)))
    with pytest.raises(SystemExit):
        ops.launch_campaign(db, "rel", 3, ["BTCUSDT"], ["1h"], detach=True)
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    camp = conn.execute("SELECT campaign_status,worker_pid,last_error,active_run_id FROM burnin_campaigns").fetchone()
    assert camp["campaign_status"] == "FAILED" and camp["worker_pid"] is None
    assert camp["last_error"] == "SYSTEM_EXIT_DURING_ATTACHMENT"
    assert conn.execute("SELECT status FROM burnin_campaign_runs WHERE burnin_run_id=?", (camp["active_run_id"],)).fetchone()[0] == "FAILED"


def test_worker_exit_during_attachment_is_detected_without_timeout(monkeypatch, tmp_path):
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='STARTING', worker_pid=999, worker_started_at=? WHERE campaign_id=?", (utc_now(), camp.campaign_id))
    conn.execute("UPDATE burnin_runs SET status='STARTING' WHERE burnin_run_id=?", (run,))
    conn.execute("UPDATE burnin_campaign_runs SET status='STARTING' WHERE burnin_run_id=?", (run,))
    conn.commit()
    import alphaforge.burnin_ops as ops
    monkeypatch.setattr(ops, "_pid_alive", lambda pid: False)
    out = verify_worker_attachment(conn, camp.campaign_id, worker_started_at=utc_now(), launch_started_at=utc_now(), timeout_seconds=60, process=SimpleNamespace(poll=lambda: 9))
    assert out["reason"] == "WORKER_EXITED_BEFORE_ATTACHMENT"
    row = conn.execute("SELECT campaign_status,worker_pid,last_error FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()
    assert row["campaign_status"] == "FAILED" and row["worker_pid"] is None


def test_failed_zero_exposure_startup_recovery_drill_safe_terminalization(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='FAILED', worker_pid=NULL, last_error='WORKER_ATTACHMENT_INTERRUPTED' WHERE campaign_id=?", (camp.campaign_id,))
    conn.execute("UPDATE burnin_runs SET status='FAILED', end_time=? WHERE burnin_run_id=?", (utc_now(), run))
    conn.execute("UPDATE burnin_campaign_runs SET status='FAILED', ended_at=? WHERE burnin_run_id=?", (utc_now(), run))
    conn.commit()
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *a, **k: {"blocked": False, "current_exposure_check": {"active_positions": 0, "pending_orders": 0, "orphan_orders": 0, "orphan_positions": 0}, "availability": {"active_positions_available": True, "pending_orders_available": True, "orphan_evidence_available": True, "kill_switch_available": True}, "kill_switch_active": False})
    out = recovery_drill(conn, camp.campaign_id)
    assert out["status"] == "PASS"
    assert out["checks"]["safe_terminalization"] is True
    assert conn.execute("SELECT COUNT(*) FROM burnin_campaign_events WHERE event_type='PHASE9_ZERO_EXPOSURE_STARTUP_FAILURE_TERMINALIZED'").fetchone()[0] == 1


def _terminal_provider_failure(conn, camp, run):
    conn.execute("UPDATE burnin_campaigns SET campaign_status='FAILED', worker_pid=NULL, last_error='EXCHANGE_RECONCILIATION_UNAVAILABLE' WHERE campaign_id=?", (camp.campaign_id,))
    conn.execute("UPDATE burnin_runs SET status='FAILED', end_time=? WHERE burnin_run_id=?", (utc_now(), run))
    conn.execute("UPDATE burnin_campaign_runs SET status='FAILED', ended_at=? WHERE burnin_run_id=?", (utc_now(), run))
    conn.commit()


def test_terminal_paper_provider_failure_with_zero_execution_is_terminalized_and_unblocks_future_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION", "false")
    import alphaforge.burnin_ops as ops
    from alphaforge.persistence import init_db
    from alphaforge.runtime_state import RuntimeStateSnapshot, evaluate_runtime_recovery, save_runtime_state_snapshot

    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    _terminal_provider_failure(conn, camp, run)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED' WHERE campaign_id=?", (camp.campaign_id,))
    conn.commit()
    engine = init_db(f"sqlite+pysqlite:///{db}")
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(
        mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="RECOVERY_REQUIRED",
        instance_id="failed-startup", startup_id="failed-startup", process_id=0,
        campaign_id=camp.campaign_id, burnin_run_id=run, release_id=camp.release_id,
        fail_closed_reason="EXCHANGE_RECONCILIATION_UNAVAILABLE", recovery_action_required=True,
    ))
    engine.dispose()
    monkeypatch.setattr(ops, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: pytest.fail("terminal recovery must not launch a worker"))

    # Reproduce the pre-#304 attempt: provider unavailability accompanied by a
    # local query error fails closed and stamps the campaign as recovery-owned.
    authoritative_recovery = ops._authoritative_recovery_exposure
    provider = "reconciliation_probe:RuntimeError:read_only_reconciliation_provider_unavailable"
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *a, **k: {
        "blocked": True, "reason": "RECOVERY_EVIDENCE_UNAVAILABLE", "scope": "SAME_CAMPAIGN",
        "latest": {"mode": "PAPER"}, "previous_process_alive": False, "kill_switch_active": False,
        "query_errors": [provider, "pending_orders:OperationalError:simulated"],
        "provider_unavailable_errors": [provider],
        "current_exposure_check": {name: 0 for name in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")},
        "availability": {"active_positions_available": True, "pending_orders_available": True, "orphan_evidence_available": True, "kill_switch_available": True},
    })
    old_attempt = recovery_drill(conn, camp.campaign_id)
    assert old_attempt["status"] == "FAIL"
    assert tuple(conn.execute("SELECT campaign_status,last_error FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()) == ("RECOVERY_REQUIRED", "RECOVERY_DRILL_PRECHECK_FAILED")

    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", authoritative_recovery)

    out = recovery_drill(conn, camp.campaign_id)

    assert out["status"] == "PASS"
    assert out["checks"]["provider_only_error"] is True
    assert out["checks"]["campaign_status"] == "RECOVERY_REQUIRED"
    assert out["checks"]["last_error"] == "RECOVERY_DRILL_PRECHECK_FAILED"
    assert out["checks"]["campaign_state_terminalizable"] is True
    assert out["checks"]["terminal_zero_startup_fallback_candidate"] is True
    assert out["checks"]["zero_exposure_failed_startup_terminalizable"] is True
    assert conn.execute("SELECT campaign_status FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()[0] == "FAILED"
    assert out["checks"]["startup_terminalization_evidence"] == {
        "decisions": 0, "executions": 0, "lifecycle_executions": 0,
        "run_mode": "PAPER", "run_status": "FAILED", "query_errors": [], "available": True,
    }
    audit = json.loads(conn.execute("SELECT details_json FROM burnin_campaign_events WHERE event_type='PHASE9_ZERO_EXPOSURE_STARTUP_FAILURE_TERMINALIZED' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert audit["runtime_recovery"]["fallback_original_runtime_recovery"]["scope"] == "SAME_CAMPAIGN"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "FAILED"
    assert out["after"]["resume"] is None and out["after"]["attach"] is None
    assert conn.execute("SELECT COUNT(*) FROM burnin_recovery_drills WHERE campaign_id=?", (camp.campaign_id,)).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM runtime_state_snapshots WHERE campaign_id=?", (camp.campaign_id,)).fetchone()[0] == 1
    engine = init_db(f"sqlite+pysqlite:///{db}")
    later = evaluate_runtime_recovery(engine, mode="PAPER", campaign_id="camp_unrelated_new")
    engine.dispose()
    assert later["blocked"] is False
    assert later["scope"] == "UNRELATED_HISTORICAL_RUNTIME"


@pytest.mark.parametrize("last_error", [None, "DEAD_WORKER_ZERO_EXPOSURE_RECOVERY_REQUIRED"])
def test_recovery_required_terminal_provider_failure_requires_recovery_drill_provenance(monkeypatch, tmp_path, last_error):
    import alphaforge.burnin_ops as ops

    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    _terminal_provider_failure(conn, camp, run)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',last_error=? WHERE campaign_id=?", (last_error, camp.campaign_id))
    conn.commit()
    provider = "reconciliation_probe:RuntimeError:read_only_reconciliation_provider_unavailable"
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *a, **k: {
        "blocked": True, "reason": "RECOVERY_EVIDENCE_UNAVAILABLE", "scope": "SAME_CAMPAIGN",
        "latest": {"mode": "PAPER"}, "previous_process_alive": False, "kill_switch_active": False,
        "query_errors": [provider], "provider_unavailable_errors": [provider],
        "current_exposure_check": {name: 0 for name in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")},
        "availability": {"active_positions_available": True, "pending_orders_available": True, "orphan_evidence_available": True, "kill_switch_available": True},
    })

    out = recovery_drill(conn, camp.campaign_id)

    assert out["status"] == "FAIL"
    assert out["checks"]["campaign_status"] == "RECOVERY_REQUIRED"
    assert out["checks"]["last_error"] == last_error
    assert out["checks"]["campaign_state_terminalizable"] is False
    assert out["checks"]["terminal_zero_startup_fallback_candidate"] is False


@pytest.mark.parametrize("blocker", [
    "campaign_position", "pending_reject", "worker_pid_dead", "worker_alive", "local_unavailable", "non_provider_error",
])
def test_recovery_required_terminal_provider_failure_requires_complete_zero_exposure_evidence(monkeypatch, tmp_path, blocker):
    import alphaforge.burnin_ops as ops

    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    _terminal_provider_failure(conn, camp, run)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',last_error='RECOVERY_DRILL_PRECHECK_FAILED' WHERE campaign_id=?", (camp.campaign_id,))
    if blocker in {"worker_pid_dead", "worker_alive"}:
        conn.execute("UPDATE burnin_campaigns SET worker_pid=999 WHERE campaign_id=?", (camp.campaign_id,))
    if blocker == "campaign_position":
        conn.execute("INSERT INTO burnin_pending_position_outcomes(pending_position_id,trade_id,campaign_id,burnin_run_id,signal_id,symbol,side,entry_time,source_provenance_json,status,missing_fields_json,created_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?,'OPEN','[]',?,?)", ("open", "trade", camp.campaign_id, run, "sig", "BTCUSDT", "LONG", utc_now(), "{}", utc_now(), "test"))
    if blocker == "pending_reject":
        conn.execute("INSERT INTO burnin_pending_reject_labels(pending_label_id,campaign_id,burnin_run_id,reject_decision_id,signal_id,symbol,side,decision_timestamp,entry,stop,target,horizon_seconds,execution_cost_assumptions_json,regime,reject_reason,source_provenance_json,due_at,status,created_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("reject", camp.campaign_id, run, "reject-decision", "sig", "BTCUSDT", "LONG", utc_now(), 1, .9, 1.2, 3600, "{}", "UNKNOWN", "LOW_CONFIDENCE", "{}", utc_now(), "PENDING", utc_now(), "test"))
    conn.commit()
    provider = "reconciliation_probe:RuntimeError:read_only_reconciliation_provider_unavailable"
    extra = "reconciliation:OperationalError:simulated" if blocker == "non_provider_error" else None
    errors = [provider] + ([extra] if extra else [])
    availability = {"active_positions_available": True, "pending_orders_available": True, "orphan_evidence_available": True, "kill_switch_available": True}
    if blocker == "local_unavailable":
        availability["pending_orders_available"] = False
    monkeypatch.setattr(ops, "_pid_alive", lambda _pid: blocker == "worker_alive")
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *a, **k: {
        "blocked": True, "reason": "RECOVERY_EVIDENCE_UNAVAILABLE", "scope": "SAME_CAMPAIGN",
        "latest": {"mode": "PAPER"}, "previous_process_alive": blocker == "worker_alive", "kill_switch_active": False,
        "query_errors": errors, "provider_unavailable_errors": [provider],
        "current_exposure_check": {name: 0 for name in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")},
        "availability": availability,
    })

    out = recovery_drill(conn, camp.campaign_id)

    assert out["status"] == "FAIL"
    assert out["checks"]["zero_exposure_failed_startup_terminalizable"] is False
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "FAILED"


@pytest.mark.parametrize("evidence_kind", ["decision", "execution", "lifecycle_execution"])
def test_terminal_paper_provider_failure_with_any_execution_evidence_remains_blocked(monkeypatch, tmp_path, evidence_kind):
    import alphaforge.burnin_ops as ops

    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    _terminal_provider_failure(conn, camp, run)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',last_error='RECOVERY_DRILL_PRECHECK_FAILED' WHERE campaign_id=?", (camp.campaign_id,))
    if evidence_kind in {"decision", "lifecycle_execution"}:
        lifecycle = "ORDER_PLACED" if evidence_kind == "lifecycle_execution" else "SIGNAL_REJECTED"
        persist_burnin_observation(
            conn, observation_id=f"obs-{evidence_kind}", burnin_run_id=run, release_id=camp.release_id,
            execution_mode="PAPER", observed_at=utc_now(), symbol="BTCUSDT", interval="1h",
            regime="UNKNOWN", decision="REJECT", lifecycle_state=lifecycle,
        )
    else:
        conn.execute("INSERT INTO burnin_pending_position_outcomes(pending_position_id,trade_id,campaign_id,burnin_run_id,signal_id,symbol,side,entry_time,source_provenance_json,status,missing_fields_json,created_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?,'CLOSED','[]',?,?)", ("pp", "trade", camp.campaign_id, run, "sig", "BTCUSDT", "LONG", utc_now(), "{}", utc_now(), "test"))
    conn.commit()
    provider = "reconciliation_probe:RuntimeError:read_only_reconciliation_provider_unavailable"
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *a, **k: {
        "blocked": True, "reason": "RECOVERY_EVIDENCE_UNAVAILABLE", "scope": "SAME_CAMPAIGN",
        "latest": {"mode": "PAPER"}, "previous_process_alive": False, "kill_switch_active": False,
        "query_errors": [provider], "provider_unavailable_errors": [provider],
        "current_exposure_check": {name: 0 for name in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")},
        "availability": {"active_positions_available": True, "pending_orders_available": True, "orphan_evidence_available": True, "kill_switch_available": True},
    })
    out = recovery_drill(conn, camp.campaign_id)
    assert out["status"] == "FAIL"
    assert out["checks"]["zero_exposure_failed_startup_terminalizable"] is False


@pytest.mark.parametrize("mode,exposure", [
    ("PAPER", "active_positions"), ("PAPER", "pending_orders"),
    ("PAPER", "orphan_orders"), ("PAPER", "orphan_positions"), ("LIVE", None), ("LIVE_PRECHECK", None),
])
def test_terminal_provider_failure_fallback_rejects_runtime_exposure_and_live(monkeypatch, tmp_path, mode, exposure):
    import alphaforge.burnin_ops as ops

    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    _terminal_provider_failure(conn, camp, run)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',last_error='RECOVERY_DRILL_PRECHECK_FAILED' WHERE campaign_id=?", (camp.campaign_id,))
    counts = {name: 0 for name in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")}
    if exposure:
        counts[exposure] = 1
    provider = "reconciliation_probe:RuntimeError:read_only_reconciliation_provider_unavailable"
    monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *a, **k: {
        "blocked": True, "reason": "RECOVERY_EVIDENCE_UNAVAILABLE", "scope": "SAME_CAMPAIGN",
        "latest": {"mode": mode}, "previous_process_alive": False, "kill_switch_active": False,
        "query_errors": [provider], "provider_unavailable_errors": [provider], "current_exposure_check": counts,
        "availability": {"active_positions_available": True, "pending_orders_available": True, "orphan_evidence_available": True, "kill_switch_available": True},
    })
    out = recovery_drill(conn, camp.campaign_id)
    assert out["status"] == "FAIL"
    assert out["checks"]["historical_zero_local_fallback"] is False


def test_terminal_provider_failure_sql_evidence_error_remains_blocked(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    _, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    _terminal_provider_failure(conn, camp, run)
    monkeypatch.setattr(ops, "_startup_terminalization_evidence", lambda *a, **k: {
        "decisions": 0, "executions": 0, "lifecycle_executions": 0, "run_mode": "PAPER",
        "run_status": "FAILED", "query_errors": ["decisions:OperationalError:simulated"], "available": False,
    })
    out = recovery_drill(conn, camp.campaign_id)
    assert out["status"] == "FAIL"
    assert out["checks"]["startup_terminalization_evidence"]["available"] is False


def test_cli_symbols_accept_comma_and_space_forms():
    import alphaforge.burnin_ops as ops
    assert ops._symbols("BTCUSDT,ETHUSDT") == ["BTCUSDT", "ETHUSDT"]
    assert ops._symbols(["BTCUSDT", "ETHUSDT"]) == ["BTCUSDT", "ETHUSDT"]


def test_launch_worker_runtime_error_terminalizes_starting_and_unblocks_preflight(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops

    real_preflight = ops.preflight
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    monkeypatch.setattr(ops, "preflight", lambda *a, **k: {"status": "PASS", "evidence_locations": {}})
    monkeypatch.setattr(ops, "_launch_worker", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PHASE8_CAMPAIGN_ACTIVE_RUN_MAPPING_INVALID")))
    db = str(tmp_path / "runtime_error.db")

    out = launch_campaign(db, "rel", 3, ["BTCUSDT"], ["1h"], detach=True)
    assert out["status"] == "FAILED"
    assert out["reason"] == "PHASE8_CAMPAIGN_ACTIVE_RUN_MAPPING_INVALID"
    assert out["stdout_log_path"].endswith("worker.stdout.log")
    assert out["stderr_log_path"].endswith("worker.stderr.log")
    assert out["worker_exit_code"] is None

    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    camp = conn.execute("SELECT campaign_id,campaign_status,worker_pid,last_error,active_run_id FROM burnin_campaigns").fetchone()
    assert camp["campaign_status"] == "FAILED"
    assert camp["worker_pid"] is None
    assert camp["last_error"] == "PHASE8_CAMPAIGN_ACTIVE_RUN_MAPPING_INVALID"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (camp["active_run_id"],)).fetchone()[0] == "FAILED"
    assert conn.execute("SELECT status FROM burnin_campaign_runs WHERE burnin_run_id=?", (camp["active_run_id"],)).fetchone()[0] == "FAILED"
    evt = conn.execute("SELECT details_json FROM burnin_campaign_events WHERE event_type='PHASE9_CAMPAIGN_FAILED' ORDER BY id DESC LIMIT 1").fetchone()
    details = json.loads(evt["details_json"])
    assert details["reason"] == "PHASE8_CAMPAIGN_ACTIVE_RUN_MAPPING_INVALID"
    assert details["stdout_log_path"].endswith("worker.stdout.log")
    assert details["stderr_log_path"].endswith("worker.stderr.log")

    monkeypatch.setattr(ops, "preflight", real_preflight)
    monkeypatch.setattr(ops, "_git_clean", lambda: True)
    monkeypatch.setattr(ops, "_git_commit", lambda: "commit")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "dev\n")
    monkeypatch.setattr(ops, "clock_skew_check", lambda: {"status": "PASS"})
    monkeypatch.setattr(ops, "_actual_runtime_identity", lambda release, symbols, intervals: {**ops._candidate_identity(release, symbols, intervals), "execution_mode": "PAPER"})
    pf = ops.preflight(db, "rel", ["BTCUSDT"], ["1h"], require_market_data=False)
    checks = {c["name"]: c for c in pf["checks"]}
    assert checks["no_duplicate_active_campaign"]["status"] == "PASS"
    assert checks["no_stale_worker_occupying_campaign"]["status"] == "PASS"


def test_health_uses_only_current_campaign_runtime_instance_heartbeat(monkeypatch, tmp_path):
    import alphaforge.burnin_ops as ops
    db, conn = _conn(tmp_path / "lineage_scoped")
    camp, run = _campaign(conn)
    event(conn, camp.campaign_id, "PHASE8_CAMPAIGN_ATTACHED", burnin_run_id=run,
          details={"runtime_instance_id": "runtime:target", "active_run_id": run})
    conn.execute("""CREATE TABLE runtime_heartbeats(
        id INTEGER PRIMARY KEY AUTOINCREMENT, runtime_instance_id TEXT NOT NULL,
        execution_mode TEXT NOT NULL, heartbeat_ts TEXT NOT NULL, scanner_source TEXT,
        runtime_state TEXT NOT NULL, last_scan_ts TEXT, last_decision_ts TEXT,
        active_positions_count INTEGER, pending_orders_count INTEGER,
        evidence_status TEXT NOT NULL, payload_json TEXT)""")
    stale = "2020-01-01T00:00:00+00:00"
    conn.execute("INSERT INTO runtime_heartbeats(runtime_instance_id,execution_mode,heartbeat_ts,runtime_state,last_scan_ts,last_decision_ts,evidence_status,payload_json) VALUES (?,?,?,?,?,?,?,?)",
                 ("runtime:target", "PAPER", stale, "OPERATING", stale, None, "MEASURED_RUNTIME_HEARTBEAT", json.dumps({"scans": 2, "symbols_selected": 3, "decisions_generated": 1, "rejects_persisted": 1})))
    conn.execute("INSERT INTO runtime_heartbeats(runtime_instance_id,execution_mode,heartbeat_ts,runtime_state,last_scan_ts,last_decision_ts,evidence_status,payload_json) VALUES (?,?,?,?,?,?,?,?)",
                 ("runtime:unrelated", "PAPER", utc_now(), "OPERATING", utc_now(), utc_now(), "MEASURED_RUNTIME_HEARTBEAT", json.dumps({"scans": 999, "symbols_selected": 999, "decisions_generated": 999, "rejects_persisted": 999})))
    conn.commit()
    monkeypatch.setattr(ops, "_pid_alive", lambda _pid: True)

    health = health_payload(conn, camp.campaign_id, max_heartbeat_age=120)

    assert health["status"] == "UNHEALTHY"
    assert "RUNTIME_HEARTBEAT_STALE" in health["unhealthy_reasons"]
    assert health["runtime_instance_id"] == "runtime:target"
    assert (health["scans"], health["symbols_selected"], health["decisions_generated"], health["rejects_persisted"]) == (2, 3, 1, 1)
    assert health["last_scan_ts"] == stale and health["last_decision_ts"] is None
    conn.close()


def test_health_never_falls_back_to_global_heartbeat_without_attachment(tmp_path):
    db, conn = _conn(tmp_path / "missing_lineage")
    camp, _run = _campaign(conn)
    conn.execute("""CREATE TABLE runtime_heartbeats(
        id INTEGER PRIMARY KEY AUTOINCREMENT, runtime_instance_id TEXT NOT NULL,
        execution_mode TEXT NOT NULL, heartbeat_ts TEXT NOT NULL, scanner_source TEXT,
        runtime_state TEXT NOT NULL, last_scan_ts TEXT, last_decision_ts TEXT,
        active_positions_count INTEGER, pending_orders_count INTEGER,
        evidence_status TEXT NOT NULL, payload_json TEXT)""")
    conn.execute("INSERT INTO runtime_heartbeats(runtime_instance_id,execution_mode,heartbeat_ts,runtime_state,last_scan_ts,last_decision_ts,evidence_status,payload_json) VALUES (?,?,?,?,?,?,?,?)",
                 ("runtime:unrelated", "PAPER", utc_now(), "OPERATING", utc_now(), utc_now(), "MEASURED_RUNTIME_HEARTBEAT", json.dumps({"scans": 999, "symbols_selected": 999, "decisions_generated": 999, "rejects_persisted": 999})))
    conn.commit()

    health = health_payload(conn, camp.campaign_id, max_heartbeat_age=999999)

    assert health["status"] == "UNHEALTHY"
    assert "RUNTIME_ATTACHMENT_IDENTITY_MISSING" in health["unhealthy_reasons"]
    assert health["runtime_status"] == "UNAVAILABLE" and health["runtime_instance_id"] is None
    assert health["runtime_heartbeat_age"] is None and health["last_scan_ts"] is None and health["last_decision_ts"] is None
    assert (health["scans"], health["symbols_selected"], health["decisions_generated"], health["rejects_persisted"]) == (0, 0, 0, 0)
    conn.close()


def _healthy_resolver_campaign(conn, camp, run):
    now = utc_now()
    conn.execute("""CREATE TABLE IF NOT EXISTS runtime_heartbeats(
        id INTEGER PRIMARY KEY AUTOINCREMENT,runtime_instance_id TEXT,execution_mode TEXT,
        heartbeat_ts TEXT,runtime_state TEXT,last_scan_ts TEXT,last_decision_ts TEXT,
        evidence_status TEXT,payload_json TEXT)""")
    conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED', last_heartbeat_at=? WHERE campaign_id=?", (now, camp.campaign_id))
    event(conn, camp.campaign_id, "PHASE8_CAMPAIGN_ATTACHED", burnin_run_id=run,
          details={"runtime_instance_id": "runtime:health", "active_run_id": run})
    conn.execute("INSERT INTO runtime_heartbeats(runtime_instance_id,execution_mode,heartbeat_ts,runtime_state,last_scan_ts,evidence_status,payload_json) VALUES (?,?,?,?,?,?,?)",
                 ("runtime:health", "PAPER", now, "OPERATING", now, "MEASURED_RUNTIME_HEARTBEAT", "{}"))
    conn.commit()


def _health_label(conn, camp, run, suffix, *, due_at, status="PENDING", claimed_at=None):
    conn.execute("""INSERT INTO burnin_pending_reject_labels(
        pending_label_id,campaign_id,burnin_run_id,reject_decision_id,signal_id,symbol,side,
        decision_timestamp,entry,stop,target,horizon_seconds,execution_cost_assumptions_json,
        regime,reject_reason,source_provenance_json,due_at,status,claimed_at,created_at,schema_version)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"p-{suffix}", camp.campaign_id, run, f"r-{suffix}", f"s-{suffix}", "BTCUSDT", "LONG",
         utc_now(), 1, .9, 1.2, 3600, "{}", "TREND", "LOW", "{}", due_at, status,
         claimed_at, utc_now(), "sv"))
    conn.commit()


def test_health_immature_pending_growth_stays_healthy(tmp_path):
    from datetime import datetime, timedelta, timezone
    _, conn = _conn(tmp_path); camp, run = _campaign(conn); _healthy_resolver_campaign(conn, camp, run)
    assert health_payload(conn, camp.campaign_id)["status"] == "HEALTHY"
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _health_label(conn, camp, run, "immature", due_at=future)
    health = health_payload(conn, camp.campaign_id)
    assert health["status"] == "HEALTHY"
    assert "RESOLVER_BACKLOG_GROWTH" not in health["unhealthy_reasons"]


def test_health_overdue_growth_and_stale_claim_are_unhealthy(tmp_path):
    from datetime import datetime, timedelta, timezone
    _, conn = _conn(tmp_path); camp, run = _campaign(conn); _healthy_resolver_campaign(conn, camp, run)
    health_payload(conn, camp.campaign_id)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _health_label(conn, camp, run, "overdue", due_at=past)
    assert "RESOLVER_BACKLOG_GROWTH" in health_payload(conn, camp.campaign_id)["unhealthy_reasons"]
    _health_label(conn, camp, run, "stale", due_at=past, status="RESOLVING", claimed_at=past)
    assert "STALE_RESOLVING_CLAIMS" in health_payload(conn, camp.campaign_id, max_heartbeat_age=60)["unhealthy_reasons"]


def test_health_resolver_and_provider_failures_remain_unhealthy(tmp_path):
    _, conn = _conn(tmp_path); camp, run = _campaign(conn); _healthy_resolver_campaign(conn, camp, run)
    for index in range(3):
        event(conn, camp.campaign_id, "RESOLVER_BATCH_FAILED", details={"error": "PROVIDER_FAILURE", "n": index})
    conn.commit()
    reasons = health_payload(conn, camp.campaign_id)["unhealthy_reasons"]
    assert "RESOLVER_FAILURES" in reasons
    assert "REPEATED_PROVIDER_FAILURES" in reasons
