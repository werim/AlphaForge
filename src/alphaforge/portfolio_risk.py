from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

PORTFOLIO_REJECT_REASONS = {
    "MAX_OPEN_POSITIONS", "MAX_CONCURRENT_POSITIONS", "MAX_NOTIONAL_EXPOSURE",
    "MAX_SYMBOL_NOTIONAL_EXPOSURE", "MAX_DAILY_LOSS", "MAX_ROLLING_DRAWDOWN",
    "SYMBOL_COOLDOWN_ACTIVE", "DAILY_SYMBOL_TRADE_LIMIT", "DAILY_GLOBAL_TRADE_LIMIT",
    "CORRELATION_OVEREXPOSURE", "SAME_SIDE_OVEREXPOSURE", "NET_EXPOSURE_TOO_HIGH",
    "LOSS_CLUSTER_ACTIVE", "UNKNOWN_PORTFOLIO_RISK", "INVALID_EQUITY", "INVALID_POSITION_SIZE",
}

@dataclass(slots=True)
class PortfolioRiskSnapshot:
    mode: str
    timestamp: str
    equity: float | None = None
    available_balance: float | None = None
    open_position_count: int | None = None
    max_open_positions: int | None = None
    concurrent_position_count: int | None = None
    max_concurrent_positions: int | None = None
    total_notional_exposure: float | None = None
    max_notional_exposure: float | None = None
    symbol_notional_exposure: float | None = None
    max_symbol_notional: float | None = None
    side_exposure_long: float | None = None
    side_exposure_short: float | None = None
    net_exposure: float | None = None
    gross_exposure: float | None = None
    leverage_estimate: float | None = None
    symbol_cooldown_remaining_sec: float | None = None
    trades_today_symbol: int | None = None
    trades_today_global: int | None = None
    daily_realized_pnl: float | None = None
    daily_loss_pct: float | None = None
    max_daily_loss_pct: float | None = None
    rolling_peak_equity: float | None = None
    rolling_drawdown_pct: float | None = None
    max_rolling_drawdown_pct: float | None = None
    consecutive_loss_count: int | None = None
    symbol_consecutive_loss_count: int | None = None
    loss_cluster_active: bool | None = None
    symbol_loss_cluster_active: bool | None = None
    risk_state_complete: bool | None = None
    risk_state_source: str | None = None
    risk_state_missing_fields: list[str] = field(default_factory=list)
    correlation_group: str | None = None
    correlation_group_exposure: float | None = None
    max_correlation_group_exposure: float | None = None
    correlated_position_count: int | None = None
    max_correlated_positions: int | None = None
    risk_flags: list[str] = field(default_factory=list)
    reject_reason: str = ""
    diagnostics_json: str = "{}"
    symbol_notional_exposures: dict[str, float] = field(default_factory=dict)
    correlation_group_exposures: dict[str, float] = field(default_factory=dict)
    correlated_position_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(slots=True)
class PortfolioRiskDecision:
    accepted: bool
    reject_reason: str = ""
    risk_flags: list[str] = field(default_factory=list)
    risk_state: str = "ACCEPTED"
    size_multiplier: float = 1.0
    max_allowed_size: float | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


PORTFOLIO_ALLOCATOR_VERSION = "joint_portfolio_allocator_v1"
PORTFOLIO_ALLOCATION_SCHEMA_VERSION = "portfolio_allocation_v1"
PORTFOLIO_ALLOCATION_ACTIONS = frozenset(
    {"APPROVE", "REDUCE_SIZE", "REJECT", "HOLD_CASH"}
)


@dataclass(frozen=True, slots=True)
class CandidateAllocationDecision:
    candidate_id: str
    symbol: str
    side: str
    action: str
    reason_codes: tuple[str, ...]
    requested_notional: float | None
    allocated_notional: float
    requested_risk: float | None
    allocated_risk: float | None
    requested_quantity: float | None
    allocated_quantity: float | None
    correlation_group: str
    correlation_contribution: float
    concentration_contribution: float
    candidate_inputs: dict[str, Any]
    preference_components: dict[str, float | None]
    hard_gate_accepted: bool
    hard_gate_reason: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


