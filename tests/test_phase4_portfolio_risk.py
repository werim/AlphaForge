from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from alphaforge.persistence import init_db, save_rejected_decision_artifact
from alphaforge.portfolio_risk import evaluate_portfolio_risk, snapshot_from_state, correlation_group_for_symbol
from alphaforge.live_readiness import LiveReadinessEvaluator


def _snapshot(**overrides):
    base = dict(mode="BACKTEST", symbol="BTCUSDT", side="LONG", candidate_notional=1000.0, equity=10000.0, available_balance=9000.0, open_positions={}, config={"max_open_positions": 3, "max_concurrent_positions": 3, "max_notional_exposure": 5000.0, "max_symbol_notional": 2000.0, "max_daily_loss_pct": 0.05, "max_rolling_drawdown_pct": 0.1, "max_correlation_group_exposure": 3000.0, "max_correlated_positions": 2, "reject_unknown_portfolio_risk": True})
    base.update(overrides)
    return snapshot_from_state(**base)


def test_portfolio_risk_rejects_limits_and_unknown_state():
    cand = {"symbol": "BTCUSDT", "entry": 100.0, "quantity": 10.0}
    cases = [
        (_snapshot(open_positions={"ETHUSDT": {"notional": 1000}, "SOLUSDT": {"notional": 1000}, "XRPUSDT": {"notional": 1000}}), "MAX_OPEN_POSITIONS"),
        (_snapshot(open_positions={"ETHUSDT": {"notional": 4500}}), "MAX_NOTIONAL_EXPOSURE"),
        (_snapshot(open_positions={"BTCUSDT": {"notional": 1500}}), "MAX_SYMBOL_NOTIONAL_EXPOSURE"),
        (_snapshot(daily_realized_pnl=-600.0), "MAX_DAILY_LOSS"),
        (_snapshot(rolling_drawdown_pct=0.12), "MAX_ROLLING_DRAWDOWN"),
        (_snapshot(now=1000.0, cooldown_until={"BTCUSDT": 1010.0}), "SYMBOL_COOLDOWN_ACTIVE"),
        (_snapshot(open_positions={"BTCUSD": {"notional": 2500}}), "CORRELATION_OVEREXPOSURE"),
        (_snapshot(equity=None), "UNKNOWN_PORTFOLIO_RISK"),
    ]
    for snap, reason in cases:
        decision = evaluate_portfolio_risk(cand, snap, {"reject_unknown_portfolio_risk": True})
        assert not decision.accepted
        assert decision.reject_reason == reason


def test_portfolio_risk_accepts_clean_and_groups_conservatively():
    snap = _snapshot(open_positions={"ETHUSDT": {"notional": 500, "side": "LONG"}})
    decision = evaluate_portfolio_risk({"symbol": "BTCUSDT", "entry": 100, "quantity": 5}, snap, {"reject_unknown_portfolio_risk": True})
    assert decision.accepted
    assert correlation_group_for_symbol("PEPEUSDT") == "CRYPTO_MEME_LOW_LIQUIDITY"
    assert correlation_group_for_symbol("NEWCOINUSDT") == "UNKNOWN_CONSERVATIVE"


def test_portfolio_reject_persists_to_order_decisions(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'p.db'}")
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        save_rejected_decision_artifact(s, signal_id="s1", symbol="BTCUSDT", mode="BACKTEST", reject_reason="MAX_NOTIONAL_EXPOSURE", portfolio_reject_reason="MAX_NOTIONAL_EXPOSURE", portfolio_risk_state="MAX_NOTIONAL_EXPOSURE", portfolio_diagnostics={"snapshot": {"equity": 1000}}, risk_flags=["MAX_NOTIONAL_EXPOSURE"], rr=2.0, effective_rr=1.8, score=8.0)
        s.commit()
    with engine.connect() as c:
        row = c.execute(text("SELECT reject_reason, portfolio_reject_reason, portfolio_risk_state, risk_flags FROM order_decisions WHERE signal_id='s1'")).mappings().one()
    assert row["reject_reason"] == "MAX_NOTIONAL_EXPOSURE"
    assert row["portfolio_reject_reason"] == "MAX_NOTIONAL_EXPOSURE"
    assert "MAX_NOTIONAL_EXPOSURE" in row["risk_flags"]


def test_readiness_fails_when_portfolio_evidence_missing(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'r.db'}")
    checks = LiveReadinessEvaluator(engine)._check_persistence(engine.connect())
    names = {c.name: c for c in checks}
    assert names["portfolio_risk_snapshot_present"].passed is False
    assert names["correlation_risk_evidence_present"].passed is False

