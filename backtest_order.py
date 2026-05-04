import argparse
import csv
import json
import os
from dataclasses import dataclass, asdict

from alphaforge.order import OrderExecutionContext, TradingMode, run_order_cycle
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from urllib.parse import urlencode
from urllib.request import urlopen


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
    volume_24h_usdt: float = 0.0
    spread_pct: float = 0.0
    funding_rate_pct: float = 0.0


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
    params = urlencode({"symbol": symbol, "interval": interval, "startTime": start_ms, "endTime": end_ms, "limit": 1500})
    rows = fetch_json(f"https://fapi.binance.com/fapi/v1/klines?{params}")
    return [Candle(timestamp=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]), volume=float(r[5])) for r in rows]


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
                out.append(Candle(ts, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row.get("volume", 0.0))))
    out.sort(key=lambda x: x.timestamp)
    return out


def scan_symbol_backtest(symbol: str, candles: List[Candle], idx: int, context: Dict[str, Any]) -> Optional[CandidateOrder]:
    if idx < 2:
        return None
    now = candles[idx]
    prev = candles[idx - 1]
    if now.close <= prev.high:
        return None
    entry = now.close
    sl = min(now.low, prev.low)
    tp = entry + 2 * (entry - sl)
    mctx = {"entry": entry, "sl": sl, "tp": tp, "rr": 2.0, "score": 0.8, "setup_type": context.get("mode", "BACKTEST"), "setup_reason": "BREAKOUT_UP", "regime": "TREND", "expectancy": 0.1, "side": "LONG"}
    ctx = OrderExecutionContext(mode=TradingMode.BACKTEST, timestamp=now.timestamp, symbol=symbol, balance=float(context.get("balance",1000)), risk_pct=float(context.get("risk_pct",1.0)), market_ctx=mctx)
    result = run_order_cycle(ctx, recent_stats=context.get("recent_stats", {}))
    context["last_result"] = result
    if result.get("status") != "executed":
        return None
    c = result["candidate"]
    return CandidateOrder(now.timestamp, symbol, c.side, c.entry, c.sl, c.tp, c.rr, c.setup_type, c.setup_reason, c.regime, c.score, c.order_type)


def simulate_candidate(candidate: CandidateOrder, candles: List[Candle], idx: int, balance: float, risk_pct: float) -> LifecycleRow:
    status_before = "CREATED"
    triggered_ts = None
    trigger_price = 0.0
    if candidate.order_type in {"MARKET", "BREAKOUT", "IMMEDIATE"}:
        status_after = "TRIGGERED"
        triggered_ts = candles[idx].timestamp
        trigger_price = candidate.entry
        start_idx = idx
    else:
        status_after = "WAITING_ENTRY_ZONE"
        start_idx = idx
        for j in range(idx, len(candles)):
            c = candles[j]
            if c.low <= candidate.entry <= c.high:
                status_after = "TRIGGERED"
                triggered_ts = c.timestamp
                trigger_price = candidate.entry
                start_idx = j
                break
        if triggered_ts is None:
            return LifecycleRow(candidate.timestamp, candidate.symbol, candidate.side, candidate.setup_type, candidate.setup_reason, candidate.regime, candidate.score, candidate.rr, candidate.entry, candidate.sl, candidate.tp, status_before, "TIMEOUT", cancel_reason="TIMEOUT", order_type=candidate.order_type)

    for j in range(start_idx, len(candles)):
        c = candles[j]
        hit_sl = c.low <= candidate.sl
        hit_tp = c.high >= candidate.tp
        if hit_sl and hit_tp:
            hit_tp = False
        if hit_sl:
            pnl_pct = ((candidate.sl - candidate.entry) / candidate.entry) * 100
            return finalize(candidate, status_before, "SL_HIT", trigger_price, candidate.sl, "SL_HIT", pnl_pct, balance, risk_pct, triggered_ts, c.timestamp)
        if hit_tp:
            pnl_pct = ((candidate.tp - candidate.entry) / candidate.entry) * 100
            return finalize(candidate, status_before, "TP_HIT", trigger_price, candidate.tp, "TP_HIT", pnl_pct, balance, risk_pct, triggered_ts, c.timestamp)

    c = candles[-1]
    pnl_pct = ((c.close - candidate.entry) / candidate.entry) * 100
    return finalize(candidate, status_before, "OPEN_AT_END", trigger_price, c.close, "BACKTEST_END", pnl_pct, balance, risk_pct, triggered_ts, c.timestamp)


