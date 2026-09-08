import json
import sqlite3

from alphaforge.burnin import DIAGNOSTIC_OBSERVATION_KIND, persist_burnin_observation, persist_burnin_reject_outcome
from alphaforge.burnin_campaign import aggregate_campaign, create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import persist_pending_reject_label, resolve_campaign_batch


COSTS = {
    "spread_cost": 0.01,
    "entry_slippage_cost": 0.01,
    "exit_slippage_cost": 0.01,
    "fee_cost": 0.01,
    "funding_cost": 0.0,
    "latency_cost": 0.0,
}


def _campaign(conn, release_id="rel"):
    campaign = create_campaign(
        conn, release_id=release_id, duration_days=1,
        symbols=["BTCUSDT"], intervals=["1m"])
    run_id = start_or_resume_campaign(conn, campaign.campaign_id)["burnin_run_id"]
    return campaign.campaign_id, run_id


def _canonical_reject(conn, campaign_id, run_id, reject_id):
    release_id = conn.execute(
        "SELECT release_id FROM burnin_runs WHERE burnin_run_id=?", (run_id,)
    ).fetchone()[0]
    persist_burnin_observation(
        conn, observation_id=f"canonical:{run_id}:{reject_id}",
        burnin_run_id=run_id, release_id=release_id, execution_mode="PAPER",
        decision="REJECTED", lifecycle_state="SIGNAL_REJECTED",
        metrics={"reject_decision_id": reject_id, "signal_id": f"signal:{reject_id}",
                 "campaign_id": campaign_id},
    )


def _pending_kwargs(campaign_id, run_id, reject_id, **overrides):
    values = {
        "campaign_id": campaign_id,
        "burnin_run_id": run_id,
        "reject_decision_id": reject_id,
        "signal_id": f"signal:{reject_id}",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "decision_timestamp": "2026-01-01T00:00:00Z",
        "timeframe": "1m",
        "horizon_bars": 1,
        "entry": 100,
        "stop": 90,
        "target": 120,
        "execution_cost_assumptions": COSTS,
        "regime": "TRENDING",
        "reject_reason": "LOW_CONFIDENCE",
        "source_provenance": {"provider": "PAPER", "forward_label_subject": "GUIDED_CANDIDATE"},
    }
    values.update(overrides)
    return values


