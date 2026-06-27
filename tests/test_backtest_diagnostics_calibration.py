from __future__ import annotations

import json

import importlib.util
import sys
from pathlib import Path


def _build_calibration_outputs(*args, **kwargs):
    path = Path(__file__).resolve().parents[1] / "src" / "alphaforge" / "dashboard" / "backtest_control.py"
    spec = importlib.util.spec_from_file_location("bt_control_for_tests", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module._build_calibration_outputs(*args, **kwargs)


def test_score_saturation_and_daily_global_limit_diagnostics_are_exported() -> None:
    lifecycle_rows = [
        {"signal_id": "a1", "symbol": "ETHUSDT", "timestamp": "2026-06-01T09:00:00Z", "lifecycle_state": "WAITING_ENTRY_ZONE", "decision": "ACCEPTED", "score": "10", "execution_ctx": json.dumps({"close_reason": "TP_HIT", "net_pnl": "2.0"})},
        {"signal_id": "a1", "symbol": "ETHUSDT", "timestamp": "2026-06-01T09:00:00Z", "lifecycle_state": "POSITION_CLOSED", "decision": "ACCEPTED", "score": "10", "close_reason": "TP_HIT"},
    ]
    rejected_rows = [
        {"signal_id": "r1", "symbol": "ETHUSDT", "timestamp": "2026-06-01T10:00:00Z", "side": "LONG", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "DAILY_GLOBAL_TRADE_LIMIT", "score": "10", "effective_rr": "2.1"},
        {"signal_id": "r2", "symbol": "BTCUSDT", "timestamp": "2026-06-01T11:00:00Z", "side": "SHORT", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "LOW_SCORE", "score": "10", "effective_rr": "1.4"},
    ]
    shadow_rows = [{**rejected_rows[0], "shadow_outcome": "WOULD_SL"}, {**rejected_rows[1], "shadow_outcome": "WOULD_TP"}]

    _, summary = _build_calibration_outputs(lifecycle_rows, rejected_rows, {"accepted_count": "1"}, shadow_rows)

    score = summary["score_saturation_diagnostics"]
    assert score["mode"] == "DIAGNOSTIC_ONLY"
    assert score["acceptance_logic_changed"] is False
    assert score["score_10"]["would_tp_count"] == 1
    assert score["score_10"]["would_sl_count"] == 1
    assert any(row["score_bucket"] == "10" for row in score["rejected_score_bucket_shadow_split"])
    daily = summary["daily_global_trade_limit_diagnostics"]
    assert daily[0]["symbol"] == "ETHUSDT"
    assert daily[0]["shadow_outcome"] == "WOULD_SL"
    assert daily[0]["net_outcome_if_accepted"] == "WORSENED"
    assert daily[0]["same_day_accepted_trade_count"] == 1
    assert summary["dynamic_trade_limit_proposal"]["enabled"] is False
    assert summary["dynamic_trade_limit_proposal"]["default_behavior_changed"] is False


def test_closed_accepted_diagnostics_require_lifecycle_connected_exported_pnl() -> None:
    lifecycle_rows = [
        {"signal_id": "a1", "symbol": "BTCUSDT", "timestamp": "1000", "lifecycle_state": "WAITING_ENTRY_ZONE", "decision": "ACCEPTED", "score": "8", "execution_ctx": json.dumps({"side": "LONG", "entry": "100", "sl": "98", "tp": "104"})},
        {"signal_id": "a1", "symbol": "BTCUSDT", "timestamp": "1000", "lifecycle_state": "POSITION_CLOSED", "decision": "ACCEPTED", "execution_ctx": json.dumps({"close_reason": "TP_HIT", "exit_price": "104", "net_pnl": "3.5"})},
    ]
    _, summary = _build_calibration_outputs(lifecycle_rows, [], {"accepted_count": "1"}, [])
    diag = summary["accepted_trade_diagnostics"][0]

    assert diag["side"] == "LONG"
    assert diag["entry"] == "100"
    assert diag["sl"] == "98"
    assert diag["tp"] == "104"
    assert diag["close_reason"] == "TP_HIT"
    assert diag["exit_price"] == "104"
    assert diag["net_pnl_status"] == "EXPORTED"
