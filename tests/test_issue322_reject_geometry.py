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


def test_production_binance_scanner_to_pending_reject_label(monkeypatch, tmp_path) -> None:
    """Exercise the production chain without injecting candidate geometry."""
    from alphaforge.config import load_config_from_env
    from alphaforge.exchange_market_scanner import enrich_selected_market_geometry, scan_exchange_markets
    class Response:
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(self.payload).encode()

    payloads = iter([
        {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]},
        [{"symbol": "BTCUSDT", "lastPrice": "100", "lowPrice": "1", "highPrice": "999",
          "quoteVolume": "90000000", "priceChangePercent": "1.2"}],
        [{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1"}],
        [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}],
        [[0, "98", "100", "97", "99", "10"],
         [60_000, "99", "101", "98", "100", "12"],
         [120_000, "100", "500", "1", "400", "1"]],
    ])
    monkeypatch.setenv("HYPERLIQUID_ENABLED", "false")
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response(next(payloads)))
    cfg = load_config_from_env()
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'production-chain.db'}")

    async def scanner():
        return await scan_exchange_markets(cfg)

    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, reject_forward_horizon_bars=2),
        ai_brain=AIBrain(session_factory=sessionmaker(bind=engine, expire_on_commit=False, future=True),
                         min_accept_score=11.0),
        market_scanner=scanner,
        selected_candidate_enricher=lambda candidates: enrich_selected_market_geometry(candidates, cfg),
        persistence_engine=engine,
    )
    runtime._burnin_run_id = "issue-323-production-chain"
    asyncio.run(runtime._scan_once())

    reject = runtime._reject_log[-1]
    assert reject["reason"] == "SCORE BELOW THRESHOLD OR NEGATIVE EXPECTANCY."
    assert reject["entry"] == 100.0 and reject["sl"] == 97.0
    assert reject["tp"] > reject["entry"]
    assert runtime.metrics.symbols_selected == 1
    assert runtime.metrics.executions == 0 and runtime._pending_orders == {} and runtime._active_positions == {}
    with engine.connect() as conn:
        pending = conn.execute(text(
            "SELECT symbol,side,entry,stop,target,timeframe FROM burnin_pending_reject_labels"
        )).one()
        assert pending.symbol == "BTCUSDT" and pending.side == "LONG" and pending.timeframe == "1m"
        assert pending.stop == 97.0 and pending.target > pending.entry
        assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM positions")).scalar_one() == 0


def test_shared_geometry_short_and_phase_b_parity_are_stable() -> None:
    from alphaforge.agents.contracts import AgentStage, DecisionStatus, StageInput
    from alphaforge.agents.phase_b import QualityAgent, SignalAgent
    from alphaforge.signal_geometry import build_breakout_geometry

    geometry = build_breakout_geometry(
        {"open": 100, "high": 101, "low": 97, "close": 98},
        {"open": 102, "high": 103, "low": 99, "close": 101},
    )
    assert geometry["side"] == "SHORT" and geometry["sl"] == 103.0 and geometry["tp"] < 98.0
    payload = {**geometry, "symbol": "BTCUSDT", "score": 8.0, "score_components": {"trend": 8.0},
               "decision": "REJECTED", "reject_reason": "STOP_TOO_WIDE",
               "spread_pct": 0.0002, "expected_slippage_pct": 0.0001,
               "liquidity_score": 0.9, "regime": "TREND"}
    stage = StageInput("d", "c", "PAPER", AgentStage.SIGNAL, "BTCUSDT", payload, ())
    signal = SignalAgent().run(stage)
    assert signal.status is DecisionStatus.PASS
    assert signal.evidence["raw_rr"] == pytest.approx(geometry["rr"])
    quality_stage = StageInput("d", "c", "PAPER", AgentStage.QUALITY, "BTCUSDT", payload, (signal,))
    quality = QualityAgent().run(quality_stage)
    assert quality.evidence["rr_difference"] == pytest.approx(0.0)
    assert quality.evidence["parity_status"] == "MATCH"
    assert quality.evidence["quality_diagnostics"]["sl_pct"] == pytest.approx(
        abs(geometry["entry"] - geometry["sl"]) / geometry["entry"] * 100
    )


def _selection_candidate(symbol: str, *, volume: float, source: str = "binance") -> dict:
    return {
        "symbol": symbol, "source_exchange": source, "entry": 100.0, "side": "LONG",
        "timeframe": "1m", "market_ts": 99_999_999_999.0, "volume_24h_usdt": volume,
        "spread_pct": 0.0002, "expected_slippage_pct": 0.0001, "funding_rate_pct": 0.0,
        "volatility_pct": 0.01, "trend_strength": 0.9, "liquidity_score": 0.9,
        "chop_score": 0.1, "regime": "TREND",
    }


