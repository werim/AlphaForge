from __future__ import annotations

from alphaforge.db_doctor import diagnose
from alphaforge.db_doctor.writer_probes import run_writer_probes
from alphaforge.persistence import init_db
from alphaforge.runtime_heartbeat import ensure_runtime_heartbeat_schema


def test_fresh_bootstrap_matches_runtime_writers_and_paper_contract(tmp_path):
    db = tmp_path / "fresh-runtime-contract.db"
    engine = init_db(f"sqlite+pysqlite:///{db}")
    # Heartbeats are deliberately PAPER/LIVE-provisioned, never BACKTEST
    # bootstrap side effects. Once provisioned, their contract must diagnose.
    ensure_runtime_heartbeat_schema(engine)
    engine.dispose()

    result = diagnose(db)
    assert result["inspection"]["integrity"] == ["ok"]
    assert result["status"] == "HEALTHY"
    assert not result["runtime_blockers"]
    assert not [i for i in result["issues"] if i["code"] == "NOT_NULL_WRITER_CONFLICT"]
    canonical = {
        "signals", "order_decisions", "ai_decision_features", "trade_lifecycle_events",
        "setup_expectancy_stats", "regime_expectancy_stats", "symbol_expectancy_stats",
        "reconciliation_incidents",
    }
    assert not [
        i for i in result["issues"]
        if i["code"] == "INCOMPATIBLE_OWNER_CONTRACTS" and i["table"] in canonical
    ]
    for writer in (
        "persistence.save_signal", "persistence.save_order_decision",
        "persistence.save_trade_lifecycle_event", "reconciliation.persist_incident",
        "runtime_control", "runtime_heartbeat.record_runtime_heartbeat",
        "runtime_state.persist_runtime_state",
    ):
        assert result["writer_compatibility"][writer]["compatible"], writer
    assert canonical - {"reconciliation_incidents"} <= set(
        result["ORM_alignment"]["excluded_sql_first_tables"]
    )

    probes = run_writer_probes(db)
    assert probes["passed"], probes
    assert {c["name"] for c in probes["checks"]} >= {
        "save_signal", "save_order_decision", "save_trade_lifecycle_event_signal_created",
        "reconciliation_persist_findings", "runtime_heartbeat", "runtime_state_snapshot",
    }
