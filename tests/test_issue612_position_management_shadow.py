from __future__ import annotations

import json
import sqlite3

import pytest
from sqlalchemy import create_engine

from alphaforge.burnin import canonical_hash
from alphaforge.burnin_campaign import (
    BurnInCampaignRunner,
    build_phase8_campaign_identity,
    create_campaign,
    export_campaign_bundle,
    start_or_resume_campaign,
)
from alphaforge.burnin_resolver import persist_pending_position
from alphaforge.config import RuntimeSettings, load_config_from_env
from alphaforge.position_management_shadow import (
    PaperPositionManagementShadowProvider,
    persist_position_management_shadow_proposals,
)


def _cost_model() -> dict:
    return {
        "spread_penalty": 0.0,
        "slippage_penalty": 0.0,
        "fee_penalty": 0.0,
        "funding_penalty": 0.0,
        "latency_penalty": 0.0,
        "volatility_penalty": 0.0,
        "liquidity_penalty": 0.0,
    }


def _position(*, trailing_allowed: bool = False, trailing_distance=None) -> dict:
    provenance = {
        "execution_cost_unit": "USD",
        "execution_cost_model_unit": "USD",
        "execution_cost_model": _cost_model(),
        "target_policy": {
            "target_authority": "STRUCTURAL_MARKET_EVIDENCE",
            "realization_profile": "EARLY_REALIZATION_ELIGIBLE",
            "trailing_allowed": trailing_allowed,
            "trailing_distance": trailing_distance,
            "trailing_distance_status": (
                "CALIBRATED" if trailing_distance is not None else "UNSET_REQUIRES_CALIBRATION"
            ),
        },
    }
    return {
        "trade_id": "trade-612",
        "campaign_id": "camp_612fixture0001",
        "burnin_run_id": "camp_612fixture0001_run_0001",
        "decision_time": "2026-10-06T12:00:00Z",
        "entry_time": "2026-10-06T12:00:00Z",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "setup_type": "PULLBACK",
        "regime": "TRENDING",
        "planned_entry": 100.0,
        "simulated_fill": 100.0,
        "stop": 90.0,
        "current_stop": 90.0,
        "target": 120.0,
        "current_target": 120.0,
        "quantity": 1.0,
        "remaining_quantity": 1.0,
        "entry_spread": 0.0,
        "entry_slippage": 0.0,
        "entry_fee": 0.0,
        "source_provenance_json": json.dumps(provenance, sort_keys=True),
    }


def _campaign() -> dict:
    return {
        "campaign_id": "camp_612fixture0001",
        "active_run_id": "camp_612fixture0001_run_0001",
        "release_id": "issue612",
        "git_commit": "abc123",
        "config_hash": "cfg612",
        "strategy_config_hash": "strategy612",
        "execution_mode": "PAPER",
    }


def _candles():
    return [
        {
            "timestamp": "2026-10-06T12:01:00Z",
            "high": 105.0,
            "low": 98.0,
            "close": 104.0,
            "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
        },
        {
            "timestamp": "2026-10-06T12:02:00Z",
            "high": 109.0,
            "low": 101.0,
            "close": 108.0,
            "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
        },
    ]


def test_shadow_config_defaults_off_and_is_identity_bearing_when_enabled():
    disabled = RuntimeSettings(execution_mode="PAPER")
    enabled = RuntimeSettings(
        execution_mode="PAPER",
        paper_position_management_shadow_enabled=True,
    )

    disabled_identity = build_phase8_campaign_identity(
        disabled, ["BTCUSDT"], ["1m"], release_id="issue612"
    )
    enabled_identity = build_phase8_campaign_identity(
        enabled, ["BTCUSDT"], ["1m"], release_id="issue612"
    )

    assert disabled.paper_position_management_shadow_enabled is False
    assert "PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED" not in disabled_identity["config_payload"]
    assert enabled_identity["config_payload"]["PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED"] is True
    assert disabled_identity["config_hash"] != enabled_identity["config_hash"]
    # Counterfactual observation must not silently become strategy authority.
    assert disabled_identity["strategy_config_hash"] == enabled_identity["strategy_config_hash"]


def test_shadow_config_is_paper_only(tmp_path):
    with pytest.raises(
        ValueError,
        match="ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED is PAPER-only",
    ):
        load_config_from_env(
            env={
                "ALPHAFORGE_EXECUTION_MODE": "LIVE",
                "ALPHAFORGE_PAPER_POSITION_MANAGEMENT_SHADOW_ENABLED": "true",
            },
            root=tmp_path,
        )


