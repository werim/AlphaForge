from __future__ import annotations

import json
import sqlite3

from sqlalchemy import create_engine, text

from alphaforge.burnin import BurnInRun, persist_burnin_observation, persist_burnin_reject_outcome, persist_burnin_run
from alphaforge.burnin_campaign import aggregate_campaign, BurnInCampaignRunner, create_campaign, start_or_resume_campaign
from alphaforge.burnin_qualification import BurnInQualificationEngine, BurnInThresholds
from alphaforge.burnin_ops import bootstrap_ops_schema, finalize, health_payload
from alphaforge.burnin_resolver import persist_pending_reject_label
from alphaforge.dashboard.queries import fetch_phase8_campaign


COSTS = {"spread_cost": .01, "entry_slippage_cost": .01, "exit_slippage_cost": .01,
         "fee_cost": .01, "funding_cost": 0.0, "latency_cost": 0.0,
         "execution_cost_unit": "R"}


def _campaign(tmp_path):
    db = tmp_path / "evidence.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    bootstrap_ops_schema(conn)
    campaign = create_campaign(conn, release_id="rel", duration_days=1,
                               symbols=["BTCUSDT"], intervals=["1m"])
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED', observed_duration_seconds=60 WHERE campaign_id=?", (campaign.campaign_id,))
    conn.execute("UPDATE burnin_campaign_runs SET status='PAUSED', ended_at=started_at WHERE burnin_run_id=?", (run["burnin_run_id"],))
    conn.execute("UPDATE burnin_runs SET status='PAUSED', observed_duration_seconds=60, end_time=start_time WHERE burnin_run_id=?", (run["burnin_run_id"],))
    return db, conn, campaign.campaign_id, run["burnin_run_id"]


def _canonical_reject(conn, run_id, reject_id, setup_identity):
    persist_burnin_observation(
        conn, observation_id=f"obs-{setup_identity}", burnin_run_id=run_id,
        release_id="rel", execution_mode="PAPER", decision="REJECTED",
        metrics={"reject_decision_id": reject_id, "signal_id": reject_id,
                 "setup_identity": setup_identity},
    )


def _canonical_accept(conn, run_id, signal_id):
    persist_burnin_observation(
        conn, observation_id=f"obs-{signal_id}", burnin_run_id=run_id,
        release_id="rel", execution_mode="PAPER", decision="ACCEPTED",
        metrics={"signal_id": signal_id, "setup_identity": f"setup:{signal_id}"},
    )


def _outcome(conn, run_id, reject_id, *, net=-1.0, subject="GUIDED_CANDIDATE"):
    persist_burnin_reject_outcome(
        conn, reject_outcome_id=f"rout_{reject_id}", burnin_run_id=run_id,
        release_id="rel", reject_reason="MTF_EXECUTION_NOT_CONFIRMED",
        symbol="BTCUSDT", regime="TRENDING", forward_label="SL_BEFORE_TP" if net <= 0 else "TP_BEFORE_SL",
        hypothetical_net_r_after_costs=net, avoided_loss=max(0, -net),
        missed_profit=max(0, net), evidence_complete=True,
        payload={"reject_decision_id": reject_id, "forward_label_subject": subject,
                 "reject_quality_attributable": subject == "GUIDED_CANDIDATE"},
    )


