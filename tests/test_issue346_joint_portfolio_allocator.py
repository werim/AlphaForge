from __future__ import annotations

from dataclasses import asdict, replace
import itertools
import sqlite3

import pytest
from sqlalchemy import create_engine, event

from alphaforge.portfolio_allocation_evidence import (
    PORTFOLIO_ALLOCATION_DDL,
    persist_portfolio_allocation,
)
from alphaforge.portfolio_risk import (
    PortfolioRiskSnapshot,
    allocate_portfolio_candidates,
    apply_candidate_allocation,
    correlation_group_for_symbol,
)
from alphaforge.order import _project_precomputed_portfolio_allocation


def _snapshot(**overrides) -> PortfolioRiskSnapshot:
    values = dict(
        mode="PAPER",
        timestamp="2026-10-02T00:00:00+00:00",
        equity=1_000.0,
        available_balance=1_000.0,
        open_position_count=0,
        max_open_positions=10,
        concurrent_position_count=0,
        max_concurrent_positions=10,
        total_notional_exposure=0.0,
        max_notional_exposure=1_000.0,
        symbol_notional_exposure=0.0,
        max_symbol_notional=1_000.0,
        side_exposure_long=0.0,
        side_exposure_short=0.0,
        net_exposure=0.0,
        gross_exposure=0.0,
        daily_loss_pct=0.0,
        max_daily_loss_pct=0.05,
        rolling_drawdown_pct=0.0,
        max_rolling_drawdown_pct=0.10,
        loss_cluster_active=False,
        symbol_loss_cluster_active=False,
        risk_state_complete=True,
        correlation_group="CRYPTO_MAJOR",
        correlation_group_exposure=0.0,
        max_correlation_group_exposure=1_000.0,
        correlated_position_count=0,
        max_correlated_positions=10,
    )
    values.update(overrides)
    return PortfolioRiskSnapshot(**values)


def _config(**overrides):
    values = dict(
        max_open_positions=10,
        max_concurrent_positions=10,
        max_notional_exposure=1_000.0,
        max_symbol_notional=1_000.0,
        max_same_side_exposure=1_000.0,
        max_net_exposure=1_000.0,
        max_correlation_group_exposure=1_000.0,
        max_correlated_positions=10,
        reject_unknown_portfolio_risk=True,
        universe_hash="universe:test",
        git_sha="abc123",
        release_id="release:test",
        runtime_identity="runtime:test",
    )
    values.update(overrides)
    return values


def _candidate(symbol: str, value: float, notional: float = 70.0, **extra):
    return {
        "candidate_id": f"candidate:{symbol}",
        "symbol": symbol,
        "side": "LONG",
        "notional": notional,
        "quantity": notional / 10.0,
        "entry": 10.0,
        "risk_at_stop_usdt": notional / 10.0,
        "expected_net_r": value,
        **extra,
    }


def _by_symbol(decision):
    return {candidate.symbol: candidate for candidate in decision.candidates}


def test_candidate_permutations_are_order_independent():
    candidates = [
        _candidate("BTCUSDT", 3.0),
        _candidate("ETHUSDT", 2.0),
        _candidate("SOLUSDT", 1.0),
    ]
    snapshot = _snapshot(max_correlation_group_exposure=100.0)
    config = _config(max_correlation_group_exposure=100.0)
    results = [
        allocate_portfolio_candidates(order, snapshot, config, mode="PAPER").to_dict()
        for order in itertools.permutations(candidates)
    ]
    assert all(result == results[0] for result in results)


def test_hard_gate_dominates_stronger_soft_score():
    strong = _candidate(
        "BTCUSDT", 100.0, hard_gate_accepted=False, hard_gate_reason="LOW_EFFECTIVE_RR"
    )
    weak = _candidate("ETHUSDT", 1.0)
    decision = allocate_portfolio_candidates([strong, weak], _snapshot(), _config(), mode="PAPER")
    rows = _by_symbol(decision)
    assert rows["BTCUSDT"].action == "REJECT"
    assert rows["BTCUSDT"].reason_codes == ("LOW_EFFECTIVE_RR",)
    assert rows["ETHUSDT"].action == "APPROVE"


def test_correlated_candidates_compete_and_stronger_displaces_weaker():
    decision = allocate_portfolio_candidates(
        [_candidate("ETHUSDT", 1.0), _candidate("BTCUSDT", 2.0)],
        _snapshot(max_correlation_group_exposure=100.0),
        _config(max_correlation_group_exposure=100.0),
        mode="PAPER",
    )
    rows = _by_symbol(decision)
    assert rows["BTCUSDT"].action == "APPROVE"
    assert rows["BTCUSDT"].allocated_notional == pytest.approx(70.0)
    assert rows["ETHUSDT"].action == "REDUCE_SIZE"
    assert rows["ETHUSDT"].allocated_notional == pytest.approx(30.0)
    assert sum(row.allocated_notional for row in rows.values()) == pytest.approx(100.0)


