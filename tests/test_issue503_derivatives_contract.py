from __future__ import annotations

from alphaforge.binance_reconciliation_provider import (
    BinanceReadonlyReconciliationConfig,
    BinanceReadonlyReconciliationProvider,
)
from alphaforge.derivatives_contract import (
    HOLD_AWARE_FUNDING_COMPLETE,
    evaluate_notional_only_derivatives_contract,
    evaluate_reported_liquidation_boundary,
    normalize_derivatives_position_fields,
)


def _position(
    *,
    symbol: str = "BTCUSDT",
    leverage: object = 1,
    margin_type: str = "cross",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "leverage": leverage,
        "margin_type": margin_type.upper(),
    }


def test_position_fields_preserve_measured_derivatives_evidence() -> None:
    normalized = normalize_derivatives_position_fields(
        {
            "leverage": "1",
            "marginType": "cross",
            "liquidationPrice": "0",
        }
    )
    assert normalized == {
        "leverage": 1.0,
        "leverage_status": "MEASURED",
        "margin_type": "CROSS",
        "margin_type_status": "MEASURED",
        "liquidation_price": 0.0,
        "liquidation_price_status": "MEASURED",
    }


def test_notional_only_1x_contract_can_pass_only_with_hold_aware_funding() -> None:
    report = evaluate_notional_only_derivatives_contract(
        positions=[_position()],
        required_symbols=["BTCUSDT"],
        provider_evidence_status="COMPLETE",
        funding_accrual_status=HOLD_AWARE_FUNDING_COMPLETE,
    )
    assert report["account_model"] == "NOTIONAL_ONLY_1X"
    assert report["account_model_status"] == "PASS"
    assert report["leveraged_live_supported"] is False
    assert report["funding_live_equivalent"] is True
    assert report["live_eligible"] is True
    assert report["blocking_reasons"] == ()


def test_entry_rate_only_funding_blocks_live_even_at_1x() -> None:
    report = evaluate_notional_only_derivatives_contract(
        positions=[_position()],
        required_symbols=["BTCUSDT"],
        provider_evidence_status="COMPLETE",
    )
    assert report["account_model_status"] == "PASS"
    assert report["funding_live_equivalent"] is False
    assert report["live_eligible"] is False
    assert "HOLD_AWARE_FUNDING_UNAVAILABLE" in report["blocking_reasons"]


def test_non_1x_or_missing_margin_evidence_fails_closed() -> None:
    leveraged = evaluate_notional_only_derivatives_contract(
        positions=[_position(leverage=5)],
        required_symbols=["BTCUSDT"],
        provider_evidence_status="COMPLETE",
        funding_accrual_status=HOLD_AWARE_FUNDING_COMPLETE,
    )
    assert leveraged["live_eligible"] is False
    assert "LEVERAGE_NOT_1X:BTCUSDT" in leveraged["blocking_reasons"]

    missing_margin = evaluate_notional_only_derivatives_contract(
        positions=[_position(margin_type="")],
        required_symbols=["BTCUSDT"],
        provider_evidence_status="COMPLETE",
        funding_accrual_status=HOLD_AWARE_FUNDING_COMPLETE,
    )
    assert missing_margin["live_eligible"] is False
    assert "MARGIN_MODE_UNAVAILABLE:BTCUSDT" in missing_margin["blocking_reasons"]


def test_missing_position_risk_row_is_not_treated_as_zero_risk() -> None:
    report = evaluate_notional_only_derivatives_contract(
        positions=[],
        required_symbols=["BTCUSDT"],
        provider_evidence_status="COMPLETE",
        funding_accrual_status=HOLD_AWARE_FUNDING_COMPLETE,
    )
    assert report["live_eligible"] is False
    assert "POSITION_RISK_ROW_MISSING:BTCUSDT" in report["blocking_reasons"]


def test_reported_liquidation_boundary_long_and_short() -> None:
    long_safe = evaluate_reported_liquidation_boundary(
        side="LONG", stop_price=95, liquidation_price=90
    )
    long_blocked = evaluate_reported_liquidation_boundary(
        side="LONG", stop_price=85, liquidation_price=90
    )
    short_safe = evaluate_reported_liquidation_boundary(
        side="SHORT", stop_price=105, liquidation_price=110
    )
    short_blocked = evaluate_reported_liquidation_boundary(
        side="SHORT", stop_price=115, liquidation_price=110
    )
    assert long_safe["safe"] is True
    assert short_safe["safe"] is True
    assert long_blocked["reason"] == "LIQUIDATION_BOUNDARY_PRECEDES_OR_EQUALS_STOP"
    assert short_blocked["reason"] == "LIQUIDATION_BOUNDARY_PRECEDES_OR_EQUALS_STOP"


def test_missing_liquidation_evidence_never_synthesizes_a_safe_boundary() -> None:
    result = evaluate_reported_liquidation_boundary(
        side="LONG", stop_price=95, liquidation_price=None
    )
    assert result == {
        "status": "INCOMPLETE",
        "safe": False,
        "reason": "LIQUIDATION_PRICE_UNAVAILABLE",
    }


def test_binance_position_risk_exposes_contract_without_claiming_live_equivalence() -> None:
    def http(url, headers, timeout):
        if "positionRisk" in url:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0",
                    "entryPrice": "0",
                    "positionSide": "BOTH",
                    "unRealizedProfit": "0",
                    "leverage": "1",
                    "marginType": "cross",
                    "liquidationPrice": "0",
                }
            ]
        if "openOrders" in url:
            return []
        if "userTrades" in url:
            return []
        return {"serverTime": 1700000000001}

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(
            base_url="https://demo-fapi.binance.com",
            api_key="k",
            api_secret="s",
        ),
        tracked_symbols=lambda: {"BTCUSDT"},
        now_ms=lambda: 1700000000000,
        http_get_json=http,
    )
    snapshot = provider.snapshot()
    assert snapshot["evidence_status"] == "COMPLETE"
    assert snapshot["positions"][0]["leverage"] == 1.0
    assert snapshot["positions"][0]["margin_type"] == "CROSS"
    assert snapshot["derivatives_contract"]["account_model_status"] == "PASS"
    assert snapshot["derivatives_contract"]["leveraged_live_supported"] is False
    assert snapshot["derivatives_contract"]["funding_live_equivalent"] is False
    assert snapshot["derivatives_contract"]["live_eligible"] is False
    assert "HOLD_AWARE_FUNDING_UNAVAILABLE" in snapshot["derivatives_contract"]["blocking_reasons"]