@dataclass(frozen=True, slots=True)
class PortfolioAllocationDecision:
    allocation_cycle_id: str
    timestamp: str
    mode: str
    action: str
    candidate_set_hash: str
    portfolio_snapshot_hash: str
    config_hash: str
    universe_hash: str
    evidence_hash: str
    git_sha: str
    release_id: str
    runtime_identity: str
    allocator_version: str
    schema_version: str
    candidates: tuple[CandidateAllocationDecision, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidates"] = [candidate.to_dict() for candidate in self.candidates]
        return payload


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def scale_candidate_exposure(
    *,
    original_notional: Any,
    risk_scale: Any = 1.0,
    original_quantity: Any | None = None,
    require_scale: bool = False,
) -> dict[str, Any]:
    """Return one fail-closed capital-scaling projection for pre-submit sizing.

    risk_scale is a fraction in [0, 1]. When a caller requires a scale,
    missing or invalid evidence is never promoted to 1.0.
    """
    try:
        notional = float(original_notional)
    except (TypeError, ValueError):
        notional = float("nan")
    if not math.isfinite(notional) or notional <= 0.0:
        return {
            "status": "INVALID",
            "reason": "INVALID_ORIGINAL_NOTIONAL",
            "original_notional": None,
            "risk_scale": None,
            "effective_notional": None,
            "original_quantity": None,
            "effective_quantity": None,
        }

    raw_scale = risk_scale
    if raw_scale is None and not require_scale:
        raw_scale = 1.0
    try:
        scale = float(raw_scale)
    except (TypeError, ValueError):
        scale = float("nan")
    if not math.isfinite(scale) or scale < 0.0 or scale > 1.0:
        return {
            "status": "INVALID",
            "reason": "INVALID_RISK_SCALE",
            "original_notional": notional,
            "risk_scale": None,
            "effective_notional": None,
            "original_quantity": None,
            "effective_quantity": None,
        }

    original_qty = None
    effective_qty = None
    if original_quantity is not None:
        try:
            original_qty = float(original_quantity)
        except (TypeError, ValueError):
            original_qty = float("nan")
        if not math.isfinite(original_qty) or original_qty <= 0.0:
            return {
                "status": "INVALID",
                "reason": "INVALID_ORIGINAL_QUANTITY",
                "original_notional": notional,
                "risk_scale": scale,
                "effective_notional": None,
                "original_quantity": None,
                "effective_quantity": None,
            }
        effective_qty = original_qty * scale

    effective = notional * scale
    return {
        "status": "COMPLETE",
        "reason": "",
        "original_notional": notional,
        "risk_scale": scale,
        "effective_notional": effective,
        "original_quantity": original_qty,
        "effective_quantity": effective_qty,
    }


def risk_based_candidate_exposure(
    *,
    equity: Any,
    entry: Any,
    stop: Any,
    risk_pct_per_trade: Any,
    rolling_drawdown_pct: Any = 0.0,
    max_rolling_drawdown_pct: Any = None,
    expected_slippage_pct: Any = None,
    max_expected_slippage_pct: Any = None,
    volatility_penalty_pct: Any = None,
    max_volatility_penalty_pct: Any = None,
    liquidity_depth_usdt: Any = None,
    max_liquidity_participation_pct: Any = 0.01,
    max_leverage: Any = 1.0,
    min_notional: Any = 0.0,
    hard_notional_caps: Mapping[str, Any] | None = None,
    require_execution_limits: bool = True,
) -> dict[str, Any]:
    """Derive PAPER notional from equity risk and executable stop distance.

    All modifiers are monotonic risk reducers. No cap, minimum or reporting
    objective may increase the risk budget. Missing mandatory execution
    evidence fails closed when the projection is authoritative.
    """
    def num(value: Any) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None

    eq = num(equity)
    px = num(entry)
    sl = num(stop)
    risk_pct = num(risk_pct_per_trade)
    if eq is None or eq <= 0 or px is None or px <= 0 or sl is None:
        return {"status": "UNAVAILABLE", "reason": "RISK_SIZING_CORE_EVIDENCE_UNAVAILABLE"}
    if risk_pct is None or risk_pct <= 0 or risk_pct > 1:
        return {"status": "INVALID", "reason": "INVALID_RISK_PCT_PER_TRADE"}

    stop_distance_pct = abs(px - sl) / px
    if not math.isfinite(stop_distance_pct) or stop_distance_pct <= 0:
        return {"status": "INVALID", "reason": "INVALID_STOP_DISTANCE"}

    dd = num(rolling_drawdown_pct)
    max_dd = num(max_rolling_drawdown_pct)
    if dd is None or dd < 0:
        return {"status": "UNAVAILABLE", "reason": "DRAWDOWN_EVIDENCE_UNAVAILABLE"}
    drawdown_multiplier = 1.0
    if max_dd is not None and max_dd > 0:
        drawdown_multiplier = max(0.0, min(1.0, 1.0 - (dd / max_dd)))
    if drawdown_multiplier <= 0:
        return {"status": "REJECTED", "reason": "MAX_ROLLING_DRAWDOWN"}

    slip = num(expected_slippage_pct)
    max_slip = num(max_expected_slippage_pct)
    vol_penalty = num(volatility_penalty_pct)
    max_vol_penalty = num(max_volatility_penalty_pct)
    depth = num(liquidity_depth_usdt)
    participation = num(max_liquidity_participation_pct)
    leverage = num(max_leverage)
    min_size = num(min_notional) or 0.0

    if require_execution_limits and (
        slip is None or max_slip is None or vol_penalty is None
        or max_vol_penalty is None or depth is None
    ):
        return {"status": "UNAVAILABLE", "reason": "RISK_SIZING_EXECUTION_EVIDENCE_UNAVAILABLE"}
    if slip is not None and max_slip is not None and slip > max_slip:
        return {"status": "REJECTED", "reason": "EXCESSIVE_EXPECTED_SLIPPAGE"}
    if participation is None or participation < 0 or participation > 1:
        return {"status": "INVALID", "reason": "INVALID_LIQUIDITY_PARTICIPATION"}
    if leverage is None or leverage <= 0 or leverage > 1:
        return {"status": "INVALID", "reason": "INVALID_PAPER_MAX_LEVERAGE"}
    if min_size < 0:
        return {"status": "INVALID", "reason": "INVALID_MIN_NOTIONAL"}

    volatility_multiplier = 1.0
    if vol_penalty is not None and max_vol_penalty is not None:
        if max_vol_penalty <= 0:
            if vol_penalty > 0:
                return {"status": "REJECTED", "reason": "EXCESSIVE_VOLATILITY"}
        else:
            if vol_penalty > max_vol_penalty:
                return {"status": "REJECTED", "reason": "EXCESSIVE_VOLATILITY"}
            volatility_multiplier = max(0.0, min(1.0, 1.0 - (vol_penalty / max_vol_penalty)))

    risk_budget_usdt = eq * risk_pct * drawdown_multiplier * volatility_multiplier
    raw_notional = risk_budget_usdt / stop_distance_pct

    caps: dict[str, float] = {
        "leverage_cap": eq * leverage,
    }
    if depth is not None and participation is not None:
        caps["liquidity_cap"] = max(0.0, depth * participation)
    for name, value in dict(hard_notional_caps or {}).items():
        parsed = num(value)
        if parsed is None:
            if require_execution_limits:
                return {"status": "UNAVAILABLE", "reason": f"{str(name).upper()}_CAP_UNAVAILABLE"}
            continue
        caps[str(name)] = max(0.0, parsed)

    selected_notional = min([raw_notional, *caps.values()]) if caps else raw_notional
    if selected_notional <= 0:
        return {"status": "REJECTED", "reason": "NO_RISK_CAPACITY", "caps": caps}
    if min_size > 0 and selected_notional < min_size:
        return {
            "status": "REJECTED",
            "reason": "BELOW_MIN_NOTIONAL",
            "selected_notional": selected_notional,
            "min_notional": min_size,
            "caps": caps,
        }

    quantity = selected_notional / px
    return {
        "status": "COMPLETE",
        "reason": "",
        "entry_basis": px,
        "stop": sl,
        "stop_distance_pct": stop_distance_pct,
        "risk_pct_per_trade": risk_pct,
        "risk_budget_usdt": risk_budget_usdt,
        "drawdown_multiplier": drawdown_multiplier,
        "volatility_multiplier": volatility_multiplier,
        "raw_risk_notional": raw_notional,
        "selected_notional": selected_notional,
        "quantity": quantity,
        "risk_at_stop_usdt": selected_notional * stop_distance_pct,
        "risk_at_stop_pct_equity": (selected_notional * stop_distance_pct) / eq,
        "caps": caps,
    }


def correlation_group_for_symbol(symbol: str, override: Mapping[str, str] | None = None) -> str:
    s = str(symbol or "").upper().replace("-", "")
    if override and s in {k.upper(): v for k, v in override.items()}:
        return {k.upper(): v for k, v in override.items()}[s]
    base = s
    for q in ("USDT", "USD", "USDC", "BUSD", "BTC", "ETH"):
        if base.endswith(q) and len(base) > len(q):
            base = base[:-len(q)]
            break
    if base in {"BTC", "WBTC", "ETH", "STETH", "WETH"}: return "CRYPTO_MAJOR"
    if base in {"DOGE", "SHIB", "PEPE", "BONK", "FLOKI", "WIF"}: return "CRYPTO_MEME_LOW_LIQUIDITY"
    if base in {"SOL", "AVAX", "BNB", "XRP", "ADA", "LINK", "DOT", "MATIC", "ARB", "OP"}: return "CRYPTO_HIGH_BETA_ALT"
    if base in {"USDT", "USDC", "DAI", "USD", "EUR"}: return "STABLE_FIAT"
    return "UNKNOWN_CONSERVATIVE"


def snapshot_from_state(*, mode: str, symbol: str, side: str = "LONG", candidate_notional: float | None = None, equity: float | None = None, available_balance: float | None = None, open_positions: Mapping[str, Mapping[str, Any] | float] | None = None, config: Mapping[str, Any] | Any | None = None, now: float | None = None, cooldown_until: Mapping[str, float] | None = None, daily_realized_pnl: float | None = None, trades_today_symbol: int | None = None, trades_today_global: int | None = None, consecutive_loss_count: int | None = None, symbol_consecutive_loss_count: int | None = None, rolling_peak_equity: float | None = None, rolling_drawdown_pct: float | None = None, risk_state_complete: bool | None = None, risk_state_source: str | None = None, risk_state_missing_fields: list[str] | None = None, correlation_overrides: Mapping[str, str] | None = None) -> PortfolioRiskSnapshot:
    cfgget = (lambda k, d=None: getattr(config, k, d)) if config is not None and not isinstance(config, Mapping) else (lambda k, d=None: (config or {}).get(k, d))
    positions = open_positions or {}
    total = 0.0; sym = 0.0; long = 0.0; short = 0.0; group_exp = 0.0; group_count = 0
    symbol_exposures: dict[str, float] = {}
    group_exposures: dict[str, float] = {}
    group_counts: dict[str, int] = {}
    group = correlation_group_for_symbol(symbol, correlation_overrides)
    candidate_side = str(side or "LONG").upper()
    for psym, pdata in positions.items():
        if isinstance(pdata, Mapping):
            notional = _num(pdata.get("notional") or pdata.get("notional_usdt"), None)
            pside = str(pdata.get("side", "LONG")).upper()
        else:
            notional = _num(pdata, None); pside = "LONG"
        if notional is None: continue
        normalized_symbol = str(psym).upper()
        position_group = correlation_group_for_symbol(psym, correlation_overrides)
        symbol_exposures[normalized_symbol] = symbol_exposures.get(normalized_symbol, 0.0) + abs(notional)
        correlation_sides = (pside,) if pside in {"LONG", "SHORT"} else ("LONG", "SHORT")
        for correlation_side in correlation_sides:
            correlation_key = f"{position_group}:{correlation_side}"
            group_exposures[correlation_key] = group_exposures.get(correlation_key, 0.0) + abs(notional)
            group_counts[correlation_key] = group_counts.get(correlation_key, 0) + 1
        total += abs(notional)
        if str(psym).upper() == str(symbol).upper(): sym += abs(notional)
        if pside == "SHORT": short += abs(notional)
        else: long += abs(notional)
        same_direction = pside == candidate_side or pside not in {"LONG", "SHORT"}
        if correlation_group_for_symbol(psym, correlation_overrides) == group and same_direction:
            group_exp += abs(notional); group_count += 1
    ts = now_iso()
    cooldown_remaining = None
    if cooldown_until and now is not None:
        cooldown_remaining = max(0.0, float(cooldown_until.get(symbol, 0.0)) - float(now))
    daily_loss_pct = None if equity in (None, 0) or daily_realized_pnl is None else max(0.0, -float(daily_realized_pnl) / float(equity))
    gross = long + short
    net = long - short
    global_loss_limit = cfgget("global_loss_streak_limit", cfgget("max_consecutive_losses", 999999))
    symbol_loss_limit = cfgget("symbol_loss_streak_limit", 999999)
    global_loss_active = (
        None if consecutive_loss_count is None or global_loss_limit is None
        else int(consecutive_loss_count) >= int(global_loss_limit)
    )
    symbol_loss_active = (
        None if symbol_consecutive_loss_count is None or symbol_loss_limit is None
        else int(symbol_consecutive_loss_count) >= int(symbol_loss_limit)
    )
    return PortfolioRiskSnapshot(mode=mode, timestamp=ts, equity=equity, available_balance=available_balance, open_position_count=len(positions), max_open_positions=cfgget("max_open_positions", cfgget("max_concurrent_positions")), concurrent_position_count=len(positions), max_concurrent_positions=cfgget("max_concurrent_positions"), total_notional_exposure=total, max_notional_exposure=cfgget("max_notional_exposure"), symbol_notional_exposure=sym, max_symbol_notional=cfgget("max_symbol_notional"), side_exposure_long=long, side_exposure_short=short, net_exposure=net, gross_exposure=gross, leverage_estimate=None if equity in (None, 0) else gross / float(equity), symbol_cooldown_remaining_sec=cooldown_remaining, trades_today_symbol=trades_today_symbol, trades_today_global=trades_today_global, daily_realized_pnl=daily_realized_pnl, daily_loss_pct=daily_loss_pct, max_daily_loss_pct=cfgget("max_daily_loss_pct"), rolling_peak_equity=rolling_peak_equity, rolling_drawdown_pct=rolling_drawdown_pct, max_rolling_drawdown_pct=cfgget("max_rolling_drawdown_pct"), consecutive_loss_count=consecutive_loss_count, symbol_consecutive_loss_count=symbol_consecutive_loss_count, loss_cluster_active=global_loss_active, symbol_loss_cluster_active=symbol_loss_active, risk_state_complete=risk_state_complete, risk_state_source=risk_state_source, risk_state_missing_fields=list(risk_state_missing_fields or []), correlation_group=group, correlation_group_exposure=group_exp, max_correlation_group_exposure=cfgget("max_correlation_group_exposure"), correlated_position_count=group_count, max_correlated_positions=cfgget("max_correlated_positions"), diagnostics_json=json.dumps({"candidate_notional": candidate_notional, "candidate_side": candidate_side, "correlation_direction": candidate_side, "risk_state_source": risk_state_source, "risk_state_complete": risk_state_complete, "risk_state_missing_fields": list(risk_state_missing_fields or [])}, sort_keys=True), symbol_notional_exposures=symbol_exposures, correlation_group_exposures=group_exposures, correlated_position_counts=group_counts)


def evaluate_portfolio_risk(candidate: Mapping[str, Any] | Any, portfolio_snapshot: PortfolioRiskSnapshot, config: Mapping[str, Any] | Any | None = None, mode: str = "PAPER") -> PortfolioRiskDecision:
    cfgget = (lambda k, d=None: getattr(config, k, d)) if config is not None and not isinstance(config, Mapping) else (lambda k, d=None: (config or {}).get(k, d))
    cand = candidate if isinstance(candidate, Mapping) else getattr(candidate, "__dict__", {})
    qty = _num(cand.get("quantity"), None)
    price = _num(cand.get("entry") or cand.get("entry_price") or cand.get("price"), None)
    notional = _num(cand.get("notional") or cand.get("notional_usdt"), None)
    if notional is None and qty is not None and price is not None: notional = abs(qty * price)
    if notional is None: notional = _num(cfgget("default_candidate_notional"), None)
    diagnostic_fail_open = bool(cfgget("portfolio_risk_diagnostic_fail_open", False))
    reject_unknown = bool(cfgget("reject_unknown_portfolio_risk", True)) and not diagnostic_fail_open
    diagnostics = {"snapshot": portfolio_snapshot.to_dict(), "candidate_notional": notional, "portfolio_risk_diagnostic_fail_open": diagnostic_fail_open}
    flags: list[str] = []
    def fail(reason: str) -> PortfolioRiskDecision:
        return PortfolioRiskDecision(False, reason, flags + [reason], reason, 0.0, 0.0, diagnostics)
    if reject_unknown and portfolio_snapshot.risk_state_complete is False:
        return fail("UNKNOWN_PORTFOLIO_RISK")
    required = [portfolio_snapshot.equity, portfolio_snapshot.open_position_count, portfolio_snapshot.total_notional_exposure, portfolio_snapshot.symbol_notional_exposure]
    if reject_unknown and any(v is None for v in required): return fail("UNKNOWN_PORTFOLIO_RISK")
    if not diagnostic_fail_open and (portfolio_snapshot.equity is None or portfolio_snapshot.equity <= 0): return fail("INVALID_EQUITY")
    if notional is None or notional <= 0: return fail("INVALID_POSITION_SIZE")
    checks = [
        (portfolio_snapshot.open_position_count, portfolio_snapshot.max_open_positions, 1, "MAX_OPEN_POSITIONS"),
        (portfolio_snapshot.concurrent_position_count, portfolio_snapshot.max_concurrent_positions, 1, "MAX_CONCURRENT_POSITIONS"),
        (portfolio_snapshot.total_notional_exposure, portfolio_snapshot.max_notional_exposure, notional, "MAX_NOTIONAL_EXPOSURE"),
        (portfolio_snapshot.symbol_notional_exposure, portfolio_snapshot.max_symbol_notional, notional, "MAX_SYMBOL_NOTIONAL_EXPOSURE"),
        (portfolio_snapshot.correlation_group_exposure, portfolio_snapshot.max_correlation_group_exposure, notional, "CORRELATION_OVEREXPOSURE"),
        (portfolio_snapshot.correlated_position_count, portfolio_snapshot.max_correlated_positions, 1, "CORRELATION_OVEREXPOSURE"),
    ]
    for current, limit, inc, reason in checks:
        if limit is not None and current is not None and float(current) + float(inc) > float(limit): return fail(reason)
    if portfolio_snapshot.daily_loss_pct is not None and portfolio_snapshot.max_daily_loss_pct is not None and portfolio_snapshot.daily_loss_pct >= portfolio_snapshot.max_daily_loss_pct: return fail("MAX_DAILY_LOSS")
    if portfolio_snapshot.rolling_drawdown_pct is not None and portfolio_snapshot.max_rolling_drawdown_pct is not None and portfolio_snapshot.rolling_drawdown_pct >= portfolio_snapshot.max_rolling_drawdown_pct: return fail("MAX_ROLLING_DRAWDOWN")
    if (portfolio_snapshot.symbol_cooldown_remaining_sec or 0) > 0: return fail("SYMBOL_COOLDOWN_ACTIVE")
    max_symbol_trades = cfgget(
        "max_daily_symbol_trades",
        cfgget("max_symbol_trades_per_day", cfgget("max_trades_symbol_per_day")),
    )
    if max_symbol_trades is not None and portfolio_snapshot.trades_today_symbol is not None and int(portfolio_snapshot.trades_today_symbol) >= int(max_symbol_trades): return fail("DAILY_SYMBOL_TRADE_LIMIT")
    max_global_trades = cfgget(
        "max_daily_global_trades",
        cfgget("max_global_trades_per_day", cfgget("max_trades_global_per_day")),
    )
    if max_global_trades is not None and portfolio_snapshot.trades_today_global is not None and int(portfolio_snapshot.trades_today_global) >= int(max_global_trades): return fail("DAILY_GLOBAL_TRADE_LIMIT")
    side = str(cand.get("side") or "LONG").upper()
    same_side_limit = cfgget("max_same_side_exposure")
    if same_side_limit is not None:
        current_side = portfolio_snapshot.side_exposure_short if side == "SHORT" else portfolio_snapshot.side_exposure_long
        if current_side is not None and float(current_side) + float(notional) > float(same_side_limit): return fail("SAME_SIDE_OVEREXPOSURE")
    net_limit = cfgget("max_net_exposure")
    if net_limit is not None and portfolio_snapshot.net_exposure is not None:
        signed = -float(notional) if side == "SHORT" else float(notional)
        if abs(float(portfolio_snapshot.net_exposure) + signed) > float(net_limit): return fail("NET_EXPOSURE_TOO_HIGH")
    if portfolio_snapshot.symbol_loss_cluster_active or portfolio_snapshot.loss_cluster_active:
        return fail("LOSS_CLUSTER_ACTIVE")
    return PortfolioRiskDecision(True, "", [], "ACCEPTED", 1.0, notional, diagnostics)


_REDUCIBLE_CAPACITY_REASONS = frozenset({
    "MAX_NOTIONAL_EXPOSURE",
    "MAX_SYMBOL_NOTIONAL_EXPOSURE",
    "CORRELATION_OVEREXPOSURE",
    "SAME_SIDE_OVEREXPOSURE",
    "NET_EXPOSURE_TOO_HIGH",
})
_ALLOCATION_CONFIG_KEYS = (
    "max_open_positions",
    "max_concurrent_positions",
    "max_notional_exposure",
    "max_symbol_notional",
    "max_same_side_exposure",
    "max_net_exposure",
    "max_correlation_group_exposure",
    "max_correlated_positions",
    "max_daily_loss_pct",
    "max_rolling_drawdown_pct",
    "max_daily_symbol_trades",
    "max_symbol_trades_per_day",
    "max_trades_symbol_per_day",
    "max_daily_global_trades",
    "max_global_trades_per_day",
    "max_trades_global_per_day",
    "reject_unknown_portfolio_risk",
    "portfolio_risk_diagnostic_fail_open",
)


def _allocation_json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _allocation_json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset)):
        return [_allocation_json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_allocation_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "INVALID_NON_FINITE"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _allocation_hash(value: Any) -> str:
    encoded = json.dumps(
        _allocation_json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _config_get(config: Mapping[str, Any] | Any | None, key: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default) if config is not None else default


def _candidate_mapping(candidate: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(candidate, Mapping):
        return dict(candidate)
    try:
        return asdict(candidate)
    except (TypeError, ValueError):
        return dict(getattr(candidate, "__dict__", {}))


def _first_number(candidate: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in candidate and candidate.get(key) is not None:
            return _num(candidate.get(key), None)
    return None


def _normalized_allocation_candidate(candidate: Mapping[str, Any] | Any) -> dict[str, Any]:
    raw = _candidate_mapping(candidate)
    symbol = str(raw.get("symbol") or "").strip().upper()
    side = str(raw.get("side") or "LONG").strip().upper()
    requested_quantity = _first_number(raw, "requested_quantity", "quantity", "qty")
    entry = _first_number(raw, "entry", "entry_price", "price")
    requested_notional = _first_number(
        raw,
        "requested_notional",
        "notional",
        "notional_usdt",
        "order_notional",
        "effective_notional",
    )
    if requested_notional is None and requested_quantity is not None and entry is not None:
        requested_notional = abs(requested_quantity * entry)
    candidate_id = str(
        raw.get("candidate_id")
        or raw.get("signal_id")
        or f"{symbol}:{side}"
    )
    correlation_group = str(
        raw.get("correlation_group") or correlation_group_for_symbol(symbol)
    )
    preference_components = {
        "expected_net_r": _first_number(
            raw, "expected_net_r", "expected_net_R", "expectancy_after_costs"
        ),
        "effective_rr": _first_number(raw, "effective_rr", "expected_effective_r"),
        "score": _first_number(raw, "score", "decision_score", "symbol_score"),
        "execution_quality": _first_number(
            raw, "execution_quality", "liquidity_score"
        ),
    }
    return {
        "raw": raw,
        "candidate_inputs": _allocation_json_safe(raw),
        "candidate_id": candidate_id,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "requested_quantity": requested_quantity,
        "requested_notional": requested_notional,
        "requested_risk": _first_number(
            raw, "requested_risk", "risk_at_stop_usdt", "risk_at_stop"
        ),
        "correlation_group": correlation_group,
        "preference_components": preference_components,
    }


def _preference_sort_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    components = candidate["preference_components"]
    values = tuple(
        -float(components[key]) if components[key] is not None else math.inf
        for key in ("expected_net_r", "effective_rr", "score", "execution_quality")
    )
    return (*values, candidate["candidate_id"], candidate["symbol"], candidate["side"])


def _candidate_snapshot(
    base: PortfolioRiskSnapshot,
    candidate: Mapping[str, Any],
    *,
    total_exposure: float,
    symbol_exposures: Mapping[str, float],
    side_long: float,
    side_short: float,
    net_exposure: float,
    allocated_positions: int,
    group_exposures: Mapping[str, float],
    group_counts: Mapping[str, int],
) -> PortfolioRiskSnapshot:
    raw = candidate["raw"]
    symbol = candidate["symbol"]
    side = candidate["side"]
    group = candidate["correlation_group"]
    group_key = f"{group}:{side}"
    overrides: dict[str, Any] = {}
    for field_name in (
        "symbol_cooldown_remaining_sec",
        "trades_today_symbol",
        "symbol_consecutive_loss_count",
        "symbol_loss_cluster_active",
        "risk_state_complete",
        "risk_state_source",
    ):
        if raw.get(field_name) is not None:
            overrides[field_name] = raw[field_name]
    return replace(
        base,
        mode=str(base.mode),
        open_position_count=(
            None if base.open_position_count is None
            else int(base.open_position_count) + allocated_positions
        ),
        concurrent_position_count=(
            None if base.concurrent_position_count is None
            else int(base.concurrent_position_count) + allocated_positions
        ),
        total_notional_exposure=total_exposure,
        gross_exposure=side_long + side_short,
        symbol_notional_exposure=float(symbol_exposures.get(symbol, 0.0)),
        side_exposure_long=side_long,
        side_exposure_short=side_short,
        net_exposure=net_exposure,
        correlation_group=group,
        correlation_group_exposure=float(group_exposures.get(group_key, 0.0)),
        correlated_position_count=int(group_counts.get(group_key, 0)),
        **overrides,
    )


def allocate_portfolio_candidates(
    candidates: Sequence[Mapping[str, Any] | Any],
    portfolio_snapshot: PortfolioRiskSnapshot,
    config: Mapping[str, Any] | Any | None,
    *,
    mode: str,
) -> PortfolioAllocationDecision:
    """Allocate a simultaneous candidate set against one immutable snapshot.

    The greedy pass is deterministic because preference and identity tie-breaks
    are explicit.  It only simulates already allocated exposure; the supplied
    snapshot and real portfolio are never mutated.
    """
    normalized = [_normalized_allocation_candidate(item) for item in candidates]
    identities = [item["candidate_id"] for item in normalized]
    if len(set(identities)) != len(identities):
        raise ValueError("DUPLICATE_ALLOCATION_CANDIDATE_ID")
    ordered = sorted(normalized, key=_preference_sort_key)

    candidate_identity_payload = [
        {
            "candidate_id": item["candidate_id"],
            "candidate_inputs": item["candidate_inputs"],
            "normalized": {
                key: item[key]
                for key in (
                    "symbol", "side", "entry", "requested_quantity",
                    "requested_notional", "requested_risk", "correlation_group",
                    "preference_components",
                )
            },
        }
        for item in sorted(normalized, key=lambda row: row["candidate_id"])
    ]
    candidate_set_hash = _allocation_hash(candidate_identity_payload)
    snapshot_payload = portfolio_snapshot.to_dict()
    portfolio_snapshot_hash = _allocation_hash(snapshot_payload)
    config_payload = {
        key: _config_get(config, key)
        for key in _ALLOCATION_CONFIG_KEYS
        if _config_get(config, key) is not None
    }
    config_hash = _allocation_hash(config_payload)
    universe_hash = str(
        _config_get(config, "universe_hash")
        or _allocation_hash(sorted(item["symbol"] for item in normalized))
    )

    total_exposure = float(portfolio_snapshot.total_notional_exposure or 0.0)
    side_long = float(portfolio_snapshot.side_exposure_long or 0.0)
    side_short = float(portfolio_snapshot.side_exposure_short or 0.0)
    net_exposure = float(portfolio_snapshot.net_exposure or 0.0)
    symbol_exposures = {
        str(key).upper(): float(value)
        for key, value in portfolio_snapshot.symbol_notional_exposures.items()
    }
    group_exposures = {
        str(key): float(value)
        for key, value in portfolio_snapshot.correlation_group_exposures.items()
    }
    group_counts = {
        str(key): int(value)
        for key, value in portfolio_snapshot.correlated_position_counts.items()
    }
    if not symbol_exposures and portfolio_snapshot.symbol_notional_exposure:
        # Legacy snapshots do not identify which symbol the scalar belongs to.
        # Applying it conservatively to every candidate cannot create capacity.
        for item in normalized:
            symbol_exposures[item["symbol"]] = float(
                portfolio_snapshot.symbol_notional_exposure
            )
    if not group_exposures and portfolio_snapshot.correlation_group:
        for side in {item["side"] for item in normalized}:
            key = f"{portfolio_snapshot.correlation_group}:{side}"
            group_exposures[key] = float(
                portfolio_snapshot.correlation_group_exposure or 0.0
            )
            group_counts[key] = int(portfolio_snapshot.correlated_position_count or 0)

    decisions: list[CandidateAllocationDecision] = []
    allocated_positions = 0
    for item in ordered:
        raw = item["raw"]
        requested = item["requested_notional"]
        requested_risk = item["requested_risk"]
        requested_quantity = item["requested_quantity"]
        group_key = f"{item['correlation_group']}:{item['side']}"
        simulated = _candidate_snapshot(
            portfolio_snapshot,
            item,
            total_exposure=total_exposure,
            symbol_exposures=symbol_exposures,
            side_long=side_long,
            side_short=side_short,
            net_exposure=net_exposure,
            allocated_positions=allocated_positions,
            group_exposures=group_exposures,
            group_counts=group_counts,
        )
        hard = evaluate_portfolio_risk(
            {
                "symbol": item["symbol"],
                "side": item["side"],
                "entry": item["entry"],
                "quantity": requested_quantity,
                "notional": requested,
            },
            simulated,
            config,
            mode=mode,
        )
        explicit_hard_reason = str(raw.get("hard_gate_reason") or "")
        explicit_hard_failure = raw.get("hard_gate_accepted") is False or bool(
            explicit_hard_reason
        )
        non_capacity_failure = (
            explicit_hard_failure
            or (
                not hard.accepted
                and hard.reject_reason not in _REDUCIBLE_CAPACITY_REASONS
            )
        )
        reasons: list[str] = []
        allocated = 0.0
        if non_capacity_failure:
            reasons.append(explicit_hard_reason or hard.reject_reason or "HARD_GATE_REJECTED")
        elif requested is None or requested <= 0 or not math.isfinite(requested):
            reasons.append("INVALID_POSITION_SIZE")
        else:
            capacity = float(requested)
            open_limit = simulated.max_open_positions
            concurrent_limit = simulated.max_concurrent_positions
            if open_limit is not None and simulated.open_position_count is not None:
                if int(simulated.open_position_count) >= int(open_limit):
                    capacity = 0.0
                    reasons.append("MAX_OPEN_POSITIONS")
            if concurrent_limit is not None and simulated.concurrent_position_count is not None:
                if int(simulated.concurrent_position_count) >= int(concurrent_limit):
                    capacity = 0.0
                    reasons.append("MAX_CONCURRENT_POSITIONS")
            if simulated.max_correlated_positions is not None:
                if int(group_counts.get(group_key, 0)) >= int(simulated.max_correlated_positions):
                    capacity = 0.0
                    reasons.append("CORRELATION_OVEREXPOSURE")

            caps = (
                (
                    simulated.max_notional_exposure,
                    total_exposure,
                    "MAX_NOTIONAL_EXPOSURE",
                ),
                (
                    simulated.max_symbol_notional,
                    float(symbol_exposures.get(item["symbol"], 0.0)),
                    "MAX_SYMBOL_NOTIONAL_EXPOSURE",
                ),
                (
                    simulated.max_correlation_group_exposure,
                    float(group_exposures.get(group_key, 0.0)),
                    "CORRELATION_OVEREXPOSURE",
                ),
            )
            for limit, current, reason in caps:
                if limit is not None:
                    remaining = max(0.0, float(limit) - float(current))
                    if remaining < capacity:
                        capacity = remaining
                        reasons.append(reason)

            same_side_limit = _config_get(config, "max_same_side_exposure")
            if same_side_limit is not None:
                current_side = side_short if item["side"] == "SHORT" else side_long
                remaining = max(0.0, float(same_side_limit) - current_side)
                if remaining < capacity:
                    capacity = remaining
                    reasons.append("SAME_SIDE_OVEREXPOSURE")
            net_limit = _config_get(config, "max_net_exposure")
            if net_limit is not None:
                remaining = (
                    float(net_limit) + net_exposure
                    if item["side"] == "SHORT"
                    else float(net_limit) - net_exposure
                )
                remaining = max(0.0, remaining)
                if remaining < capacity:
                    capacity = remaining
                    reasons.append("NET_EXPOSURE_TOO_HIGH")

            allocated = max(0.0, min(float(requested), capacity))
            if allocated > 0:
                final_snapshot = _candidate_snapshot(
                    portfolio_snapshot,
                    item,
                    total_exposure=total_exposure,
                    symbol_exposures=symbol_exposures,
                    side_long=side_long,
                    side_short=side_short,
                    net_exposure=net_exposure,
                    allocated_positions=allocated_positions,
                    group_exposures=group_exposures,
                    group_counts=group_counts,
                )
                final_gate = evaluate_portfolio_risk(
                    {
                        "symbol": item["symbol"],
                        "side": item["side"],
                        "entry": item["entry"],
                        "notional": allocated,
                    },
                    final_snapshot,
                    config,
                    mode=mode,
                )
                if not final_gate.accepted:
                    allocated = 0.0
                    reasons.append(final_gate.reject_reason or "HARD_GATE_REJECTED")

        if allocated <= 0:
            action = "REJECT"
            allocated = 0.0
            if not reasons:
                reasons.append("NO_PORTFOLIO_CAPACITY")
        elif allocated + 1e-12 < float(requested):
            action = "REDUCE_SIZE"
        else:
            action = "APPROVE"
            reasons = []

        scale = 0.0 if not requested or requested <= 0 else allocated / float(requested)
        allocated_quantity = (
            None if requested_quantity is None else requested_quantity * scale
        )
        allocated_risk = None if requested_risk is None else requested_risk * scale
        decision = CandidateAllocationDecision(
            candidate_id=item["candidate_id"],
            symbol=item["symbol"],
            side=item["side"],
            action=action,
            reason_codes=tuple(dict.fromkeys(reasons)),
            requested_notional=requested,
            allocated_notional=allocated,
            requested_risk=requested_risk,
            allocated_risk=allocated_risk,
            requested_quantity=requested_quantity,
            allocated_quantity=allocated_quantity,
            correlation_group=item["correlation_group"],
            correlation_contribution=allocated,
            concentration_contribution=(
                0.0
                if portfolio_snapshot.equity in (None, 0)
                else allocated / float(portfolio_snapshot.equity)
            ),
            candidate_inputs=dict(item["candidate_inputs"]),
            preference_components=dict(item["preference_components"]),
            hard_gate_accepted=bool(hard.accepted),
            hard_gate_reason=str(explicit_hard_reason or hard.reject_reason or ""),
        )
        decisions.append(decision)
        if allocated > 0:
            allocated_positions += 1
            total_exposure += allocated
            symbol_exposures[item["symbol"]] = (
                float(symbol_exposures.get(item["symbol"], 0.0)) + allocated
            )
            group_exposures[group_key] = float(group_exposures.get(group_key, 0.0)) + allocated
            group_counts[group_key] = int(group_counts.get(group_key, 0)) + 1
            if item["side"] == "SHORT":
                side_short += allocated
                net_exposure -= allocated
            else:
                side_long += allocated
                net_exposure += allocated

    if not any(item.allocated_notional > 0 for item in decisions):
        overall_action = "HOLD_CASH"
    elif all(item.action == "APPROVE" for item in decisions):
        overall_action = "APPROVE"
    else:
        overall_action = "REDUCE_SIZE"

    identity_payload = {
        "mode": str(mode).upper(),
        "candidate_set_hash": candidate_set_hash,
        "portfolio_snapshot_hash": portfolio_snapshot_hash,
        "config_hash": config_hash,
        "universe_hash": universe_hash,
        "allocator_version": PORTFOLIO_ALLOCATOR_VERSION,
    }
    cycle_id = str(
        _config_get(config, "allocation_cycle_id")
        or "palloc_" + _allocation_hash(identity_payload)[:24]
    )
    timestamp = str(portfolio_snapshot.timestamp)
    core = {
        **identity_payload,
        "allocation_cycle_id": cycle_id,
        "timestamp": timestamp,
        "action": overall_action,
        "git_sha": str(
            _config_get(config, "git_sha", _config_get(config, "git_commit", "UNKNOWN_GIT_COMMIT"))
        ),
        "release_id": str(_config_get(config, "release_id", "UNKNOWN_RELEASE")),
        "runtime_identity": str(
            _config_get(config, "runtime_identity", _config_get(config, "runtime_instance_id", "UNKNOWN_RUNTIME"))
        ),
        "schema_version": PORTFOLIO_ALLOCATION_SCHEMA_VERSION,
        "candidates": [item.to_dict() for item in decisions],
    }
    return PortfolioAllocationDecision(
        allocation_cycle_id=cycle_id,
        timestamp=timestamp,
        mode=str(mode).upper(),
        action=overall_action,
        candidate_set_hash=candidate_set_hash,
        portfolio_snapshot_hash=portfolio_snapshot_hash,
        config_hash=config_hash,
        universe_hash=universe_hash,
        evidence_hash=_allocation_hash(core),
        git_sha=core["git_sha"],
        release_id=core["release_id"],
        runtime_identity=core["runtime_identity"],
        allocator_version=PORTFOLIO_ALLOCATOR_VERSION,
        schema_version=PORTFOLIO_ALLOCATION_SCHEMA_VERSION,
        candidates=tuple(decisions),
    )


def apply_candidate_allocation(
    candidate: Mapping[str, Any],
    allocation: CandidateAllocationDecision,
) -> dict[str, Any]:
    """Project an allocator decision into the values consumed by sizing/order code."""
    projected = dict(candidate)
    projected["portfolio_allocation"] = allocation.to_dict()
    projected["allocation_action"] = allocation.action
    if allocation.action in {"APPROVE", "REDUCE_SIZE"}:
        current_notional = _first_number(
            projected,
            "notional",
            "notional_usdt",
            "order_notional",
            "effective_notional",
        )
        downstream_notional = (
            allocation.allocated_notional
            if current_notional is None
            else min(current_notional, allocation.allocated_notional)
        )
        downstream_scale = (
            0.0
            if allocation.allocated_notional <= 0
            else downstream_notional / allocation.allocated_notional
        )
        projected["allocated_notional"] = downstream_notional
        projected["allocated_risk"] = (
            None
            if allocation.allocated_risk is None
            else allocation.allocated_risk * downstream_scale
        )
        for key in ("notional", "notional_usdt", "order_notional", "effective_notional"):
            if key in projected or key == "notional":
                projected[key] = downstream_notional
        if allocation.allocated_quantity is not None:
            downstream_quantity = allocation.allocated_quantity * downstream_scale
            projected["quantity"] = downstream_quantity
            projected["qty"] = downstream_quantity
            if "effective_quantity" in projected:
                projected["effective_quantity"] = downstream_quantity
    else:
        projected["allocated_notional"] = 0.0
        projected["allocated_risk"] = 0.0
    return projected


@dataclass(slots=True)
class BacktestPosition:
    position_id: str
    symbol: str
    side: str
    notional: float
    entry_price: float
    opened_ts: int
    correlation_group: str


@dataclass(slots=True)
class BacktestPortfolioState:
    initial_equity: float
    current_equity: float | None = None
    peak_equity: float | None = None
    open_positions: dict[str, BacktestPosition] = field(default_factory=dict)
    pending_entries: dict[str, float] = field(default_factory=dict)
    daily_realized_pnl: dict[str, float] = field(default_factory=dict)
    symbol_daily_trade_counts: dict[str, int] = field(default_factory=dict)
    global_daily_trade_counts: dict[str, int] = field(default_factory=dict)
    cooldown_until: dict[str, float] = field(default_factory=dict)
    consecutive_loss_count: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.current_equity is None:
            self.current_equity = float(self.initial_equity)
        if self.peak_equity is None:
            self.peak_equity = float(self.current_equity)

    def _day_key(self, ts: int | float | str | None) -> str:
        try:
            raw = float(ts or 0.0)
        except (TypeError, ValueError):
            raw = 0.0
        seconds = raw / 1000.0 if raw > 10_000_000_000 else raw
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d") if seconds > 0 else "UNKNOWN_DAY"

    def notional_for(self, *, entry: Any, balance: Any | None = None, risk_pct: Any | None = None, risk_scale: Any = 1.0, notional: Any | None = None) -> float | None:
        explicit = _num(notional, None)
        base_notional = explicit
        if base_notional is None:
            equity = _num(balance, self.current_equity)
            pct = _num(risk_pct, None)
            if equity is None or pct is None:
                return None
            base_notional = equity * (pct / 100.0)
        projection = scale_candidate_exposure(
            original_notional=base_notional,
            risk_scale=risk_scale,
            require_scale=False,
        )
        effective = projection.get("effective_notional")
        if projection.get("status") != "COMPLETE" or effective is None:
            return None
        return float(effective) if float(effective) > 0.0 else None

    def snapshot(self, *, mode: str, symbol: str, side: str = "LONG", config: Mapping[str, Any] | Any | None = None, timestamp: int | float | None = None, candidate_notional: float | None = None) -> PortfolioRiskSnapshot:
        day = self._day_key(timestamp)
        symbol_key = f"{symbol}:{day}"
        total_daily = self.global_daily_trade_counts.get(day)
        symbol_daily = self.symbol_daily_trade_counts.get(symbol_key)
        daily_pnl = self.daily_realized_pnl.get(day, 0.0)
        rolling_dd = None
        if self.peak_equity and self.current_equity is not None:
            rolling_dd = max(0.0, (self.peak_equity - self.current_equity) / self.peak_equity)
        open_map = {pid: {"symbol": p.symbol, "notional": p.notional, "side": p.side} for pid, p in self.open_positions.items()}
        snap = snapshot_from_state(mode=mode, symbol=symbol, side=side, candidate_notional=candidate_notional, equity=self.current_equity, available_balance=self.current_equity, open_positions={p.symbol: {"notional": p.notional, "side": p.side} for p in self.open_positions.values()}, config=config, now=float(timestamp or 0.0) / 1000.0 if timestamp else None, cooldown_until=self.cooldown_until, daily_realized_pnl=daily_pnl, trades_today_symbol=symbol_daily, trades_today_global=total_daily, consecutive_loss_count=self.consecutive_loss_count, rolling_drawdown_pct=rolling_dd)
        try:
            diag = json.loads(snap.diagnostics_json or "{}")
        except Exception:
            diag = {}
        diag.update({"open_positions": open_map, "pending_entries": dict(self.pending_entries), "accounting_source": "BacktestPortfolioState"})
        snap.diagnostics_json = json.dumps(diag, sort_keys=True)
        return snap

    def mark_pending(self, position_id: str, notional: float | None) -> None:
        if notional is not None and notional > 0:
            self.pending_entries[position_id] = float(notional)

    def open_position(self, *, position_id: str, symbol: str, side: str, notional: float | None, entry_price: float, timestamp: int) -> None:
        self.pending_entries.pop(position_id, None)
        if notional is None or notional <= 0:
            return
        self.open_positions[position_id] = BacktestPosition(position_id=position_id, symbol=symbol, side=str(side).upper(), notional=float(notional), entry_price=float(entry_price or 0.0), opened_ts=int(timestamp or 0), correlation_group=correlation_group_for_symbol(symbol))

    def close_position(self, *, position_id: str, symbol: str, timestamp: int, net_pnl_usdt: float, close_reason: str) -> None:
        self.pending_entries.pop(position_id, None)
        self.open_positions.pop(position_id, None)
        pnl = float(net_pnl_usdt or 0.0)
        self.current_equity = float(self.current_equity or 0.0) + pnl
        self.peak_equity = max(float(self.peak_equity or self.current_equity or 0.0), float(self.current_equity or 0.0))
        day = self._day_key(timestamp)
        self.daily_realized_pnl[day] = self.daily_realized_pnl.get(day, 0.0) + pnl
        if pnl < 0 or str(close_reason).upper() == "SL_HIT":
            self.consecutive_loss_count += 1
        elif pnl > 0 or str(close_reason).upper() == "TP_HIT":
            self.consecutive_loss_count = 0

    def record_trade_count(self, *, symbol: str, timestamp: int) -> None:
        day = self._day_key(timestamp)
        self.global_daily_trade_counts[day] = self.global_daily_trade_counts.get(day, 0) + 1
        key = f"{symbol}:{day}"
        self.symbol_daily_trade_counts[key] = self.symbol_daily_trade_counts.get(key, 0) + 1

    def cancel_pending(self, position_id: str) -> None:
        self.pending_entries.pop(position_id, None)


def _num(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value in (None, "", "UNKNOWN", "UNAVAILABLE"): return default
        return float(value)
    except (TypeError, ValueError):
        return default