def test_pullback_proposals_are_causal_and_authoritative_trailing_denial_is_preserved():
    provider = PaperPositionManagementShadowProvider(max_evidence_age_seconds=120.0)
    result = provider(
        _position(trailing_allowed=False),
        _candles(),
        "2026-10-06T12:03:30Z",
        _campaign(),
    )
    proposals = result["proposals"]

    assert {row["proposed_action"] for row in proposals} == {
        "WOULD_TIGHTEN_STOP",
        "WOULD_PARTIAL_EXIT",
        "WOULD_ENABLE_TRAILING",
        "WOULD_PROTECTIVE_EXIT",
    }
    assert all(row["closed_candle_time"] == "2026-10-06T12:03:00Z" for row in proposals)
    assert all(row["proposal_time"] == "2026-10-06T12:03:00Z" for row in proposals)
    assert all(row["decision_time"] == "2026-10-06T12:03:00Z" for row in proposals)
    assert all(row["source_trade_decision_time"] == "2026-10-06T12:00:00Z" for row in proposals)
    assert all(row["observed_price"] == pytest.approx(108.0) for row in proposals)
    assert all(row["mfe_r"] == pytest.approx(0.9) for row in proposals)
    assert all(row["mae_r"] == pytest.approx(0.2) for row in proposals)
    trailing = next(row for row in proposals if row["proposed_action"] == "WOULD_ENABLE_TRAILING")
    assert trailing["eligibility_status"] == "DENIED"
    assert trailing["reason"] == "AUTHORITATIVE_POLICY_DENIES_TRAILING"
    assert trailing["counterfactual_net_r"]["realized_evidence"] is False


def test_explicit_calibrated_trailing_distance_can_be_proposed_but_not_realized():
    provider = PaperPositionManagementShadowProvider(max_evidence_age_seconds=120.0)
    proposals = provider(
        _position(trailing_allowed=True, trailing_distance=0.75),
        _candles(),
        "2026-10-06T12:03:30Z",
        _campaign(),
    )["proposals"]
    trailing = next(row for row in proposals if row["proposed_action"] == "WOULD_ENABLE_TRAILING")

    assert trailing["eligibility_status"] == "ELIGIBLE"
    assert trailing["parameters"] == {"trailing_distance": pytest.approx(0.75)}
    assert trailing["estimated_execution_costs"]["immediate_execution_cost_usd"] == 0.0
    assert trailing["counterfactual_net_r"]["status"] == "PATH_DEPENDENT"
    assert trailing["counterfactual_net_r"]["net_r"] is None
    assert trailing["authoritative_state_mutation"] is False
    assert trailing["authoritative_realized_pnl"] is False


def test_kline_open_timestamp_is_not_mislabeled_as_closed_boundary():
    provider = PaperPositionManagementShadowProvider(max_evidence_age_seconds=120.0)
    candle = {
        "timestamp": "2026-10-06T12:01:00Z",
        "high": 105.0,
        "low": 98.0,
        "close": 104.0,
        "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
    }

    proposals = provider(
        _position(trailing_allowed=True, trailing_distance=0.75),
        [candle],
        "2026-10-06T12:02:30Z",
        _campaign(),
    )["proposals"]

    trailing = next(row for row in proposals if row["proposed_action"] == "WOULD_ENABLE_TRAILING")
    assert trailing["eligibility_status"] == "ELIGIBLE"
    assert trailing["closed_candle_time"] == "2026-10-06T12:02:00Z"
    assert trailing["proposal_time"] == "2026-10-06T12:02:00Z"
    assert trailing["decision_time"] == "2026-10-06T12:02:00Z"
    assert trailing["market_evidence"]["latest_candle"]["open_time"] == "2026-10-06T12:01:00Z"
    assert trailing["market_evidence"]["latest_candle"]["closed_candle_time"] == "2026-10-06T12:02:00Z"


def test_open_kline_cannot_be_used_before_its_close():
    provider = PaperPositionManagementShadowProvider(max_evidence_age_seconds=120.0)
    candle = {
        "timestamp": "2026-10-06T12:02:00Z",
        "high": 119.0,
        "low": 101.0,
        "close": 118.0,
        "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
    }

    proposals = provider(
        _position(trailing_allowed=True, trailing_distance=0.75),
        [candle],
        "2026-10-06T12:02:30Z",
        _campaign(),
    )["proposals"]

    assert all(row["eligibility_status"] == "DENIED" for row in proposals)
    assert all(row["reason"] == "OPEN_OR_FUTURE_CANDLE_PRESENT" for row in proposals)
    assert all(row["closed_candle_time"] is None for row in proposals)


