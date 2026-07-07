from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Mapping

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
    rolling_drawdown_pct: float | None = None
    max_rolling_drawdown_pct: float | None = None
    consecutive_loss_count: int | None = None
    loss_cluster_active: bool | None = None
    correlation_group: str | None = None
    correlation_group_exposure: float | None = None
    max_correlation_group_exposure: float | None = None
    correlated_position_count: int | None = None
    max_correlated_positions: int | None = None
    risk_flags: list[str] = field(default_factory=list)
    reject_reason: str = ""
    diagnostics_json: str = "{}"

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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def correlation_group_for_symbol(symbol: str, override: Mapping[str, str] | None = None) -> str:
    s = str(symbol or "").upper().replace("-", "")
    if override and s in {k.upper(): v for k, v in override.items()}:
        return {k.upper(): v for k, v in override.items()}[s]
    base = s
    for q in ("USDT", "USD", "USDC", "BUSD", "BTC", "ETH"):
        if base.endswith(q) and len(base) > len(q):
            base = base[:-len(q)]
            break
    if base in {"BTC", "WBTC"}: return "CRYPTO_MAJOR_BTC"
    if base in {"ETH", "STETH", "WETH"}: return "CRYPTO_MAJOR_ETH"
    if base in {"DOGE", "SHIB", "PEPE", "BONK", "FLOKI", "WIF"}: return "CRYPTO_MEME_LOW_LIQUIDITY"
    if base in {"SOL", "AVAX", "BNB", "XRP", "ADA", "LINK", "DOT", "MATIC", "ARB", "OP"}: return "CRYPTO_HIGH_BETA_ALT"
    if base in {"USDT", "USDC", "DAI", "USD", "EUR"}: return "STABLE_FIAT"
    return "UNKNOWN_CONSERVATIVE"


def snapshot_from_state(*, mode: str, symbol: str, side: str = "LONG", candidate_notional: float | None = None, equity: float | None = None, available_balance: float | None = None, open_positions: Mapping[str, Mapping[str, Any] | float] | None = None, config: Mapping[str, Any] | Any | None = None, now: float | None = None, cooldown_until: Mapping[str, float] | None = None, daily_realized_pnl: float | None = None, trades_today_symbol: int | None = None, trades_today_global: int | None = None, consecutive_loss_count: int | None = None, rolling_drawdown_pct: float | None = None, correlation_overrides: Mapping[str, str] | None = None) -> PortfolioRiskSnapshot:
    cfgget = (lambda k, d=None: getattr(config, k, d)) if config is not None and not isinstance(config, Mapping) else (lambda k, d=None: (config or {}).get(k, d))
    positions = open_positions or {}
    total = 0.0; sym = 0.0; long = 0.0; short = 0.0; group_exp = 0.0; group_count = 0
    group = correlation_group_for_symbol(symbol, correlation_overrides)
    for psym, pdata in positions.items():
        if isinstance(pdata, Mapping):
            notional = _num(pdata.get("notional") or pdata.get("notional_usdt"), None)
            pside = str(pdata.get("side", "LONG")).upper()
        else:
            notional = _num(pdata, None); pside = "LONG"
        if notional is None: continue
        total += abs(notional)
        if str(psym).upper() == str(symbol).upper(): sym += abs(notional)
        if pside == "SHORT": short += abs(notional)
        else: long += abs(notional)
        if correlation_group_for_symbol(psym, correlation_overrides) == group:
            group_exp += abs(notional); group_count += 1
    ts = now_iso()
    cooldown_remaining = None
    if cooldown_until and now is not None:
        cooldown_remaining = max(0.0, float(cooldown_until.get(symbol, 0.0)) - float(now))
    daily_loss_pct = None if equity in (None, 0) or daily_realized_pnl is None else max(0.0, -float(daily_realized_pnl) / float(equity))
    gross = long + short
    net = long - short
    return PortfolioRiskSnapshot(mode=mode, timestamp=ts, equity=equity, available_balance=available_balance, open_position_count=len(positions), max_open_positions=cfgget("max_open_positions", cfgget("max_concurrent_positions")), concurrent_position_count=len(positions), max_concurrent_positions=cfgget("max_concurrent_positions"), total_notional_exposure=total, max_notional_exposure=cfgget("max_notional_exposure"), symbol_notional_exposure=sym, max_symbol_notional=cfgget("max_symbol_notional"), side_exposure_long=long, side_exposure_short=short, net_exposure=net, gross_exposure=gross, leverage_estimate=None if equity in (None, 0) else gross / float(equity), symbol_cooldown_remaining_sec=cooldown_remaining, trades_today_symbol=trades_today_symbol, trades_today_global=trades_today_global, daily_realized_pnl=daily_realized_pnl, daily_loss_pct=daily_loss_pct, max_daily_loss_pct=cfgget("max_daily_loss_pct"), rolling_drawdown_pct=rolling_drawdown_pct, max_rolling_drawdown_pct=cfgget("max_rolling_drawdown_pct"), consecutive_loss_count=consecutive_loss_count, loss_cluster_active=(consecutive_loss_count or 0) >= int(cfgget("max_consecutive_losses", 999999) or 999999), correlation_group=group, correlation_group_exposure=group_exp, max_correlation_group_exposure=cfgget("max_correlation_group_exposure"), correlated_position_count=group_count, max_correlated_positions=cfgget("max_correlated_positions"), diagnostics_json=json.dumps({"candidate_notional": candidate_notional, "candidate_side": side}))


def evaluate_portfolio_risk(candidate: Mapping[str, Any] | Any, portfolio_snapshot: PortfolioRiskSnapshot, config: Mapping[str, Any] | Any | None = None, mode: str = "PAPER") -> PortfolioRiskDecision:
    cfgget = (lambda k, d=None: getattr(config, k, d)) if config is not None and not isinstance(config, Mapping) else (lambda k, d=None: (config or {}).get(k, d))
    cand = candidate if isinstance(candidate, Mapping) else getattr(candidate, "__dict__", {})
    qty = _num(cand.get("quantity"), None)
    price = _num(cand.get("entry") or cand.get("entry_price") or cand.get("price"), None)
    notional = _num(cand.get("notional") or cand.get("notional_usdt"), None)
    if notional is None and qty is not None and price is not None: notional = abs(qty * price)
    if notional is None: notional = _num(cfgget("default_candidate_notional"), None)
    reject_unknown = bool(cfgget("reject_unknown_portfolio_risk", True))
    diagnostics = {"snapshot": portfolio_snapshot.to_dict(), "candidate_notional": notional}
    flags: list[str] = []
    def fail(reason: str) -> PortfolioRiskDecision:
        return PortfolioRiskDecision(False, reason, flags + [reason], reason, 0.0, 0.0, diagnostics)
    required = [portfolio_snapshot.equity, portfolio_snapshot.open_position_count, portfolio_snapshot.total_notional_exposure, portfolio_snapshot.symbol_notional_exposure]
    if reject_unknown and any(v is None for v in required): return fail("UNKNOWN_PORTFOLIO_RISK")
    if portfolio_snapshot.equity is None or portfolio_snapshot.equity <= 0: return fail("INVALID_EQUITY")
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
    if portfolio_snapshot.loss_cluster_active: return fail("LOSS_CLUSTER_ACTIVE")
    return PortfolioRiskDecision(True, "", [], "ACCEPTED", 1.0, notional, diagnostics)


def _num(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value in (None, "", "UNKNOWN", "UNAVAILABLE"): return default
        return float(value)
    except (TypeError, ValueError):
        return default
