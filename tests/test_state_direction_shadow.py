import asyncio
import sqlite3
import time
from types import SimpleNamespace

from sqlalchemy import text

from alphaforge.burnin import config_hash as burnin_config_hash
from alphaforge.burnin import canonical_decision_sql
from alphaforge.burnin_resolver import evaluate_forward_outcome
from alphaforge.config import runtime_filter_config
from alphaforge.multi_timeframe import evaluate_mtf_alignment
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.state_direction_shadow import (
    StateDirectionShadowStore,
    build_state_direction_shadow_draft,
    derive_shadow_geometry,
    resolve_state_direction_shadow_outcomes,
    state_direction_shadow_report,
)


DECISION_MS = 1_767_225_600_000
DECISION_TS = "2026-01-01T00:00:00Z"


def _context(timeframe, direction, *, close=DECISION_MS, **extra):
    return {
        "timeframe": timeframe,
        "direction": direction,
        "last_closed_candle_ms": close,
        "evidence_status": "COMPLETE",
        **extra,
    }


def _execution_ctx():
    return {
        "spread_pct": 0.0002,
        "expected_slippage_pct": 0.0001,
        "latency_ms": 20.0,
        "funding_rate_pct": 0.0,
        "fee_pct": 0.0004,
        "liquidity_score": 0.9,
        "volatility_regime": "NORMAL",
    }


def _market(side="LONG", **overrides):
    values = {
        "symbol": "BTCUSDT",
        "source_exchange": "binance",
        "side": side,
        "entry": 100.0,
        "sl": 99.0 if side == "LONG" else 101.0,
        "tp": 102.0 if side == "LONG" else 98.0,
        "rr": 2.0,
        "setup_type": "BREAKOUT_UP",
        "geometry_status": "COMPLETE",
        "spread_pct": 0.0002,
        "expected_slippage_pct": 0.0001,
        "market_data_latency_ms": 20.0,
        "latency_ms": 20.0,
        "funding_rate_pct": 0.0,
        "fee_pct": 0.0004,
        "liquidity_score": 0.9,
        "volatility_regime": "NORMAL",
        "volume_24h_usdt": 100_000_000,
        "market_ts": DECISION_MS / 1000,
        "timeframe": "1m",
        "equity": 100_000.0,
        "available_balance": 100_000.0,
        "notional": 1_000.0,
    }
    values.update(overrides)
    return values


def _mtf(regime="SHORT", setup="SHORT", execution="LONG"):
    r = _context("1h", regime)
    s = _context("15m", setup)
    e = _context("1m", execution, trigger="CONFIRMED")
    alignment = evaluate_mtf_alignment(r, s, e, decision_ts_ms=DECISION_MS)
    return {
        "provider": "TEST_CLOSED_CANDLES",
        "regime": r,
        "setup": s,
        "execution": e,
        "alignment": alignment,
    }


class _Provider:
    def __init__(self, *, aligned=False):
        self.aligned = aligned

    async def build(self, *_args, decision_ts_ms, state_direction_resolution_enabled=False, **_kwargs):
        direction = "LONG" if self.aligned else "SHORT"
        r = _context("1h", direction, close=decision_ts_ms)
        s = _context("15m", direction, close=decision_ts_ms)
        e = _context("1m", "LONG", close=decision_ts_ms, trigger="CONFIRMED")
        alignment = evaluate_mtf_alignment(
            r, s, e, decision_ts_ms=decision_ts_ms,
            state_direction_resolution_enabled=state_direction_resolution_enabled,
        )
        return {
            "provider": "TEST_CLOSED_CANDLES",
            "regime": r,
            "setup": s,
            "execution": e,
            "alignment": alignment,
        }


class _AlwaysAcceptBrain:
    def before_real_order(self, *_args, **_kwargs):
        score = SimpleNamespace(total_score=0.9, components={})
        plan = SimpleNamespace(
            decision="ACCEPTED", reason="", confidence=0.9,
            order_type="MARKET", limit_price=None, stop_price=None,
        )
        return score, plan, "accepted"


class _FailingStore:
    def record(self, *_args, **_kwargs):
        raise sqlite3.OperationalError("shadow database unavailable")


def _selection(market=None):
    market = market or _market()
    return SimpleNamespace(
        symbol="BTCUSDT", regime_hint="TREND",
        diagnostics={"inputs": market},
    )


