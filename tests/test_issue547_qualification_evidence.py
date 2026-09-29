import asyncio
import copy
import json

import pytest
from sqlalchemy import text

from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import (
    evaluate_forward_outcome,
    persist_pending_position,
    resolve_position_closure,
)
from alphaforge.multi_timeframe import BinanceMTFProvider
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _runtime(engine=None):
    return RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=object(),
        market_scanner=lambda: None,
        persistence_engine=engine,
    )


def _campaign_runtime(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'issue547.sqlite3'}")
    with engine.begin() as conn:
        campaign = create_campaign(
            conn,
            release_id="issue547",
            duration_days=1,
            symbols=["BTCUSDT"],
            intervals=["1m", "15m", "1h"],
        )
        run = start_or_resume_campaign(conn, campaign.campaign_id)
    runtime = _runtime(engine)
    runtime._campaign_id = campaign.campaign_id
    runtime._burnin_run_id = run["burnin_run_id"]
    return engine, campaign, run, runtime


def _provider_rows(closes, decision_ms):
    rows = []
    for index, close in enumerate(closes):
        close_ms = decision_ms - (len(closes) - index - 1) * 60_000
        rows.append([
            close_ms - 59_999,
            str(close + .02),
            str(close + .08),
            str(close - .08),
            str(close),
            "100",
            close_ms,
        ])
    return rows


def _outside_zone_mtf(monkeypatch):
    decision_ms = 20_000_000
    rows_by_tf = {
        "1h": _provider_rows([103 - i * .1 for i in range(24)], decision_ms),
        "15m": _provider_rows([100 + i * .1 for i in range(16)], decision_ms),
        "1m": _provider_rows([101 - i * .1 for i in range(8)], decision_ms),
    }
    provider = BinanceMTFProvider()
    monkeypatch.setattr(provider, "_fetch", lambda _symbol, timeframe: rows_by_tf[timeframe])
    execution_ctx = {
        "spread_pct": .0002,
        "expected_slippage_pct": .0004,
        "market_data_latency_ms": 20.0,
        "latency_ms": 20.0,
        "liquidity_score": .9,
        "funding_rate_pct": 0.0,
        "fee_pct": .0004,
        "volatility_regime": "normal",
    }
    mtf = asyncio.run(
        provider.build(
            "BTCUSDT",
            execution_ctx,
            execution_ctx=execution_ctx,
            decision_ts_ms=decision_ms,
            regime_timeframe="1h",
            setup_timeframe="15m",
            execution_timeframe="1m",
        )
    )
    return mtf, execution_ctx


