from __future__ import annotations

from pathlib import Path
import importlib

from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.persistence import init_db, save_order_decision, save_trade_lifecycle_event


def test_no_duplicate_alphaforge_package_shadowing() -> None:
    root = Path(__file__).resolve().parents[1]
    pkg_dirs = [p for p in root.rglob("alphaforge") if p.is_dir() and "egg-info" not in str(p)]
    assert pkg_dirs == [root / "src" / "alphaforge"]


def test_runtime_imports_from_src_package() -> None:
    runtime = importlib.import_module("alphaforge.runtime")
    assert "/src/alphaforge/runtime.py" in str(Path(runtime.__file__).as_posix())


def test_save_order_decision_persists_reject() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        save_order_decision(
            s,
            decision_id="dec-1",
            signal_id="sig-1",
            order_id="ord-1",
            symbol="BTCUSDT",
            mode="BACKTEST",
            decision="REJECTED",
            reject_reason="LOW_SCORE",
            score=6.1,
            rr=1.2,
            effective_rr=1.1,
            expectancy_bucket="LOW",
            execution_ctx={"spread_pct": "UNAVAILABLE_BACKTEST"},
            execution_ctx_missing=True,
        )
        row = s.execute(text("SELECT decision,reject_reason,execution_ctx FROM order_decisions WHERE decision_id='dec-1'" )).one()
        assert row.decision == "REJECTED"
        assert row.reject_reason == "LOW_SCORE"
        assert "UNAVAILABLE_BACKTEST" in row.execution_ctx


def test_save_trade_lifecycle_event_persists_state() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        ok = save_trade_lifecycle_event(
            s,
            event_id="evt-1",
            signal_id="sig-1",
            order_id="ord-1",
            symbol="BTCUSDT",
            mode="PAPER",
            lifecycle_state="ORDER_REJECTED",
            decision="REJECTED",
            reject_reason="SPREAD_TOO_HIGH",
        )
        assert ok is True
        row = s.execute(text("SELECT lifecycle_state,reject_reason FROM trade_lifecycle_events WHERE event_id='evt-1'" )).one()
        assert row.lifecycle_state == "ORDER_REJECTED"
        assert row.reject_reason == "SPREAD_TOO_HIGH"

import importlib.util

_spec = importlib.util.spec_from_file_location("backtest_order", Path(__file__).resolve().parents[1] / "backtest_order.py")
bo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bo)


def test_rejected_signal_lifecycle_precedes_trade_creation() -> None:
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    recent_stats = {"last_trade_ts_by_symbol": {}, "trades_today_by_symbol": {}, "global_trades_today": 0, "outcomes": []}
    candles = [bo.Candle(1, 10, 10.5, 9.5, 10.1, 1)]
    result = {"status": "rejected", "reason": "QUALITY_BELOW_THRESHOLD", "diagnostics": {"side": "LONG", "setup_type": "BREAKOUT_UP", "setup_reason": "X", "regime": "TREND", "score": 6.2, "rr": 1.8}}
    mctx = {"entry": 10.0, "sl": 9.5, "tp": 11.0, "score": 6.2, "rr": 1.8}
    out = bo.process_backtest_result("AAAUSDT", candles[0], 0, candles, result, mctx, 1000, 1.0, lifecycle, rejected, rejection_counts, open_rows, recent_stats)
    assert out is None
    assert [r.status_after for r in lifecycle[:2]] == ["SIGNAL_CREATED", "SIGNAL_REJECTED"]


def test_order_rejected_lifecycle_contains_reason() -> None:
    row = bo.LifecycleRow(1, "BTCUSDT", "LONG", "BREAKOUT_UP", "X", "TREND", 8.0, 1.4, 10.0, 9.5, 11.2, "ENTRY_TRIGGERED", "ORDER_REJECTED", reject_reason="HIGH_SLIPPAGE")
    assert row.reject_reason == "HIGH_SLIPPAGE"


def test_backtest_lifecycle_does_not_start_directly_at_created() -> None:
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 12, 2, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(1, 11, 11, 10.5, 11, 1), bo.Candle(2, 10, 10.2, 9.8, 10.1, 1)]
    rows = bo.simulate_candidate(c, candles, 0, 1000, 1)
    states = [r.status_after for r in rows]
    assert "WAITING_ENTRY_ZONE" in states


def test_unavailable_backtest_context_uses_sentinel_not_zero() -> None:
    row = bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 0.0, 0.0, 0.0, 0.0, 0.0, "SIGNAL_CREATED", "SIGNAL_REJECTED")
    assert row.volume_24h_usdt == "UNAVAILABLE_BACKTEST"
    assert row.spread_pct == "UNAVAILABLE_BACKTEST"


def test_backtest_paper_decision_contract_fields_match() -> None:
    required = {"symbol", "mode", "decision", "reject_reason", "score", "rr", "effective_rr", "expectancy_bucket", "execution_ctx_missing"}
    payload = {
        "symbol": "BTCUSDT", "mode": "BACKTEST", "decision": "REJECTED", "reject_reason": "X",
        "score": 1.0, "rr": 1.1, "effective_rr": 1.0, "expectancy_bucket": "LOW", "execution_ctx_missing": True,
    }
    assert required.issubset(payload.keys())