def _insert_orphan_pending(conn, campaign_id, run_id, reject_id):
    conn.execute("""INSERT INTO burnin_pending_reject_labels(
        pending_label_id,campaign_id,burnin_run_id,reject_decision_id,signal_id,
        symbol,side,decision_timestamp,timeframe,horizon_bars,entry,stop,target,
        horizon_seconds,execution_cost_assumptions_json,regime,reject_reason,
        source_provenance_json,due_at,status,created_at,schema_version)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        f"orphan:{reject_id}", campaign_id, run_id, reject_id, f"signal:{reject_id}",
        "BTCUSDT", "LONG", "2026-01-01T00:00:00Z", "1m", 1, 100, 90, 120,
        60, json.dumps(COSTS), "TRENDING", "LOW_CONFIDENCE",
        json.dumps({"forward_label_subject": "GUIDED_CANDIDATE"}),
        "2026-01-01T00:01:00Z", "PENDING", "2026-01-01T00:00:00Z", "test"))


def test_canonical_pending_label_is_exactly_once_and_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    campaign_id, run_id = _campaign(conn)
    _canonical_reject(conn, campaign_id, run_id, "reject:one")
    kwargs = _pending_kwargs(campaign_id, run_id, "reject:one")

    first = persist_pending_reject_label(conn, **kwargs)
    second = persist_pending_reject_label(conn, **kwargs)

    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM burnin_pending_reject_labels").fetchone()[0] == 1
    metrics = aggregate_campaign(conn, campaign_id)["metrics"]
    assert metrics["label_eligible_rejects"] == 1
    assert metrics["eligible_reject_labels_persisted"] == 1
    assert metrics["reject_label_coverage"] == 1.0


def test_distinct_canonical_rejects_have_distinct_pending_label_ids():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    campaign_id, run_id = _campaign(conn)
    ids = []
    for reject_id in ("reject:first", "reject:second"):
        _canonical_reject(conn, campaign_id, run_id, reject_id)
        ids.append(persist_pending_reject_label(
            conn, **_pending_kwargs(campaign_id, run_id, reject_id)))

    assert None not in ids
    assert len(set(ids)) == 2
    assert ids == ["prej_reject:first", "prej_reject:second"]


def test_duplicate_canonical_observation_artifact_does_not_inflate_denominator():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    campaign_id, run_id = _campaign(conn)
    reject_id = "reject:duplicate-observation"
    _canonical_reject(conn, campaign_id, run_id, reject_id)
    persist_burnin_observation(
        conn, observation_id="duplicate-persistence-artifact", burnin_run_id=run_id,
        release_id="rel", execution_mode="PAPER", decision="REJECTED",
        lifecycle_state="SIGNAL_REJECTED",
        metrics={"reject_decision_id": reject_id, "campaign_id": campaign_id})
    persist_pending_reject_label(conn, **_pending_kwargs(campaign_id, run_id, reject_id))

    metrics = aggregate_campaign(conn, campaign_id)["metrics"]
    assert metrics["canonical_rejected_decisions"] == 1
    assert metrics["label_eligible_rejects"] == 1
    assert metrics["eligible_reject_labels_persisted"] == 1
    assert metrics["reject_label_coverage"] == 1.0


def test_diagnostic_and_orphan_identities_do_not_increase_canonical_counts():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    campaign_id, run_id = _campaign(conn)
    _canonical_reject(conn, campaign_id, run_id, "reject:canonical")
    persist_pending_reject_label(conn, **_pending_kwargs(campaign_id, run_id, "reject:canonical"))
    persist_burnin_observation(
        conn, observation_id="diagnostic:orphan", burnin_run_id=run_id,
        release_id="rel", execution_mode="PAPER", decision="REJECTED",
        metrics={"reject_decision_id": "reject:diagnostic", "campaign_id": campaign_id},
        observation_kind=DIAGNOSTIC_OBSERVATION_KIND)
    _insert_orphan_pending(conn, campaign_id, run_id, "reject:orphan")

    metrics = aggregate_campaign(conn, campaign_id)["metrics"]
    assert metrics["canonical_rejected_decisions"] == 1
    assert metrics["label_eligible_rejects"] == 1
    assert metrics["eligible_reject_labels_persisted"] == 1
    assert metrics["unique_reject_labels_persisted"] == 1
    assert metrics["diagnostic_unique_reject_labels_persisted"] == 2
    assert metrics["non_attributable_reject_labels_persisted"] == 1
    assert "LABELS_WITHOUT_CANONICAL_REJECT" in metrics["reject_label_integrity_issues"]
    assert metrics["eligible_reject_labels_persisted"] <= metrics["label_eligible_rejects"]


def test_missing_cross_campaign_and_cross_run_identity_fails_closed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    campaign_a, run_a = _campaign(conn, "rel-a")
    campaign_b, run_b = _campaign(conn, "rel-b")
    _canonical_reject(conn, campaign_a, run_a, "reject:a")

    assert persist_pending_reject_label(
        conn, **_pending_kwargs(campaign_a, run_a, "reject:missing")) is None
    assert persist_pending_reject_label(
        conn, **_pending_kwargs(campaign_b, run_b, "reject:a")) is None
    assert persist_pending_reject_label(
        conn, **_pending_kwargs(campaign_b, run_a, "reject:a")) is None
    assert conn.execute("SELECT COUNT(*) FROM burnin_pending_reject_labels").fetchone()[0] == 0
    diagnostics = conn.execute("""SELECT COUNT(*) FROM burnin_observations
        WHERE observation_id LIKE 'invalid_reject_identity_%'
          AND json_extract(metrics_json,'$.reject_quality_attributable')=0""").fetchone()[0]
    assert diagnostics == 3


def test_resolver_retry_produces_one_identity_preserving_outcome():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    campaign_id, run_id = _campaign(conn)
    reject_id = "reject:retry"
    _canonical_reject(conn, campaign_id, run_id, reject_id)
    persist_pending_reject_label(conn, **_pending_kwargs(campaign_id, run_id, reject_id))
    candles = {"BTCUSDT": [{"timestamp": "2026-01-01T00:01:00Z", "high": 121, "low": 99}]}

    assert resolve_campaign_batch(
        conn, campaign_id, candles, now="2026-01-01T00:02:00Z")["resolved"] == 1
    first = conn.execute("SELECT * FROM burnin_reject_outcomes").fetchone()
    conn.execute("""UPDATE burnin_pending_reject_labels
        SET status='RESOLVING',claim_token='stale-replay',
            claimed_at='2025-12-31T23:00:00Z'""")
    resolve_campaign_batch(
        conn, campaign_id,
        {"BTCUSDT": [{"timestamp": "2026-01-01T00:01:00Z", "high": 101, "low": 89}]},
        now="2026-01-01T00:02:00Z")

    outcomes = conn.execute("SELECT * FROM burnin_reject_outcomes").fetchall()
    assert len(outcomes) == 1
    assert dict(outcomes[0]) == dict(first)
    payload = json.loads(outcomes[0]["payload_json"])
    assert payload["reject_decision_id"] == reject_id
    assert payload["pending_label_id"] == conn.execute(
        "SELECT pending_label_id FROM burnin_pending_reject_labels").fetchone()[0]

    persist_burnin_reject_outcome(
        conn, reject_outcome_id="diagnostic-duplicate", burnin_run_id=run_id,
        release_id="rel", reject_reason="LOW_CONFIDENCE", symbol="BTCUSDT",
        forward_label="SL_BEFORE_TP", hypothetical_net_r_after_costs=-1.0,
            evidence_complete=True,
            payload={"reject_decision_id": reject_id,
                     "pending_label_id": payload["pending_label_id"],
                     "campaign_id": campaign_id, "burnin_run_id": run_id,
                     "reject_quality_attributable": True})
    metrics = aggregate_campaign(conn, campaign_id)["metrics"]
    assert metrics["completed_rejected_forward_outcomes"] == 1
    assert metrics["duplicate_canonical_rejected_forward_outcomes"] == 1
    assert "DUPLICATE_CANONICAL_REJECT_OUTCOMES" in metrics["reject_label_integrity_issues"]


def test_matching_outcome_id_without_explicit_pending_ownership_fails_closed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    campaign_id, run_id = _campaign(conn)
    reject_id = "reject:forged-outcome"
    _canonical_reject(conn, campaign_id, run_id, reject_id)
    persist_pending_reject_label(conn, **_pending_kwargs(campaign_id, run_id, reject_id))
    persist_burnin_reject_outcome(
        conn, reject_outcome_id=f"rout_{reject_id}", burnin_run_id=run_id,
        release_id="rel", reject_reason="LOW_CONFIDENCE", symbol="BTCUSDT",
        forward_label="SL_BEFORE_TP", hypothetical_net_r_after_costs=-1.0,
        evidence_complete=True, payload={})

    counts = resolve_campaign_batch(
        conn, campaign_id, {}, now="2026-01-01T00:02:00Z")

    pending = conn.execute(
        "SELECT status,last_error FROM burnin_pending_reject_labels").fetchone()
    assert counts["failed"] == 1
    assert tuple(pending) == ("FAILED", "CANONICAL_OUTCOME_IDENTITY_CONFLICT")
    metrics = aggregate_campaign(conn, campaign_id)["metrics"]
    assert metrics["completed_rejected_forward_outcomes"] == 0
    assert metrics["diagnostic_completed_rejected_forward_outcomes"] == 1


def test_resolver_rejects_manually_persisted_orphan_pending_label():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    campaign_id, run_id = _campaign(conn)
    _insert_orphan_pending(conn, campaign_id, run_id, "reject:orphan")

    counts = resolve_campaign_batch(
        conn, campaign_id,
        {"BTCUSDT": [{"timestamp": "2026-01-01T00:01:00Z", "high": 121, "low": 99}]},
        now="2026-01-01T00:02:00Z")

    pending = conn.execute("SELECT status,last_error FROM burnin_pending_reject_labels").fetchone()
    assert counts["failed"] == 1
    assert tuple(pending) == (
        "FAILED", "CANONICAL_REJECT_IDENTITY_INVALID:CANONICAL_REJECT_NOT_FOUND")
    assert conn.execute("SELECT COUNT(*) FROM burnin_reject_outcomes").fetchone()[0] == 0


def test_geometry_ineligible_canonical_reject_cannot_be_force_labeled():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    campaign_id, run_id = _campaign(conn)
    reject_id = "reject:geometry"
    _canonical_reject(conn, campaign_id, run_id, reject_id)

    invalid = _pending_kwargs(campaign_id, run_id, reject_id, stop=None)
    assert persist_pending_reject_label(conn, **invalid) is None
    assert persist_pending_reject_label(
        conn, **_pending_kwargs(campaign_id, run_id, reject_id)) is None

    metrics = aggregate_campaign(conn, campaign_id)["metrics"]
    assert metrics["label_eligible_rejects"] == 0
    assert metrics["label_ineligible_rejects"] == 1
    assert metrics["eligible_reject_labels_persisted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM burnin_pending_reject_labels").fetchone()[0] == 0
