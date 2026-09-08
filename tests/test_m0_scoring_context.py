from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain
from alphaforge.multi_timeframe import (
    build_execution_context as build_mtf_execution_context,
    build_setup_context,
)
from alphaforge.order import after_position_close
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _runtime(engine=None, *, mode: ExecutionMode = ExecutionMode.PAPER) -> RuntimeOrchestrator:
    brain = AIBrain(Session(engine or init_db("sqlite+pysqlite:///:memory:")), min_accept_score=0.62)
    return RuntimeOrchestrator(
        RuntimeConfig(execution_mode=mode),
        brain,
        lambda: None,
        persistence_engine=engine,
    )


def _guided_market(**overrides):
    market = {
        "mode": "PAPER",
        "entry": 100.0,
        "rr": 2.0,
        "side": "LONG",
        "setup": "LONG_CONTINUATION",
        "spread_pct": 0.0002,
        "spread_bps": 2.0,
        "expected_slippage_pct": 0.0002,
        "latency_ms": 20.0,
        "mtf": {
            "setup": {"setup_quality": 0.82},
            "execution": {
                "momentum_confirmation": 0.74,
                "liquidity_score": 0.91,
                "volatility_fit": 0.67,
            },
            "regime": {"regime": "TRENDING", "alignment": 0.88},
            "alignment": {"aligned": True},
        },
    }
    market.update(overrides)
    return market


def test_guided_numeric_scoring_features_are_propagated() -> None:
    runtime = _runtime()
    market = _guided_market()
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="TREND")

    signal = runtime._build_signal(selection, market, signal_id="guided-numeric")
    scored_market, regime, stats = runtime._build_scoring_context(signal, market)

    assert signal["setup_quality"] == pytest.approx(0.82)
    assert scored_market["momentum_confirmation"] == pytest.approx(0.74)
    assert scored_market["liquidity_quality"] == pytest.approx(0.91)
    assert scored_market["volatility_fit"] == pytest.approx(0.67)
    assert regime["alignment"] == pytest.approx(0.88)
    assert stats["sample_size"] == 0
    assert scored_market["scoring_context_diagnostics"]["status"] == "COMPLETE"


def test_missing_or_qualitative_features_are_conservative_and_observable() -> None:
    runtime = _runtime()
    market = _guided_market(
        liquidity_quality="HIGH",
        volatility_fit="GOOD",
        mtf={"setup": {}, "execution": {}, "regime": {"regime": "TRENDING"}, "alignment": {"aligned": True}},
    )
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="TREND")
    signal = runtime._build_signal(selection, market, signal_id="guided-missing")
    scored_market, regime, stats = runtime._build_scoring_context(signal, market)

    score = runtime.ai_brain.score_signal(signal, scored_market, regime, stats)

    assert "setup_quality" not in signal
    assert "momentum_confirmation" not in scored_market
    assert "liquidity_quality" not in scored_market
    assert "volatility_fit" not in scored_market
    assert "alignment" not in regime
    assert set(score.probabilistic["missing_scoring_inputs"]) == {
        "setup_quality", "momentum_confirmation", "liquidity_quality",
        "volatility_fit", "regime_alignment",
    }
    assert scored_market["scoring_context_diagnostics"]["status"] == "SCORING_CONTEXT_INCOMPLETE"
    assert "SCORING_CONTEXT_INCOMPLETE" in score.probabilistic["warnings"]


def test_stats_context_reads_existing_expectancy_and_sample_size(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'stats.sqlite3'}")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO setup_expectancy_stats (setup,samples,expectancy) VALUES ('LONG_CONTINUATION',7,0.12)"
        ))
        connection.execute(text(
            "INSERT INTO regime_expectancy_stats (regime,samples,expectancy) VALUES ('TRENDING',5,0.08)"
        ))
        connection.execute(text(
            "INSERT INTO symbol_expectancy_stats (symbol,samples,expectancy) VALUES ('BTCUSDT',9,0.17)"
        ))
    runtime = _runtime(engine)

    stats = runtime._build_stats_context(
        {"symbol": "BTCUSDT", "setup": "LONG_CONTINUATION"},
        {"regime": "TRENDING"},
    )

    assert stats["setup"] == {"LONG_CONTINUATION": pytest.approx(0.12)}
    assert stats["regime"] == {"TRENDING": pytest.approx(0.08)}
    assert stats["symbol"] == {"BTCUSDT": pytest.approx(0.17)}
    assert stats["sample_size"] == 5


