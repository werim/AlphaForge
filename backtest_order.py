import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Mapping
# Allow running this script directly from the repo root without requiring
# prior editable install.
ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
from alphaforge.execution import build_execution_context
from alphaforge.persistence import init_db, save_trade_lifecycle_event
from alphaforge.symbol_selector import select_symbol
from sqlalchemy import text
from sqlalchemy.orm import Session
from urllib.parse import urlencode
from urllib.request import urlopen


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
@dataclass
class ForwardWindowEvaluation:
    signal_id: str
    symbol: str
    decision: str
    lifecycle_state: str
    reject_reason: str
    setup_type: str
    score: float
    rr: float
    effective_rr: float
    predicted_quality: float
    forward_window_minutes: int
    would_have_hit_tp: bool
    would_have_hit_sl: bool
    mfe_pct: float
    mae_pct: float
    max_forward_return: float
    max_adverse_return: float
    reject_correct: Optional[bool]
    reject_missed_winner: bool
    reject_saved_from_loss: bool
    forward_window_regime: str
    execution_quality_bucket: str
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
def _execution_reject_flags(rr: float, market_ctx: Mapping[str, Any]) -> tuple[float, list[str]]:
    slippage = float(market_ctx.get("expected_slippage_pct", 0.0) or 0.0)
    spread = float(market_ctx.get("spread_pct", 0.0) or 0.0)
    liquidity_score = float(market_ctx.get("liquidity_score", 1.0) or 1.0)
    execution_penalty = (slippage + spread) * 50.0
    effective = round(max(float(rr) * (1.0 - execution_penalty), 0.0), 6)
    flags: list[str] = []
    if slippage >= 0.02:
        flags.append("HIGH_SLIPPAGE")
    if liquidity_score < 0.3:
        flags.append("LOW_LIQUIDITY")
    if effective < 1.1:
        flags.append("LOW_EFFECTIVE_RR")
    return effective, flags
def _estimate_backtest_spread_pct(liquidity_score: float, volatility_pct: float) -> float:
    base_spread_pct = 0.015 + (1.0 - liquidity_score) * 0.09
    volatility_widening = min(0.04, max(0.0, (volatility_pct - 2.0) * 0.0015))
    return max(0.005, min(0.22, base_spread_pct + volatility_widening))
