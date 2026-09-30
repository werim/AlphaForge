from __future__ import annotations

import json
import sqlite3

import pytest
import alphaforge.burnin_resolver as burnin_resolver
from sqlalchemy import create_engine

from alphaforge.burnin_campaign import (
    BurnInCampaignRunner,
    aggregate_campaign,
    bootstrap_campaign_schema,
    create_campaign,
    export_campaign_bundle,
    start_or_resume_campaign,
)
from alphaforge.burnin_ops import _check_and_update_source_baseline
from alphaforge.burnin_resolver import (
    apply_position_management_action,
    load_position_management_state,
    persist_pending_position,
    resolve_campaign_positions,
    resolve_position_closure,
)


def _management_evidence(price: float, *, trigger_reason: str | None = None) -> dict:
    payload = {
        "evidence_status": "COMPLETE",
        "source": "ISSUE505_DETERMINISTIC_FIXTURE",
        "execution_risk_status": "MEASURED",
        "regime_status": "MEASURED",
        "liquidity_status": "MEASURED",
        "observed_price": price,
    }
    if trigger_reason:
        payload["trigger_reason"] = trigger_reason
    return payload


def _exit_costs(**overrides) -> dict:
    payload = {
        "cost_unit": "USD",
        "exit_spread": 0.0,
        "exit_slippage": 0.0,
        "exit_fee": 0.0,
        "funding": 0.0,
        "latency_impact_penalty": 0.0,
        "volatility_penalty": 0.0,
        "liquidity_penalty": 0.0,
    }
    payload.update(overrides)
    return payload


def _setup(tmp_path, *, entry_cost: float = 0.0, trailing_allowed: bool = True):
    path = tmp_path / "issue505.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    bootstrap_campaign_schema(conn)
    campaign = create_campaign(
        conn,
        release_id="issue505",
        duration_days=1,
        symbols=["BTCUSDT"],
        intervals=["1m"],
        source_provenance={"provider": "ISSUE505_FIXTURE", "exchange": "BINANCE"},
    )
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    provenance = {
        "execution_cost_unit": "USD",
        "execution_cost_model_unit": "USD",
        "execution_cost_model": {
            "spread_penalty": 0.0,
            "slippage_penalty": 0.0,
            "fee_penalty": 0.0,
            "funding_penalty": 0.0,
            "latency_penalty": 0.0,
            "volatility_penalty": 0.0,
            "liquidity_penalty": 0.0,
        },
        "target_policy": {
            "target_authority": "STRUCTURAL_MARKET_EVIDENCE",
            "trailing_allowed": trailing_allowed,
            "trailing_distance": None,
        },
    }
    persist_pending_position(
        conn,
        trade_id="trade-505",
        campaign_id=campaign.campaign_id,
        burnin_run_id=run["burnin_run_id"],
        signal_id="signal-505",
        source_decision_id="decision-505",
        decision_time="2026-09-30T12:00:00Z",
        symbol="BTCUSDT",
        side="LONG",
        setup_type="LONG_CONTINUATION",
        entry_time="2026-09-30T12:00:00Z",
        planned_entry=100.0,
        simulated_fill=100.0,
        stop=90.0,
        target=120.0,
        quantity=1.0,
        notional=100.0,
        entry_spread=entry_cost,
        entry_slippage=entry_cost,
        entry_fee=entry_cost,
        regime="TRENDING",
        source_provenance=provenance,
    )
    conn.commit()
    return path, conn, campaign.campaign_id


