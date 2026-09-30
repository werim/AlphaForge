"""Fail-closed derivatives account contract for AlphaForge.

AlphaForge currently supports a notional-only 1x futures contract. It does not
claim an authoritative leveraged liquidation model or hold-aware funding model.
Those missing semantics block LIVE derivatives authorization without changing
PAPER research behavior.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

ACCOUNT_MODEL = "NOTIONAL_ONLY_1X"
HOLD_AWARE_FUNDING_COMPLETE = "COMPLETE_MEASURED_HOLD_AWARE"
DEFAULT_FUNDING_STATUS = "UNAVAILABLE_BLOCKING_ENTRY_RATE_ONLY"


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, "", "UNKNOWN", "UNAVAILABLE"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def normalize_derivatives_position_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """Extract derivative-account fields without making generic reconciliation fail.

    Missing/invalid values remain explicit and are evaluated by the separate
    derivatives contract. This preserves existing read-only reconciliation while
    preventing missing leverage evidence from becoming a favorable default.
    """
    leverage = _finite_number(row.get("leverage"))
    if leverage is not None and leverage <= 0.0:
        leverage = None
    margin_raw = str(row.get("marginType") or row.get("margin_type") or "").strip().upper()
    margin_type = margin_raw if margin_raw in {"CROSS", "ISOLATED"} else None
    liquidation = _finite_number(
        row.get("liquidationPrice", row.get("liquidation_price"))
    )
    if liquidation is not None and liquidation < 0.0:
        liquidation = None
    return {
        "leverage": leverage,
        "leverage_status": "MEASURED" if leverage is not None else "UNAVAILABLE",
        "margin_type": margin_type,
        "margin_type_status": "MEASURED" if margin_type is not None else "UNAVAILABLE",
        "liquidation_price": liquidation,
        "liquidation_price_status": (
            "MEASURED" if liquidation is not None else "UNAVAILABLE"
        ),
    }


def evaluate_reported_liquidation_boundary(
    *,
    side: str,
    stop_price: Any,
    liquidation_price: Any,
) -> dict[str, Any]:
    """Compare a measured exchange liquidation boundary with an intended stop.

    This does not calculate liquidation price. It only validates exchange-reported
    evidence, so missing evidence remains blocking rather than being synthesized.
    """
    normalized_side = str(side or "").strip().upper()
    stop = _finite_number(stop_price)
    liquidation = _finite_number(liquidation_price)
    if normalized_side not in {"LONG", "SHORT"}:
        return {"status": "INCOMPLETE", "safe": False, "reason": "SIDE_UNAVAILABLE"}
    if stop is None or stop <= 0.0:
        return {"status": "INCOMPLETE", "safe": False, "reason": "STOP_UNAVAILABLE"}
    if liquidation is None or liquidation <= 0.0:
        return {
            "status": "INCOMPLETE",
            "safe": False,
            "reason": "LIQUIDATION_PRICE_UNAVAILABLE",
        }
    safe = stop > liquidation if normalized_side == "LONG" else stop < liquidation
    return {
        "status": "PASS" if safe else "BLOCKED",
        "safe": safe,
        "reason": (
            "STOP_PRECEDES_LIQUIDATION"
            if safe
            else "LIQUIDATION_BOUNDARY_PRECEDES_OR_EQUALS_STOP"
        ),
        "side": normalized_side,
        "stop_price": stop,
        "liquidation_price": liquidation,
    }


def evaluate_notional_only_derivatives_contract(
    *,
    positions: Sequence[Mapping[str, Any]],
    required_symbols: Iterable[str],
    provider_evidence_status: str,
    funding_accrual_status: str = DEFAULT_FUNDING_STATUS,
) -> dict[str, Any]:
    required = tuple(sorted({str(symbol).strip().upper() for symbol in required_symbols if str(symbol).strip()}))
    normalized_provider_status = str(provider_evidence_status or "INCOMPLETE").upper()
    funding_status = str(funding_accrual_status or DEFAULT_FUNDING_STATUS).upper()
    blockers: list[str] = []

    if normalized_provider_status != "COMPLETE":
        blockers.append("DERIVATIVES_PROVIDER_EVIDENCE_INCOMPLETE")
    if not required:
        blockers.append("DERIVATIVES_SYMBOL_SCOPE_EMPTY")

    by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for row in positions:
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            by_symbol.setdefault(symbol, []).append(row)

    leverage_rows = 0
    margin_rows = 0
    for symbol in required:
        rows = by_symbol.get(symbol, [])
        if not rows:
            blockers.append(f"POSITION_RISK_ROW_MISSING:{symbol}")
            continue
        for row in rows:
            leverage = _finite_number(row.get("leverage"))
            if leverage is None:
                blockers.append(f"LEVERAGE_UNAVAILABLE:{symbol}")
            elif not math.isclose(leverage, 1.0, rel_tol=0.0, abs_tol=1e-12):
                blockers.append(f"LEVERAGE_NOT_1X:{symbol}")
            else:
                leverage_rows += 1
            margin_type = str(row.get("margin_type") or "").upper()
            if margin_type not in {"CROSS", "ISOLATED"}:
                blockers.append(f"MARGIN_MODE_UNAVAILABLE:{symbol}")
            else:
                margin_rows += 1

    leveraged_live_supported = False
    account_model_passed = bool(required) and not any(
        reason.startswith(
            (
                "DERIVATIVES_PROVIDER_EVIDENCE_INCOMPLETE",
                "DERIVATIVES_SYMBOL_SCOPE_EMPTY",
                "POSITION_RISK_ROW_MISSING:",
                "LEVERAGE_UNAVAILABLE:",
                "LEVERAGE_NOT_1X:",
                "MARGIN_MODE_UNAVAILABLE:",
            )
        )
        for reason in blockers
    )
    hold_aware_funding = funding_status == HOLD_AWARE_FUNDING_COMPLETE
    if not hold_aware_funding:
        blockers.append("HOLD_AWARE_FUNDING_UNAVAILABLE")

    notional_only_live_eligible = account_model_passed and hold_aware_funding
    return {
        "schema_version": "derivatives_contract_v1",
        "account_model": ACCOUNT_MODEL,
        "provider_evidence_status": normalized_provider_status,
        "required_symbols": required,
        "account_model_status": "PASS" if account_model_passed else "BLOCKED",
        "measured_1x_rows": leverage_rows,
        "measured_margin_rows": margin_rows,
        "leveraged_live_supported": leveraged_live_supported,
        "liquidation_model_status": "UNSUPPORTED_LEVERAGED_LIVE",
        "funding_accrual_status": funding_status,
        "funding_live_equivalent": hold_aware_funding,
        "notional_only_live_eligible": notional_only_live_eligible,
        "live_eligible": notional_only_live_eligible,
        "paper_live_equivalent": False,
        "blocking_reasons": tuple(sorted(set(blockers))),
    }
