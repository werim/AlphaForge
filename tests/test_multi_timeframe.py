import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from alphaforge.execution import build_execution_context as build_canonical_execution_context
from alphaforge.burnin import (BurnInRun, bootstrap_burnin_schema,
                               canonical_decision_sql, persist_burnin_run)
from alphaforge.multi_timeframe import (BinanceMTFProvider, build_execution_context,
                                        build_setup_context, closed_candles,
                                        evaluate_mtf_alignment)
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.persistence import init_db

NOW = 10_000_000

def context(tf, direction, close=NOW, **extra):
    return {"timeframe": tf, "direction": direction, "last_closed_candle_ms": close,
            "evidence_status": "COMPLETE", **extra}

def aligned(direction="LONG"):
    return (context("1h", direction), context("15m", direction),
            context("1m", direction, trigger="CONFIRMED"))

def test_perfect_long_and_short_alignment():
    for direction in ("LONG", "SHORT"):
        result = evaluate_mtf_alignment(*aligned(direction), decision_ts_ms=NOW)
        assert result["aligned"] and result["direction"] == direction

def test_regime_setup_and_execution_mismatches_are_specific():
    r, s, e = aligned("SHORT")
    r["direction"] = "LONG"
    assert evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"] == ["MTF_REGIME_SETUP_MISMATCH"]
    r, s, e = aligned("LONG"); e["direction"] = "SHORT"
    assert evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"] == ["MTF_SETUP_EXECUTION_MISMATCH"]

def test_missing_layers_no_setup_and_stale_fail_closed():
    r, s, e = aligned()
    assert "MTF_REGIME_UNAVAILABLE" in evaluate_mtf_alignment(None, s, e, decision_ts_ms=NOW)["reasons"]
    assert "MTF_SETUP_UNAVAILABLE" in evaluate_mtf_alignment(r, None, e, decision_ts_ms=NOW)["reasons"]
    assert "MTF_EXECUTION_UNAVAILABLE" in evaluate_mtf_alignment(r, s, None, decision_ts_ms=NOW)["reasons"]
    s["direction"] = "NONE"
    assert "MTF_NO_VALID_SETUP" in evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"]
    r, s, e = aligned(); r["last_closed_candle_ms"] = NOW - 8_000_000
    assert "MTF_CONTEXT_STALE" in evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"]

def test_partial_and_future_candles_cannot_contaminate_decision():
    rows = [[0,"1","2",".5","1.5","10",9999], [5000,"1.5","99","1","98","10",15000]]
    selected = closed_candles(rows, timeframe="1h", decision_ts_ms=10_000)
    assert len(selected) == 1 and selected[0]["close"] == 1.5


def _execution_candles(closes):
    return [{"open_ts": i * 60_000, "open": close, "high": close, "low": close,
             "close": close, "volume": 1.0, "close_ts": NOW - (len(closes) - i - 1) * 60_000}
            for i, close in enumerate(closes)]


def _provider_rows(closes, decision_ms):
    rows = []
    for index, close in enumerate(closes):
        close_ms = decision_ms - (len(closes) - index - 1) * 60_000
        rows.append([close_ms - 59_999, str(close + .02), str(close + .08),
                     str(close - .08), str(close), "100", close_ms])
    return rows


def _setup_candles_for_delta(delta):
    recent = 700.0 * (1.0 + delta) / (7.0 - 5.0 * delta)
    return _execution_candles([100.0] * 7 + [recent] * 5)


def test_setup_threshold_default_recovers_observed_directional_strength():
    setup = build_setup_context(
        _setup_candles_for_delta(.000491), "15m", regime={"direction": "LONG"})

    assert setup["direction_threshold"] == .0003
    assert setup["ma_delta_strength"] == pytest.approx(.000491)
    assert setup["direction"] == "LONG"
    assert setup["phase"] == "CONTINUATION"


def test_same_setup_strength_remains_neutral_at_old_threshold():
    setup = build_setup_context(
        _setup_candles_for_delta(.000491), "15m", regime={"direction": "LONG"},
        direction_threshold=.0005)

    assert setup["direction"] == "NONE"
    assert setup["phase"] == "NO_SETUP"