def test_stop_can_tighten_but_cannot_widen(tmp_path):
    _path, conn, _cid = _setup(tmp_path)
    before = load_position_management_state(conn, "trade-505")
    assert before["initial_stop"] == pytest.approx(90.0)

    applied = apply_position_management_action(
        conn,
        trade_id="trade-505",
        management_event_id="mgt-tighten-1",
        action="TIGHTEN_STOP",
        event_time="2026-09-30T12:01:00Z",
        new_stop=95.0,
        evidence=_management_evidence(101.0),
    )
    assert applied["current_stop"] == pytest.approx(95.0)

    with pytest.raises(ValueError, match="STOP_WIDEN_BLOCKED"):
        apply_position_management_action(
            conn,
            trade_id="trade-505",
            management_event_id="mgt-widen-blocked",
            action="TIGHTEN_STOP",
            event_time="2026-09-30T12:02:00Z",
            new_stop=94.0,
            evidence=_management_evidence(101.0),
        )

    row = conn.execute(
        "SELECT stop,current_stop FROM burnin_pending_position_outcomes WHERE trade_id=?",
        ("trade-505",),
    ).fetchone()
    assert row["stop"] == pytest.approx(90.0)
    assert row["current_stop"] == pytest.approx(95.0)


def test_partial_exit_updates_exposure_exactly_once(tmp_path):
    _path, conn, _cid = _setup(tmp_path, entry_cost=0.1)
    action = dict(
        trade_id="trade-505",
        management_event_id="mgt-partial-1",
        action="PARTIAL_EXIT",
        event_time="2026-09-30T12:01:00Z",
        exit_quantity=0.4,
        execution_price=110.0,
        exit_costs=_exit_costs(
            exit_spread=0.01,
            exit_slippage=0.01,
            exit_fee=0.01,
            funding=0.01,
            latency_impact_penalty=0.01,
            volatility_penalty=0.01,
            liquidity_penalty=0.01,
        ),
        evidence=_management_evidence(110.0),
    )
    first = apply_position_management_action(conn, **action)
    second = apply_position_management_action(conn, **action)

    assert first["remaining_quantity"] == pytest.approx(0.6)
    assert first["remaining_notional"] == pytest.approx(60.0)
    assert first["realized_gross_pnl"] == pytest.approx(4.0)
    assert first["realized_execution_cost"] == pytest.approx(0.19)
    assert first["realized_net_pnl"] == pytest.approx(3.81)
    assert second["status"] == "IDEMPOTENT"
    assert second["remaining_quantity"] == pytest.approx(0.6)
    assert conn.execute(
        "SELECT COUNT(*) FROM burnin_position_management_events WHERE management_event_id='mgt-partial-1'"
    ).fetchone()[0] == 1

    changed = {**action, "exit_quantity": 0.2}
    with pytest.raises(ValueError, match="EVENT_ID_CONFLICT"):
        apply_position_management_action(conn, **changed)


def test_incomplete_final_cost_preserves_known_management_ledger_and_marks_evidence_incomplete(tmp_path):
    _path, conn, _cid = _setup(tmp_path)

    result = resolve_position_closure(
        conn,
        trade_id="trade-505",
        exit_time="2026-09-30T12:05:00Z",
        exit_price=95.0,
        exit_reason="SL_HIT",
        exit_costs=_exit_costs(funding=None),
    )
    assert result["evidence_complete"] is False

    row = conn.execute(
        """SELECT status,total_execution_cost,net_pnl,net_r,
                  realized_gross_pnl,realized_execution_cost,realized_net_pnl,
                  evidence_complete,missing_fields_json
           FROM burnin_pending_position_outcomes WHERE trade_id='trade-505'"""
    ).fetchone()
    assert row["status"] == "CLOSED"
    assert row["total_execution_cost"] is None
    assert row["net_pnl"] is None
    assert row["net_r"] is None
    assert row["realized_gross_pnl"] == pytest.approx(-5.0)
    # The running management ledger remains the last known-complete amount.
    # Unknown final costs must stay unknown in canonical terminal fields rather
    # than being coerced to zero merely to satisfy NOT NULL accumulator columns.
    assert row["realized_execution_cost"] == pytest.approx(0.0)
    assert row["realized_net_pnl"] == pytest.approx(0.0)
    assert row["evidence_complete"] == 0
    assert "funding" in json.loads(row["missing_fields_json"])


