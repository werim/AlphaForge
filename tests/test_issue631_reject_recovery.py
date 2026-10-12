"""Restarted reject workers and safety gates must agree about claim lifecycle."""
import json

import pytest
from sqlalchemy import create_engine

from alphaforge.burnin_campaign import BurnInCampaignRunner, check_campaign_completion
from alphaforge.burnin_ops import _recovery_campaign_exposure
from alphaforge.burnin_resolver import resolve_pending_rejects
from test_phase8_reject_resolver import COSTS, persist_pending_reject_label, setup


def pending(conn, camp, run, *, reject_id="claimed", symbol="BTCUSDT"):
    persist_pending_reject_label(
        conn, campaign_id=camp.campaign_id, burnin_run_id=run["burnin_run_id"],
        reject_decision_id=reject_id, signal_id=reject_id, symbol=symbol, side="LONG",
        decision_timestamp="2026-01-01T00:00:00Z", timeframe="1m", horizon_bars=2,
        entry=100, stop=90, target=120, execution_cost_assumptions=COSTS,
        regime="TRENDING", reject_reason="LOW_CONFIDENCE",
        source_provenance={"provider": "PAPER"},
    )


def candles(*_):
    return [{"timestamp": "2026-01-01T00:01:00Z", "high": 121, "low": 99},
            {"timestamp": "2026-01-01T00:02:00Z", "high": 122, "low": 100}]


@pytest.mark.parametrize("claim", ["2025-12-31T23:00:00Z", "2026-01-01T00:09:00Z", None])
def test_worker_fetches_only_reclaimable_claims_and_preserves_recovery(tmp_path, monkeypatch, claim):
    conn, camp, run = setup(tmp_path)
    pending(conn, camp, run)
    conn.execute("UPDATE burnin_pending_reject_labels SET status='RESOLVING',claim_token='old',claimed_at=?", (claim,))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED' WHERE campaign_id=?", (camp.campaign_id,))
    conn.commit()
    monkeypatch.setattr("alphaforge.burnin_campaign.utc_now", lambda: "2026-01-01T00:10:00Z")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'a.db'}")
    calls = []
    def provider(*args):
        calls.append(args)
        return candles()
    try:
        runner = BurnInCampaignRunner(engine, camp.campaign_id, provider)
        monkeypatch.setattr(runner, "_qualify_if_due", lambda: None)
        first = runner.resolver_tick()
        reclaimable = claim == "2025-12-31T23:00:00Z"
        assert first["status"] == "OK"
        assert first["resolver_counts"]["resolved"] == int(reclaimable)
        assert len(calls) == int(reclaimable)
        if reclaimable:
            assert calls[0] == ("BTCUSDT", "2026-01-01T00:00:00Z", "2026-01-01T00:02:00Z", "1m")
            outcome = conn.execute("SELECT payload_json FROM burnin_reject_outcomes").fetchone()[0]
            assert json.loads(outcome)["campaign_id"] == camp.campaign_id
            # A new runner simulates restart; immutable evidence is not replayed.
            restarted = BurnInCampaignRunner(engine, camp.campaign_id, provider)
            monkeypatch.setattr(restarted, "_qualify_if_due", lambda: None)
            assert restarted.resolver_tick()["resolver_counts"]["resolved"] == 0
            assert conn.execute("SELECT payload_json FROM burnin_reject_outcomes").fetchone()[0] == outcome
        else:
            assert conn.execute("SELECT status,claim_token,claimed_at FROM burnin_pending_reject_labels").fetchone()[:] == ("RESOLVING", "old", claim)
        assert conn.execute("SELECT campaign_status,worker_pid FROM burnin_campaigns").fetchone()[:] == ("RECOVERY_REQUIRED", None)
    finally:
        engine.dispose()
        conn.close()


@pytest.mark.parametrize("status,complete", [("RESOLVING", 0), ("UNKNOWN", 0), ("FAILED", 0),
                                           ("AMBIGUOUS", 0), ("RESOLVED", 0), ("RESOLVED", 1)])
def test_recovery_and_completion_keep_unresolved_labels_as_blockers(tmp_path, status, complete):
    conn, camp, run = setup(tmp_path)
    pending(conn, camp, run)
    conn.execute("UPDATE burnin_pending_reject_labels SET status=?,evidence_complete=?", (status, complete))
    expected = int(status != "RESOLVED" or complete != 1)
    assert _recovery_campaign_exposure(conn, camp.campaign_id)["pending_reject_labels"] == expected
    completion = check_campaign_completion(conn, camp.campaign_id)
    assert ("PENDING_OUTCOME_BACKLOG" in completion["blockers"]) == bool(expected)
    conn.close()


