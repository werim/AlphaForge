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
from alphaforge.burnin_resolver import resolve_campaign_batch, resolve_campaign_positions
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

    async def preserve_candidates(candidates):
        return candidates

    lifecycle_events = []

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
        lifecycle_events.append(event)

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
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            **fixture["frozen_market_event"].get("runtime_config", {}),
        ),
        ai_brain=AIBrain(session_factory=factory),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        persistence_engine=engine, scanner_source="FROZEN_REPLAY",
        on_lifecycle_event=persist_event, on_reject_persist=persist_reject,
        selected_candidate_enricher=(
            preserve_candidates
            if fixture["frozen_market_event"].get("geometry_gate_required")
            else None
        ),
    )
    runtime._campaign_id = campaign.campaign_id
    runtime._burnin_run_id = run["burnin_run_id"]
    if fixture["frozen_market_event"].get("reconciliation_provider_outage"):
        class OutageProvider:
            def snapshot(self):
                raise TimeoutError("frozen provider outage")

        runtime.live_reconciliation_provider = OutageProvider()
        asyncio.run(runtime._run_reconciliation_once())
        assert runtime._reconciliation_status == "EXCHANGE_STATE_UNKNOWN"
        assert runtime._execution_reconciliation_blocked()
    if fixture["frozen_market_event"].get("reconciliation_orphan_position"):
        class OrphanPositionProvider:
            def snapshot(self):
                return {
                    "evidence_status": "COMPLETE",
                    "authenticated": True,
                    "input_source": "FROZEN_REPLAY",
                    "orders": [],
                    "positions": [{"symbol": fixture["symbol"], "qty": 0.25}],
                    "fills": [],
                }

        runtime.live_reconciliation_provider = OrphanPositionProvider()
        asyncio.run(runtime._run_reconciliation_once())
        assert runtime._reconciliation_status == "DIRTY"
        assert runtime._fail_closed_reason == "ORPHAN_POSITION_DETECTED"
        assert runtime._execution_reconciliation_blocked()
    execution_override = fixture["frozen_market_event"].get("paper_execution_override")
    if execution_override:
        simulated_execution = RuntimeOrchestrator._simulate_paper_execution

        def replay_execution(self, symbol, decision, market_ctx):
            return {
                **simulated_execution(self, symbol, decision, market_ctx),
                **execution_override,
            }

        monkeypatch.setattr(RuntimeOrchestrator, "_simulate_paper_execution", replay_execution)
    for initial in fixture["frozen_market_event"].get("initial_positions", []):
        runtime._active_positions[initial["symbol"]] = initial["notional"]
        runtime._active_position_sides[initial["symbol"]] = initial["side"]
    market = {
        **fixture["frozen_market_event"]["market_context"],
        "signal_id": fixture["frozen_market_event"]["event_id"],
        "market_ts": event_dt.timestamp()
        + fixture["frozen_market_event"].get("market_ts_offset_sec", 0.0),
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
            "SELECT trade_id,signal_id,source_decision_id,status,simulated_fill,target,quantity,notional,source_provenance_json "
            "FROM burnin_pending_position_outcomes WHERE campaign_id=:campaign"
        ), {"campaign": campaign.campaign_id}).mappings().one_or_none()
        observation = conn.execute(text(
            "SELECT metrics_json FROM burnin_observations WHERE burnin_run_id=:run "
            "AND decision IN ('ACCEPTED','REJECTED') "
            "AND COALESCE(json_extract(metrics_json,'$.observation_kind'),"
            "'CANONICAL_DECISION')='CANONICAL_DECISION'"
        ), {"run": run["burnin_run_id"]}).mappings().one()
    assert decision["decision"] == fixture["expected"]["decision"]
    assert decision["sl"] == market["sl"] and decision["tp"] == market["tp"]
    diagnostics = json.loads(decision["diagnostics_json"])
    assert diagnostics["geometry_status"] == fixture["expected"]["geometry_status"]
    assert diagnostics["campaign_id"] == campaign.campaign_id
    assert json.loads(observation["metrics_json"])["campaign_id"] == campaign.campaign_id
    outcome = None
    resolved_status = None
    resolved_net_r = None
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
        if fixture["expected"]["expected_fill_status"] == "PARTIAL":
            partial_opened = [
                event for event in lifecycle_events
                if event["lifecycle_state"] == "POSITION_OPENED"
                and event["details"].get("fill_state") == "partial"
            ]
            assert len(partial_opened) == 1
            assert not any(event["lifecycle_state"] == "ERROR" for event in lifecycle_events)
            assert runtime._active_positions[fixture["symbol"]] == pytest.approx(5.001)
            assert position["quantity"] == pytest.approx(0.05)
            assert position["notional"] == pytest.approx(5.001)
            assert json.loads(position["source_provenance_json"])["fill_state"] == "PARTIAL"
        assert runtime._canonical_final_decision_recorded(decision["signal_id"]) is True
        asyncio.run(runtime._process_symbol(SimpleNamespace(
            symbol=fixture["symbol"], regime_hint="TRENDING", diagnostics={"inputs": market},
        )))
        assert runtime.metrics.executions == 1
        assert runtime.metrics.finalized_signal_replays_skipped == 1
        if fixture["expected"]["resolver"] == "PENDING":
            assert position["status"] == "OPEN"
        else:
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
            resolved_status = outcome["exit_reason"]
            resolved_net_r = outcome["net_r"]
            ambiguous = terminal_key == "ambiguous"
            assert outcome["evidence_complete"] == (0 if ambiguous else 1)
            assert (outcome["net_r"] > 0) == (fixture["expected"]["resolver"] == "TP_HIT")
    else:
        assert position is None and runtime.metrics.executions == 0
        assert decision["reject_reason"] == fixture["expected"]["failed_gates"][0]
        assert set(fixture["expected"]["failed_gates"]) <= set(
            json.loads(decision["reject_flags"] or "[]")
        )
        if fixture["expected"]["effective_rr_status"] == "NOT_APPLICABLE":
            assert decision["effective_rr"] is None
        elif fixture["expected"]["effective_rr_status"] == "BELOW_THRESHOLD":
            assert decision["effective_rr"] < runtime.config.min_effective_rr
        else:
            assert decision["effective_rr"] >= runtime.config.min_effective_rr
        if fixture["expected"]["portfolio_state"] == "NOT_EVALUATED":
            assert decision["portfolio_risk_state"] is None
        else:
            assert decision["portfolio_risk_state"] == fixture["expected"]["portfolio_state"]
        if fixture["expected"]["effective_rr_status"] == "NOT_APPLICABLE":
            assert diagnostics.get("expected_fill") is None
        else:
            assert diagnostics["expected_fill"] == decision["entry"]
        with engine.connect() as conn:
            label = conn.execute(text(
                "SELECT status,signal_id,reject_reason,reject_decision_id FROM burnin_pending_reject_labels "
                "WHERE campaign_id=:campaign"
            ), {"campaign": campaign.campaign_id}).mappings().one_or_none()
            final = conn.execute(text(
                "SELECT decision_id FROM order_decisions WHERE signal_id=:signal "
                "AND decision='REJECTED' AND phase='final'"
            ), {"signal": decision["signal_id"]}).mappings().one()
        if fixture["expected"]["resolver"] == "REJECT_LABEL_PENDING":
            assert label is not None and label["status"] == "PENDING"
            assert label["signal_id"] == decision["signal_id"]
            assert label["reject_reason"] == decision["reject_reason"]
            assert label["reject_decision_id"] == final["decision_id"]
        else:
            assert fixture["expected"]["resolver"] in {
                "INELIGIBLE", "REJECT_LABEL_TP", "REJECT_LABEL_SL",
            }
            if fixture["expected"]["resolver"] == "INELIGIBLE":
                assert label is None
            else:
                assert label is not None and label["status"] == "PENDING"
                expected_label = {
                    "REJECT_LABEL_TP": "TP_BEFORE_SL",
                    "REJECT_LABEL_SL": "SL_BEFORE_TP",
                }[fixture["expected"]["resolver"]]
                with engine.begin() as conn:
                    counts = resolve_campaign_batch(
                        conn, campaign.campaign_id,
                        {fixture["symbol"]: fixture["frozen_market_event"]["resolver_candles"]},
                        now=fixture["frozen_market_event"]["resolver_now"],
                    )
                assert counts["resolved"] == 1
                with engine.connect() as conn:
                    outcome = conn.execute(text(
                        "SELECT forward_label,evidence_complete,hypothetical_net_r_after_costs "
                        "FROM burnin_reject_outcomes WHERE burnin_run_id=:run"
                    ), {"run": run["burnin_run_id"]}).mappings().one()
                assert outcome["forward_label"] == expected_label
                assert outcome["evidence_complete"] == 1
                resolved_status = outcome["forward_label"]
                resolved_net_r = outcome["hypothetical_net_r_after_costs"]
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
        "phase2_accept_evidence_present"
        if fixture["expected"]["decision"] == "ACCEPT"
        else "phase2_reject_evidence_present"
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
        assert first["shadow_decisions_added"] == (
            0 if fixture["expected"]["decision"] == "ACCEPT" else 1
        )
        assert second["envelopes_added"] == second["outcomes_added"] == 0
        audit_row = audit.execute(
            "SELECT decision,campaign_id,burnin_run_id,reject_reason "
            "FROM audit_decision_envelopes"
        ).fetchone()
        assert audit_row[0] == fixture["expected"]["decision"]
        assert audit_row[1:3] == (campaign.campaign_id, run["burnin_run_id"])
        if fixture["expected"]["decision"] == "ACCEPT":
            audited_outcome = audit.execute(
                "SELECT outcome_status,authoritative FROM audit_outcomes"
            ).fetchone()
            if outcome:
                assert tuple(audited_outcome) == (
                    fixture["expected"]["resolver"],
                    0 if fixture["expected"]["resolver"] == "AMBIGUOUS_INTRABAR" else 1,
                )
            else:
                assert fixture["expected"]["resolver"] == "PENDING"
                assert audited_outcome is None
        else:
            assert audit_row[3] == fixture["expected"]["failed_gates"][0]
            if outcome:
                audited_outcome = audit.execute(
                    "SELECT outcome_status,authoritative FROM audit_outcomes"
                ).fetchone()
                assert tuple(audited_outcome) == (resolved_status, 1)
    finally:
        source.close()
        audit.close()
        engine.dispose()
    return {
        "decision": decision["decision"],
        "effective_rr": (
            round(decision["effective_rr"], 8)
            if decision["effective_rr"] is not None else None
        ),
        "exit_reason": resolved_status,
        "net_r": round(resolved_net_r, 8) if resolved_net_r is not None else None,
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


def test_high_slippage_replays_same_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "high_slippage")
    second = _replay(tmp_path / "second", monkeypatch, "high_slippage")
    assert first == second