def _build_market_ctx(
    now: Candle,
    prev: Candle,
    symbol_meta: Mapping[str, Any],
    recent: Optional[List[Candle]] = None,
) -> Dict[str, Any]:
    entry = now.close
    sl = min(now.low, prev.low)
    risk = max(entry - sl, 1e-9)
    body = abs(now.close - now.open)
    breakout_strength = max(0.0, (now.close - prev.high) / max(prev.high, 1e-9))
    range_pct = ((now.high - now.low) / max(now.close, 1e-9)) * 100.0
    rr = max(1.1, min(3.5, 1.2 + breakout_strength * 25.0 + body / max(now.open, 1e-9) * 8.0))
    tp = entry + rr * risk
    score = max(0.0, min(10.0, 3.0 + breakout_strength * 500.0 + range_pct))
    expectancy = ((score / 10.0) - 0.5) * (rr - 1.0)
    quote_volume = symbol_meta.get("quoteVolume")
    if quote_volume in (None, "", 0, 0.0):
        quote_volume = now.volume * now.close * 1440.0
    candle_range_pct = ((now.high - now.low) / max(now.close, 1e-9)) * 100.0
    liq = min(1.0, max(0.05, float(symbol_meta.get("quoteVolume", 0.0) or 0.0) / 100000000.0))
    spread_source = "ACTUAL" if symbol_meta.get("actual_spread_pct") not in (None, "") else "ESTIMATED_BACKTEST"
    spread_pct = float(symbol_meta.get("actual_spread_pct") or symbol_meta.get("estimated_spread_pct") or _estimate_backtest_spread_pct(liq, candle_range_pct))
    base = {
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "score": score,
        "setup_type": "BREAKOUT_UP",
        "setup_reason": "CLOSE_ABOVE_PREV_HIGH",
        "regime": "BREAKOUT" if breakout_strength > 0.002 else "TREND",
        "expectancy": expectancy,
        "expectancy_bucket": _bucket_expectancy(expectancy),
        "side": "LONG",
        "volume_24h_usdt": float(quote_volume),
        "spread_pct": spread_pct,
        "spread_source": spread_source,
        "candle_range_pct": candle_range_pct,
        "volatility_pct": candle_range_pct,
        "funding_rate_pct": float(symbol_meta.get("fundingRate", 0.0) or 0.0),
    }
    klines = [{"high": c.high, "low": c.low, "close": c.close} for c in (recent or [])[-20:] if c]
    exec_ctx = build_execution_context(
        {
            **base,
            "recent_klines": klines,
            "liquidity_score": min(
                1.0,
                max(0.1, float(symbol_meta.get("quoteVolume", 0.0) or 0.0) / 100000000.0),
            ),
        }
    )
    base.update(exec_ctx)
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
    if actual_spread_pct not in (None, ""):
        spread_pct = float(actual_spread_pct)
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
        "actual_spread_pct": float(actual_spread_pct) if actual_spread_pct not in (None, "") else None,
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
def select_symbol_universe(top_n: int, quote: str = "USDT") -> List[Dict[str, Any]]:
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
def load_or_fetch_candles(symbol: str, interval: str, start_ms: int, end_ms: int, output_dir: str) -> List[Candle]:
    path = os.path.join(output_dir, "candles", f"{symbol}_{interval}.csv")
    if os.path.exists(path):
        return load_candles(path, start_ms, end_ms)
    candles = fetch_klines(symbol, interval, start_ms, end_ms)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
        w.writeheader()
        for c in candles:
            w.writerow(asdict(c))
    return candles
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
            "SIGNAL_CREATED",
            "WAITING_ENTRY_ZONE",
            order_type=candidate.order_type,
            expectancy_bucket=candidate.expectancy_bucket,
            volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
            liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
        )
    ]
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
            return rows
    mfe = 0.0
    mae = 0.0
    tp_distance = max(candidate.tp - candidate.entry, 1e-9)
    sl_distance = max(candidate.entry - candidate.sl, 1e-9)
    for j in range(start_idx, len(candles)):
        c = candles[j]
        mfe = max(mfe, c.high - candidate.entry)
        mae = max(mae, candidate.entry - c.low)
        hit_sl = c.low <= candidate.sl
        hit_tp = c.high >= candidate.tp
        # Conservative same-candle rule:
        # if both TP and SL touch inside the same candle, count SL first.
        if hit_sl and hit_tp:
            hit_tp = False
        if hit_sl:
            pnl_pct = ((candidate.sl - candidate.entry) / candidate.entry) * 100
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
            return rows
        if hit_tp:
            pnl_pct = ((candidate.tp - candidate.entry) / candidate.entry) * 100
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
            return rows
    c = candles[-1]
    pnl_pct = ((c.close - candidate.entry) / candidate.entry) * 100
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
    return rows
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
    for c in scan:
        if c.low <= candidate.entry <= c.high:
            would_trigger = True
        if would_trigger:
            mfe = max(mfe, c.high - candidate.entry)
            mae = max(mae, candidate.entry - c.low)
            hit_sl = c.low <= candidate.sl
            hit_tp = c.high >= candidate.tp
            # Deterministic same-candle rule for diagnostics parity:
            # if both touch, count stop loss first.
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
) -> Optional[CandidateOrder]:
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
        )
    )
    if result.get("status") == "rejected":
        reason = result.get("reason", "UNKNOWN")
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
            )
        )
        rejected.append(
            {
                "timestamp": candle.timestamp,
                "symbol": symbol,
                "side": side,
                "setup_type": setup_type,
                "setup_reason": setup_reason,
                "regime": regime,
                "score": score,
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
                "effective_rr": diagnostics.get("effective_rr", rr),
                "min_required_score": ((diagnostics.get("adaptive_thresholds") or {}).get("min_score") if isinstance(diagnostics, dict) else None),
                "trend_strength": mctx.get("trend_strength", "UNAVAILABLE_BACKTEST"),
                "volatility_pct": mctx.get("volatility_pct", "UNAVAILABLE_BACKTEST"),
                "range_position": mctx.get("range_position", "UNAVAILABLE_BACKTEST"),
                "spread_pct": mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                "slippage_pct": mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                "liquidity_score": mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                "first_blocking_gate": diagnostics.get("failed_filter", ""),
                "all_failed_gates": json.dumps(diagnostics.get("all_failed_gates", []), sort_keys=True) if isinstance(diagnostics, dict) else "[]",
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
    effective_rr, execution_flags = _execution_reject_flags(cand.rr, mctx)
    if execution_flags:
        reason = execution_flags[0]
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
            )
        )
        rejected.append(
            {
                "timestamp": candle.timestamp,
                "symbol": symbol,
                "side": cand.side,
                "setup_type": cand.setup_type,
                "setup_reason": cand.setup_reason,
                "regime": cand.regime,
                "score": cand.score,
                "rr": cand.rr,
                "expectancy": expectancy,
                "quality_score": diagnostics.get("quality_score", ""),
                "reject_reason": reason,
                "diagnostics": json.dumps(
                    {"effective_rr": effective_rr, "execution_flags": execution_flags},
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
                "min_required_score": ((diagnostics.get("adaptive_thresholds") or {}).get("min_score") if isinstance(diagnostics, dict) else None),
                "trend_strength": mctx.get("trend_strength", "UNAVAILABLE_BACKTEST"),
                "volatility_pct": mctx.get("volatility_pct", "UNAVAILABLE_BACKTEST"),
                "range_position": mctx.get("range_position", "UNAVAILABLE_BACKTEST"),
                "slippage_pct": mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                "first_blocking_gate": "execution",
                "all_failed_gates": json.dumps(execution_flags, sort_keys=True),
            }
        )
        return None
    sim_rows = simulate_candidate(cand, candles, idx, balance, risk_pct, market_ctx=mctx)
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
def _lifecycle_event_id(row: LifecycleRow, index: int) -> str:
    return (
        f"{row.timestamp}:{row.symbol}:{row.status_before}:{row.status_after}:"
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
            lifecycle_state = row.status_after
            if lifecycle_state in {"SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"}:
                decision = "REJECTED"
            elif lifecycle_state == "SIGNAL_CREATED":
                decision = "PENDING"
            else:
                decision = "ACCEPTED"
            save_trade_lifecycle_event(
                session,
                event_id=_lifecycle_event_id(row, idx),
                signal_id=f"{row.symbol}:{row.timestamp}",
                order_id=None,
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
                    "close_reason": row.close_reason,
                },
                execution_ctx_missing=execution_ctx_missing,
                event_ts=str(row.timestamp),
            )
        persisted = session.execute(
            text(
                """
                SELECT event_id, signal_id, order_id, symbol, mode, lifecycle_state, decision, reject_reason,
                       score, rr, effective_rr, expectancy_bucket, execution_ctx, execution_ctx_missing,
                       event_ts, created_at
                FROM trade_lifecycle_events
                ORDER BY event_ts, event_id
                """
            )
        ).mappings().all()
    return [dict(row) for row in persisted]


def _derive_backtest_counts(lifecycle: List[LifecycleRow]) -> Dict[str, int]:
    signal_ids = {
        f"{row.symbol}:{row.timestamp}"
        for row in lifecycle
        if row.status_after in {"SIGNAL_CREATED", "SYMBOL_REJECTED"}
    }
    total_candidates = len(signal_ids)
    rejected_count = sum(1 for row in lifecycle if row.status_after in {"SYMBOL_REJECTED", "SIGNAL_REJECTED", "ORDER_REJECTED"})
    accepted_count = total_candidates - rejected_count
    total_orders = sum(1 for row in lifecycle if row.status_after == "WAITING_ENTRY_ZONE")
    triggered_orders = sum(1 for row in lifecycle if row.status_after == "ENTRY_TRIGGERED")
    not_triggered_orders = sum(1 for row in lifecycle if row.status_after == "ENTRY_TIMEOUT")
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

    return {
        "total_candidates": total,
        "accepted_count": len(accepted_rows),
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


def verify_export_integrity(
    persisted_lifecycle_rows: List[Mapping[str, Any]],
    rejected_rows: List[Mapping[str, Any]],
    lifecycle_csv_rows: List[Mapping[str, Any]],
    rejected_csv_rows: List[Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    if len(persisted_lifecycle_rows) != len(lifecycle_csv_rows):
        errors.append("lifecycle row count mismatch between SQLite lifecycle and order_lifecycle.csv")
    if len(rejected_rows) != len(rejected_csv_rows):
        errors.append("rejected row count mismatch between rejected records and rejected_orders.csv")
    for idx, row in enumerate(persisted_lifecycle_rows):
        decision = str(row.get("decision", "")).upper()
        reject_reason = str(row.get("reject_reason", "") or "").strip().upper()
        if decision == "REJECTED" and reject_reason in {"", "UNKNOWN"}:
            errors.append(f"rejected lifecycle row index={idx} missing reject_reason")
        expectancy_bucket = str(row.get("expectancy_bucket", "") or "").strip().upper()
        if expectancy_bucket == "":
            errors.append(f"lifecycle row index={idx} missing expectancy_bucket")
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
    spread_pct = _safe_float(candidate_row.get("spread_pct"), 0.0)
    liquidity_score = _safe_float(candidate_row.get("liquidity_score"), 1.0)
    volatility_score = _safe_float(candidate_row.get("volatility_score"), spread_pct)
    effective_rr, _ = _execution_reject_flags(
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
    cost_penalty = max(rr - effective_rr, 0.0)
    effective_tp_hit = (
        counterfactual["outcome"] == "WOULD_TP"
        and effective_rr >= 1.1
        and liquidity_ok
        and volatility_ok
    )
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
    )
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
    }
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--last-n-days", type=int, default=7)
    p.add_argument("--top-n", type=int, default=100)
    p.add_argument("--quote", default="USDT")
    p.add_argument("--interval", default="1m")
    p.add_argument("--output-dir", default="data/backtest")
    p.add_argument("--mode", default="BACKTEST")
    p.add_argument("--balance", type=float, default=1000)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--telegram", action="store_true")
    p.add_argument("--offline", action="store_true", help="Run without network APIs using deterministic fixture data")
    p.add_argument("--ci", action="store_true", help="CI-safe mode; implies --offline")
    args = p.parse_args()
    if args.ci:
        args.offline = True
    now = datetime.now(timezone.utc)
    default_end = int(now.timestamp() * 1000)
    default_start = int((now.timestamp() - args.last_n_days * 86400) * 1000)
    start_ms = parse_ts(args.start) if args.start else default_start
    end_ms = parse_ts(args.end) if args.end else default_end
    os.makedirs(args.output_dir, exist_ok=True)
    if args.offline:
        universe, candles_by_symbol = _offline_fixture(start_ms)
    else:
        universe = select_symbol_universe(args.top_n, args.quote)
        candles_by_symbol = {}
        for row in universe:
            c = load_or_fetch_candles(row["symbol"], args.interval, start_ms, end_ms, args.output_dir)
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
        ("forward_evaluations.csv", forward_eval_rows),
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
    summary = {
        "selected_symbols": len(universe),
        "total_candidates": counts["total_candidates"],
        "accepted_count": counts["accepted_count"],
        "rejected_count": counts["rejected_count"],
        "total_rejected": counts["rejected_count"],
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
    with Session(init_db("sqlite+pysqlite:///:memory:")) as session:
        for row in forward_eval_rows:
            session.execute(
                text(
                    """
                    INSERT INTO calibration_snapshots (
                        signal_id, predicted_quality, realized_outcome, score, rr, effective_rr, regime, setup_type,
                        rejection_reason, forward_window_minutes, mfe_pct, mae_pct, would_have_hit_tp, would_have_hit_sl,
                        reject_correct, created_at, payload_json
                    ) VALUES (
                        :signal_id, :predicted_quality, :realized_outcome, :score, :rr, :effective_rr, :regime, :setup_type,
                        :rejection_reason, :forward_window_minutes, :mfe_pct, :mae_pct, :would_have_hit_tp, :would_have_hit_sl,
                        :reject_correct, :created_at, :payload_json
                    )
                    ON CONFLICT(signal_id, forward_window_minutes, realized_outcome) DO NOTHING
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
                    "payload_json": json.dumps(row, sort_keys=True),
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
