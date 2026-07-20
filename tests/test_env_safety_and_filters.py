from __future__ import annotations

import pytest

from alphaforge.config import load_config_from_env
from alphaforge.config_audit import audit_config
from alphaforge.order import (
    OrderCandidate,
    OrderExecutionContext,
    TradingMode,
    evaluate_trade_quality,
    execute_order_candidate,
)


def _candidate(*, setup_type: str = "BREAKOUT_UP", regime: str = "TREND") -> OrderCandidate:
    return OrderCandidate(
        symbol="BTCUSDT", side="LONG", setup_type=setup_type, setup_reason="contract-test",
        regime=regime, score=0.9, rr=2.0, expectancy=0.2, entry=100.0, sl=99.0,
        tp=102.0, order_type="LIMIT",
    )


def _live_context(*, allow: bool, **authorization: bool) -> tuple[OrderExecutionContext, dict[str, int]]:
    calls = {"orders": 0}
    defaults = {
        "live_trading_enabled": True,
        "operator_acknowledged": True,
        "qualification_passed": True,
        "reconciliation_passed": True,
        "kill_switch_active": False,
    }
    defaults.update(authorization)
    ctx = OrderExecutionContext(
        mode=TradingMode.LIVE, timestamp=1, symbol="BTCUSDT", balance=1000.0,
        risk_pct=1.0, allow_live_orders=allow,
        storage={
            "live_authorization": defaults,
            "live_authorization_provider": lambda: dict(defaults),
            "binance_place_order": lambda candidate: calls.__setitem__("orders", calls["orders"] + 1) or {"id": "test"},
        },
    )
    return ctx, calls


def test_live_order_authorization_is_additive_and_fail_closed(monkeypatch):
    candidate = _candidate()
    monkeypatch.setenv("ALPHAFORGE_ALLOW_LIVE_ORDERS", "false")
    disabled, disabled_calls = _live_context(allow=False)
    with pytest.raises(RuntimeError, match="allow_live_orders"):
        execute_order_candidate(candidate, disabled)
    assert disabled_calls["orders"] == 0

    monkeypatch.setenv("ALPHAFORGE_ALLOW_LIVE_ORDERS", "true")
    flag_alone, alone_calls = _live_context(allow=True, operator_acknowledged=False, qualification_passed=False)
    with pytest.raises(RuntimeError, match="operator_acknowledged"):
        execute_order_candidate(candidate, flag_alone)
    assert alone_calls["orders"] == 0

    killed, killed_calls = _live_context(allow=True, kill_switch_active=True)
    with pytest.raises(RuntimeError, match="kill_switch_inactive"):
        execute_order_candidate(candidate, killed)
    assert killed_calls["orders"] == 0

    authorized, authorized_calls = _live_context(allow=True)
    assert execute_order_candidate(candidate, authorized)["type"] == "live"
    assert authorized_calls["orders"] == 1

    for mode in (TradingMode.BACKTEST, TradingMode.PAPER):
        ctx = OrderExecutionContext(mode=mode, timestamp=1, symbol="BTCUSDT", balance=1000.0, risk_pct=1.0)
        assert execute_order_candidate(candidate, ctx)["type"] in {"virtual", "paper"}


def test_allow_live_orders_is_loaded_but_does_not_bypass_other_gates(monkeypatch):
    monkeypatch.setenv("ALPHAFORGE_ALLOW_LIVE_ORDERS", "true")
    cfg = load_config_from_env()
    assert cfg.runtime.allow_live_orders is True
    assert cfg.runtime.live_enabled is False
    assert cfg.runtime.operator_live_acknowledged is False


def test_regime_alias_changes_actual_decision(monkeypatch):
    candidate = _candidate(setup_type="RANGE_MEAN_REVERSION", regime="TREND")
    monkeypatch.setenv("ENABLE_REGIME_FILTER", "false")
    assert evaluate_trade_quality(candidate, {}, {}, {"MODE": "PAPER"}).accepted
    monkeypatch.setenv("ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT", "true")
    assert evaluate_trade_quality(candidate, {}, {}, {"MODE": "PAPER"}).reject_reason == "REGIME_MISMATCH"

    conflict = audit_config(env={"ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT": "true", "ENABLE_REGIME_FILTER": "false"})
    assert conflict["status"] == "FAIL"
    equal = audit_config(env={"ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT": "true", "ENABLE_REGIME_FILTER": "true"})
    assert equal["status"] != "FAIL"


def test_orderbook_filter_changes_decision_without_disabling_other_gates(monkeypatch):
    candidate = _candidate()
    risky = {"orderbook_imbalance": 0.97, "orderbook_status": "MEASURED", "spoof_risk": 0.9, "volatility_regime": "normal"}
    disabled = evaluate_trade_quality(candidate, risky, {}, {"MODE": "PAPER", "ENABLE_ORDERBOOK_FILTER": False})
    enabled = evaluate_trade_quality(candidate, risky, {}, {"MODE": "PAPER", "ENABLE_ORDERBOOK_FILTER": True})
    assert disabled.accepted
    assert enabled.reject_reason == "ORDERBOOK_RISK"

    missing = evaluate_trade_quality(candidate, {"volatility_regime": "normal"}, {}, {"MODE": "BACKTEST", "ENABLE_ORDERBOOK_FILTER": True, "REJECT_UNKNOWN_EXECUTION_CONTEXT": True})
    assert missing.reject_reason == "ORDERBOOK_CONTEXT_MISSING"
    wide = evaluate_trade_quality(candidate, {**risky, "spread_pct": 1.0}, {}, {"MODE": "PAPER", "ENABLE_ORDERBOOK_FILTER": False})
    assert wide.reject_reason == "SPREAD_TOO_HIGH"

    monkeypatch.setenv("ENABLE_ORDERBOOK_FILTER", "true")
    assert load_config_from_env().runtime.enable_orderbook_filter is True
    conflict = audit_config(env={"ALPHAFORGE_ENABLE_ORDERBOOK_FILTER": "false", "ENABLE_ORDERBOOK_FILTER": "true"})
    assert conflict["status"] == "FAIL"


def test_mode_specific_settings_do_not_leak(monkeypatch):
    baseline = load_config_from_env().runtime
    monkeypatch.setenv("ALPHAFORGE_BACKTEST_INITIAL_BALANCE", "987654")
    changed = load_config_from_env().runtime
    assert changed.max_notional_exposure == baseline.max_notional_exposure
    assert changed.max_daily_loss_pct == baseline.max_daily_loss_pct

    candidate = _candidate()
    paper = evaluate_trade_quality(candidate, {"volatility_regime": "normal"}, {}, {"MODE": "PAPER"})
    monkeypatch.setenv("ALPHAFORGE_ALLOW_LIVE_ORDERS", "true")
    still_paper = evaluate_trade_quality(candidate, {"volatility_regime": "normal"}, {}, {"MODE": "PAPER"})
    assert still_paper.accepted == paper.accepted
