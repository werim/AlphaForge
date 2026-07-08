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
    diagnostic_fail_open = bool(cfgget("portfolio_risk_diagnostic_fail_open", False))
    reject_unknown = bool(cfgget("reject_unknown_portfolio_risk", True)) and not diagnostic_fail_open
    diagnostics = {"snapshot": portfolio_snapshot.to_dict(), "candidate_notional": notional, "portfolio_risk_diagnostic_fail_open": diagnostic_fail_open}
    flags: list[str] = []
    def fail(reason: str) -> PortfolioRiskDecision:
        return PortfolioRiskDecision(False, reason, flags + [reason], reason, 0.0, 0.0, diagnostics)
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
    max_symbol_trades = cfgget("max_daily_symbol_trades", cfgget("max_symbol_trades_per_day"))
    if max_symbol_trades is not None and portfolio_snapshot.trades_today_symbol is not None and int(portfolio_snapshot.trades_today_symbol) >= int(max_symbol_trades): return fail("DAILY_SYMBOL_TRADE_LIMIT")
    max_global_trades = cfgget("max_daily_global_trades", cfgget("max_global_trades_per_day"))
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
    if portfolio_snapshot.loss_cluster_active: return fail("LOSS_CLUSTER_ACTIVE")
    return PortfolioRiskDecision(True, "", [], "ACCEPTED", 1.0, notional, diagnostics)


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
        if explicit is not None and explicit > 0:
            return explicit * max(_num(risk_scale, 1.0) or 1.0, 0.0)
        equity = _num(balance, self.current_equity)
        pct = _num(risk_pct, None)
        if equity is None or pct is None:
            return None
        scaled = equity * (pct / 100.0) * max(_num(risk_scale, 1.0) or 1.0, 0.0)
        return scaled if scaled > 0 else None

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