def test_setup_classifies_countertrend_ma_as_regime_guided_pullback_not_long_trade():
    candles = _execution_candles([100 + i * .1 for i in range(12)])
    setup = build_setup_context(candles, "15m", regime={"direction": "SHORT"})

    assert setup["direction"] == "LONG"  # retained diagnostic observation
    assert setup["observed_direction"] == "LONG"
    assert setup["trade_side"] == "SHORT"
    assert setup["phase"] == "PULLBACK"
    assert setup["setup_type"] == "SHORT_PULLBACK"
    assert setup["candidate_ready"] is True


def test_setup_detects_reentry_ready_when_recent_momentum_returns_to_regime():
    candles = _execution_candles([100, 100.2, 100.4, 100.6, 100.8, 101, 101.2,
                                  102, 101.8, 101.6, 101.4, 101.2])
    setup = build_setup_context(candles, "15m", regime={"direction": "SHORT"})

    assert setup["direction"] == "LONG"
    assert setup["recent_direction"] == "SHORT"
    assert setup["trade_side"] == "SHORT"
    assert setup["phase"] == "REENTRY_READY"


def test_guided_pullback_uses_execution_as_regime_side_confirmation():
    regime = context("1h", "SHORT")
    setup = context("15m", "LONG", generation_mode="REGIME_GUIDED",
                    phase="PULLBACK", trade_side="SHORT", candidate_ready=True)
    execution = context("1m", "SHORT", trigger="MOMENTUM_CONFIRMED",
                        trade_side="SHORT", confirmed_for_side=True)

    result = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=NOW)

    assert result["aligned"] is True
    assert result["direction"] == "SHORT"
    assert result["setup_phase"] == "PULLBACK"
    assert "MTF_REGIME_SETUP_MISMATCH" not in result["reasons"]


def test_guided_execution_counter_regime_is_rejected_with_additive_reason():
    regime = context("1h", "SHORT")
    setup = context("15m", "LONG", generation_mode="REGIME_GUIDED",
                    phase="PULLBACK", trade_side="SHORT", candidate_ready=True)
    execution = context("1m", "LONG", trigger="MOMENTUM_CONFIRMED",
                        trade_side="SHORT", confirmed_for_side=False)

    result = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=NOW)

    assert result["aligned"] is False
    assert result["reasons"] == ["MTF_EXECUTION_COUNTER_REGIME"]


def test_guided_flat_setup_is_complete_no_setup_evidence_not_unavailable():
    candles = _execution_candles([100.0] * 12)
    setup = build_setup_context(candles, "15m", regime={"direction": "SHORT"})
    regime = context("1h", "SHORT")
    execution = context("1m", "SHORT", trigger="MOMENTUM_CONFIRMED",
                        trade_side="SHORT", confirmed_for_side=True)

    result = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=NOW)

    assert setup["evidence_status"] == "COMPLETE"
    assert setup["phase"] == "NO_SETUP"
    assert result["reasons"] == ["MTF_NO_VALID_SETUP"]
    assert "MTF_SETUP_UNAVAILABLE" not in result["reasons"]


def test_non_finite_setup_evidence_is_invalid_and_fails_closed():
    candles = _execution_candles([100 + i * .1 for i in range(12)])
    candles[-1]["close"] = float("nan")
    setup = build_setup_context(candles, "15m", regime={"direction": "LONG"})
    regime = context("1h", "LONG")
    execution = context("1m", "LONG", trigger="MOMENTUM_CONFIRMED",
                        trade_side="LONG", confirmed_for_side=True)

    result = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=NOW)

    assert setup["phase"] == "INVALID"
    assert setup["last_closed_candle_ms"] == NOW
    assert setup["candidate_ready"] is False
    assert setup["evidence_status"] == "INCOMPLETE"
    assert "MTF_SETUP_UNAVAILABLE" in result["reasons"]
    assert "MTF_NO_VALID_SETUP" in result["reasons"]