def test_identity_aware_zero_rejects_fail_closed_for_orphan_outcomes(tmp_path):
    db, conn, campaign_id, run_id = _campaign(tmp_path)
    _canonical_accept(conn, run_id, "accepted-1")
    baseline = aggregate_campaign(conn, campaign_id)
    _outcome(conn, run_id, "orphan-guided-1")
    _outcome(conn, run_id, "orphan-guided-2", net=2)
    after_orphans = aggregate_campaign(conn, campaign_id)
    conn.commit(); conn.close()

    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    thresholds = BurnInThresholds(
        minimum_duration_seconds=0, minimum_total_decisions=0,
        minimum_accepted_trades=0, minimum_closed_trades=0,
        minimum_rejected_forward_outcomes=1, minimum_regime_coverage=0,
        minimum_calibration_sample=0, require_operator_ack=False,
        require_phase1_6_gates=False,
    )
    snapshot = BurnInQualificationEngine(engine, thresholds).evaluate(run_id)
    engine.dispose()

    assert snapshot.metrics["qualification_reject_identity_mode"] == "CANONICAL_LINK_REQUIRED"
    assert snapshot.metrics["diagnostic_completed_rejected_forward_outcomes"] == 2
    assert snapshot.metrics["completed_rejected_forward_outcomes"] == 0
    assert snapshot.metrics["orphan_rejected_forward_outcomes"] == 2
    assert any(b.startswith("MINIMUM_REJECTED_FORWARD_OUTCOMES:") for b in snapshot.blockers)
    assert after_orphans["metrics"]["completed_rejected_forward_outcomes"] == 0
    assert after_orphans["evidence_hash"] == baseline["evidence_hash"]


def test_canonical_pending_and_ambiguous_counts_ignore_orphan_outcomes(tmp_path):
    db, conn, _campaign_id, run_id = _campaign(tmp_path)
    for index in range(13):
        reject_id = f"canonical-{index}"
        _canonical_reject(conn, run_id, reject_id, f"setup:BTCUSDT:15m:{index}")
        if index < 5:
            _outcome(conn, run_id, reject_id)
    for index in range(20):
        _outcome(conn, run_id, f"orphan-{index}")
    conn.execute("UPDATE burnin_reject_outcomes SET forward_label='AMBIGUOUS', ambiguous=1 WHERE reject_outcome_id IN ('rout_canonical-0','rout_orphan-0')")
    conn.commit(); conn.close()

    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    snapshot = BurnInQualificationEngine(engine, BurnInThresholds(
        minimum_duration_seconds=0, minimum_total_decisions=0,
        minimum_accepted_trades=0, minimum_closed_trades=0,
        minimum_rejected_forward_outcomes=0, minimum_regime_coverage=0,
        minimum_calibration_sample=0, require_operator_ack=False,
        require_phase1_6_gates=False,
    )).evaluate(run_id)
    engine.dispose()

    assert snapshot.metrics["diagnostic_completed_rejected_forward_outcomes"] == 25
    assert snapshot.metrics["identity_linked_rejected_forward_outcomes"] == 5
    assert snapshot.metrics["pending_rejected_forward_outcomes"] == 8
    assert snapshot.metrics["ambiguous_rejected_forward_outcomes"] == 1
    assert snapshot.metrics["diagnostic_ambiguous_rejected_forward_outcomes"] == 2


def test_phase7_counts_only_canonical_guided_reject_identity(tmp_path):
    db, conn, _campaign_id, run_id = _campaign(tmp_path)
    _canonical_reject(conn, run_id, "canonical-1", "setup:BTCUSDT:15m:1")
    _outcome(conn, run_id, "canonical-1", net=-1)
    _outcome(conn, run_id, "scan-orphan-1", net=100)
    _outcome(conn, run_id, "scan-orphan-2", net=-100)
    conn.commit(); conn.close()

    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    thresholds = BurnInThresholds(
        minimum_duration_seconds=0, minimum_total_decisions=1,
        minimum_accepted_trades=0, minimum_closed_trades=0,
        minimum_rejected_forward_outcomes=1, minimum_regime_coverage=0,
        minimum_calibration_sample=0, require_operator_ack=False,
        require_phase1_6_gates=False,
    )
    snapshot = BurnInQualificationEngine(engine, thresholds).evaluate(run_id)
    engine.dispose()

    assert snapshot.metrics["diagnostic_completed_rejected_forward_outcomes"] == 3
    assert snapshot.metrics["completed_rejected_forward_outcomes"] == 1
    assert snapshot.metrics["orphan_rejected_forward_outcomes"] == 2
    assert snapshot.metrics["reject_precision"] == 1.0
    assert snapshot.metrics["false_reject_rate"] == 0.0
    assert snapshot.metrics["net_reject_value"] == 1.0