def test_same_side_exposure_cap_is_joint():
    decision = allocate_portfolio_candidates(
        [_candidate("BTCUSDT", 2.0), _candidate("SOLUSDT", 1.0)],
        _snapshot(),
        _config(max_same_side_exposure=100.0),
        mode="PAPER",
    )
    assert sum(row.allocated_notional for row in decision.candidates) == pytest.approx(100.0)
    assert any(row.action == "REDUCE_SIZE" for row in decision.candidates)


def test_gross_exposure_cap_is_joint():
    decision = allocate_portfolio_candidates(
        [_candidate("BTCUSDT", 2.0), _candidate("SOLUSDT", 1.0)],
        _snapshot(max_notional_exposure=100.0),
        _config(max_notional_exposure=100.0),
        mode="PAPER",
    )
    assert sum(row.allocated_notional for row in decision.candidates) == pytest.approx(100.0)


def test_net_exposure_cap_reduces_candidate():
    decision = allocate_portfolio_candidates(
        [_candidate("SOLUSDT", 1.0)],
        _snapshot(
            total_notional_exposure=60.0,
            side_exposure_long=60.0,
            net_exposure=60.0,
            gross_exposure=60.0,
        ),
        _config(max_net_exposure=100.0),
        mode="PAPER",
    )
    row = decision.candidates[0]
    assert row.action == "REDUCE_SIZE"
    assert row.allocated_notional == pytest.approx(40.0)


def test_drawdown_and_loss_cluster_only_preserve_or_reduce_allocation():
    candidate = [_candidate("BTCUSDT", 2.0)]
    normal = allocate_portfolio_candidates(candidate, _snapshot(), _config(), mode="PAPER")
    drawdown = allocate_portfolio_candidates(
        candidate,
        _snapshot(rolling_drawdown_pct=0.10),
        _config(),
        mode="PAPER",
    )
    loss_cluster = allocate_portfolio_candidates(
        candidate,
        _snapshot(loss_cluster_active=True),
        _config(),
        mode="PAPER",
    )
    assert drawdown.candidates[0].allocated_notional <= normal.candidates[0].allocated_notional
    assert loss_cluster.candidates[0].allocated_notional <= normal.candidates[0].allocated_notional
    assert drawdown.action == "HOLD_CASH"
    assert loss_cluster.action == "HOLD_CASH"


def test_unknown_portfolio_state_fails_closed():
    decision = allocate_portfolio_candidates(
        [_candidate("BTCUSDT", 2.0)],
        _snapshot(risk_state_complete=False),
        _config(),
        mode="PAPER",
    )
    assert decision.action == "HOLD_CASH"
    assert decision.candidates[0].reason_codes == ("UNKNOWN_PORTFOLIO_RISK",)


def test_no_capacity_is_first_class_hold_cash():
    decision = allocate_portfolio_candidates(
        [_candidate("BTCUSDT", 2.0)],
        _snapshot(total_notional_exposure=100.0, max_notional_exposure=100.0),
        _config(max_notional_exposure=100.0),
        mode="PAPER",
    )
    assert decision.action == "HOLD_CASH"
    assert decision.candidates[0].action == "REJECT"
    assert decision.candidates[0].allocated_notional == 0.0


def test_reduce_size_propagates_actual_notional_risk_and_quantity():
    decision = allocate_portfolio_candidates(
        [_candidate("BTCUSDT", 2.0)],
        _snapshot(max_notional_exposure=35.0),
        _config(max_notional_exposure=35.0),
        mode="PAPER",
    )
    row = decision.candidates[0]
    projected = apply_candidate_allocation(
        {"notional": 70.0, "quantity": 7.0, "effective_notional": 70.0}, row
    )
    assert row.action == "REDUCE_SIZE"
    assert projected["notional"] == pytest.approx(35.0)
    assert projected["effective_notional"] == pytest.approx(35.0)
    assert projected["quantity"] == pytest.approx(3.5)
    assert projected["allocated_risk"] == pytest.approx(3.5)
    order_projection, reject_reason = _project_precomputed_portfolio_allocation(
        {
            "entry": 10.0,
            "notional": 70.0,
            "quantity": 7.0,
            "portfolio_allocation": row.to_dict(),
        }
    )
    assert reject_reason == ""
    assert order_projection["notional"] == pytest.approx(35.0)
    assert order_projection["quantity"] == pytest.approx(3.5)


def test_same_inputs_replay_deterministically_and_do_not_mutate_snapshot():
    snapshot = _snapshot(max_correlation_group_exposure=100.0)
    before = asdict(snapshot)
    candidates = [_candidate("BTCUSDT", 2.0), _candidate("ETHUSDT", 1.0)]
    first = allocate_portfolio_candidates(candidates, snapshot, _config(max_correlation_group_exposure=100.0), mode="PAPER")
    second = allocate_portfolio_candidates(candidates, snapshot, _config(max_correlation_group_exposure=100.0), mode="PAPER")
    assert first == second
    assert asdict(snapshot) == before


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for statement in PORTFOLIO_ALLOCATION_DDL:
        conn.execute(statement)
    return conn