def test_provider_generates_regime_guided_candidate(monkeypatch):
    decision_ms = 20_000_000
    rows_by_tf = {
        "1h": _provider_rows([103 - i * .1 for i in range(24)], decision_ms),
        "15m": _provider_rows([100 + i * .1 for i in range(16)], decision_ms),
        "1m": _provider_rows([101 - i * .1 for i in range(8)], decision_ms),
    }
    provider = BinanceMTFProvider()
    monkeypatch.setattr(provider, "_fetch", lambda _symbol, timeframe: rows_by_tf[timeframe])
    canonical = {"spread_pct": .0002, "expected_slippage_pct": .0004,
                 "market_data_latency_ms": 20.0, "liquidity_score": .9}

    mtf = asyncio.run(provider.build("BTCUSDT", canonical, execution_ctx=canonical,
        decision_ts_ms=decision_ms, regime_timeframe="1h", setup_timeframe="15m",
        execution_timeframe="1m"))

    assert mtf["regime"]["direction"] == "SHORT"
    assert mtf["setup"]["direction"] == "LONG"
    assert mtf["setup"]["phase"] == "PULLBACK"
    assert mtf["alignment"]["aligned"] is True
    assert mtf["generation"]["mode"] == "REGIME_GUIDED"
    candidate = mtf["generation"]["candidate"]
    assert candidate["side"] == "SHORT"
    assert candidate["setup_type"] == "SHORT_PULLBACK"
    assert candidate["sl"] > candidate["entry"] > candidate["tp"]


def test_provider_rollback_mode_retains_legacy_equality_veto(monkeypatch):
    decision_ms = 20_000_000
    rows_by_tf = {
        "1h": _provider_rows([103 - i * .1 for i in range(24)], decision_ms),
        "15m": _provider_rows([100 + i * .1 for i in range(16)], decision_ms),
        "1m": _provider_rows([101 - i * .1 for i in range(8)], decision_ms),
    }
    provider = BinanceMTFProvider(guided_signal_generation_enabled=False)
    monkeypatch.setattr(provider, "_fetch", lambda _symbol, timeframe: rows_by_tf[timeframe])
    canonical = {"spread_pct": .0002, "expected_slippage_pct": .0004,
                 "latency_ms": 20.0, "liquidity_score": .9}

    mtf = asyncio.run(provider.build("BTCUSDT", canonical, execution_ctx=canonical,
        decision_ts_ms=decision_ms, regime_timeframe="1h", setup_timeframe="15m",
        execution_timeframe="1m"))

    assert mtf["generation"]["mode"] == "LEGACY_VETO"
    assert mtf["alignment"]["aligned"] is False
    assert "MTF_REGIME_SETUP_MISMATCH" in mtf["alignment"]["reasons"]


def test_neutral_execution_is_available_but_never_confirmed():
    market = {"spread_pct": .000348, "expected_slippage_pct": .001,
              "market_data_latency_ms": 374.0, "liquidity_score": 1.0}
    execution = build_execution_context(_execution_candles([100.0] * 5), "1m", market)
    regime, setup, _ = aligned("LONG")
    result = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=NOW)

    assert execution["evidence_status"] == "COMPLETE"
    assert execution["direction"] == "NEUTRAL" and execution["trigger"] is None
    assert execution["ma_delta_strength"] == 0.0
    assert execution["direction_threshold"] == .0005
    assert result["aligned"] is False
    assert "MTF_EXECUTION_NOT_CONFIRMED" in result["reasons"]
    assert "MTF_EXECUTION_UNAVAILABLE" not in result["reasons"]


@pytest.mark.parametrize("missing_field", ["spread_pct", "expected_slippage_pct", "latency_ms", "liquidity_score"])
def test_missing_required_execution_market_evidence_is_unavailable(missing_field):
    market = {"spread_pct": .0002, "expected_slippage_pct": .001,
              "latency_ms": 20.0, "liquidity_score": 1.0}
    market[missing_field] = None
    execution = build_execution_context(_execution_candles([100, 100.1, 100.2, 100.3, 100.4]), "1m", market)
    regime, setup, _ = aligned("LONG")
    result = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=NOW)
    assert execution["evidence_status"] == "INCOMPLETE"
    assert "MTF_EXECUTION_UNAVAILABLE" in result["reasons"]