def test_legacy_resolver_can_reclaim_an_expired_claim(tmp_path):
    conn, camp, run = setup(tmp_path)
    pending(conn, camp, run)
    conn.execute("UPDATE burnin_pending_reject_labels SET status='RESOLVING',claimed_at='2025-12-31T23:00:00Z'")
    assert resolve_pending_rejects(conn, {"BTCUSDT": candles()}, now="2026-01-01T00:10:00Z")["resolved"] == 1
    conn.close()


@pytest.mark.parametrize("data", [[], [{"timestamp": "2026-01-01T00:02:00Z", "high": 101, "low": 99}]])
def test_restarted_worker_does_not_finalize_missing_or_gapped_data(tmp_path, monkeypatch, data):
    conn, camp, run = setup(tmp_path)
    pending(conn, camp, run)
    conn.execute("UPDATE burnin_pending_reject_labels SET status='RESOLVING',claimed_at='2025-12-31T23:00:00Z'")
    conn.commit()
    monkeypatch.setattr("alphaforge.burnin_campaign.utc_now", lambda: "2026-01-01T00:10:00Z")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'a.db'}")
    try:
        runner = BurnInCampaignRunner(engine, camp.campaign_id, lambda *_: data)
        monkeypatch.setattr(runner, "_qualify_if_due", lambda: None)
        result = runner.resolver_tick()
        assert result["resolver_counts"]["pending"] == 1
        assert conn.execute("SELECT status,evidence_complete FROM burnin_pending_reject_labels").fetchone()[:] == ("PENDING", 0)
        assert conn.execute("SELECT COUNT(*) FROM burnin_reject_outcomes").fetchone()[0] == 0
    finally:
        engine.dispose()
        conn.close()


def test_incomplete_resolved_label_still_protects_compaction_window(tmp_path):
    from test_issue622_stats_only_retention import build_db, CID, RUN
    from alphaforge.storage_policy import plan_stats_only_universe, readonly
    engine, policy, _ = build_db(tmp_path, cycles=3, candidates=20)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("""INSERT INTO burnin_pending_reject_labels(
                pending_label_id,campaign_id,burnin_run_id,reject_decision_id,symbol,side,
                decision_timestamp,execution_cost_assumptions_json,source_provenance_json,
                due_at,status,evidence_complete,created_at,schema_version
            ) VALUES ('incomplete',?,?,'reject','S0000USDT','LONG',
                '2020-09-13T12:26:40Z','{}','{}','2020-09-13T12:27:40Z',
                'RESOLVED',0,'2020-09-13T12:26:40Z','v1')""", (CID, RUN))
        with readonly(tmp_path / 'stats-only.db') as conn:
            plan = plan_stats_only_universe(conn, policy)
            assert plan["cycles"] == []
            assert "UNRESOLVED_OUTCOME_WINDOW_PROTECTED" in plan["blockers"]
    finally:
        engine.dispose()


def storage_recovery(tmp_path):
    import sqlite3
    from test_issue622_stats_only_retention import build_db, CID, RUN
    from alphaforge.burnin_campaign import event
    engine, policy, _ = build_db(tmp_path, cycles=3, candidates=20)
    conn = sqlite3.connect(tmp_path / 'stats-only.db')
    conn.row_factory = sqlite3.Row
    report = {"blocked": True, "status": "STORAGE_PRESSURE_RECOVERY_REQUIRED"}
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED',last_error='STORAGE_PRESSURE_RECOVERY_REQUIRED',worker_pid=NULL")
    conn.execute("UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED'")
    conn.execute("UPDATE burnin_runs SET status='RECOVERY_REQUIRED'")
    conn.execute("""UPDATE runtime_state_snapshots SET runtime_status='RECOVERY_REQUIRED',
        release_id='issue622-stats',process_id=999999,recovery_action_required=1,
        fail_closed_reason='STORAGE_PRESSURE_RECOVERY_REQUIRED',
        timestamp='2020-01-01T00:00:00Z',diagnostics_json=?""", (json.dumps({"storage_policy": report}),))
    snapshot = dict(conn.execute("SELECT * FROM runtime_state_snapshots ORDER BY id DESC LIMIT 1").fetchone())
    event(conn, CID, "STORAGE_PRESSURE_RECOVERY_REQUIRED", burnin_run_id=RUN,
          details={"storage_report": {**report, "recovery_state_persisted": True},
                   "recovery_snapshot_id": snapshot["id"],
                   "runtime_instance_id": snapshot["instance_id"], "startup_id": snapshot["startup_id"]})
    conn.commit()
    return engine, policy, conn, CID, RUN


