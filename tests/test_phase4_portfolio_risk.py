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