def test_missing_execution_candles_remain_unavailable():
    execution = build_execution_context([], "1m", {"spread_pct": .0002,
        "expected_slippage_pct": .001, "latency_ms": 20.0, "liquidity_score": 1.0})
    regime, setup, _ = aligned("LONG")
    result = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=NOW)
    assert execution["evidence_status"] == "INCOMPLETE"
    assert "MTF_EXECUTION_UNAVAILABLE" in result["reasons"]


def test_realistic_binance_candidate_uses_canonical_normalized_execution_context(monkeypatch):
    decision_ms = 4_000_000
    rows = []
    for index in range(30):
        close = 100.0 + index * 0.1
        rows.append([index * 60_000, str(close - .02), str(close + .03), str(close - .03),
                     str(close), "100", min(index * 60_000 + 59_999, decision_ms)])
    provider = BinanceMTFProvider()
    monkeypatch.setattr(provider, "_fetch", lambda _symbol, _timeframe: rows)
    raw_market = {"spread_pct": 0.0002, "market_data_latency_ms": 42.0,
                  "liquidity_score": 0.9, "volatility_regime": "MODERATE"}
    assert "expected_slippage_pct" not in raw_market and "latency_ms" not in raw_market
    canonical = build_canonical_execution_context(raw_market)
    assert canonical["expected_slippage_pct"] is not None
    assert canonical["market_data_latency_ms"] == 42.0
    assert canonical["latency_ms"] is None

    mtf = asyncio.run(provider.build("BTCUSDT", raw_market, execution_ctx=canonical,
        decision_ts_ms=decision_ms, regime_timeframe="1h", setup_timeframe="15m",
        execution_timeframe="1m"))

    assert mtf["execution"]["evidence_status"] == "COMPLETE"
    assert mtf["execution"]["expected_slippage_pct"] == canonical["expected_slippage_pct"]
    assert mtf["execution"]["market_data_latency_ms"] == 42.0
    assert mtf["execution"]["latency_ms"] is None
    assert "MTF_EXECUTION_UNAVAILABLE" not in mtf["alignment"]["reasons"]


class _AlignedProvider:
    def __init__(self, direction):
        self.direction = direction
        self.execution_ctx = None

    async def build(self, _symbol, _market, *, execution_ctx, **_kwargs):
        self.execution_ctx = execution_ctx
        return {"provider": "BINANCE_FUTURES_CLOSED_KLINES", "alignment": {
            "aligned": True, "direction": self.direction, "reasons": [],
            "timeframes": {"regime": "1h", "setup": "15m", "execution": "1m"}}}


class _NeutralExecutionProvider:
    async def build(self, *_args, **_kwargs):
        regime, setup, _ = aligned("LONG")
        execution = context("1m", "NEUTRAL", trigger=None, ma_delta_strength=0.0,
                            direction_threshold=.0005)
        alignment = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=NOW)
        return {"provider": "BINANCE_FUTURES_CLOSED_KLINES", "regime": regime,
                "setup": setup, "execution": execution, "alignment": alignment}


class _GuidedProvider:
    async def build(self, *_args, **_kwargs):
        candidate = {"side": "SHORT", "entry": 100.0, "sl": 101.0, "tp": 98.0,
                     "rr": 2.0, "setup_type": "SHORT_PULLBACK", "setup_phase": "PULLBACK",
                     "geometry_status": "COMPLETE", "geometry_reason": None,
                     "geometry_source": "MTF_REGIME_GUIDED_CLOSED_KLINES"}
        return {"provider": "BINANCE_FUTURES_CLOSED_KLINES",
                "regime": {"regime": "TRENDING", "direction": "SHORT"},
                "setup": {"phase": "PULLBACK", "trade_side": "SHORT", "direction": "LONG"},
                "execution": {"direction": "SHORT", "confirmed_for_side": True},
                "alignment": {"aligned": True, "direction": "SHORT", "reasons": [],
                    "generation_mode": "REGIME_GUIDED", "setup_phase": "PULLBACK",
                    "timeframes": {"regime": "1h", "setup": "15m", "execution": "1m"}},
                "generation": {"mode": "REGIME_GUIDED", "evidence_status": "COMPLETE",
                               "candidate": candidate, "reason": None}}


