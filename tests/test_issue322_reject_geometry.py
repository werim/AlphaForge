from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from alphaforge.ai_brain import AIBrain
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


class _EarlyRejectBrain:
    def __init__(self, reason: str, score: float = 0.1) -> None:
        self.reason = reason
        self.score = score

    def before_real_order(self, signal, market, regime, stats):
        score = type("Score", (), {"total_score": self.score, "components": {}})()
        plan = type("Plan", (), {"decision": "REJECTED", "reason": self.reason,
                                  "confidence": self.score, "order_type": "REJECTED",
                                  "limit_price": None, "stop_price": None})()
        return score, plan, self.reason


def _candidate(side: str, *, geometry: bool = True) -> dict:
    row = {
        "symbol": "BTCUSDT", "entry": 100.0, "side": side, "rr": 2.0, "timeframe": "1m",
        "volume_24h_usdt": 90_000_000.0, "spread_pct": 0.0002,
        "expected_slippage_pct": 0.0001, "funding_rate_pct": 0.0,
        "volatility_pct": 0.01, "trend_strength": 0.9, "liquidity_score": 0.9,
        "chop_score": 0.1, "regime": "TREND", "setup_type": "BREAKOUT",
    }
    if geometry:
        row.update({"sl": 101.0, "tp": 98.0} if side == "SHORT" else {"sl": 99.0, "tp": 102.0})
    return row


def _runtime(tmp_path, candidate: dict, reason: str) -> tuple[RuntimeOrchestrator, object]:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / (reason.replace(' ', '_') + '.db')}")

    async def scanner():
        return [candidate]

    brain = (_EarlyRejectBrain(reason) if reason != "Score below threshold or negative expectancy."
             else AIBrain(session_factory=sessionmaker(bind=engine, expire_on_commit=False, future=True),
                          min_accept_score=1.1))
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, reject_forward_horizon_bars=2),
        ai_brain=brain, market_scanner=scanner, persistence_engine=engine,
    )
    runtime._burnin_run_id = "issue-322-run"
    return runtime, engine


@pytest.mark.parametrize(
    ("side", "reason"),
    [("LONG", "Score below threshold or negative expectancy."),
     ("SHORT", "Score below threshold or negative expectancy."),
     ("LONG", "negative_expectancy_after_costs")],
)
def test_real_early_reject_sequence_retains_observational_geometry_without_execution(
    tmp_path, side: str, reason: str,
) -> None:
    runtime, engine = _runtime(tmp_path, _candidate(side), reason)
    asyncio.run(runtime._scan_once())

    assert runtime._reject_log[-1]["decision"] == "REJECTED"
    assert runtime._reject_log[-1]["reason"] == reason.upper()
    entry, stop, target = (runtime._reject_log[-1][key] for key in ("entry", "sl", "tp"))
    assert (target < entry < stop) if side == "SHORT" else (stop < entry < target)
    assert runtime._pending_orders == {}
    assert runtime._active_positions == {}
    assert runtime.metrics.executions == 0
    with engine.connect() as conn:
        pending = conn.execute(text("SELECT entry,stop,target,timeframe FROM burnin_pending_reject_labels")).all()
        assert len(pending) == 1 and pending[0].timeframe == "1m"
        assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM positions")).scalar_one() == 0
        incomplete = conn.execute(text(
            "SELECT COUNT(*) FROM burnin_observations WHERE observation_id LIKE 'incomplete_reject_geometry_%'"
        )).scalar_one()
        assert incomplete == 0


def test_repeated_real_early_reject_is_idempotent(tmp_path) -> None:
    runtime, engine = _runtime(tmp_path, _candidate("LONG"), "Score below threshold or negative expectancy.")
    asyncio.run(runtime._scan_once())
    asyncio.run(runtime._scan_once())
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM rejected_signal_reviews")).scalar_one() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM burnin_pending_reject_labels")).scalar_one() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM burnin_reject_outcomes")).scalar_one() == 0


def test_real_early_reject_missing_source_geometry_remains_ineligible(tmp_path) -> None:
    runtime, engine = _runtime(tmp_path, _candidate("LONG", geometry=False),
                              "Score below threshold or negative expectancy.")
    asyncio.run(runtime._scan_once())
    reject = runtime._reject_log[-1]
    assert reject["decision"] == "REJECTED" and reject["sl"] is None and reject["tp"] is None
    assert runtime.metrics.executions == 0 and runtime._pending_orders == {} and runtime._active_positions == {}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM burnin_pending_reject_labels")).scalar_one() == 0
        row = conn.execute(text(
            "SELECT missing_fields_json FROM burnin_observations "
            "WHERE observation_id LIKE 'incomplete_reject_geometry_%'"
        )).one()
        assert json.loads(row.missing_fields_json) == ["stop", "target"]
