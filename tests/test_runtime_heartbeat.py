from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import inspect, text

from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.runtime_heartbeat import (
    evaluate_runtime_heartbeat_freshness,
    fetch_latest_runtime_heartbeat,
    save_runtime_heartbeat,
)


class _NoopBrain:
    pass


def _runtime(engine, mode: ExecutionMode) -> RuntimeOrchestrator:
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=mode),
        ai_brain=_NoopBrain(),
        market_scanner=lambda: None,
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        persistence_engine=engine,
    )
    orchestrator.metrics.persistence_enabled = True
    return orchestrator


def test_runtime_owned_paper_heartbeat_persists_operating_evidence(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'paper_runtime.db'}")
    runtime = _runtime(engine, ExecutionMode.PAPER)
    runtime.metrics.scans = 3
    runtime.metrics.last_scan_ts = datetime.now(timezone.utc).isoformat()
    runtime._persist_runtime_heartbeat()

    latest = fetch_latest_runtime_heartbeat(engine)
    assert latest is not None
    assert latest["runtime_instance_id"] == runtime.runtime_instance_id
    assert latest["execution_mode"] == "PAPER"
    assert latest["runtime_state"] == "OPERATING"
    assert latest["evidence_status"] == "MEASURED_RUNTIME_HEARTBEAT"


def test_backtest_runtime_does_not_persist_heartbeat_evidence(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'backtest_runtime.db'}")
    runtime = _runtime(engine, ExecutionMode.BACKTEST)
    runtime._persist_runtime_heartbeat()
    assert not inspect(engine).has_table("runtime_heartbeats")


def test_payload_json_uses_allowlist_and_excludes_credentials(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'redaction.db'}")
    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:payload",
        execution_mode="PAPER",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        payload={"scans": 2, "api_key": "must-not-persist", "signature": "must-not-persist"},
    )
    with engine.connect() as conn:
        payload_json = conn.execute(text("SELECT payload_json FROM runtime_heartbeats LIMIT 1")).scalar_one()
    payload = json.loads(payload_json)
    assert payload == {"scans": 2}


def test_payload_json_preserves_mtf_observability_counters(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'mtf_counters.db'}")
    mtf_counters = {
        "mtf_contexts_built": 257,
        "mtf_alignment_pass": 0,
        "mtf_alignment_reject": 257,
        "mtf_regime_missing": 0,
        "mtf_setup_missing": 89,
        "mtf_execution_missing": 0,
        "mtf_execution_not_confirmed": 257,
        "mtf_direction_mismatch": 168,
        "mtf_stale_context": 0,
    }
    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:mtf-observability",
        execution_mode="PAPER",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        payload={**mtf_counters, "api_key": "must-not-persist"},
    )

    with engine.connect() as conn:
        payload_json = conn.execute(text(
            "SELECT payload_json FROM runtime_heartbeats LIMIT 1"
        )).scalar_one()

    assert json.loads(payload_json) == mtf_counters


def test_freshness_states_are_deterministic_and_fail_closed(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'freshness.db'}")
    now = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    assert evaluate_runtime_heartbeat_freshness(engine, now=now).state == "MISSING"

    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:stale",
        execution_mode="LIVE",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        heartbeat_ts=(now - timedelta(seconds=121)).isoformat(),
    )
    assert evaluate_runtime_heartbeat_freshness(engine, required_mode="LIVE", max_age_sec=120, now=now).state == "STALE"

    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:future",
        execution_mode="LIVE",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        heartbeat_ts=(now + timedelta(seconds=30)).isoformat(),
    )
    assert evaluate_runtime_heartbeat_freshness(engine, required_mode="LIVE", now=now).state == "FUTURE_DATED"

    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:invalid",
        execution_mode="LIVE",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        heartbeat_ts="invalid",
    )
    assert evaluate_runtime_heartbeat_freshness(engine, required_mode="LIVE", now=now).state == "INVALID"


def test_latest_heartbeat_selection_prefers_last_insert_for_equal_timestamp(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'latest.db'}")
    heartbeat_ts = "2026-05-23T12:00:00+00:00"
    save_runtime_heartbeat(engine, runtime_instance_id="runtime:a", execution_mode="PAPER", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA", heartbeat_ts=heartbeat_ts)
    save_runtime_heartbeat(engine, runtime_instance_id="runtime:b", execution_mode="PAPER", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA", heartbeat_ts=heartbeat_ts)
    assert fetch_latest_runtime_heartbeat(engine)["runtime_instance_id"] == "runtime:b"
