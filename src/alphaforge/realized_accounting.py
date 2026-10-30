"""Cash identities shared by persisted PAPER closure and legacy BACKTEST.

Costs are cash amounts, after conversion from any model units. Funding is signed;
other costs cannot be negative. Filled prices already include price slippage.
"""
import math
from typing import Any, Mapping


def finite_cash(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def position_cash_geometry(*, fill: float, stop: float, quantity: float,
                           exit_price: float, side: str, remaining_quantity: float | None = None,
                           previous_gross: float = 0.0) -> dict[str, float | None]:
    remaining = quantity if remaining_quantity is None else remaining_quantity
    risk = abs(fill - stop) * quantity
    final_gross = (exit_price - fill) * remaining * (-1 if side == "SHORT" else 1)
    gross = previous_gross + final_gross
    return dict(initial_risk_usd=risk, gross_pnl_usd=gross,
                gross_r=gross / risk if risk > 0 else None, final_slice_gross=final_gross)


def realized_cash_ledger(*, gross_pnl: float, initial_risk: float,
                         costs: Mapping[str, Any], missing=()) -> dict[str, Any]:
    absent = list(missing)
    parsed = {}
    for key, value in costs.items():
        cash = finite_cash(value)
        if cash is None or (cash < 0 and key != "funding_cost"):
            absent.append(key)
        else:
            parsed[key] = cash
    if finite_cash(gross_pnl) is None:
        absent.append("gross_pnl")
    if finite_cash(initial_risk) is None or initial_risk <= 0:
        absent.append("risk_usd")
    total = None if absent else sum(parsed.values())
    net = None if total is None else gross_pnl - total
    return dict(total_cost_usd=total, net_pnl_usd=net,
                net_r=None if net is None else net / initial_risk,
                missing_fields=sorted(set(absent)))


def sum_cash(values) -> float | None:
    """An incomplete cash series cannot become a favorable zero/partial sum."""
    parsed = [finite_cash(value) for value in values]
    return None if any(value is None for value in parsed) else sum(parsed)


def mean_cash(total, count) -> float | None:
    return None if total is None else total / count if count else 0.0
