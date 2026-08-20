import json
import sqlite3

from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import persist_pending_reject_label, resolve_pending_rejects
from alphaforge.persistence import init_db
from alphaforge.reject_label_status import reject_label_status

COSTS = {"spread_cost": .01, "entry_slippage_cost": .01, "exit_slippage_cost": .01,
         "fee_cost": .01, "funding_cost": 0.0, "latency_cost": 0.0}
NOW = "2026-01-01T00:03:00Z"


def database(tmp_path, *, resolve=True, costs=COSTS, candles=None):
    path = tmp_path / "labels.db"
    init_db(f"sqlite+pysqlite:///{path}").dispose()
    conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
    campaign = create_campaign(conn, release_id="rel", duration_days=1, symbols=["BTCUSDT"], intervals=["1m"])
    run = start_or_resume_campaign(conn, campaign.campaign_id)["burnin_run_id"]
    conn.execute("""INSERT INTO rejected_signal_reviews(reject_decision_id,signal_id,reject_reason,raw_rr,effective_rr,created_at,payload_json)
                    VALUES('reject:1','s1','LOW_CONFIDENCE',2.0,1.8,'2026-01-01T00:00:00Z',?)""",
                 (json.dumps({"campaign_id": campaign.campaign_id}),))
    persist_pending_reject_label(conn, campaign_id=campaign.campaign_id, burnin_run_id=run,
        reject_decision_id="reject:1", signal_id="s1", symbol="BTCUSDT", side="LONG",
        decision_timestamp="2026-01-01T00:00:00Z", timeframe="1m", horizon_bars=1,
        entry=100, stop=90, target=120, execution_cost_assumptions=costs, regime="TRENDING",
        reject_reason="LOW_CONFIDENCE", source_provenance={"provider": "PAPER"})
    if resolve:
        resolve_pending_rejects(conn, {"BTCUSDT": candles or [{"timestamp": "2026-01-01T00:01:00Z", "high": 101, "low": 89}]}, now=NOW)
    conn.commit()
    return path, conn, campaign.campaign_id


def report(conn, identity):
    return reject_label_status(conn, identity, now=NOW)


def add_reject_observation(conn, run, reject_id, *, incomplete=False):
    conn.execute("""INSERT INTO burnin_observations(
        observation_id,burnin_run_id,release_id,observed_at,execution_mode,decision,
        lifecycle_state,evidence_complete,missing_fields_json,metrics_json,
        source_provenance_json,schema_version)
        VALUES(?,?,?,?,?,'REJECTED','SIGNAL_REJECTED',?,?,?,?,?)""", (
        ("incomplete_reject_geometry_" if incomplete else "reject_") + reject_id,
        run, "rel", "2026-01-01T00:00:00Z", "PAPER", 0 if incomplete else 1,
        json.dumps(["entry"] if incomplete else []),
        json.dumps({"reject_decision_id": reject_id, "reject_reason": "LOW_CONFIDENCE"}),
        json.dumps({"provider": "PAPER"}), "test"))


def test_healthy_complete_pipeline_passes_and_correctness_is_valid(tmp_path):
    _, conn, cid = database(tmp_path)
    result = report(conn, cid)
    assert result["status"] == "PASS" and result["reason_codes"] == []
    quality = result["reject_quality"][0]
    assert quality["reject_accuracy"] == 1.0 and quality["reject_correct_count"] == 1
    assert quality["average_mfe_pct"] == 1.0 and quality["average_mae_pct"] == 11.0
    assert quality["average_raw_rr"] == 2.0 and quality["average_effective_rr"] == 1.8


def test_early_immature_and_zero_outcomes_are_incomplete(tmp_path):
    _, conn, cid = database(tmp_path, resolve=False)
    result = reject_label_status(conn, cid, now="2026-01-01T00:00:30Z")
    assert result["status"] == "INCOMPLETE"
    assert {"NO_FORWARD_OUTCOMES_YET", "INSUFFICIENT_MATURE_EVIDENCE", "IMMATURE_LABELS_PRESENT"} <= set(result["reason_codes"])
    assert result["resolver_state"]["PENDING"] == 1 and result["resolver_state"]["overdue_pending_labels"] == 0


def test_incomplete_candle_evidence_stays_pending_and_overdue(tmp_path):
    _, conn, cid = database(tmp_path, resolve=True, candles=[{"timestamp": "2026-01-01T00:02:00Z", "high": 101, "low": 99}])
    result = report(conn, cid)
    assert result["status"] == "INCOMPLETE" and "OVERDUE_PENDING_LABELS" in result["reason_codes"]
    assert result["resolver_state"]["PENDING"] == 1