def test_stats_context_confidence_is_zero_when_one_scope_has_no_history(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'partial-stats.sqlite3'}")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO setup_expectancy_stats (setup,samples,expectancy) VALUES ('LONG_CONTINUATION',7,0.12)"
        ))
        connection.execute(text(
            "INSERT INTO symbol_expectancy_stats (symbol,samples,expectancy) VALUES ('BTCUSDT',90,0.17)"
        ))
    runtime = _runtime(engine)

    stats = runtime._build_stats_context(
        {"symbol": "BTCUSDT", "setup": "LONG_CONTINUATION"},
        {"regime": "TRENDING"},
    )

    assert stats["sample_size"] == 0
    assert stats["regime"] == {}


def test_closed_trade_updates_expectancy_statistics_once() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        shared = {"symbol": "BTCUSDT", "setup": "LONG_CONTINUATION", "regime": "TRENDING"}
        after_position_close(session, {**shared, "trade_id": "win", "pnl": 1.0}, {})
        after_position_close(session, {**shared, "trade_id": "loss", "pnl": -1.0}, {})
        row = session.execute(text(
            "SELECT samples,total_pnl,expectancy FROM setup_expectancy_stats "
            "WHERE setup='LONG_CONTINUATION'"
        )).mappings().one()

    assert dict(row) == {"samples": 2, "total_pnl": 0.0, "expectancy": 0.0}


def test_stats_context_has_zero_sample_size_without_history() -> None:
    runtime = _runtime()

    stats = runtime._build_stats_context(
        {"symbol": "BTCUSDT", "setup": "LONG_CONTINUATION"},
        {"regime": "TRENDING"},
    )

    assert stats == {"setup": {}, "regime": {}, "symbol": {}, "sample_size": 0}


def test_different_guided_inputs_do_not_collapse_to_default_profile() -> None:
    runtime = _runtime()
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="TREND")

    def score_for(market):
        signal = runtime._build_signal(selection, market)
        scored_market, regime, stats = runtime._build_scoring_context(signal, market)
        return runtime.ai_brain.score_signal(signal, scored_market, regime, stats)

    strong = score_for(_guided_market())
    weak = score_for(_guided_market(mtf={
        "setup": {"setup_quality": 0.21},
        "execution": {"momentum_confirmation": 0.18, "liquidity_score": 0.34, "volatility_fit": 0.27},
        "regime": {"regime": "TRENDING", "alignment": 0.41},
        "alignment": {"aligned": True},
    }))

    assert strong.components != weak.components
    assert strong.total_score > weak.total_score
    assert strong.probabilistic["p_win"] > weak.probabilistic["p_win"]
    assert strong.probabilistic["confidence"] > weak.probabilistic["confidence"]


def test_below_acceptance_score_has_explicit_low_score_flag() -> None:
    brain = AIBrain(Session(init_db("sqlite+pysqlite:///:memory:")), min_accept_score=0.62)

    score = brain.score_signal(
        {"symbol": "BTCUSDT", "risk_reward": 1.0, "setup_quality": 0.1},
        {"momentum_confirmation": 0.1, "liquidity_quality": 0.1, "volatility_fit": 0.1},
        {"alignment": 0.1},
        {"sample_size": 0},
    )

    assert score.total_score < brain.min_accept_score
    assert "low_score" in score.reason_flags
    assert "low_confidence" in score.reason_flags
    assert "negative_expectancy_after_costs" in score.reason_flags

    boundary_brain = AIBrain(
        Session(init_db("sqlite+pysqlite:///:memory:")),
        min_accept_score=score.total_score,
    )
    boundary = boundary_brain.score_signal(
        {"symbol": "BTCUSDT", "risk_reward": 1.0, "setup_quality": 0.1},
        {"momentum_confirmation": 0.1, "liquidity_quality": 0.1, "volatility_fit": 0.1},
        {"alignment": 0.1},
        {"sample_size": 0},
    )
    assert boundary.total_score == pytest.approx(boundary_brain.min_accept_score)
    assert "low_score" not in boundary.reason_flags
    assert "low_confidence" in boundary.reason_flags
    assert "negative_expectancy_after_costs" in boundary.reason_flags