def test_new_setup_adds_sample_but_diagnostic_scans_do_not_change_hash(tmp_path):
    db, conn, campaign_id, run_id = _campaign(tmp_path)
    _canonical_reject(conn, run_id, "canonical-1", "setup:BTCUSDT:15m:1")
    _outcome(conn, run_id, "canonical-1")
    first = aggregate_campaign(conn, campaign_id)

    for reject_id in ("scan-1", "scan-2"):
        persist_pending_reject_label(
            conn, campaign_id=campaign_id, burnin_run_id=run_id,
            reject_decision_id=reject_id, signal_id=reject_id, symbol="BTCUSDT",
            side="LONG", decision_timestamp="2026-01-01T00:00:00Z",
            timeframe="1m", horizon_bars=1, entry=100, stop=99, target=102,
            execution_cost_assumptions=COSTS, regime="TRENDING",
            reject_reason="MTF_EXECUTION_NOT_CONFIRMED",
            source_provenance={"forward_label_subject": "GUIDED_CANDIDATE"},
        )
        _outcome(conn, run_id, reject_id)
    diagnostic = aggregate_campaign(conn, campaign_id)
    assert diagnostic["metrics"]["diagnostic_completed_rejected_forward_outcomes"] == 3
    assert diagnostic["metrics"]["completed_rejected_forward_outcomes"] == 1
    assert diagnostic["evidence_hash"] == first["evidence_hash"]

    _canonical_reject(conn, run_id, "canonical-2", "setup:BTCUSDT:15m:2")
    _outcome(conn, run_id, "canonical-2")
    second = aggregate_campaign(conn, campaign_id)
    assert second["metrics"]["sample_count"] == 2
    assert second["metrics"]["completed_rejected_forward_outcomes"] == 2
    assert second["evidence_hash"] != first["evidence_hash"]
    conn.commit()

    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    dashboard = fetch_phase8_campaign(engine, campaign_id)
    engine.dispose()
    assert (dashboard["decisions"], dashboard["accepted"], dashboard["rejected"]) == (2, 0, 2)


def test_shadow_with_canonical_identity_remains_non_attributable(tmp_path):
    db, conn, _campaign_id, run_id = _campaign(tmp_path)
    _canonical_reject(conn, run_id, "shadow", "setup:BTCUSDT:15m:1")
    _outcome(conn, run_id, "shadow", net=-50, subject="LEGACY_SCANNER_SHADOW_CANDIDATE")
    _canonical_reject(conn, run_id, "guided", "setup:BTCUSDT:15m:2")
    _outcome(conn, run_id, "guided", net=2, subject="GUIDED_CANDIDATE")
    conn.commit(); conn.close()

    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    thresholds = BurnInThresholds(minimum_duration_seconds=0, minimum_total_decisions=0,
        minimum_accepted_trades=0, minimum_closed_trades=0,
        minimum_rejected_forward_outcomes=0, minimum_regime_coverage=0,
        minimum_calibration_sample=0, require_operator_ack=False,
        require_phase1_6_gates=False)
    snapshot = BurnInQualificationEngine(engine, thresholds).evaluate(run_id)
    engine.dispose()
    assert snapshot.metrics["completed_rejected_forward_outcomes"] == 1
    assert snapshot.metrics["false_reject_rate"] == 1.0
    assert snapshot.metrics["net_reject_value"] == -2.0


def test_health_separates_stale_snapshot_blockers_from_current_blockers(tmp_path):
    _db, conn, campaign_id, run_id = _campaign(tmp_path)
    _canonical_reject(conn, run_id, "canonical-1", "setup:BTCUSDT:15m:1")
    conn.execute("""INSERT INTO burnin_qualification_snapshots(
        qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,
        expectancy_status,execution_status,regime_status,reject_quality_status,
        calibration_status,drawdown_status,concentration_status,reconciliation_status,
        evidence_completeness_status,blockers_json,warnings_json,thresholds_json,
        metrics_json,evidence_hash,schema_version,campaign_id,source_run_ids_json,
        aggregate_evidence_hash) VALUES(
        'stale',?,'rel','2026-01-01T00:00:00Z','BURN_IN_INSUFFICIENT','INSUFFICIENT',
        'FAIL','FAIL','FAIL','FAIL','FAIL','PASS','FAIL','FAIL','PASS',
        '["MINIMUM_TOTAL_DECISIONS:0<500"]','[]','{}','{}','old','v',?,?,'old')""",
        (run_id, campaign_id, json.dumps([run_id])))
    conn.execute("UPDATE burnin_campaigns SET latest_qualification_id='stale' WHERE campaign_id=?", (campaign_id,))
    conn.commit()

    health = health_payload(conn, campaign_id, max_heartbeat_age=10**9)
    assert health["qualification_snapshot_stale"] is True
    assert health["latest_blockers"] == []
    assert health["stale_qualification_blockers"] == ["MINIMUM_TOTAL_DECISIONS:0<500"]
    assert health["total_decisions"] == 1