class _CounterRegimeGuidedProvider:
    async def build(self, *_args, **_kwargs):
        return {"provider": "BINANCE_FUTURES_CLOSED_KLINES",
                "regime": {"regime": "TRENDING", "direction": "SHORT"},
                "setup": {"phase": "PULLBACK", "trade_side": "SHORT", "direction": "LONG"},
                "execution": {"direction": "LONG", "confirmed_for_side": False,
                              "ma_delta_strength": .001},
                "alignment": {"aligned": False, "direction": None,
                    "generation_mode": "REGIME_GUIDED", "setup_phase": "PULLBACK",
                    "reasons": ["MTF_EXECUTION_COUNTER_REGIME"],
                    "timeframes": {"regime": "1h", "setup": "15m", "execution": "1m"}},
                "generation": {"mode": "REGIME_GUIDED", "evidence_status": "INCOMPLETE",
                               "candidate": None, "reason": None}}


class _SequencedGuidedProvider:
    def __init__(self, setup_closes, confirmations):
        self.setup_closes = list(setup_closes)
        self.confirmations = list(confirmations)
        self.calls = 0

    async def build(self, *_args, **_kwargs):
        index = min(self.calls, len(self.setup_closes) - 1)
        setup_close = self.setup_closes[index]
        confirmed = self.confirmations[min(self.calls, len(self.confirmations) - 1)]
        self.calls += 1
        setup_phase = "PULLBACK" if any(self.confirmations) else "NO_SETUP"
        candidate = ({"side": "LONG", "entry": 100.0, "sl": 99.0, "tp": 102.0,
                      "rr": 2.0, "setup_type": "LONG_PULLBACK",
                      "setup_phase": "PULLBACK", "geometry_status": "COMPLETE",
                      "geometry_reason": None,
                      "geometry_source": "MTF_REGIME_GUIDED_CLOSED_KLINES"}
                     if confirmed else None)
        reasons = [] if confirmed else (["MTF_EXECUTION_NOT_CONFIRMED"]
                                         if setup_phase == "PULLBACK"
                                         else ["MTF_NO_VALID_SETUP"])
        return {"provider": "BINANCE_FUTURES_CLOSED_KLINES",
                "regime": {"timeframe": "1h", "regime": "TRENDING",
                           "direction": "LONG", "last_closed_candle_ms": 1},
                "setup": {"timeframe": "15m", "phase": setup_phase,
                          "trade_side": "LONG", "direction": "NONE",
                          "last_closed_candle_ms": setup_close},
                "execution": {"timeframe": "1m",
                              "direction": "LONG" if confirmed else "NEUTRAL",
                              "confirmed_for_side": confirmed,
                              "last_closed_candle_ms": self.calls},
                "alignment": {"aligned": confirmed,
                    "direction": "LONG" if confirmed else None, "reasons": reasons,
                    "generation_mode": "REGIME_GUIDED", "setup_phase": setup_phase,
                    "timeframes": {"regime": "1h", "setup": "15m", "execution": "1m"}},
                "generation": {"mode": "REGIME_GUIDED",
                    "evidence_status": "COMPLETE" if candidate else "INCOMPLETE",
                    "candidate": candidate, "reason": None}}


class _AlwaysAcceptBrain:
    def before_real_order(self, *_args, **_kwargs):
        plan = SimpleNamespace(decision="ACCEPTED", reason="", confidence=.9,
                               order_type="MARKET", limit_price=None, stop_price=None)
        return SimpleNamespace(total_score=.9, components={}), plan, "accepted"


