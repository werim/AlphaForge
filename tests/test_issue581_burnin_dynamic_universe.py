from __future__ import annotations

import asyncio
import sqlite3
import time
from decimal import Decimal

import pytest
from sqlalchemy import text

from alphaforge.binance_reconciliation_provider import (
    BinanceReadonlyReconciliationConfig,
    BinanceReadonlyReconciliationProvider,
)
from alphaforge.burnin_campaign import (
    DYNAMIC_UNIVERSE_SCOPE_MODE,
    build_phase8_campaign_identity,
    bootstrap_campaign_schema,
    campaign_uses_dynamic_universe,
    create_campaign,
    get_campaign,
)
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _candidate(symbol: str, volume: float, now: float) -> dict:
    return {
        "symbol": symbol,
        "source_exchange": "binance",
        "market_ts": now,
        "market_observed_at": now,
        "market_data_source": "TEST_BINANCE_PUBLIC",
        "volume_24h_usdt": volume,
        "spread_pct": 0.0002,
        "expected_slippage_pct": 0.0001,
        "volatility_pct": 0.02,
        "trend_strength": 0.7,
        "chop_score": 0.2,
        "liquidity_score": 0.9,
        "funding_rate_pct": 0.0,
        "funding_status": "MEASURED",
    }


def test_dynamic_campaign_identity_is_constraint_bound_and_replay_stable() -> None:
    cfg = RuntimeConfig(
        execution_mode=ExecutionMode.PAPER,
        universe_candidate_pool_top_n_volume=50,
        max_symbols_per_scan=5,
    )
    first = build_phase8_campaign_identity(
        cfg, [], ["1h"], release_id="rel", dynamic_universe=True
    )
    replay = build_phase8_campaign_identity(
        cfg, [], ["1h"], release_id="rel", dynamic_universe=True
    )
    changed = build_phase8_campaign_identity(
        RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            universe_candidate_pool_top_n_volume=40,
            max_symbols_per_scan=5,
        ),
        [],
        ["1h"],
        release_id="rel",
        dynamic_universe=True,
    )

    assert first == replay
    assert first["config_payload"]["campaign_universe_scope_mode"] == DYNAMIC_UNIVERSE_SCOPE_MODE
    assert first["config_payload"]["symbols"] == []
    assert first["universe_hash"] != changed["universe_hash"]
    with pytest.raises(ValueError, match="DYNAMIC_UNIVERSE_REQUIRES_EMPTY_FIXED_SYMBOLS"):
        build_phase8_campaign_identity(
            cfg, ["BTCUSDT"], ["1h"], release_id="rel", dynamic_universe=True
        )


def test_dynamic_campaign_persists_selector_owned_scope_without_fixed_allowlist() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_campaign_schema(conn)
    cfg = RuntimeConfig(execution_mode=ExecutionMode.PAPER)

    campaign = create_campaign(
        conn,
        release_id="dynamic-rel",
        duration_days=1,
        symbols=[],
        intervals=["1h"],
        runtime_config=cfg,
        source_provenance={"provider": "BINANCE_READ_ONLY_KLINES", "mode": "PAPER"},
        dynamic_universe=True,
    )
    conn.commit()
    stored = get_campaign(conn, campaign.campaign_id)
    assert stored is not None
    assert stored["symbols"] == []
    assert campaign_uses_dynamic_universe(stored)
    assert stored["source_provenance"]["universe_scope_mode"] == DYNAMIC_UNIVERSE_SCOPE_MODE


def test_attached_dynamic_campaign_runs_full_candidate_set_before_top5(monkeypatch, tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'dynamic.db'}")
    with engine.begin() as conn:
        bootstrap_campaign_schema(conn)
    now = time.time()
    rows = [_candidate(f"S{index:02d}USDT", 100_000_000 - index * 1000, now) for index in range(50)]
    processed: list[str] = []

    async def process(_self, selection):
        processed.append(selection.symbol)

    monkeypatch.setattr(RuntimeOrchestrator, "_process_symbol", process)
    monkeypatch.setattr(RuntimeOrchestrator, "_allocate_selected_candidates", lambda *args, **kwargs: True)

    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            universe_min_volume_24h_usd=1,
            universe_candidate_pool_top_n_volume=50,
            max_symbols_per_scan=5,
        ),
        ai_brain=None,
        market_scanner=lambda: asyncio.sleep(0, result=rows),
        scanner_source="BINANCE_PUBLIC_MARKET_DATA",
        persistence_engine=engine,
    )
    runtime._campaign_id = "camp_dynamic"
    runtime._burnin_run_id = "run_dynamic"
    runtime._campaign_dynamic_universe = True
    runtime._campaign_symbols = frozenset()
    runtime._campaign_source_exchanges = frozenset({"binance"})

    asyncio.run(runtime._scan_once())

    assert len(processed) == 5
    assert runtime.metrics.symbols_selected == 5
    with engine.connect() as conn:
        cycle = conn.execute(text(
            "SELECT candidate_count, selected_symbols_json FROM universe_selection_cycles"
        )).mappings().one()
        link = conn.execute(text(
            "SELECT campaign_id, burnin_run_id, cycle_id FROM burnin_universe_selection_links"
        )).mappings().one()
    assert cycle["candidate_count"] == 50
    assert link["campaign_id"] == "camp_dynamic"
    assert link["burnin_run_id"] == "run_dynamic"