def authenticated_probe():
    from alphaforge.burnin import utc_now
    return {"authenticated": True, "input_source": "AUTHENTICATED_EXCHANGE_SNAPSHOT",
            "retrieved_at": utc_now(), "evidence_status": "COMPLETE",
            "orders": [], "positions": [], "errors": []}


@pytest.mark.parametrize("fresh_stop", [False, True])
def test_controlled_storage_stop_can_terminalize_without_a_watchdog_event(tmp_path, monkeypatch, fresh_stop):
    import alphaforge.burnin_ops as ops
    from alphaforge.runtime_state import evaluate_runtime_recovery
    from alphaforge.storage_policy import compact_stats_only_universe_batch, stats_only_campaign_summary
    engine, policy, conn, cid, run = storage_recovery(tmp_path)
    try:
        if fresh_stop:
            from alphaforge.burnin import utc_now
            conn.execute("UPDATE runtime_state_snapshots SET timestamp=?", (utc_now(),))
            conn.commit()
        before = ops._campaign_source_evidence_hash(conn, cid)
        monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *_:
                            evaluate_runtime_recovery(engine, mode="PAPER", campaign_id=cid,
                                                      reconciliation_probe=authenticated_probe))
        result = ops.terminalize_zero_exposure_recovery(conn, cid)
        assert result["status"] == "PASS", result
        assert result["terminal_status"] == "FAILED"
        assert ops._campaign_source_evidence_hash(conn, cid) == before
        assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "FAILED"
        assert conn.execute("SELECT COUNT(*) FROM burnin_campaign_runs").fetchone()[0] == 1
        assert conn.execute("SELECT campaign_status,worker_pid FROM burnin_campaigns").fetchone()[:] == ("FAILED", None)
        assert ops.terminalize_zero_exposure_recovery(conn, cid)["idempotent_replay"] is True
        assert compact_stats_only_universe_batch(engine, policy)["cycles_compacted"] == 2
        assert stats_only_campaign_summary(conn, cid)["qualification_eligible"] is False
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
        engine.dispose()


@pytest.mark.parametrize("fault", ["cross_run", "cross_release", "wrong_instance", "missing_report",
                                  "unknown_exposure", "missing_snapshot", "live_process", "provider_unavailable",
                                  "stale_probe", "future_probe", "missing_probe_time"])
def test_storage_terminalization_rejects_incomplete_identity_or_current_safety(tmp_path, monkeypatch, fault):
    import alphaforge.burnin_ops as ops
    from alphaforge.runtime_state import evaluate_runtime_recovery
    engine, _, conn, cid, _ = storage_recovery(tmp_path)
    try:
        if fault == "cross_run":
            conn.execute("UPDATE runtime_state_snapshots SET burnin_run_id='other'")
        elif fault == "cross_release":
            conn.execute("UPDATE runtime_state_snapshots SET release_id='other'")
        elif fault == "wrong_instance":
            conn.execute("UPDATE runtime_state_snapshots SET instance_id='other'")
        elif fault == "missing_report":
            conn.execute("UPDATE runtime_state_snapshots SET diagnostics_json='{}'")
        elif fault == "unknown_exposure":
            conn.execute("UPDATE runtime_state_snapshots SET unknown_exchange_state=1")
        elif fault == "missing_snapshot":
            conn.execute("DELETE FROM runtime_state_snapshots")
        elif fault == "live_process":
            monkeypatch.setattr(ops, "_pid_alive", lambda *_: True)
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM runtime_state_snapshots").fetchone()[0]
        def unavailable():
            raise RuntimeError("read_only_reconciliation_provider_unavailable")
        def probe():
            if fault == "provider_unavailable":
                return unavailable()
            evidence = authenticated_probe()
            if fault == "stale_probe":
                evidence["retrieved_at"] = "2020-01-01T00:00:00Z"
            elif fault == "future_probe":
                evidence["retrieved_at"] = "2099-01-01T00:00:00Z"
            elif fault == "missing_probe_time":
                evidence.pop("retrieved_at")
            return evidence
        monkeypatch.setattr(ops, "_authoritative_recovery_exposure", lambda *_:
                            evaluate_runtime_recovery(engine, mode="PAPER", campaign_id=cid,
                                                      reconciliation_probe=probe))
        result = ops.terminalize_zero_exposure_recovery(conn, cid)
        assert result["status"] == "FAIL_CLOSED", result
        assert conn.execute("SELECT campaign_status FROM burnin_campaigns").fetchone()[0] == "RECOVERY_REQUIRED"
        assert conn.execute("SELECT COUNT(*) FROM runtime_state_snapshots").fetchone()[0] == count
    finally:
        conn.close()
        engine.dispose()