def _runtime_candidate():
    return {"symbol": "BTCUSDT", "source_exchange": "binance", "side": "LONG",
            "entry": 100.0, "sl": 99.0, "tp": 102.0, "rr": 2.0,
            "setup_type": "BREAKOUT_UP", "geometry_status": "COMPLETE",
            "spread_pct": .0002, "market_data_latency_ms": 20.0,
            "liquidity_score": .9, "volume_24h_usdt": 100_000_000,
            "market_ts": time.time(), "timeframe": "1m", "equity": 100_000.0,
            "available_balance": 100_000.0, "notional": 1_000.0}


def _selection(candidate=None):
    candidate = candidate or _runtime_candidate()
    return SimpleNamespace(symbol="BTCUSDT", regime_hint="TREND",
                           diagnostics={"inputs": candidate})


def test_repeated_same_no_setup_candle_is_one_canonical_reject_and_counter(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'setup-idempotency.db'}")
    with engine.begin() as conn:
        bootstrap_burnin_schema(conn)
        persist_burnin_run(conn, BurnInRun("mtf-run", "rel", git_commit="g",
            config_hash="c", strategy_config_hash="s", universe_hash="u",
            source_provenance={"provider": "PAPER"}))
    rejects = []
    provider = _SequencedGuidedProvider([900_000, 900_000], [False, False])
    runtime = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True), SimpleNamespace(), lambda: asyncio.sleep(0, result=[]),
        mtf_context_provider=provider, persistence_engine=engine,
        on_reject_persist=lambda payload: rejects.append(payload))
    runtime._burnin_run_id = "mtf-run"

    asyncio.run(runtime._process_symbol(_selection()))
    asyncio.run(runtime._process_symbol(_selection()))
    restarted = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True), SimpleNamespace(), lambda: asyncio.sleep(0, result=[]),
        mtf_context_provider=_SequencedGuidedProvider([900_000], [False]),
        persistence_engine=engine, on_reject_persist=lambda payload: rejects.append(payload))
    restarted._burnin_run_id = "mtf-run"
    asyncio.run(restarted._process_symbol(_selection()))

    with engine.connect() as conn:
        canonical = conn.execute(text(
            f"SELECT COUNT(*) FROM burnin_observations o WHERE {canonical_decision_sql('o')}"
        )).scalar_one()
        counters = conn.execute(text(
            "SELECT sample_count,rejected_count FROM burnin_runs WHERE burnin_run_id='mtf-run'"
        )).one()
    assert len(rejects) == 1
    assert rejects[0]["setup_identity"] == "setup:BTCUSDT:15m:900000"
    assert canonical == 1
    assert tuple(counters) == (1, 1)


def test_new_closed_setup_candle_can_create_new_canonical_decision():
    rejects = []
    provider = _SequencedGuidedProvider([900_000, 1_800_000], [False, False])
    runtime = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True), SimpleNamespace(), lambda: asyncio.sleep(0, result=[]),
        mtf_context_provider=provider,
        on_reject_persist=lambda payload: rejects.append(payload))

    asyncio.run(runtime._process_symbol(_selection()))
    asyncio.run(runtime._process_symbol(_selection()))

    assert len(rejects) == 2
    assert rejects[0]["setup_identity"] != rejects[1]["setup_identity"]


def test_valid_setup_allows_later_execution_confirmation_but_only_one_entry():
    rejects = []
    provider = _SequencedGuidedProvider([900_000] * 3, [False, True, True])
    runtime = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True), _AlwaysAcceptBrain(),
        lambda: asyncio.sleep(0, result=[]), mtf_context_provider=provider,
        on_reject_persist=lambda payload: rejects.append(payload))

    asyncio.run(runtime._process_symbol(_selection()))
    asyncio.run(runtime._process_symbol(_selection()))
    runtime._active_positions.clear()
    runtime._symbol_cooldown_until.clear()
    asyncio.run(runtime._process_symbol(_selection()))

    assert [row["reason"] for row in rejects] == ["MTF_EXECUTION_NOT_CONFIRMED"]
    assert runtime.metrics.executions == 1
    assert runtime._accepted_setup_identities == {"setup:BTCUSDT:15m:900000"}


