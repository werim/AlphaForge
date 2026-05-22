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

    def score_signal(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        class _Score:
            total_score = 0.91
        return _Score()

    def choose_order_plan(self, signal_payload, market_ctx, score_ctx):
        class _Plan:
            decision = "ACCEPTED"
            reason = ""
            confidence = 0.9
            order_type = "MARKET"
            limit_price = None
            stop_price = None
        return _Plan()

    def explain_decision(self, signal_payload, score_ctx, order_plan):
        return "ok"


class _CleanProvider:
    def snapshot(self):
        return {"evidence_status": "COMPLETE", "orders": [], "positions": [], "fills": []}


class _DirtyProvider:
    def snapshot(self):
        return {
            "evidence_status": "COMPLETE",
            "orders": [{"order_id": "o-1", "symbol": "BTCUSDT", "status": "OPEN", "created_at": "2020-01-01T00:00:00Z"}],
            "positions": [{"symbol": "BTCUSDT", "qty": "1"}],
            "fills": [{"trade_id": "t-1", "symbol": "BTCUSDT"}, {"trade_id": "t-1", "symbol": "BTCUSDT"}],
        }


def test_live_qualification_fail_closed_with_reconciliation_findings_and_no_incident_persistence() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        _seed_valid(session)
    runtime = RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, enable_shadow_mode=True, enable_canary_mode=True, operator_live_acknowledged=True), ai_brain=_AcceptBrain(Session(engine)), market_scanner=lambda: asyncio.sleep(0, result=[]), real_execution_adapter=object(), persistence_engine=engine, scanner_source="EXCHANGE_PUBLIC_MARKET_DATA", live_reconciliation_provider=_DirtyProvider())
    with pytest.raises(RuntimeError, match="readiness qualification failed"):
        asyncio.run(runtime._run_live_qualification_gate())
    with engine.begin() as conn:
        exists = conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='reconciliation_incidents'")).scalar_one()
        if exists:
            assert conn.execute(text("SELECT COUNT(*) FROM reconciliation_incidents")).scalar_one() == 0
    assert runtime._qualification_report is not None
    assert runtime._qualification_report.qualified is False
    report_map = {check.name: check for check in runtime._qualification_report.checks}
    assert report_map["reconciliation_no_orphans"].passed is False
    assert report_map["duplicate_execution_free"].passed is False


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
    snapshot = {
        "nested": {
            "api_key": "nested-api-key-value",
            "api_secret": "nested-api-secret-value",
            "secret": "nested-secret-value",
            "signature": "nested-signature-value",
            "authorization": "Bearer nested-auth-value",
            "signed_payload": "opaque-signed-payload-value",
            "safe": "kept",
        },
        "values": [
            "api_key=value-api-key",
            "api_secret=value-api-secret",
            "secret=value-secret",
            "signature=value-signature",
            "authorization=Bearer value-auth",
        ],
        "request_url": "https://example.test/order?symbol=BTCUSDT&signature=url-signature&timestamp=1",
        "signed_url": "https://example.test/order?symbol=BTCUSDT&signed=value-signed-query&timestamp=1",
        "assigned_symbols": ["BTCUSDT", "ETHUSDT"],
        "safe": 1,
    }
    out = evaluator.write_forensic_snapshot(tmp_path, report, snapshot)
    payload_text = out.read_text(encoding="utf-8")
    data = json.loads(payload_text)
    runtime_snapshot = data["runtime_snapshot"]
    assert runtime_snapshot["nested"] == {"safe": "kept"}
    assert runtime_snapshot["assigned_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert "signed_url" in runtime_snapshot
    assert "signed=[REDACTED]" in runtime_snapshot["signed_url"]
    assert "signature=[REDACTED]" in runtime_snapshot["request_url"]
    for leaked_value in (
        "nested-api-key-value",
        "nested-api-secret-value",
        "nested-secret-value",
        "nested-signature-value",
        "nested-auth-value",
        "opaque-signed-payload-value",
        "value-api-key",
        "value-api-secret",
        "value-secret",
        "value-signature",
        "value-auth",
        "url-signature",
        "value-signed-query",
    ):
        assert leaked_value not in payload_text
    assert payload_text.count("[REDACTED]") >= 7


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


def test_mode_parity_evidence_does_not_mutate_persistence_and_is_deterministic() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        _seed_valid(session)
        session.commit()
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, enable_shadow_mode=True, enable_canary_mode=True, operator_live_acknowledged=True),
        ai_brain=_AcceptBrain(Session(engine)),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        real_execution_adapter=object(),
        persistence_engine=engine,
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        live_reconciliation_provider=_CleanProvider(),
    )

    with engine.begin() as conn:
        tables = ["signals", "order_decisions", "ai_decision_features", "trade_lifecycle_events"]
        optional_tables = ["rejected_signal_reviews", "reconciliation_incidents"]
        before = {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one() for t in tables}
        for t in optional_tables:
            exists = conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=:name"), {"name": t}).scalar_one()
            if exists:
                before[t] = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()

    first = runtime._build_mode_parity_evidence(min_sample_count=3)
    second = runtime._build_mode_parity_evidence(min_sample_count=3)

    with engine.begin() as conn:
        after = {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one() for t in before.keys()}
    assert before == after
    assert first["no_order_submission_verified"] is True

    first_no_time = {k: v for k, v in first.items() if k != "generated_at"}
    second_no_time = {k: v for k, v in second.items() if k != "generated_at"}
    assert first_no_time == second_no_time
    assert [s["sample_id"] for s in first_no_time["samples"]] == [s["sample_id"] for s in second_no_time["samples"]]


def test_mode_parity_evidence_uses_expected_mode_labels_for_paper_and_live_precheck() -> None:
    class _ProbeBrain(_AcceptBrain):
        def __init__(self, session: Session):
            super().__init__(session)
            self.calls: list[tuple[str, str]] = []

        def score_signal(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
            self.calls.append((str(signal_payload.get("mode")), str(market_ctx.get("mode"))))
            return super().score_signal(signal_payload, market_ctx, regime_ctx, stats_ctx)

    engine = init_db("sqlite+pysqlite:///:memory:")
    brain = _ProbeBrain(Session(engine))
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE),
        ai_brain=brain,
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        real_execution_adapter=object(),
        persistence_engine=engine,
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        live_reconciliation_provider=_CleanProvider(),
    )

    evidence = runtime._build_mode_parity_evidence(min_sample_count=3)
    assert evidence["sample_count"] == 3
    paper_calls = [call for call in brain.calls if call == ("PAPER", "PAPER")]
    live_precheck_calls = [call for call in brain.calls if call == ("LIVE_PRECHECK", "LIVE_PRECHECK")]
    assert len(paper_calls) == 3
    assert len(live_precheck_calls) == 3
