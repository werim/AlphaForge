import json
import sqlite3

from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import (
    evaluate_forward_outcome,
    persist_pending_position,
    resolve_campaign_positions,
)


MODEL = {
    "spread_penalty": 0.0,
    "slippage_penalty": 0.0,
    "fee_penalty": 0.0,
    "funding_penalty": 0.0,
    "latency_penalty": 0.0,
    "volatility_penalty": 0.0,
    "liquidity_penalty": 0.0,
}


def _setup(tmp_path):
    conn = sqlite3.connect(tmp_path / "p0c.db")
    conn.row_factory = sqlite3.Row
    camp = create_campaign(
        conn, release_id="p0c", duration_days=1,
        symbols=["BTCUSDT"], intervals=["1m"],
    )
    run = start_or_resume_campaign(conn, camp.campaign_id)
    conn.commit()
    return conn, camp, run


def _persist(conn, camp, run, trade_id="trade"):
    persist_pending_position(
        conn,
        trade_id=trade_id,
        campaign_id=camp.campaign_id,
        burnin_run_id=run["burnin_run_id"],
        signal_id=f"signal-{trade_id}",
        symbol="BTCUSDT",
        side="LONG",
        entry_time="2026-01-01T00:00:00Z",
        planned_entry=100,
        simulated_fill=100,
        stop=90,
        target=120,
        quantity=1,
        notional=100,
        entry_spread=0.0,
        entry_slippage=0.0,
        entry_fee=0.0,
        regime="TRENDING",
        source_provenance={
            "provider": "PAPER",
            "execution_cost_unit": "R",
            "execution_cost_model": MODEL,
        },
    )


def _pending(conn, trade_id="trade"):
    row = conn.execute(
        "SELECT status,evidence_complete,missing_fields_json "
        "FROM burnin_pending_position_outcomes WHERE trade_id=?",
        (trade_id,),
    ).fetchone()
    return tuple(row)


def test_missing_middle_candle_before_tp_remains_pending(tmp_path):
    conn, camp, run = _setup(tmp_path)
    _persist(conn, camp, run)
    candles = [
        {"timestamp": "2026-01-01T00:01:00Z", "high": 105, "low": 95},
        {"timestamp": "2026-01-01T00:03:00Z", "high": 121, "low": 99},
    ]
    counts = resolve_campaign_positions(
        conn, camp.campaign_id, {"trade": candles}, now="2026-01-01T00:04:00Z"
    )
    assert counts["pending"] == 1 and counts["closed"] == 0
    assert _pending(conn)[0] == "OPEN"
    assert "incomplete_market_window" in (_pending(conn)[2] or "")
    assert conn.execute("SELECT COUNT(*) FROM burnin_trade_outcomes").fetchone()[0] == 0


def test_missing_middle_candle_before_sl_remains_pending(tmp_path):
    conn, camp, run = _setup(tmp_path)
    _persist(conn, camp, run)
    candles = [
        {"timestamp": "2026-01-01T00:01:00Z", "high": 105, "low": 95},
        {"timestamp": "2026-01-01T00:03:00Z", "high": 101, "low": 89},
    ]
    counts = resolve_campaign_positions(
        conn, camp.campaign_id, {"trade": candles}, now="2026-01-01T00:04:00Z"
    )
    assert counts["pending"] == 1 and counts["closed"] == 0
    assert _pending(conn)[0] == "OPEN"
    assert conn.execute("SELECT COUNT(*) FROM burnin_trade_outcomes").fetchone()[0] == 0


def test_gap_closes_on_retry_then_finalized_outcome_is_immutable(tmp_path):
    conn, camp, run = _setup(tmp_path)
    _persist(conn, camp, run)
    gapped = [
        {"timestamp": "2026-01-01T00:01:00Z", "high": 105, "low": 95},
        {"timestamp": "2026-01-01T00:03:00Z", "high": 121, "low": 99},
    ]
    assert resolve_campaign_positions(
        conn, camp.campaign_id, {"trade": gapped}, now="2026-01-01T00:04:00Z"
    )["pending"] == 1
    full = [
        {"timestamp": "2026-01-01T00:01:00Z", "high": 105, "low": 95},
        {"timestamp": "2026-01-01T00:02:00Z", "high": 106, "low": 96},
        {"timestamp": "2026-01-01T00:03:00Z", "high": 121, "low": 99},
    ]
    counts = resolve_campaign_positions(
        conn, camp.campaign_id, {"trade": full}, now="2026-01-01T00:04:00Z"
    )
    assert counts["tp"] == 1 and counts["closed"] == 1
    first = tuple(conn.execute(
        "SELECT exit_reason,closed_at,gross_r,net_r,payload_json "
        "FROM burnin_trade_outcomes WHERE trade_id='trade'"
    ).fetchone())
    conflicting_retry = [
        {"timestamp": "2026-01-01T00:01:00Z", "high": 101, "low": 89},
    ]
    retry = resolve_campaign_positions(
        conn, camp.campaign_id, {"trade": conflicting_retry}, now="2026-01-01T00:05:00Z"
    )
    assert retry["closed"] == 0
    assert tuple(conn.execute(
        "SELECT exit_reason,closed_at,gross_r,net_r,payload_json "
        "FROM burnin_trade_outcomes WHERE trade_id='trade'"
    ).fetchone()) == first
    assert conn.execute("SELECT COUNT(*) FROM burnin_trade_outcomes").fetchone()[0] == 1


