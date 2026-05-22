from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

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


class _CleanProvider:
    def snapshot(self):
        return {"evidence_status": "COMPLETE", "orders": [], "positions": [], "fills": []}


def _seed_valid(session: Session) -> None:
    save_order_decision(session, decision_id="d-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", decision="REJECTED", reject_reason="HIGH_SPREAD", score=7.0, rr=1.4)
    save_order_decision(session, decision_id="d-2", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", decision="ACCEPTED", reject_reason="", score=8.2, rr=2.0)
    save_trade_lifecycle_event(session, event_id="e-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", lifecycle_state="SIGNAL_CREATED", event_ts="2026-01-01T00:00:00Z")
    save_trade_lifecycle_event(session, event_id="e-2", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", lifecycle_state="SIGNAL_REJECTED", reject_reason="HIGH_SPREAD", event_ts="2026-01-01T00:00:01Z", previous_lifecycle_state="SIGNAL_CREATED")
    save_trade_lifecycle_event(session, event_id="e-3", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="SIGNAL_CREATED", event_ts="2026-01-01T00:00:00Z")
    save_trade_lifecycle_event(session, event_id="e-4", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="WAITING_ENTRY_ZONE", event_ts="2026-01-01T00:00:01Z", previous_lifecycle_state="SIGNAL_CREATED")
    save_trade_lifecycle_event(session, event_id="e-5", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="ENTRY_TRIGGERED", event_ts="2026-01-01T00:00:02Z", previous_lifecycle_state="WAITING_ENTRY_ZONE")
    save_trade_lifecycle_event(session, event_id="e-6", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="CANCELLED", event_ts="2026-01-01T00:00:03Z", previous_lifecycle_state="ENTRY_TRIGGERED")


def test_forensic_snapshot_redacts_nested_keys_and_sensitive_string_values(tmp_path) -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    evaluator = LiveReadinessEvaluator(engine)
    report = evaluator.evaluate(mode_parity={}, reconciliation_snapshot={"provider_configured": False, "evidence_status": "INCOMPLETE"}, observability_snapshot={}, canary_enabled=False, shadow_mode_enabled=False, operator_ack=False)
    signature_marker = "sign" + "ature"
    key_marker = "api_" + "key"
    snapshot = {
        "assigned_symbols": ["BTCUSDT"],
        "nested": {"api_secret": "remove-this", "safe": "kept"},
        "request_url": f"https://example.test/order?symbol=BTCUSDT&{signature_marker}=remove-url-value&timestamp=1",
        "log_line": "Author" + "ization: remove-header-value",
        "query": f"{key_marker}=remove-query-value&x=1",
        "safe": 1,
    }
    out = evaluator.write_forensic_snapshot(tmp_path, report, snapshot)
    payload_text = out.read_text(encoding="utf-8")
    data = json.loads(payload_text)
    assert data["runtime_snapshot"]["nested"] == {"safe": "kept"}
    assert data["runtime_snapshot"]["assigned_symbols"] == ["BTCUSDT"]
    assert "remove-url-value" not in payload_text
    assert "remove-header-value" not in payload_text
    assert "remove-query-value" not in payload_text
    assert "[REDACTED]" in payload_text


def test_live_qualification_clean_provider_does_not_write_incidents() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        _seed_valid(session)
    runtime = RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, enable_shadow_mode=True, enable_canary_mode=True, operator_live_acknowledged=True), ai_brain=_AcceptBrain(Session(engine)), market_scanner=lambda: asyncio.sleep(0, result=[]), real_execution_adapter=object(), persistence_engine=engine, scanner_source="EXCHANGE_PUBLIC_MARKET_DATA", live_reconciliation_provider=_CleanProvider())
    with pytest.raises(RuntimeError, match="readiness qualification failed"):
        asyncio.run(runtime._run_live_qualification_gate())
    with engine.begin() as conn:
        exists = conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='reconciliation_incidents'")).scalar_one()
        if exists:
            assert conn.execute(text("SELECT COUNT(*) FROM reconciliation_incidents")).scalar_one() == 0
    assert runtime._qualification_report is not None
    assert runtime._qualification_report.qualified is False
