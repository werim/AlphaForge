"""#622: opt-in PAPER stats-only universe retention is safe and explicit."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import threading
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from alphaforge.burnin import BurnInRun, bootstrap_burnin_schema, persist_burnin_run
from alphaforge.burnin_campaign import (
    aggregate_campaign,
    bootstrap_campaign_schema,
    build_phase8_campaign_identity,
    create_campaign,
    get_campaign,
    qualify_campaign,
)
from alphaforge.config import load_config_from_env
from alphaforge.persistence import init_db
from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot
from alphaforge.sqlite_safety import SQLiteBusyExhausted
from alphaforge.storage_policy import (
    StorageController, StoragePolicy,
    compact_stats_only_universe_batch,
    ensure_stats_only_schema,
    plan_stats_only_universe,
    readonly,
    stats_only_campaign_summary,
)
from alphaforge.symbol_selector import UniverseConstraints, build_selected_universe
from alphaforge.universe_evidence import (
    load_persisted_universe_selection,
    persist_burnin_universe_selection_link,
    persist_universe_selection,
)


CID = "camp_622stats00000001"
RUN = CID + "_run_0000"
GIT_SHA = "a" * 40


def configured_runtime():
    return load_config_from_env(env={
        "ALPHAFORGE_STORAGE_HIGH_BYTES": str(10 * 1024 ** 3),
        "ALPHAFORGE_STORAGE_LOW_BYTES": str(5 * 1024 ** 3),
        "ALPHAFORGE_STORAGE_STATS_ONLY_ENABLED": "true",
        "ALPHAFORGE_STORAGE_STATS_ONLY_START_BYTES": str(8 * 1024 ** 3),
        "ALPHAFORGE_STORAGE_STATS_ONLY_TARGET_BYTES": str(5 * 1024 ** 3),
        "ALPHAFORGE_STORAGE_STATS_ONLY_MIN_AGE_SEC": "1",
        "ALPHAFORGE_STORAGE_STATS_ONLY_RECENT_CYCLES": "1",
        "ALPHAFORGE_STORAGE_BATCH_ROWS": "5000",
        "ALPHAFORGE_STORAGE_BATCH_SECONDS": "10",
    }).runtime


def stats_policy(**changes):
    return replace(StoragePolicy.from_config(configured_runtime()), **changes)


def selection_at(ts: float, count: int):
    candidates = [{
        "symbol": f"S{i:04d}USDT", "source_exchange": "binance",
        "market_ts": ts, "market_observed_at": ts,
        "market_data_source": "BINANCE_PUBLIC", "contract_type": "PERPETUAL",
        "quote_asset": "USDT", "instrument_status": "TRADING",
        "volume_24h_usdt": 200_000_000 - i * 10_000,
        "spread_pct": 0.0001 + i * 0.000001, "spread_status": "MEASURED",
        "expected_slippage_pct": 0.0002, "volatility_pct": 0.03,
        "volatility_status": "MEASURED", "funding_rate_pct": 0.0,
        "funding_status": "MEASURED", "trend_strength": 0.8,
        "chop_score": 0.1, "liquidity_score": 0.7, "timeframe": "1m",
    } for i in range(count)]
    return build_selected_universe(
        candidates, UniverseConstraints(max_active_symbols=5),
        decision_timestamp=ts, execution_mode="PAPER", git_sha=GIT_SHA,
        strategy_config_hash="strategy-622-stats",
    )


def build_db(tmp_path: Path, *, cycles: int = 5, candidates: int = 300,
             stats_only: bool = True, policy_changes: dict | None = None):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'stats-only.db'}")
    policy = stats_policy(**(policy_changes or {}))
    provenance = {
        "provider": "BINANCE_READ_ONLY_KLINES", "exchange": "BINANCE",
        "order_submission": "DISABLED", "source": "test",
        "universe_scope_mode": "DYNAMIC_PROVIDER_UNIVERSE",
    }
    if stats_only:
        provenance.update({
            "storage_retention_mode": "PAPER_STATS_ONLY",
            "storage_policy_hash": policy.identity_hash(),
            "qualification_semantics": "STATS_ONLY_NOT_QUALIFICATION_ELIGIBLE",
        })
    selections = [selection_at(1_600_000_000.0 + index, candidates) for index in range(cycles)]
    with engine.begin() as conn:
        bootstrap_burnin_schema(conn)
        bootstrap_campaign_schema(conn)
        persist_burnin_run(conn, BurnInRun(
            burnin_run_id=RUN, release_id="issue622-stats", execution_mode="PAPER",
            phase="PHASE8", status="RUNNING", git_commit=GIT_SHA,
            config_hash="campaign-config-622", strategy_config_hash="strategy-622-stats",
            universe_hash="campaign-universe-622", source_provenance=provenance,
            symbols=[], intervals=["1m"],
        ))
        conn.execute(text("""
            INSERT INTO burnin_campaigns(
              campaign_id,release_id,campaign_status,created_at,started_at,
              active_run_id,config_hash,strategy_config_hash,universe_hash,git_commit,
              source_provenance_json,symbols_json,intervals_json,schema_version
            ) VALUES (
              :cid,'issue622-stats','RUNNING','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z',
              :run,'campaign-config-622','strategy-622-stats','campaign-universe-622',:sha,
              :provenance,'[]','["1m"]','phase8_campaign_v1'
            )
        """), {"cid": CID, "run": RUN, "sha": GIT_SHA,
               "provenance": json.dumps(provenance, sort_keys=True)})
        conn.execute(text("""
            INSERT INTO burnin_campaign_runs(
              campaign_id,burnin_run_id,continuation_sequence,status,started_at,
              created_at,schema_version
            ) VALUES (:cid,:run,0,'RUNNING','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z','phase8_campaign_v1')
        """), {"cid": CID, "run": RUN})
        for selection in selections:
            persist_universe_selection(conn, selection)
            persist_burnin_universe_selection_link(
                conn, selection, campaign_id=CID, burnin_run_id=RUN
            )
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(
        mode="PAPER", requested_mode="PAPER", actual_mode="PAPER",
        runtime_status="OPERATING", instance_id="stats-only-runtime",
        campaign_id=CID, burnin_run_id=RUN, active_position_count=0,
        pending_order_count=0, recovery_action_required=False,
        unknown_exchange_state=False, reconciliation_status="CLEAN",
    ))
    with engine.begin() as conn:
        ensure_stats_only_schema(conn)
    return engine, policy, selections


def test_stats_only_config_is_opt_in_identity_bound_and_validated():
    default = load_config_from_env(env={}).runtime
    assert default.storage_stats_only_enabled is False
    assert default.storage_high_bytes == 1024 ** 3
    assert default.storage_stats_only_start_bytes == 8 * 1024 ** 3
    assert default.storage_stats_only_target_bytes == 5 * 1024 ** 3
    with pytest.raises(ValueError, match="stats-only storage"):
        load_config_from_env(env={"ALPHAFORGE_STORAGE_STATS_ONLY_ENABLED": "true"})
    configured = configured_runtime()
    assert configured.storage_high_bytes == 10 * 1024 ** 3
    assert configured.storage_stats_only_start_bytes == 8 * 1024 ** 3
    assert configured.storage_stats_only_target_bytes == 5 * 1024 ** 3
    full = build_phase8_campaign_identity(default, [], ["1m"], dynamic_universe=True)
    stats = build_phase8_campaign_identity(configured, [], ["1m"], dynamic_universe=True)
    assert full["config_hash"] != stats["config_hash"]
    assert full["strategy_config_hash"] == stats["strategy_config_hash"]


def test_new_dynamic_campaign_persists_stats_only_policy_identity(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'identity.db'}")
    runtime = configured_runtime()
    provenance = {
        "provider": "BINANCE_READ_ONLY_KLINES", "exchange": "BINANCE",
        "order_submission": "DISABLED", "source": "test",
    }
    try:
        with engine.begin() as conn:
            campaign = create_campaign(
                conn, release_id="issue622-new-identity", duration_days=1,
                symbols=[], intervals=["1m"], source_provenance=provenance,
                runtime_config=runtime, dynamic_universe=True,
            )
            loaded = get_campaign(conn, campaign.campaign_id)
            assert loaded["source_provenance"]["storage_retention_mode"] == "PAPER_STATS_ONLY"
            assert loaded["source_provenance"]["storage_policy_hash"] == StoragePolicy.from_config(runtime).identity_hash()
            assert loaded["source_provenance"]["qualification_semantics"] == "STATS_ONLY_NOT_QUALIFICATION_ELIGIBLE"
        with engine.begin() as conn:
            with pytest.raises(ValueError, match="REQUIRES_DYNAMIC_PAPER_CAMPAIGN"):
                create_campaign(
                    conn, release_id="issue622-fixed-invalid", duration_days=1,
                    symbols=["BTCUSDT"], intervals=["1m"],
                    source_provenance=provenance, runtime_config=runtime,
                    dynamic_universe=False,
                )
    finally:
        engine.dispose()


def _seed_protected_economics(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO order_decisions(decision_id,decision,symbol,mode,payload) VALUES ('d1','REJECTED','S0000USDT','PAPER','{}')")
        conn.exec_driver_sql("INSERT INTO fills(fill_id,order_id,symbol,qty,price,fee) VALUES ('f1','o1','S0000USDT',1,100,0.1)")
        conn.exec_driver_sql("INSERT INTO positions(position_id,symbol,status) VALUES ('p1','S0000USDT','CLOSED')")
        conn.exec_driver_sql("INSERT INTO orders(order_id,symbol,status) VALUES ('o1','S0000USDT','FILLED')")
        conn.exec_driver_sql("INSERT INTO trade_lifecycle_events(event_id,trade_id,lifecycle_state,payload) VALUES ('e1','t1','CLOSED','{}')")
        conn.exec_driver_sql("""INSERT INTO expectancy_evidence(
            evidence_id,source_decision_id,evidence_type,decision_time,resolved_at,
            symbol,net_r,run_id,campaign_id,evidence_complete,created_at
        ) VALUES ('x1','d1','REJECT_FORWARD','2020-01-01','2020-01-02',
          'S0000USDT',-0.2,?,?,1,'2020-01-02')""", (RUN, CID))
        conn.exec_driver_sql("""INSERT INTO burnin_observations(
            observation_id,burnin_run_id,release_id,observed_at,execution_mode,
            symbol,decision,evidence_complete,missing_fields_json,metrics_json,
            source_provenance_json,schema_version
        ) VALUES ('obs1',?,'issue622-stats','2020-01-01','PAPER','S0000USDT',
          'REJECTED',1,'[]','{}','{}','v1')""", (RUN,))
        conn.exec_driver_sql("""INSERT INTO burnin_trade_outcomes(
            outcome_id,burnin_run_id,release_id,trade_id,symbol,closed_at,net_r,net_pnl,
            evidence_complete,missing_cost_fields_json,payload_json,schema_version
        ) VALUES ('out1',?,'issue622-stats','t1','S0000USDT','2020-01-02',0.4,4,
          1,'[]','{}','v1')""", (RUN,))
        conn.exec_driver_sql("""INSERT INTO burnin_reject_outcomes(
            reject_outcome_id,burnin_run_id,release_id,reject_reason,symbol,decision_time,
            forward_label,hypothetical_net_r_after_costs,evidence_complete,payload_json,schema_version
        ) VALUES ('rej1',?,'issue622-stats','QUALITY','S0000USDT','2020-01-01',
          'SL_BEFORE_TP',-0.3,1,'{}','v1')""", (RUN,))
        conn.exec_driver_sql("""INSERT INTO portfolio_allocation_cycles(
            allocation_cycle_id,timestamp,mode,action,candidate_set_hash,
            portfolio_snapshot_hash,config_hash,universe_hash,evidence_hash,git_sha,
            release_id,runtime_identity,allocator_version,schema_version,payload_json
        ) VALUES ('pa1','2020-01-01','PAPER','ALLOCATE','c','p','cfg','u','ev',?,
          'issue622-stats','runtime','v1','v1','{}')""", (GIT_SHA,))
        conn.exec_driver_sql("""INSERT INTO portfolio_allocation_candidates(
            allocation_cycle_id,candidate_index,candidate_id,symbol,side,action,
            reason_codes_json,allocated_notional,correlation_group,
            correlation_contribution,concentration_contribution,hard_gate_accepted,
            hard_gate_reason,candidate_inputs_json,preference_components_json,payload_json
        ) VALUES ('pa1',0,'candidate','S0000USDT','LONG','ALLOCATE','[]',100,
          'DEFAULT',0,0,1,'PASS','{}','{}','{}')""")


def _protected_snapshot(engine):
    protected = (
        "order_decisions", "fills", "positions", "orders", "trade_lifecycle_events",
        "expectancy_evidence", "burnin_observations", "burnin_trade_outcomes",
        "burnin_reject_outcomes", "portfolio_allocation_cycles",
        "portfolio_allocation_candidates",
    )
    with engine.connect() as conn:
        return {table: [tuple(row) for row in conn.exec_driver_sql(
            f"SELECT * FROM {table} ORDER BY rowid"
        )] for table in protected}


@pytest.mark.parametrize("candidate_count", [300, 525])
def test_realistic_load_compacts_oldest_first_and_preserves_economics(tmp_path, candidate_count):
    engine, policy, selections = build_db(
        tmp_path, cycles=5, candidates=candidate_count,
        policy_changes={"batch_rows": 2 * (candidate_count + 2)},
    )
    try:
        _seed_protected_economics(engine)
        before = _protected_snapshot(engine)
        result = compact_stats_only_universe_batch(engine, policy)
        assert result["cycles_compacted"] == 2, result
        assert result["rows_removed"] == 2 * (candidate_count + 2)
        assert _protected_snapshot(engine) == before
        with readonly(Path(engine.url.database)) as conn:
            summary = stats_only_campaign_summary(conn, CID)
            assert summary["status"] == "STATS_ONLY_VERIFIED"
            assert summary["cumulative_candidate_count"] == 2 * candidate_count
            assert summary["qualification_eligible"] is False
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            remaining = [row[0] for row in conn.execute(
                "SELECT cycle_id FROM universe_selection_cycles ORDER BY decision_timestamp"
            )]
            assert remaining == [selection.cycle_id for selection in selections[2:]]
            with pytest.raises(ValueError, match="STATS_ONLY_NOT_REPLAYABLE"):
                load_persisted_universe_selection(conn, selections[0].cycle_id)
        with engine.connect() as conn:
            aggregate = aggregate_campaign(conn, CID)
        assert aggregate["metrics"]["universe_evidence_mode"] == "PAPER_STATS_ONLY"
        assert aggregate["metrics"]["qualification_eligible"] is False
        qualified = qualify_campaign(engine, CID)
        assert qualified["verdict"] == "STATS_ONLY_NOT_QUALIFICATION_ELIGIBLE"
        assert qualified["materialized"] is False
    finally:
        engine.dispose()


def test_old_campaign_without_policy_identity_is_never_compacted(tmp_path):
    engine, policy, _ = build_db(tmp_path, stats_only=False)
    try:
        with readonly(Path(engine.url.database)) as conn:
            plan = plan_stats_only_universe(conn, policy)
        assert plan["cycles"] == []
        assert plan["reason"] == "CAMPAIGN_NOT_BOUND_TO_STATS_ONLY_POLICY"
        result = compact_stats_only_universe_batch(engine, policy)
        assert result["rows_removed"] == 0
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM universe_selection_cycles").scalar_one() == 5
    finally:
        engine.dispose()


def test_unresolved_outcome_window_and_recent_cycle_are_pinned(tmp_path):
    engine, policy, selections = build_db(tmp_path, cycles=5, candidates=20)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("""INSERT INTO burnin_pending_reject_labels(
                pending_label_id,campaign_id,burnin_run_id,reject_decision_id,symbol,side,
                decision_timestamp,execution_cost_assumptions_json,source_provenance_json,
                due_at,status,created_at,schema_version
            ) VALUES ('pending',?,?, 'reject','S0000USDT','LONG',?,'{}','{}',
              '2030-01-01','PENDING','2020-01-01','v1')""",
                (CID, RUN, "2020-09-13T12:26:41.500000Z"))
        result = compact_stats_only_universe_batch(engine, policy)
        assert result["cycles_compacted"] == 2
        with engine.connect() as conn:
            remaining = set(conn.exec_driver_sql(
                "SELECT cycle_id FROM universe_selection_cycles"
            ).scalars())
        assert set(selection.cycle_id for selection in selections[2:]) == remaining
    finally:
        engine.dispose()


def test_crash_rolls_back_rollup_checkpoint_authorization_and_source_delete(tmp_path):
    engine, policy, _ = build_db(
        tmp_path, cycles=3, candidates=30, policy_changes={"batch_rows": 32}
    )
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("""CREATE TRIGGER inject_stats_only_crash
                BEFORE DELETE ON universe_selection_candidates
                BEGIN SELECT RAISE(ABORT,'injected stats-only crash'); END""")
        with pytest.raises((sqlite3.IntegrityError, IntegrityError), match="injected stats-only crash"):
            compact_stats_only_universe_batch(engine, policy)
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM universe_selection_cycles").scalar_one() == 3
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM storage_universe_compaction_rollups").scalar_one() == 0
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM storage_universe_compaction_checkpoints").scalar_one() == 0
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM storage_stats_only_authorizations").scalar_one() == 0
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER inject_stats_only_crash")
        assert compact_stats_only_universe_batch(engine, policy)["cycles_compacted"] == 1
    finally:
        engine.dispose()


def test_corrupt_source_and_forged_capability_fail_closed(tmp_path):
    engine, policy, selections = build_db(
        tmp_path, cycles=3, candidates=20, policy_changes={"batch_rows": 22}
    )
    try:
        with pytest.raises((sqlite3.Error, IntegrityError, OperationalError)):
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO storage_stats_only_authorizations VALUES ('forged',?,?,?,?)",
                    (CID, selections[0].cycle_id, selections[0].evidence_hash, "forged"),
                )
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER trg_universe_selection_candidates_no_update")
            conn.exec_driver_sql("""UPDATE universe_selection_candidates
                SET eligibility_state='INVALID' WHERE cycle_id=? AND candidate_index=0""",
                (selections[0].cycle_id,))
        with pytest.raises(ValueError, match="UNIVERSE_SELECTION_EVIDENCE_INVALID"):
            compact_stats_only_universe_batch(engine, policy)
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM universe_selection_cycles").scalar_one() == 3
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM storage_universe_compaction_rollups").scalar_one() == 0
    finally:
        engine.dispose()


def test_held_writer_is_bounded_and_concurrent_restarts_do_not_double_count(tmp_path):
    engine, policy, _ = build_db(
        tmp_path, cycles=4, candidates=30,
        policy_changes={"batch_rows": 32, "batch_seconds": 2},
    )
    held = sqlite3.connect(engine.url.database)
    held.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(SQLiteBusyExhausted):
            compact_stats_only_universe_batch(engine, policy)
        assert time.monotonic() - started < 2
    finally:
        held.rollback()
        held.close()
    results = []
    errors = []
    def run():
        try:
            results.append(compact_stats_only_universe_batch(engine, policy))
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    try:
        assert not errors and not any(thread.is_alive() for thread in threads)
        assert sum(result["cycles_compacted"] for result in results) == 2
        with readonly(Path(engine.url.database)) as conn:
            summary = stats_only_campaign_summary(conn, CID)
            assert summary["compacted_cycle_count"] == 2
            assert summary["cumulative_candidate_count"] == 60
            assert conn.execute("SELECT COUNT(*) FROM storage_stats_only_authorizations").fetchone()[0] == 0
    finally:
        engine.dispose()


def test_controller_starts_at_stats_threshold_without_blocking_before_hard_cap(tmp_path):
    changes = {
        "high_bytes": 50 * 1024 ** 2,
        "low_bytes": 64 * 1024,
        "stats_only_start_bytes": 128 * 1024,
        "stats_only_target_bytes": 96 * 1024,
        "batch_rows": 302,
        "batch_seconds": 10,
    }
    engine, policy, _ = build_db(
        tmp_path, cycles=4, candidates=300, policy_changes=changes
    )
    reader = sqlite3.connect(engine.url.database)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM universe_selection_candidates").fetchone()
    wal = Path(str(engine.url.database) + "-wal")
    try:
        report = StorageController(engine, policy).check(force=True)
        assert report["before"]["physical_bytes"] >= policy.stats_only_start_bytes
        assert report["before"]["physical_bytes"] < policy.high_bytes
        assert report["cycles_compacted"] == 1
        assert report["status"] == "STATS_ONLY_CLEANUP_IN_PROGRESS"
        assert report["blocked"] is False
        assert wal.exists()
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
    finally:
        reader.rollback()
        reader.close()
        engine.dispose()