def finalize(candidate, before, after, trigger_price, close_price, close_reason, pnl_pct, balance, risk_pct, triggered_ts, closed_ts):
    risk_usdt = balance * (risk_pct / 100)
    net_pnl_usdt = risk_usdt * (pnl_pct / 100)
    hold = (closed_ts - triggered_ts) / 60000 if triggered_ts else 0
    return LifecycleRow(candidate.timestamp, candidate.symbol, candidate.side, candidate.setup_type, candidate.setup_reason, candidate.regime, candidate.score, candidate.rr, candidate.entry, candidate.sl, candidate.tp, before, after, trigger_price=trigger_price, close_price=close_price, close_reason=close_reason, net_pnl_pct=pnl_pct, net_pnl_usdt=net_pnl_usdt, hold_minutes=hold, order_type=candidate.order_type)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--top-n", type=int, default=100)
    p.add_argument("--quote", default="USDT")
    p.add_argument("--interval", default="1m")
    p.add_argument("--output-dir", default="data/backtest")
    p.add_argument("--mode", default="BACKTEST")
    p.add_argument("--balance", type=float, default=1000)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--telegram", action="store_true")
    args = p.parse_args()

    start_ms, end_ms = parse_ts(args.start), parse_ts(args.end)
    universe = select_symbol_universe(args.top_n, args.quote)
    save_symbol_universe(os.path.join(args.output_dir, "symbol_universe.csv"), universe)

    candles_by_symbol = {}
    for row in universe:
        c = load_or_fetch_candles(row["symbol"], args.interval, start_ms, end_ms, args.output_dir)
        if c:
            candles_by_symbol[row["symbol"]] = c

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
    }
    rejection_counts: Dict[str, int] = {}
    for symbol, candles in candles_by_symbol.items():
        for i in range(len(candles)):
            scan_ctx = {"mode": args.mode, "balance": args.balance, "risk_pct": args.risk_pct, "recent_stats": recent_stats}
            cand = scan_symbol_backtest(symbol, candles, i, scan_ctx)
            result = scan_ctx.get("last_result", {})
            if result:
                if result.get("status") == "rejected":
                    reason = result.get("reason", "UNKNOWN")
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    diagnostics = result.get("diagnostics", {})
                    rejected.append({
                        "timestamp": candles[i].timestamp,
                        "symbol": symbol,
                        "side": (diagnostics.get("side") or "LONG"),
                        "setup_type": diagnostics.get("setup_type", ""),
                        "setup_reason": diagnostics.get("setup_reason", ""),
                        "regime": diagnostics.get("regime", ""),
                        "score": diagnostics.get("score", 0.0),
                        "rr": diagnostics.get("rr", 0.0),
                        "expectancy": diagnostics.get("expectancy"),
                        "quality_score": diagnostics.get("quality_score", 0.0),
                        "reject_reason": reason,
                        "diagnostics": json.dumps(diagnostics, sort_keys=True),
                    })
            if not cand:
                continue
            candidates.append(cand)
            row = simulate_candidate(cand, candles, i, args.balance, args.risk_pct)
            lifecycle.append(row)
            recent_stats["last_trade_ts_by_symbol"][symbol] = candles[i].timestamp
            recent_stats["trades_today_by_symbol"][symbol] = int(recent_stats["trades_today_by_symbol"].get(symbol, 0)) + 1
            recent_stats["global_trades_today"] += 1
            if row.status_after == "OPEN_AT_END":
                open_rows.append(row)

    os.makedirs(args.output_dir, exist_ok=True)
    candidate_rows = [{**asdict(x), "quality_score": "", "accepted": True, "reject_reason": ""} for x in candidates]
    for name, rows in [("order_lifecycle.csv", [asdict(x) for x in lifecycle]), ("order_candidates.csv", candidate_rows), ("rejected_orders.csv", rejected), ("open_at_end.csv", [asdict(x) for x in open_rows])]:
        with open(os.path.join(args.output_dir, name), "w", newline="") as f:
            if not rows:
                f.write("")
                continue
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)

    summary = {
        "selected_symbols": len(universe), "total_candidates": len(candidates) + len(rejected), "total_rejected": len(rejected), "rejection_rate": (0.0 if (len(candidates)+len(rejected)) == 0 else len(rejected)/(len(candidates)+len(rejected))), "total_orders": len(lifecycle),
        "triggered_orders": sum(1 for r in lifecycle if r.status_after in {"TP_HIT", "SL_HIT", "OPEN_AT_END"}), "not_triggered_orders": sum(1 for r in lifecycle if r.status_after == "TIMEOUT"),
        "tp_hits": sum(1 for r in lifecycle if r.status_after == "TP_HIT"), "sl_hits": sum(1 for r in lifecycle if r.status_after == "SL_HIT"), "open_at_end": len(open_rows),
        "win_rate": 0.0 if not lifecycle else sum(1 for r in lifecycle if r.status_after == "TP_HIT") / len(lifecycle), "avg_rr": 0.0 if not lifecycle else sum(r.rr for r in lifecycle)/len(lifecycle),
        "avg_pnl_pct": 0.0 if not lifecycle else sum(r.net_pnl_pct for r in lifecycle)/len(lifecycle), "total_pnl_pct": sum(r.net_pnl_pct for r in lifecycle), "total_net_pnl_usdt": sum(r.net_pnl_usdt for r in lifecycle),
        "avg_hold_minutes": 0.0 if not lifecycle else sum(r.hold_minutes for r in lifecycle)/len(lifecycle), "performance_by_symbol": {}, "performance_by_regime": {}, "performance_by_setup_type": {}, "rejection_counts": json.dumps(rejection_counts, sort_keys=True), "cancel_counts": {},
    }
    with open(os.path.join(args.output_dir, "order_backtest_summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary.keys())); w.writeheader(); w.writerow(summary)


if __name__ == "__main__":
    main()