def test_runtime_replaces_independent_side_with_guided_candidate_before_quality_gates():
    rejects = []
    candidate = {"symbol": "BTCUSDT", "source_exchange": "binance", "side": "LONG",
        "entry": 100.0, "sl": 99.0, "tp": 102.0, "rr": 2.0,
        "setup_type": "BREAKOUT_UP", "geometry_status": "COMPLETE",
        "spread_pct": .02, "market_data_latency_ms": 20.0, "liquidity_score": .9,
        "volume_24h_usdt": 100_000_000, "market_ts": time.time()}
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="TREND",
                                diagnostics={"inputs": candidate})
    orchestrator = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True, max_spread_pct=.001), SimpleNamespace(),
        lambda: asyncio.sleep(0, result=[]), mtf_context_provider=_GuidedProvider(),
        on_reject_persist=lambda payload: rejects.append(payload))

    asyncio.run(orchestrator._process_symbol(selection))

    assert rejects[-1]["reason"] == "SPREAD_TOO_HIGH"
    assert rejects[-1]["side"] == "SHORT"
    assert rejects[-1]["setup_type"] == "SHORT_PULLBACK"
    assert rejects[-1]["mtf"]["shadow_evidence"]["would_have_been_side"] == "LONG"
    assert rejects[-1]["mtf"]["shadow_evidence"]["disposition"] == "COUNTER_REGIME_LEGACY_CANDIDATE"
    assert rejects[-1]["forward_label_subject"] == "GUIDED_CANDIDATE"
    assert orchestrator.metrics.mtf_guided_candidates_generated == 1
    assert orchestrator.metrics.mtf_legacy_candidates_shadowed == 1
    assert orchestrator.metrics.mtf_setup_pullback == 1


def test_counter_regime_candidate_remains_forward_labelled_with_mtf_provenance(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'guided-forward.db'}")
    candidate = {"symbol": "BTCUSDT", "source_exchange": "binance", "side": "LONG",
        "entry": 100.0, "sl": 99.0, "tp": 102.0, "rr": 2.0,
        "setup_type": "BREAKOUT_UP", "geometry_status": "COMPLETE",
        "spread_pct": .0002, "market_data_latency_ms": 20.0, "liquidity_score": .9,
        "volume_24h_usdt": 100_000_000, "market_ts": time.time(), "timeframe": "1m"}
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="TREND",
                                diagnostics={"inputs": candidate})
    orchestrator = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True, reject_forward_horizon_bars=2), SimpleNamespace(),
        lambda: asyncio.sleep(0, result=[]), mtf_context_provider=_CounterRegimeGuidedProvider(),
        persistence_engine=engine)
    orchestrator._burnin_run_id = "guided-forward-run"

    asyncio.run(orchestrator._process_symbol(selection))

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT reject_reason,side,source_provenance_json "
            "FROM burnin_pending_reject_labels"
        )).mappings().one()
    provenance = json.loads(row["source_provenance_json"])
    assert row["reject_reason"] == "MTF_EXECUTION_COUNTER_REGIME"
    assert row["side"] == "LONG"
    assert provenance["mtf"]["setup"]["phase"] == "PULLBACK"
    assert provenance["mtf"]["setup"]["trade_side"] == "SHORT"
    assert provenance["forward_label_subject"] == "LEGACY_SCANNER_SHADOW_CANDIDATE"
    assert provenance["forward_label_side"] == "LONG"
    assert orchestrator.metrics.mtf_execution_counter_regime == 1
    assert orchestrator.metrics.mtf_direction_mismatch == 1


