import argparse
import csv
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Dict, Optional


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class VirtualOrder:
    symbol: str
    side: str
    entry: float
    sl: float
    tp: float
    score: float = 0.0
    rr: float = 0.0
    setup_type: str = "BACKTEST"
    setup_reason: str = "MANUAL_OR_IMPORTED"
    regime: str = "UNKNOWN"
    status: str = "CREATED"
    created_ts: int = 0
    triggered_ts: Optional[int] = None
    closed_ts: Optional[int] = None
    close_reason: str = ""
    close_price: Optional[float] = None
    net_pnl_pct: float = 0.0
    hold_minutes: float = 0.0


def parse_ts(value: str) -> int:
    if value.isdigit():
        return int(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def load_candles(path: str) -> List[Candle]:
    candles = []

    with open(path, newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            ts = (
                row.get("timestamp")
                or row.get("open_time")
                or row.get("time")
                or row.get("date")
            )

            candles.append(
                Candle(
                    timestamp=parse_ts(str(ts)),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0.0)),
                )
            )

    candles.sort(key=lambda x: x.timestamp)
    return candles


def touched_entry(order: VirtualOrder, candle: Candle) -> bool:
    return candle.low <= order.entry <= candle.high


def hit_tp(order: VirtualOrder, candle: Candle) -> bool:
    if order.side == "LONG":
        return candle.high >= order.tp
    return candle.low <= order.tp


def hit_sl(order: VirtualOrder, candle: Candle) -> bool:
    if order.side == "LONG":
        return candle.low <= order.sl
    return candle.high >= order.sl


def calc_pnl_pct(order: VirtualOrder, exit_price: float) -> float:
    if order.side == "LONG":
        return ((exit_price - order.entry) / order.entry) * 100
    return ((order.entry - exit_price) / order.entry) * 100


def simulate_order(
    symbol: str,
    candles: List[Candle],
    side: str,
    entry: float,
    sl: float,
    tp: float,
    timeout_minutes: int = 240,
    immediate: bool = False,
) -> VirtualOrder:

    order = VirtualOrder(
        symbol=symbol,
        side=side.upper(),
        entry=entry,
        sl=sl,
        tp=tp,
        created_ts=candles[0].timestamp,
    )

    risk = abs(entry - sl)
    reward = abs(tp - entry)
    order.rr = reward / risk if risk > 0 else 0.0

    if risk <= 0:
        order.status = "REJECTED"
        order.close_reason = "INVALID_RISK"
        return order

    order.status = "WATCHING"

    for candle in candles:
        age_minutes = (candle.timestamp - order.created_ts) / 60000

        if order.triggered_ts is None:
            if age_minutes > timeout_minutes:
                order.status = "CANCELLED"
                order.close_reason = "TIMEOUT_NOT_TRIGGERED"
                order.closed_ts = candle.timestamp
                return order

            if immediate or touched_entry(order, candle):
                order.status = "POSITION_OPENED"
                order.triggered_ts = candle.timestamp
            else:
                order.status = "WAITING_ENTRY_ZONE"
                continue

        # Conservative rule:
        # If TP and SL happen in same candle, assume SL first.
        if hit_sl(order, candle):
            order.status = "SL_HIT"
            order.close_reason = "SL_HIT"
            order.close_price = order.sl
            order.closed_ts = candle.timestamp
            break

        if hit_tp(order, candle):
            order.status = "TP_HIT"
            order.close_reason = "TP_HIT"
            order.close_price = order.tp
            order.closed_ts = candle.timestamp
            break

    if order.closed_ts is None:
        last = candles[-1]
        order.status = "OPEN_AT_END"
        order.close_reason = "BACKTEST_END"
        order.close_price = last.close
        order.closed_ts = last.timestamp

    order.net_pnl_pct = calc_pnl_pct(order, order.close_price)
    order.hold_minutes = (
        (order.closed_ts - order.triggered_ts) / 60000
        if order.triggered_ts
        else 0.0
    )

    return order


def write_results(path: str, rows: List[VirtualOrder]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

    fields = list(asdict(rows[0]).keys())

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

        for row in rows:
            writer.writerow(asdict(row))


def write_summary(path: str, rows: List[VirtualOrder]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

    total = len(rows)
    triggered = [r for r in rows if r.triggered_ts]
    wins = [r for r in rows if r.status == "TP_HIT"]
    losses = [r for r in rows if r.status == "SL_HIT"]

    summary = {
        "total_orders": total,
        "triggered_orders": len(triggered),
        "not_triggered_orders": total - len(triggered),
        "tp_hits": len(wins),
        "sl_hits": len(losses),
        "win_rate": round(len(wins) / len(triggered) * 100, 2) if triggered else 0.0,
        "avg_rr": round(sum(r.rr for r in rows) / total, 4) if total else 0.0,
        "avg_pnl_pct": round(sum(r.net_pnl_pct for r in rows) / total, 4) if total else 0.0,
        "total_pnl_pct": round(sum(r.net_pnl_pct for r in rows), 4),
        "avg_hold_minutes": round(sum(r.hold_minutes for r in triggered) / len(triggered), 2)
        if triggered
        else 0.0,
    }

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=summary.keys())
        writer.writeheader()
        writer.writerow(summary)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--symbol", required=True)
    parser.add_argument("--candles", required=True)
    parser.add_argument("--side", required=True, choices=["LONG", "SHORT"])
    parser.add_argument("--entry", type=float, required=True)
    parser.add_argument("--sl", type=float, required=True)
    parser.add_argument("--tp", type=float, required=True)
    parser.add_argument("--timeout-minutes", type=int, default=240)
    parser.add_argument("--immediate", action="store_true")
    parser.add_argument("--output-dir", default="data/backtest")

    args = parser.parse_args()

    candles = load_candles(args.candles)

    if not candles:
        raise RuntimeError("No candles loaded")

    result = simulate_order(
        symbol=args.symbol,
        candles=candles,
        side=args.side,
        entry=args.entry,
        sl=args.sl,
        tp=args.tp,
        timeout_minutes=args.timeout_minutes,
        immediate=args.immediate,
    )

    lifecycle_path = os.path.join(args.output_dir, "order_lifecycle.csv")
    summary_path = os.path.join(args.output_dir, "order_backtest_summary.csv")

    write_results(lifecycle_path, [result])
    write_summary(summary_path, [result])

    print("Backtest complete")
    print(f"Lifecycle: {lifecycle_path}")
    print(f"Summary: {summary_path}")
    print(result)


if __name__ == "__main__":
    main()