def test_trailing_activation_requires_entry_authority_and_managed_stop_executes(tmp_path):
    _path, conn, cid = _setup(tmp_path, trailing_allowed=True)
    apply_position_management_action(
        conn,
        trade_id="trade-505",
        management_event_id="mgt-trailing-enable",
        action="ENABLE_TRAILING",
        event_time="2026-09-30T12:01:00Z",
        evidence=_management_evidence(110.0),
    )
    apply_position_management_action(
        conn,
        trade_id="trade-505",
        management_event_id="mgt-trailing-stop",
        action="TIGHTEN_STOP",
        event_time="2026-09-30T12:02:00Z",
        new_stop=105.0,
        evidence={**_management_evidence(110.0), "trigger": "TRAILING"},
    )

    counts = resolve_campaign_positions(
        conn,
        cid,
        {
            "trade-505": [
                {
                    "timestamp": "2026-09-30T12:03:00Z",
                    "high": 106.0,
                    "low": 104.0,
                }
            ]
        },
        now="2026-09-30T12:04:00Z",
    )
    assert counts["sl"] == 1

    position = conn.execute(
        "SELECT status,stop,current_stop,exit_price,exit_reason FROM burnin_pending_position_outcomes WHERE trade_id='trade-505'"
    ).fetchone()
    assert position["status"] == "CLOSED"
    assert position["stop"] == pytest.approx(90.0)
    assert position["current_stop"] == pytest.approx(105.0)
    assert position["exit_price"] == pytest.approx(105.0)
    assert position["exit_reason"] == "SL_HIT"

    outcome = conn.execute(
        "SELECT payload_json FROM burnin_trade_outcomes WHERE trade_id='trade-505'"
    ).fetchone()
    payload = json.loads(outcome["payload_json"])
    assert payload["trailing_enabled"] is True
    assert payload["initial_stop"] == pytest.approx(90.0)
    assert payload["final_managed_stop"] == pytest.approx(105.0)


def test_trailing_cannot_be_enabled_without_authorized_entry_policy(tmp_path):
    _path, conn, _cid = _setup(tmp_path, trailing_allowed=False)
    with pytest.raises(ValueError, match="TRAILING_NOT_AUTHORIZED"):
        apply_position_management_action(
            conn,
            trade_id="trade-505",
            management_event_id="mgt-trailing-denied",
            action="ENABLE_TRAILING",
            event_time="2026-09-30T12:01:00Z",
            evidence=_management_evidence(105.0),
        )


def test_regime_or_execution_deterioration_can_protectively_exit_with_complete_evidence(tmp_path):
    _path, conn, _cid = _setup(tmp_path)
    result = apply_position_management_action(
        conn,
        trade_id="trade-505",
        management_event_id="mgt-protective-1",
        action="PROTECTIVE_EXIT",
        event_time="2026-09-30T12:05:00Z",
        execution_price=97.0,
        exit_costs=_exit_costs(),
        evidence=_management_evidence(
            97.0, trigger_reason="REGIME_AND_EXECUTION_RISK_DETERIORATED"
        ),
    )
    assert result["status"] == "APPLIED"
    assert result["remaining_quantity"] == pytest.approx(0.0)

    row = conn.execute(
        "SELECT status,exit_reason FROM burnin_pending_position_outcomes WHERE trade_id='trade-505'"
    ).fetchone()
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "RUNTIME_PROTECTIVE_EXIT"
    event = conn.execute(
        "SELECT action FROM burnin_position_management_events WHERE management_event_id='mgt-protective-1'"
    ).fetchone()
    assert event["action"] == "PROTECTIVE_EXIT"


def test_restart_restores_management_state_from_sql(tmp_path):
    path, conn, _cid = _setup(tmp_path)
    apply_position_management_action(
        conn,
        trade_id="trade-505",
        management_event_id="mgt-partial-restart",
        action="PARTIAL_EXIT",
        event_time="2026-09-30T12:01:00Z",
        exit_quantity=0.25,
        execution_price=108.0,
        exit_costs=_exit_costs(),
        evidence=_management_evidence(108.0),
    )
    apply_position_management_action(
        conn,
        trade_id="trade-505",
        management_event_id="mgt-stop-restart",
        action="TIGHTEN_STOP",
        event_time="2026-09-30T12:02:00Z",
        new_stop=96.0,
        evidence=_management_evidence(108.0),
    )
    conn.commit()
    conn.close()

    restarted = sqlite3.connect(path)
    restarted.row_factory = sqlite3.Row
    bootstrap_campaign_schema(restarted)
    state = load_position_management_state(restarted, "trade-505")
    assert state["position_status"] == "OPEN"
    assert state["current_stop"] == pytest.approx(96.0)
    assert state["remaining_quantity"] == pytest.approx(0.75)
    assert state["remaining_notional"] == pytest.approx(75.0)
    assert state["management_version"] == 2


