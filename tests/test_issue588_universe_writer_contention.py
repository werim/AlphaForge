from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from sqlalchemy import event, text

from alphaforge.burnin_campaign import bootstrap_campaign_schema
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.symbol_selector import (
    UniverseConstraints,
    build_selected_universe,
    selection_results,
)
from alphaforge.universe_evidence import persist_universe_selection


def _candidate(symbol: str, now: float, *, volume: float = 100_000_000.0) -> dict:
    return {
        "symbol": symbol,
        "source_exchange": "binance",
        "instrument_status": "TRADING",
        "contract_type": "PERPETUAL",
        "quote_asset": "USDT",
        "market_ts": now,
        "market_observed_at": now,
        "market_data_source": "TEST_BINANCE_PUBLIC",
        "entry": 100.0,
        "volume_24h_usdt": volume,
        "spread_pct": 0.0002,
        "spread_status": "MEASURED",
        "expected_slippage_pct": 0.0001,
        "volatility_pct": 0.02,
        "volatility_status": "MEASURED",
        "trend_strength": 0.7,
        "chop_score": 0.2,
        "liquidity_score": 0.9,
        "funding_rate_pct": 0.0,
        "funding_status": "MEASURED",
        "timeframe": "1m",
    }


def _selection(rows: list[dict], now: float):
    return build_selected_universe(
        rows,
        UniverseConstraints(
            min_volume_24h_usd=1.0,
            candidate_pool_top_n_volume=50,
            max_active_symbols=5,
        ),
        decision_timestamp=now,
        execution_mode="PAPER",
        git_sha="a" * 40,
        strategy_config_hash="strategy",
    )


def test_large_universe_candidate_persistence_is_batched_and_idempotent(tmp_path) -> None:
    path = tmp_path / "batched-universe.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    now = time.time()
    selection = _selection(
        [
            _candidate(
                f"S{index:03d}USDT",
                now,
                volume=500_000_000.0 - index * 1_000.0,
            )
            for index in range(520)
        ],
        now,
    )

    statements: list[tuple[str, bool]] = []

    def capture(_conn, _cursor, statement, _parameters, _context, executemany):
        normalized = " ".join(str(statement).split())
        if "universe_selection_candidates" in normalized:
            statements.append((normalized, bool(executemany)))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with engine.begin() as conn:
            assert persist_universe_selection(conn, selection)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    inserts = [
        item for item in statements
        if item[0].startswith("INSERT INTO universe_selection_candidates")
    ]
    verifications = [
        item for item in statements
        if item[0].startswith("SELECT candidate_index")
    ]
    assert len(inserts) == 1
    assert inserts[0][1] is True
    assert len(verifications) == 1

    with engine.begin() as conn:
        assert persist_universe_selection(conn, selection)
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM universe_selection_candidates "
                "WHERE cycle_id=:cycle_id"
            ),
            {"cycle_id": selection.cycle_id},
        ).scalar_one() == 520
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM universe_selection_cycles "
                "WHERE cycle_id=:cycle_id"
            ),
            {"cycle_id": selection.cycle_id},
        ).scalar_one() == 1


def test_universe_busy_is_controlled_recovery_required_and_replay_exact_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-universe-busy.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    with engine.begin() as conn:
        bootstrap_campaign_schema(conn)
    now = time.time()
    rows = [_candidate("BTCUSDT", now)]

    async def process(_self, _selection):
        return None

    monkeypatch.setattr(RuntimeOrchestrator, "_process_symbol", process)
    monkeypatch.setattr(
        RuntimeOrchestrator,
        "_allocate_selected_candidates",
        lambda *_args, **_kwargs: True,
    )

    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            universe_min_volume_24h_usd=1.0,
            max_symbols_per_scan=1,
        ),
        ai_brain=None,
        market_scanner=lambda: asyncio.sleep(0, result=rows),
        persistence_engine=engine,
    )
    runtime._campaign_id = "camp-588"
    runtime._burnin_run_id = "run-588"
    runtime._campaign_dynamic_universe = True
    runtime._campaign_symbols = frozenset()
    runtime._campaign_source_exchanges = frozenset({"binance"})

    blocker = sqlite3.connect(path, timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        asyncio.run(runtime._scan_once())
    finally:
        blocker.rollback()
        blocker.close()

    assert runtime.metrics.universe_selection_persistence_failures == 1
    assert runtime._burnin_evidence_incomplete is True
    assert runtime._recovery_required is True
    assert runtime._runtime_status == "RECOVERY_REQUIRED"
    assert runtime._fail_closed_reason == "UNIVERSE_SELECTION_EVIDENCE_PERSISTENCE_FAILED"

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM universe_selection_cycles")).scalar_one() == 0
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30000

    asyncio.run(runtime._scan_once())

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM universe_selection_cycles")).scalar_one() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM burnin_universe_selection_links")).scalar_one() == 1
    assert runtime._recovery_required is True


def test_non_busy_universe_persistence_failure_remains_fatal(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-universe-fatal.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    now = time.time()

    def fail_non_busy(*_args, **_kwargs):
        raise RuntimeError("UNIVERSE_SCHEMA_BROKEN")

    monkeypatch.setattr("alphaforge.runtime.persist_universe_selection", fail_non_busy)
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            universe_min_volume_24h_usd=1.0,
            max_symbols_per_scan=1,
        ),
        ai_brain=None,
        market_scanner=lambda: asyncio.sleep(
            0, result=[_candidate("BTCUSDT", now)]
        ),
        persistence_engine=engine,
    )

    with pytest.raises(RuntimeError, match="UNIVERSE_SCHEMA_BROKEN"):
        asyncio.run(runtime._scan_once())


def test_portfolio_allocation_busy_marks_recovery_required(
    tmp_path,
) -> None:
    path = tmp_path / "runtime-allocation-busy.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    now = time.time()
    selection = _selection([_candidate("BTCUSDT", now)], now)
    selected = [
        row
        for row in selection_results(selection, {})
        if bool(row.diagnostics.get("selected"))
    ]
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            paper_candidate_notional=10.0,
            universe_min_volume_24h_usd=1.0,
            max_symbols_per_scan=1,
        ),
        ai_brain=None,
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        persistence_engine=engine,
    )

    blocker = sqlite3.connect(path, timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        persisted = runtime._allocate_selected_candidates(selected, selection)
    finally:
        blocker.rollback()
        blocker.close()

    assert persisted is False
    assert runtime.metrics.portfolio_allocation_persistence_failures == 1
    assert runtime._burnin_evidence_incomplete is True
    assert runtime._recovery_required is True
    assert runtime._runtime_status == "RECOVERY_REQUIRED"
    assert runtime._fail_closed_reason == "PORTFOLIO_ALLOCATION_EVIDENCE_PERSISTENCE_FAILED"