def test_paper_neutral_execution_rejects_before_ai_brain():
    class BrainMustNotRun:
        async def before_real_order(self, *_args, **_kwargs):
            raise AssertionError("AIBrain must not run after an MTF rejection")

    rejects = []
    candidate = {"symbol": "BTCUSDT", "source_exchange": "binance", "side": "LONG",
        "spread_pct": .0002, "market_data_latency_ms": 20.0, "liquidity_score": 1.0,
        "market_ts": NOW / 1000}
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="FAVORABLE",
                                diagnostics={"inputs": candidate})
    orchestrator = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True), BrainMustNotRun(), lambda: asyncio.sleep(0, result=[]),
        mtf_context_provider=_NeutralExecutionProvider(),
        on_reject_persist=lambda payload: rejects.append(payload))

    asyncio.run(orchestrator._process_symbol(selection))

    assert rejects[-1]["reason"] == "MTF_EXECUTION_NOT_CONFIRMED"
    assert rejects[-1]["decision"] == "REJECTED"
    assert "MTF_EXECUTION_UNAVAILABLE" not in rejects[-1]["mtf"]["alignment"]["reasons"]
    assert orchestrator.metrics.mtf_execution_not_confirmed == 1
    assert orchestrator.metrics.mtf_execution_missing == 0


@pytest.mark.parametrize(("mtf_side", "geometry_side", "expected_reason"), [
    ("LONG", "LONG", "SPREAD_TOO_HIGH"),
    ("SHORT", "SHORT", "SPREAD_TOO_HIGH"),
    ("LONG", "SHORT", "MTF_DIRECTION_MISMATCH"),
    ("SHORT", "LONG", "MTF_DIRECTION_MISMATCH"),
])
def test_runtime_binds_aligned_direction_to_geometry_and_persists_reject(
        mtf_side, geometry_side, expected_reason):
    provider = _AlignedProvider(mtf_side)
    rejects = []
    candidate = {"symbol": "BTCUSDT", "source_exchange": "binance", "side": geometry_side,
        "entry": 100.0, "sl": 99.0 if geometry_side == "LONG" else 101.0,
        "tp": 102.0 if geometry_side == "LONG" else 98.0, "rr": 2.0,
        "spread_pct": 0.02, "market_data_latency_ms": 31.0, "liquidity_score": .9,
        "volume_24h_usdt": 100_000_000, "market_ts": time.time()}
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="FAVORABLE",
        diagnostics={"inputs": candidate})
    orchestrator = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True, max_spread_pct=.001), SimpleNamespace(),
        lambda: asyncio.sleep(0, result=[]), mtf_context_provider=provider,
        on_reject_persist=lambda payload: rejects.append(payload))

    asyncio.run(orchestrator._process_symbol(selection))

    assert rejects and rejects[-1]["reason"] == expected_reason
    assert provider.execution_ctx["market_data_latency_ms"] == 31.0
    assert provider.execution_ctx["latency_ms"] == 50.0
    assert provider.execution_ctx["latency_status"] == "MODEL_ESTIMATE"
    assert provider.execution_ctx["latency_source"] == "CONFIGURED_PAPER_ASSUMPTION"
    if expected_reason == "MTF_DIRECTION_MISMATCH":
        assert rejects[-1]["mtf"]["alignment"]["aligned"] is False


def test_non_binance_source_is_not_evaluated_with_binance_provider():
    provider = _AlignedProvider("LONG")
    rejects = []
    candidate = {"symbol": "BTCUSDT", "source_exchange": "hyperliquid", "side": "LONG",
        "spread_pct": .0001, "market_data_latency_ms": 20.0, "liquidity_score": .9,
        "market_ts": 4_000.0}
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="FAVORABLE",
        diagnostics={"inputs": candidate})
    orchestrator = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True), SimpleNamespace(), lambda: asyncio.sleep(0, result=[]),
        mtf_context_provider=provider, on_reject_persist=lambda payload: rejects.append(payload))

    asyncio.run(orchestrator._process_symbol(selection))

    assert provider.execution_ctx is None
    assert rejects[-1]["reason"] == "MTF_EXECUTION_UNAVAILABLE"
    assert rejects[-1]["mtf"]["provenance_error"] == "UNSUPPORTED_MTF_PROVIDER"
