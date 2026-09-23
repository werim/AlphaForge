from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text

from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import persist_pending_position, resolve_position_closure
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


class _AlwaysAcceptBrain:
    def before_real_order(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        class _Plan:
            decision = "ACCEPTED"
            reason = ""
            confidence = 0.9
            order_type = "MARKET"
            limit_price = None
            stop_price = None

        return SimpleNamespace(total_score=9.0, components={}), _Plan(), "ok"


def _iso(minutes_ago: float = 0.0) -> str:
    # Keep daily-risk fixtures inside the current UTC trading day even when
    # CI runs immediately after midnight. The argument is an ordering weight:
    # larger values are further in the past, but never before today's 00:00Z.
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = max((now - day_start).total_seconds(), 0.001)
    weight = max(float(minutes_ago), 0.0)
    seconds_ago = elapsed * min(weight / 1000.0, 0.90)
    value = now - timedelta(seconds=seconds_ago)
    return value.isoformat().replace("+00:00", "Z")


def _create_campaign(db_path, release_id: str, *, symbols: list[str] | None = None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    campaign = create_campaign(
        conn,
        release_id=release_id,
        duration_days=1,
        symbols=symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        intervals=["1m"],
    )
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    conn.commit()
    conn.close()
    return campaign.campaign_id, run["burnin_run_id"]


def _seed_trade(
    db_path,
    *,
    campaign_id: str,
    burnin_run_id: str,
    trade_id: str,
    symbol: str,
    pnl: float,
    entry_minutes_ago: float,
    close_minutes_ago: float | None = None,
    evidence_complete: bool = True,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    entry_time = _iso(entry_minutes_ago)
    persist_pending_position(
        conn,
        trade_id=trade_id,
        campaign_id=campaign_id,
        burnin_run_id=burnin_run_id,
        signal_id=f"signal-{trade_id}",
        source_decision_id=None,
        decision_time=entry_time,
        symbol=symbol,
        side="LONG",
        setup_type="TREND",
        entry_time=entry_time,
        planned_entry=100.0,
        simulated_fill=100.0,
        stop=90.0,
        target=200.0,
        quantity=1.0,
        notional=100.0,
        entry_spread=0.0,
        entry_slippage=0.0,
        entry_fee=0.0,
        regime="TREND",
        source_provenance={"effective_rr_at_entry": 2.0},
    )
    exit_time = _iso(
        close_minutes_ago
        if close_minutes_ago is not None
        else max(entry_minutes_ago - 0.5, 0.0)
    )
    exit_reason = "SL_HIT" if pnl < 0 else "TP_HIT" if pnl > 0 else "TIMEOUT"
    exit_costs: dict[str, float | None] = {
        "exit_spread": 0.0,
        "exit_slippage": 0.0,
        "exit_fee": 0.0,
        "funding": 0.0 if evidence_complete else None,
        "latency_impact_penalty": 0.0,
        "volatility_penalty": 0.0,
        "liquidity_penalty": 0.0,
    }
    resolve_position_closure(
        conn,
        trade_id=trade_id,
        exit_time=exit_time,
        exit_price=100.0 + float(pnl),
        exit_reason=exit_reason,
        exit_costs=exit_costs,
    )
    conn.commit()
    conn.close()


def _runtime(
    engine,
    campaign_id: str,
    burnin_run_id: str,
    **overrides: Any,
) -> tuple[RuntimeOrchestrator, list[dict[str, Any]]]:
    cfg = {
        "execution_mode": ExecutionMode.PAPER,
        "paper_initial_equity": 1000.0,
        "paper_candidate_notional": 10.0,
        "paper_fee_bps": 0.0,
        "paper_execution_latency_ms": 50.0,
        "symbol_cooldown_sec": 0.0,
        "max_daily_loss_pct": 1.0,
        "max_rolling_drawdown_pct": 1.0,
        "max_trades_symbol_per_day": 99,
        "max_trades_global_per_day": 99,
        "symbol_loss_streak_limit": 99,
        "global_loss_streak_limit": 99,
        "max_concurrent_positions": 99,
        "max_open_positions": 99,
        "max_correlated_positions": 99,
        "max_notional_exposure": 1_000_000.0,
        "max_symbol_notional": 1_000_000.0,
        "max_correlation_group_exposure": 1_000_000.0,
    }
    cfg.update(overrides)
    rejects: list[dict[str, Any]] = []
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(**cfg),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        persistence_engine=engine,
        scanner_source="FIXTURE",
        on_reject_persist=lambda payload: rejects.append(payload),
    )
    runtime._campaign_id = campaign_id
    runtime._burnin_run_id = burnin_run_id
    return runtime, rejects


def _market(symbol: str, *, signal_id: str) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "symbol": symbol,
        "source_exchange": "binance",
        "timeframe": "1m",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "rr": 3.0,
        "side": "LONG",
        "market_ts": time.time(),
        "volume_24h_usdt": 90_000_000.0,
        "spread_pct": 0.0002,
        "spread_status": "MEASURED",
        "spread_source": "FIXTURE",
        "expected_slippage_pct": 0.0002,
        "slippage_status": "MODEL_ESTIMATE",
        "slippage_source": "CONFIGURED_PAPER_ASSUMPTION",
        "liquidity_score": 0.90,
        "liquidity_status": "MEASURED",
        "liquidity_source": "FIXTURE",
        "funding_rate_pct": 0.00005,
        "funding_status": "MEASURED",
        "funding_source": "FIXTURE",
        "orderbook_imbalance": 0.10,
        "orderbook_status": "MEASURED",
        "orderbook_source": "FIXTURE",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
        "volatility_source": "FIXTURE",
        "trend_strength": 0.90,
        "chop_score": 0.10,
    }


def _process(runtime: RuntimeOrchestrator, symbol: str, signal_id: str) -> None:
    asyncio.run(runtime._process_symbol(SimpleNamespace(
        symbol=symbol,
        regime_hint="TREND",
        diagnostics={"inputs": _market(symbol, signal_id=signal_id)},
    )))


def test_paper_risk_state_reconstructs_restart_and_continuation_history(tmp_path) -> None:
    db = tmp_path / "restart-risk.db"
    campaign_id, run1 = _create_campaign(db, "p0b-restart")
    _seed_trade(
        db,
        campaign_id=campaign_id,
        burnin_run_id=run1,
        trade_id="btc-loss-1",
        symbol="BTCUSDT",
        pnl=-10.0,
        entry_minutes_ago=40,
    )

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    run2 = start_or_resume_campaign(conn, campaign_id, resume=True)["burnin_run_id"]
    conn.commit()
    conn.close()
    _seed_trade(
        db,
        campaign_id=campaign_id,
        burnin_run_id=run2,
        trade_id="eth-win",
        symbol="ETHUSDT",
        pnl=20.0,
        entry_minutes_ago=30,
    )
    _seed_trade(
        db,
        campaign_id=campaign_id,
        burnin_run_id=run2,
        trade_id="btc-loss-2",
        symbol="BTCUSDT",
        pnl=-5.0,
        entry_minutes_ago=20,
    )

    engine = init_db(f"sqlite+pysqlite:///{db}")
    first, _ = _runtime(engine, campaign_id, run2)
    second, _ = _runtime(engine, campaign_id, run2)
    state1 = first._paper_portfolio_risk_state("BTCUSDT", now_ts=time.time())
    state2 = second._paper_portfolio_risk_state("BTCUSDT", now_ts=time.time())

    assert state1["risk_state_complete"] is True
    assert state1["risk_state_source"] == "BURNIN_CAMPAIGN_EVIDENCE"
    assert state1["trades_today_global"] == 3
    assert state1["trades_today_symbol"] == 2
    assert state1["daily_realized_pnl"] == pytest.approx(5.0)
    assert state1["equity"] == pytest.approx(1005.0)
    assert state1["available_balance"] == pytest.approx(1005.0)
    assert state1["rolling_peak_equity"] == pytest.approx(1010.0)
    assert state1["rolling_drawdown_pct"] == pytest.approx(5.0 / 1010.0)
    assert state1["consecutive_loss_count"] == 1
    assert state1["symbol_consecutive_loss_count"] == 2
    for key in (
        "equity",
        "daily_realized_pnl",
        "rolling_peak_equity",
        "rolling_drawdown_pct",
        "consecutive_loss_count",
        "symbol_consecutive_loss_count",
        "trades_today_symbol",
        "trades_today_global",
    ):
        assert state2[key] == pytest.approx(state1[key])
    engine.dispose()


def test_paper_risk_state_excludes_other_campaign_history(tmp_path) -> None:
    db = tmp_path / "campaign-scope.db"
    bad_campaign, bad_run = _create_campaign(db, "p0b-bad-campaign")
    clean_campaign, clean_run = _create_campaign(db, "p0b-clean-campaign")
    _seed_trade(
        db,
        campaign_id=bad_campaign,
        burnin_run_id=bad_run,
        trade_id="foreign-loss",
        symbol="BTCUSDT",
        pnl=-50.0,
        entry_minutes_ago=10,
    )

    engine = init_db(f"sqlite+pysqlite:///{db}")
    runtime, _ = _runtime(engine, clean_campaign, clean_run)
    state = runtime._paper_portfolio_risk_state("BTCUSDT", now_ts=time.time())

    assert state["risk_state_complete"] is True
    assert state["equity"] == pytest.approx(1000.0)
    assert state["daily_realized_pnl"] == pytest.approx(0.0)
    assert state["trades_today_symbol"] == 0
    assert state["trades_today_global"] == 0
    assert state["consecutive_loss_count"] == 0
    engine.dispose()


def test_restart_reconstructs_persisted_symbol_cooldown(tmp_path) -> None:
    db = tmp_path / "persisted-cooldown.db"
    campaign_id, run_id = _create_campaign(db, "p0b-persisted-cooldown")
    _seed_trade(
        db,
        campaign_id=campaign_id,
        burnin_run_id=run_id,
        trade_id="recent-btc",
        symbol="BTCUSDT",
        pnl=0.0,
        entry_minutes_ago=1.0,
    )
    engine = init_db(f"sqlite+pysqlite:///{db}")
    runtime, rejects = _runtime(
        engine,
        campaign_id,
        run_id,
        symbol_cooldown_sec=120.0,
    )

    state = runtime._paper_portfolio_risk_state("BTCUSDT", now_ts=time.time())
    assert state["risk_state_complete"] is True
    assert state["persisted_cooldown_until"] is not None
    assert state["persisted_cooldown_until"] > time.time()

    _process(runtime, "BTCUSDT", "candidate-persisted-cooldown")

    assert rejects[-1]["reason"] == "SYMBOL_COOLDOWN_ACTIVE"
    assert (
        rejects[-1]["portfolio_diagnostics"]["snapshot"]["symbol_cooldown_remaining_sec"]
        > 0
    )
    engine.dispose()


def test_runtime_rejects_max_daily_loss_from_persisted_paper_state(tmp_path) -> None:
    db = tmp_path / "daily-loss.db"
    campaign_id, run_id = _create_campaign(db, "p0b-daily-loss")
    _seed_trade(
        db,
        campaign_id=campaign_id,
        burnin_run_id=run_id,
        trade_id="daily-loss",
        symbol="ETHUSDT",
        pnl=-31.0,
        entry_minutes_ago=10,
    )
    engine = init_db(f"sqlite+pysqlite:///{db}")
    runtime, rejects = _runtime(
        engine,
        campaign_id,
        run_id,
        max_daily_loss_pct=0.03,
    )

    _process(runtime, "BTCUSDT", "candidate-daily-loss")

    assert runtime.metrics.executions == 0
    assert rejects[-1]["reason"] == "MAX_DAILY_LOSS"
    snapshot = rejects[-1]["portfolio_diagnostics"]["snapshot"]
    assert snapshot["daily_realized_pnl"] == pytest.approx(-31.0)
    assert snapshot["risk_state_source"] == "BURNIN_CAMPAIGN_EVIDENCE"
    engine.dispose()


def test_runtime_rejects_rolling_drawdown_when_daily_pnl_is_positive(tmp_path) -> None:
    db = tmp_path / "drawdown.db"
    campaign_id, run_id = _create_campaign(db, "p0b-drawdown")
    _seed_trade(
        db,
        campaign_id=campaign_id,
        burnin_run_id=run_id,
        trade_id="peak-win",
        symbol="ETHUSDT",
        pnl=100.0,
        entry_minutes_ago=30,
    )
    _seed_trade(
        db,
        campaign_id=campaign_id,
        burnin_run_id=run_id,
        trade_id="drawdown-loss",
        symbol="ETHUSDT",
        pnl=-90.0,
        entry_minutes_ago=20,
    )
    engine = init_db(f"sqlite+pysqlite:///{db}")
    runtime, rejects = _runtime(
        engine,
        campaign_id,
        run_id,
        max_daily_loss_pct=1.0,
        max_rolling_drawdown_pct=0.05,
    )

    _process(runtime, "BTCUSDT", "candidate-drawdown")

    assert rejects[-1]["reason"] == "MAX_ROLLING_DRAWDOWN"
    snapshot = rejects[-1]["portfolio_diagnostics"]["snapshot"]
    assert snapshot["daily_realized_pnl"] == pytest.approx(10.0)
    assert snapshot["rolling_peak_equity"] == pytest.approx(1100.0)
    assert snapshot["rolling_drawdown_pct"] == pytest.approx(90.0 / 1100.0)
    engine.dispose()


@pytest.mark.parametrize(
    ("seed_symbol", "candidate_symbol", "config_overrides", "expected_reason"),
    [
        (
            "BTCUSDT",
            "BTCUSDT",
            {"max_trades_symbol_per_day": 1, "max_trades_global_per_day": 99},
            "DAILY_SYMBOL_TRADE_LIMIT",
        ),
        (
            "ETHUSDT",
            "BTCUSDT",
            {"max_trades_symbol_per_day": 99, "max_trades_global_per_day": 1},
            "DAILY_GLOBAL_TRADE_LIMIT",
        ),
    ],
)
def test_runtime_daily_trade_caps_use_canonical_runtime_config_names(
    tmp_path,
    seed_symbol: str,
    candidate_symbol: str,
    config_overrides: dict[str, int],
    expected_reason: str,
) -> None:
    db = tmp_path / f"{expected_reason.lower()}.db"
    campaign_id, run_id = _create_campaign(db, f"p0b-{expected_reason.lower()}")
    _seed_trade(
        db,
        campaign_id=campaign_id,
        burnin_run_id=run_id,
        trade_id="counted-trade",
        symbol=seed_symbol,
        pnl=0.0,
        entry_minutes_ago=10,
    )
    engine = init_db(f"sqlite+pysqlite:///{db}")
    runtime, rejects = _runtime(engine, campaign_id, run_id, **config_overrides)

    _process(runtime, candidate_symbol, f"candidate-{expected_reason.lower()}")

    assert rejects[-1]["reason"] == expected_reason
    snapshot = rejects[-1]["portfolio_diagnostics"]["snapshot"]
    assert snapshot["trades_today_global"] == 1
    engine.dispose()


@pytest.mark.parametrize(
    ("loss_symbols", "candidate_symbol", "config_overrides"),
    [
        (
            ["BTCUSDT", "BTCUSDT"],
            "BTCUSDT",
            {"symbol_loss_streak_limit": 2, "global_loss_streak_limit": 99},
        ),
        (
            ["BTCUSDT", "ETHUSDT"],
            "SOLUSDT",
            {"symbol_loss_streak_limit": 99, "global_loss_streak_limit": 2},
        ),
    ],
)
def test_runtime_rejects_symbol_and_global_loss_clusters(
    tmp_path,
    loss_symbols: list[str],
    candidate_symbol: str,
    config_overrides: dict[str, int],
) -> None:
    db = tmp_path / f"loss-cluster-{candidate_symbol}.db"
    campaign_id, run_id = _create_campaign(db, f"p0b-loss-cluster-{candidate_symbol.lower()}")
    for idx, symbol in enumerate(loss_symbols):
        _seed_trade(
            db,
            campaign_id=campaign_id,
            burnin_run_id=run_id,
            trade_id=f"loss-{idx}",
            symbol=symbol,
            pnl=-5.0,
            entry_minutes_ago=20 - idx * 5,
        )
    engine = init_db(f"sqlite+pysqlite:///{db}")
    runtime, rejects = _runtime(engine, campaign_id, run_id, **config_overrides)

    _process(runtime, candidate_symbol, f"candidate-loss-cluster-{candidate_symbol}")

    assert rejects[-1]["reason"] == "LOSS_CLUSTER_ACTIVE"
    snapshot = rejects[-1]["portfolio_diagnostics"]["snapshot"]
    if candidate_symbol == "BTCUSDT":
        assert snapshot["symbol_consecutive_loss_count"] == 2
        assert snapshot["symbol_loss_cluster_active"] is True
    else:
        assert snapshot["consecutive_loss_count"] == 2
        assert snapshot["loss_cluster_active"] is True
    engine.dispose()


def test_incomplete_realized_history_fails_closed_as_unknown_portfolio_risk(tmp_path) -> None:
    db = tmp_path / "incomplete-risk.db"
    campaign_id, run_id = _create_campaign(db, "p0b-incomplete")
    _seed_trade(
        db,
        campaign_id=campaign_id,
        burnin_run_id=run_id,
        trade_id="incomplete-outcome",
        symbol="ETHUSDT",
        pnl=-5.0,
        entry_minutes_ago=10,
        evidence_complete=False,
    )
    engine = init_db(f"sqlite+pysqlite:///{db}")
    runtime, rejects = _runtime(engine, campaign_id, run_id)

    state = runtime._paper_portfolio_risk_state("BTCUSDT", now_ts=time.time())
    assert state["risk_state_complete"] is False
    assert any(
        "incomplete-outcome" in field
        for field in state["risk_state_missing_fields"]
    )

    _process(runtime, "BTCUSDT", "candidate-incomplete-risk")

    assert rejects[-1]["reason"] == "UNKNOWN_PORTFOLIO_RISK"
    assert rejects[-1]["portfolio_diagnostics"]["snapshot"]["risk_state_complete"] is False
    engine.dispose()


@pytest.mark.parametrize("drift_kind", ["FOREIGN_RUN", "OPEN_WITH_REALIZED_OUTCOME", "FUTURE_CLOSED"])
def test_paper_risk_state_fails_closed_on_persisted_lineage_or_status_drift(
    tmp_path,
    drift_kind: str,
) -> None:
    db = tmp_path / f"risk-drift-{drift_kind.lower()}.db"
    campaign_id, run_id = _create_campaign(db, f"p0b-risk-drift-{drift_kind.lower()}")
    _seed_trade(
        db,
        campaign_id=campaign_id,
        burnin_run_id=run_id,
        trade_id="drifted-trade",
        symbol="ETHUSDT",
        pnl=-5.0,
        entry_minutes_ago=10,
    )

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    if drift_kind == "FOREIGN_RUN":
        foreign = create_campaign(
            conn,
            release_id="p0b-risk-drift-foreign",
            duration_days=1,
            symbols=["ETHUSDT"],
            intervals=["1m"],
        )
        foreign_run = start_or_resume_campaign(conn, foreign.campaign_id)["burnin_run_id"]
        conn.execute(
            "UPDATE burnin_pending_position_outcomes "
            "SET burnin_run_id=? WHERE trade_id='drifted-trade'",
            (foreign_run,),
        )
    elif drift_kind == "OPEN_WITH_REALIZED_OUTCOME":
        conn.execute(
            "UPDATE burnin_pending_position_outcomes "
            "SET status='OPEN' WHERE trade_id='drifted-trade'"
        )
    else:
        future_close = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat().replace("+00:00", "Z")
        conn.execute(
            "UPDATE burnin_trade_outcomes "
            "SET closed_at=? WHERE trade_id='drifted-trade'",
            (future_close,),
        )
    conn.commit()
    conn.close()

    engine = init_db(f"sqlite+pysqlite:///{db}")
    runtime, rejects = _runtime(engine, campaign_id, run_id)
    state = runtime._paper_portfolio_risk_state("BTCUSDT", now_ts=time.time())

    assert state["risk_state_complete"] is False
    assert state["risk_state_missing_fields"]

    _process(runtime, "BTCUSDT", f"candidate-{drift_kind.lower()}")

    assert rejects[-1]["reason"] == "UNKNOWN_PORTFOLIO_RISK"
    engine.dispose()


def test_executed_paper_trade_updates_persisted_daily_count_before_next_decision(tmp_path) -> None:
    db = tmp_path / "accepted-count.db"
    campaign_id, run_id = _create_campaign(db, "p0b-accepted-count")
    engine = init_db(f"sqlite+pysqlite:///{db}")
    runtime, rejects = _runtime(
        engine,
        campaign_id,
        run_id,
        max_trades_symbol_per_day=1,
        max_trades_global_per_day=99,
    )

    _process(runtime, "BTCUSDT", "first-accepted")

    assert runtime.metrics.executions == 1
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT trade_id
            FROM burnin_pending_position_outcomes
            WHERE campaign_id=:cid AND symbol='BTCUSDT'
            ORDER BY id DESC LIMIT 1
        """), {"cid": campaign_id}).first()
    assert row is not None

    open_state = runtime._paper_portfolio_risk_state(
        "BTCUSDT",
        now_ts=time.time(),
    )
    assert open_state["risk_state_complete"] is True
    assert open_state["trades_today_symbol"] == 1
    assert open_state["trades_today_global"] == 1

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE burnin_pending_position_outcomes "
                "SET entry_time=:entry_time, decision_time=:entry_time "
                "WHERE trade_id=:trade_id"
            ),
            {"entry_time": _iso(1.0), "trade_id": str(row[0])},
        )
        resolve_position_closure(
            conn,
            trade_id=str(row[0]),
            exit_time=_iso(0.0),
            exit_price=100.0,
            exit_reason="TIMEOUT",
            exit_costs={
                "exit_spread": 0.0,
                "exit_slippage": 0.0,
                "exit_fee": 0.0,
                "funding": 0.0,
                "latency_impact_penalty": 0.0,
                "volatility_penalty": 0.0,
                "liquidity_penalty": 0.0,
            },
        )
    runtime._sync_resolved_paper_positions()

    _process(runtime, "BTCUSDT", "second-candidate")

    assert runtime.metrics.executions == 1
    assert rejects[-1]["reason"] == "DAILY_SYMBOL_TRADE_LIMIT"
    assert rejects[-1]["portfolio_diagnostics"]["snapshot"]["trades_today_symbol"] == 1
    engine.dispose()