def test_management_action_loop_applies_before_terminal_candle(tmp_path):
    _path, conn, cid = _setup(tmp_path)
    counts = resolve_campaign_positions(
        conn,
        cid,
        {
            "trade-505": [
                {
                    "timestamp": "2026-09-30T12:02:00Z",
                    "high": 101.0,
                    "low": 96.5,
                }
            ]
        },
        now="2026-09-30T12:03:00Z",
        management_actions_by_trade={
            "trade-505": [
                {
                    "management_event_id": "mgt-loop-tighten",
                    "action": "TIGHTEN_STOP",
                    "event_time": "2026-09-30T12:01:00Z",
                    "new_stop": 97.0,
                    "evidence": _management_evidence(101.0),
                }
            ]
        },
    )
    assert counts["sl"] == 1
    row = conn.execute(
        "SELECT exit_price,current_stop FROM burnin_pending_position_outcomes WHERE trade_id='trade-505'"
    ).fetchone()
    assert row["exit_price"] == pytest.approx(97.0)
    assert row["current_stop"] == pytest.approx(97.0)


def test_ambiguous_managed_stop_and_target_remains_non_qualifying(tmp_path):
    _path, conn, cid = _setup(tmp_path)
    apply_position_management_action(
        conn,
        trade_id="trade-505",
        management_event_id="mgt-amb-stop",
        action="TIGHTEN_STOP",
        event_time="2026-09-30T12:01:00Z",
        new_stop=95.0,
        evidence=_management_evidence(110.0),
    )
    counts = resolve_campaign_positions(
        conn,
        cid,
        {
            "trade-505": [
                {
                    "timestamp": "2026-09-30T12:02:00Z",
                    "high": 121.0,
                    "low": 94.0,
                }
            ]
        },
        now="2026-09-30T12:03:00Z",
    )
    assert counts["ambiguous"] == 1
    row = conn.execute(
        "SELECT evidence_complete,exit_reason FROM burnin_pending_position_outcomes WHERE trade_id='trade-505'"
    ).fetchone()
    assert row["evidence_complete"] == 0
    assert row["exit_reason"] == "AMBIGUOUS_INTRABAR"


def test_multiple_partial_exits_reconcile_full_trade_net_r_once(tmp_path):
    _path, conn, _cid = _setup(tmp_path, entry_cost=0.1)
    for event_id, qty, price, minute in (
        ("mgt-p1", 0.4, 110.0, 1),
        ("mgt-p2", 0.2, 115.0, 2),
    ):
        apply_position_management_action(
            conn,
            trade_id="trade-505",
            management_event_id=event_id,
            action="PARTIAL_EXIT",
            event_time=f"2026-09-30T12:0{minute}:00Z",
            exit_quantity=qty,
            execution_price=price,
            exit_costs=_exit_costs(),
            evidence=_management_evidence(price),
        )

    closed = resolve_position_closure(
        conn,
        trade_id="trade-505",
        exit_time="2026-09-30T12:03:00Z",
        exit_price=120.0,
        exit_reason="TP_HIT",
        exit_costs=_exit_costs(),
    )
    assert closed["status"] == "CLOSED"

    row = conn.execute(
        "SELECT gross_pnl,total_execution_cost,net_pnl,net_r,remaining_quantity,remaining_notional "
        "FROM burnin_pending_position_outcomes WHERE trade_id='trade-505'"
    ).fetchone()
    assert row["gross_pnl"] == pytest.approx(15.0)
    assert row["total_execution_cost"] == pytest.approx(0.3)
    assert row["net_pnl"] == pytest.approx(14.7)
    assert row["net_r"] == pytest.approx(1.47)
    assert row["remaining_quantity"] == pytest.approx(0.0)
    assert row["remaining_notional"] == pytest.approx(0.0)

    outcome = conn.execute(
        "SELECT gross_pnl,total_execution_cost,net_pnl,net_r,payload_json "
        "FROM burnin_trade_outcomes WHERE trade_id='trade-505'"
    ).fetchone()
    assert outcome["gross_pnl"] == pytest.approx(15.0)
    assert outcome["total_execution_cost"] == pytest.approx(0.3)
    assert outcome["net_pnl"] == pytest.approx(14.7)
    assert outcome["net_r"] == pytest.approx(1.47)
    payload = json.loads(outcome["payload_json"])
    assert payload["partial_exit_count"] == 2
    assert payload["final_exit_quantity"] == pytest.approx(0.4)