def test_orphan_pending_label_fails(tmp_path):
    _, conn, cid = database(tmp_path, resolve=False)
    conn.execute("DELETE FROM rejected_signal_reviews")
    result = report(conn, cid)
    assert result["status"] == "FAIL" and "ORPHAN_PENDING_LABEL" in result["reason_codes"]


def test_orphan_review_fails(tmp_path):
    _, conn, cid = database(tmp_path, resolve=False)
    conn.execute("INSERT INTO rejected_signal_reviews(reject_decision_id,reject_reason,created_at,payload_json) VALUES('orphan','LOW_CONFIDENCE','2026-01-01T00:00:00Z',?)", (json.dumps({"campaign_id": cid}),))
    result = report(conn, cid)
    assert result["status"] == "FAIL" and "ORPHAN_REJECT_REVIEW" in result["reason_codes"]


def test_duplicate_review_identity_fails(tmp_path):
    _, conn, cid = database(tmp_path, resolve=False)
    conn.execute("DROP INDEX ux_rejected_reviews_decision_id")
    conn.execute("INSERT INTO rejected_signal_reviews(reject_decision_id,reject_reason,created_at,payload_json) VALUES('reject:1','LOW_CONFIDENCE','2026-01-01T00:00:00Z',?)", (json.dumps({"campaign_id": cid}),))
    result = report(conn, cid)
    assert result["status"] == "FAIL" and "DUPLICATE_REJECT_IDENTITY" in result["reason_codes"]


def test_resolved_without_canonical_outcome_fails(tmp_path):
    _, conn, cid = database(tmp_path, resolve=False)
    conn.execute("UPDATE burnin_pending_reject_labels SET status='RESOLVED', evidence_complete=1")
    result = report(conn, cid)
    assert result["status"] == "FAIL" and "RESOLVED_WITHOUT_OUTCOME" in result["reason_codes"]


def test_stale_resolver_claim_is_detected_as_incomplete(tmp_path):
    _, conn, cid = database(tmp_path, resolve=False)
    conn.execute("UPDATE burnin_pending_reject_labels SET status='RESOLVING',claim_token='x',claimed_at='2025-12-31T23:00:00Z'")
    result = report(conn, cid)
    assert result["status"] == "INCOMPLETE" and "STALE_RESOLVER_CLAIM" in result["reason_codes"]
    assert result["resolver_state"]["stale_resolving_claims"] == 1


def test_execution_invalid_ambiguous_and_incomplete_are_excluded_from_accuracy(tmp_path):
    _, conn, cid = database(tmp_path)
    conn.execute("UPDATE rejected_signal_reviews SET reject_correct=NULL,evidence_complete=0,execution_invalidated=1")
    invalid = report(conn, cid)["reject_quality"][0]
    assert invalid["reject_accuracy"] is None and invalid["execution_invalidated_count"] == 1
    conn.execute("UPDATE rejected_signal_reviews SET execution_invalidated=0,outcome_ambiguous=1")
    ambiguous = report(conn, cid)["reject_quality"][0]
    assert ambiguous["reject_accuracy"] is None and ambiguous["ambiguous_count"] == 1
    conn.execute("UPDATE rejected_signal_reviews SET outcome_ambiguous=0,evidence_complete=0")
    assert report(conn, cid)["reject_quality"][0]["reject_accuracy"] is None


def test_invalid_non_null_correct_label_fails(tmp_path):
    _, conn, cid = database(tmp_path)
    conn.execute("UPDATE rejected_signal_reviews SET evidence_complete=0,reject_correct=1")
    result = report(conn, cid)
    assert result["status"] == "FAIL" and "INVALID_REJECT_CORRECT_LABEL" in result["reason_codes"]


def test_legacy_null_decision_review_links_by_signal_without_mutation(tmp_path):
    _, conn, cid = database(tmp_path, resolve=False)
    conn.execute("UPDATE rejected_signal_reviews SET reject_decision_id=NULL")
    result = report(conn, cid)
    assert result["status"] == "INCOMPLETE"
    assert result["integrity"]["pending_labels_without_reviews"] == 0
    assert result["integrity"]["reviews_without_eligible_pending_labels"] == 0
    assert conn.execute("SELECT reject_decision_id FROM rejected_signal_reviews").fetchone()[0] is None


def test_explicit_decision_mismatch_is_not_hidden_by_same_signal(tmp_path):
    _, conn, cid = database(tmp_path, resolve=False)
    conn.execute("UPDATE rejected_signal_reviews SET reject_decision_id='reject:other'")
    result = report(conn, cid)
    assert result["status"] == "FAIL"
    assert {"ORPHAN_PENDING_LABEL", "ORPHAN_REJECT_REVIEW"} <= set(result["reason_codes"])