def test_duplicate_position_is_one_canonical_reject_per_setup_and_position_episode(tmp_path):
    engine, campaign, run, runtime = _campaign_runtime(tmp_path)
    setup_identity = "setup:BTCUSDT:15m:20000000"
    episode_id = "paper-order-episode-1"

    for scan in range(8):
        if runtime._duplicate_position_reject_recorded(setup_identity, episode_id):
            continue
        asyncio.run(
            runtime._persist_reject(
                {
                    "signal_id": f"scan-{scan}",
                    "symbol": "BTCUSDT",
                    "decision": "REJECTED",
                    "reason": "DUPLICATE_POSITION",
                    "setup_identity": setup_identity,
                    "active_position_episode_id": episode_id,
                    "side": "LONG",
                    "entry": 100.0,
                    "sl": 99.0,
                    "tp": 102.0,
                    "timeframe": "1m",
                    "execution_ctx": {
                        "spread_pct": .0002,
                        "expected_slippage_pct": .0002,
                        "latency_ms": 20.0,
                        "funding_rate_pct": 0.0,
                        "fee_pct": .0004,
                    },
                }
            )
        )

    with engine.connect() as conn:
        canonical = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM burnin_observations
                WHERE burnin_run_id=:run
                  AND decision='REJECTED'
                  AND json_extract(metrics_json,'$.primary_reject_reason')='DUPLICATE_POSITION'
                  AND json_extract(metrics_json,'$.setup_identity')=:setup
                  AND json_extract(metrics_json,'$.active_position_episode_id')=:episode
            """),
            {"run": run["burnin_run_id"], "setup": setup_identity, "episode": episode_id},
        ).scalar_one()
    assert canonical == 1

    restarted = _runtime(engine)
    restarted._campaign_id = campaign.campaign_id
    restarted._burnin_run_id = run["burnin_run_id"]
    assert restarted._duplicate_position_reject_recorded(setup_identity, episode_id)
    assert not restarted._duplicate_position_reject_recorded(
        "setup:BTCUSDT:15m:NEW", episode_id
    )
    assert not restarted._duplicate_position_reject_recorded(
        setup_identity, "paper-order-episode-2"
    )


def test_guided_outside_zone_reject_preserves_exact_reproducible_geometry(monkeypatch):
    mtf, execution_ctx = _outside_zone_mtf(monkeypatch)
    generation = mtf["generation"]
    evidence = generation["geometry_evidence"]

    assert generation["candidate"] is None
    assert generation["reason"] == "EXECUTION_ENTRY_OUTSIDE_SETUP_ZONE"
    assert evidence["evidence_status"] == "COMPLETE"
    assert evidence["reason"] == "EXECUTION_ENTRY_OUTSIDE_SETUP_ZONE"
    assert evidence["entry_zone_low"] < evidence["entry_zone_high"]
    assert (
        evidence["execution_entry"] < evidence["entry_zone_low"]
        or evidence["execution_entry"] > evidence["entry_zone_high"]
    )
    assert evidence["structural_stop"] is not None
    assert evidence["structural_target"] is not None

    runtime = _runtime()
    canonical = runtime._canonical_reject_payload(
        {
            "signal_id": "outside-zone",
            "symbol": "BTCUSDT",
            "decision": "REJECTED",
            "reason": "EXECUTION_ENTRY_OUTSIDE_SETUP_ZONE",
            "timeframe": "1m",
            "mtf": mtf,
            "execution_ctx": execution_ctx,
            # Deliberately contradictory scanner geometry must not become authority.
            "side": "LONG",
            "entry": 999.0,
            "sl": 998.0,
            "tp": 1000.0,
        }
    )

    assert canonical["forward_label_subject"] == "GUIDED_GEOMETRY_REJECT"
    assert canonical["reject_quality_attributable"] is bool(
        evidence["forward_geometry_valid"]
    )
    assert canonical["entry"] == evidence["execution_entry"]
    assert canonical["sl"] == evidence["structural_stop"]
    assert canonical["tp"] == evidence["structural_target"]
    assert canonical["side"] == evidence["side"]
    assert canonical["guided_reject_geometry"] == evidence
    assert canonical["candidate_rr"] is None
    assert "legacy_shadow_geometry" not in canonical


def test_incomplete_guided_reject_geometry_remains_non_attributable(monkeypatch):
    mtf, execution_ctx = _outside_zone_mtf(monkeypatch)
    mtf = copy.deepcopy(mtf)
    mtf["generation"]["geometry_evidence"]["evidence_status"] = "INCOMPLETE"
    mtf["generation"]["geometry_evidence"]["execution_entry"] = None

    canonical = _runtime()._canonical_reject_payload(
        {
            "signal_id": "outside-zone-incomplete",
            "symbol": "BTCUSDT",
            "decision": "REJECTED",
            "reason": "EXECUTION_ENTRY_OUTSIDE_SETUP_ZONE",
            "timeframe": "1m",
            "mtf": mtf,
            "execution_ctx": execution_ctx,
            "side": "LONG",
            "entry": 999.0,
            "sl": 998.0,
            "tp": 1000.0,
        }
    )

    assert canonical["forward_label_subject"] == "LEGACY_SCANNER_SHADOW_CANDIDATE"
    assert canonical["reject_quality_attributable"] is False
    assert canonical["non_attributable_reason"] == "LEGACY_SHADOW_NOT_GUIDED_EQUIVALENT"
    assert canonical["entry"] is None
    assert canonical["sl"] is None
    assert canonical["tp"] is None


def test_persisted_guided_geometry_reject_carries_exact_gate_provenance(tmp_path, monkeypatch):
    engine, _campaign, _run, runtime = _campaign_runtime(tmp_path)
    mtf, execution_ctx = _outside_zone_mtf(monkeypatch)
    mtf = copy.deepcopy(mtf)
    evidence = mtf["generation"]["geometry_evidence"]
    evidence.update(
        {
            "evidence_status": "COMPLETE",
            "reason": "EXECUTION_ENTRY_OUTSIDE_SETUP_ZONE",
            "execution_entry": 102.0,
            "entry_zone_low": 100.0,
            "entry_zone_high": 101.0,
            "structural_stop": 103.0,
            "structural_target": 99.0,
            "side": "SHORT",
            "regime_direction": "SHORT",
            "setup_type": "SHORT_PULLBACK",
            "setup_phase": "PULLBACK",
            "setup_observed_direction": "LONG",
            "setup_recent_direction": "LONG",
            "execution_direction": "SHORT",
            "forward_geometry_valid": True,
        }
    )

    asyncio.run(
        runtime._persist_reject(
            {
                "signal_id": "guided-outside-zone",
                "symbol": "BTCUSDT",
                "decision": "REJECTED",
                "reason": "EXECUTION_ENTRY_OUTSIDE_SETUP_ZONE",
                "timeframe": "1m",
                "mtf": mtf,
                "execution_ctx": execution_ctx,
            }
        )
    )

    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT entry,stop,target,source_provenance_json
                FROM burnin_pending_reject_labels
                WHERE signal_id='guided-outside-zone'
            """)
        ).mappings().one()
    provenance = json.loads(row["source_provenance_json"])

    assert provenance["forward_label_subject"] == "GUIDED_GEOMETRY_REJECT"
    assert provenance["reject_quality_attributable"] is True
    assert provenance["reject_execution_basis"] == "EXPECTED_FILL_RUNTIME_PARITY"
    assert provenance["decision_execution_entry"] == evidence["execution_entry"]
    assert provenance["entry_zone_low"] == evidence["entry_zone_low"]
    assert provenance["entry_zone_high"] == evidence["entry_zone_high"]
    assert provenance["structural_stop"] == evidence["structural_stop"]
    assert provenance["structural_target"] == evidence["structural_target"]
    assert provenance["setup_phase"] == evidence["setup_phase"]
    assert provenance["execution_direction"] == evidence["execution_direction"]
    # Forward labels are based on expected executable fill, not planned entry.
    assert row["entry"] == pytest.approx(provenance["executable_entry"])