def test_future_candle_never_leaks_into_mfe_or_favorable_proposal():
    provider = PaperPositionManagementShadowProvider(max_evidence_age_seconds=120.0)
    candles = _candles() + [{
        "timestamp": "2026-10-06T12:05:00Z",
        "high": 999.0,
        "low": 1.0,
        "close": 500.0,
        "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
    }]
    proposals = provider(
        _position(trailing_allowed=True, trailing_distance=0.75),
        candles,
        "2026-10-06T12:03:30Z",
        _campaign(),
    )["proposals"]

    assert all(row["eligibility_status"] == "DENIED" for row in proposals)
    assert all(row["reason"] == "OPEN_OR_FUTURE_CANDLE_PRESENT" for row in proposals)
    assert all(row["mfe_r"] == pytest.approx(0.9) for row in proposals)
    assert all(row["mae_r"] == pytest.approx(0.2) for row in proposals)


def test_terminal_and_post_terminal_candles_never_rewrite_preterminal_proposal():
    provider = PaperPositionManagementShadowProvider(max_evidence_age_seconds=300.0)
    candles = [
        {
            "timestamp": "2026-10-06T12:01:00Z",
            "high": 105.0,
            "low": 98.0,
            "close": 104.0,
            "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
        },
        {
            # Target=120 is hit intra-candle. A management proposal at this
            # candle's close would be too late and must not read this OHLC.
            "timestamp": "2026-10-06T12:02:00Z",
            "high": 121.0,
            "low": 103.0,
            "close": 119.0,
            "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
        },
        {
            # Extreme post-terminal values are deliberately adversarial.
            "timestamp": "2026-10-06T12:03:00Z",
            "high": 999.0,
            "low": 1.0,
            "close": 500.0,
            "stale": True,
            "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
        },
    ]
    proposals = provider(
        _position(trailing_allowed=True, trailing_distance=0.75),
        candles,
        "2026-10-06T12:04:30Z",
        _campaign(),
    )["proposals"]

    trailing = next(row for row in proposals if row["proposed_action"] == "WOULD_ENABLE_TRAILING")
    assert trailing["eligibility_status"] == "ELIGIBLE"
    assert trailing["closed_candle_time"] == "2026-10-06T12:02:00Z"
    assert trailing["proposal_time"] == "2026-10-06T12:02:00Z"
    assert trailing["observed_price"] == pytest.approx(104.0)
    assert trailing["mfe_r"] == pytest.approx(0.5)
    assert trailing["mae_r"] == pytest.approx(0.2)
    assert trailing["market_evidence"]["first_terminal_candle_open_time"] == "2026-10-06T12:02:00Z"
    assert trailing["market_evidence"]["first_terminal_candle_close_time"] == "2026-10-06T12:03:00Z"
    assert trailing["market_evidence"]["terminal_and_post_terminal_candles_excluded"] is True
    assert "STALE_OR_UNAVAILABLE_MARKET_EVIDENCE" not in trailing["market_evidence"]["errors"]


def test_first_post_entry_candle_terminal_emits_only_explicit_no_action_reasons():
    provider = PaperPositionManagementShadowProvider(max_evidence_age_seconds=120.0)
    candles = [{
        "timestamp": "2026-10-06T12:01:00Z",
        "high": 121.0,
        "low": 99.0,
        "close": 119.0,
        "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
    }]
    proposals = provider(
        _position(trailing_allowed=True, trailing_distance=0.75),
        candles,
        "2026-10-06T12:02:30Z",
        _campaign(),
    )["proposals"]

    assert len(proposals) == 4
    assert all(row["eligibility_status"] == "DENIED" for row in proposals)
    assert all(row["reason"] == "NO_PRE_TERMINAL_CLOSED_CANDLE" for row in proposals)
    assert all(row["closed_candle_time"] is None for row in proposals)
    assert all(row["mfe_r"] == pytest.approx(0.0) for row in proposals)
    assert all(row["mae_r"] == pytest.approx(0.0) for row in proposals)


