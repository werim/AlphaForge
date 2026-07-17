from __future__ import annotations

import json, sqlite3, subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from alphaforge.burnin import config_hash, persist_burnin_observation, persist_burnin_reject_outcome, persist_burnin_trade_outcome, utc_now
from alphaforge.burnin_campaign import build_phase8_campaign_identity, create_campaign, event, start_or_resume_campaign, update_campaign_heartbeat, aggregate_campaign, get_campaign, fail_active_campaign_run
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
)


def _conn(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "ops.db"
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
    monkeypatch.setattr(ops, "_git_clean", lambda: True)
    monkeypatch.setattr(ops, "_git_commit", lambda: "commit")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "dev\n")
    monkeypatch.setattr(ops, "clock_skew_check", lambda: {"status": "PASS"})
    monkeypatch.setattr(ops, "_actual_runtime_identity", lambda release, symbols, intervals: {**ops._candidate_identity(release, symbols, intervals), "execution_mode": "PAPER"})

    out = ops.preflight(str(tmp_path / "pf.db"), "rel", ["BTCUSDT"], ["1h"], require_market_data=False)
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
    assert conn.execute("SELECT status,end_time FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()["status"] == "FAILED"
    assert conn.execute("SELECT status,ended_at FROM burnin_campaign_runs WHERE burnin_run_id=?", (run,)).fetchone()["status"] == "FAILED"
    row = conn.execute("SELECT campaign_status,worker_pid,worker_started_at FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()
    assert row["campaign_status"] == "FAILED" and row["worker_pid"] is None and row["worker_started_at"] is None


def test_pause_is_operator_activity_not_runtime_heartbeat_and_terminalizes_run(tmp_path):
    from alphaforge.burnin_campaign import pause_campaign
    db, conn = _conn(tmp_path)
    camp, run = _campaign(conn)
    heartbeat = "2026-01-01T00:00:00+00:00"
    conn.execute("UPDATE burnin_campaigns SET last_heartbeat_at=?,worker_pid=42 WHERE campaign_id=?", (heartbeat, camp.campaign_id))
    pause_campaign(conn, camp.campaign_id); conn.commit()
    row = conn.execute("SELECT last_heartbeat_at,last_operator_activity_at,worker_pid,campaign_status FROM burnin_campaigns WHERE campaign_id=?", (camp.campaign_id,)).fetchone()
    assert row["last_heartbeat_at"] == heartbeat and row["last_operator_activity_at"] and row["worker_pid"] is None and row["campaign_status"] == "PAUSED"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()["status"] == "PAUSED"
    assert conn.execute("SELECT status FROM burnin_campaign_runs WHERE burnin_run_id=?", (run,)).fetchone()["status"] == "PAUSED"
