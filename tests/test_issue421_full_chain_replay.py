"""Frozen PAPER input through the production decision, resolver and audit path."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from alphaforge.ai_brain import AIBrain
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import resolve_campaign_positions
from alphaforge.live_readiness import LiveReadinessEvaluator
from alphaforge.persistence import (
    init_db, save_rejected_decision_artifact, save_trade_lifecycle_event,
)
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.system_audit_store import ingest_audit_evidence


FIXTURES = Path(__file__).parent / "fixtures" / "system_golden"


def _replay(tmp_path: Path, monkeypatch, scenario: str) -> dict:
    tmp_path.mkdir()
    fixture = json.loads((FIXTURES / f"{scenario}.json").read_text())
    frozen_market = fixture["frozen_market_event"]["market_context"]
    event_time = fixture["frozen_market_event"]["timestamp"]
    event_dt = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    db_path = tmp_path / "campaign.sqlite3"
    engine = init_db(f"sqlite+pysqlite:///{db_path}")
    with engine.begin() as conn:
        campaign = create_campaign(
            conn, release_id="p2d-replay", duration_days=1,
            symbols=[fixture["symbol"]], intervals=["1m"],
        )
        run = start_or_resume_campaign(conn, campaign.campaign_id)
        for table, key in (
            ("setup_expectancy_stats", "setup"),
            ("regime_expectancy_stats", "regime"),
            ("symbol_expectancy_stats", "symbol"),
        ):
            value = {
                "setup": frozen_market["setup"],
                "regime": frozen_market["mtf"]["regime"]["regime"] if "mtf" in frozen_market else "TRENDING",
                "symbol": fixture["symbol"],
            }[key]
            conn.execute(text(
                f"INSERT INTO {table} ({key},samples,expectancy) VALUES (:value,100,0.2)"
            ), {"value": value})

    monkeypatch.setattr("alphaforge.runtime.time.time", lambda: event_dt.timestamp())
    monkeypatch.setattr("alphaforge.runtime.canonical_utc_timestamp", lambda: event_time)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def persist_event(event):
        with factory.begin() as session:
            assert save_trade_lifecycle_event(
                session, signal_id=event["signal_id"], symbol=event["symbol"],
                mode=event["mode"], lifecycle_state=event["lifecycle_state"],
                previous_lifecycle_state=event["previous_lifecycle_state"],
                event_ts=event["timestamp"], payload=event["details"],
                reject_reason=event["details"].get("reason"),
                execution_ctx=event["details"].get("execution_ctx", {}),
            )

    def persist_reject(payload):
        with factory() as session:
            assert save_rejected_decision_artifact(
                session, decision_id=payload["reject_decision_id"],
                signal_id=payload["signal_id"], symbol=payload["symbol"],
                mode="PAPER", phase=payload.get("phase", "final"),
                reject_reason=payload["reason"], score=payload.get("score"),
                rr=payload.get("rr"), effective_rr=payload.get("effective_rr"),
                execution_ctx=payload.get("execution_ctx", {}),
            )
            session.commit()

    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=AIBrain(session_factory=factory),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        persistence_engine=engine, scanner_source="FROZEN_REPLAY",
        on_lifecycle_event=persist_event, on_reject_persist=persist_reject,
    )
    runtime._campaign_id = campaign.campaign_id
    runtime._burnin_run_id = run["burnin_run_id"]
    market = {
        **fixture["frozen_market_event"]["market_context"],
        "signal_id": fixture["frozen_market_event"]["event_id"],
        "market_ts": event_dt.timestamp(),
    }
    asyncio.run(runtime._process_symbol(SimpleNamespace(
        symbol=fixture["symbol"], regime_hint="TRENDING", diagnostics={"inputs": market},
    )))

    with engine.connect() as conn:
        decision = conn.execute(text(
            "SELECT decision,effective_rr,signal_id,raw_rr,entry,sl,tp,reject_reason,reject_flags,"
            "portfolio_risk_state,portfolio_equity,diagnostics_json "
            "FROM decision_evidence WHERE run_id=:run"
        ), {"run": run["burnin_run_id"]}).mappings().one()
        position = conn.execute(text(
            "SELECT trade_id,signal_id,source_decision_id,status,simulated_fill,target "
            "FROM burnin_pending_position_outcomes WHERE campaign_id=:campaign"
        ), {"campaign": campaign.campaign_id}).mappings().one_or_none()
        observation = conn.execute(text(
            "SELECT metrics_json FROM burnin_observations WHERE burnin_run_id=:run "
            "AND decision IN ('ACCEPTED','REJECTED')"
        ), {"run": run["burnin_run_id"]}).mappings().one()
    assert decision["decision"] == fixture["expected"]["decision"]
    assert decision["sl"] == market["sl"] and decision["tp"] == market["tp"]
    diagnostics = json.loads(decision["diagnostics_json"])
    assert diagnostics["geometry_status"] == fixture["expected"]["geometry_status"]
    assert diagnostics["campaign_id"] == campaign.campaign_id
    assert json.loads(observation["metrics_json"])["campaign_id"] == campaign.campaign_id
    if fixture["expected"]["decision"] == "ACCEPT":
        assert json.loads(decision["reject_flags"] or "[]") == fixture["expected"]["failed_gates"]
        adverse_side = 1 if market["side"] == "LONG" else -1
        expected_fill = market["entry"] * (
            1 + adverse_side * market["expected_slippage_pct"]
        )
        assert decision["entry"] == pytest.approx(expected_fill)
        assert decision["raw_rr"] > runtime.config.min_effective_rr
        assert decision["effective_rr"] >= runtime.config.min_effective_rr
        assert decision["portfolio_risk_state"] == fixture["expected"]["portfolio_state"]
        assert decision["portfolio_equity"] == runtime.config.paper_initial_equity
        assert json.loads(decision["diagnostics_json"])["expected_fill"] == decision["entry"]
        assert position is not None and position["signal_id"] == decision["signal_id"]
        assert position["source_decision_id"]
        assert position["status"] == "OPEN"
        assert runtime._canonical_final_decision_recorded(decision["signal_id"]) is True
        asyncio.run(runtime._process_symbol(SimpleNamespace(
            symbol=fixture["symbol"], regime_hint="TRENDING", diagnostics={"inputs": market},
        )))
        assert runtime.metrics.executions == 1
        assert runtime.metrics.finalized_signal_replays_skipped == 1
        with engine.begin() as conn:
            resolved = resolve_campaign_positions(
                conn, campaign.campaign_id,
                {position["trade_id"]: fixture["frozen_market_event"]["resolver_candles"]},
                now=fixture["frozen_market_event"]["resolver_now"],
            )
        terminal_key = {
            "TP_HIT": "tp", "SL_HIT": "sl", "AMBIGUOUS_INTRABAR": "ambiguous",
        }[fixture["expected"]["resolver"]]
        assert resolved[terminal_key] == resolved["closed"] == 1
        with engine.connect() as conn:
            outcome = conn.execute(text(
                "SELECT exit_reason,evidence_complete,net_r FROM burnin_trade_outcomes WHERE trade_id=:trade"
            ), {"trade": position["trade_id"]}).mappings().one()
        assert outcome["exit_reason"] == fixture["expected"]["resolver"]
        ambiguous = terminal_key == "ambiguous"
        assert outcome["evidence_complete"] == (0 if ambiguous else 1)
        assert (outcome["net_r"] > 0) == (fixture["expected"]["resolver"] == "TP_HIT")
    else:
        assert position is None and runtime.metrics.executions == 0
        assert decision["reject_reason"] == fixture["expected"]["failed_gates"][0]
        assert fixture["expected"]["failed_gates"][0] in json.loads(decision["reject_flags"] or "[]")
        if fixture["expected"]["effective_rr_status"] == "BELOW_THRESHOLD":
            assert decision["effective_rr"] < runtime.config.min_effective_rr
        else:
            assert decision["effective_rr"] >= runtime.config.min_effective_rr
        assert fixture["expected"]["portfolio_state"] == "NOT_EVALUATED"
        assert decision["portfolio_risk_state"] is None
        assert diagnostics["expected_fill"] == decision["entry"]
        with engine.connect() as conn:
            label = conn.execute(text(
                "SELECT status,signal_id,reject_reason,reject_decision_id FROM burnin_pending_reject_labels "
                "WHERE campaign_id=:campaign"
            ), {"campaign": campaign.campaign_id}).mappings().one()
            final = conn.execute(text(
                "SELECT decision_id FROM order_decisions WHERE signal_id=:signal "
                "AND decision='REJECTED' AND phase='final'"
            ), {"signal": decision["signal_id"]}).mappings().one()
        assert label["status"] == "PENDING"
        assert label["signal_id"] == decision["signal_id"]
        assert label["reject_reason"] == decision["reject_reason"]
        assert label["reject_decision_id"] == final["decision_id"]
        outcome = None
    readiness = LiveReadinessEvaluator(
        engine, campaign_id=campaign.campaign_id,
        burnin_run_id=run["burnin_run_id"], evidence_mode="PAPER",
    ).evaluate(
        mode_parity={}, reconciliation_snapshot={}, observability_snapshot={},
        canary_enabled=False, shadow_mode_enabled=False, operator_ack=False,
    )
    by_name = {check.name: check for check in readiness.checks}
    assert by_name["phase2_decision_evidence_rows_present"].passed
    assert by_name[
        "phase2_accept_evidence_present" if outcome else "phase2_reject_evidence_present"
    ].passed
    assert readiness.qualified is False
    assert readiness.verdict != "READY_FOR_OPERATOR_REVIEW"

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    audit = sqlite3.connect(tmp_path / "audit.sqlite3")
    try:
        first = ingest_audit_evidence(source, audit, burnin_run_id=run["burnin_run_id"])
        second = ingest_audit_evidence(source, audit, burnin_run_id=run["burnin_run_id"])
        assert first["source_decisions"] == first["envelopes_added"] == 1
        assert first["outcomes_added"] == (1 if outcome else 0)
        assert first["shadow_decisions_added"] == (0 if outcome else 1)
        assert second["envelopes_added"] == second["outcomes_added"] == 0
        audit_row = audit.execute(
            "SELECT decision,campaign_id,burnin_run_id,reject_reason "
            "FROM audit_decision_envelopes"
        ).fetchone()
        assert audit_row[0] == fixture["expected"]["decision"]
        assert audit_row[1:3] == (campaign.campaign_id, run["burnin_run_id"])
        if outcome:
            audited_outcome = audit.execute(
                "SELECT outcome_status,authoritative FROM audit_outcomes"
            ).fetchone()
            assert tuple(audited_outcome) == (
                fixture["expected"]["resolver"],
                0 if fixture["expected"]["resolver"] == "AMBIGUOUS_INTRABAR" else 1,
            )
        else:
            assert audit_row[3] == fixture["expected"]["failed_gates"][0]
    finally:
        source.close()
        audit.close()
        engine.dispose()
    return {
        "decision": decision["decision"], "effective_rr": round(decision["effective_rr"], 8),
        "exit_reason": outcome["exit_reason"] if outcome else None,
        "net_r": round(outcome["net_r"], 8) if outcome else None,
        "audit_decision": audit_row[0],
    }


def test_profitable_long_replays_same_full_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "profitable_long")
    second = _replay(tmp_path / "second", monkeypatch, "profitable_long")
    assert first == second


def test_profitable_short_replays_same_full_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "profitable_short")
    second = _replay(tmp_path / "second", monkeypatch, "profitable_short")
    assert first == second


def test_losing_accept_replays_same_full_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "losing_accept")
    second = _replay(tmp_path / "second", monkeypatch, "losing_accept")
    assert first == second


def test_ambiguous_tp_sl_replays_non_authoritative_outcome_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "ambiguous_tp_sl")
    second = _replay(tmp_path / "second", monkeypatch, "ambiguous_tp_sl")
    assert first == second


def test_high_spread_replays_same_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "high_spread")
    second = _replay(tmp_path / "second", monkeypatch, "high_spread")
    assert first == second


def test_low_effective_rr_replays_same_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "low_effective_rr")
    second = _replay(tmp_path / "second", monkeypatch, "low_effective_rr")
    assert first == second


def test_unavailable_execution_context_replays_same_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "execution_context_unavailable")
    second = _replay(tmp_path / "second", monkeypatch, "execution_context_unavailable")
    assert first == second


def test_final_reject_from_another_campaign_cannot_skip_same_signal(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID", raising=False)
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'scope.sqlite3'}")
    with engine.begin() as conn:
        first = create_campaign(
            conn, release_id="first", duration_days=1,
            symbols=["BTCUSDT"], intervals=["1m"],
        )
        first_run = start_or_resume_campaign(conn, first.campaign_id)
        second = create_campaign(
            conn, release_id="second", duration_days=1,
            symbols=["BTCUSDT"], intervals=["1m"],
        )
        second_run = start_or_resume_campaign(conn, second.campaign_id)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=AIBrain(session_factory=factory),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        persistence_engine=engine,
    )
    runtime._campaign_id = first.campaign_id
    runtime._burnin_run_id = first_run["burnin_run_id"]
    signal_id = "golden:reused-signal"
    decision_id = runtime._canonical_reject_decision_id({
        "signal_id": signal_id, "symbol": "BTCUSDT",
    })
    with factory() as session:
        assert save_rejected_decision_artifact(
            session, decision_id=decision_id, signal_id=signal_id,
            symbol="BTCUSDT", mode="PAPER", phase="final",
            reject_reason="SPREAD_TOO_HIGH", execution_ctx={},
        )
        session.commit()
    assert runtime._canonical_final_decision_recorded(signal_id) is True
    runtime._campaign_id = second.campaign_id
    runtime._burnin_run_id = second_run["burnin_run_id"]
    monkeypatch.setenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID", first.campaign_id)
    assert runtime._canonical_final_decision_recorded(signal_id) is False
    scoped_reject = runtime._canonical_reject_payload({
        "signal_id": "golden:forged-campaign", "symbol": "BTCUSDT",
        "reason": "SPREAD_TOO_HIGH", "campaign_id": first.campaign_id,
        "runtime_identity": first.campaign_id,
    })
    assert scoped_reject["campaign_id"] == second.campaign_id
    assert scoped_reject["runtime_identity"] == second.campaign_id
    runtime._burnin_run_id = None
    assert runtime._canonical_final_decision_recorded(signal_id) is None
    engine.dispose()