def test_multiple_legacy_signal_matches_fail_closed_as_ambiguous(tmp_path):
    _, conn, cid = database(tmp_path, resolve=False)
    conn.execute("UPDATE rejected_signal_reviews SET reject_decision_id=NULL")
    conn.execute("""INSERT INTO rejected_signal_reviews(
        reject_decision_id,signal_id,reject_reason,created_at,payload_json
    ) VALUES(NULL,'s1','LOW_CONFIDENCE','2026-01-01T00:00:00Z',?)""",
                 (json.dumps({"campaign_id": cid}),))
    result = report(conn, cid)
    assert result["status"] == "FAIL"
    assert {"AMBIGUOUS_REVIEW_LINKAGE", "DUPLICATE_REJECT_IDENTITY"} <= set(result["reason_codes"])
    assert result["integrity"]["ambiguous_review_linkages"] == 1


def test_one_legacy_review_cannot_link_to_multiple_pending_decisions(tmp_path):
    _, conn, cid = database(tmp_path, resolve=False)
    conn.execute("UPDATE rejected_signal_reviews SET reject_decision_id=NULL")
    original = dict(conn.execute("SELECT * FROM burnin_pending_reject_labels").fetchone())
    columns = [key for key in original if key != "id"]
    values = [original[key] for key in columns]
    values[columns.index("pending_label_id")] = "prej_second"
    values[columns.index("reject_decision_id")] = "reject:second"
    conn.execute(f"INSERT INTO burnin_pending_reject_labels({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)
    result = report(conn, cid)
    assert result["status"] == "FAIL"
    assert result["integrity"]["ambiguous_review_linkages"] == 2
    assert "AMBIGUOUS_REVIEW_LINKAGE" in result["reason_codes"]


def test_legacy_linkage_remains_valid_after_resolver_synchronization(tmp_path):
    _, conn, cid = database(tmp_path, resolve=False)
    conn.execute("UPDATE rejected_signal_reviews SET reject_decision_id=NULL")
    resolve_pending_rejects(conn, {"BTCUSDT": [{"timestamp": "2026-01-01T00:01:00Z", "high": 101, "low": 89}]}, now=NOW)
    result = report(conn, cid)
    assert result["status"] == "PASS"
    assert result["reject_quality"][0]["reject_accuracy"] == 1.0
    assert result["reject_quality"][0]["reject_correct_count"] == 1
    assert conn.execute("SELECT reject_decision_id FROM rejected_signal_reviews").fetchone()[0] is None


def test_legacy_pre317_database_bootstraps_without_fabricating_geometry(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY,symbol TEXT,qty REAL,status TEXT)")
    conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY,order_id TEXT,symbol TEXT,status TEXT)")
    conn.execute("""CREATE TABLE burnin_pending_reject_labels(id INTEGER PRIMARY KEY,pending_label_id TEXT UNIQUE,campaign_id TEXT,burnin_run_id TEXT,reject_decision_id TEXT UNIQUE,signal_id TEXT,symbol TEXT,side TEXT,decision_timestamp TEXT,entry REAL,stop REAL,target REAL,horizon_seconds REAL,execution_cost_assumptions_json TEXT,regime TEXT,reject_reason TEXT,source_provenance_json TEXT,due_at TEXT,status TEXT,evidence_complete INTEGER,last_error TEXT,created_at TEXT,resolved_at TEXT,schema_version TEXT)""")
    conn.execute("INSERT INTO burnin_pending_reject_labels VALUES(1,'p','standalone:run','run','legacy','s','BTCUSDT','LONG','2026-01-01T00:00:00Z',100,90,120,3600,'{}','R','LOW_CONFIDENCE','{}','2026-01-01T01:00:00Z','PENDING',0,NULL,'2026-01-01T00:00:00Z',NULL,'old')")
    conn.commit(); conn.close()
    init_db(f"sqlite+pysqlite:///{path}").dispose()
    conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT timeframe,horizon_bars,horizon_seconds FROM burnin_pending_reject_labels").fetchone()
    assert tuple(row) == (None, None, 3600)
    result = reject_label_status(conn, "standalone:run", now=NOW)
    assert result["status"] == "FAIL" and "SCHEMA_INCOMPLETE" not in result["reason_codes"]
    assert "ORPHAN_PENDING_LABEL" in result["reason_codes"]


def test_repeated_validation_is_read_only_and_idempotent(tmp_path):
    path, conn, cid = database(tmp_path)
    before = path.read_bytes()
    first = report(conn, cid); second = report(conn, cid)
    assert first == second and path.read_bytes() == before