def test_fixed_campaign_allowlist_behavior_is_preserved(monkeypatch, tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'fixed.db'}")
    with engine.begin() as conn:
        bootstrap_campaign_schema(conn)
    now = time.time()
    rows = [
        _candidate("BTCUSDT", 100_000_000, now),
        _candidate("ETHUSDT", 90_000_000, now),
        _candidate("SOLUSDT", 500_000_000, now),
    ]
    processed: list[str] = []

    async def process(_self, selection):
        processed.append(selection.symbol)

    monkeypatch.setattr(RuntimeOrchestrator, "_process_symbol", process)
    monkeypatch.setattr(RuntimeOrchestrator, "_allocate_selected_candidates", lambda *args, **kwargs: True)

    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            universe_min_volume_24h_usd=1,
            max_symbols_per_scan=5,
        ),
        ai_brain=None,
        market_scanner=lambda: asyncio.sleep(0, result=rows),
        persistence_engine=engine,
    )
    runtime._campaign_id = "camp_fixed"
    runtime._burnin_run_id = "run_fixed"
    runtime._campaign_dynamic_universe = False
    runtime._campaign_symbols = frozenset({"BTCUSDT", "ETHUSDT"})
    runtime._campaign_source_exchanges = frozenset({"binance"})

    asyncio.run(runtime._scan_once())
    assert set(processed) == {"BTCUSDT", "ETHUSDT"}
    assert "SOLUSDT" not in processed


def test_dynamic_campaign_still_enforces_provider_scope() -> None:
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=None,
        market_scanner=lambda: asyncio.sleep(0, result=[]),
    )
    runtime._burnin_run_id = "run"
    runtime._campaign_id = "camp"
    runtime._campaign_dynamic_universe = True
    runtime._campaign_symbols = frozenset()
    runtime._campaign_source_exchanges = frozenset({"binance"})

    runtime._assert_campaign_candidate("BTCUSDT", "binance", "TEST")
    with pytest.raises(RuntimeError, match="CAMPAIGN_UNIVERSE_RUNTIME_MISMATCH"):
        runtime._assert_campaign_candidate("BTCUSDT", "hyperliquid", "TEST")


def test_dynamic_reconciliation_does_not_fan_out_to_full_universe_user_trades() -> None:
    calls: list[str] = []

    def http(url, _headers, _timeout):
        calls.append(url)
        if "/fapi/v3/positionRisk" in url:
            return []
        if "/fapi/v1/openOrders" in url:
            return []
        if "/fapi/v1/userTrades" in url:
            raise AssertionError("dynamic universe must not fan out userTrades")
        raise AssertionError(f"unexpected endpoint: {url}")

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(
            base_url="https://fapi.binance.com",
            api_key="key",
            api_secret="secret",
            max_fill_symbols=10,
        ),
        tracked_symbols=lambda: set(),
        http_get_json=http,
    )
    snapshot = provider.snapshot()

    assert snapshot["evidence_status"] == "COMPLETE"
    assert snapshot["selected_symbols"] == []
    assert not any("/fapi/v1/userTrades" in call for call in calls)


def test_dynamic_reconciliation_fails_closed_when_real_exposure_exceeds_cap() -> None:
    def http(url, _headers, _timeout):
        if "/fapi/v3/positionRisk" in url:
            return [
                {
                    "symbol": f"S{index:02d}USDT",
                    "positionAmt": "1",
                    "entryPrice": "100",
                    "markPrice": "100",
                    "notional": "100",
                    "positionSide": "BOTH",
                }
                for index in range(11)
            ]
        if "/fapi/v1/openOrders" in url:
            return []
        if "/fapi/v1/userTrades" in url:
            return []
        raise AssertionError(f"unexpected endpoint: {url}")

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(
            base_url="https://fapi.binance.com",
            api_key="key",
            api_secret="secret",
            position_epsilon=Decimal("0.00000001"),
            max_fill_symbols=10,
        ),
        tracked_symbols=lambda: set(),
        http_get_json=http,
    )
    snapshot = provider.snapshot()

    assert snapshot["evidence_status"] == "INCOMPLETE"
    assert snapshot["failure_class"] is not None
    assert snapshot["selected_count"] == 11