def test_mirror_is_diagnostic_and_invalid_mirror_fails_closed():
    mirrored = derive_shadow_geometry(_market(), "WOULD_SHORT")
    assert mirrored["shadow_geometry_type"] == "MIRRORED_GEOMETRY"
    assert mirrored["shadow_sl"] == 101.0
    assert mirrored["shadow_tp"] == 98.0
    assert mirrored["geometry_valid"] is True

    invalid = derive_shadow_geometry(
        _market(entry=1.0, sl=0.5, tp=3.0), "WOULD_SHORT")
    assert invalid["shadow_geometry_type"] == "GEOMETRY_UNAVAILABLE"
    assert invalid["geometry_valid"] is False
    assert invalid["shadow_sl"] is None and invalid["shadow_tp"] is None


def test_shadow_store_is_separate_idempotent_and_reports_outcomes(tmp_path):
    campaign_path = tmp_path / "campaign.db"
    shadow_path = tmp_path / "adaptive-shadow.db"
    campaign = sqlite3.connect(campaign_path)
    campaign.execute("CREATE TABLE burnin_campaigns (campaign_id TEXT)")
    campaign.commit()

    draft = build_state_direction_shadow_draft(
        symbol="BTCUSDT", signal_id="signal-1", market_ctx=_market(),
        mtf=_mtf("LONG", "SHORT", "LONG"), execution_ctx=_execution_ctx(),
        horizon_bars=2, observed_at=DECISION_TS,
    )
    assert draft["shadow_final_direction"] == "WOULD_NO_TRADE"
    store = StateDirectionShadowStore(shadow_path)
    for _ in range(2):
        store.record(
            draft, actual_decision="REJECTED", actual_side="LONG",
            actual_reject_reason="MTF_REGIME_SETUP_MISMATCH",
        )

    with sqlite3.connect(shadow_path) as shadow:
        assert shadow.execute(
            "SELECT COUNT(*) FROM state_direction_shadow_decisions"
        ).fetchone()[0] == 1
        candles = {
            "BTCUSDT": [
                {"timestamp": "2026-01-01T00:01:00Z", "high": 100.5,
                 "low": 98.5, "is_closed": True},
                {"timestamp": "2026-01-01T00:02:00Z", "high": 100.0,
                 "low": 98.0, "is_closed": True},
            ]
        }
        first = resolve_state_direction_shadow_outcomes(
            shadow, candles, now="2026-01-01T00:03:00Z")
        second = resolve_state_direction_shadow_outcomes(
            shadow, candles, now="2026-01-01T00:03:00Z")
        assert first["resolved"] == 1
        assert second == {"resolved": 0, "incomplete": 0, "ambiguous": 0, "pending": 0}
        assert shadow.execute(
            "SELECT COUNT(*) FROM state_direction_shadow_outcomes"
        ).fetchone()[0] == 1
        outcome = shadow.execute(
            "SELECT forward_label,avoided_loss,missed_profit,evidence_complete "
            "FROM state_direction_shadow_outcomes"
        ).fetchone()
        assert outcome[0] == "SL_BEFORE_TP"
        assert outcome[1] > 0 and outcome[2] == 0
        assert outcome[3] == 1
        report = state_direction_shadow_report(
            shadow, group_by=("symbol", "shadow_final_direction"))
        assert report[0]["samples"] == 1
        assert report[0]["resolved"] == 1
        assert report[0]["WOULD_NO_TRADE"] == 1
        assert report[0]["avoided_losses"] > 0

    assert campaign.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE name LIKE 'state_direction_shadow_%'"
    ).fetchone()[0] == 0
    campaign.close()


def test_forward_evidence_future_open_and_stale_fail_closed():
    common = {
        "side": "LONG", "entry": 100.0, "stop": 99.0, "target": 102.0,
        "decision_timestamp": DECISION_TS,
        "due_at": "2026-01-01T00:02:00Z",
        "timeframe": "1m", "horizon_bars": 2,
    }
    cases = [
        [{"timestamp": "2026-01-01T00:03:00Z", "high": 103.0,
          "low": 99.5, "is_closed": True}],
        [{"timestamp": "2026-01-01T00:01:00Z", "high": 103.0,
          "low": 99.5, "is_closed": False}],
        [{"timestamp": "2026-01-01T00:01:00Z", "high": 100.5,
          "low": 99.5, "is_closed": True}],
    ]
    for candles in cases:
        result = evaluate_forward_outcome(**common, candles=candles)
        assert result["evidence_complete"] is False
        assert result["window_complete"] is False