def test_complete_denominator_prevents_one_good_label_hiding_unlabelable_rejects(tmp_path):
    _, conn, cid = database(tmp_path)
    run = conn.execute("SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=?", (cid,)).fetchone()[0]
    for index in range(5):
        reject_id = f"bad:{index}"
        add_reject_observation(conn, run, reject_id, incomplete=True)
        conn.execute("""INSERT INTO rejected_signal_reviews(
            reject_decision_id,signal_id,reject_reason,created_at,payload_json)
            VALUES(?,?,?,?,?)""", (reject_id, reject_id, "LOW_CONFIDENCE",
            "2026-01-01T00:00:00Z", json.dumps({"campaign_id": cid})))
    result = report(conn, cid)
    assert result["status"] == "INCOMPLETE"
    assert result["coverage"]["total_rejected_decisions"] == 6
    assert result["coverage"]["incomplete_geometry_rejects"] == 5
    assert "INCOMPLETE_REJECT_GEOMETRY" in result["reason_codes"]


def test_eligible_reject_without_pending_label_fails(tmp_path):
    _, conn, cid = database(tmp_path)
    run = conn.execute("SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=?", (cid,)).fetchone()[0]
    add_reject_observation(conn, run, "eligible:missing")
    conn.execute("""INSERT INTO rejected_signal_reviews(
        reject_decision_id,signal_id,reject_reason,created_at,payload_json)
        VALUES('eligible:missing','missing','LOW_CONFIDENCE','2026-01-01T00:00:00Z',?)""",
        (json.dumps({"campaign_id": cid}),))
    result = report(conn, cid)
    assert result["status"] == "FAIL"
    assert result["coverage"]["unlabeled_rejects"] == 1
    assert "MISSING_ELIGIBLE_PENDING_LABEL" in result["reason_codes"]


def test_failed_ambiguous_and_execution_invalidated_populations_are_blocking(tmp_path):
    _, conn, cid = database(tmp_path)
    conn.execute("UPDATE burnin_pending_reject_labels SET status='FAILED',evidence_complete=0")
    conn.execute("UPDATE burnin_reject_outcomes SET evidence_complete=0,execution_invalidated=1")
    conn.execute("UPDATE rejected_signal_reviews SET reject_correct=NULL,evidence_complete=0,execution_invalidated=1")
    failed = report(conn, cid)
    assert failed["status"] == "INCOMPLETE" and "FAILED_LABELS_PRESENT" in failed["reason_codes"]
    conn.execute("UPDATE burnin_pending_reject_labels SET status='AMBIGUOUS'")
    conn.execute("UPDATE burnin_reject_outcomes SET execution_invalidated=0,ambiguous=1")
    conn.execute("UPDATE rejected_signal_reviews SET execution_invalidated=0,outcome_ambiguous=1")
    ambiguous = report(conn, cid)
    assert ambiguous["status"] == "INCOMPLETE" and "AMBIGUOUS_LABELS_PRESENT" in ambiguous["reason_codes"]
    conn.execute("UPDATE burnin_pending_reject_labels SET status='FAILED'")
    conn.execute("UPDATE burnin_reject_outcomes SET ambiguous=0,execution_invalidated=1")
    conn.execute("UPDATE rejected_signal_reviews SET outcome_ambiguous=0,execution_invalidated=1")
    invalidated = report(conn, cid)
    assert invalidated["status"] != "PASS"
    assert "EXECUTION_INVALIDATED_LABELS_PRESENT" in invalidated["reason_codes"]


def test_legacy_unattributed_observation_is_separate_and_incomplete(tmp_path):
    _, conn, cid = database(tmp_path)
    run = conn.execute("SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=?", (cid,)).fetchone()[0]
    conn.execute("""INSERT INTO burnin_observations(
        observation_id,burnin_run_id,release_id,observed_at,execution_mode,decision,
        evidence_complete,missing_fields_json,metrics_json,source_provenance_json,schema_version)
        VALUES('legacy-unattributed',?,?,?,'PAPER','REJECTED',1,'[]','{}','{}','legacy')""",
        (run, "rel", "2025-01-01T00:00:00Z"))
    result = report(conn, cid)
    assert result["status"] == "INCOMPLETE"
    assert result["coverage"]["total_rejected_decisions"] == 1
    assert result["coverage"]["legacy_unattributed_observations"] == 1
    assert "LEGACY_UNATTRIBUTED_REJECT_EVIDENCE" in result["reason_codes"]