@pytest.mark.parametrize(
    "side,candle,expected_mfe,expected_mae",
    [
        ("LONG", {"timestamp": "2026-01-01T00:01:00Z", "high": 99.0, "low": 95.0}, 0.0, 5.0),
        ("LONG", {"timestamp": "2026-01-01T00:01:00Z", "high": 105.0, "low": 101.0}, 5.0, 0.0),
        ("SHORT", {"timestamp": "2026-01-01T00:01:00Z", "high": 105.0, "low": 101.0}, 0.0, 5.0),
        ("SHORT", {"timestamp": "2026-01-01T00:01:00Z", "high": 99.0, "low": 95.0}, 5.0, 0.0),
    ],
)
def test_forward_excursions_are_direction_correct_and_never_negative(
    side, candle, expected_mfe, expected_mae
):
    result = evaluate_forward_outcome(
        side=side,
        entry=100.0,
        stop=90.0 if side == "LONG" else 110.0,
        target=120.0 if side == "LONG" else 80.0,
        decision_timestamp="2026-01-01T00:00:00Z",
        due_at="2026-01-01T00:01:00Z",
        timeframe="1m",
        horizon_bars=1,
        candles=[candle],
    )
    assert result["mfe"] == pytest.approx(expected_mfe)
    assert result["mae"] == pytest.approx(expected_mae)
    assert result["mfe"] >= 0.0
    assert result["mae"] >= 0.0


def test_position_closure_clamps_external_negative_excursions(tmp_path):
    engine, campaign, run, _runtime_instance = _campaign_runtime(tmp_path)
    with engine.begin() as conn:
        persist_pending_position(
            conn,
            trade_id="negative-excursion",
            campaign_id=campaign.campaign_id,
            burnin_run_id=run["burnin_run_id"],
            signal_id="accepted",
            symbol="BTCUSDT",
            side="LONG",
            entry_time="2026-01-01T00:00:00Z",
            planned_entry=100.0,
            simulated_fill=100.0,
            stop=90.0,
            target=120.0,
            quantity=1.0,
            notional=100.0,
            entry_spread=0.0,
            entry_slippage=0.0,
            entry_fee=0.0,
            regime="TRENDING",
            source_provenance={},
        )
        resolve_position_closure(
            conn,
            trade_id="negative-excursion",
            exit_time="2026-01-01T00:01:00Z",
            exit_price=90.0,
            exit_reason="SL_HIT",
            exit_costs={
                "exit_spread": 0.0,
                "exit_slippage": 0.0,
                "exit_fee": 0.0,
                "funding": 0.0,
                "latency_impact_penalty": 0.0,
            },
            mfe=-0.5,
            mae=-0.2,
        )
        row = conn.execute(
            text("""
                SELECT mfe,mae FROM burnin_trade_outcomes
                WHERE trade_id='negative-excursion'
            """)
        ).one()
    assert row.mfe == 0.0
    assert row.mae == 0.0
