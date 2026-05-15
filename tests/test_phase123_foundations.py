from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.persistence import init_db, save_order_decision, save_trade_lifecycle_event


import importlib.util

_spec = importlib.util.spec_from_file_location('backtest_order', Path(__file__).resolve().parents[1] / 'backtest_order.py')
_module = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_module)
CandidateOrder = _module.CandidateOrder
Candle = _module.Candle
simulate_candidate = _module.simulate_candidate


def test_no_duplicate_alphaforge_package_shadowing() -> None:
    repo = Path(__file__).resolve().parents[1]
    dupes = [p for p in repo.rglob('alphaforge/__init__.py') if 'src/alphaforge' not in str(p)]
    assert not dupes


def test_runtime_imports_from_src_package() -> None:
    spec = importlib.util.find_spec('alphaforge.runtime')
    assert spec is not None and spec.origin is not None
    assert '/src/alphaforge/runtime.py' in spec.origin


def test_save_order_decision_persists_reject() -> None:
    engine = init_db()
    with Session(engine) as s:
        save_order_decision(s, signal_id='sig-1', symbol='BTCUSDT', mode='BACKTEST', decision='REJECTED', reject_reason='LOW_EFFECTIVE_RR', score=0.41, rr=1.0, effective_rr=0.85)
        row = s.execute(text("SELECT decision,reject_reason,effective_rr FROM order_decisions WHERE signal_id='sig-1'" )).one()
        assert row.decision == 'REJECTED'
        assert row.reject_reason == 'LOW_EFFECTIVE_RR'
        assert float(row.effective_rr) == 0.85


def test_save_trade_lifecycle_event_persists_state() -> None:
    engine = init_db()
    with Session(engine) as s:
        ok = save_trade_lifecycle_event(s, signal_id='sig-2', symbol='BTCUSDT', mode='PAPER', state='ORDER_REJECTED', reject_reason='SPREAD_TOO_WIDE', event_ts='2026-05-14T00:00:00Z')
        assert ok
        row = s.execute(text("SELECT state,reject_reason FROM trade_lifecycle_events WHERE signal_id='sig-2'" )).one()
        assert row.state == 'ORDER_REJECTED'
        assert row.reject_reason == 'SPREAD_TOO_WIDE'


def test_rejected_signal_lifecycle_precedes_trade_creation() -> None:
    engine = init_db()
    with Session(engine) as s:
        save_trade_lifecycle_event(s, signal_id='sig-3', symbol='BTCUSDT', mode='BACKTEST', state='SIGNAL_REJECTED', reject_reason='NEGATIVE_EXPECTANCY', event_ts='1')
        count = s.execute(text("SELECT COUNT(1) AS c FROM trade_lifecycle_events WHERE signal_id='sig-3'" )).one().c
        assert count == 1


def test_order_rejected_lifecycle_contains_reason() -> None:
    engine = init_db()
    with Session(engine) as s:
        save_trade_lifecycle_event(s, signal_id='sig-4', symbol='BTCUSDT', mode='BACKTEST', state='ORDER_REJECTED', reject_reason='LOW_LIQUIDITY', event_ts='2')
        reason = s.execute(text("SELECT reject_reason FROM trade_lifecycle_events WHERE signal_id='sig-4'" )).scalar_one()
        assert reason == 'LOW_LIQUIDITY'


def test_backtest_lifecycle_does_not_start_directly_at_created() -> None:
    candidate = CandidateOrder(1, 'BTCUSDT', 'LONG', 100.0, 99.0, 101.0, 1.0, 'BREAKOUT', 'X', 'TREND', 8.0, 'LIMIT')
    candles = [Candle(1, 100, 100.5, 99.5, 100, 10), Candle(2, 100, 100.2, 99.8, 100, 10), Candle(3, 100, 101.2, 99.9, 101, 10)]
    rows = simulate_candidate(candidate, candles, 1, 1000.0, 1.0, market_ctx={})
    assert rows[0].status_before == 'SIGNAL_CREATED'
    assert rows[0].status_after == 'WAITING_ENTRY_ZONE'


def test_unavailable_backtest_context_uses_sentinel_not_zero() -> None:
    candidate = CandidateOrder(1, 'BTCUSDT', 'LONG', 100.0, 99.0, 101.0, 1.0, 'BREAKOUT', 'X', 'TREND', 8.0, 'MARKET')
    candles = [Candle(1, 100, 100.5, 99.5, 100, 10), Candle(2, 100, 101.2, 99.8, 101, 10)]
    rows = simulate_candidate(candidate, candles, 1, 1000.0, 1.0, market_ctx={})
    assert rows[0].volume_24h_usdt == 'UNAVAILABLE_BACKTEST'
    assert rows[0].spread_pct == 'UNAVAILABLE_BACKTEST'


def test_backtest_paper_decision_contract_fields_match() -> None:
    fields = {'signal_id','symbol','mode','decision','reject_reason','score','rr','effective_rr','expectancy_bucket'}
    engine = init_db()
    with Session(engine) as s:
        save_order_decision(s, signal_id='sig-b', symbol='BTCUSDT', mode='BACKTEST', decision='ACCEPTED')
        save_order_decision(s, signal_id='sig-p', symbol='BTCUSDT', mode='PAPER', decision='ACCEPTED')
        cols = {r[1] for r in s.execute(text("PRAGMA table_info(order_decisions)" )).fetchall()}
    assert fields.issubset(cols)
