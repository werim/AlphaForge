from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain
from alphaforge.live_readiness import LiveReadinessEvaluator
from alphaforge.persistence import init_db, save_order_decision, save_trade_lifecycle_event
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


class _AcceptBrain:
    def __init__(self, session: Session):
        self.session = session

    def before_real_order(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        class _Plan:
            decision = "ACCEPTED"
            reason = ""
            confidence = 0.9
            order_type = "MARKET"
            limit_price = None
            stop_price = None

        return {}, _Plan(), "ok"


def _seed_valid(session: Session) -> None:
    save_order_decision(session, decision_id="d-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", decision="REJECTED", reject_reason="HIGH_SPREAD", score=7.0, rr=1.4)
    save_order_decision(session, decision_id="d-2", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", decision="ACCEPTED", reject_reason="", score=8.2, rr=2.0)
    save_trade_lifecycle_event(session, event_id="e-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", lifecycle_state="SIGNAL_CREATED", event_ts="2026-01-01T00:00:00Z")
    save_trade_lifecycle_event(session, event_id="e-2", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", lifecycle_state="SIGNAL_REJECTED", reject_reason="HIGH_SPREAD", event_ts="2026-01-01T00:00:01Z", previous_lifecycle_state="SIGNAL_CREATED")
    save_trade_lifecycle_event(session, event_id="e-3", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="SIGNAL_CREATED", event_ts="2026-01-01T00:00:00Z")
    save_trade_lifecycle_event(session, event_id="e-4", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="WAITING_ENTRY_ZONE", event_ts="2026-01-01T00:00:01Z", previous_lifecycle_state="SIGNAL_CREATED")
    save_trade_lifecycle_event(session, event_id="e-5", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="ENTRY_TRIGGERED", event_ts="2026-01-01T00:00:02Z", previous_lifecycle_state="WAITING_ENTRY_ZONE")
    save_trade_lifecycle_event(session, event_id="e-6", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="CANCELLED", event_ts="2026-01-01T00:00:03Z", previous_lifecycle_state="ENTRY_TRIGGERED")


def test_live_readiness_pass_and_persistence() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        _seed_valid(s)
    evaluator = LiveReadinessEvaluator(engine)
    report = evaluator.evaluate(
        mode_parity={"evidence_status": "COMPLETE", "sample_count": 5, "min_sample_count": 3, "mismatch_count": 0, "missing_field_count": 0, "no_order_submission_verified": True},
        reconciliation_snapshot={"provider_configured": True, "evidence_status": "COMPLETE", "orphan_positions": 0, "orphan_orders": 0, "duplicate_fills": 0, "fail_closed_findings": 0},
        observability_snapshot={"evidence_status": "COMPLETE", "qualification_persistence_verified": True, "incident_persistence_verified": True, "forensic_export_verified": True, "sensitive_data_redaction_verified": True, "alert_delivery_verified": True, "rollback_evidence_status": "COMPLETE", "kill_switch_block_verified": True, "no_submit_on_kill_switch_verified": True, "fail_closed_reconciliation_verified": True, "repair_actions_non_mutating_verified": True},
        canary_enabled=True,
        shadow_mode_enabled=True,
        operator_ack=True,
    )
    assert report.qualified is True
    evaluator.persist_report(report)
    with engine.begin() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM live_readiness_reports")).scalar_one()
    assert count == 1


def test_live_readiness_detects_lifecycle_orphan() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        save_order_decision(s, decision_id="d-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", decision="REJECTED", reject_reason="HIGH_SPREAD", score=7.0, rr=1.4)
        save_order_decision(s, decision_id="d-2", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", decision="ACCEPTED", reject_reason="", score=8.2, rr=2.0)
        save_trade_lifecycle_event(s, event_id="e-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", lifecycle_state="ENTRY_TRIGGERED", event_ts="2026-01-01T00:00:00Z")
    evaluator = LiveReadinessEvaluator(engine)
    report = evaluator.evaluate(
        mode_parity={"evidence_status": "COMPLETE", "sample_count": 5, "min_sample_count": 3, "mismatch_count": 0, "missing_field_count": 0, "no_order_submission_verified": True},
        reconciliation_snapshot={"provider_configured": True, "evidence_status": "COMPLETE", "orphan_positions": 0, "orphan_orders": 0, "duplicate_fills": 0, "fail_closed_findings": 0},
        observability_snapshot={"evidence_status": "COMPLETE", "qualification_persistence_verified": True, "incident_persistence_verified": True, "forensic_export_verified": True, "sensitive_data_redaction_verified": True, "alert_delivery_verified": True, "rollback_evidence_status": "COMPLETE", "kill_switch_block_verified": True, "no_submit_on_kill_switch_verified": True, "fail_closed_reconciliation_verified": True, "repair_actions_non_mutating_verified": True},
        canary_enabled=True,
        shadow_mode_enabled=True,
        operator_ack=True,
    )
    assert report.qualified is False
    assert any(c.name == "lifecycle_no_orphans" and not c.passed for c in report.checks)


def test_runtime_live_mode_blocked_without_acknowledgement() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        _seed_valid(s)
        brain = _AcceptBrain(s)

        async def scanner():
            return []

        rt = RuntimeOrchestrator(
            config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, enable_shadow_mode=True, enable_canary_mode=True, operator_live_acknowledged=False),
            ai_brain=brain,
            market_scanner=scanner,
            real_execution_adapter=object(),
        )
        with pytest.raises(RuntimeError, match="LIVE mode blocked"):
            asyncio.run(rt._run_live_qualification_gate())


def test_forensic_snapshot_written(tmp_path) -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        _seed_valid(s)
    evaluator = LiveReadinessEvaluator(engine)
    report = evaluator.evaluate(
        mode_parity={"evidence_status": "COMPLETE", "sample_count": 5, "min_sample_count": 3, "mismatch_count": 0, "missing_field_count": 0, "no_order_submission_verified": True},
        reconciliation_snapshot={"provider_configured": True, "evidence_status": "COMPLETE", "orphan_positions": 0, "orphan_orders": 0, "duplicate_fills": 0, "fail_closed_findings": 0},
        observability_snapshot={"evidence_status": "COMPLETE", "qualification_persistence_verified": True, "incident_persistence_verified": True, "forensic_export_verified": True, "sensitive_data_redaction_verified": True, "alert_delivery_verified": True, "rollback_evidence_status": "COMPLETE", "kill_switch_block_verified": True, "no_submit_on_kill_switch_verified": True, "fail_closed_reconciliation_verified": True, "repair_actions_non_mutating_verified": True},
        canary_enabled=True,
        shadow_mode_enabled=True,
        operator_ack=True,
    )
    out = evaluator.write_forensic_snapshot(tmp_path, report, {"positions": 0})
    payload = json.loads(out.read_text())
    assert payload["version"] == "gen5"
    assert payload["report"]["qualified"] is True


def test_forensic_snapshot_redacts_secrets(tmp_path) -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    evaluator = LiveReadinessEvaluator(engine)
    report = evaluator.evaluate(
        mode_parity={},
        reconciliation_snapshot={"provider_configured": False, "evidence_status": "INCOMPLETE"},
        observability_snapshot={},
        canary_enabled=False,
        shadow_mode_enabled=False,
        operator_ack=False,
    )
    out = evaluator.write_forensic_snapshot(
        tmp_path,
        report,
        {
            "api_key": "x",
            "nested": {
                "api_secret": "y",
                "secret": "z",
                "signature": "deadbeef",
                "authorization": "Bearer token",
                "signed_url": "https://example.test/api?symbol=BTCUSDT&signature=abc123&api_key=zzz",
            },
            "headers": {"Authorization": "Bearer x", "X-MBX-APIKEY": "abc"},
            "safe": 1,
        },
    )
    data = json.loads(out.read_text())
    assert "api_key" not in data["runtime_snapshot"]
    assert "Authorization" not in data["runtime_snapshot"]["headers"]
    assert "api_secret" not in data["runtime_snapshot"]["nested"]
    assert "secret" not in data["runtime_snapshot"]["nested"]
    assert "signature" not in data["runtime_snapshot"]["nested"]
    assert "authorization" not in data["runtime_snapshot"]["nested"]
    assert "signed_url" not in data["runtime_snapshot"]["nested"]


def test_live_qualification_startup_does_not_persist_probe_reconciliation_incidents() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        _seed_valid(s)
    rt = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, enable_shadow_mode=True, enable_canary_mode=True, operator_live_acknowledged=True),
        ai_brain=_AcceptBrain(Session(engine)),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        real_execution_adapter=object(),
        persistence_engine=engine,
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        live_reconciliation_provider=_Provider({"evidence_status": "COMPLETE", "orders": [{"order_id": "x", "symbol": "BTCUSDT", "status": "OPEN"}], "positions": [], "fills": []}),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(rt._run_live_qualification_gate())
    with engine.begin() as conn:
        table_exists = conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='reconciliation_incidents'")).scalar_one()
    assert table_exists == 0


def test_runtime_live_qualification_fails_closed_on_missing_evidence() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        _seed_valid(s)
    rt = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, enable_shadow_mode=True, enable_canary_mode=True, operator_live_acknowledged=True),
        ai_brain=_AcceptBrain(Session(engine)),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        real_execution_adapter=object(),
        persistence_engine=engine,
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
    )
    with pytest.raises(RuntimeError, match="readiness qualification failed"):
        asyncio.run(rt._run_live_qualification_gate())
    assert rt._qualification_report is not None
    failed = {c.name: c.details for c in rt._qualification_report.checks if not c.passed}
    assert failed["mode_parity"] == "MODE_PARITY_UNVERIFIED"
    assert failed["live_reconciliation_provider"] == "LIVE_RECONCILIATION_PROVIDER_MISSING"
    assert failed["observability_coverage"] == "OBSERVABILITY_EVIDENCE_UNVERIFIED"
    assert failed["rollback_ready"] == "ROLLBACK_EVIDENCE_UNVERIFIED"