def test_missing_candle_gap_is_fail_closed_for_all_shadow_actions():
    provider = PaperPositionManagementShadowProvider(max_evidence_age_seconds=120.0)
    candles = [
        {
            "timestamp": "2026-10-06T12:01:00Z",
            "high": 105.0,
            "low": 98.0,
            "close": 104.0,
            "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
        },
        {
            "timestamp": "2026-10-06T12:04:00Z",
            "high": 106.0,
            "low": 103.0,
            "close": 105.0,
            "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
        },
    ]
    proposals = provider(
        _position(trailing_allowed=True, trailing_distance=0.75),
        candles,
        "2026-10-06T12:05:30Z",
        _campaign(),
    )["proposals"]

    assert all(row["eligibility_status"] == "DENIED" for row in proposals)
    assert all(row["reason"] == "MARKET_CANDLE_GAP_OR_TIME_INVALID" for row in proposals)


@pytest.mark.parametrize(
    "mutation, expected_reason",
    [
        (lambda position, candles, campaign: candles.__setitem__(
            1, {**candles[1], "stale": True}
        ), "STALE_OR_UNAVAILABLE_MARKET_EVIDENCE"),
        (lambda position, candles, campaign: position.update(
            source_provenance_json=json.dumps({"target_policy": {"trailing_allowed": True}})
        ), "EXECUTION_COST_EVIDENCE_INCOMPLETE"),
        (lambda position, candles, campaign: campaign.update(campaign_id="camp_other"),
         "CROSS_SCOPED_POSITION_CAMPAIGN"),
        (lambda position, candles, campaign: candles[1]["source_provenance"].pop("interval"),
         "CANDLE_INTERVAL_UNAVAILABLE_OR_UNSUPPORTED"),
    ],
)
def test_missing_stale_or_cross_scoped_evidence_never_produces_favorable_proposal(
    mutation, expected_reason
):
    provider = PaperPositionManagementShadowProvider(max_evidence_age_seconds=120.0)
    position = _position(trailing_allowed=True, trailing_distance=0.75)
    candles = _candles()
    campaign = _campaign()
    mutation(position, candles, campaign)

    proposals = provider(position, candles, "2026-10-06T12:03:30Z", campaign)["proposals"]

    assert all(row["eligibility_status"] == "DENIED" for row in proposals)
    assert all(expected_reason in row["market_evidence"]["errors"] or row["reason"] == expected_reason
               for row in proposals)


def test_shadow_persistence_is_idempotent_and_conflicts_fail_closed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    provider = PaperPositionManagementShadowProvider(max_evidence_age_seconds=120.0)
    proposals = provider(
        _position(),
        _candles(),
        "2026-10-06T12:03:30Z",
        _campaign(),
    )["proposals"]

    first = persist_position_management_shadow_proposals(conn, proposals)
    second = persist_position_management_shadow_proposals(conn, proposals)
    assert first == {"inserted": 4, "idempotent": 0}
    assert second == {"inserted": 0, "idempotent": 4}
    assert conn.execute(
        "SELECT COUNT(*) FROM burnin_position_management_shadow_proposals"
    ).fetchone()[0] == 4

    conflicting = dict(proposals[0])
    conflicting["reason"] = "DIFFERENT_CAUSAL_OUTPUT"
    conflicting["proposal_hash"] = canonical_hash({
        key: value for key, value in conflicting.items() if key != "proposal_hash"
    })
    with pytest.raises(ValueError, match="POSITION_MANAGEMENT_SHADOW_PROPOSAL_ID_CONFLICT"):
        persist_position_management_shadow_proposals(conn, [conflicting])


def _runner_setup(tmp_path):
    path = tmp_path / "issue612.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    campaign = create_campaign(
        conn,
        release_id="issue612-runner",
        duration_days=1,
        symbols=["BTCUSDT"],
        intervals=["1m"],
        source_provenance={"provider": "BINANCE_READ_ONLY_KLINES", "exchange": "BINANCE"},
    )
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    provenance = {
        "execution_cost_unit": "USD",
        "execution_cost_model_unit": "USD",
        "execution_cost_model": _cost_model(),
        "target_policy": {
            "target_authority": "STRUCTURAL_MARKET_EVIDENCE",
            "realization_profile": "EARLY_REALIZATION_ELIGIBLE",
            "trailing_allowed": False,
            "trailing_distance": None,
            "trailing_distance_status": "UNSET_REQUIRES_CALIBRATION",
        },
    }
    persist_pending_position(
        conn,
        trade_id="trade-runner-612",
        campaign_id=campaign.campaign_id,
        burnin_run_id=run["burnin_run_id"],
        signal_id="signal-runner-612",
        source_decision_id="decision-runner-612",
        decision_time="2026-10-06T12:00:00Z",
        symbol="BTCUSDT",
        side="LONG",
        setup_type="PULLBACK",
        entry_time="2026-10-06T12:00:00Z",
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
        source_provenance=provenance,
    )
    conn.commit()
    conn.close()
    return path, campaign.campaign_id