def test_thin_liquidity_replays_same_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "thin_liquidity")
    second = _replay(tmp_path / "second", monkeypatch, "thin_liquidity")
    assert first == second


def test_multi_gate_reject_replays_complete_gate_set_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "multi_gate_reject")
    second = _replay(tmp_path / "second", monkeypatch, "multi_gate_reject")
    assert first == second


def test_portfolio_overexposure_replays_same_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "portfolio_overexposure")
    second = _replay(tmp_path / "second", monkeypatch, "portfolio_overexposure")
    assert first == second


def test_incomplete_geometry_replays_ineligible_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "incomplete_geometry")
    second = _replay(tmp_path / "second", monkeypatch, "incomplete_geometry")
    assert first == second


def test_correct_reject_replays_stop_first_outcome_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "correct_reject")
    second = _replay(tmp_path / "second", monkeypatch, "correct_reject")
    assert first == second


def test_false_reject_replays_target_first_outcome_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "false_reject")
    second = _replay(tmp_path / "second", monkeypatch, "false_reject")
    assert first == second


def test_delayed_resolver_replays_open_position_without_outcome_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "delayed_resolver")
    second = _replay(tmp_path / "second", monkeypatch, "delayed_resolver")
    assert first == second


def test_partial_fill_replays_one_canonical_open_position_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "partial_fill")
    second = _replay(tmp_path / "second", monkeypatch, "partial_fill")
    assert first == second


def test_stale_candle_replays_ineligible_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "stale_candle")
    second = _replay(tmp_path / "second", monkeypatch, "stale_candle")
    assert first == second


def test_future_candle_replays_ineligible_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "future_candle")
    second = _replay(tmp_path / "second", monkeypatch, "future_candle")
    assert first == second


def test_provider_outage_replays_fail_closed_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "provider_outage")
    second = _replay(tmp_path / "second", monkeypatch, "provider_outage")
    assert first == second


def test_orphan_position_replays_fail_closed_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "orphan_position")
    second = _replay(tmp_path / "second", monkeypatch, "orphan_position")
    assert first == second


def test_regime_setup_mismatch_replays_same_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "regime_mismatch")
    second = _replay(tmp_path / "second", monkeypatch, "regime_mismatch")
    assert first == second


def test_setup_execution_mismatch_replays_same_reject_chain_twice(tmp_path, monkeypatch):
    first = _replay(tmp_path / "first", monkeypatch, "mtf_conflict")
    second = _replay(tmp_path / "second", monkeypatch, "mtf_conflict")
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