def test_bounded_cadence_refreshes_changed_canonical_evidence(tmp_path):
    db, conn, campaign_id, run_id = _campaign(tmp_path)
    old_hash = aggregate_campaign(conn, campaign_id)["evidence_hash"]
    conn.execute("""INSERT INTO burnin_qualification_snapshots(
        qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,
        expectancy_status,execution_status,regime_status,reject_quality_status,
        calibration_status,drawdown_status,concentration_status,reconciliation_status,
        evidence_completeness_status,blockers_json,warnings_json,thresholds_json,
        metrics_json,evidence_hash,schema_version,campaign_id,source_run_ids_json,
        aggregate_evidence_hash) VALUES(
        'prior',?,'rel','2026-01-01T00:00:00Z','BURN_IN_INSUFFICIENT','INSUFFICIENT',
        'FAIL','FAIL','FAIL','FAIL','FAIL','PASS','FAIL','FAIL','PASS',
        '[]','[]','{}','{}','old','v',?,? ,?)""",
        (run_id, campaign_id, json.dumps([run_id]), old_hash))
    conn.execute("UPDATE burnin_campaigns SET latest_qualification_id='prior' WHERE campaign_id=?", (campaign_id,))
    _canonical_reject(conn, run_id, "new", "setup:BTCUSDT:15m:new")
    conn.commit(); conn.close()

    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    runner = BurnInCampaignRunner(engine, campaign_id, lambda *_: [],
                                  qualification_interval_seconds=0,
                                  qualification_observation_threshold=25)
    assert runner._qualification_due() is True
    engine.dispose()


def test_finalize_refreshes_stale_hash_link_before_integrity_audit(tmp_path):
    db, conn, campaign_id, run_id = _campaign(tmp_path)
    _canonical_reject(conn, run_id, "canonical", "setup:BTCUSDT:15m:1")
    conn.execute("""INSERT INTO burnin_qualification_snapshots(
        qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,
        expectancy_status,execution_status,regime_status,reject_quality_status,
        calibration_status,drawdown_status,concentration_status,reconciliation_status,
        evidence_completeness_status,blockers_json,warnings_json,thresholds_json,
        metrics_json,evidence_hash,schema_version,campaign_id,source_run_ids_json,
        aggregate_evidence_hash) VALUES(
        'prior',?,'rel','2026-01-01T00:00:00Z','BURN_IN_INSUFFICIENT','INSUFFICIENT',
        'FAIL','FAIL','FAIL','FAIL','FAIL','PASS','FAIL','FAIL','PASS',
        '[]','[]','{}','{}','old','v',?,?,'old')""",
        (run_id, campaign_id, json.dumps([run_id])))
    conn.execute("UPDATE burnin_campaigns SET latest_qualification_id='prior' WHERE campaign_id=?", (campaign_id,))
    conn.commit()

    result = finalize(conn, str(db), campaign_id, tmp_path / "final")
    current = aggregate_campaign(conn, campaign_id)["evidence_hash"]
    stored = conn.execute("""SELECT aggregate_evidence_hash FROM burnin_qualification_snapshots
        WHERE qualification_id=(SELECT latest_qualification_id FROM burnin_campaigns WHERE campaign_id=?)""",
        (campaign_id,)).fetchone()[0]
    assert stored == current
    assert "AGGREGATE_EVIDENCE_HASH_LINK_MISSING" not in result["blockers"]