def test_one_resolved_plus_future_pending_population_cannot_pass(tmp_path):
    _, conn, cid = database(tmp_path)
    run = conn.execute("SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=?", (cid,)).fetchone()[0]
    for index in range(3):
        rid = f"future:{index}"
        add_reject_observation(conn, run, rid)
        conn.execute("""INSERT INTO rejected_signal_reviews(
            reject_decision_id,signal_id,reject_reason,created_at,payload_json)
            VALUES(?,?,?,?,?)""", (rid, rid, "LOW_CONFIDENCE", "2026-01-01T00:02:30Z",
            json.dumps({"campaign_id": cid})))
        persist_pending_reject_label(conn, campaign_id=cid, burnin_run_id=run,
            reject_decision_id=rid, signal_id=rid, symbol="BTCUSDT", side="LONG",
            decision_timestamp="2026-01-01T00:02:30Z", timeframe="1m", horizon_bars=10,
            entry=100, stop=90, target=120, execution_cost_assumptions=COSTS,
            regime="TRENDING", reject_reason="LOW_CONFIDENCE", source_provenance={"provider": "PAPER"})
    result = report(conn, cid)
    assert result["status"] == "INCOMPLETE"
    assert result["resolver_state"]["PENDING"] == 3
    assert result["coverage"]["mature_coverage_ratio"] == .25
    assert {"IMMATURE_LABELS_PRESENT", "INCOMPLETE_MATURE_COVERAGE"} <= set(result["reason_codes"])


def test_pending_outcome_state_contradictions_and_unknown_status_fail(tmp_path):
    _, conn, cid = database(tmp_path)
    conn.execute("UPDATE burnin_pending_reject_labels SET status='PENDING'")
    pending = report(conn, cid)
    assert pending["status"] == "FAIL"
    assert "PENDING_OUTCOME_STATE_INCONSISTENCY" in pending["reason_codes"]

    conn.execute("UPDATE burnin_pending_reject_labels SET status='RESOLVED'")
    conn.execute("UPDATE burnin_reject_outcomes SET evidence_complete=0")
    assert report(conn, cid)["status"] == "FAIL"

    conn.execute("UPDATE burnin_pending_reject_labels SET status='AMBIGUOUS'")
    conn.execute("UPDATE burnin_reject_outcomes SET evidence_complete=0,ambiguous=0")
    assert report(conn, cid)["status"] == "FAIL"

    conn.execute("UPDATE burnin_pending_reject_labels SET status='FAILED'")
    conn.execute("UPDATE burnin_reject_outcomes SET evidence_complete=1")
    assert report(conn, cid)["status"] == "FAIL"

    conn.execute("UPDATE burnin_pending_reject_labels SET status='UNKNOWN_STATE'")
    assert report(conn, cid)["status"] == "FAIL"


def test_campaign_standalone_and_unrelated_history_are_isolated(tmp_path):
    _, conn, cid = database(tmp_path)
    campaign_result = report(conn, cid)
    run = conn.execute("SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=?", (cid,)).fetchone()[0]
    add_reject_observation(conn, "unrelated-run", "unrelated")
    conn.execute("""INSERT INTO rejected_signal_reviews(
        reject_decision_id,signal_id,reject_reason,created_at,payload_json)
        VALUES('unrelated','unrelated','LOW_CONFIDENCE','2020-01-01T00:00:00Z','{}')""")
    assert report(conn, cid) == campaign_result
    standalone = report(conn, "standalone:unrelated-run")
    assert standalone["coverage"]["total_rejected_decisions"] == 1
    assert standalone["coverage"]["unlabeled_rejects"] == 1


def test_campaign_universe_contamination_is_read_only_structural_failure(tmp_path):
    _, conn, cid = database(tmp_path)
    run = conn.execute("SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=?", (cid,)).fetchone()[0]
    before = conn.total_changes
    conn.execute("""INSERT INTO rejected_signal_reviews(
        reject_decision_id,signal_id,symbol,reject_reason,created_at,payload_json)
        VALUES('reject:bch','bch','BCHUSDT','LOW_CONFIDENCE','2026-01-01T00:00:00Z',?)""",
        (json.dumps({"campaign_id": cid, "symbol": "BCHUSDT", "source_exchange": "binance"}),))
    add_reject_observation(conn, run, "reject:bch", incomplete=True)
    conn.commit()
    before = conn.total_changes
    result = report(conn, cid)
    assert result["status"] == "FAIL"
    assert "CAMPAIGN_UNIVERSE_MISMATCH" in result["reason_codes"]
    assert result["campaign_scope"]["out_of_universe_symbols"] == ["BCHUSDT"]
    assert result["campaign_scope"]["out_of_universe_decision_count"] == 1
    assert conn.total_changes == before
