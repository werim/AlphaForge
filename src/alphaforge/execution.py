from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

EXECUTION_EVIDENCE_COMPLETE_MEASURED = "COMPLETE_MEASURED"
EXECUTION_EVIDENCE_PARTIAL_ESTIMATED = "PARTIAL_ESTIMATED"
EXECUTION_EVIDENCE_UNAVAILABLE_BLOCKING = "UNAVAILABLE_BLOCKING"
EXECUTION_EVIDENCE_INVALID_FAKE_ZERO = "INVALID_FAKE_ZERO"

MEASURED_STATUSES = {"MEASURED", "MEASURED_PUBLIC", "MEASURED_EXCHANGE"}
ESTIMATED_STATUSES = {"MODEL_ESTIMATE", "ESTIMATED", "ESTIMATED_BACKTEST"}
UNAVAILABLE_STATUSES = {"", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST", "NULL"}
REQUIRED_EXECUTION_FIELDS = (
    "spread_pct",
    "expected_slippage_pct",
    "latency_ms",
    "liquidity_score",
    "funding_rate_pct",
    "orderbook_imbalance",
    "volatility_regime",
)

SOURCE_MEASURED = "MEASURED"
SOURCE_ESTIMATED_BACKTEST = "ESTIMATED_BACKTEST"
SOURCE_MODELLED = "MODELLED"
SOURCE_UNAVAILABLE = "UNAVAILABLE"


def execution_context_is_unavailable(execution_ctx: Mapping[str, Any] | None) -> bool:
    """Return whether the active execution-safety evidence is unavailable.

    When a runtime safety evaluation exists it is authoritative over the raw
    classifier, because optional/disabled evidence (for example orderbook when
    its filter is disabled) must not poison persistence/readiness metadata.
    """
    ctx = dict(execution_ctx or {})
    status_raw = ctx.get("safety_evidence_status", ctx.get("evidence_status"))
    if status_raw is None:
        return True
    status = str(status_raw).strip().upper()
    return status in {
        "",
        "UNKNOWN",
        "UNAVAILABLE",
        "UNAVAILABLE_BACKTEST",
        EXECUTION_EVIDENCE_UNAVAILABLE_BLOCKING,
        EXECUTION_EVIDENCE_INVALID_FAKE_ZERO,
        "NULL",
    }


EXECUTION_COST_REFERENCE_PRICE = "STRATEGY_ENTRY"
EXECUTION_COST_PERCENTAGE_DENOMINATOR = "STRATEGY_ENTRY"
EXECUTION_COST_SIGN_CONVENTION = "POSITIVE_IS_ADVERSE"
PROVENANCE_ACTUAL = "ACTUAL"
PROVENANCE_ESTIMATED = "ESTIMATED"
PROVENANCE_ASSUMED = "ASSUMED"
PROVENANCE_MODELLED = SOURCE_MODELLED
PROVENANCE_UNAVAILABLE = SOURCE_UNAVAILABLE

_EXECUTION_COST_PROVENANCE = {
    PROVENANCE_ACTUAL,
    PROVENANCE_ESTIMATED,
    PROVENANCE_ASSUMED,
    PROVENANCE_MODELLED,
    PROVENANCE_UNAVAILABLE,
}


def _finite_positive_price(value: Any, *, field: str) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite positive price") from exc
    if price <= 0.0 or price != price or price in {float("inf"), float("-inf")}:
        raise ValueError(f"{field} must be a finite positive price")
    return price


def _execution_cost_provenance(value: Any, *, actual_fill_available: bool) -> str:
    provenance = str(value or PROVENANCE_UNAVAILABLE).strip().upper()
    if provenance not in _EXECUTION_COST_PROVENANCE:
        raise ValueError(f"unsupported execution-cost provenance: {provenance}")
    if not actual_fill_available:
        return PROVENANCE_UNAVAILABLE
    return provenance


@dataclass(frozen=True)
class ExecutionCostSemantics:
    """Canonical entry -> expected fill -> actual fill execution-cost contract.

    All percentage and basis-point values use the strategy entry as denominator.
    Price deltas are side-normalized so positive always means adverse execution.
    Fees and other explicit penalties are deliberately outside this value object.
    """

    side: str
    entry: float
    expected_fill: float
    actual_fill: float | None
    expected_execution_cost_price: float
    expected_execution_cost_pct: float
    expected_execution_cost_bps: float
    realized_execution_deviation_price: float | None
    realized_execution_deviation_pct: float | None
    realized_execution_deviation_bps: float | None
    total_realized_execution_cost_price: float | None
    total_realized_execution_cost_pct: float | None
    total_realized_execution_cost_bps: float | None
    expected_fill_provenance: str
    actual_fill_provenance: str
    decision_timestamp: str | None
    fill_timestamp: str | None
    reference_price: str = EXECUTION_COST_REFERENCE_PRICE
    percentage_denominator: str = EXECUTION_COST_PERCENTAGE_DENOMINATOR
    sign_convention: str = EXECUTION_COST_SIGN_CONVENTION
    fee_treatment: str = "SEPARATE_NOT_INCLUDED"

    def decision_time_dict(self) -> dict[str, Any]:
        """Return only evidence available before submission/fill."""
        return {
            "entry": self.entry,
            "expected_fill": self.expected_fill,
            "expected_execution_cost_price": self.expected_execution_cost_price,
            "expected_execution_cost_pct": self.expected_execution_cost_pct,
            "expected_execution_cost_bps": self.expected_execution_cost_bps,
            "expected_fill_provenance": self.expected_fill_provenance,
            "decision_timestamp": self.decision_timestamp,
            "reference_price": self.reference_price,
            "percentage_denominator": self.percentage_denominator,
            "sign_convention": self.sign_convention,
            "fee_treatment": self.fee_treatment,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.decision_time_dict(),
            "actual_fill": self.actual_fill,
            "realized_execution_deviation_price": self.realized_execution_deviation_price,
            "realized_execution_deviation_pct": self.realized_execution_deviation_pct,
            "realized_execution_deviation_bps": self.realized_execution_deviation_bps,
            "total_realized_execution_cost_price": self.total_realized_execution_cost_price,
            "total_realized_execution_cost_pct": self.total_realized_execution_cost_pct,
            "total_realized_execution_cost_bps": self.total_realized_execution_cost_bps,
            "actual_fill_provenance": self.actual_fill_provenance,
            "fill_timestamp": self.fill_timestamp,
        }


def build_execution_cost_semantics(
    *,
    entry: Any,
    expected_fill: Any,
    actual_fill: Any | None,
    side: Any,
    expected_fill_provenance: str = PROVENANCE_ESTIMATED,
    actual_fill_provenance: str = PROVENANCE_UNAVAILABLE,
    decision_timestamp: str | None = None,
    fill_timestamp: str | None = None,
) -> ExecutionCostSemantics:
    """Build the canonical cost decomposition without fees or future inference."""
    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    entry_price = _finite_positive_price(entry, field="entry")
    expected_price = _finite_positive_price(expected_fill, field="expected_fill")
    actual_price = None if actual_fill is None else _finite_positive_price(actual_fill, field="actual_fill")
    side_sign = 1.0 if normalized_side == "LONG" else -1.0

    expected_cost_price = side_sign * (expected_price - entry_price)
    expected_cost_pct = expected_cost_price / entry_price
    if actual_price is None:
        realized_deviation_price = None
        total_cost_price = None
    else:
        realized_deviation_price = side_sign * (actual_price - expected_price)
        total_cost_price = side_sign * (actual_price - entry_price)

    expected_provenance = _execution_cost_provenance(
        expected_fill_provenance, actual_fill_available=True)
    actual_provenance = _execution_cost_provenance(
        actual_fill_provenance, actual_fill_available=actual_price is not None)
    return ExecutionCostSemantics(
        side=normalized_side,
        entry=entry_price,
        expected_fill=expected_price,
        actual_fill=actual_price,
        expected_execution_cost_price=expected_cost_price,
        expected_execution_cost_pct=expected_cost_pct,
        expected_execution_cost_bps=expected_cost_pct * 10_000.0,
        realized_execution_deviation_price=realized_deviation_price,
        realized_execution_deviation_pct=(
            None if realized_deviation_price is None else realized_deviation_price / entry_price),
        realized_execution_deviation_bps=(
            None if realized_deviation_price is None else realized_deviation_price / entry_price * 10_000.0),
        total_realized_execution_cost_price=total_cost_price,
        total_realized_execution_cost_pct=(
            None if total_cost_price is None else total_cost_price / entry_price),
        total_realized_execution_cost_bps=(
            None if total_cost_price is None else total_cost_price / entry_price * 10_000.0),
        expected_fill_provenance=expected_provenance,
        actual_fill_provenance=actual_provenance,
        decision_timestamp=decision_timestamp,
        fill_timestamp=fill_timestamp if actual_price is not None else None,
    )


def weighted_average_fill_price(fills: Any) -> float | None:
    """Return the quantity-weighted price for canonical ``fills`` ledger rows.

    Empty input means the realized fill is unavailable. Invalid or non-positive
    price/quantity evidence is rejected instead of being coerced to zero.
    """
    rows = list(fills or [])
    if not rows:
        return None
    total_quantity = 0.0
    total_notional = 0.0
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("fill evidence must be a mapping")
        price = _finite_positive_price(row.get("price"), field="fill.price")
        quantity = _finite_positive_price(
            row.get("qty", row.get("quantity")), field="fill.qty")
        total_quantity += quantity
        total_notional += price * quantity
    return total_notional / total_quantity


def execution_cost_semantics_from_record(
    record: Mapping[str, Any],
) -> ExecutionCostSemantics | None:
    """Adapt a persisted/legacy fill record to the canonical contract.

    No actual fill is inferred from entry. A legacy ``expected_slippage_pct``
    may reconstruct expected fill only when side and entry are explicit because
    it is pre-submit model evidence, not future market state.
    """
    raw_side = str(record.get("side") or "").strip().upper()
    side = {"BUY": "LONG", "SELL": "SHORT"}.get(raw_side, raw_side)
    if side not in {"LONG", "SHORT"}:
        return None
    entry = record.get("entry", record.get("entry_price"))
    expected_fill = record.get("expected_fill", record.get("expected_fill_price"))
    if expected_fill is None and record.get("expected_slippage_pct") is not None:
        try:
            entry_price = _finite_positive_price(entry, field="entry")
            expected_pct = abs(float(record["expected_slippage_pct"]))
        except (TypeError, ValueError, KeyError):
            return None
        expected_fill = entry_price * (
            1.0 + expected_pct if side == "LONG" else 1.0 - expected_pct)

    actual_fill = record.get("actual_fill", record.get("filled_entry_price"))
    mode = str(record.get("mode") or "").strip().upper()
    expected_provenance = str(
        record.get("expected_fill_provenance")
        or (PROVENANCE_MODELLED if mode == "PAPER" else PROVENANCE_ESTIMATED)
    )
    actual_provenance = record.get("actual_fill_provenance")
    if actual_provenance is None:
        actual_provenance = (
            PROVENANCE_MODELLED
            if actual_fill is not None and mode == "PAPER"
            else PROVENANCE_UNAVAILABLE
        )
    try:
        return build_execution_cost_semantics(
            entry=entry,
            expected_fill=expected_fill,
            actual_fill=actual_fill,
            side=side,
            expected_fill_provenance=expected_provenance,
            actual_fill_provenance=str(actual_provenance),
            decision_timestamp=record.get("decision_timestamp", record.get("decision_time")),
            fill_timestamp=record.get("fill_timestamp", record.get("filled_at")),
        )
    except ValueError:
        return None


def build_execution_review_metrics(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return canonical fill-quality evidence plus narrow legacy aliases."""
    semantics = execution_cost_semantics_from_record(record)
    if semantics is None:
        return {
            "entry_price": record.get("entry_price"),
            "expected_fill_price": record.get("expected_fill"),
            "filled_entry_price": record.get("filled_entry_price"),
            "expected_slippage_pct": None,
            "realized_slippage_pct": None,
            "fill_quality_score": None,
            "actual_fill_provenance": PROVENANCE_UNAVAILABLE,
            "execution_cost_semantics_status": "UNAVAILABLE",
        }

    metrics = semantics.as_dict()
    deviation_pct = semantics.realized_execution_deviation_pct
    fill_quality = (
        None
        if deviation_pct is None
        else max(0.0, min(1.0, 1.0 - max(deviation_pct, 0.0) * 100.0))
    )
    metrics.update({
        "entry_price": semantics.entry,
        "expected_fill_price": semantics.expected_fill,
        "filled_entry_price": semantics.actual_fill,
        "expected_slippage_pct": semantics.expected_execution_cost_pct,
        # Compatibility alias: the historical entry -> fill metric is total
        # realized cost, not actual-vs-expected deviation.
        "realized_slippage_pct": semantics.total_realized_execution_cost_pct,
        "actual_slippage_pct_semantics": "TOTAL_REALIZED_EXECUTION_COST_PCT",
        "fill_quality_score": fill_quality,
        "execution_cost_semantics_status": "AVAILABLE",
    })
    return metrics



def build_execution_context(market_ctx: Mapping[str, Any], funding_rate_pct: float | None = None) -> dict[str, Any]:
    klines = list(market_ctx.get("recent_klines", []) or [])
    expected_slippage_pct, slippage_unit_assumed = normalize_pct_input(
        market_ctx.get("expected_slippage_pct", _expected_slippage_pct(klines, market_ctx)),
        field="expected_slippage_pct",
    )
    raw_spread = market_ctx.get("spread_pct", _spread_pct_from_prices(market_ctx))
    spread_pct, spread_unit_assumed = normalize_pct_input(raw_spread, field="spread_pct")

    def _to_float(v: Any) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    explicit_spread = _to_float(market_ctx.get("spread_pct"))
    best_bid = _to_float(market_ctx.get("best_bid"))
    best_ask = _to_float(market_ctx.get("best_ask"))
    measured_spread = (
        explicit_spread is not None
        or (best_bid is not None and best_bid > 0.0 and best_ask is not None and best_ask > 0.0)
    ) and _to_float(raw_spread) is not None
    spread_status = str(
        market_ctx.get("spread_status", "MEASURED" if measured_spread else "UNAVAILABLE")
    )
    spread_source = str(
        market_ctx.get("spread_source", "BOOK_TICKER" if measured_spread else "UNAVAILABLE")
    )
    if not measured_spread:
        spread_status = "UNAVAILABLE"
        spread_source = "UNAVAILABLE"

    slippage_has_evidence = (
        market_ctx.get("expected_slippage_pct") not in (None, "")
        or bool(klines)
    )
    slippage_status = str(
        market_ctx.get(
            "slippage_status",
            "MODEL_ESTIMATE" if slippage_has_evidence else "UNAVAILABLE",
        )
    )
    slippage_source = str(
        market_ctx.get(
            "slippage_source",
            (
                "KLINE_RANGE_MODEL"
                if klines
                else "EXPLICIT_EXECUTION_SLIPPAGE"
                if slippage_has_evidence
                else "UNAVAILABLE"
            ),
        )
    )
    if not slippage_has_evidence:
        slippage_status = "UNAVAILABLE"
        slippage_source = "UNAVAILABLE"

    md_latency = _to_float(market_ctx.get("market_data_latency_ms"))
    md_latency_status = str(market_ctx.get("market_data_latency_status", "MEASURED" if md_latency is not None else "UNAVAILABLE"))
    md_latency_source = str(market_ctx.get("market_data_latency_source", "UNKNOWN" if md_latency is not None else "UNAVAILABLE"))

    # Keep public market-data/network RTT separate from executable order latency.
    # Generic latency_ms is accepted only when it is explicitly supplied as
    # execution evidence (for example a PAPER/BACKTEST model assumption).
    explicit_latency = _to_float(market_ctx.get("latency_ms"))
    explicit_latency_status = str(
        market_ctx.get(
            "latency_status",
            "UNAVAILABLE" if explicit_latency is None else "MODEL_ESTIMATE",
        )
    )
    explicit_latency_source = str(
        market_ctx.get(
            "latency_source",
            "UNAVAILABLE" if explicit_latency is None else "EXPLICIT_EXECUTION_LATENCY",
        )
    )

    submit_ack = _to_float(market_ctx.get("submit_ack_latency_ms"))
    submit_ack_status = str(market_ctx.get("submit_ack_latency_status", "UNAVAILABLE" if submit_ack is None else "MEASURED"))
    submit_ack_source = str(market_ctx.get("submit_ack_latency_source", "UNAVAILABLE" if submit_ack is None else "RUNTIME_ACK"))

    # A measured submit/ack latency is authoritative when available. Public
    # market-data HTTP RTT must never be promoted into this field.
    if submit_ack is not None:
        execution_latency = submit_ack
        execution_latency_status = submit_ack_status
        execution_latency_source = submit_ack_source
    else:
        execution_latency = explicit_latency
        execution_latency_status = explicit_latency_status
        execution_latency_source = explicit_latency_source

    funding = funding_rate_pct if funding_rate_pct is not None else market_ctx.get("funding_rate_pct")
    funding_val = _to_float(funding)
    funding_status = str(market_ctx.get("funding_status", "MEASURED" if funding_val is not None else "UNAVAILABLE"))
    funding_source = str(market_ctx.get("funding_source", "UNKNOWN" if funding_val is not None else "UNAVAILABLE"))

    fee = _to_float(market_ctx.get("fee_pct"))
    fee_status = str(market_ctx.get("fee_status", "CONFIGURED" if fee is not None else "UNAVAILABLE"))
    fee_source = str(market_ctx.get("fee_source", "CONFIGURED_PAPER_ASSUMPTION" if fee is not None else "UNAVAILABLE"))
    if fee is not None and (fee < 0 or fee_status.upper() == "UNAVAILABLE"):
        fee = None

    orderbook = _to_float(market_ctx.get("orderbook_imbalance"))
    orderbook_status = str(market_ctx.get("orderbook_status", "MEASURED" if orderbook is not None else "UNAVAILABLE"))
    orderbook_source = str(market_ctx.get("orderbook_source", "UNKNOWN" if orderbook is not None else "UNAVAILABLE"))

    raw_liquidity = market_ctx.get("liquidity_score")
    if raw_liquidity in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
        liquidity_score = None
    else:
        liquidity_score = float(raw_liquidity)
    raw_volatility_regime = market_ctx.get("volatility_regime")
    if raw_volatility_regime in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
        volatility_regime = _volatility_regime(klines) if klines else None
    else:
        volatility_regime = str(raw_volatility_regime)

    liquidity_status = str(market_ctx.get("liquidity_status", "MEASURED" if market_ctx.get("liquidity_score") is not None else "UNAVAILABLE"))
    liquidity_source = str(market_ctx.get("liquidity_source", "UNKNOWN" if market_ctx.get("liquidity_score") is not None else "UNAVAILABLE"))
    volatility_status = str(market_ctx.get("volatility_status", "MEASURED" if klines else "UNAVAILABLE"))
    volatility_source = str(market_ctx.get(
        "volatility_source",
        market_ctx.get("recent_klines_source", "KLINE_RANGE") if klines else "UNAVAILABLE",
    ))

    return {
        # Preserve the legacy normalized estimate for MTF/research consumers,
        # but keep slippage_status=UNAVAILABLE so execution-safety remains fail-closed.
        "expected_slippage_pct": max(expected_slippage_pct, 0.0),
        "expected_slippage_legacy_pct": max(expected_slippage_pct, 0.0),
        "slippage_status": slippage_status,
        "slippage_source": slippage_source,
        "expected_slippage_pct_zero_verified": bool(
            market_ctx.get("expected_slippage_pct_zero_verified", False)
        ),
        "market_data_latency_ms": max(md_latency, 0.0) if md_latency is not None else None,
        "market_data_latency_status": md_latency_status,
        "market_data_latency_source": md_latency_source,
        "submit_ack_latency_ms": max(submit_ack, 0.0) if submit_ack is not None else None,
        "submit_ack_latency_status": submit_ack_status,
        "submit_ack_latency_source": submit_ack_source,
        "latency_ms": max(execution_latency, 0.0) if execution_latency is not None else None,
        "latency_status": execution_latency_status,
        "latency_source": execution_latency_source,
        "latency_ms_zero_verified": bool(
            market_ctx.get("latency_ms_zero_verified", False)
        ),
        "spread_pct": max(spread_pct, 0.0) if spread_status != "UNAVAILABLE" else None,
        "spread_status": spread_status,
        "spread_source": spread_source,
        "spread_pct_zero_verified": bool(
            market_ctx.get("spread_pct_zero_verified", False)
        ),
        "spread_unit_assumed": spread_unit_assumed,
        "slippage_unit_assumed": slippage_unit_assumed,
        "orderbook_imbalance": max(min(orderbook, 1.0), -1.0) if orderbook is not None else None,
        "orderbook_status": orderbook_status,
        "orderbook_source": orderbook_source,
        "orderbook_imbalance_zero_verified": bool(
            market_ctx.get("orderbook_imbalance_zero_verified", False)
        ),
        "liquidity_score": (max(min(liquidity_score, 1.0), 0.0) if liquidity_score is not None and liquidity_status != "UNAVAILABLE" else None),
        "liquidity_status": liquidity_status,
        "liquidity_source": liquidity_source,
        "funding_rate_pct": funding_val,
        "funding_status": funding_status,
        "funding_source": funding_source,
        "funding_rate_pct_zero_verified": bool(
            market_ctx.get("funding_rate_pct_zero_verified", False)
        ),
        "fee_pct": fee,
        "fee_status": fee_status if fee is not None else "UNAVAILABLE",
        "fee_source": fee_source if fee is not None else "UNAVAILABLE",
        "volatility_regime": volatility_regime if volatility_regime is not None and volatility_status.upper() != "UNAVAILABLE" else None,
        "volatility_status": volatility_status,
        "volatility_source": volatility_source,
        "evidence_status": classify_execution_evidence({
            "spread_pct": max(spread_pct, 0.0) if spread_status != "UNAVAILABLE" else None,
            "spread_status": spread_status,
            "expected_slippage_pct": max(expected_slippage_pct, 0.0) if slippage_status != "UNAVAILABLE" else None,
            "slippage_status": slippage_status,
            "latency_ms": max(execution_latency, 0.0) if execution_latency is not None else None,
            "latency_status": execution_latency_status,
            "market_data_latency_status": md_latency_status,
            "liquidity_score": (max(min(liquidity_score, 1.0), 0.0) if liquidity_score is not None and liquidity_status != "UNAVAILABLE" else None),
            "liquidity_status": liquidity_status,
            "funding_rate_pct": funding_val,
            "funding_status": funding_status,
            "orderbook_imbalance": max(min(orderbook, 1.0), -1.0) if orderbook is not None else None,
            "orderbook_status": orderbook_status,
            "volatility_regime": volatility_regime if volatility_regime is not None and volatility_status.upper() != "UNAVAILABLE" else None,
            "volatility_status": volatility_status,
        }),
        "spoof_risk": float(market_ctx.get("spoof_risk", 0.0) or 0.0),
        "absorption_score": float(market_ctx.get("absorption_score", 0.0) or 0.0),
    }


def neutral_execution_context() -> dict[str, Any]:
    return {
        "expected_slippage_pct": 0.0,
        "latency_ms": 50.0,
        "spread_pct": 0.0,
        "spread_source": "UNKNOWN",
        "orderbook_imbalance": 0.0,
        "liquidity_score": 1.0,
        "funding_rate_pct": None,
        "funding_status": "UNAVAILABLE",
        "volatility_regime": None,
        "volatility_status": "UNAVAILABLE",
        "spoof_risk": 0.0,
        "absorption_score": 0.0,
    }


def _spread_pct_from_prices(market_ctx: Mapping[str, Any]) -> float:
    bid = float(market_ctx.get("best_bid", 0.0) or 0.0)
    ask = float(market_ctx.get("best_ask", 0.0) or 0.0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
    if mid <= 0:
        return 0.0
    return (ask - bid) / mid


def _expected_slippage_pct(klines: list[Any], market_ctx: Mapping[str, Any]) -> float:
    if not klines:
        return float(market_ctx.get("expected_slippage_pct", 0.001) or 0.001)
    highs, lows = [], []
    for k in klines[-20:]:
        if isinstance(k, Mapping):
            highs.append(float(k.get("high", 0.0) or 0.0))
            lows.append(float(k.get("low", 0.0) or 0.0))
    if not highs or not lows:
        return float(market_ctx.get("expected_slippage_pct", 0.001) or 0.001)
    avg_high = sum(highs) / len(highs)
    avg_low = sum(lows) / len(lows)
    if avg_high <= 0:
        return 0.001
    return max((avg_high - avg_low) / avg_high * 0.05, 0.0001)


def _volatility_regime(klines: list[Any]) -> str:
    if not klines:
        return "normal"
    ranges = []
    for k in klines[-20:]:
        if isinstance(k, Mapping):
            h = float(k.get("high", 0.0) or 0.0)
            l = float(k.get("low", 0.0) or 0.0)
            if h > 0:
                ranges.append((h - l) / h)
    if not ranges:
        return "normal"
    r = sum(ranges) / len(ranges)
    if r > 0.02:
        return "high"
    if r < 0.005:
        return "low"
    return "normal"



@dataclass(frozen=True)
class ExecutionCostBreakdown:
    spread_pct: float | None
    spread_source: str
    slippage_pct: float | None
    slippage_source: str
    fee_pct: float | None
    fee_source: str
    funding_rate_pct: float | None
    funding_source: str
    latency_ms: float | None
    latency_source: str
    liquidity_score: float | None
    liquidity_status: str
    volatility_penalty_pct: float | None
    volatility_source: str
    total_explicit_cost_pct: float
    raw_rr: float
    effective_rr: float
    cost_penalty_rr: float
    reject_flags: tuple[str, ...]
    unavailable_fields: tuple[str, ...]
    diagnostics_json: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "spread_pct": self.spread_pct, "spread_source": self.spread_source,
            "slippage_pct": self.slippage_pct, "expected_slippage_pct": self.slippage_pct, "slippage_source": self.slippage_source,
            "fee_pct": self.fee_pct, "fee_source": self.fee_source,
            "funding_rate_pct": self.funding_rate_pct, "funding_source": self.funding_source,
            "latency_ms": self.latency_ms, "latency_source": self.latency_source,
            "liquidity_score": self.liquidity_score, "liquidity_status": self.liquidity_status,
            "volatility_penalty_pct": self.volatility_penalty_pct, "volatility_source": self.volatility_source,
            "total_explicit_cost_pct": self.total_explicit_cost_pct, "total_cost_pct": self.total_explicit_cost_pct, "raw_rr": self.raw_rr, "effective_rr": self.effective_rr,
            "cost_penalty": self.cost_penalty_rr, "cost_penalty_rr": self.cost_penalty_rr, "cost_penalty_total": self.cost_penalty_rr,
            "reject_flags": list(self.reject_flags), "unavailable_fields": list(self.unavailable_fields),
            "diagnostics_json": self.diagnostics_json,
        }


def _source_from_status(ctx: Mapping[str, Any], status_key: str, source_key: str, *, estimated: str = SOURCE_MODELLED) -> str:
    status = str(ctx.get(status_key, "") or "").upper()
    source = str(ctx.get(source_key, "") or "").upper()
    if status in UNAVAILABLE_STATUSES or source in UNAVAILABLE_STATUSES:
        return SOURCE_UNAVAILABLE
    if "BACKTEST" in status or "BACKTEST" in source:
        return SOURCE_ESTIMATED_BACKTEST
    if status in ESTIMATED_STATUSES:
        return estimated
    if status in MEASURED_STATUSES or source not in {"", "UNKNOWN"}:
        return SOURCE_MEASURED
    return SOURCE_UNAVAILABLE


def build_execution_cost_breakdown(raw_rr: Any, execution_ctx: Mapping[str, Any], *, min_effective_rr: float = 1.6, thresholds: Mapping[str, Any] | None = None, include_missing_penalty: bool = False) -> ExecutionCostBreakdown:
    try:
        raw = float(raw_rr or 0.0)
    except (TypeError, ValueError):
        raw = 0.0
    model = build_execution_cost_model(execution_ctx, include_missing_penalty=include_missing_penalty)
    effective = round(max(raw - model.total_penalty, 0.0), 6)

    def f(key: str) -> float | None:
        value = execution_ctx.get(key)
        if value in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    t = dict(thresholds or {})
    max_spread = float(t.get("MAX_SPREAD_PCT", t.get("max_spread_pct", 0.0025)) or 0.0025)
    max_slip = float(t.get("MAX_SLIPPAGE_PCT", t.get("MAX_EXPECTED_SLIPPAGE_PCT", 0.002)) or 0.002)
    max_total = float(t.get("MAX_TOTAL_COST_PCT", 0.20) or 0.20)
    min_liq = float(t.get("MIN_LIQUIDITY_SCORE", 0.30) or 0.30)
    max_latency = float(t.get("MAX_LATENCY_MS", 2500) or 2500)
    max_vol_pen = float(t.get("MAX_VOLATILITY_PENALTY_PCT", 0.20) or 0.20)
    reject_unknown = bool(t.get("REJECT_UNKNOWN_EXECUTION_CONTEXT", False))
    require_funding = bool(t.get("REQUIRE_FUNDING_RATE", False))
    max_funding = float(t.get("MAX_ABS_FUNDING_RATE_PCT", 1.0) or 1.0)

    spread, slip, fee, funding, latency, liq = f("spread_pct"), f("expected_slippage_pct"), f("fee_pct"), f("funding_rate_pct"), f("latency_ms"), f("liquidity_score")
    total_explicit_cost_pct = round(sum(abs(x) for x in (spread, slip, fee, funding) if x is not None), 10)
    flags: list[str] = []
    if slip is not None and slip > max_slip: flags.append("HIGH_SLIPPAGE")
    if spread is not None and spread > max_spread: flags.append("HIGH_SPREAD")
    if total_explicit_cost_pct > max_total: flags.append("HIGH_TOTAL_COST")
    if liq is not None and liq < min_liq: flags.append("LOW_LIQUIDITY")
    if latency is not None and latency > max_latency: flags.append("HIGH_LATENCY")
    if model.volatility_penalty > max_vol_pen: flags.append("EXCESSIVE_VOLATILITY_PENALTY")
    if funding is None and require_funding: flags.append("FUNDING_UNAVAILABLE")
    if funding is not None and abs(funding) > max_funding: flags.append("FUNDING_TOO_HIGH")
    if model.missing_fields and reject_unknown: flags.append("EXECUTION_CONTEXT_UNAVAILABLE")
    if effective < float(min_effective_rr): flags.append("LOW_EFFECTIVE_RR")
    diagnostics = {**model.__dict__, "total_explicit_cost_pct": total_explicit_cost_pct, "total_rr_penalty": model.total_penalty, "cost_penalty_rr": model.total_penalty, "formula": "effective_rr = raw_rr - spread_penalty - slippage_penalty - fee_penalty - funding_penalty - latency_penalty - liquidity_penalty - volatility_penalty"}
    return ExecutionCostBreakdown(
        spread, _source_from_status(execution_ctx, "spread_status", "spread_source", estimated=SOURCE_ESTIMATED_BACKTEST),
        slip, _source_from_status(execution_ctx, "slippage_status", "slippage_source", estimated=SOURCE_MODELLED),
        fee, _source_from_status(execution_ctx, "fee_status", "fee_source", estimated=SOURCE_MODELLED),
        funding, _source_from_status(execution_ctx, "funding_status", "funding_source", estimated=SOURCE_ESTIMATED_BACKTEST),
        latency, _source_from_status(execution_ctx, "latency_status", "latency_source", estimated=SOURCE_MODELLED),
        liq, str(execution_ctx.get("liquidity_status", SOURCE_UNAVAILABLE) or SOURCE_UNAVAILABLE),
        model.volatility_penalty, _source_from_status(execution_ctx, "volatility_status", "volatility_source", estimated=SOURCE_ESTIMATED_BACKTEST),
        total_explicit_cost_pct, round(raw, 6), effective, model.total_penalty, tuple(dict.fromkeys(flags)), model.missing_fields, json.dumps(diagnostics, sort_keys=True),
    )

@dataclass(frozen=True)
class ExecutionCostModel:
    spread_penalty: float
    slippage_penalty: float
    latency_penalty: float
    fee_penalty: float
    funding_penalty: float
    liquidity_penalty: float
    volatility_penalty: float
    total_penalty: float
    missing_fields: tuple[str, ...]
    completeness: str


def classify_execution_evidence(execution_ctx: Mapping[str, Any], *, require_measured: bool = False) -> str:
    statuses = {
        "spread_pct": str(execution_ctx.get("spread_status", "")).upper(),
        "expected_slippage_pct": str(execution_ctx.get("slippage_status", "")).upper(),
        "latency_ms": str(execution_ctx.get("latency_status", "")).upper(),
        "liquidity_score": str(execution_ctx.get("liquidity_status", "")).upper(),
        "funding_rate_pct": str(execution_ctx.get("funding_status", "")).upper(),
        "orderbook_imbalance": str(execution_ctx.get("orderbook_status", "")).upper(),
        "volatility_regime": str(execution_ctx.get("volatility_status", "")).upper(),
    }
    missing = [field for field in REQUIRED_EXECUTION_FIELDS if execution_ctx.get(field) in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST")]
    fake_zero_fields = []
    for field in ("spread_pct", "expected_slippage_pct", "latency_ms", "funding_rate_pct", "orderbook_imbalance"):
        try:
            is_zero = float(execution_ctx.get(field)) == 0.0
        except (TypeError, ValueError):
            is_zero = False
        status = statuses.get(field, "")
        if is_zero and (require_measured or status in MEASURED_STATUSES) and not bool(execution_ctx.get(f"{field}_zero_verified", False)):
            fake_zero_fields.append(field)
    if fake_zero_fields:
        return EXECUTION_EVIDENCE_INVALID_FAKE_ZERO
    if missing or any(status in UNAVAILABLE_STATUSES for status in statuses.values()):
        return EXECUTION_EVIDENCE_UNAVAILABLE_BLOCKING
    if require_measured and any(status not in MEASURED_STATUSES for status in statuses.values()):
        return EXECUTION_EVIDENCE_UNAVAILABLE_BLOCKING
    if all(status in MEASURED_STATUSES for status in statuses.values()):
        return EXECUTION_EVIDENCE_COMPLETE_MEASURED
    if any(status in ESTIMATED_STATUSES for status in statuses.values()):
        return EXECUTION_EVIDENCE_PARTIAL_ESTIMATED
    return EXECUTION_EVIDENCE_UNAVAILABLE_BLOCKING


def build_execution_cost_model(execution_ctx: Mapping[str, Any], *, include_missing_penalty: bool = False) -> ExecutionCostModel:
    missing=[]
    def req_float(k:str):
        v=execution_ctx.get(k)
        if v in (None, '', 'UNKNOWN', 'UNAVAILABLE', 'UNAVAILABLE_BACKTEST'):
            missing.append(k); return None
        try:return float(v)
        except (TypeError,ValueError): missing.append(k); return None

    spread=req_float('spread_pct')
    slippage=req_float('expected_slippage_pct')
    latency=req_float('latency_ms')
    funding=req_float('funding_rate_pct')
    fee=req_float('fee_pct') if 'fee_pct' in execution_ctx else 0.0
    liquidity=req_float('liquidity_score')
    volatility_regime = str(execution_ctx.get('volatility_regime', '') or '').lower()
    if not volatility_regime or volatility_regime in {'unknown', 'unavailable'}:
        missing.append('volatility_regime')

    spread_penalty=max((spread or 0.0)*25.0,0.0)
    slippage_penalty=max((slippage or 0.0)*30.0,0.0)
    latency_penalty=max(((latency or 0.0)/1000.0)*0.2,0.0)
    fee_penalty=max(abs(fee or 0.0)*10.0,0.0)
    funding_penalty=max(abs(funding or 0.0)*2.5,0.0)
    liquidity_penalty=max((1.0-max(min(liquidity if liquidity is not None else 1.0,1.0),0.0))*0.6,0.0)
    volatility_penalty={"low":0.02,"normal":0.0,"high":0.12,"extreme":0.25}.get(volatility_regime,0.10)

    completeness=classify_execution_evidence(execution_ctx)
    total=spread_penalty+slippage_penalty+fee_penalty+latency_penalty+funding_penalty+liquidity_penalty+volatility_penalty
    if include_missing_penalty and missing:
        total += min(0.5, 0.1*len(missing))
    return ExecutionCostModel(
        spread_penalty=spread_penalty,
        slippage_penalty=slippage_penalty,
        latency_penalty=latency_penalty,
        fee_penalty=fee_penalty,
        funding_penalty=funding_penalty,
        liquidity_penalty=liquidity_penalty,
        volatility_penalty=volatility_penalty,
        total_penalty=round(total, 6),
        missing_fields=tuple(sorted(set(missing))),
        completeness=completeness,
    )
def evaluate_execution_safety(
    execution_ctx: Mapping[str, Any],
    *,
    effective_rr: Any,
    min_effective_rr: float,
    thresholds: Mapping[str, Any] | None = None,
    require_measured: bool = False,
) -> dict[str, Any]:
    """Authoritative pre-submit execution-safety contract.

    This function does not recompute executable geometry. effective_rr must
    already reflect the canonical entry -> expected_fill geometry and residual
    cost treatment chosen by the caller. The contract only validates execution
    evidence and protected execution thresholds, so a high raw RR can never
    bypass unknown or unsafe execution conditions.
    """
    t = dict(thresholds or {})
    model = build_execution_cost_model(execution_ctx, include_missing_penalty=False)
    raw_evidence_status = classify_execution_evidence(
        execution_ctx, require_measured=require_measured
    )

    def threshold(*keys: str, default: float) -> float:
        for key in keys:
            value = t.get(key)
            if value not in (None, ""):
                return float(value)
        return float(default)

    def number(field: str) -> float | None:
        value = execution_ctx.get(field)
        if value in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else None

    max_spread = threshold("MAX_SPREAD_PCT", "max_spread_pct", default=0.0025)
    max_slippage = threshold(
        "MAX_EXPECTED_SLIPPAGE_PCT", "MAX_SLIPPAGE_PCT", "max_expected_slippage_pct",
        default=0.002,
    )
    max_total_cost = threshold("MAX_TOTAL_COST_PCT", "max_total_cost_pct", default=0.20)
    min_liquidity = threshold("MIN_LIQUIDITY_SCORE", "min_liquidity_score", default=0.30)
    max_latency = threshold("MAX_LATENCY_MS", "max_latency_ms", default=2500.0)
    max_volatility_penalty = threshold(
        "MAX_VOLATILITY_PENALTY_PCT", "max_volatility_penalty_pct", default=0.20
    )
    max_funding = threshold(
        "MAX_ABS_FUNDING_RATE_PCT", "max_abs_funding_rate_pct", default=0.001
    )
    reject_unknown = bool(
        t.get(
            "REJECT_UNKNOWN_EXECUTION_CONTEXT",
            t.get("reject_unknown_execution_context", True),
        )
    )
    orderbook_required = bool(
        t.get("ENABLE_ORDERBOOK_FILTER", t.get("enable_orderbook_filter", False))
    )

    status_keys = {
        "spread_pct": "spread_status",
        "expected_slippage_pct": "slippage_status",
        "latency_ms": "latency_status",
        "liquidity_score": "liquidity_status",
        "funding_rate_pct": "funding_status",
        "volatility_regime": "volatility_status",
        "orderbook_imbalance": "orderbook_status",
    }
    critical_fields = [
        "spread_pct",
        "expected_slippage_pct",
        "latency_ms",
        "liquidity_score",
        "funding_rate_pct",
        "volatility_regime",
    ]
    if orderbook_required:
        critical_fields.append("orderbook_imbalance")

    missing_fields: list[str] = []
    for field in critical_fields:
        value = execution_ctx.get(field)
        status = str(execution_ctx.get(status_keys[field], "") or "").upper()
        unavailable_value = value in (
            None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"
        )
        unavailable_status = status in UNAVAILABLE_STATUSES
        not_measured = require_measured and status not in MEASURED_STATUSES
        if unavailable_value or unavailable_status or not_measured:
            missing_fields.append(field)

    fake_zero_fields: list[str] = []
    zero_sensitive_fields = [
        "spread_pct",
        "expected_slippage_pct",
        "latency_ms",
        "funding_rate_pct",
    ]
    if orderbook_required:
        zero_sensitive_fields.append("orderbook_imbalance")
    for field in zero_sensitive_fields:
        value = number(field)
        status = str(execution_ctx.get(status_keys[field], "") or "").upper()
        if (
            require_measured
            and value == 0.0
            and status in MEASURED_STATUSES
            and not bool(execution_ctx.get(f"{field}_zero_verified", False))
        ):
            fake_zero_fields.append(field)

    active_statuses = [
        str(execution_ctx.get(status_keys[field], "") or "").upper()
        for field in critical_fields
    ]
    if fake_zero_fields:
        evidence_status = EXECUTION_EVIDENCE_INVALID_FAKE_ZERO
    elif missing_fields:
        evidence_status = EXECUTION_EVIDENCE_UNAVAILABLE_BLOCKING
    elif require_measured:
        evidence_status = EXECUTION_EVIDENCE_COMPLETE_MEASURED
    elif any(status in ESTIMATED_STATUSES for status in active_statuses):
        evidence_status = EXECUTION_EVIDENCE_PARTIAL_ESTIMATED
    elif active_statuses and all(status in MEASURED_STATUSES for status in active_statuses):
        evidence_status = EXECUTION_EVIDENCE_COMPLETE_MEASURED
    else:
        evidence_status = raw_evidence_status

    spread = number("spread_pct")
    slippage = number("expected_slippage_pct")
    fee = number("fee_pct")
    funding = number("funding_rate_pct")
    latency = number("latency_ms")
    liquidity = number("liquidity_score")
    try:
        effective = float(effective_rr)
    except (TypeError, ValueError):
        effective = 0.0
    total_explicit_cost = round(
        sum(abs(value) for value in (spread, slippage, fee, funding) if value is not None),
        10,
    )

    failed: list[str] = []
    evidence: list[dict[str, Any]] = []

    def fail(gate: str, observed: Any, limit: Any, comparison: str) -> None:
        if gate not in failed:
            failed.append(gate)
        evidence.append({
            "gate": gate,
            "observed": observed,
            "threshold": limit,
            "comparison": comparison,
            "source": "EXECUTION_SAFETY_CONTRACT",
        })

    if reject_unknown and missing_fields:
        fail(
            "EXECUTION_CONTEXT_UNAVAILABLE",
            sorted(set(missing_fields)),
            "AVAILABLE",
            "required",
        )
    if fake_zero_fields:
        fail(
            "INVALID_FAKE_ZERO",
            sorted(set(fake_zero_fields)),
            "VERIFIED_ZERO_OR_NONZERO",
            "required",
        )
    if spread is not None and spread > max_spread:
        fail("SPREAD_TOO_HIGH", spread, max_spread, ">")
    if slippage is not None and slippage > max_slippage:
        fail("SLIPPAGE_TOO_HIGH", slippage, max_slippage, ">")
    if total_explicit_cost > max_total_cost:
        fail("HIGH_TOTAL_COST", total_explicit_cost, max_total_cost, ">")
    if liquidity is not None and liquidity < min_liquidity:
        fail("THIN_LIQUIDITY", liquidity, min_liquidity, "<")
    if latency is not None and latency > max_latency:
        fail("HIGH_LATENCY", latency, max_latency, ">")
    if model.volatility_penalty > max_volatility_penalty:
        fail(
            "EXCESSIVE_VOLATILITY",
            model.volatility_penalty,
            max_volatility_penalty,
            ">",
        )
    if funding is not None and abs(funding) > max_funding:
        fail("FUNDING_TOO_HIGH", abs(funding), max_funding, ">")
    if effective < float(min_effective_rr):
        fail("LOW_EFFECTIVE_RR", effective, float(min_effective_rr), "<")

    priority = (
        "EXECUTION_CONTEXT_UNAVAILABLE",
        "INVALID_FAKE_ZERO",
        "SPREAD_TOO_HIGH",
        "SLIPPAGE_TOO_HIGH",
        "HIGH_TOTAL_COST",
        "THIN_LIQUIDITY",
        "HIGH_LATENCY",
        "EXCESSIVE_VOLATILITY",
        "FUNDING_TOO_HIGH",
        "LOW_EFFECTIVE_RR",
    )
    primary = next((gate for gate in priority if gate in failed), None)
    return {
        "accepted": not failed,
        "primary_reject_reason": primary,
        "all_failed_gates": list(failed),
        "failed_gate_evidence": evidence,
        "execution_evidence_status": evidence_status,
        "raw_execution_evidence_status": raw_evidence_status,
        "missing_fields": sorted(set(missing_fields)),
        "fake_zero_fields": sorted(set(fake_zero_fields)),
        "total_explicit_cost_pct": total_explicit_cost,
        "volatility_penalty": model.volatility_penalty,
        "effective_rr": round(effective, 6),
        "min_effective_rr": float(min_effective_rr),
        "require_measured": bool(require_measured),
    }


def normalize_pct_input(value: Any, *, field: str) -> tuple[float, str]:
    """
    Normalize spread/slippage inputs into fractional rate units.
    Contract:
      - 0.001 means 0.1%
      - 0.1 is treated as percent-point 0.1% and normalized to 0.001
    """
    try:
        raw = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0, "UNAVAILABLE"
    v = abs(raw)
    if v > 0.05:
        return v / 100.0, "PERCENT_POINT_NORMALIZED"
    return v, "FRACTIONAL_RATE"
