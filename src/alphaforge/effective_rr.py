from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from alphaforge.execution import build_execution_cost_model


@dataclass(frozen=True)
class EffectiveRRResult:
    raw_rr: float
    effective_rr: float
    cost_penalty_total: float
    spread_penalty: float
    slippage_penalty: float
    latency_penalty: float
    funding_penalty: float
    liquidity_penalty: float
    missing_fields: tuple[str, ...]
    completeness: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_rr": self.raw_rr,
            "effective_rr": self.effective_rr,
            "cost_penalty_total": self.cost_penalty_total,
            "spread_penalty": self.spread_penalty,
            "slippage_penalty": self.slippage_penalty,
            "latency_penalty": self.latency_penalty,
            "funding_penalty": self.funding_penalty,
            "liquidity_penalty": self.liquidity_penalty,
            "missing_fields": list(self.missing_fields),
            "execution_cost_completeness": self.completeness,
        }


def calculate_effective_rr(
    raw_rr: Any,
    execution_ctx: Mapping[str, Any],
    *,
    include_missing_penalty: bool = False,
) -> EffectiveRRResult:
    """Canonical execution-adjusted RR contract.

    This helper is intentionally small and deterministic so BACKTEST, PAPER,
    and LIVE-adjacent pre-submit checks can use the same calculation without
    copying formula fragments.

    Contract:
    - raw_rr is treated as pre-cost RR.
    - effective_rr = max(raw_rr - total_execution_penalty, 0).
    - spread, slippage, latency, funding, and liquidity penalties are sourced
      only from alphaforge.execution.build_execution_cost_model(...).
    - missing execution fields are surfaced instead of hidden behind raw_rr
      fallback.
    """
    try:
        raw = float(raw_rr or 0.0)
    except (TypeError, ValueError):
        raw = 0.0
    model = build_execution_cost_model(
        execution_ctx,
        include_missing_penalty=include_missing_penalty,
    )
    effective = round(max(raw - model.total_penalty, 0.0), 6)
    return EffectiveRRResult(
        raw_rr=round(raw, 6),
        effective_rr=effective,
        cost_penalty_total=model.total_penalty,
        spread_penalty=model.spread_penalty,
        slippage_penalty=model.slippage_penalty,
        latency_penalty=model.latency_penalty,
        funding_penalty=model.funding_penalty,
        liquidity_penalty=model.liquidity_penalty,
        missing_fields=model.missing_fields,
        completeness=model.completeness,
    )