@pytest.mark.parametrize("limit", [5, 2])
def test_canonical_selection_precedes_and_bounds_geometry_enrichment(monkeypatch, limit: int) -> None:
    from alphaforge.config import load_config_from_env
    from alphaforge.exchange_market_scanner import enrich_selected_market_geometry

    candidates = []
    for i in range(30):
        row = _selection_candidate(f"S{i:02d}USDT", volume=90_000_000)
        if i < 25:
            row.update({"liquidity_score": 0.5, "trend_strength": 0.3, "spread_pct": 0.002})
        candidates.append(row)
    enriched_symbols: list[str] = []

    async def scanner():
        return candidates

    def geometry(_base_url, symbol, *, timeout_sec):
        enriched_symbols.append(symbol)
        return {"entry": 100.0, "side": "LONG", "sl": 99.0, "tp": 101.3, "rr": 1.3}

    monkeypatch.setattr("alphaforge.exchange_market_scanner._binance_kline_geometry", geometry)
    cfg = load_config_from_env()

    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, max_symbols_per_scan=limit),
        ai_brain=_EarlyRejectBrain("Score below threshold or negative expectancy."),
        market_scanner=scanner,
        selected_candidate_enricher=lambda selected: enrich_selected_market_geometry(selected, cfg),
    )
    asyncio.run(runtime._scan_once())

    # The highest-ranked symbols are at the end of the raw 30-row universe,
    # proving enrichment did not truncate before canonical select_symbols().
    assert set(enriched_symbols) == {f"S{i:02d}USDT" for i in range(25, 25 + limit)}
    assert len(enriched_symbols) == limit
    assert runtime.metrics.symbols_selected == limit


def test_zero_selected_symbols_skips_geometry_enrichment() -> None:
    called = False

    async def enricher(selected):
        nonlocal called
        called = True
        return selected

    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, max_symbols_per_scan=5),
        ai_brain=_EarlyRejectBrain("reject"),
        market_scanner=lambda: asyncio.sleep(0, result=[
            {**_selection_candidate("ILLQUSDT", volume=1.0), "liquidity_score": 0.01}
        ]),
        selected_candidate_enricher=enricher,
    )
    asyncio.run(runtime._scan_once())
    assert called is False and runtime.metrics.symbols_selected == 0


def test_geometry_provider_enriches_only_unique_selected_binance_symbols(monkeypatch) -> None:
    from alphaforge.config import load_config_from_env
    from alphaforge.exchange_market_scanner import enrich_selected_market_geometry

    calls: list[str] = []

    def geometry(_base_url, symbol, *, timeout_sec):
        calls.append(symbol)
        return {"entry": 100.0, "side": "LONG", "sl": 99.0, "tp": 101.3, "rr": 1.3}

    monkeypatch.setattr("alphaforge.exchange_market_scanner._binance_kline_geometry", geometry)
    selected = [
        _selection_candidate("BTCUSDT", volume=10_000_000),
        _selection_candidate("ETHUSDT", volume=9_000_000, source="hyperliquid"),
        _selection_candidate("BTCUSDT", volume=8_000_000),
    ]
    result = asyncio.run(enrich_selected_market_geometry(selected, load_config_from_env()))
    assert calls == ["BTCUSDT"]
    assert "sl" in result[0] and "sl" not in result[1] and "sl" in result[2]


def test_geometry_timeout_remains_incomplete_and_non_executing(monkeypatch, tmp_path) -> None:
    from alphaforge.config import load_config_from_env
    from alphaforge.exchange_market_scanner import enrich_selected_market_geometry

    monkeypatch.setattr("alphaforge.exchange_market_scanner._fetch_json",
                        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("timed out")))
    candidate = _selection_candidate("BTCUSDT", volume=90_000_000)
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'geometry-timeout.db'}")
    cfg = load_config_from_env()
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, max_symbols_per_scan=5),
        ai_brain=_EarlyRejectBrain("Score below threshold or negative expectancy."),
        market_scanner=lambda: asyncio.sleep(0, result=[candidate]),
        selected_candidate_enricher=lambda rows: enrich_selected_market_geometry(rows, cfg),
        persistence_engine=engine,
    )
    runtime._burnin_run_id = "geometry-timeout"
    asyncio.run(runtime._scan_once())
    assert runtime._reject_log[-1]["sl"] is None and runtime._reject_log[-1]["tp"] is None
    assert runtime.metrics.scans == 1 and runtime.metrics.symbols_selected == 1
    assert runtime.metrics.executions == 0 and runtime._pending_orders == {} and runtime._active_positions == {}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM burnin_pending_reject_labels")).scalar_one() == 0
        missing = conn.execute(text(
            "SELECT missing_fields_json FROM burnin_observations "
            "WHERE observation_id LIKE 'incomplete_reject_geometry_%'"
        )).scalar_one()
        assert json.loads(missing) == ["stop", "target"]


def test_geometry_programmer_error_propagates_without_false_scan_progress() -> None:
    async def broken_enricher(_selected):
        raise RuntimeError("geometry contract bug")

    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, max_symbols_per_scan=5),
        ai_brain=_EarlyRejectBrain("reject"),
        market_scanner=lambda: asyncio.sleep(
            0, result=[_selection_candidate("BTCUSDT", volume=90_000_000)]
        ),
        selected_candidate_enricher=broken_enricher,
    )
    with pytest.raises(RuntimeError, match="geometry contract bug"):
        asyncio.run(runtime._scan_once())
    assert runtime.metrics.scans == 1
    assert runtime.metrics.last_scan_ts is None
    assert runtime.metrics.decisions_generated == 0