def test_out_of_order_complete_candles_resolve_deterministically(tmp_path):
    conn, camp, run = _setup(tmp_path)
    _persist(conn, camp, run)
    candles = [
        {"timestamp": "2026-01-01T00:02:00Z", "high": 121, "low": 99},
        {"timestamp": "2026-01-01T00:01:00Z", "high": 105, "low": 95},
    ]
    counts = resolve_campaign_positions(
        conn, camp.campaign_id, {"trade": candles}, now="2026-01-01T00:03:00Z"
    )
    row = conn.execute(
        "SELECT exit_reason,closed_at FROM burnin_trade_outcomes WHERE trade_id='trade'"
    ).fetchone()
    assert counts["tp"] == 1
    assert row["exit_reason"] == "TP_HIT"
    assert row["closed_at"] == "2026-01-01T00:02:00+00:00" or row["closed_at"] == "2026-01-01T00:02:00Z"


def test_duplicate_candles_are_deduped_without_false_gap(tmp_path):
    conn, camp, run = _setup(tmp_path)
    _persist(conn, camp, run)
    first = {"timestamp": "2026-01-01T00:01:00Z", "high": 105, "low": 95}
    candles = [
        first,
        dict(first),
        {"timestamp": "2026-01-01T00:02:00Z", "high": 121, "low": 99},
    ]
    counts = resolve_campaign_positions(
        conn, camp.campaign_id, {"trade": candles}, now="2026-01-01T00:03:00Z"
    )
    assert counts["tp"] == 1 and counts["closed"] == 1
    assert conn.execute(
        "SELECT exit_reason FROM burnin_trade_outcomes WHERE trade_id='trade'"
    ).fetchone()[0] == "TP_HIT"


def test_same_candle_tp_sl_is_ambiguous_and_not_complete_evidence(tmp_path):
    conn, camp, run = _setup(tmp_path)
    _persist(conn, camp, run)
    counts = resolve_campaign_positions(
        conn,
        camp.campaign_id,
        {"trade": [{"timestamp": "2026-01-01T00:01:00Z", "high": 121, "low": 89}]},
        now="2026-01-01T00:02:00Z",
    )
    pending = conn.execute(
        "SELECT status,evidence_complete,exit_reason FROM burnin_pending_position_outcomes "
        "WHERE trade_id='trade'"
    ).fetchone()
    outcome = conn.execute(
        "SELECT evidence_complete,exit_reason FROM burnin_trade_outcomes WHERE trade_id='trade'"
    ).fetchone()
    assert counts["ambiguous"] == 1
    assert tuple(pending) == ("CLOSED", 0, "AMBIGUOUS_INTRABAR")
    assert tuple(outcome) == (0, "AMBIGUOUS_INTRABAR")


def test_partial_window_without_terminal_remains_pending(tmp_path):
    conn, camp, run = _setup(tmp_path)
    _persist(conn, camp, run)
    counts = resolve_campaign_positions(
        conn,
        camp.campaign_id,
        {"trade": [{"timestamp": "2026-01-01T00:01:00Z", "high": 105, "low": 95}]},
        now="2026-01-01T00:02:00Z",
    )
    assert counts["pending"] == 1 and counts["closed"] == 0
    assert _pending(conn)[0] == "OPEN"


def test_malformed_timestamp_blocks_later_terminal(tmp_path):
    conn, camp, run = _setup(tmp_path)
    _persist(conn, camp, run)
    candles = [
        {"timestamp": "not-a-timestamp", "high": 105, "low": 95},
        {"timestamp": "2026-01-01T00:01:00Z", "high": 121, "low": 99},
    ]
    counts = resolve_campaign_positions(
        conn, camp.campaign_id, {"trade": candles}, now="2026-01-01T00:02:00Z"
    )
    assert counts["pending"] == 1 and counts["closed"] == 0
    assert "malformed_timestamp" in (_pending(conn)[2] or "")
    assert conn.execute("SELECT COUNT(*) FROM burnin_trade_outcomes").fetchone()[0] == 0


def test_future_terminal_candle_is_not_used(tmp_path):
    conn, camp, run = _setup(tmp_path)
    _persist(conn, camp, run)
    candles = [
        {"timestamp": "2026-01-01T00:01:00Z", "high": 105, "low": 95},
        {"timestamp": "2026-01-01T00:03:00Z", "high": 121, "low": 99},
    ]
    counts = resolve_campaign_positions(
        conn, camp.campaign_id, {"trade": candles}, now="2026-01-01T00:01:30Z"
    )
    assert counts["pending"] == 1 and counts["closed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM burnin_trade_outcomes").fetchone()[0] == 0


def test_reject_and_accepted_resolvers_share_gap_completeness_contract(tmp_path):
    candles = [
        {"timestamp": "2026-01-01T00:01:00Z", "high": 105, "low": 95},
        {"timestamp": "2026-01-01T00:03:00Z", "high": 121, "low": 99},
    ]
    rejected = evaluate_forward_outcome(
        side="LONG",
        entry=100,
        stop=90,
        target=120,
        decision_timestamp="2026-01-01T00:00:00Z",
        due_at="2026-01-01T00:03:00Z",
        timeframe="1m",
        horizon_bars=3,
        candles=candles,
    )
    assert rejected["forward_label"] == "TP_BEFORE_SL"
    assert rejected["window_complete"] is False
    assert rejected["evidence_complete"] is False
    assert rejected["market_gaps"]

    conn, camp, run = _setup(tmp_path)
    _persist(conn, camp, run)
    accepted = resolve_campaign_positions(
        conn, camp.campaign_id, {"trade": candles}, now="2026-01-01T00:04:00Z"
    )
    assert accepted["pending"] == 1 and accepted["closed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM burnin_trade_outcomes").fetchone()[0] == 0