from alphaforge.portfolio_risk import BacktestPortfolioState


def test_unknown_equity_rejects_by_default_and_runtime_no_synthetic_equity():
    snap = snapshot_from_state(mode="PAPER", symbol="BTCUSDT", side="LONG", equity=None, available_balance=None, open_positions={}, config={"max_notional_exposure": 10000.0}, candidate_notional=1000.0)
    decision = evaluate_portfolio_risk({"symbol": "BTCUSDT", "entry": 100, "quantity": 10}, snap, {"reject_unknown_portfolio_risk": True})
    assert not decision.accepted
    assert decision.reject_reason == "UNKNOWN_PORTFOLIO_RISK"
    assert decision.diagnostics["snapshot"]["equity"] is None


def test_backtest_portfolio_accounting_open_close_loss_win_and_open_at_end():
    state = BacktestPortfolioState(initial_equity=10_000.0)
    cfg = {"max_open_positions": 3, "max_notional_exposure": 10_000.0, "max_symbol_notional": 5_000.0, "max_correlation_group_exposure": 8_000.0}
    snap0 = state.snapshot(mode="BACKTEST", symbol="BTCUSDT", side="LONG", config=cfg, timestamp=1_700_000_000_000, candidate_notional=1000.0)
    assert snap0.total_notional_exposure == 0.0
    state.mark_pending("p1", 1000.0)
    assert state.snapshot(mode="BACKTEST", symbol="BTCUSDT", config=cfg, timestamp=1_700_000_000_000).total_notional_exposure == 0.0
    state.open_position(position_id="p1", symbol="BTCUSDT", side="LONG", notional=1000.0, entry_price=100.0, timestamp=1_700_000_000_000)
    snap_open = state.snapshot(mode="BACKTEST", symbol="BTCUSDT", side="LONG", config=cfg, timestamp=1_700_000_000_000)
    assert snap_open.open_position_count == 1
    assert snap_open.total_notional_exposure == 1000.0
    assert snap_open.correlation_group_exposure == 1000.0
    state.close_position(position_id="p1", symbol="BTCUSDT", timestamp=1_700_000_060_000, net_pnl_usdt=-100.0, close_reason="SL_HIT")
    snap_loss = state.snapshot(mode="BACKTEST", symbol="BTCUSDT", side="LONG", config=cfg, timestamp=1_700_000_060_000)
    assert snap_loss.open_position_count == 0
    assert snap_loss.equity == 9900.0
    assert snap_loss.daily_realized_pnl == -100.0
    assert snap_loss.consecutive_loss_count == 1
    state.open_position(position_id="p2", symbol="ETHUSDT", side="LONG", notional=500.0, entry_price=100.0, timestamp=1_700_000_120_000)
    state.close_position(position_id="p2", symbol="ETHUSDT", timestamp=1_700_000_180_000, net_pnl_usdt=150.0, close_reason="TP_HIT")
    assert state.snapshot(mode="BACKTEST", symbol="ETHUSDT", config=cfg, timestamp=1_700_000_180_000).consecutive_loss_count == 0
    state.open_position(position_id="p3", symbol="SOLUSDT", side="LONG", notional=700.0, entry_price=50.0, timestamp=1_700_000_240_000)
    assert state.snapshot(mode="BACKTEST", symbol="SOLUSDT", config=cfg, timestamp=1_700_000_300_000).open_position_count == 1


def test_daily_trade_same_side_and_net_exposure_rejects():
    base = _snapshot(open_positions={"ETHUSDT": {"notional": 500, "side": "LONG"}}, trades_today_symbol=2, trades_today_global=5)
    cand = {"symbol": "BTCUSDT", "side": "LONG", "entry": 100, "quantity": 1}
    assert evaluate_portfolio_risk(cand, base, {"max_daily_symbol_trades": 2}).reject_reason == "DAILY_SYMBOL_TRADE_LIMIT"
    assert evaluate_portfolio_risk(cand, base, {"max_daily_global_trades": 5}).reject_reason == "DAILY_GLOBAL_TRADE_LIMIT"
    assert evaluate_portfolio_risk(cand, base, {"max_same_side_exposure": 550}).reject_reason == "SAME_SIDE_OVEREXPOSURE"
    assert evaluate_portfolio_risk(cand, base, {"max_net_exposure": 550}).reject_reason == "NET_EXPOSURE_TOO_HIGH"