def test_real_mtf_strength_fields_are_forwarded_without_rescaling() -> None:
    now = 10_000_000

    def candles(values):
        return [
            {"open_ts": index * 60_000, "open": value, "high": value,
             "low": value, "close": value, "volume": 1.0,
             "close_ts": now - (len(values) - index - 1) * 60_000}
            for index, value in enumerate(values)
        ]

    setup = build_setup_context(
        candles([100.0 + index * 0.1 for index in range(12)]),
        "15m",
        regime={"direction": "LONG"},
    )
    execution = build_mtf_execution_context(
        candles([100.0 + index * 0.1 for index in range(5)]),
        "1m",
        {"spread_pct": 0.0002, "expected_slippage_pct": 0.0002,
         "market_data_latency_ms": 20.0, "liquidity_score": 0.9},
        trade_side="LONG",
    )
    market = _guided_market(
        regime_alignment=0.8,
        volatility_fit=0.7,
        mtf={"setup": setup, "execution": execution,
             "regime": {"regime": "TRENDING"}, "alignment": {"aligned": True}},
    )
    runtime = _runtime()
    signal = runtime._build_signal(SimpleNamespace(symbol="BTCUSDT"), market)
    scored_market, _, _ = runtime._build_scoring_context(signal, market)

    assert signal["setup_quality"] == pytest.approx(setup["structure_quality"])
    assert scored_market["momentum_confirmation"] == pytest.approx(execution["ma_delta_strength"])
    diagnostics = scored_market["scoring_context_diagnostics"]
    assert diagnostics["sources"]["setup_quality"] == "signal.setup_quality"
    assert diagnostics["sources"]["momentum_confirmation"] == "mtf.execution.ma_delta_strength"


def test_context_rules_are_identical_for_paper_and_live_and_thresholds_unchanged() -> None:
    market = _guided_market()
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="TREND")
    contexts = []
    for mode in (ExecutionMode.PAPER, ExecutionMode.LIVE):
        runtime = _runtime(mode=mode)
        signal = runtime._build_signal(selection, {**market, "mode": mode.value})
        contexts.append(runtime._build_scoring_context(signal, {**market, "mode": mode.value}))
        assert runtime.config.min_signal_score == pytest.approx(0.62)
        assert runtime.config.min_effective_rr == pytest.approx(1.10)
        assert runtime.ai_brain.min_confidence == pytest.approx(0.35)

    paper, live = contexts
    assert paper[0]["scoring_context_diagnostics"] == live[0]["scoring_context_diagnostics"]
    assert paper[1:] == live[1:]


def test_real_runtime_decision_path_passes_built_context_to_ai_brain() -> None:
    captured = {}

    class CapturingBrain:
        def before_real_order(self, signal, market, regime, stats):
            captured.update(signal=signal, market=market, regime=regime, stats=stats)
            score = SimpleNamespace(total_score=0.1, components={}, reason_flags=["low_score"])
            plan = SimpleNamespace(decision="REJECTED", confidence=0.1)
            return score, plan, "test reject"

    market = _guided_market(
        symbol="BTCUSDT",
        market_ts=99_999_999_999.0,
        volume_24h_usdt=90_000_000.0,
        volatility_pct=0.4,
        trend_strength=0.9,
        liquidity_score=0.91,
        chop_score=0.1,
        equity=100_000.0,
        available_balance=100_000.0,
        notional=1_000.0,
    )
    selection = SimpleNamespace(
        symbol="BTCUSDT", regime_hint="TREND", diagnostics={"inputs": market}
    )
    runtime = RuntimeOrchestrator(
        RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        CapturingBrain(),
        lambda: None,
        on_reject_persist=lambda _payload: None,
    )

    asyncio.run(runtime._process_symbol(selection))

    assert captured["signal"]["setup_quality"] == pytest.approx(0.82)
    assert captured["market"]["momentum_confirmation"] == pytest.approx(0.74)
    assert captured["market"]["liquidity_quality"] == pytest.approx(0.91)
    assert captured["market"]["volatility_fit"] == pytest.approx(0.67)
    assert captured["regime"]["alignment"] == pytest.approx(0.88)
    assert captured["stats"]["sample_size"] == 0