def test_candidate_evidence_persistence_batches_insert_and_verification(tmp_path):
    candidates = [
        _candidate(f"ALT{index:03d}USDT", float(100 - index), notional=1.0)
        for index in range(64)
    ]
    allocation = allocate_portfolio_candidates(
        candidates,
        _snapshot(
            max_open_positions=100,
            max_concurrent_positions=100,
            max_notional_exposure=10_000.0,
            max_symbol_notional=10_000.0,
            max_correlation_group_exposure=10_000.0,
            max_correlated_positions=100,
        ),
        _config(
            max_open_positions=100,
            max_concurrent_positions=100,
            max_notional_exposure=10_000.0,
            max_symbol_notional=10_000.0,
            max_same_side_exposure=10_000.0,
            max_net_exposure=10_000.0,
            max_correlation_group_exposure=10_000.0,
            max_correlated_positions=100,
        ),
        mode="PAPER",
    )
    assert len(allocation.candidates) == 64

    path = tmp_path / "portfolio-allocation-batch.db"
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    with engine.begin() as conn:
        for statement in PORTFOLIO_ALLOCATION_DDL:
            conn.exec_driver_sql(statement)

    statements: list[tuple[str, bool]] = []

    def capture(_conn, _cursor, statement, _parameters, _context, executemany):
        normalized = " ".join(str(statement).split())
        if "portfolio_allocation_candidates" in normalized:
            statements.append((normalized, bool(executemany)))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with engine.begin() as conn:
            assert persist_portfolio_allocation(conn, allocation)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    inserts = [
        item for item in statements
        if item[0].startswith("INSERT INTO portfolio_allocation_candidates")
    ]
    verifications = [
        item for item in statements
        if item[0].startswith("SELECT candidate_index")
    ]
    assert len(inserts) == 1
    assert inserts[0][1] is True
    assert len(verifications) == 1

    with engine.begin() as conn:
        assert persist_portfolio_allocation(conn, allocation)
    with engine.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM portfolio_allocation_candidates"
        ).scalar_one() == 64
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM portfolio_allocation_cycles"
        ).scalar_one() == 1
    engine.dispose()


def test_restart_persistence_is_idempotent_and_conflicts_fail_closed():
    allocation = allocate_portfolio_candidates(
        [_candidate("BTCUSDT", 2.0)], _snapshot(), _config(), mode="PAPER"
    )
    conn = _db()
    assert persist_portfolio_allocation(conn, allocation)
    assert persist_portfolio_allocation(conn, allocation)
    assert conn.execute("SELECT COUNT(*) FROM portfolio_allocation_cycles").fetchone()[0] == 1
    conflicting = replace(allocation, action="HOLD_CASH", evidence_hash="conflict")
    with pytest.raises(RuntimeError, match="PORTFOLIO_ALLOCATION_IDEMPOTENCY_CONFLICT"):
        persist_portfolio_allocation(conn, conflicting)


def test_candidate_permutation_persists_identical_allocation():
    candidates = [_candidate("ETHUSDT", 1.0), _candidate("BTCUSDT", 2.0)]
    config = _config(max_correlation_group_exposure=100.0)
    snapshot = _snapshot(max_correlation_group_exposure=100.0)
    first = allocate_portfolio_candidates(candidates, snapshot, config, mode="PAPER")
    second = allocate_portfolio_candidates(list(reversed(candidates)), snapshot, config, mode="PAPER")
    conn = _db()
    assert persist_portfolio_allocation(conn, first)
    assert persist_portfolio_allocation(conn, second)
    stored = conn.execute(
        "SELECT payload_json FROM portfolio_allocation_cycles WHERE allocation_cycle_id=?",
        (first.allocation_cycle_id,),
    ).fetchone()[0]
    assert stored
    assert first.evidence_hash == second.evidence_hash


def test_backtest_paper_live_precheck_share_pure_allocation_semantics():
    candidates = [_candidate("BTCUSDT", 2.0), _candidate("ETHUSDT", 1.0)]
    snapshot = _snapshot(max_correlation_group_exposure=100.0)
    config = _config(max_correlation_group_exposure=100.0)
    decisions = [
        allocate_portfolio_candidates(
            candidates, replace(snapshot, mode=mode), config, mode=mode
        )
        for mode in ("BACKTEST", "PAPER", "LIVE_PRECHECK")
    ]
    allocations = [
        [(row.symbol, row.action, row.allocated_notional) for row in decision.candidates]
        for decision in decisions
    ]
    assert allocations[0] == allocations[1] == allocations[2]


def test_allocator_reuses_canonical_correlation_authority():
    decision = allocate_portfolio_candidates(
        [_candidate("BTCUSDT", 2.0)], _snapshot(), _config(), mode="PAPER"
    )
    assert decision.candidates[0].correlation_group == correlation_group_for_symbol("BTCUSDT")
    assert allocate_portfolio_candidates.__module__ == "alphaforge.portfolio_risk"
