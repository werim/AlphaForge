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
        mode_parity={"paper_live_decision_path": True, "paper_live_reject_path": True},
        reconciliation_snapshot={"orphan_positions": 0, "orphan_orders": 0, "duplicate_fills": 0},
        observability_snapshot={"alerts_configured": True, "forensic_exports": True, "rollback_ready": True},
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
        mode_parity={"paper_live_decision_path": True},
        reconciliation_snapshot={"orphan_positions": 0, "orphan_orders": 0, "duplicate_fills": 0},
        observability_snapshot={"alerts_configured": True, "forensic_exports": True, "rollback_ready": True},
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
        mode_parity={"paper_live_decision_path": True, "paper_live_reject_path": True},
        reconciliation_snapshot={"orphan_positions": 0, "orphan_orders": 0, "duplicate_fills": 0},
        observability_snapshot={"alerts_configured": True, "forensic_exports": True, "rollback_ready": True},
        canary_enabled=True,
        shadow_mode_enabled=True,
        operator_ack=True,
    )
    out = evaluator.write_forensic_snapshot(tmp_path, report, {"positions": 0})
    payload = json.loads(out.read_text())
    assert payload["version"] == "gen5"
    assert payload["report"]["qualified"] is True