def test_management_ledger_is_bound_to_campaign_hash_and_export(tmp_path):
    path, conn, cid = _setup(tmp_path)
    before = aggregate_campaign(conn, cid)
    assert before["status"] == "OK"

    apply_position_management_action(
        conn,
        trade_id="trade-505",
        management_event_id="mgt-evidence-bind",
        action="TIGHTEN_STOP",
        event_time="2026-09-30T12:01:00Z",
        new_stop=95.0,
        evidence=_management_evidence(105.0),
    )
    conn.commit()
    after = aggregate_campaign(conn, cid)
    assert after["evidence_hash"] != before["evidence_hash"]
    assert after["metrics"]["position_management_event_count"] == 1
    conn.close()

    exported = export_campaign_bundle(path, tmp_path / "artifacts", cid)
    manifest = exported["manifest"]
    assert manifest["row_counts"]["position_management_events.csv"] == 1
    ledger = (tmp_path / "artifacts" / f"burnin_campaign_{cid}" / "position_management_events.csv").read_text()
    assert "mgt-evidence-bind" in ledger
    assert "TIGHTEN_STOP" in ledger


def test_campaign_runner_invokes_management_provider_before_position_resolution(tmp_path):
    path, conn, cid = _setup(tmp_path)
    conn.commit()
    conn.close()

    observed = {"calls": 0}

    def candle_provider(symbol, start, end, timeframe="1m"):
        assert symbol == "BTCUSDT"
        return [{
            "timestamp": "2026-09-30T12:01:00Z",
            "high": 101.0,
            "low": 96.5,
        }]

    def management_provider(position, candles, now):
        observed["calls"] += 1
        assert position["trade_id"] == "trade-505"
        assert candles and candles[0]["low"] == pytest.approx(96.5)
        return {"actions": [{
            "management_event_id": "mgt-runner-tighten",
            "action": "TIGHTEN_STOP",
            "event_time": "2026-09-30T12:00:30Z",
            "new_stop": 97.0,
            "evidence": _management_evidence(101.0),
        }]}

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    runner = BurnInCampaignRunner(
        engine,
        cid,
        candle_provider,
        position_management_provider=management_provider,
    )
    runner._qualify_if_due = lambda: None

    result = runner.resolver_tick()
    assert result["status"] == "OK"
    assert result["position_counts"]["sl"] == 1
    assert observed["calls"] == 1

    with engine.connect() as db:
        position = db.exec_driver_sql(
            "SELECT status,current_stop,exit_price FROM burnin_pending_position_outcomes "
            "WHERE trade_id='trade-505'"
        ).mappings().one()
        event_count = db.exec_driver_sql(
            "SELECT COUNT(*) FROM burnin_position_management_events "
            "WHERE management_event_id='mgt-runner-tighten'"
        ).scalar_one()
    assert position["status"] == "CLOSED"
    assert position["current_stop"] == pytest.approx(97.0)
    assert position["exit_price"] == pytest.approx(97.0)
    assert event_count == 1


