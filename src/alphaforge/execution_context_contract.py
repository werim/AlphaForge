from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

REQUIRED_EXECUTION_CONTEXT_FIELDS: tuple[str, ...] = (
    "volume_24h_usdt",
    "spread_pct",
    "expected_slippage_pct",
    "latency_ms",
    "liquidity_score",
    "funding_rate_pct",
    "orderbook_imbalance",
    "volatility_regime",
)

UNAVAILABLE_MARKERS = {None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"}


@dataclass(frozen=True)
class ExecutionContextAudit:
    execution_ctx: dict[str, Any]
    missing_fields: tuple[str, ...]
    execution_ctx_missing: bool
    completeness: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_ctx": self.execution_ctx,
            "missing_fields": list(self.missing_fields),
            "execution_ctx_missing": self.execution_ctx_missing,
            "execution_ctx_completeness": self.completeness,
        }


def is_unavailable(value: Any) -> bool:
    if value in UNAVAILABLE_MARKERS:
        return True
    if isinstance(value, str) and value.strip().upper() in {"", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"}:
        return True
    return False


def audit_execution_context(ctx: Mapping[str, Any]) -> ExecutionContextAudit:
    execution_ctx = dict(ctx or {})
    missing = tuple(field for field in REQUIRED_EXECUTION_CONTEXT_FIELDS if is_unavailable(execution_ctx.get(field)))
    if not missing:
        completeness = "complete"
    elif len(missing) < len(REQUIRED_EXECUTION_CONTEXT_FIELDS):
        completeness = "partial"
    else:
        completeness = "unavailable"
    return ExecutionContextAudit(
        execution_ctx=execution_ctx,
        missing_fields=missing,
        execution_ctx_missing=bool(missing),
        completeness=completeness,
    )


def with_execution_context_audit(ctx: Mapping[str, Any]) -> dict[str, Any]:
    audit = audit_execution_context(ctx)
    enriched = dict(audit.execution_ctx)
    enriched["execution_ctx_missing"] = audit.execution_ctx_missing
    enriched["execution_ctx_missing_fields"] = list(audit.missing_fields)
    enriched["execution_ctx_completeness"] = audit.completeness
    return enriched
