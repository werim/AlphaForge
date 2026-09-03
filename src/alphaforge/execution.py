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

    measured_spread = _to_float(raw_spread) is not None
    spread_status = str(market_ctx.get("spread_status", "MEASURED" if measured_spread else "UNAVAILABLE"))
    spread_source = str(market_ctx.get("spread_source", "BOOK_TICKER" if measured_spread else "UNAVAILABLE"))

    slippage_status = str(market_ctx.get("slippage_status", "MODEL_ESTIMATE"))
    slippage_source = str(market_ctx.get("slippage_source", "KLINE_RANGE_MODEL"))

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
    volatility_regime = str(market_ctx.get("volatility_regime", _volatility_regime(klines)))

    liquidity_status = str(market_ctx.get("liquidity_status", "MEASURED" if market_ctx.get("liquidity_score") is not None else "UNAVAILABLE"))
    liquidity_source = str(market_ctx.get("liquidity_source", "UNKNOWN" if market_ctx.get("liquidity_score") is not None else "UNAVAILABLE"))
    volatility_status = str(market_ctx.get("volatility_status", "MEASURED" if klines else "UNAVAILABLE"))
    volatility_source = str(market_ctx.get(
        "volatility_source",
        market_ctx.get("recent_klines_source", "KLINE_RANGE") if klines else "UNAVAILABLE",
    ))

    return {
        "expected_slippage_pct": max(expected_slippage_pct, 0.0) if slippage_status != "UNAVAILABLE" else None,
        "expected_slippage_legacy_pct": max(expected_slippage_pct, 0.0),
        "slippage_status": slippage_status,
        "slippage_source": slippage_source,
        "market_data_latency_ms": max(md_latency, 0.0) if md_latency is not None else None,
        "market_data_latency_status": md_latency_status,
        "market_data_latency_source": md_latency_source,
        "submit_ack_latency_ms": max(submit_ack, 0.0) if submit_ack is not None else None,
        "submit_ack_latency_status": submit_ack_status,
        "submit_ack_latency_source": submit_ack_source,
        "latency_ms": max(execution_latency, 0.0) if execution_latency is not None else None,
        "latency_status": execution_latency_status,
        "latency_source": execution_latency_source,
        "spread_pct": max(spread_pct, 0.0) if spread_status != "UNAVAILABLE" else None,
        "spread_status": spread_status,
        "spread_source": spread_source,
        "spread_unit_assumed": spread_unit_assumed,
        "slippage_unit_assumed": slippage_unit_assumed,
        "orderbook_imbalance": max(min(orderbook, 1.0), -1.0) if orderbook is not None else None,
        "orderbook_status": orderbook_status,
        "orderbook_source": orderbook_source,
        "liquidity_score": (max(min(liquidity_score, 1.0), 0.0) if liquidity_score is not None and liquidity_status != "UNAVAILABLE" else None),
        "liquidity_status": liquidity_status,
        "liquidity_source": liquidity_source,
        "funding_rate_pct": funding_val,
        "funding_status": funding_status,
        "funding_source": funding_source,
        "fee_pct": fee,
        "fee_status": fee_status if fee is not None else "UNAVAILABLE",
        "fee_source": fee_source if fee is not None else "UNAVAILABLE",
        "volatility_regime": volatility_regime if volatility_status != "UNAVAILABLE" else None,
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
            "volatility_regime": volatility_regime if volatility_status != "UNAVAILABLE" else None,
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