def test_stale_management_writer_fails_closed_without_event(tmp_path, monkeypatch):
    _path, conn, _cid = _setup(tmp_path)

    def race(target_conn, trade_id, expected_version):
        assert expected_version == 0
        target_conn.execute(
            "UPDATE burnin_pending_position_outcomes "
            "SET management_version=management_version+1 WHERE trade_id=?",
            (trade_id,),
        )

    monkeypatch.setattr(burnin_resolver, "_position_management_pre_update_hook", race)
    with pytest.raises(RuntimeError, match="CONCURRENT_STATE_CHANGE"):
        apply_position_management_action(
            conn,
            trade_id="trade-505",
            management_event_id="mgt-stale-writer",
            action="TIGHTEN_STOP",
            event_time="2026-09-30T12:01:00Z",
            new_stop=95.0,
            evidence=_management_evidence(101.0),
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM burnin_position_management_events "
        "WHERE management_event_id='mgt-stale-writer'"
    ).fetchone()[0] == 0
    state = conn.execute(
        "SELECT stop,current_stop,management_version FROM burnin_pending_position_outcomes "
        "WHERE trade_id='trade-505'"
    ).fetchone()
    assert state["stop"] == pytest.approx(90.0)
    assert state["current_stop"] == pytest.approx(90.0)
    assert state["management_version"] == 1


def test_management_event_mutation_breaks_source_evidence_baseline(tmp_path):
    _path, conn, _cid = _setup(tmp_path)
    run_id = conn.execute(
        "SELECT burnin_run_id FROM burnin_pending_position_outcomes WHERE trade_id='trade-505'"
    ).fetchone()[0]

    ok, created = _check_and_update_source_baseline(conn, _cid, run_id)
    assert ok is True
    assert created["baseline_created"] is True

    apply_position_management_action(
        conn,
        trade_id="trade-505",
        management_event_id="mgt-baseline",
        action="TIGHTEN_STOP",
        event_time="2026-09-30T12:01:00Z",
        new_stop=95.0,
        evidence=_management_evidence(101.0),
    )
    ok, extended = _check_and_update_source_baseline(conn, _cid, run_id)
    assert ok is True
    assert extended["added_rows"]["position_management"]

    conn.execute(
        "UPDATE burnin_position_management_events "
        "SET evidence_json='{}' WHERE management_event_id='mgt-baseline'"
    )
    ok, violation = _check_and_update_source_baseline(conn, _cid, run_id)
    assert ok is False
    assert violation["mutated_rows"]["position_management"]


def test_management_event_after_resume_belongs_to_new_continuation(tmp_path):
    _path, conn, cid = _setup(tmp_path)
    entry_run = conn.execute(
        "SELECT burnin_run_id FROM burnin_pending_position_outcomes WHERE trade_id='trade-505'"
    ).fetchone()[0]
    ok, _ = _check_and_update_source_baseline(conn, cid, entry_run)
    assert ok is True

    resumed = start_or_resume_campaign(conn, cid, resume=True)
    assert resumed["burnin_run_id"] != entry_run

    apply_position_management_action(
        conn,
        trade_id="trade-505",
        management_event_id="mgt-after-resume",
        action="TIGHTEN_STOP",
        event_time="2026-09-30T12:01:00Z",
        new_stop=95.0,
        evidence=_management_evidence(101.0),
    )

    event = conn.execute(
        "SELECT burnin_run_id,evidence_json FROM burnin_position_management_events "
        "WHERE management_event_id='mgt-after-resume'"
    ).fetchone()
    evidence = json.loads(event["evidence_json"])
    assert event["burnin_run_id"] == resumed["burnin_run_id"]
    assert evidence["position_entry_run_id"] == entry_run
    assert evidence["management_run_id"] == resumed["burnin_run_id"]

    old_ok, old_details = _check_and_update_source_baseline(conn, cid, entry_run)
    assert old_ok is True
    assert old_details["baseline_reason"] == "TERMINAL_RECOVERY_REQUIRED"

    new_ok, new_details = _check_and_update_source_baseline(
        conn, cid, resumed["burnin_run_id"]
    )
    assert new_ok is True
    assert new_details["baseline_created"] is True