def test_runtime_live_qualification_persists_fail_closed_details() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        _seed_valid(s)
    rt = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, enable_shadow_mode=True, enable_canary_mode=True, operator_live_acknowledged=True),
        ai_brain=_AcceptBrain(Session(engine)),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        real_execution_adapter=object(),
        persistence_engine=engine,
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
    )
    with pytest.raises(RuntimeError):
        asyncio.run(rt._run_live_qualification_gate())
    with engine.begin() as conn:
        payload = conn.execute(text("SELECT report_payload FROM live_readiness_reports ORDER BY id DESC LIMIT 1")).scalar_one()
    data = json.loads(payload)
    failed = {c["name"]: c["details"] for c in data["checks"] if not c["passed"]}
    assert failed["mode_parity"] == "MODE_PARITY_UNVERIFIED"
    assert failed["live_reconciliation_provider"] == "LIVE_RECONCILIATION_PROVIDER_MISSING"

class _Provider:
    def __init__(self, payload):
        self.payload = payload

    def snapshot(self):
        return dict(self.payload)


def test_runtime_live_qualification_ignores_provider_optimistic_orphan_order_counter() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        _seed_valid(s)
    rt = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, enable_shadow_mode=True, enable_canary_mode=True, operator_live_acknowledged=True),
        ai_brain=_AcceptBrain(Session(engine)),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        real_execution_adapter=object(),
        persistence_engine=engine,
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        live_reconciliation_provider=_Provider({"evidence_status": "COMPLETE", "orphan_orders": 0, "orphan_positions": 0, "duplicate_fills": 0, "orders": [{"order_id": "x", "symbol": "BTCUSDT", "status": "OPEN"}], "positions": [], "fills": []}),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(rt._run_live_qualification_gate())
    assert any(c.name == "reconciliation_no_orphans" and not c.passed for c in rt._qualification_report.checks)


def test_runtime_live_qualification_ignores_provider_optimistic_orphan_position_counter() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        _seed_valid(s)
    rt = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, enable_shadow_mode=True, enable_canary_mode=True, operator_live_acknowledged=True),
        ai_brain=_AcceptBrain(Session(engine)),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        real_execution_adapter=object(),
        persistence_engine=engine,
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        live_reconciliation_provider=_Provider({"evidence_status": "COMPLETE", "orphan_orders": 0, "orphan_positions": 0, "duplicate_fills": 0, "orders": [], "positions": [{"symbol": "XRPUSDT", "qty": 1.0}], "fills": []}),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(rt._run_live_qualification_gate())
    assert any(c.name == "reconciliation_no_orphans" and not c.passed for c in rt._qualification_report.checks)