def test_runner_shadow_is_immutable_and_restart_replay_does_not_duplicate(tmp_path):
    path, campaign_id = _runner_setup(tmp_path)

    def candle_provider(symbol, start, end, timeframe="1m"):
        assert symbol == "BTCUSDT"
        return [{
            "timestamp": "2026-10-06T12:01:00Z",
            "high": 105.0,
            "low": 98.0,
            "close": 104.0,
            "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
        }]

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    provider = PaperPositionManagementShadowProvider(max_evidence_age_seconds=1_000_000_000.0)

    with engine.connect() as db:
        before = db.exec_driver_sql(
            "SELECT current_stop,current_target,remaining_quantity,trailing_enabled,"
            "management_version,gross_pnl,net_pnl FROM burnin_pending_position_outcomes "
            "WHERE trade_id='trade-runner-612'"
        ).mappings().one()

    runner = BurnInCampaignRunner(
        engine,
        campaign_id,
        candle_provider,
        position_management_shadow_provider=provider,
    )
    runner._qualify_if_due = lambda: None
    first = runner.resolver_tick()
    assert first["status"] == "OK"
    assert first["position_management_shadow"]["inserted"] == 4

    with engine.connect() as db:
        after = db.exec_driver_sql(
            "SELECT current_stop,current_target,remaining_quantity,trailing_enabled,"
            "management_version,gross_pnl,net_pnl FROM burnin_pending_position_outcomes "
            "WHERE trade_id='trade-runner-612'"
        ).mappings().one()
        authoritative_events = db.exec_driver_sql(
            "SELECT COUNT(*) FROM burnin_position_management_events"
        ).scalar_one()
        shadow_rows = db.exec_driver_sql(
            "SELECT COUNT(*) FROM burnin_position_management_shadow_proposals"
        ).scalar_one()

    assert dict(after) == dict(before)
    assert authoritative_events == 0
    assert shadow_rows == 4

    # New runner instance simulates restart/replay of the same closed-candle evidence.
    restarted = BurnInCampaignRunner(
        engine,
        campaign_id,
        candle_provider,
        position_management_shadow_provider=PaperPositionManagementShadowProvider(
            max_evidence_age_seconds=1_000_000_000.0
        ),
    )
    restarted._qualify_if_due = lambda: None
    replay = restarted.resolver_tick()
    assert replay["status"] == "OK"
    assert replay["position_management_shadow"] == {"inserted": 0, "idempotent": 4}

    with engine.connect() as db:
        assert db.exec_driver_sql(
            "SELECT COUNT(*) FROM burnin_position_management_shadow_proposals"
        ).scalar_one() == 4
    engine.dispose()


def test_export_labels_counterfactual_rows_separately(tmp_path):
    path, campaign_id = _runner_setup(tmp_path)

    def candle_provider(symbol, start, end, timeframe="1m"):
        return [{
            "timestamp": "2026-10-06T12:01:00Z",
            "high": 105.0,
            "low": 98.0,
            "close": 104.0,
            "source_provenance": {"provider": "BINANCE_READ_ONLY_KLINES", "interval": "1m"},
        }]

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    runner = BurnInCampaignRunner(
        engine,
        campaign_id,
        candle_provider,
        position_management_shadow_provider=PaperPositionManagementShadowProvider(
            max_evidence_age_seconds=1_000_000_000.0
        ),
    )
    runner._qualify_if_due = lambda: None
    assert runner.resolver_tick()["status"] == "OK"
    engine.dispose()

    exported = export_campaign_bundle(path, tmp_path / "artifacts", campaign_id)
    assert exported["manifest"]["row_counts"]["position_management_shadow_proposals.csv"] == 4
    csv_path = (
        tmp_path / "artifacts" / f"burnin_campaign_{campaign_id}"
        / "position_management_shadow_proposals.csv"
    )
    text = csv_path.read_text()
    assert "counterfactual_net_r" in text or "payload_json" in text
    assert "WOULD_ENABLE_TRAILING" in text
