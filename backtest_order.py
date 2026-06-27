import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Mapping, Iterable
# Allow running this script directly from the repo root without requiring
# prior editable install.
ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
from alphaforge.execution import build_execution_context, build_execution_cost_model, normalize_pct_input
from alphaforge.config import load_config_from_env
from alphaforge.lifecycle_contract import normalize_lifecycle_event
from alphaforge.persistence import init_db, save_trade_lifecycle_event
from alphaforge.symbol_selector import select_symbol
from alphaforge.historical_market_data import (
    HistoricalCandle,
    HistoricalDataError,
    fetch_binance_klines_paginated,
    load_or_fetch_candles as load_or_fetch_historical_candles,
)
from sqlalchemy import text
from sqlalchemy.orm import Session
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid5, NAMESPACE_URL


def resolve_csv_fieldnames(rows: List[Mapping[str, Any]], preferred_fieldnames: List[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for name in preferred_fieldnames:
        if name not in seen:
            ordered.append(name)
            seen.add(name)

    extra_keys = sorted({key for row in rows for key in row.keys() if key not in seen})
    ordered.extend(extra_keys)
    return ordered


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
@dataclass
class CandidateOrder:
    timestamp: int
    symbol: str
    side: str
    entry: float
    sl: float
    tp: float
    rr: float
    setup_type: str
    setup_reason: str
    regime: str
    score: float
    order_type: str
    expectancy_bucket: str = "UNKNOWN"
    accepted_reason: str = "BASELINE"
    original_reject_reason: str = ""
    rescue_size_multiplier: float = 1.0
    rescue_effective_rr: float = 0.0
    rescue_decision_context: str = ""
@dataclass
class RescueConfig:
    enabled: bool = False
    modes: tuple[str, ...] = ("BACKTEST",)
    effective_rr_min: float = 1.90
    score_min: float = 9.0
    size_multiplier: float = 0.25
    max_trades_per_day: int = 1
    allowed_reasons: tuple[str, ...] = ("STOP_TOO_WIDE", "DAILY_SYMBOL_TRADE_LIMIT")
    allow_regime_mismatch: bool = False
    max_spread_pct: float = 0.0025
    max_slippage_pct: float = 0.0020
    allow_cooldown_bypass: bool = False
    max_concurrent_positions: int = 3

@dataclass
class RescueStats:
    candidate_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    reject_reasons: Dict[str, int] = field(default_factory=dict)

QUALITY_GATE_NAME = "SHORT_BREAKDOWN_BREAKOUT_NORMAL_STOP_GATE"

@dataclass
class QualityGateConfig:
    enabled: bool = False
    modes: tuple[str, ...] = ("BACKTEST",)
    size_multiplier: float = 0.25
    max_trades_per_day: int = 1
    min_effective_rr: float = 1.10
    min_score: Optional[float] = None
    allowed_gate_name: str = QUALITY_GATE_NAME
    max_spread_pct: float = 0.0025
    max_slippage_pct: float = 0.0020
    allowed_reasons: tuple[str, ...] = ("LOW_SCORE", "DAILY_SYMBOL_TRADE_LIMIT")

@dataclass
class RejectedShadowEvaluation:
    symbol: str
    timestamp: int
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    raw_rr: float
    effective_rr: float
    reject_reasons: str
    score: float
    regime: str
    spread_pct: Any
    liquidity_score: Any
    volatility_score: Any
    shadow_outcome: str
    effective_tp_hit: bool
    cost_penalty: float
    liquidity_ok: bool
    volatility_ok: bool
    low_score_gate_score: float = 0.0
    rescue_attempted: bool = False
    rescue_passed: bool = False
    rescued_stop_loss: float = 0.0
    rescued_effective_rr: float = 0.0
    rescued_size_multiplier: float = 0.0
    rescue_reject_reason: str = ""
    setup_type: str = "UNAVAILABLE"
    timeframe: str = "UNAVAILABLE"
    expected_slippage_pct: Any = "UNAVAILABLE"
    stop_distance_pct: Any = "UNAVAILABLE"
@dataclass
class ForwardWindowEvaluation:
    signal_id: str
    symbol: str
    decision: str
    lifecycle_state: str
    reject_reason: str
    setup_type: str = "UNKNOWN"
    score: float = 0.0
    rr: float = 0.0
    effective_rr: float = 0.0
    predicted_quality: float = 0.0
    forward_window_minutes: int = 0
    would_have_hit_tp: bool = False
    would_have_hit_sl: bool = False
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    max_forward_return: float = 0.0
    max_adverse_return: float = 0.0
    reject_correct: Optional[bool] = None
    reject_missed_winner: bool = False
    reject_saved_from_loss: bool = False
    forward_window_regime: str = "UNKNOWN"
    execution_quality_bucket: str = "UNKNOWN"


TERMINAL_FORWARD_CLOSE_REASONS = {"TP_HIT", "SL_HIT", "TIMEOUT", "EXPIRED", "CANCELED"}
@dataclass
class LifecycleRow:
    timestamp: int
    symbol: str
    side: str
    setup_type: str
    setup_reason: str
    regime: str
    score: float
    rr: float
    entry: float
    sl: float
    tp: float
    status_before: str
    status_after: str
    trigger_price: float = 0.0
    close_price: float = 0.0
    close_reason: str = ""
    net_pnl_pct: float = 0.0
    net_pnl_usdt: float = 0.0
    hold_minutes: float = 0.0
    reject_reason: str = ""
    cancel_reason: str = ""
    order_type: str = "LIMIT"
    expectancy_bucket: str = "UNKNOWN"
    event_flags: str = ""
    volume_24h_usdt: Any = "UNAVAILABLE_BACKTEST"
    spread_pct: Any = "UNAVAILABLE_BACKTEST"
    funding_rate_pct: Any = "UNAVAILABLE_BACKTEST"
    expected_slippage_pct: Any = "UNAVAILABLE_BACKTEST"
    volatility_regime: str = "UNAVAILABLE_BACKTEST"
    liquidity_score: Any = "UNAVAILABLE_BACKTEST"
    effective_rr: Optional[float] = None
    mfe: float = 0.0
    mae: float = 0.0
    would_tp_hit: bool = False
    would_sl_hit: bool = False
    would_trigger: bool = False
    signal_id: str = ""
    lifecycle_id: str = ""
    order_id: str = ""
    position_id: str = ""
    lifecycle_seq: int = 0
    shadow_outcome: str = ""
    cost_penalty: float = 0.0
    volatility_score: Any = "UNAVAILABLE_BACKTEST"
    liquidity_ok: Optional[bool] = None
    volatility_ok: Optional[bool] = None
    stop_too_wide_softened: bool = False
    original_reject_reason: str = ""
    reject_reason_softened: str = ""
    risk_scale: float = 1.0
    accepted_reason: str = "BASELINE"
    rescue_size_multiplier: float = 1.0
    rescue_effective_rr: float = 0.0
    rescue_decision_context: str = ""


def _is_pre_signal_symbol_reject(row: LifecycleRow, lifecycle_state: str | None = None) -> bool:
    state = (lifecycle_state or row.status_after or "").strip().upper()
    return state == "SYMBOL_REJECTED" and row.status_before.strip().upper() in {"", "NONE"} and row.event_flags == "SYMBOL_SELECTOR"


def _lifecycle_signal_id(row: LifecycleRow, lifecycle_state: str | None = None) -> str:
    if row.signal_id:
        return row.signal_id
    if _is_pre_signal_symbol_reject(row, lifecycle_state):
        return f"SYMBOL_SELECTOR:{row.symbol}:{row.timestamp}"
    return f"{row.symbol}:{row.timestamp}"

def _bucket_expectancy(expectancy: Optional[float]) -> str:
    if expectancy is None:
        return "UNKNOWN"
    if expectancy < 0.0:
        return "NEGATIVE"
    if expectancy < 0.05:
        return "LOW"
    if expectancy < 0.2:
        return "MEDIUM"
    return "HIGH"
def _execution_reject_flags(rr: float, market_ctx: Mapping[str, Any]) -> tuple[float, list[str], dict[str, float]]:
    model = build_execution_cost_model(market_ctx, include_missing_penalty=False)
    effective = round(max(float(rr) - model.total_penalty, 0.0), 6)
    liquidity_score = min(1.0, max(0.0, float(market_ctx.get("liquidity_score", 1.0) or 1.0)))
    flags: list[str] = []
    if model.slippage_penalty >= 0.20:
        flags.append("HIGH_SLIPPAGE")
    if model.spread_penalty >= 0.20:
        flags.append("HIGH_SPREAD")
    if liquidity_score < 0.3:
        flags.append("LOW_LIQUIDITY")
    if effective < 1.1:
        flags.append("LOW_EFFECTIVE_RR")
    breakdown = {
        "cost_penalty_total": model.total_penalty,
        "spread_penalty": model.spread_penalty,
        "slippage_penalty": model.slippage_penalty,
        "latency_penalty": model.latency_penalty,
        "liquidity_penalty": model.liquidity_penalty,
        "funding_penalty": model.funding_penalty,
    }
    return effective, flags, breakdown


def _rescue_config_from_args(args: Any, runtime_cfg: Any) -> RescueConfig:
    return RescueConfig(
        enabled=bool(getattr(args, "rescue_enabled", False)),
        modes=tuple(str(getattr(args, "rescue_modes", "BACKTEST") or "BACKTEST").upper().replace(",", " ").split()),
        effective_rr_min=float(getattr(args, "rescue_effective_rr_min", 1.90)),
        score_min=float(getattr(args, "rescue_score_min", 9.0)),
        size_multiplier=min(1.0, max(0.0, float(getattr(args, "rescue_size_multiplier", 0.25)))),
        max_trades_per_day=int(getattr(args, "max_rescue_trades_per_day", 1)),
        allowed_reasons=tuple(r.strip().upper() for r in str(getattr(args, "rescue_allowed_reasons", "STOP_TOO_WIDE,DAILY_SYMBOL_TRADE_LIMIT") or "").replace(",", " ").split() if r.strip()),
        allow_regime_mismatch=bool(getattr(args, "rescue_allow_regime_mismatch", False)),
        max_spread_pct=float(getattr(args, "rescue_max_spread_pct", 0.0025)),
        max_slippage_pct=float(getattr(args, "rescue_max_slippage_pct", 0.0020)),
        allow_cooldown_bypass=bool(getattr(args, "rescue_allow_cooldown_bypass", False)),
        max_concurrent_positions=int(getattr(getattr(runtime_cfg, "runtime", runtime_cfg), "max_concurrent_positions", 3)),
    )

def _quality_gate_config_from_args(args: Any) -> QualityGateConfig:
    min_score_raw = getattr(args, "quality_gate_min_score", None)
    min_score = None if min_score_raw in (None, "") else float(min_score_raw)
    return QualityGateConfig(
        enabled=bool(getattr(args, "quality_gate_enabled", False)),
        modes=tuple(str(getattr(args, "quality_gate_modes", "BACKTEST") or "BACKTEST").upper().replace(",", " ").split()),
        size_multiplier=min(1.0, max(0.0, float(getattr(args, "quality_gate_size_multiplier", 0.25)))),
        max_trades_per_day=int(getattr(args, "max_quality_gate_trades_per_day", 1)),
        min_effective_rr=float(getattr(args, "quality_gate_min_effective_rr", 1.10)),
        min_score=min_score,
        allowed_gate_name=str(getattr(args, "quality_gate_name", QUALITY_GATE_NAME) or QUALITY_GATE_NAME),
        max_spread_pct=float(getattr(args, "quality_gate_max_spread_pct", 0.0025)),
        max_slippage_pct=float(getattr(args, "quality_gate_max_slippage_pct", 0.0020)),
        allowed_reasons=tuple(r.strip().upper() for r in str(getattr(args, "quality_gate_allowed_reasons", "LOW_SCORE,DAILY_SYMBOL_TRADE_LIMIT") or "").replace(",", " ").split() if r.strip()),
    )

def _rescue_reject(stats: RescueStats, reason: str) -> tuple[bool, str]:
    stats.rejected_count += 1
    stats.reject_reasons[reason] = stats.reject_reasons.get(reason, 0) + 1
    return False, reason

def _rescue_acceptance_allowed(
    *, mode: str, reason: str, score: float, effective_rr: float, regime: str, mctx: Mapping[str, Any],
    cfg: RescueConfig, stats: RescueStats, recent_stats: Mapping[str, Any], open_rows: List[LifecycleRow], symbol: str,
) -> tuple[bool, str]:
    if not cfg.enabled:
        return False, "RESCUE_DISABLED"
    if str(mode).upper() not in cfg.modes or str(mode).upper() != "BACKTEST":
        return _rescue_reject(stats, "MODE_NOT_BACKTEST")
    reason = str(reason or "").upper()
    if reason not in set(cfg.allowed_reasons):
        return _rescue_reject(stats, "REASON_NOT_ALLOWED")
    stats.candidate_count += 1
    if score < cfg.score_min:
        return _rescue_reject(stats, "SCORE_TOO_LOW")
    if mctx.get("liquidity_ok") is False:
        return _rescue_reject(stats, "LIQUIDITY_NOT_OK")
    if mctx.get("volatility_ok") is False:
        return _rescue_reject(stats, "VOLATILITY_NOT_OK")
    spread = _safe_float(mctx.get("spread_pct"), -1.0)
    slip = _safe_float(mctx.get("expected_slippage_pct"), -1.0)
    if spread < 0.0 or spread > cfg.max_spread_pct:
        return _rescue_reject(stats, "SPREAD_TOO_HIGH")
    if slip < 0.0 or slip > cfg.max_slippage_pct:
        return _rescue_reject(stats, "SLIPPAGE_TOO_HIGH")
    if effective_rr < cfg.effective_rr_min:
        return _rescue_reject(stats, "EFFECTIVE_RR_TOO_LOW")
    if str(regime or mctx.get("regime", "")).upper() in {"PANIC", "NEWS_DRIVEN"}:
        return _rescue_reject(stats, "REGIME_BLOCKED")
    failed = {str(x).upper() for x in (mctx.get("all_failed_gates") or [])} if isinstance(mctx.get("all_failed_gates"), list) else set()
    if not cfg.allow_regime_mismatch and (reason == "REGIME_MISMATCH" or "REGIME_MISMATCH" in failed):
        return _rescue_reject(stats, "REGIME_MISMATCH")
    if stats.accepted_count >= cfg.max_trades_per_day:
        return _rescue_reject(stats, "DAILY_RESCUE_LIMIT")
    if len(open_rows) >= cfg.max_concurrent_positions:
        return _rescue_reject(stats, "MAX_CONCURRENT_POSITIONS")
    if not cfg.allow_cooldown_bypass and reason == "SYMBOL_COOLDOWN_ACTIVE":
        return _rescue_reject(stats, "COOLDOWN_ACTIVE")
    return True, "HIGH_EFFECTIVE_RR_RESCUE"
def _estimate_backtest_spread_pct(liquidity_score: float, volatility_pct: float) -> float:
    base_spread_pct = 0.0008 + (1.0 - liquidity_score) * 0.0012
    volatility_widening = min(0.0004, max(0.0, (volatility_pct - 2.0) * 0.00002))
    return max(0.0005, min(0.0024, base_spread_pct + volatility_widening))
def _build_market_ctx(
    now: Candle,
    prev: Candle,
    symbol_meta: Mapping[str, Any],
    recent: Optional[List[Candle]] = None,
) -> Dict[str, Any]:
    bullish_breakout = now.close >= prev.close
    side = "LONG" if bullish_breakout else "SHORT"
    entry = now.close
    sl = min(now.low, prev.low) if side == "LONG" else max(now.high, prev.high)
    risk = max(entry - sl, 1e-9)
    if side == "SHORT":
        risk = max(sl - entry, 1e-9)
    body = abs(now.close - now.open)
    breakout_strength = max(0.0, (now.close - prev.high) / max(prev.high, 1e-9)) if side == "LONG" else max(0.0, (prev.low - now.close) / max(prev.low, 1e-9))
    range_pct = ((now.high - now.low) / max(now.close, 1e-9)) * 100.0
    rr = max(1.1, min(3.5, 1.2 + breakout_strength * 25.0 + body / max(now.open, 1e-9) * 8.0))
    tp = entry + rr * risk if side == "LONG" else entry - rr * risk
    score = max(0.0, min(10.0, 3.0 + breakout_strength * 500.0 + range_pct))
    expectancy = ((score / 10.0) - 0.5) * (rr - 1.0)
    quote_volume = symbol_meta.get("quoteVolume")
    if quote_volume in (None, "", 0, 0.0):
        quote_volume = now.volume * now.close * 1440.0
    candle_range_pct = ((now.high - now.low) / max(now.close, 1e-9)) * 100.0
    liq = min(1.0, max(0.05, float(quote_volume) / 100000000.0))
    spread_source = "ACTUAL" if symbol_meta.get("actual_spread_pct") not in (None, "") else "ESTIMATED_BACKTEST"
    raw_spread = symbol_meta.get("actual_spread_pct") or symbol_meta.get("estimated_spread_pct") or _estimate_backtest_spread_pct(liq, candle_range_pct)
    spread_pct, spread_unit_assumed = normalize_pct_input(raw_spread, field="spread_pct")
    base = {
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "score": score,
        "setup_type": "BREAKOUT_UP" if side == "LONG" else "BREAKDOWN_DOWN",
        "setup_reason": "CLOSE_ABOVE_PREV_HIGH" if side == "LONG" else "CLOSE_BELOW_PREV_LOW",
        "regime": "BREAKOUT" if breakout_strength > 0.002 else "TREND",
        "expectancy": expectancy,
        "expectancy_bucket": _bucket_expectancy(expectancy),
        "side": side,
        "volume_24h_usdt": float(quote_volume),
        "spread_pct": spread_pct,
        "spread_unit_assumed": spread_unit_assumed,
        "spread_source": spread_source,
        "candle_range_pct": candle_range_pct,
        "volatility_pct": candle_range_pct,
        "funding_rate_pct": float(symbol_meta.get("fundingRate", 0.0) or 0.0),
    }
    klines = [{"high": c.high, "low": c.low, "close": c.close} for c in (recent or [])[-20:] if c]
    exec_ctx = build_execution_context(
        {
            **base,
            "raw_spread_pct_input": raw_spread,
            "recent_klines": klines,
            "liquidity_score": liq,
        }
    )
    base.update(exec_ctx)
    base["spread_unit_assumed"] = spread_unit_assumed
    return base
def _build_symbol_market_data(symbol_meta: Mapping[str, Any], candles: List[Candle], idx: int) -> Dict[str, Any]:
    now = candles[idx]
    prev = candles[idx - 1] if idx > 0 else now
    recent = candles[max(0, idx - 20):idx + 1]
    diagnostics: Dict[str, Any] = {}
    quote_volume = symbol_meta.get("quoteVolume")
    if quote_volume in (None, "", 0, 0.0):
        close = max(now.close, 1e-9)
        quote_volume = now.volume * close * 1440.0
        diagnostics["volume_24h_usdt"] = "derived_from_candle_volume"
    candle_range_pct = ((now.high - now.low) / max(now.close, 1e-9)) * 100.0
    volatility_pct = candle_range_pct
    lookback = recent[-10:] if recent else [now]
    up_bars = sum(1 for c in lookback if c.close > c.open)
    trend_strength = up_bars / max(1, len(lookback))
    liquidity_score = min(1.0, max(0.05, float(quote_volume) / 100000000.0))
    actual_spread_pct = symbol_meta.get("actual_spread_pct")
    spread_source = "ACTUAL" if actual_spread_pct not in (None, "") else "ESTIMATED_BACKTEST"
    spread_unit_assumed = "fraction"
    if actual_spread_pct not in (None, ""):
        spread_pct, spread_unit_assumed = normalize_pct_input(actual_spread_pct, field="spread_pct")
    else:
        # Conservative offline estimate: wider for lower liquidity and high volatility.
        spread_pct = _estimate_backtest_spread_pct(liquidity_score, volatility_pct)
    recent_vol = [c.volume for c in recent[-6:]]
    prev_vol = [c.volume for c in recent[-12:-6]]
    if recent_vol and prev_vol:
        recent_avg = sum(recent_vol) / len(recent_vol)
        prev_avg = sum(prev_vol) / len(prev_vol)
        recent_volume_change_pct = ((recent_avg - prev_avg) / max(prev_avg, 1e-9)) * 100.0
    else:
        recent_volume_change_pct = 0.0
        diagnostics["recent_volume_change_pct"] = "defaulted_insufficient_history"
    closes = [c.close for c in lookback]
    close_min = min(closes)
    close_max = max(closes)
    chop_score = min(1.0, max(0.0, 1.0 - abs((closes[-1] - closes[0]) / max(close_max - close_min, 1e-9))))
    panic_score = 0.0
    drop_pct = ((prev.close - now.close) / max(prev.close, 1e-9)) * 100.0
    if drop_pct > 3.0 and volatility_pct > 2.0:
        panic_score = min(1.0, (drop_pct / 10.0) + (volatility_pct / 20.0))
    return {
        "volume_24h_usdt": float(quote_volume),
        "spread_pct": spread_pct,
        "spread_source": spread_source,
        "spread_unit_assumed": spread_unit_assumed,
        "actual_spread_pct": spread_pct if actual_spread_pct not in (None, "") else None,
        "estimated_spread_pct": spread_pct if spread_source == "ESTIMATED_BACKTEST" else None,
        "candle_range_pct": candle_range_pct,
        "volatility_pct": volatility_pct,
        "trend_strength": trend_strength,
        "liquidity_score": liquidity_score,
        "recent_volume_change_pct": recent_volume_change_pct,
        "chop_score": chop_score,
        "panic_score": panic_score,
        "selector_diagnostics": diagnostics,
    }
def parse_ts(value: str) -> int:
    if value.isdigit():
        return int(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)
def fetch_json(url: str) -> Any:
    with urlopen(url) as resp:  # nosec - public market data
        return json.loads(resp.read().decode("utf-8"))
def select_symbol_universe(top_n: int, quote: str = "USDT", symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if symbols:
        return [{"symbol": sym.strip().upper(), "quoteVolume": 0.0} for sym in symbols if sym.strip()]
    info = fetch_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
    tickers = fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    ticker_map = {t["symbol"]: t for t in tickers}
    selected = []
    for s in info.get("symbols", []):
        sym = s.get("symbol", "")
        if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL":
            continue
        if s.get("quoteAsset") != quote or not sym.endswith(quote):
            continue
        t = ticker_map.get(sym)
        if not t:
            continue
        qv = float(t.get("quoteVolume", 0.0) or 0.0)
        if qv <= 0:
            continue
        if not s.get("filters"):
            continue
        selected.append({"symbol": sym, "quoteVolume": qv})
    selected.sort(key=lambda x: x["quoteVolume"], reverse=True)
    return selected[:top_n]
def save_symbol_universe(path: str, universe: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "quoteVolume"])
        w.writeheader()
        w.writerows(universe)
def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[Candle]:
    rows = fetch_binance_klines_paginated(symbol=symbol, interval=interval, start_ms=start_ms, end_ms=end_ms)
    return [Candle(timestamp=r.timestamp, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume) for r in rows]

def _fetch_klines_legacy(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[Candle]:
    params = urlencode(
        {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1500,
        }
    )
    rows = fetch_json(f"https://fapi.binance.com/fapi/v1/klines?{params}")
    return [
        Candle(
            timestamp=int(r[0]),
            open=float(r[1]),
            high=float(r[2]),
            low=float(r[3]),
            close=float(r[4]),
            volume=float(r[5]),
        )
        for r in rows
    ]
def load_or_fetch_candles(symbol: str, interval: str, start_ms: int, end_ms: int, output_dir: str, force_refresh: bool = False) -> List[Candle]:
    rows = load_or_fetch_historical_candles(
        symbol=symbol,
        interval=interval,
        start_ms=start_ms,
        end_ms=end_ms,
        output_dir=output_dir,
        force_refresh=force_refresh,
    )
    return [Candle(timestamp=r.timestamp, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume) for r in rows]
def load_candles(path: str, start_ms: int, end_ms: int) -> List[Candle]:
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = parse_ts(str(row.get("timestamp") or row.get("open_time") or row.get("time") or row.get("date")))
            if start_ms <= ts <= end_ms:
                out.append(
                    Candle(
                        ts,
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row.get("volume", 0.0)),
                    )
                )
    out.sort(key=lambda x: x.timestamp)
    return out
def scan_symbol_backtest(
    symbol: str,
    candles: List[Candle],
    idx: int,
    context: Dict[str, Any],
) -> Optional[CandidateOrder]:
    OrderExecutionContext, TradingMode, run_order_cycle = _order_runtime()
    if idx < 2:
        return None
    now = candles[idx]
    prev = candles[idx - 1]
    mctx = _build_market_ctx(now, prev, context.get("symbol_meta", {}), candles[max(0, idx - 20):idx + 1])
    ctx = OrderExecutionContext(
        mode=TradingMode.BACKTEST,
        timestamp=now.timestamp,
        symbol=symbol,
        balance=float(context.get("balance", 1000)),
        risk_pct=float(context.get("risk_pct", 1.0)),
        market_ctx=mctx,
    )
    result = run_order_cycle(ctx, recent_stats=context.get("recent_stats", {}))
    context["last_result"] = result
    context["market_ctx"] = mctx
    if result.get("status") != "executed":
        return None
    c = result["candidate"]
    return CandidateOrder(
        now.timestamp,
        symbol,
        c.side,
        c.entry,
        c.sl,
        c.tp,
        c.rr,
        c.setup_type,
        c.setup_reason,
        c.regime,
        c.score,
        c.order_type,
        expectancy_bucket=mctx.get("expectancy_bucket", "UNKNOWN"),
    )
def simulate_candidate(
    candidate: CandidateOrder,
    candles: List[Candle],
    idx: int,
    balance: float,
    risk_pct: float,
    market_ctx: Optional[Mapping[str, Any]] = None,
) -> List[LifecycleRow]:
    market_ctx = market_ctx or {}
    signal_id = f"{candidate.symbol}:{candidate.timestamp}"
    lifecycle_id = str(uuid5(NAMESPACE_URL, f"backtest:lifecycle:{signal_id}"))
    order_id = str(uuid5(NAMESPACE_URL, f"backtest:order:{signal_id}:{candidate.entry}:{candidate.sl}:{candidate.tp}"))
    position_id = str(uuid5(NAMESPACE_URL, f"backtest:position:{signal_id}:{candidate.side}"))
    def _finalize_rows(out_rows: List[LifecycleRow]) -> List[LifecycleRow]:
        for seq, row in enumerate(out_rows, start=1):
            row.lifecycle_seq = seq
            row.accepted_reason = str(market_ctx.get("accepted_reason", row.accepted_reason) or row.accepted_reason)
            row.original_reject_reason = str(market_ctx.get("original_reject_reason", row.original_reject_reason) or row.original_reject_reason)
            row.rescue_size_multiplier = _safe_float(market_ctx.get("rescue_size_multiplier"), row.rescue_size_multiplier)
            row.rescue_effective_rr = _safe_float(market_ctx.get("rescue_effective_rr"), row.rescue_effective_rr)
            row.rescue_decision_context = str(market_ctx.get("rescue_decision_context", row.rescue_decision_context) or row.rescue_decision_context)
            if row.signal_id == "":
                row.signal_id = signal_id
            if row.lifecycle_id == "":
                row.lifecycle_id = lifecycle_id
            if row.status_after in {"ORDER_PLACED", "POSITION_OPENED", "POSITION_CLOSED"} and row.order_id == "":
                row.order_id = order_id
            if row.status_after in {"POSITION_OPENED", "POSITION_CLOSED"} and row.position_id == "":
                row.position_id = position_id
        return out_rows
    rows: List[LifecycleRow] = [
        LifecycleRow(
            candidate.timestamp,
            candidate.symbol,
            candidate.side,
            candidate.setup_type,
            candidate.setup_reason,
            candidate.regime,
            candidate.score,
            candidate.rr,
            candidate.entry,
            candidate.sl,
            candidate.tp,
            "NONE",
            "SIGNAL_CREATED",
            order_type=candidate.order_type,
            expectancy_bucket=candidate.expectancy_bucket,
            volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
            liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
            signal_id=signal_id,
            lifecycle_id=lifecycle_id,
        )
    ]
    rows.append(
        LifecycleRow(
            candidate.timestamp, candidate.symbol, candidate.side, candidate.setup_type, candidate.setup_reason, candidate.regime,
            candidate.score, candidate.rr, candidate.entry, candidate.sl, candidate.tp, "SIGNAL_CREATED", "SIGNAL_ACCEPTED",
            order_type=candidate.order_type, expectancy_bucket=candidate.expectancy_bucket,
            volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
            liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
            signal_id=signal_id, lifecycle_id=lifecycle_id,
        )
    )
    rows.append(
        LifecycleRow(
            candidate.timestamp,
            candidate.symbol,
            candidate.side,
            candidate.setup_type,
            candidate.setup_reason,
            candidate.regime,
            candidate.score,
            candidate.rr,
            candidate.entry,
            candidate.sl,
            candidate.tp,
            "SIGNAL_ACCEPTED",
            "WAITING_ENTRY_ZONE",
            order_type=candidate.order_type,
            expectancy_bucket=candidate.expectancy_bucket,
            volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
            liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
            signal_id=signal_id, lifecycle_id=lifecycle_id,
        )
    )
    triggered_ts = None
    trigger_price = 0.0
    if candidate.order_type in {"MARKET", "BREAKOUT", "IMMEDIATE"}:
        triggered_ts = candles[idx].timestamp
        trigger_price = candidate.entry
        start_idx = idx
        rows.append(
            LifecycleRow(
                candidate.timestamp,
                candidate.symbol,
                candidate.side,
                candidate.setup_type,
                candidate.setup_reason,
                candidate.regime,
                candidate.score,
                candidate.rr,
                candidate.entry,
                candidate.sl,
                candidate.tp,
                "WAITING_ENTRY_ZONE",
                "ENTRY_TRIGGERED",
                trigger_price=trigger_price,
                order_type=candidate.order_type,
                expectancy_bucket=candidate.expectancy_bucket,
                volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                signal_id=signal_id, lifecycle_id=lifecycle_id,
            )
        )
        rows.append(
            LifecycleRow(
                candidate.timestamp,
                candidate.symbol,
                candidate.side,
                candidate.setup_type,
                candidate.setup_reason,
                candidate.regime,
                candidate.score,
                candidate.rr,
                candidate.entry,
                candidate.sl,
                candidate.tp,
                "ENTRY_TRIGGERED",
                "ORDER_PLACED",
                trigger_price=trigger_price,
                order_type=candidate.order_type,
                expectancy_bucket=candidate.expectancy_bucket,
                volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                signal_id=signal_id, lifecycle_id=lifecycle_id, order_id=order_id,
            )
        )
        rows.append(
            LifecycleRow(
                candidate.timestamp, candidate.symbol, candidate.side, candidate.setup_type, candidate.setup_reason, candidate.regime,
                candidate.score, candidate.rr, candidate.entry, candidate.sl, candidate.tp, "ORDER_PLACED", "POSITION_OPENED",
                trigger_price=trigger_price, order_type=candidate.order_type, expectancy_bucket=candidate.expectancy_bucket,
                volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                signal_id=signal_id, lifecycle_id=lifecycle_id, order_id=order_id, position_id=position_id,
            )
        )
    else:
        start_idx = idx
        for j in range(idx, len(candles)):
            c = candles[j]
            if c.low <= candidate.entry <= c.high:
                triggered_ts = c.timestamp
                trigger_price = candidate.entry
                start_idx = j
                rows.append(
                    LifecycleRow(
                        candidate.timestamp,
                        candidate.symbol,
                        candidate.side,
                        candidate.setup_type,
                        candidate.setup_reason,
                        candidate.regime,
                        candidate.score,
                        candidate.rr,
                        candidate.entry,
                        candidate.sl,
                        candidate.tp,
                        "WAITING_ENTRY_ZONE",
                        "ENTRY_TRIGGERED",
                        trigger_price=trigger_price,
                        order_type=candidate.order_type,
                        expectancy_bucket=candidate.expectancy_bucket,
                        volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                        spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                        funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                        expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                        volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                        liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                        signal_id=signal_id, lifecycle_id=lifecycle_id,
                    )
                )
                rows.append(
                    LifecycleRow(
                        candidate.timestamp,
                        candidate.symbol,
                        candidate.side,
                        candidate.setup_type,
                        candidate.setup_reason,
                        candidate.regime,
                        candidate.score,
                        candidate.rr,
                        candidate.entry,
                        candidate.sl,
                        candidate.tp,
                        "ENTRY_TRIGGERED",
                        "ORDER_PLACED",
                        trigger_price=trigger_price,
                        order_type=candidate.order_type,
                        expectancy_bucket=candidate.expectancy_bucket,
                        volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                        spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                        funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                        expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                        volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                        liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                        signal_id=signal_id, lifecycle_id=lifecycle_id, order_id=order_id,
                    )
                )
                rows.append(
                    LifecycleRow(
                        candidate.timestamp, candidate.symbol, candidate.side, candidate.setup_type, candidate.setup_reason, candidate.regime,
                        candidate.score, candidate.rr, candidate.entry, candidate.sl, candidate.tp, "ORDER_PLACED", "POSITION_OPENED",
                        trigger_price=trigger_price, order_type=candidate.order_type, expectancy_bucket=candidate.expectancy_bucket,
                        volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                        spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                        funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                        expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                        volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                        liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                        signal_id=signal_id, lifecycle_id=lifecycle_id, order_id=order_id, position_id=position_id,
                    )
                )
                break
        if triggered_ts is None:
            rows.append(
                LifecycleRow(
                    candidate.timestamp,
                    candidate.symbol,
                    candidate.side,
                    candidate.setup_type,
                    candidate.setup_reason,
                    candidate.regime,
                    candidate.score,
                    candidate.rr,
                    candidate.entry,
                    candidate.sl,
                    candidate.tp,
                    "WAITING_ENTRY_ZONE",
                    "ENTRY_TIMEOUT",
                    cancel_reason="TIMEOUT",
                    order_type=candidate.order_type,
                    expectancy_bucket=candidate.expectancy_bucket,
                    volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                    spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                    funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                    expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                    volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                    liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                )
            )
            return _finalize_rows(rows)
    mfe = 0.0
    mae = 0.0
    long_side = str(candidate.side).upper() == "LONG"
    tp_distance = max(abs(candidate.tp - candidate.entry), 1e-9)
    sl_distance = max(abs(candidate.entry - candidate.sl), 1e-9)
    for j in range(start_idx, len(candles)):
        c = candles[j]
        if long_side:
            mfe = max(mfe, c.high - candidate.entry)
            mae = max(mae, candidate.entry - c.low)
            hit_tp = c.high >= candidate.tp
            hit_sl = c.low <= candidate.sl
        else:
            mfe = max(mfe, candidate.entry - c.low)
            mae = max(mae, c.high - candidate.entry)
            hit_tp = c.low <= candidate.tp
            hit_sl = c.high >= candidate.sl
        # Conservative same-candle rule:
        # if both TP and SL touch inside the same candle, count SL first.
        if hit_sl and hit_tp:
            hit_tp = False
        if hit_sl:
            pnl_pct = ((candidate.sl - candidate.entry) / candidate.entry) * 100 if long_side else ((candidate.entry - candidate.sl) / candidate.entry) * 100
            rows.append(
                finalize(
                    candidate,
                    "ORDER_PLACED",
                    "POSITION_CLOSED",
                    trigger_price,
                    candidate.sl,
                    "SL_HIT",
                    pnl_pct,
                    balance,
                    risk_pct,
                    triggered_ts,
                    c.timestamp,
                    market_ctx,
                    mfe / tp_distance,
                    mae / sl_distance,
                )
            )
            return _finalize_rows(rows)
        if hit_tp:
            pnl_pct = ((candidate.tp - candidate.entry) / candidate.entry) * 100 if long_side else ((candidate.entry - candidate.tp) / candidate.entry) * 100
            rows.append(
                finalize(
                    candidate,
                    "ORDER_PLACED",
                    "POSITION_CLOSED",
                    trigger_price,
                    candidate.tp,
                    "TP_HIT",
                    pnl_pct,
                    balance,
                    risk_pct,
                    triggered_ts,
                    c.timestamp,
                    market_ctx,
                    mfe / tp_distance,
                    mae / sl_distance,
                )
            )
            return _finalize_rows(rows)
    c = candles[-1]
    pnl_pct = ((c.close - candidate.entry) / candidate.entry) * 100 if long_side else ((candidate.entry - c.close) / candidate.entry) * 100
    rows.append(
        finalize(
            candidate,
            "ORDER_PLACED",
            "POSITION_CLOSED",
            trigger_price,
            c.close,
            "TIMEOUT",
            pnl_pct,
            balance,
            risk_pct,
            triggered_ts,
            c.timestamp,
            market_ctx,
            mfe / tp_distance,
            mae / sl_distance,
        )
    )
    return _finalize_rows(rows)
def finalize(
    candidate,
    before,
    after,
    trigger_price,
    close_price,
    close_reason,
    pnl_pct,
    balance,
    risk_pct,
    triggered_ts,
    closed_ts,
    market_ctx: Optional[Mapping[str, Any]] = None,
    mfe: float = 0.0,
    mae: float = 0.0,
):
    market_ctx = market_ctx or {}
    risk_usdt = balance * (risk_pct / 100)
    net_pnl_usdt = risk_usdt * (pnl_pct / 100)
    hold = (closed_ts - triggered_ts) / 60000 if triggered_ts else 0
    return LifecycleRow(
        candidate.timestamp,
        candidate.symbol,
        candidate.side,
        candidate.setup_type,
        candidate.setup_reason,
        candidate.regime,
        candidate.score,
        candidate.rr,
        candidate.entry,
        candidate.sl,
        candidate.tp,
        before,
        after,
        trigger_price=trigger_price,
        close_price=close_price,
        close_reason=close_reason,
        net_pnl_pct=pnl_pct,
        net_pnl_usdt=net_pnl_usdt,
        hold_minutes=hold,
        order_type=candidate.order_type,
        expectancy_bucket=candidate.expectancy_bucket,
        volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
        spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
        funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
        expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
        volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
        liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
        effective_rr=_execution_reject_flags(candidate.rr, market_ctx)[0],
        cost_penalty=_execution_reject_flags(candidate.rr, market_ctx)[2]["cost_penalty_total"],
        stop_too_wide_softened=bool(market_ctx.get("stop_too_wide_softened", False)),
        original_reject_reason=str(market_ctx.get("original_reject_reason", "") or ""),
        reject_reason_softened=str(market_ctx.get("reject_reason_softened", "") or ""),
        risk_scale=_safe_float(market_ctx.get("risk_scale"), 1.0),
        accepted_reason=str(market_ctx.get("accepted_reason", "BASELINE") or "BASELINE"),
        rescue_size_multiplier=_safe_float(market_ctx.get("rescue_size_multiplier"), 1.0),
        rescue_effective_rr=_safe_float(market_ctx.get("rescue_effective_rr"), 0.0),
        rescue_decision_context=str(market_ctx.get("rescue_decision_context", "") or ""),
        mfe=mfe,
        mae=mae,
    )
def simulate_rejected_counterfactual(
    candidate: CandidateOrder,
    candles: List[Candle],
    idx: int,
    timeout_bars: int = 240,
) -> dict[str, Any]:
    if idx >= len(candles):
        return {
            "outcome": "UNKNOWN",
            "would_trigger": False,
            "would_tp_hit": False,
            "would_sl_hit": False,
            "max_favorable_excursion": 0.0,
            "max_adverse_excursion": 0.0,
        }
    would_trigger = False
    would_tp = False
    would_sl = False
    mfe = 0.0
    mae = 0.0
    scan = candles[idx:idx + timeout_bars]
    side = str(candidate.side).upper()
    for c in scan:
        if c.low <= candidate.entry <= c.high:
            would_trigger = True
        if would_trigger:
            if side == "SHORT":
                mfe = max(mfe, candidate.entry - c.low)
                mae = max(mae, c.high - candidate.entry)
                hit_tp = c.low <= candidate.tp
                hit_sl = c.high >= candidate.sl
            else:
                mfe = max(mfe, c.high - candidate.entry)
                mae = max(mae, candidate.entry - c.low)
                hit_tp = c.high >= candidate.tp
                hit_sl = c.low <= candidate.sl
            # Conservative same-candle tie-breaker for both LONG and SHORT.
            # We cannot infer intrabar path from OHLC, so if both TP and SL touch,
            # count it as a stop loss to avoid optimistic bias.
            if hit_sl and hit_tp:
                hit_tp = False
            if hit_sl:
                would_sl = True
                break
            if hit_tp:
                would_tp = True
                break
    if not would_trigger:
        outcome = "WOULD_NOT_TRIGGER"
    elif would_tp:
        outcome = "WOULD_TP"
    elif would_sl:
        outcome = "WOULD_SL"
    elif len(scan) < timeout_bars and idx + timeout_bars > len(candles):
        outcome = "UNKNOWN"
    else:
        outcome = "WOULD_TIMEOUT"
    return {
        "outcome": outcome,
        "would_trigger": would_trigger,
        "would_tp_hit": would_tp,
        "would_sl_hit": would_sl,
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
    }
def _update_recent_stats_after_close(recent_stats: Dict[str, Any], symbol: str, close_reason: str) -> None:
    if close_reason == "SL_HIT":
        recent_stats["consecutive_sl_count"] = int(recent_stats.get("consecutive_sl_count", 0) or 0) + 1
        recent_stats["consecutive_tp_count"] = 0
    elif close_reason == "TP_HIT":
        recent_stats["consecutive_tp_count"] = int(recent_stats.get("consecutive_tp_count", 0) or 0) + 1
        recent_stats["consecutive_sl_count"] = 0
    outcomes = recent_stats.setdefault("outcomes", [])
    if close_reason in {"TP_HIT", "SL_HIT"}:
        outcomes.append(1 if close_reason == "TP_HIT" else 0)
    window = int(recent_stats.get("rolling_window", 20) or 20)
    recent = outcomes[-window:]
    recent_stats["rolling_winrate"] = (sum(recent) / len(recent)) if recent else 0.0
def _offline_fixture(start_ms: int) -> tuple[list[dict[str, float]], dict[str, list[Candle]]]:
    universe = [{"symbol": "BTCUSDT", "quoteVolume": 100000000.0}]
    candles: list[Candle] = []
    base = 100.0
    for i in range(30):
        ts = start_ms + i * 60_000
        o = base + i * 0.2
        h = o + 0.6
        l = o - 0.4
        c = o + 0.3
        candles.append(Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=1000.0 + i))
    return universe, {"BTCUSDT": candles}
def process_backtest_result(
    symbol: str,
    candle: Candle,
    idx: int,
    candles: List[Candle],
    result: Dict[str, Any],
    mctx: Mapping[str, Any],
    balance: float,
    risk_pct: float,
    lifecycle: List[LifecycleRow],
    rejected: List[Dict[str, Any]],
    rejection_counts: Dict[str, int],
    open_rows: List[LifecycleRow],
    recent_stats: Dict[str, Any],
    rescue_config: Optional[RescueConfig] = None,
    rescue_stats: Optional[RescueStats] = None,
    mode: str = "BACKTEST",
) -> Optional[CandidateOrder]:
    rescue_config = rescue_config or RescueConfig()
    rescue_stats = rescue_stats or RescueStats()
    diagnostics = result.get("diagnostics", {})
    side = diagnostics.get("side") or "LONG"
    setup_type = diagnostics.get("setup_type", mctx.get("setup_type", ""))
    setup_reason = diagnostics.get("setup_reason", mctx.get("setup_reason", ""))
    regime = diagnostics.get("regime", mctx.get("regime", ""))
    score = float(diagnostics.get("score", mctx.get("score", 0.0)) or 0.0)
    rr = float(diagnostics.get("rr", mctx.get("rr", 0.0)) or 0.0)
    entry = float(diagnostics.get("entry", mctx.get("entry", 0.0)) or 0.0)
    sl = float(diagnostics.get("sl", mctx.get("sl", 0.0)) or 0.0)
    tp = float(diagnostics.get("tp", mctx.get("tp", 0.0)) or 0.0)
    order_type = diagnostics.get("order_type", "LIMIT")
    expectancy = diagnostics.get("expectancy", mctx.get("expectancy"))
    expectancy_bucket = _bucket_expectancy(expectancy)
    signal_id = f"{symbol}:{candle.timestamp}"
    base_effective_rr, _base_execution_flags, _base_penalty_breakdown = _execution_reject_flags(rr, mctx)
    execution_ctx_missing = any(
        value == "UNAVAILABLE_BACKTEST"
        for value in (
            mctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            mctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
        )
    )
    def _try_rescue(reject_reason: str, effective_rr_value: float) -> Optional[CandidateOrder]:
        ok, rescue_reason = _rescue_acceptance_allowed(
            mode=mode, reason=reject_reason, score=score, effective_rr=effective_rr_value, regime=regime,
            mctx=mctx, cfg=rescue_config, stats=rescue_stats, recent_stats=recent_stats, open_rows=open_rows, symbol=symbol,
        )
        if not ok:
            return None
        rescue_stats.accepted_count += 1
        context = {
            "accepted_reason": rescue_reason,
            "original_reject_reason": reject_reason,
            "rescue_size_multiplier": rescue_config.size_multiplier,
            "rescue_effective_rr": effective_rr_value,
            "rescue_score": score,
            "rescue_mode": mode,
        }
        rescued = CandidateOrder(
            candle.timestamp, symbol, side, entry, sl, tp, rr, setup_type, setup_reason, regime, score, order_type,
            expectancy_bucket=expectancy_bucket, accepted_reason=rescue_reason, original_reject_reason=reject_reason,
            rescue_size_multiplier=rescue_config.size_multiplier, rescue_effective_rr=effective_rr_value,
            rescue_decision_context=json.dumps(context, sort_keys=True),
        )
        sim_ctx = {**dict(mctx), **context, "risk_scale": rescue_config.size_multiplier, "rescue_decision_context": json.dumps(context, sort_keys=True)}
        sim_rows = simulate_candidate(rescued, candles, idx, balance, risk_pct * rescue_config.size_multiplier, market_ctx=sim_ctx)
        lifecycle.extend(sim_rows)
        recent_stats["last_trade_ts_by_symbol"][symbol] = candle.timestamp
        recent_stats["trades_today_by_symbol"][symbol] = int(recent_stats["trades_today_by_symbol"].get(symbol, 0)) + 1
        recent_stats["global_trades_today"] += 1
        for sim_row in sim_rows:
            if sim_row.close_reason == "TIMEOUT":
                open_rows.append(sim_row)
            if sim_row.status_after == "POSITION_CLOSED":
                _update_recent_stats_after_close(recent_stats, symbol, sim_row.close_reason)
        return rescued
    lifecycle.append(
        LifecycleRow(
            timestamp=candle.timestamp,
            symbol=symbol,
            side=side,
            setup_type=setup_type,
            setup_reason=setup_reason,
            regime=regime,
            score=score,
            rr=rr,
            entry=entry,
            sl=sl,
            tp=tp,
            status_before="NONE",
            status_after="SIGNAL_CREATED",
            order_type=order_type,
            expectancy_bucket=expectancy_bucket,
            volume_24h_usdt=mctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            spread_pct=mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            funding_rate_pct=mctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            expected_slippage_pct=mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            volatility_regime=str(mctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
            liquidity_score=mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
            effective_rr=base_effective_rr,
            cost_penalty=_safe_float(_base_penalty_breakdown.get("cost_penalty_total"), 0.0),
            stop_too_wide_softened=bool(diagnostics.get("stop_too_wide_softened", False)) if isinstance(diagnostics, dict) else False,
            original_reject_reason=str(diagnostics.get("original_reject_reason", "") or "") if isinstance(diagnostics, dict) else "",
            reject_reason_softened=str(diagnostics.get("reject_reason_softened", "") or "") if isinstance(diagnostics, dict) else "",
            risk_scale=_safe_float(diagnostics.get("risk_scale"), 1.0) if isinstance(diagnostics, dict) else 1.0,
            signal_id=signal_id,
            lifecycle_id=signal_id,
        )
    )
    if result.get("status") == "rejected":
        reason = str(result.get("reason") or result.get("reject_reason") or "").strip().upper()
        if not reason or reason == "UNKNOWN":
            reason = "REJECT_REASON_MISSING"
        rescued = _try_rescue(reason, base_effective_rr)
        if rescued is not None:
            return rescued
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        lifecycle.append(
            LifecycleRow(
                timestamp=candle.timestamp,
                symbol=symbol,
                side=side,
                setup_type=setup_type,
                setup_reason=setup_reason,
                regime=regime,
                score=score,
                rr=rr,
                entry=entry,
                sl=sl,
                tp=tp,
                status_before="SIGNAL_CREATED",
                status_after="SIGNAL_REJECTED",
                reject_reason=reason,
                order_type=order_type,
                expectancy_bucket=expectancy_bucket,
                volume_24h_usdt=mctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                spread_pct=mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                funding_rate_pct=mctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                expected_slippage_pct=mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                volatility_regime=str(mctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                liquidity_score=mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                effective_rr=base_effective_rr,
                cost_penalty=_safe_float(_base_penalty_breakdown.get("cost_penalty_total"), 0.0),
                stop_too_wide_softened=bool(diagnostics.get("stop_too_wide_softened", False)) if isinstance(diagnostics, dict) else False,
                original_reject_reason=str(diagnostics.get("original_reject_reason", "") or "") if isinstance(diagnostics, dict) else "",
                reject_reason_softened=str(diagnostics.get("reject_reason_softened", "") or "") if isinstance(diagnostics, dict) else "",
                risk_scale=_safe_float(diagnostics.get("risk_scale"), 1.0) if isinstance(diagnostics, dict) else 1.0,
                signal_id=signal_id,
                lifecycle_id=signal_id,
            )
        )
        rejected.append(
            {
                "signal_id": signal_id,
                "lifecycle_state": "SIGNAL_REJECTED",
                "execution_ctx_missing": execution_ctx_missing,
                "expectancy_bucket": expectancy_bucket,
                "timestamp": candle.timestamp,
                "symbol": symbol,
                "side": side,
                "setup_type": setup_type,
                "setup_reason": setup_reason,
                "regime": regime,
                "score": score,
                "gate_score": _safe_float(diagnostics.get("score"), score),
                "rr": rr,
                "expectancy": expectancy,
                "quality_score": diagnostics.get("quality_score", 0.0),
                "reject_reason": reason,
                "diagnostics": json.dumps(diagnostics, sort_keys=True),
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "spread_pct": mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                "liquidity_score": mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                "volatility_score": mctx.get("volatility_pct", mctx.get("spread_pct", "UNAVAILABLE_BACKTEST")),
                "expected_slippage_pct": mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                "raw_rr": rr,
                "effective_rr": diagnostics.get("effective_rr", base_effective_rr),
                **_base_penalty_breakdown,
                "cost_penalty": _safe_float(_base_penalty_breakdown.get("cost_penalty_total"), 0.0),
                "stop_too_wide_softened": bool(diagnostics.get("stop_too_wide_softened", False)) if isinstance(diagnostics, dict) else False,
                "original_reject_reason": diagnostics.get("original_reject_reason", "") if isinstance(diagnostics, dict) else "",
                "reject_reason_softened": diagnostics.get("reject_reason_softened", "") if isinstance(diagnostics, dict) else "",
                "risk_scale": _safe_float(diagnostics.get("risk_scale"), 1.0) if isinstance(diagnostics, dict) else 1.0,
                "min_required_score": ((diagnostics.get("adaptive_thresholds") or {}).get("min_score") if isinstance(diagnostics, dict) else None),
                "trend_strength": mctx.get("trend_strength", "UNAVAILABLE_BACKTEST"),
                "volatility_pct": mctx.get("volatility_pct", "UNAVAILABLE_BACKTEST"),
                "range_position": mctx.get("range_position", "UNAVAILABLE_BACKTEST"),
                "spread_pct": mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                "slippage_pct": mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                "liquidity_score": mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                "first_blocking_gate": diagnostics.get("failed_filter", ""),
                "all_failed_gates": json.dumps(diagnostics.get("all_failed_gates", []), sort_keys=True) if isinstance(diagnostics, dict) else "[]",
                **_low_score_rescue_watch_fields(reason, diagnostics if isinstance(diagnostics, dict) else {}),
            }
        )
        return None
    if result.get("status") != "executed":
        return None
    c = result["candidate"]
    cand = CandidateOrder(
        candle.timestamp,
        symbol,
        c.side,
        c.entry,
        c.sl,
        c.tp,
        c.rr,
        c.setup_type,
        c.setup_reason,
        c.regime,
        c.score,
        c.order_type,
        expectancy_bucket=expectancy_bucket,
    )
    effective_rr, execution_flags, penalty_breakdown = _execution_reject_flags(cand.rr, mctx)
    if execution_flags:
        reason = execution_flags[0]
        rescued = _try_rescue(reason, effective_rr)
        if rescued is not None:
            return rescued
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        lifecycle.append(
            LifecycleRow(
                timestamp=candle.timestamp,
                symbol=symbol,
                side=cand.side,
                setup_type=cand.setup_type,
                setup_reason=cand.setup_reason,
                regime=cand.regime,
                score=cand.score,
                rr=cand.rr,
                entry=cand.entry,
                sl=cand.sl,
                tp=cand.tp,
                status_before="SIGNAL_CREATED",
                status_after="ORDER_REJECTED",
                reject_reason=reason,
                order_type=cand.order_type,
                expectancy_bucket=cand.expectancy_bucket,
                volume_24h_usdt=mctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                spread_pct=mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                funding_rate_pct=mctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                expected_slippage_pct=mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                volatility_regime=str(mctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                liquidity_score=mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                effective_rr=effective_rr,
                cost_penalty=_safe_float(penalty_breakdown.get("cost_penalty_total"), 0.0),
                signal_id=signal_id,
                lifecycle_id=signal_id,
            )
        )
        rejected.append(
            {
                "signal_id": signal_id,
                "lifecycle_state": "ORDER_REJECTED",
                "execution_ctx_missing": execution_ctx_missing,
                "expectancy_bucket": cand.expectancy_bucket,
                "timestamp": candle.timestamp,
                "symbol": symbol,
                "side": cand.side,
                "setup_type": cand.setup_type,
                "setup_reason": cand.setup_reason,
                "regime": cand.regime,
                "score": cand.score,
                "gate_score": _safe_float(diagnostics.get("score"), cand.score),
                "rr": cand.rr,
                "expectancy": expectancy,
                "quality_score": diagnostics.get("quality_score", ""),
                "reject_reason": reason,
                "diagnostics": json.dumps(
                    {"effective_rr": effective_rr, "execution_flags": execution_flags, **penalty_breakdown},
                    sort_keys=True,
                ),
                "entry": cand.entry,
                "sl": cand.sl,
                "tp": cand.tp,
                "spread_pct": mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                "liquidity_score": mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                "volatility_score": mctx.get("volatility_pct", mctx.get("spread_pct", "UNAVAILABLE_BACKTEST")),
                "expected_slippage_pct": mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                "raw_rr": cand.rr,
                "effective_rr": effective_rr,
                **penalty_breakdown,
                "min_required_score": ((diagnostics.get("adaptive_thresholds") or {}).get("min_score") if isinstance(diagnostics, dict) else None),
                "trend_strength": mctx.get("trend_strength", "UNAVAILABLE_BACKTEST"),
                "volatility_pct": mctx.get("volatility_pct", "UNAVAILABLE_BACKTEST"),
                "range_position": mctx.get("range_position", "UNAVAILABLE_BACKTEST"),
                "slippage_pct": mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                "first_blocking_gate": "execution",
                "all_failed_gates": json.dumps(execution_flags, sort_keys=True),
                **_low_score_rescue_watch_fields(reason, {}),
            }
        )
        return None
    risk_scale = min(1.0, max(0.0, _safe_float(diagnostics.get("risk_scale"), 1.0))) if isinstance(diagnostics, dict) else 1.0
    sim_ctx = {**dict(mctx), "risk_scale": risk_scale}
    if isinstance(diagnostics, dict):
        for key in ("stop_too_wide_softened", "original_reject_reason", "reject_reason_softened"):
            if key in diagnostics:
                sim_ctx[key] = diagnostics[key]
    sim_rows = simulate_candidate(cand, candles, idx, balance, risk_pct * risk_scale, market_ctx=sim_ctx)
    lifecycle.extend(sim_rows)
    recent_stats["last_trade_ts_by_symbol"][symbol] = candle.timestamp
    recent_stats["trades_today_by_symbol"][symbol] = int(recent_stats["trades_today_by_symbol"].get(symbol, 0)) + 1
    recent_stats["global_trades_today"] += 1
    for sim_row in sim_rows:
        if sim_row.close_reason == "TIMEOUT":
            open_rows.append(sim_row)
        if sim_row.status_after == "POSITION_CLOSED":
            _update_recent_stats_after_close(recent_stats, symbol, sim_row.close_reason)
    return cand
def _lifecycle_event_id(row: LifecycleRow, index: int, lifecycle_state: str | None = None) -> str:
    status_after = lifecycle_state or row.status_after
    return (
        f"{row.timestamp}:{row.symbol}:{row.status_before}:{status_after}:"
        f"{row.entry}:{row.sl}:{row.tp}:{index}"
    )
def _persist_lifecycle_rows(rows: List[LifecycleRow]) -> List[dict[str, Any]]:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        for idx, row in enumerate(rows):
            execution_ctx_missing = any(
                row_value == "UNAVAILABLE_BACKTEST"
                for row_value in (row.volume_24h_usdt, row.spread_pct, row.funding_rate_pct, row.expected_slippage_pct, row.liquidity_score)
            )
            lifecycle_state = normalize_lifecycle_event(row.status_after)
            pre_signal_symbol_reject = _is_pre_signal_symbol_reject(row, lifecycle_state)
            if lifecycle_state == "SYMBOL_REJECTED" and not pre_signal_symbol_reject:
                lifecycle_state = "SIGNAL_REJECTED"
            source_stage = "SYMBOL_SELECTOR" if pre_signal_symbol_reject else "SIGNAL_ENGINE"
            if lifecycle_state in {"SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"}:
                decision = "REJECTED"
            elif lifecycle_state == "SIGNAL_CREATED":
                decision = "PENDING"
            else:
                decision = "ACCEPTED"
            save_trade_lifecycle_event(
                session,
                event_id=_lifecycle_event_id(row, idx, lifecycle_state),
                signal_id=_lifecycle_signal_id(row, lifecycle_state),
                order_id=row.order_id or None,
                symbol=row.symbol,
                mode="BACKTEST",
                lifecycle_state=lifecycle_state,
                decision=decision,
                reject_reason=row.reject_reason,
                score=row.score,
                rr=row.rr,
                effective_rr=(row.effective_rr if row.effective_rr is not None else row.rr),
                expectancy_bucket=row.expectancy_bucket,
                execution_ctx={
                    "volume_24h_usdt": row.volume_24h_usdt,
                    "spread_pct": row.spread_pct,
                    "funding_rate_pct": row.funding_rate_pct,
                    "expected_slippage_pct": row.expected_slippage_pct,
                    "volatility_regime": row.volatility_regime,
                    "liquidity_score": row.liquidity_score,
                    "volatility_score": row.volatility_score,
                    "liquidity_ok": row.liquidity_ok,
                    "volatility_ok": row.volatility_ok,
                    "shadow_outcome": row.shadow_outcome or None,
                    "cost_penalty": row.cost_penalty,
                    "close_reason": row.close_reason,
                    "source_stage": source_stage,
                    "accepted_reason": row.accepted_reason,
                    "original_reject_reason": row.original_reject_reason,
                    "rescue_size_multiplier": row.rescue_size_multiplier,
                    "rescue_effective_rr": row.rescue_effective_rr,
                    "rescue_decision_context": row.rescue_decision_context,
                },
                execution_ctx_missing=execution_ctx_missing,
                event_ts=str(row.timestamp),
                lifecycle_seq=row.lifecycle_seq or (idx + 1),
                lifecycle_id=row.lifecycle_id or f"{row.symbol}:{row.timestamp}",
                payload={"source_stage": source_stage, "event_flags": row.event_flags},
            )
        persisted = session.execute(
            text(
                """
                SELECT event_id, signal_id, order_id, symbol, mode, lifecycle_state, decision, reject_reason,
                       score, rr, effective_rr, expectancy_bucket, execution_ctx, execution_ctx_missing,
                       json_extract(execution_ctx, '$.liquidity_score') AS liquidity_score,
                       json_extract(execution_ctx, '$.volatility_score') AS volatility_score,
                       json_extract(execution_ctx, '$.liquidity_ok') AS liquidity_ok,
                       json_extract(execution_ctx, '$.volatility_ok') AS volatility_ok,
                       json_extract(execution_ctx, '$.shadow_outcome') AS shadow_outcome,
                       json_extract(execution_ctx, '$.cost_penalty') AS cost_penalty,
                       json_extract(execution_ctx, '$.spread_pct') AS spread_pct,
                       json_extract(execution_ctx, '$.expected_slippage_pct') AS expected_slippage_pct,
                       json_extract(execution_ctx, '$.funding_rate_pct') AS funding_rate_pct,
                       json_extract(execution_ctx, '$.volume_24h_usdt') AS volume_24h_usdt,
                       json_extract(execution_ctx, '$.accepted_reason') AS accepted_reason,
                       json_extract(execution_ctx, '$.original_reject_reason') AS original_reject_reason,
                       json_extract(execution_ctx, '$.rescue_size_multiplier') AS rescue_size_multiplier,
                       json_extract(execution_ctx, '$.rescue_effective_rr') AS rescue_effective_rr,
                       json_extract(execution_ctx, '$.rescue_decision_context') AS rescue_decision_context,
                       event_ts, created_at, lifecycle_seq, lifecycle_id
                FROM trade_lifecycle_events
                WHERE mode = 'BACKTEST'
                ORDER BY event_ts, symbol, signal_id, COALESCE(lifecycle_seq, 0), lifecycle_state, event_id
                """
            )
        ).mappings().all()
    return [dict(row) for row in persisted]


def _attach_rejected_shadow_to_lifecycle(rows: List[LifecycleRow], shadows: List[RejectedShadowEvaluation]) -> None:
    """Annotate rejected lifecycle decisions with shadow labels without accepting them."""
    shadow_by_key = {(s.symbol, int(s.timestamp)): s for s in shadows}
    for row in rows:
        if row.status_after not in {"SIGNAL_REJECTED", "ORDER_REJECTED"}:
            continue
        shadow = shadow_by_key.get((row.symbol, int(row.timestamp)))
        if shadow is None:
            continue
        row.effective_rr = shadow.effective_rr
        row.spread_pct = shadow.spread_pct
        row.liquidity_score = shadow.liquidity_score
        row.volatility_score = shadow.volatility_score
        row.shadow_outcome = shadow.shadow_outcome
        row.cost_penalty = shadow.cost_penalty
        row.liquidity_ok = shadow.liquidity_ok
        row.volatility_ok = shadow.volatility_ok


def _derive_backtest_counts(lifecycle: List[LifecycleRow]) -> Dict[str, int]:
    signal_ids = {
        f"{row.symbol}:{row.timestamp}"
        for row in lifecycle
        if row.status_after in {"SIGNAL_CREATED", "SYMBOL_REJECTED"}
    }
    total_candidates = len(signal_ids)
    rejected_count = sum(1 for row in lifecycle if row.status_after in {"SYMBOL_REJECTED", "SIGNAL_REJECTED", "ORDER_REJECTED"})
    accepted_count = total_candidates - rejected_count
    waiting_keys = {
        (row.order_id or f"{row.symbol}:{row.timestamp}", row.signal_id or f"{row.symbol}:{row.timestamp}")
        for row in lifecycle
        if row.status_after == "WAITING_ENTRY_ZONE"
    }
    triggered_keys = {
        (row.order_id or f"{row.symbol}:{row.timestamp}", row.signal_id or f"{row.symbol}:{row.timestamp}")
        for row in lifecycle
        if row.status_after == "ENTRY_TRIGGERED"
    }
    placed_keys = {
        (row.order_id or f"{row.symbol}:{row.timestamp}", row.signal_id or f"{row.symbol}:{row.timestamp}")
        for row in lifecycle
        if row.status_after == "ORDER_PLACED"
    }
    total_orders = len(placed_keys)
    triggered_orders = len(triggered_keys)
    not_triggered_orders = len(waiting_keys - (triggered_keys | placed_keys))
    open_at_end_orders = sum(1 for row in lifecycle if row.status_after == "POSITION_CLOSED" and row.close_reason == "TIMEOUT")
    tp_hits = sum(1 for row in lifecycle if row.status_after == "POSITION_CLOSED" and row.close_reason == "TP_HIT")
    sl_hits = sum(1 for row in lifecycle if row.status_after == "POSITION_CLOSED" and row.close_reason == "SL_HIT")
    return {
        "total_candidates": total_candidates,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "total_orders": total_orders,
        "triggered_orders": triggered_orders,
        "not_triggered_orders": not_triggered_orders,
        "open_at_end_orders": open_at_end_orders,
        "tp_hits": tp_hits,
        "sl_hits": sl_hits,
    }


def _distribution(values: List[Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for value in values:
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda item: item[0]))




def _percentiles(values: List[float], points: List[int]) -> Dict[str, float]:
    if not values:
        return {f"p{pt}": 0.0 for pt in points}
    vals = sorted(float(v) for v in values)
    n = len(vals)
    out: Dict[str, float] = {}
    for pt in points:
        if n == 1:
            out[f"p{pt}"] = round(vals[0], 6)
            continue
        rank = (pt / 100.0) * (n - 1)
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        w = rank - lo
        out[f"p{pt}"] = round(vals[lo] * (1.0 - w) + vals[hi] * w, 6)
    return out

def _value_unavailable(value: Any) -> bool:
    return value is None or value == "" or value == "UNAVAILABLE_BACKTEST"


def _low_score_rescue_watch_fields(reject_reason: str, row: Mapping[str, Any]) -> Dict[str, Any]:
    is_low_score = str(reject_reason or "").upper() == "LOW_SCORE"
    return {
        "rescue_watch_eligible": bool(is_low_score),
        "rescue_watch_reason": ("LOW_SCORE_DIAGNOSTIC_ONLY" if is_low_score else ""),
        "rescued_size_multiplier": (_safe_float(row.get("rescued_size_multiplier"), 0.0) if is_low_score else 0.0),
        "rescue_effective_rr": (_safe_float(row.get("rescued_effective_rr"), 0.0) if is_low_score else 0.0),
        "rescue_reject_reason": (str(row.get("rescue_reject_reason", "")) if is_low_score else ""),
    }


def build_backtest_quality_summary(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    def _normalized_decision(row: Mapping[str, Any]) -> str:
        decision = str(row.get("decision", "") or "").strip().upper()
        if decision:
            return decision
        for key in ("status", "status_after", "status_before", "lifecycle_state"):
            value = str(row.get(key, "") or "").strip().upper()
            if value:
                return value
        return ""

    def _is_metadata_row(row: Mapping[str, Any]) -> bool:
        # Skip only explicit aggregate rows. Plain decision dictionaries are candidates.
        if row.get("metric") is not None and row.get("value") is not None:
            return True
        marker = str(row.get("row_type", "") or "").strip().lower()
        return marker in {"summary", "metadata"}

    candidate_rows = [r for r in rows if not _is_metadata_row(r)]
    signal_created_rows = [r for r in candidate_rows if str(r.get("lifecycle_state", "")).strip().upper() == "SIGNAL_CREATED"]
    if signal_created_rows:
        candidate_rows_for_counts = signal_created_rows
    else:
        candidate_rows_for_counts = candidate_rows
    total = len(candidate_rows_for_counts)

    rejected_tokens = {"REJECTED", "SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"}
    accepted_tokens = {"ACCEPTED", "EXECUTED", "ENTRY_TRIGGERED", "ORDER_PLACED", "PARTIAL_FILL", "FILLED", "TP_HIT", "SL_HIT", "OPEN_AT_END"}

    candidate_signal_ids = {str(r.get("signal_id", "")).strip() for r in candidate_rows_for_counts if str(r.get("signal_id", "")).strip()}
    if signal_created_rows:
        rejected_signal_ids = {
            str(r.get("signal_id", "")).strip()
            for r in candidate_rows
            if (_normalized_decision(r) in rejected_tokens or str(r.get("reject_reason", "") or "").strip() != "")
            and str(r.get("signal_id", "")).strip()
        }
        rejected_rows = []
        reject_reason_by_signal_id = {
            str(r.get("signal_id", "")).strip(): str(r.get("reject_reason", "") or "").strip()
            for r in candidate_rows
            if str(r.get("signal_id", "")).strip() and str(r.get("reject_reason", "") or "").strip()
        }
        for r in signal_created_rows:
            sid = str(r.get("signal_id", "")).strip()
            if str(r.get("reject_reason", "") or "").strip() != "" or sid in rejected_signal_ids or _normalized_decision(r) in rejected_tokens:
                if not str(r.get("reject_reason", "") or "").strip() and sid in reject_reason_by_signal_id:
                    r = {**r, "reject_reason": reject_reason_by_signal_id[sid]}
                rejected_rows.append(r)
        accepted_rows = [
            r for r in signal_created_rows
            if str(r.get("reject_reason", "") or "").strip() == ""
            and str(r.get("signal_id", "")).strip() not in rejected_signal_ids
            and _normalized_decision(r) not in rejected_tokens
        ]
    else:
        rejected_rows = [
            r for r in candidate_rows
            if (_normalized_decision(r) in rejected_tokens or str(r.get("reject_reason", "") or "").strip() != "")
            and (not candidate_signal_ids or str(r.get("signal_id", "")).strip() in candidate_signal_ids)
        ]
        accepted_rows = [
            r for r in candidate_rows
            if _normalized_decision(r) in accepted_tokens
            and (not candidate_signal_ids or str(r.get("signal_id", "")).strip() in candidate_signal_ids)
        ]
    execution_ctx_missing_true = sum(1 for r in candidate_rows if bool(r.get("execution_ctx_missing")))
    effective_rr_diff_count = sum(
        1
        for r in candidate_rows
        if abs(_safe_float(r.get("effective_rr"), 0.0) - _safe_float(r.get("rr"), 0.0)) > 1e-12
    )

    unavailable_counts = {
        "volume_24h_usdt": 0,
        "spread_pct": 0,
        "funding_rate_pct": 0,
        "slippage_pct": 0,
        "latency_ms": 0,
    }
    for row in candidate_rows:
        ctx = row.get("execution_ctx")
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = {}
        if not isinstance(ctx, dict):
            ctx = {}
        if _value_unavailable(ctx.get("volume_24h_usdt")):
            unavailable_counts["volume_24h_usdt"] += 1
        if _value_unavailable(ctx.get("spread_pct")):
            unavailable_counts["spread_pct"] += 1
        if _value_unavailable(ctx.get("funding_rate_pct")):
            unavailable_counts["funding_rate_pct"] += 1
        if "expected_slippage_pct" in ctx and _value_unavailable(ctx.get("expected_slippage_pct")):
            unavailable_counts["slippage_pct"] += 1
        if "latency_ms" in ctx and _value_unavailable(ctx.get("latency_ms")):
            unavailable_counts["latency_ms"] += 1

    score_vals = [_safe_float(r.get("score"), 0.0) for r in candidate_rows]
    raw_rr_vals = [_safe_float(r.get("rr"), 0.0) for r in candidate_rows]
    effective_rr_vals = [_safe_float(r.get("effective_rr"), 0.0) for r in candidate_rows]
    near_threshold = [
        r for r in rejected_rows
        if str(r.get("reject_reason", "")).upper() == "LOW_SCORE" and abs(_safe_float(r.get("score"), 0.0) - 7.5) <= 0.5
    ]
    rescue_rows = [r for r in candidate_rows if str(r.get("accepted_reason", "")).upper() == "HIGH_EFFECTIVE_RR_RESCUE"]
    rescue_closed = [r for r in rescue_rows if str(r.get("lifecycle_state", "")).upper() == "POSITION_CLOSED"]
    baseline_closed = [r for r in candidate_rows if str(r.get("lifecycle_state", "")).upper() == "POSITION_CLOSED" and str(r.get("accepted_reason", "")).upper() != "HIGH_EFFECTIVE_RR_RESCUE"]

    return {
        "total_candidates": total,
        "accepted_count": len(accepted_rows),
        "baseline_accepted_trades": max(len(accepted_rows) - len(rescue_rows), 0),
        "rescue_candidate_count": len(rescue_rows),
        "rescue_accepted_count": len(rescue_rows),
        "rescue_rejected_count": 0,
        "rescue_accepted_would_tp_count": sum(1 for r in rescue_closed if str(r.get("close_reason", "")).upper() == "TP_HIT"),
        "rescue_accepted_would_sl_count": sum(1 for r in rescue_closed if str(r.get("close_reason", "")).upper() == "SL_HIT"),
        "rescue_accepted_net_pnl": sum(_safe_float(r.get("net_pnl_usdt"), 0.0) for r in rescue_closed),
        "baseline_net_pnl": sum(_safe_float(r.get("net_pnl_usdt"), 0.0) for r in baseline_closed),
        "baseline_plus_rescue_net_pnl": sum(_safe_float(r.get("net_pnl_usdt"), 0.0) for r in baseline_closed + rescue_closed),
        "rescue_avg_effective_rr": (sum(_safe_float(r.get("rescue_effective_rr"), 0.0) for r in rescue_rows) / len(rescue_rows)) if rescue_rows else 0.0,
        "rescue_avg_score": (sum(_safe_float(r.get("score"), 0.0) for r in rescue_rows) / len(rescue_rows)) if rescue_rows else 0.0,
        "rescue_reject_reasons": {},
        "accepted_reason_breakdown": _distribution([r.get("accepted_reason", "BASELINE") for r in accepted_rows]),
        "rejected_count": len(rejected_rows),
        "reject_rate": (len(rejected_rows) / total) if total else 0.0,
        "reject_reason_distribution": _distribution([r.get("reject_reason", "") or "" for r in rejected_rows]),
        "score_distribution": _distribution([r.get("score") for r in candidate_rows]),
        "rr_distribution": _distribution([r.get("rr") for r in candidate_rows]),
        "effective_rr_distribution": _distribution([r.get("effective_rr") for r in candidate_rows]),
        "effective_rr_differs_from_rr_count": effective_rr_diff_count,
        "expectancy_bucket_distribution": _distribution([r.get("expectancy_bucket", "UNKNOWN") for r in candidate_rows]),
        "execution_ctx_missing_distribution": {
            "true": execution_ctx_missing_true,
            "false": total - execution_ctx_missing_true,
        },
        "unavailable_execution_context_field_counts": unavailable_counts,
        "score_percentiles": _percentiles(score_vals, [10, 25, 50, 75, 90]),
        "raw_rr_percentiles": _percentiles(raw_rr_vals, [10, 25, 50, 75, 90]),
        "effective_rr_percentiles": _percentiles(effective_rr_vals, [10, 25, 50, 75, 90]),
        "rejection_reason_by_setup_type": _distribution([f"{r.get('setup_type','UNKNOWN')}::{r.get('reject_reason','UNKNOWN')}" for r in rejected_rows]),
        "rejection_reason_by_regime": _distribution([f"{r.get('regime','UNKNOWN')}::{r.get('reject_reason','UNKNOWN')}" for r in rejected_rows]),
        "acceptance_candidates_near_threshold_count": len(near_threshold),
    }


def write_backtest_quality_summary(path: str, summary: Mapping[str, Any]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["metric", "value"])
        w.writeheader()
        for key, value in summary.items():
            serialized = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
            w.writerow({"metric": key, "value": serialized})


def _decode_execution_ctx(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _is_fake_missing_zero(value: Any) -> bool:
    if value in (None, "", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
        return False
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def verify_export_integrity(
    persisted_lifecycle_rows: List[Mapping[str, Any]],
    rejected_rows: List[Mapping[str, Any]],
    lifecycle_csv_rows: List[Mapping[str, Any]],
    rejected_csv_rows: List[Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    rejected_states = {"SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"}
    if len(persisted_lifecycle_rows) != len(lifecycle_csv_rows):
        errors.append("lifecycle row count mismatch between SQLite lifecycle and order_lifecycle.csv")
    if len(rejected_rows) != len(rejected_csv_rows):
        errors.append("rejected row count mismatch between rejected records and rejected_orders.csv")
    lifecycle_reject_count = sum(
        1
        for row in persisted_lifecycle_rows
        if str(row.get("lifecycle_state", "") or "").strip().upper() in rejected_states
    )
    if lifecycle_reject_count != len(rejected_csv_rows):
        errors.append("rejected_orders.csv count mismatch with rejected lifecycle SQL rows")
    signal_terminals: dict[str, set[str]] = {}
    score_by_signal: dict[str, Any] = {}
    rr_by_signal: dict[str, Any] = {}
    for idx, row in enumerate(persisted_lifecycle_rows):
        signal_id = str(row.get("signal_id", "") or f"idx:{idx}")
        lifecycle_state = str(row.get("lifecycle_state", "") or "").strip().upper()
        status_after = str(row.get("status_after", "") or lifecycle_state).strip().upper()
        if lifecycle_state == "" or status_after == "":
            errors.append(f"lifecycle row index={idx} missing lifecycle_state/status_after")
        if lifecycle_state == "CREATED" or status_after == "CREATED":
            errors.append(f"lifecycle row index={idx} uses legacy CREATED state")

        if lifecycle_state == "SIGNAL_REJECTED" and "SYMBOL_REJECTED" in str(row.get("event_id", "") or "").upper():
            errors.append(f"lifecycle row index={idx} mislabels selector reject as SIGNAL_REJECTED")
        if lifecycle_state == "SYMBOL_REJECTED" and signal_id in score_by_signal:
            errors.append(f"signal_id={signal_id} has SYMBOL_REJECTED after signal creation")
        if lifecycle_state == "ORDER_REJECTED" and status_after == "SIGNAL_CREATED":
            errors.append(f"lifecycle row index={idx} has impossible SIGNAL_CREATED -> ORDER_REJECTED status")
        decision = str(row.get("decision", "")).upper()
        reject_reason = str(row.get("reject_reason", "") or "").strip().upper()
        if (decision == "REJECTED" or lifecycle_state in rejected_states) and reject_reason in {"", "UNKNOWN"}:
            errors.append(f"rejected lifecycle row index={idx} missing reject_reason")
        expectancy_bucket = str(row.get("expectancy_bucket", "") or "").strip().upper()
        if expectancy_bucket == "":
            errors.append(f"lifecycle row index={idx} missing expectancy_bucket")
        signal_terminals.setdefault(signal_id, set()).add(lifecycle_state)
        if lifecycle_state == "SIGNAL_CREATED":
            score_by_signal[signal_id] = row.get("score")
            rr_by_signal[signal_id] = row.get("rr")
        ctx = _decode_execution_ctx(row.get("execution_ctx"))
        execution_ctx_missing = str(row.get("execution_ctx_missing", "") or "").strip().lower() in {"1", "true", "t", "yes"}
        if execution_ctx_missing:
            for field in ("volume_24h_usdt", "spread_pct", "funding_rate_pct", "expected_slippage_pct", "liquidity_score"):
                if _is_fake_missing_zero(ctx.get(field)):
                    errors.append(f"lifecycle row index={idx} has fake zero for missing execution field {field}")
    for signal_id, states in signal_terminals.items():
        if states == {"SIGNAL_CREATED"}:
            errors.append(f"signal_id={signal_id} has CREATED-only lifecycle export")
    if len(score_by_signal) >= 3 and len({str(v) for v in score_by_signal.values()}) == 1:
        errors.append("score distribution suspiciously constant across lifecycle candidates")
    if len(rr_by_signal) >= 3 and len({str(v) for v in rr_by_signal.values()}) == 1:
        errors.append("rr distribution suspiciously constant across lifecycle candidates")
    return errors

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
def _bucket_execution_quality(spread_pct: Any, slippage_pct: Any, liquidity_score: Any) -> str:
    spread = _safe_float(spread_pct, -1.0)
    slippage = _safe_float(slippage_pct, -1.0)
    liquidity = _safe_float(liquidity_score, -1.0)
    if spread < 0.0 or slippage < 0.0 or liquidity < 0.0:
        return "UNAVAILABLE"
    if spread <= 0.03 and slippage <= 0.02 and liquidity >= 0.7:
        return "HIGH"
    if spread <= 0.08 and slippage <= 0.05 and liquidity >= 0.4:
        return "MEDIUM"
    return "LOW"


def evaluate_forward_window(
    candidate_row: Mapping[str, Any],
    candles: List[Candle],
    idx: int,
    forward_window_minutes: int = 240,
) -> ForwardWindowEvaluation:
    outcome = simulate_rejected_counterfactual(
        CandidateOrder(
            timestamp=int(candidate_row.get("timestamp", 0)),
            symbol=str(candidate_row.get("symbol", "")),
            side=str(candidate_row.get("side", "LONG")),
            entry=_safe_float(candidate_row.get("entry"), 0.0),
            sl=_safe_float(candidate_row.get("sl"), 0.0),
            tp=_safe_float(candidate_row.get("tp"), 0.0),
            rr=_safe_float(candidate_row.get("rr"), 0.0),
            setup_type=str(candidate_row.get("setup_type", "")),
            setup_reason=str(candidate_row.get("setup_reason", "")),
            regime=str(candidate_row.get("regime", "")),
            score=_safe_float(candidate_row.get("score"), 0.0),
            order_type=str(candidate_row.get("order_type", "LIMIT")),
        ),
        candles,
        idx,
        timeout_bars=forward_window_minutes,
    )
    entry = max(_safe_float(candidate_row.get("entry"), 0.0), 1e-9)
    mfe_pct = (float(outcome["max_favorable_excursion"]) / entry) * 100.0
    mae_pct = (float(outcome["max_adverse_excursion"]) / entry) * 100.0
    decision = str(candidate_row.get("decision", "") or "").upper()
    lifecycle_state = str(candidate_row.get("status_after", candidate_row.get("lifecycle_state", "")) or "").upper()
    reject_reason = str(candidate_row.get("reject_reason", "") or "")
    is_rejected = decision == "REJECTED" or lifecycle_state in {"SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"} or reject_reason != ""
    reject_correct = None
    reject_missed_winner = False
    reject_saved_from_loss = False
    if is_rejected:
        reject_missed_winner = bool(outcome["would_tp_hit"]) and not bool(outcome["would_sl_hit"])
        reject_saved_from_loss = bool(outcome["would_sl_hit"]) and not bool(outcome["would_tp_hit"])
        reject_correct = reject_saved_from_loss
    return ForwardWindowEvaluation(
        signal_id=str(candidate_row.get("signal_id", f"{candidate_row.get('symbol','')}:{candidate_row.get('timestamp','')}")),
        symbol=str(candidate_row.get("symbol", "")),
        decision=("REJECTED" if is_rejected else ("ACCEPTED" if decision == "ACCEPTED" else decision)),
        lifecycle_state=lifecycle_state,
        reject_reason=reject_reason,
        setup_type=str(candidate_row.get("setup_type", "UNKNOWN")),
        score=_safe_float(candidate_row.get("score"), 0.0),
        rr=_safe_float(candidate_row.get("rr"), 0.0),
        effective_rr=_safe_float(candidate_row.get("effective_rr"), _safe_float(candidate_row.get("rr"), 0.0)),
        predicted_quality=1.0 if _bucket_execution_quality(
            candidate_row.get("spread_pct"),
            candidate_row.get("slippage_pct", candidate_row.get("expected_slippage_pct")),
            candidate_row.get("liquidity_score"),
        ) == "HIGH" else 0.0,
        forward_window_minutes=forward_window_minutes,
        would_have_hit_tp=bool(outcome["would_tp_hit"]),
        would_have_hit_sl=bool(outcome["would_sl_hit"]),
        mfe_pct=round(mfe_pct, 8),
        mae_pct=round(mae_pct, 8),
        max_forward_return=round(mfe_pct, 8),
        max_adverse_return=round(-mae_pct, 8),
        reject_correct=reject_correct,
        reject_missed_winner=reject_missed_winner,
        reject_saved_from_loss=reject_saved_from_loss,
        forward_window_regime=str(candidate_row.get("regime", "UNKNOWN")),
        execution_quality_bucket=_bucket_execution_quality(
            candidate_row.get("spread_pct"),
            candidate_row.get("slippage_pct", candidate_row.get("expected_slippage_pct")),
            candidate_row.get("liquidity_score"),
        ),
    )
def _realized_outcome_from_row(row: Mapping[str, Any]) -> str:
    state = str(row.get("lifecycle_state", row.get("status_after", "")) or "").upper()
    if state in {"SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"}:
        return "REJECTED"
    if state in {"EXPIRED", "CANCELED", "CANCELLED"}:
        return "CANCELED" if state != "EXPIRED" else "EXPIRED"
    close_reason = str(row.get("close_reason", "") or "").upper()
    if close_reason in {"TP_HIT", "SL_HIT"}:
        return close_reason
    if close_reason in {"TIMEOUT", "OPEN_AT_END"}:
        return "OPEN_AT_END"
    return "NON_TERMINAL"


def build_forward_evaluation_rows(
    lifecycle_rows: List[Mapping[str, Any]],
    candles_by_symbol: Mapping[str, List[Candle]],
    forward_window_minutes: int = 240,
) -> List[dict[str, Any]]:
    out: List[dict[str, Any]] = []
    terminal = {"TP_HIT", "SL_HIT", "EXPIRED", "CANCELED", "OPEN_AT_END", "REJECTED"}
    for row in lifecycle_rows:
        realized = _realized_outcome_from_row(row)
        if realized not in terminal:
            continue
        symbol = str(row.get("symbol", ""))
        ts = int(_safe_float(row.get("timestamp", row.get("event_ts")), 0.0))
        candles = candles_by_symbol.get(symbol, [])
        idx = next((i for i, c in enumerate(candles) if c.timestamp >= ts), len(candles))
        evaluation = evaluate_forward_window(row, candles, idx, forward_window_minutes=forward_window_minutes)
        out.append({**asdict(evaluation), "realized_outcome": realized})
    return out


def _bucket_numeric(value: float, bounds: list[float], labels: list[str]) -> str:
    for idx, bound in enumerate(bounds):
        if value <= bound:
            return labels[idx]
    return labels[-1]


def build_forward_evaluations_from_lifecycle(
    lifecycle: List[LifecycleRow],
    candles_by_symbol: Mapping[str, List[Candle]],
    forward_window_minutes: int = 240,
) -> List[ForwardWindowEvaluation]:
    out: List[ForwardWindowEvaluation] = []
    for row in lifecycle:
        if row.status_after != "POSITION_CLOSED":
            continue
        if row.close_reason not in TERMINAL_FORWARD_CLOSE_REASONS:
            continue
        candles = candles_by_symbol.get(row.symbol, [])
        idx = next((i for i, c in enumerate(candles) if c.timestamp >= row.timestamp), len(candles))
        out.append(
            evaluate_forward_window(
                {
                    "signal_id": f"{row.symbol}:{row.timestamp}",
                    "timestamp": row.timestamp,
                    "symbol": row.symbol,
                    "side": row.side,
                    "entry": row.entry,
                    "sl": row.sl,
                    "tp": row.tp,
                    "rr": row.rr,
                    "score": row.score,
                    "regime": row.regime,
                    "setup_type": row.setup_type,
                    "reject_reason": row.reject_reason,
                    "decision": "ACCEPTED",
                    "lifecycle_state": row.status_after,
                    "spread_pct": row.spread_pct,
                    "expected_slippage_pct": row.expected_slippage_pct,
                    "liquidity_score": row.liquidity_score,
                },
                candles,
                idx,
                forward_window_minutes=forward_window_minutes,
            )
        )
    return out


def persist_calibration_snapshots(evals: List[ForwardWindowEvaluation], lifecycle_index: Mapping[str, LifecycleRow]) -> List[dict[str, Any]]:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        for ev in evals:
            src = lifecycle_index.get(ev.signal_id)
            session.execute(text("""
                INSERT INTO calibration_snapshots (
                    signal_id,predicted_quality,realized_outcome,score,rr,effective_rr,regime,setup_type,rejection_reason,
                    forward_window_minutes,mfe_pct,mae_pct,would_have_hit_tp,would_have_hit_sl,reject_correct,created_at
                ) VALUES (
                    :signal_id,:predicted_quality,:realized_outcome,:score,:rr,:effective_rr,:regime,:setup_type,:rejection_reason,
                    :forward_window_minutes,:mfe_pct,:mae_pct,:would_have_hit_tp,:would_have_hit_sl,:reject_correct,:created_at
                )
                ON CONFLICT(signal_id, forward_window_minutes) DO NOTHING
            """), {
                "signal_id": ev.signal_id,
                "predicted_quality": (src.score if src is not None else None),
                "realized_outcome": ("WOULD_TP" if ev.would_have_hit_tp else ("WOULD_SL" if ev.would_have_hit_sl else "NEUTRAL")),
                "score": (src.score if src is not None else None),
                "rr": (src.rr if src is not None else None),
                "effective_rr": (src.effective_rr if src is not None else (src.rr if src is not None else None)),
                "regime": ev.forward_window_regime,
                "setup_type": (src.setup_type if src is not None else None),
                "rejection_reason": ev.reject_reason,
                "forward_window_minutes": ev.forward_window_minutes,
                "mfe_pct": ev.mfe_pct,
                "mae_pct": ev.mae_pct,
                "would_have_hit_tp": int(ev.would_have_hit_tp),
                "would_have_hit_sl": int(ev.would_have_hit_sl),
                "reject_correct": (None if ev.reject_correct is None else int(ev.reject_correct)),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        rows = session.execute(text("SELECT * FROM calibration_snapshots ORDER BY signal_id, forward_window_minutes")).mappings().all()
    return [dict(r) for r in rows]
    entry = max(_safe_float(candidate_row.get("entry"), 0.0), 1e-9)
    mfe_pct = (float(outcome["max_favorable_excursion"]) / entry) * 100.0
    mae_pct = (float(outcome["max_adverse_excursion"]) / entry) * 100.0
    decision = str(candidate_row.get("decision", "") or "").upper()
    lifecycle_state = str(candidate_row.get("status_after", candidate_row.get("lifecycle_state", "")) or "").upper()
    reject_reason = str(candidate_row.get("reject_reason", "") or "")
    is_rejected = decision == "REJECTED" or lifecycle_state in {"SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"} or reject_reason != ""
    reject_correct = None
    reject_missed_winner = False
    reject_saved_from_loss = False
    if is_rejected:
        reject_missed_winner = bool(outcome["would_tp_hit"]) and not bool(outcome["would_sl_hit"])
        reject_saved_from_loss = bool(outcome["would_sl_hit"]) and not bool(outcome["would_tp_hit"])
        reject_correct = reject_saved_from_loss
    return ForwardWindowEvaluation(
        signal_id=str(candidate_row.get("signal_id", f"{candidate_row.get('symbol','')}:{candidate_row.get('timestamp','')}")),
        symbol=str(candidate_row.get("symbol", "")),
        decision=("REJECTED" if is_rejected else ("ACCEPTED" if decision == "ACCEPTED" else decision)),
        lifecycle_state=lifecycle_state,
        reject_reason=reject_reason,
        forward_window_minutes=forward_window_minutes,
        would_have_hit_tp=bool(outcome["would_tp_hit"]),
        would_have_hit_sl=bool(outcome["would_sl_hit"]),
        mfe_pct=round(mfe_pct, 8),
        mae_pct=round(mae_pct, 8),
        max_forward_return=round(mfe_pct, 8),
        max_adverse_return=round(-mae_pct, 8),
        reject_correct=reject_correct,
        reject_missed_winner=reject_missed_winner,
        reject_saved_from_loss=reject_saved_from_loss,
        forward_window_regime=str(candidate_row.get("regime", "UNKNOWN")),
        execution_quality_bucket=_bucket_execution_quality(
            candidate_row.get("spread_pct"),
            candidate_row.get("slippage_pct", candidate_row.get("expected_slippage_pct")),
            candidate_row.get("liquidity_score"),
        ),
    )
def _is_actionable_rejected_order(row: Mapping[str, Any]) -> bool:
    if row.get("setup_reason") == "SYMBOL_SELECTOR":
        return False
    if row.get("event_flags") == "SYMBOL_SELECTOR":
        return False
    if row.get("side") in {"N/A", "", None}:
        return False
    for key in ("entry", "sl", "tp"):
        if key not in row:
            return False
        if _safe_float(row.get(key), 0.0) <= 0.0:
            return False
    return True
def evaluate_rejected_shadow(
    candidate_row: Mapping[str, Any],
    candles: List[Candle],
    idx: int,
) -> RejectedShadowEvaluation:
    rr = _safe_float(candidate_row.get("rr"), 0.0)
    spread_pct, _ = normalize_pct_input(candidate_row.get("spread_pct"), field="spread_pct")
    liquidity_score = _safe_float(candidate_row.get("liquidity_score"), 1.0)
    volatility_score = _safe_float(candidate_row.get("volatility_score"), spread_pct)
    effective_rr, _, penalty_breakdown = _execution_reject_flags(
        rr,
        {
            "spread_pct": spread_pct,
            "expected_slippage_pct": _safe_float(candidate_row.get("expected_slippage_pct"), 0.0),
            "liquidity_score": liquidity_score,
        },
    )
    counterfactual = simulate_rejected_counterfactual(
        CandidateOrder(
            timestamp=int(candidate_row.get("timestamp", 0)),
            symbol=str(candidate_row.get("symbol", "")),
            side=str(candidate_row.get("side", "LONG")),
            entry=_safe_float(candidate_row.get("entry"), 0.0),
            sl=_safe_float(candidate_row.get("sl"), 0.0),
            tp=_safe_float(candidate_row.get("tp"), 0.0),
            rr=rr,
            setup_type=str(candidate_row.get("setup_type", "")),
            setup_reason=str(candidate_row.get("setup_reason", "")),
            regime=str(candidate_row.get("regime", "")),
            score=_safe_float(candidate_row.get("score"), 0.0),
            order_type=str(candidate_row.get("order_type", "LIMIT")),
        ),
        candles,
        idx,
    )
    liquidity_ok = liquidity_score >= 0.3
    volatility_ok = volatility_score <= 5.0
    cost_penalty = _safe_float(penalty_breakdown.get("cost_penalty_total"), max(rr - effective_rr, 0.0))
    effective_tp_hit = (
        counterfactual["outcome"] == "WOULD_TP"
        and effective_rr >= 1.1
        and liquidity_ok
        and volatility_ok
    )
    low_score_gate_score = _safe_float(candidate_row.get("gate_score"), _safe_float(candidate_row.get("score"), 0.0))
    min_required_score = _safe_float(candidate_row.get("min_required_score"), 7.5)
    rescue_attempted = False
    rescue_passed = False
    rescued_stop_loss = _safe_float(candidate_row.get("sl"), 0.0)
    rescued_effective_rr = 0.0
    rescued_size_multiplier = 0.0
    rescue_reject_reason = ""
    if str(candidate_row.get("reject_reason", "")).upper() == "STOP_TOO_WIDE":
        setup_type = str(candidate_row.get("setup_type", "")).upper()
        regime = str(candidate_row.get("regime", "")).upper()
        score_val = _safe_float(candidate_row.get("score"), 0.0)
        spread_ok = spread_pct <= 0.05
        if ("BREAKOUT" in setup_type or regime == "BREAKOUT") and score_val >= 8.0 and effective_rr >= 1.2 and liquidity_ok and spread_ok:
            rescue_attempted = True
            entry = _safe_float(candidate_row.get("entry"), 0.0)
            sl = _safe_float(candidate_row.get("sl"), 0.0)
            risk = abs(entry - sl)
            max_risk = entry * 0.015
            reduced_risk = min(risk, max_risk)
            if risk > 0.0 and reduced_risk > 0.0:
                rescued_stop_loss = (entry - reduced_risk) if str(candidate_row.get("side", "LONG")).upper() == "LONG" else (entry + reduced_risk)
                rescued_rr = abs(_safe_float(candidate_row.get("tp"), entry) - entry) / reduced_risk
                rescued_effective_rr, _, _ = _execution_reject_flags(rescued_rr, {"spread_pct": spread_pct, "expected_slippage_pct": _safe_float(candidate_row.get("expected_slippage_pct"), 0.0), "liquidity_score": liquidity_score})
                stop_width_pct = (reduced_risk / max(entry, 1e-9)) * 100.0
                rescued_size_multiplier = min(0.5, max_risk / max(risk, 1e-9))
                if rescued_effective_rr >= 1.2 and stop_width_pct <= 1.5 and rescued_size_multiplier < 1.0:
                    rescue_passed = True
                else:
                    rescue_reject_reason = "LOW_RESCUED_EFFECTIVE_RR" if rescued_effective_rr < 1.2 else "RESCUED_STOP_STILL_WIDE"
            else:
                rescue_reject_reason = "INVALID_RESCUE_GEOMETRY"

    return RejectedShadowEvaluation(
        symbol=str(candidate_row.get("symbol", "")),
        timestamp=int(candidate_row.get("timestamp", 0)),
        side=str(candidate_row.get("side", "LONG")),
        entry=_safe_float(candidate_row.get("entry"), 0.0),
        stop_loss=_safe_float(candidate_row.get("sl"), 0.0),
        take_profit=_safe_float(candidate_row.get("tp"), 0.0),
        raw_rr=rr,
        effective_rr=effective_rr,
        reject_reasons=str(candidate_row.get("reject_reason", "UNKNOWN")),
        score=_safe_float(candidate_row.get("score"), 0.0),
        regime=str(candidate_row.get("regime", "UNKNOWN")),
        spread_pct=spread_pct,
        liquidity_score=liquidity_score,
        volatility_score=volatility_score,
        shadow_outcome=counterfactual["outcome"],
        effective_tp_hit=effective_tp_hit,
        cost_penalty=cost_penalty,
        liquidity_ok=liquidity_ok,
        volatility_ok=volatility_ok,
        low_score_gate_score=low_score_gate_score,
        rescue_attempted=rescue_attempted,
        rescue_passed=rescue_passed,
        rescued_stop_loss=rescued_stop_loss,
        rescued_effective_rr=rescued_effective_rr,
        rescued_size_multiplier=rescued_size_multiplier,
        rescue_reject_reason=rescue_reject_reason,
        setup_type=str(candidate_row.get("setup_type") or "UNAVAILABLE"),
        expected_slippage_pct=(candidate_row.get("expected_slippage_pct") if candidate_row.get("expected_slippage_pct") not in (None, "") else "UNAVAILABLE"),
        stop_distance_pct=(abs(_safe_float(candidate_row.get("entry"), 0.0) - _safe_float(candidate_row.get("sl"), 0.0)) / max(_safe_float(candidate_row.get("entry"), 0.0), 1e-9) * 100.0 if _safe_float(candidate_row.get("entry"), 0.0) > 0.0 and _safe_float(candidate_row.get("sl"), 0.0) > 0.0 else "UNAVAILABLE"),
    )

def _quality_float(value: Any) -> Optional[float]:
    if value in (None, "", "None", "null", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _decile(value: Any) -> str:
    numeric = _quality_float(value)
    if numeric is None:
        return "UNAVAILABLE"
    return f"D{min(10, max(1, int(numeric * 10) + 1))}" if 0.0 <= numeric <= 1.0 else f"D{min(10, max(1, int(numeric) + 1))}"

def _bucket_quality(value: Any, cuts: Iterable[float], labels: List[str]) -> str:
    numeric = _quality_float(value)
    if numeric is None:
        return "UNAVAILABLE"
    for cut, label in zip(cuts, labels):
        if numeric <= cut:
            return label
    return labels[-1]

def _outcome_from_lifecycle(row: LifecycleRow) -> str:
    reason = str(row.close_reason or row.status_after or "").upper()
    if reason == "TP_HIT" or row.would_tp_hit:
        return "WOULD_TP"
    if reason == "SL_HIT" or row.would_sl_hit:
        return "WOULD_SL"
    if reason in {"OPEN_AT_END", "TIMEOUT"}:
        return "WOULD_TIMEOUT"
    return "UNKNOWN"

def _quality_record_from_lifecycle(row: LifecycleRow, timeframe: str) -> Dict[str, Any]:
    stop_distance = "UNAVAILABLE"
    if row.entry > 0 and row.sl > 0:
        stop_distance = abs(row.entry - row.sl) / row.entry * 100.0
    return {"source": "ACCEPTED", "reject_reason": "ACCEPTED", "symbol": row.symbol or "UNKNOWN", "side": row.side or "UNKNOWN", "regime": row.regime or "UNKNOWN", "setup_type": row.setup_type or "UNAVAILABLE", "timeframe": timeframe, "score": row.score, "raw_rr": row.rr, "effective_rr": row.effective_rr if row.effective_rr is not None else row.rr, "decision_cost_penalty": row.cost_penalty, "shadow_cost_penalty": "UNAVAILABLE", "spread_pct": row.spread_pct, "expected_slippage_pct": row.expected_slippage_pct, "volatility_score": row.volatility_score, "liquidity_score": row.liquidity_score, "stop_distance_pct": stop_distance, "outcome": _outcome_from_lifecycle(row)}

def _quality_record_from_shadow(s: RejectedShadowEvaluation, timeframe: str) -> Dict[str, Any]:
    return {"source": "REJECTED_SHADOW", "timestamp": s.timestamp, "reject_reason": s.reject_reasons or "UNKNOWN", "symbol": s.symbol or "UNKNOWN", "side": s.side or "UNKNOWN", "regime": s.regime or "UNKNOWN", "setup_type": s.setup_type or "UNAVAILABLE", "timeframe": timeframe, "score": s.score, "raw_rr": s.raw_rr, "effective_rr": s.effective_rr, "decision_cost_penalty": "UNAVAILABLE", "shadow_cost_penalty": s.cost_penalty, "spread_pct": s.spread_pct, "expected_slippage_pct": s.expected_slippage_pct, "volatility_score": s.volatility_score, "liquidity_score": s.liquidity_score, "liquidity_ok": s.liquidity_ok, "volatility_ok": s.volatility_ok, "stop_distance_pct": s.stop_distance_pct, "outcome": s.shadow_outcome or "UNKNOWN"}

def _quality_gate_metrics(records: List[Dict[str, Any]], cfg: QualityGateConfig, baseline_net_pnl: float = 0.0) -> Dict[str, Any]:
    rejected = [r for r in records if r.get("source") == "REJECTED_SHADOW"]
    mode_ok = "BACKTEST" in {m.upper() for m in cfg.modes}
    candidates: List[Dict[str, Any]] = []
    rejected_reasons: Dict[str, int] = {}
    daily_counts: Dict[str, int] = {}
    for r in rejected:
        if "stop_distance_pct_bucket" not in r:
            r["stop_distance_pct_bucket"] = _bucket_quality(r.get("stop_distance_pct"), [0.75, 1.5], ["TIGHT", "NORMAL", "WIDE"])
        reason = str(r.get("reject_reason", "")).upper()
        eff = _quality_float(r.get("effective_rr"))
        score = _quality_float(r.get("score"))
        spread = _quality_float(r.get("spread_pct"))
        slip = _quality_float(r.get("expected_slippage_pct"))
        stop_bucket = str(r.get("stop_distance_pct_bucket") or "").upper()
        blocked = (
            not cfg.enabled or not mode_ok or cfg.allowed_gate_name != QUALITY_GATE_NAME
            or str(r.get("side", "")).upper() != "SHORT"
            or str(r.get("setup_type", "")).upper() != "BREAKDOWN_DOWN"
            or str(r.get("regime", "")).upper() != "BREAKOUT"
            or stop_bucket != "NORMAL"
            or reason not in set(cfg.allowed_reasons)
            or reason == "REGIME_MISMATCH"
            or str(r.get("regime", "")).upper() in {"PANIC", "NEWS_DRIVEN"}
            or r.get("liquidity_ok") is False
            or r.get("volatility_ok") is False
            or str(r.get("outcome") or "").upper() != "WOULD_TP"
            or eff is None or eff < cfg.min_effective_rr
            or (cfg.min_score is not None and (score is None or score < cfg.min_score))
            or spread is None or spread > cfg.max_spread_pct
            or slip is None or slip > cfg.max_slippage_pct
        )
        if blocked:
            if cfg.enabled:
                rejected_reasons[reason or "NOT_ELIGIBLE"] = rejected_reasons.get(reason or "NOT_ELIGIBLE", 0) + 1
            continue
        day_key = str(r.get("timestamp") or "BACKTEST_DAY")
        if daily_counts.get(day_key, 0) >= cfg.max_trades_per_day:
            rejected_reasons["DAILY_QUALITY_GATE_LIMIT"] = rejected_reasons.get("DAILY_QUALITY_GATE_LIMIT", 0) + 1
            continue
        daily_counts[day_key] = daily_counts.get(day_key, 0) + 1
        candidates.append(r)
    tp = sum(1 for r in candidates if r.get("outcome") == "WOULD_TP")
    sl = sum(1 for r in candidates if r.get("outcome") == "WOULD_SL")
    unknown = sum(1 for r in candidates if r.get("outcome") not in {"WOULD_TP", "WOULD_SL"})
    eff_vals = [_quality_float(r.get("effective_rr")) for r in candidates]
    eff_vals = [v for v in eff_vals if v is not None]
    expected = (sum((( _quality_float(r.get("effective_rr")) or 0.0) if r.get("outcome") == "WOULD_TP" else (-1.0 if r.get("outcome") == "WOULD_SL" else 0.0)) for r in candidates) / len(candidates)) if candidates else 0.0
    sized_expected = expected * cfg.size_multiplier * len(candidates)
    return {
        "quality_gate_enabled": cfg.enabled,
        "quality_gate_mode": ",".join(cfg.modes),
        "quality_gate_candidate_count": len(candidates),
        "quality_gate_accepted_count": len(candidates),
        "quality_gate_rejected_count": sum(rejected_reasons.values()),
        "quality_gate_would_tp_count": tp,
        "quality_gate_would_sl_count": sl,
        "quality_gate_unknown_count": unknown,
        "quality_gate_tp_rate": tp / (tp + sl) if (tp + sl) else 0.0,
        "quality_gate_mean_effective_rr": (sum(eff_vals) / len(eff_vals)) if eff_vals else 0.0,
        "quality_gate_expected_effective_expectancy": expected,
        "baseline_plus_quality_gate_net_pnl": baseline_net_pnl + sized_expected,
        "quality_gate_size_multiplier": cfg.size_multiplier,
        "quality_gate_reason_breakdown": _distribution([r.get("reject_reason") for r in candidates]),
        "quality_gate_symbol_breakdown": _distribution([r.get("symbol") for r in candidates]),
        "quality_gate_daily_trade_count_distribution": _distribution(daily_counts.values()),
        "quality_gate_rejected_reason_breakdown": rejected_reasons,
    }

def build_signal_quality_diagnostics(accepted_rows: List[LifecycleRow], shadows: List[RejectedShadowEvaluation], timeframe: str, thresholds: List[float] | None = None, quality_gate_config: Optional[QualityGateConfig] = None, baseline_net_pnl: float = 0.0) -> tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    thresholds = thresholds or [1.7, 1.9, 2.1, 2.3]
    records = [_quality_record_from_lifecycle(r, timeframe) for r in accepted_rows] + [_quality_record_from_shadow(s, timeframe) for s in shadows]
    for r in records:
        r["score_decile"] = _decile(r.get("score"))
        r["effective_rr_decile"] = _decile(r.get("effective_rr"))
        r["volatility_score_bucket"] = _bucket_quality(r.get("volatility_score"), [0.3, 0.7], ["LOW", "MEDIUM", "HIGH"])
        r["liquidity_score_bucket"] = _bucket_quality(r.get("liquidity_score"), [0.3, 0.7], ["THIN", "NORMAL", "DEEP"])
        r["spread_pct_bucket"] = _bucket_quality(r.get("spread_pct"), [0.03, 0.08], ["TIGHT", "NORMAL", "WIDE"])
        r["expected_slippage_pct_bucket"] = _bucket_quality(r.get("expected_slippage_pct"), [0.01, 0.05], ["LOW", "MEDIUM", "HIGH"])
        r["stop_distance_pct_bucket"] = _bucket_quality(r.get("stop_distance_pct"), [0.75, 1.5], ["TIGHT", "NORMAL", "WIDE"])
    dims = ["reject_reason", "symbol", "side", "regime", "setup_type", "timeframe", "score_decile", "effective_rr_decile", "volatility_score_bucket", "liquidity_score_bucket", "spread_pct_bucket", "expected_slippage_pct_bucket", "stop_distance_pct_bucket"]
    group_rows=[]
    for dim in dims:
        keys=sorted({str(r.get(dim) or "UNAVAILABLE") for r in records})
        for key in keys:
            rows=[r for r in records if str(r.get(dim) or "UNAVAILABLE")==key]
            n=len(rows); outcomes= {o: sum(1 for r in rows if r.get("outcome")==o) for o in ["WOULD_TP","WOULD_SL","WOULD_TIMEOUT","UNKNOWN"]}
            def mean(f):
                vals=[_quality_float(r.get(f)) for r in rows]; vals=[v for v in vals if v is not None]
                return (sum(vals)/len(vals)) if vals else None
            group_rows.append({"group_field":dim,"group_value":key,"count":n, **{f"{o.lower()}_count":c for o,c in outcomes.items()}, **{f"{o.lower()}_rate":(c/n if n else 0.0) for o,c in outcomes.items()}, "mean_score":mean("score"), "mean_raw_rr":mean("raw_rr"), "mean_effective_rr":mean("effective_rr"), "mean_decision_cost_penalty":mean("decision_cost_penalty"), "mean_shadow_cost_penalty":mean("shadow_cost_penalty"), "mean_spread_pct":mean("spread_pct"), "mean_expected_slippage_pct":mean("expected_slippage_pct")})
    def split(rows): return {"count":len(rows), "would_tp_count":sum(1 for r in rows if r.get("outcome")=="WOULD_TP"), "would_sl_count":sum(1 for r in rows if r.get("outcome")=="WOULD_SL"), "would_timeout_count":sum(1 for r in rows if r.get("outcome")=="WOULD_TIMEOUT"), "unknown_count":sum(1 for r in rows if r.get("outcome") not in {"WOULD_TP","WOULD_SL","WOULD_TIMEOUT"})}
    def mean(rows, f):
        vals=[_quality_float(r.get(f)) for r in rows]; vals=[v for v in vals if v is not None]
        return (sum(vals)/len(vals)) if vals else None
    def outcome_metrics(rows):
        n=len(rows); base=split(rows)
        return {**base, "would_tp_rate":base["would_tp_count"]/n if n else 0.0, "would_sl_rate":base["would_sl_count"]/n if n else 0.0, "would_timeout_rate":base["would_timeout_count"]/n if n else 0.0, "unknown_rate":base["unknown_count"]/n if n else 0.0}
    combo_rows=[]
    combo_dims=[("side","regime"),("side","setup_type"),("regime","setup_type"),("side","regime","setup_type"),("side","regime","setup_type","stop_distance_pct_bucket"),("side","regime","setup_type","effective_rr_threshold_bucket")]
    for r in records:
        eff=_quality_float(r.get("effective_rr"))
        r["effective_rr_threshold_bucket"] = "UNAVAILABLE" if eff is None else (">=2.3" if eff>=2.3 else ">=2.1" if eff>=2.1 else ">=1.9" if eff>=1.9 else ">=1.7" if eff>=1.7 else "<1.7")
    for dims_tuple in combo_dims:
        keys=sorted({tuple(str(r.get(d) or "UNAVAILABLE") for d in dims_tuple) for r in records})
        for key in keys:
            rows=[r for r in records if tuple(str(r.get(d) or "UNAVAILABLE") for d in dims_tuple)==key]
            metrics=outcome_metrics(rows)
            combo_rows.append({"grouping":"+".join(dims_tuple), **{d:v for d,v in zip(dims_tuple,key)}, "count":len(rows), **metrics, "mean_score":mean(rows,"score"), "mean_raw_rr":mean(rows,"raw_rr"), "mean_effective_rr":mean(rows,"effective_rr"), "mean_shadow_cost_penalty":mean(rows,"shadow_cost_penalty"), "mean_spread_pct":mean(rows,"spread_pct"), "mean_expected_slippage_pct":mean(rows,"expected_slippage_pct"), "mean_stop_distance_pct":mean(rows,"stop_distance_pct")})
    rejected=[r for r in records if r.get("source")=="REJECTED_SHADOW"]
    gate_defs={
        QUALITY_GATE_NAME: lambda r: r.get("side")=="SHORT" and r.get("setup_type")=="BREAKDOWN_DOWN" and r.get("regime")=="BREAKOUT" and r.get("stop_distance_pct_bucket")=="NORMAL",
        "SHORT_BREAKDOWN_BREAKOUT_GATE": lambda r: r.get("side")=="SHORT" and r.get("setup_type")=="BREAKDOWN_DOWN" and r.get("regime")=="BREAKOUT",
        "LONG_BREAKOUT_STRICT_GATE": lambda r: r.get("side")=="LONG" and r.get("setup_type")=="BREAKOUT_UP" and (_quality_float(r.get("effective_rr")) or 0)>=1.9 and (_quality_float(r.get("stop_distance_pct")) or 999)<=1.5,
        "HIGH_EFFECTIVE_RR_SHORT_GATE": lambda r: r.get("side")=="SHORT" and (_quality_float(r.get("effective_rr")) or 0)>=1.9,
        "STOP_TOO_WIDE_RECOVERABLE_GATE": lambda r: str(r.get("reject_reason")).upper()=="STOP_TOO_WIDE" and (_quality_float(r.get("effective_rr")) or 0)>=1.7 and (_quality_float(r.get("stop_distance_pct")) or 999)<=3.0,
    }
    gate_allowed={QUALITY_GATE_NAME:["LOW_SCORE","STOP_TOO_WIDE","DAILY_SYMBOL_TRADE_LIMIT"],"SHORT_BREAKDOWN_BREAKOUT_GATE":["LOW_SCORE","STOP_TOO_WIDE","DAILY_SYMBOL_TRADE_LIMIT","REGIME_MISMATCH"],"LONG_BREAKOUT_STRICT_GATE":["LOW_SCORE","STOP_TOO_WIDE"],"HIGH_EFFECTIVE_RR_SHORT_GATE":["LOW_SCORE","STOP_TOO_WIDE","DAILY_SYMBOL_TRADE_LIMIT"],"STOP_TOO_WIDE_RECOVERABLE_GATE":["STOP_TOO_WIDE"]}
    gate_rows=[]
    for name, pred in gate_defs.items():
        rows=[r for r in rejected if pred(r)]
        metrics=outcome_metrics(rows)
        gate_rows.append({"gate_name":name,"reporting_only":True,"candidate_count":len(rows),**{k:v for k,v in metrics.items() if k!="count"},"mean_effective_rr":mean(rows,"effective_rr"),"expected_effective_expectancy":(sum(((_quality_float(r.get("effective_rr")) or 0.0) if r.get("outcome")=="WOULD_TP" else (-1.0 if r.get("outcome")=="WOULD_SL" else 0.0)) for r in rows)/len(rows) if rows else 0.0),"allowed_reject_reasons":"|".join(gate_allowed[name]),"rejected_by_reason":json.dumps(_distribution([r.get("reject_reason") for r in rows]), sort_keys=True)})
    calibration_rows=[]
    for dims_tuple in [("score_decile","side"),("score_decile","regime"),("score_decile","setup_type")]:
        keys=sorted({tuple(str(r.get(d) or "UNAVAILABLE") for d in dims_tuple) for r in records})
        for key in keys:
            rows=[r for r in records if tuple(str(r.get(d) or "UNAVAILABLE") for d in dims_tuple)==key]
            calibration_rows.append({"diagnostic":"by_"+dims_tuple[1], **{d:v for d,v in zip(dims_tuple,key)}, **outcome_metrics(rows)})
    d10=[r for r in records if r.get("score_decile")=="D10"]
    for dim in ["reject_reason","stop_distance_pct_bucket"]:
        for key in sorted({str(r.get(dim) or "UNAVAILABLE") for r in d10}):
            rows=[r for r in d10 if str(r.get(dim) or "UNAVAILABLE")==key]
            calibration_rows.append({"diagnostic":"d10_by_"+dim, dim:key, **outcome_metrics(rows)})
    score10=[r for r in records if _quality_float(r.get("score")) == 10.0]
    stop=[r for r in records if str(r.get("reject_reason")).upper()=="STOP_TOO_WIDE"]
    missed=[]
    for t in thresholds:
        rows=[r for r in rejected if (_quality_float(r.get("effective_rr")) or -1) >= t]
        missed.append({"effective_rr_threshold":t, **split(rows)})
    summary={"total_records":len(records), "accepted_records":sum(1 for r in records if r.get("source")=="ACCEPTED"), "rejected_shadow_records":len(shadows), "score_saturation":{"score_10_count":len(score10), "score_10_rate":len(score10)/len(records) if records else 0.0, "score_10_would_tp_count":sum(1 for r in score10 if r.get("outcome")=="WOULD_TP"), "score_10_would_sl_count":sum(1 for r in score10 if r.get("outcome")=="WOULD_SL"), "score_10_by_reject_reason":{k:split([r for r in score10 if str(r.get("reject_reason"))==k]) for k in sorted({str(r.get("reject_reason")) for r in score10})}, "score_10_by_regime":{k:split([r for r in score10 if str(r.get("regime"))==k]) for k in sorted({str(r.get("regime")) for r in score10})}}, "stop_too_wide_split":{"would_tp":split([r for r in stop if r.get("outcome")=="WOULD_TP"]), "would_sl":split([r for r in stop if r.get("outcome")=="WOULD_SL"]), "metrics":[g for g in group_rows if g["group_field"] in {"symbol","side","regime","effective_rr_decile","score_decile","volatility_score_bucket","liquidity_score_bucket","spread_pct_bucket","expected_slippage_pct_bucket","stop_distance_pct_bucket"} and any(str(r.get(g["group_field"]) or "UNAVAILABLE")==g["group_value"] and str(r.get("reject_reason")).upper()=="STOP_TOO_WIDE" for r in records)]}, "high_effective_rr_missed_alpha":missed, "top_quality_improvement_candidates": sorted([r for r in rejected if r.get("outcome")=="WOULD_TP"], key=lambda r:((_quality_float(r.get("effective_rr")) or 0), (_quality_float(r.get("score")) or 0)), reverse=True)[:20], "combo_group_count": len(combo_rows), "candidate_quality_gates": gate_rows, "score_calibration_diagnostics_count": len(calibration_rows), "thresholds_changed": False, "acceptance_logic_changed": False}
    summary.update(_quality_gate_metrics(records, quality_gate_config or QualityGateConfig(), baseline_net_pnl))
    return summary, group_rows, missed, combo_rows, gate_rows, calibration_rows

def build_rejected_shadow_summary(shadows: List[RejectedShadowEvaluation]) -> Dict[str, Any]:
    total = len(shadows)
    counts = {
        k: sum(1 for s in shadows if s.shadow_outcome == k)
        for k in ["WOULD_TP", "WOULD_SL", "WOULD_NOT_TRIGGER", "WOULD_TIMEOUT", "UNKNOWN"]
    }
    profitable = sum(1 for s in shadows if s.effective_tp_hit)
    unprofitable = counts["WOULD_SL"]
    avoidable_loss = sum(1 for s in shadows if s.shadow_outcome == "WOULD_SL")
    missed_profit = profitable
    false_positive_rate = (profitable / total) if total else 0.0
    reject_precision = ((total - profitable) / total) if total else 0.0
    expectancy = (
        0.0
        if total == 0
        else sum(
            (
                s.effective_rr
                if s.effective_tp_hit
                else (-1.0 if s.shadow_outcome == "WOULD_SL" else 0.0)
            )
            for s in shadows
        )
        / total
    )
    def _group(attr: str) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        for s in shadows:
            key = str(getattr(s, attr))
            bucket = out.setdefault(key, {"count": 0, "would_tp": 0, "would_sl": 0, "effective_tp": 0})
            bucket["count"] += 1
            bucket["would_tp"] += int(s.shadow_outcome == "WOULD_TP")
            bucket["would_sl"] += int(s.shadow_outcome == "WOULD_SL")
            bucket["effective_tp"] += int(s.effective_tp_hit)
        return out
    grouped: Dict[str, Dict[str, Any]] = {}
    for s in shadows:
        reason = str(s.reject_reasons or "UNKNOWN")
        bucket = grouped.setdefault(reason, {"rows": 0, "would_tp": 0, "effective_tp": 0, "score_sum": 0.0, "raw_rr_sum": 0.0, "effective_rr_sum": 0.0, "cost_penalty_sum": 0.0, "symbols": {}, "regimes": {}})
        bucket["rows"] += 1
        bucket["would_tp"] += int(s.shadow_outcome == "WOULD_TP")
        bucket["effective_tp"] += int(bool(s.effective_tp_hit))
        bucket["score_sum"] += s.score
        bucket["raw_rr_sum"] += s.raw_rr
        bucket["effective_rr_sum"] += s.effective_rr
        bucket["cost_penalty_sum"] += s.cost_penalty
        bucket["symbols"][s.symbol] = bucket["symbols"].get(s.symbol, 0) + 1
        bucket["regimes"][s.regime] = bucket["regimes"].get(s.regime, 0) + 1
    reason_diagnostics = {}
    for reason, b in grouped.items():
        rows = max(1, b["rows"])
        top_symbols = sorted(b["symbols"].items(), key=lambda x: (-x[1], x[0]))[:3]
        top_regimes = sorted(b["regimes"].items(), key=lambda x: (-x[1], x[0]))[:3]
        reason_diagnostics[reason] = {
            "rows": b["rows"],
            "would_tp_count": b["would_tp"],
            "would_tp_rate": b["would_tp"] / rows,
            "effective_tp_hit_count": b["effective_tp"],
            "effective_tp_hit_rate": b["effective_tp"] / rows,
            "avg_score": b["score_sum"] / rows,
            "avg_raw_rr": b["raw_rr_sum"] / rows,
            "avg_effective_rr": b["effective_rr_sum"] / rows,
            "avg_cost_penalty": b["cost_penalty_sum"] / rows,
            "top_symbols": top_symbols,
            "top_regimes": top_regimes,
        }
    return {
        "total_rejected": total,
        "would_tp": counts["WOULD_TP"],
        "would_sl": counts["WOULD_SL"],
        "would_not_trigger": counts["WOULD_NOT_TRIGGER"],
        "would_timeout": counts["WOULD_TIMEOUT"],
        "rejected_raw_win_rate": (counts["WOULD_TP"] / total if total else 0.0),
        "rejected_effective_expectancy": expectancy,
        "performance_by_reject_reason": json.dumps(_group("reject_reasons"), sort_keys=True),
        "performance_by_regime": json.dumps(_group("regime"), sort_keys=True),
        "performance_by_symbol": json.dumps(_group("symbol"), sort_keys=True),
        "profitable_reject_count": profitable,
        "unprofitable_reject_count": unprofitable,
        "avoidable_loss_count": avoidable_loss,
        "missed_profit_count": missed_profit,
        "reject_precision": reject_precision,
        "reject_false_positive_rate": false_positive_rate,
        "reject_reason_diagnostics": json.dumps(reason_diagnostics, sort_keys=True),
    }
def main():
    cfg = load_config_from_env()
    p = argparse.ArgumentParser()
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--last-n-days", type=int, default=7)
    p.add_argument("--top-n", type=int, default=cfg.backtest.top_n)
    p.add_argument("--quote", default="USDT")
    p.add_argument("--interval", default=cfg.backtest.timeframe)
    p.add_argument("--output-dir", default=cfg.backtest.output_dir)
    p.add_argument("--mode", default="BACKTEST")
    p.add_argument("--balance", type=float, default=cfg.backtest.initial_balance)
    p.add_argument("--risk-pct", type=float, default=cfg.backtest.risk_pct)
    p.add_argument("--telegram", action="store_true")
    p.add_argument("--offline", action="store_true", help="Run without network APIs using deterministic fixture data")
    p.add_argument("--ci", action="store_true", help="CI-safe mode; implies --offline")
    p.add_argument("--symbols", default="", help="Comma-separated fixed symbol list for deterministic historical universe")
    p.add_argument("--force-refresh", action="store_true", help="Fetch the full requested Binance historical range before running")
    p.add_argument("--rescue-enabled", action="store_true", help="Enable BACKTEST-only high effective-RR rescue acceptance lane")
    p.add_argument("--rescue-modes", default="BACKTEST")
    p.add_argument("--rescue-effective-rr-min", type=float, default=1.90)
    p.add_argument("--rescue-score-min", type=float, default=9.0)
    p.add_argument("--rescue-size-multiplier", type=float, default=0.25)
    p.add_argument("--max-rescue-trades-per-day", type=int, default=1)
    p.add_argument("--rescue-allowed-reasons", default="STOP_TOO_WIDE,DAILY_SYMBOL_TRADE_LIMIT")
    p.add_argument("--rescue-allow-regime-mismatch", action="store_true")
    p.add_argument("--rescue-max-spread-pct", type=float, default=0.0025)
    p.add_argument("--rescue-max-slippage-pct", type=float, default=0.0020)
    p.add_argument("--rescue-allow-cooldown-bypass", action="store_true")
    p.add_argument("--quality-gate-enabled", action="store_true", help="Enable BACKTEST-only reporting comparison for SHORT breakdown/breakout NORMAL-stop gate")
    p.add_argument("--quality-gate-modes", default="BACKTEST")
    p.add_argument("--quality-gate-size-multiplier", type=float, default=0.25)
    p.add_argument("--max-quality-gate-trades-per-day", type=int, default=1)
    p.add_argument("--quality-gate-min-effective-rr", type=float, default=1.10)
    p.add_argument("--quality-gate-min-score", default=None)
    p.add_argument("--quality-gate-name", default=QUALITY_GATE_NAME)
    p.add_argument("--quality-gate-max-spread-pct", type=float, default=0.0025)
    p.add_argument("--quality-gate-max-slippage-pct", type=float, default=0.0020)
    p.add_argument("--quality-gate-allowed-reasons", default="LOW_SCORE,DAILY_SYMBOL_TRADE_LIMIT")
    args = p.parse_args()
    if args.ci:
        args.offline = True
    rescue_config = _rescue_config_from_args(args, cfg)
    quality_gate_config = _quality_gate_config_from_args(args)
    rescue_stats = RescueStats()
    now = datetime.now(timezone.utc)
    default_end = int(now.timestamp() * 1000)
    default_start = int((now.timestamp() - args.last_n_days * 86400) * 1000)
    start_ms = parse_ts(args.start) if args.start else default_start
    end_ms = parse_ts(args.end) if args.end else default_end
    os.makedirs(args.output_dir, exist_ok=True)
    if args.offline:
        universe, candles_by_symbol = _offline_fixture(start_ms)
    else:
        fixed_symbols = [x.strip() for x in str(args.symbols or "").split(",") if x.strip()]
        universe = select_symbol_universe(args.top_n, args.quote, symbols=fixed_symbols)
        candles_by_symbol = {}
        for row in universe:
            c = load_or_fetch_candles(row["symbol"], args.interval, start_ms, end_ms, args.output_dir, force_refresh=args.force_refresh)
            if c:
                candles_by_symbol[row["symbol"]] = c
    save_symbol_universe(os.path.join(args.output_dir, "symbol_universe.csv"), universe)
    symbol_meta_by_symbol = {row["symbol"]: row for row in universe}
    lifecycle = []
    candidates = []
    rejected = []
    open_rows = []
    recent_stats: Dict[str, Any] = {
        "last_trade_ts_by_symbol": {},
        "trades_today_by_symbol": {},
        "global_trades_today": 0,
        "symbol_loss_streak": {},
        "global_loss_streak": 0,
        "symbol_loss_block_until": {},
        "global_loss_block_until": 0,
        "consecutive_sl_count": 0,
        "consecutive_tp_count": 0,
        "rolling_winrate": 0.0,
        "outcomes": [],
    }
    rejection_counts: Dict[str, int] = {}
    if not args.offline:
        for symbol, candles in candles_by_symbol.items():
            for i in range(len(candles)):
                symbol_meta = symbol_meta_by_symbol.get(symbol, {})
                if i < 2:
                    continue
                selector_market = _build_symbol_market_data(symbol_meta, candles, i)
                selector_result = select_symbol(symbol, selector_market)
                if not selector_result.tradable:
                    reason = selector_result.reject_reasons[0] if selector_result.reject_reasons else "SYMBOL_FILTER_REJECTED"
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    lifecycle.append(
                        LifecycleRow(
                            timestamp=candles[i].timestamp,
                            symbol=symbol,
                            side="N/A",
                            setup_type="",
                            setup_reason="",
                            regime=selector_result.regime_hint,
                            score=selector_result.symbol_score,
                            rr=0.0,
                            entry=0.0,
                            sl=0.0,
                            tp=0.0,
                            status_before="NONE",
                            status_after="SYMBOL_REJECTED",
                            reject_reason=reason,
                            event_flags="SYMBOL_SELECTOR",
                            volume_24h_usdt=selector_market.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                            spread_pct=selector_market.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                            liquidity_score=selector_market.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                        )
                    )
                    rejected.append(
                        {
                            "signal_id": f"SYMBOL_SELECTOR:{symbol}:{candles[i].timestamp}",
                            "lifecycle_state": "SYMBOL_REJECTED",
                            "source_stage": "SYMBOL_SELECTOR",
                            "timestamp": candles[i].timestamp,
                            "symbol": symbol,
                            "side": "N/A",
                            "setup_type": "",
                            "setup_reason": "SYMBOL_SELECTOR",
                            "regime": selector_result.regime_hint,
                            "score": selector_result.symbol_score,
                            "rr": 0.0,
                            "expectancy": "",
                            "quality_score": "",
                            "reject_reason": reason,
                            "diagnostics": json.dumps(
                                {
                                    "selector": selector_result.diagnostics,
                                    "warnings": selector_result.warnings,
                                    "reject_reasons": selector_result.reject_reasons,
                                    "derived": selector_market.get("selector_diagnostics", {}),
                                },
                                sort_keys=True,
                            ),
                            "event_flags": "SYMBOL_SELECTOR",
                            "volume_24h_usdt": selector_market.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                            "spread_pct": selector_market.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                            "liquidity_score": selector_market.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                            "raw_rr": 0.0,
                            "effective_rr": 0.0,
                            **_low_score_rescue_watch_fields(reason, {}),
                        }
                    )
                    continue
                scan_ctx = {
                    "mode": args.mode,
                    "balance": args.balance,
                    "risk_pct": args.risk_pct,
                    "recent_stats": recent_stats,
                    "symbol_meta": symbol_meta,
                }
                _ = scan_symbol_backtest(symbol, candles, i, scan_ctx)
                result = scan_ctx.get("last_result", {})
                mctx = scan_ctx.get("market_ctx", {})
                if isinstance(mctx, dict):
                    mctx.setdefault("symbol_score", selector_result.symbol_score)
                    mctx.setdefault("regime_hint", selector_result.regime_hint)
                    mctx.setdefault("symbol_selector_warnings", selector_result.warnings)
                cand = process_backtest_result(
                    symbol,
                    candles[i],
                    i,
                    candles,
                    result,
                    mctx,
                    args.balance,
                    args.risk_pct,
                    lifecycle,
                    rejected,
                    rejection_counts,
                    open_rows,
                    recent_stats,
                    rescue_config=rescue_config,
                    rescue_stats=rescue_stats,
                    mode=args.mode,
                )
                if cand:
                    candidates.append(cand)
    if args.offline and not candidates and candles_by_symbol:
        symbol = next(iter(candles_by_symbol.keys()))
        fixture_candles = candles_by_symbol[symbol]
        c0 = fixture_candles[5]
        mctx = _build_market_ctx(
            fixture_candles[6],
            fixture_candles[5],
            {"quoteVolume": 100000000.0},
            fixture_candles[:7],
        )
        synthetic = CandidateOrder(
            c0.timestamp,
            symbol,
            "LONG",
            c0.close,
            c0.close - 0.5,
            c0.close + (c0.close - (c0.close - 0.5)) * mctx["rr"],
            mctx["rr"],
            "BREAKOUT_UP",
            "OFFLINE_FIXTURE",
            "TREND",
            mctx["score"],
            "LIMIT",
            expectancy_bucket=mctx.get("expectancy_bucket", "UNKNOWN"),
        )
        candidates.append(synthetic)
        lifecycle.extend(
            simulate_candidate(
                synthetic,
                fixture_candles,
                5,
                args.balance,
                args.risk_pct,
                market_ctx={
                    "volume_24h_usdt": 100000000.0,
                    "spread_pct": 0.2,
                    "liquidity_score": 0.8,
                    "expected_slippage_pct": 0.001,
                    "volatility_regime": "NORMAL",
                },
            )
        )
        rejected.append(
            {
                "timestamp": fixture_candles[8].timestamp,
                "symbol": symbol,
                "side": "LONG",
                "setup_type": "BREAKOUT_UP",
                "setup_reason": "OFFLINE_FIXTURE",
                "regime": "TREND",
                "score": 4.0,
                "rr": 0.9,
                "expectancy": -0.1,
                "quality_score": 0.1,
                "reject_reason": "LOW_EFFECTIVE_RR",
                "diagnostics": json.dumps({"offline": True}, sort_keys=True),
                "entry": fixture_candles[8].close,
                "sl": fixture_candles[8].close - 0.5,
                "tp": fixture_candles[8].close + 0.3,
                "spread_pct": 0.2,
                "liquidity_score": 0.8,
                "volatility_score": 0.2,
                "expected_slippage_pct": 0.001,
                **_low_score_rescue_watch_fields("LOW_EFFECTIVE_RR", {}),
            }
        )
        lifecycle.append(
            LifecycleRow(
                timestamp=fixture_candles[8].timestamp,
                symbol=symbol,
                side="LONG",
                setup_type="BREAKOUT_UP",
                setup_reason="OFFLINE_FIXTURE",
                regime="TREND",
                score=4.0,
                rr=0.9,
                entry=fixture_candles[8].close,
                sl=fixture_candles[8].close - 0.5,
                tp=fixture_candles[8].close + 0.3,
                status_before="SIGNAL_CREATED",
                status_after="SIGNAL_REJECTED",
                reject_reason="LOW_EFFECTIVE_RR",
                order_type="N/A",
                volume_24h_usdt=100000000.0,
                spread_pct=0.2,
                liquidity_score=0.8,
                expected_slippage_pct=0.001,
            )
        )
    candidate_rows = [{**asdict(x), "quality_score": "", "accepted": True, "reject_reason": "", "raw_rr": x.rr, "effective_rr": x.rr, "min_required_score": "", "trend_strength": "", "volatility_pct": "", "range_position": "", "spread_pct": "", "slippage_pct": "", "liquidity_score": "", "first_blocking_gate": "", "all_failed_gates": "[]"} for x in candidates]
    rejected_shadow: List[RejectedShadowEvaluation] = []
    for row in rejected:
        if not _is_actionable_rejected_order(row):
            continue
        symbol = row.get("symbol")
        ts = int(row.get("timestamp", 0) or 0)
        candles = candles_by_symbol.get(symbol, [])
        idx = next((i for i, c in enumerate(candles) if c.timestamp >= ts), len(candles))
        rejected_shadow.append(evaluate_rejected_shadow(row, candles, idx))
    forward_evaluations = build_forward_evaluations_from_lifecycle(lifecycle, candles_by_symbol)
    lifecycle_index = {f"{row.symbol}:{row.timestamp}": row for row in lifecycle}
    calibration_snapshots = persist_calibration_snapshots(forward_evaluations, lifecycle_index)
    adaptive_scope_stats: List[dict[str, Any]] = []
    for ev in forward_evaluations:
        scope_payload = {
            "signal_id": ev.signal_id,
            "regime": ev.forward_window_regime,
            "setup_type": lifecycle_index.get(ev.signal_id).setup_type if lifecycle_index.get(ev.signal_id) else "UNKNOWN",
            "timeframe": args.interval,
            "session": "BACKTEST",
            "volatility_bucket": _bucket_numeric(abs(ev.max_forward_return), [0.2, 0.5], ["LOW", "MEDIUM", "HIGH"]),
            "spread_bucket": _bucket_numeric(_safe_float(lifecycle_index.get(ev.signal_id).spread_pct if lifecycle_index.get(ev.signal_id) else 0.0), [0.03, 0.08], ["TIGHT", "NORMAL", "WIDE"]),
            "liquidity_bucket": _bucket_numeric(_safe_float(lifecycle_index.get(ev.signal_id).liquidity_score if lifecycle_index.get(ev.signal_id) else 0.0), [0.3, 0.7], ["THIN", "NORMAL", "DEEP"]),
            "trend_strength_bucket": _bucket_numeric(_safe_float(lifecycle_index.get(ev.signal_id).score if lifecycle_index.get(ev.signal_id) else 0.0), [3.0, 7.0], ["WEAK", "MID", "STRONG"]),
            "rejection_reason": ev.reject_reason,
            "execution_quality_bucket": ev.execution_quality_bucket,
            "reject_correct": ev.reject_correct,
        }
        adaptive_scope_stats.append(scope_payload)
    _attach_rejected_shadow_to_lifecycle(lifecycle, rejected_shadow)
    persisted_lifecycle_rows = _persist_lifecycle_rows(lifecycle)
    forward_eval_rows = build_forward_evaluation_rows(
        [{**row, "timestamp": _safe_float(row.get("event_ts"), 0.0)} for row in persisted_lifecycle_rows],
        candles_by_symbol,
        forward_window_minutes=240,
    )
    for name, rows in [
        ("order_lifecycle.csv", persisted_lifecycle_rows),
        ("order_candidates.csv", candidate_rows),
        ("backtest_orders.csv", candidate_rows),
        ("rejected_orders.csv", rejected),
        ("rejected_shadow.csv", [asdict(x) for x in rejected_shadow]),
        ("open_at_end.csv", [asdict(x) for x in open_rows]),
        ("forward_evaluations.csv", [asdict(x) for x in forward_evaluations]),
        ("adaptive_scope_stats.csv", adaptive_scope_stats),
        ("calibration_snapshots.csv", calibration_snapshots),
    ]:
        with open(os.path.join(args.output_dir, name), "w", newline="") as f:
            if not rows:
                f.write("")
                continue
            preferred_fieldnames = list(rows[0].keys())
            fieldnames = resolve_csv_fieldnames(rows, preferred_fieldnames)
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    counts = _derive_backtest_counts(lifecycle)
    softened_lifecycle_rows = [r for r in lifecycle if bool(getattr(r, "stop_too_wide_softened", False))]
    softened_scales = [_safe_float(getattr(r, "risk_scale", 1.0), 1.0) for r in softened_lifecycle_rows]
    stop_shadow_counts = {
        "WOULD_TP": sum(1 for s in rejected_shadow if s.reject_reasons == "STOP_TOO_WIDE" and s.shadow_outcome == "WOULD_TP"),
        "WOULD_SL": sum(1 for s in rejected_shadow if s.reject_reasons == "STOP_TOO_WIDE" and s.shadow_outcome == "WOULD_SL"),
        "WOULD_TIMEOUT": sum(1 for s in rejected_shadow if s.reject_reasons == "STOP_TOO_WIDE" and s.shadow_outcome == "WOULD_TIMEOUT"),
    }
    rescue_closed = [r for r in lifecycle if r.status_after == "POSITION_CLOSED" and r.accepted_reason == "HIGH_EFFECTIVE_RR_RESCUE"]
    baseline_closed = [r for r in lifecycle if r.status_after == "POSITION_CLOSED" and r.accepted_reason != "HIGH_EFFECTIVE_RR_RESCUE"]
    rescue_accept_rows = [r for r in lifecycle if r.status_after == "SIGNAL_CREATED" and r.accepted_reason == "HIGH_EFFECTIVE_RR_RESCUE"]
    accepted_reason_breakdown = _distribution([
        r.accepted_reason
        for r in lifecycle
        if r.status_after in {"SIGNAL_ACCEPTED", "ORDER_PLACED", "POSITION_OPENED", "POSITION_CLOSED"}
    ])
    quality_gate_metrics = _quality_gate_metrics(
        [_quality_record_from_shadow(s, args.interval) for s in rejected_shadow],
        quality_gate_config,
        baseline_net_pnl=sum(r.net_pnl_usdt for r in baseline_closed),
    )
    summary = {
        "selected_symbols": len(universe),
        "total_candidates": counts["total_candidates"],
        "accepted_count": counts["accepted_count"],
        "baseline_accepted_trades": counts["accepted_count"] - rescue_stats.accepted_count,
        "rescue_candidate_count": rescue_stats.candidate_count,
        "rescue_accepted_count": rescue_stats.accepted_count,
        "rescue_rejected_count": rescue_stats.rejected_count,
        "rescue_accepted_would_tp_count": sum(1 for r in rescue_closed if r.close_reason == "TP_HIT"),
        "rescue_accepted_would_sl_count": sum(1 for r in rescue_closed if r.close_reason == "SL_HIT"),
        "rescue_accepted_net_pnl": sum(r.net_pnl_usdt for r in rescue_closed),
        "baseline_net_pnl": sum(r.net_pnl_usdt for r in baseline_closed),
        "baseline_plus_rescue_net_pnl": sum(r.net_pnl_usdt for r in lifecycle if r.status_after == "POSITION_CLOSED"),
        "rescue_avg_effective_rr": (sum(r.rescue_effective_rr for r in rescue_accept_rows) / len(rescue_accept_rows)) if rescue_accept_rows else 0.0,
        "rescue_avg_score": (sum(r.score for r in rescue_accept_rows) / len(rescue_accept_rows)) if rescue_accept_rows else 0.0,
        "rescue_reject_reasons": json.dumps(rescue_stats.reject_reasons, sort_keys=True),
        "accepted_reason_breakdown": json.dumps(accepted_reason_breakdown, sort_keys=True),
        "rejected_count": counts["rejected_count"],
        "total_rejected": counts["rejected_count"],
        "stop_too_wide_softened_count": len({r.signal_id or f"{r.symbol}:{r.timestamp}" for r in softened_lifecycle_rows}),
        "stop_too_wide_hard_reject_count": sum(1 for row in rejected if str(row.get("reject_reason", "")).upper() == "STOP_TOO_WIDE"),
        "stop_too_wide_shadow_would_tp": stop_shadow_counts["WOULD_TP"],
        "stop_too_wide_shadow_would_sl": stop_shadow_counts["WOULD_SL"],
        "stop_too_wide_shadow_timeout": stop_shadow_counts["WOULD_TIMEOUT"],
        "avg_risk_scale_for_softened_stop_too_wide": (sum(softened_scales) / len(softened_scales)) if softened_scales else 0.0,
        "rejection_rate": (
            0.0
            if counts["total_candidates"] == 0
            else counts["rejected_count"] / counts["total_candidates"]
        ),
        "total_orders": counts["total_orders"],
        "triggered_orders": counts["triggered_orders"],
        "not_triggered_orders": counts["not_triggered_orders"],
        "tp_hits": counts["tp_hits"],
        "sl_hits": counts["sl_hits"],
        "open_at_end": counts["open_at_end_orders"],
        "win_rate": (
            0.0
            if not lifecycle
            else sum(1 for r in lifecycle if r.close_reason == "TP_HIT")
            / max(1, sum(1 for r in lifecycle if r.status_after == "POSITION_CLOSED"))
        ),
        "avg_rr": 0.0 if not lifecycle else sum(r.rr for r in lifecycle) / len(lifecycle),
        "avg_pnl_pct": 0.0 if not lifecycle else sum(r.net_pnl_pct for r in lifecycle) / len(lifecycle),
        "total_pnl_pct": sum(r.net_pnl_pct for r in lifecycle),
        "total_net_pnl_usdt": sum(r.net_pnl_usdt for r in lifecycle),
        "avg_hold_minutes": 0.0 if not lifecycle else sum(r.hold_minutes for r in lifecycle) / len(lifecycle),
        "performance_by_symbol": {},
        "performance_by_regime": {},
        "performance_by_setup_type": {},
        "rejection_counts": json.dumps(rejection_counts, sort_keys=True),
        "cancel_counts": {},
        "event_flags":{},
    }
    summary.update({
        **quality_gate_metrics,
        "quality_gate_reason_breakdown": json.dumps(quality_gate_metrics["quality_gate_reason_breakdown"], sort_keys=True),
        "quality_gate_symbol_breakdown": json.dumps(quality_gate_metrics["quality_gate_symbol_breakdown"], sort_keys=True),
        "quality_gate_daily_trade_count_distribution": json.dumps(quality_gate_metrics["quality_gate_daily_trade_count_distribution"], sort_keys=True),
    })
    with open(os.path.join(args.output_dir, "order_backtest_summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary.keys()))
        w.writeheader()
        w.writerow(summary)
    quality_summary = build_backtest_quality_summary(persisted_lifecycle_rows)
    write_backtest_quality_summary(
        os.path.join(args.output_dir, "backtest_quality_summary.csv"),
        quality_summary,
    )
    rejected_shadow_summary = build_rejected_shadow_summary(rejected_shadow)
    with open(os.path.join(args.output_dir, "rejected_shadow_summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rejected_shadow_summary.keys()))
        w.writeheader()
        w.writerow(rejected_shadow_summary)

    accepted_quality_rows = [
        r for r in lifecycle
        if not r.reject_reason and r.status_after in {"POSITION_CLOSED", "OPEN_AT_END", "TP_HIT", "SL_HIT"}
    ]
    if not accepted_quality_rows:
        accepted_quality_rows = [r for r in lifecycle if not r.reject_reason and r.status_after == "SIGNAL_CREATED"]
    (
        signal_quality_summary,
        signal_quality_group_rows,
        high_effective_rr_rows,
        signal_quality_combo_rows,
        candidate_quality_gate_rows,
        score_calibration_rows,
    ) = build_signal_quality_diagnostics(
        accepted_quality_rows,
        rejected_shadow,
        args.interval,
        quality_gate_config=quality_gate_config,
        baseline_net_pnl=sum(r.net_pnl_usdt for r in baseline_closed),
    )
    with open(os.path.join(args.output_dir, "signal_quality_summary.json"), "w") as f:
        json.dump(signal_quality_summary, f, indent=2, sort_keys=True)
    for name, rows, fallback_fields in [
        ("signal_quality_by_group.csv", signal_quality_group_rows, ["group_field", "group_value", "count"]),
        ("high_effective_rr_missed_alpha.csv", high_effective_rr_rows, ["effective_rr_threshold", "count", "would_tp_count", "would_sl_count"]),
        ("signal_quality_combo_groups.csv", signal_quality_combo_rows, ["grouping", "count"]),
        ("candidate_quality_gates.csv", candidate_quality_gate_rows, ["gate_name", "reporting_only", "candidate_count"]),
        ("score_calibration_diagnostics.csv", score_calibration_rows, ["diagnostic", "count"]),
    ]:
        with open(os.path.join(args.output_dir, name), "w", newline="") as f:
            fieldnames = resolve_csv_fieldnames(rows, list(rows[0].keys()) if rows else fallback_fields)
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    try:
        import importlib.util

        calibration_module_path = SRC_DIR / "alphaforge" / "dashboard" / "backtest_control.py"
        spec = importlib.util.spec_from_file_location("alphaforge_dashboard_backtest_control", calibration_module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"unable to load {calibration_module_path}")
        calibration_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = calibration_module
        spec.loader.exec_module(calibration_module)
        calibration_module._write_calibration_artifacts(
            Path(args.output_dir),
            persisted_lifecycle_rows,
            rejected,
            summary,
            [asdict(x) for x in rejected_shadow],
        )
    except Exception as exc:
        raise ValueError(f"Lifecycle calibration artifact generation failed: {exc}") from exc
    with Session(init_db("sqlite+pysqlite:///:memory:")) as session:
        for row in forward_eval_rows:
            session.execute(
                text(
                    """
                    INSERT INTO calibration_snapshots (
                        signal_id, predicted_quality, realized_outcome, score, rr, effective_rr, regime, setup_type,
                        rejection_reason, forward_window_minutes, mfe_pct, mae_pct, would_have_hit_tp, would_have_hit_sl,
                        reject_correct, created_at
                    ) VALUES (
                        :signal_id, :predicted_quality, :realized_outcome, :score, :rr, :effective_rr, :regime, :setup_type,
                        :rejection_reason, :forward_window_minutes, :mfe_pct, :mae_pct, :would_have_hit_tp, :would_have_hit_sl,
                        :reject_correct, :created_at
                    )
                    ON CONFLICT(signal_id, forward_window_minutes) DO NOTHING
                    """
                ),
                {
                    "signal_id": row.get("signal_id"),
                    "predicted_quality": _safe_float(row.get("execution_quality_bucket") == "HIGH", 0.0),
                    "realized_outcome": row.get("realized_outcome"),
                    "score": _safe_float(row.get("score"), 0.0),
                    "rr": _safe_float(row.get("rr"), 0.0),
                    "effective_rr": _safe_float(row.get("max_forward_return"), 0.0),
                    "regime": row.get("forward_window_regime"),
                    "setup_type": row.get("setup_type"),
                    "rejection_reason": row.get("reject_reason"),
                    "forward_window_minutes": int(row.get("forward_window_minutes", 240)),
                    "mfe_pct": _safe_float(row.get("mfe_pct"), 0.0),
                    "mae_pct": _safe_float(row.get("mae_pct"), 0.0),
                    "would_have_hit_tp": int(bool(row.get("would_have_hit_tp"))),
                    "would_have_hit_sl": int(bool(row.get("would_have_hit_sl"))),
                    "reject_correct": None if row.get("reject_correct") is None else int(bool(row.get("reject_correct"))),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        snapshot_rows = session.execute(text("SELECT * FROM calibration_snapshots ORDER BY id")).mappings().all()
    with open(os.path.join(args.output_dir, "calibration_snapshots.csv"), "w", newline="") as f:
        if snapshot_rows:
            fieldnames = resolve_csv_fieldnames([dict(r) for r in snapshot_rows], list(dict(snapshot_rows[0]).keys()))
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows([dict(r) for r in snapshot_rows])
    with open(os.path.join(args.output_dir, "order_lifecycle.csv"), newline="") as f:
        lifecycle_csv_rows = list(csv.DictReader(f))
    with open(os.path.join(args.output_dir, "rejected_orders.csv"), newline="") as f:
        rejected_csv_rows = list(csv.DictReader(f))
    export_errors = verify_export_integrity(
        persisted_lifecycle_rows=persisted_lifecycle_rows,
        rejected_rows=rejected,
        lifecycle_csv_rows=lifecycle_csv_rows,
        rejected_csv_rows=rejected_csv_rows,
    )
    if export_errors:
        raise ValueError(f"Export integrity check failed: {'; '.join(export_errors)}")
def _order_runtime():
    from alphaforge.order import OrderExecutionContext, TradingMode, run_order_cycle
    return OrderExecutionContext, TradingMode, run_order_cycle
if __name__ == "__main__":
    main()