def test_regime_guided_remains_authoritative_in_shadow():
    regime = {"timeframe": "1h", "direction": "SHORT", "regime": "TRENDING",
              "last_closed_candle_ms": DECISION_MS, "evidence_status": "COMPLETE"}
    setup = {"timeframe": "15m", "direction": "LONG", "phase": "PULLBACK",
             "trade_side": "SHORT", "generation_mode": "REGIME_GUIDED",
             "last_closed_candle_ms": DECISION_MS, "evidence_status": "COMPLETE"}
    execution = {"timeframe": "1m", "direction": "SHORT", "trigger": "CONFIRMED",
                 "confirmed_for_side": True, "last_closed_candle_ms": DECISION_MS,
                 "evidence_status": "COMPLETE"}
    actual = evaluate_mtf_alignment(
        regime, setup, execution, decision_ts_ms=DECISION_MS)
    shadow = evaluate_mtf_alignment(
        regime, setup, execution, decision_ts_ms=DECISION_MS,
        state_direction_resolution_enabled=True)
    assert actual == shadow
    assert shadow["generation_mode"] == "REGIME_GUIDED"
    assert shadow["direction"] == "SHORT"


def test_shadow_failure_leaves_actual_reject_geometry_counts_and_identity_unchanged(tmp_path):
    engines = [
        init_db(f"sqlite+pysqlite:///{tmp_path / 'baseline.db'}"),
        init_db(f"sqlite+pysqlite:///{tmp_path / 'shadow.db'}"),
    ]
    results = []
    for index, engine in enumerate(engines):
        rejects = []
        config = RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            require_mtf_alignment=True,
            enable_state_direction_resolution=False,
        )
        runtime = RuntimeOrchestrator(
            config=config,
            ai_brain=SimpleNamespace(),
            market_scanner=lambda: asyncio.sleep(0, result=[]),
            mtf_context_provider=_Provider(aligned=False),
            persistence_engine=engine,
            on_reject_persist=lambda payload, target=rejects: target.append(dict(payload)),
            state_direction_shadow_enabled=bool(index),
            state_direction_shadow_store=_FailingStore() if index else None,
        )
        asyncio.run(runtime._process_symbol(_selection()))
        with engine.connect() as conn:
            canonical = conn.execute(text(
                f"SELECT COUNT(*) FROM burnin_observations o WHERE {canonical_decision_sql('o')}"
            )).scalar_one()
            shadow_tables = conn.execute(text(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name LIKE 'state_direction_shadow_%'"
            )).scalar_one()
        results.append((runtime, rejects, canonical, shadow_tables))

    baseline, observed = results
    assert baseline[1][0]["decision"] == observed[1][0]["decision"] == "REJECTED"
    assert baseline[1][0]["side"] == observed[1][0]["side"] == "LONG"
    assert (baseline[1][0]["entry"], baseline[1][0]["sl"], baseline[1][0]["tp"]) == (
        observed[1][0]["entry"], observed[1][0]["sl"], observed[1][0]["tp"])
    assert baseline[0].metrics.executions == observed[0].metrics.executions == 0
    assert baseline[2] == observed[2] == 1
    assert baseline[3] == observed[3] == 0
    assert baseline[0]._qualification_report is observed[0]._qualification_report is None
    assert baseline[0].config is not observed[0].config
    assert baseline[0].config.enable_state_direction_resolution is False
    assert observed[0].config.enable_state_direction_resolution is False
    assert not hasattr(observed[0].config, "state_direction_shadow_enabled")
    assert burnin_config_hash(runtime_filter_config(
        baseline[0].config, mode="PAPER")) == burnin_config_hash(runtime_filter_config(
        observed[0].config, mode="PAPER"))
    strategy_fields = (
        "min_signal_score", "min_effective_rr", "min_rr",
        "regime_direction_threshold", "setup_direction_threshold",
        "execution_direction_threshold",
    )
    assert burnin_config_hash({
        key: getattr(baseline[0].config, key) for key in strategy_fields
    }) == burnin_config_hash({
        key: getattr(observed[0].config, key) for key in strategy_fields
    })


def test_shadow_enabled_accept_path_does_not_change_execution(tmp_path):
    shadow_path = tmp_path / "adaptive-shadow.db"
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            require_mtf_alignment=True,
            enable_state_direction_resolution=False,
        ),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        mtf_context_provider=_Provider(aligned=True),
        state_direction_shadow_enabled=True,
        state_direction_shadow_store=StateDirectionShadowStore(shadow_path),
    )
    asyncio.run(runtime._process_symbol(_selection(_market(market_ts=time.time()))))

    assert runtime.metrics.executions == 1
    assert runtime.config.enable_state_direction_resolution is False
    with sqlite3.connect(shadow_path) as conn:
        row = conn.execute(
            "SELECT actual_decision,actual_side,entry,base_sl,base_tp,"
            "shadow_final_direction,shadow_geometry_type "
            "FROM state_direction_shadow_decisions"
        ).fetchone()
    assert row == (
        "ACCEPTED", "LONG", 100.0, 99.0, 102.0,
        "WOULD_LONG", "SAME_SIDE_GEOMETRY",
    )
