from __future__ import annotations

import csv
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from alphaforge.config import load_config_from_env
from alphaforge.contracts import canonical_utc_timestamp

SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("1m", "15m", "1h", "4h", "1d")
INSUFFICIENT_BINANCE_DATA_MESSAGE = "Not enough historical data returned by Binance for the requested period. Try fewer days or a higher timeframe."


@dataclass(slots=True)
class DashboardBacktestRequest:
    last_days: int
    symbols: list[str]
    timeframe: str
    initial_balance: float
    max_symbols: int


@dataclass(slots=True)
class DashboardBacktestResult:
    status: str
    period: str
    symbols: list[str]
    timeframe: str
    initial_balance: float
    max_symbols: int
    output_dir: str | None = None
    summary_path: str | None = None
    lifecycle_path: str | None = None
    rejected_path: str | None = None
    total_candidates: Any = None
    accepted_trades: Any = None
    rejected_signals: Any = None
    win_count: Any = None
    loss_count: Any = None
    open_count: Any = None
    net_pnl: Any = None
    total_return_pct: Any = None
    max_drawdown: Any = None
    lifecycle_warning: str | None = None
    execution_context_warning: str | None = None
    error_message: str | None = None
    command: list[str] = field(default_factory=list)
    top_rejection_reasons: list[dict[str, Any]] = field(default_factory=list)
    signal_rows_count: int | None = None
    symbol_selector_reject_count: int | None = None
    score_distribution: dict[str, Any] = field(default_factory=dict)
    rr_distribution: dict[str, Any] = field(default_factory=dict)
    effective_rr_distribution: dict[str, Any] = field(default_factory=dict)
    pre_later_gate_pass_count: int | None = None


def default_form_values() -> dict[str, Any]:
    cfg = load_config_from_env()
    default_timeframe = cfg.backtest.timeframe if cfg.backtest.timeframe in SUPPORTED_TIMEFRAMES else "15m"
    return {
        "last_days": 30,
        "symbols": "BTCUSDT,ETHUSDT",
        "timeframe": default_timeframe,
        "initial_balance": 10000,
        "max_symbols": max(1, min(int(cfg.backtest.top_n), 200)),
        "timeframes": SUPPORTED_TIMEFRAMES,
    }


def parse_backtest_form(form: Mapping[str, Any]) -> tuple[DashboardBacktestRequest | None, dict[str, str]]:
    errors: dict[str, str] = {}

    def parse_int(name: str, default: int, min_value: int, max_value: int) -> int:
        raw = form.get(name, default)
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            errors[name] = f"{name} must be an integer."
            return default
        if value < min_value or value > max_value:
            errors[name] = f"{name} must be between {min_value} and {max_value}."
        return value

    def parse_float(name: str, default: float, min_value: float, max_value: float) -> float:
        raw = form.get(name, default)
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            errors[name] = f"{name} must be numeric."
            return default
        if value < min_value or value > max_value:
            errors[name] = f"{name} must be between {min_value:g} and {max_value:g}."
        return value

    last_days = parse_int("last_days", 30, 1, 730)
    initial_balance = parse_float("initial_balance", 10000.0, 100.0, 10_000_000.0)
    max_symbols = parse_int("max_symbols", default_form_values()["max_symbols"], 1, 200)
    symbols = [item.strip().upper() for item in str(form.get("symbols", "")).split(",") if item.strip()]
    if not symbols:
        errors["symbols"] = "symbols must contain at least one non-empty symbol."
    timeframe = str(form.get("timeframe", "")).strip()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        errors["timeframe"] = f"timeframe must be one of: {', '.join(SUPPORTED_TIMEFRAMES)}."
    if errors:
        return None, errors
    return DashboardBacktestRequest(last_days=last_days, symbols=symbols, timeframe=timeframe, initial_balance=initial_balance, max_symbols=max_symbols), {}


def _read_first_csv_row(path: Path) -> dict[str, str]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    with path.open(newline="") as fh:
        return next(csv.DictReader(fh), {}) or {}


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _safe_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_float_or_none(value: Any) -> float | None:
    try:
        if value in (None, "", "None", "null", "UNAVAILABLE_BACKTEST"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _numeric_distribution(rows: list[dict[str, str]], field: str) -> dict[str, Any]:
    values = [_safe_float_or_none(row.get(field)) for row in rows]
    values = [v for v in values if v is not None]
    if not values:
        return {"count": 0, "min": None, "mean": None, "p50": None, "p90": None, "max": None}
    ordered = sorted(values)
    def pct(q: float) -> float:
        idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
        return round(ordered[idx], 6)
    return {"count": len(values), "min": round(ordered[0], 6), "mean": round(sum(values) / len(values), 6), "p50": pct(0.5), "p90": pct(0.9), "max": round(ordered[-1], 6)}


def _rejection_diagnostics(rows: list[dict[str, str]]) -> dict[str, Any]:
    reasons = Counter((row.get("reject_reason") or row.get("reason") or "UNKNOWN").strip() or "UNKNOWN" for row in rows)
    total = len(rows)
    signal_rows = sum(1 for row in rows if str(row.get("lifecycle_state", "")).strip() == "SIGNAL_REJECTED")
    selector_rows = sum(1 for row in rows if str(row.get("lifecycle_state", "")).strip() == "SYMBOL_SELECTOR_REJECT" or str(row.get("source", "")).strip() == "SYMBOL_SELECTOR")
    def passes(row: dict[str, str]) -> bool:
        score = _safe_float_or_none(row.get("score"))
        rr = _safe_float_or_none(row.get("raw_rr") if row.get("raw_rr") not in (None, "") else row.get("rr"))
        expectancy = _safe_float_or_none(row.get("expectancy") if row.get("expectancy") not in (None, "") else row.get("expectancy_bucket"))
        min_score = _safe_float_or_none(row.get("min_required_score")) or 7.5
        return score is not None and rr is not None and expectancy is not None and score >= min_score and rr >= 1.3 and expectancy >= 0.0
    return {
        "top_rejection_reasons": [{"reason": reason, "count": count, "ratio": (count / total if total else None)} for reason, count in reasons.most_common(8)],
        "signal_rows_count": signal_rows,
        "symbol_selector_reject_count": selector_rows,
        "score_distribution": _numeric_distribution(rows, "score"),
        "rr_distribution": _numeric_distribution(rows, "raw_rr") if any("raw_rr" in r for r in rows) else _numeric_distribution(rows, "rr"),
        "effective_rr_distribution": _numeric_distribution(rows, "effective_rr"),
        "pre_later_gate_pass_count": sum(1 for row in rows if passes(row)),
    }


def run_dashboard_backtest(request: DashboardBacktestRequest) -> DashboardBacktestResult:
    """Run the existing backtest_order.py pipeline with a BACKTEST-only command boundary."""
    cfg = load_config_from_env()
    timestamp = canonical_utc_timestamp().replace(":", "").replace("-", "").replace(".", "")
    output_dir = Path(cfg.backtest.output_dir) / "dashboard" / timestamp
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "backtest_order.py"
    symbols = request.symbols[: request.max_symbols]
    command = [
        sys.executable,
        str(script),
        "--mode",
        "BACKTEST",
        "--last-n-days",
        str(request.last_days),
        "--symbols",
        ",".join(symbols),
        "--top-n",
        str(request.max_symbols),
        "--interval",
        request.timeframe,
        "--balance",
        str(request.initial_balance),
        "--output-dir",
        str(output_dir),
        "--force-refresh",
    ]
    period = f"last {request.last_days} days"
    result = DashboardBacktestResult("RUNNING", period, symbols, request.timeframe, request.initial_balance, request.max_symbols, output_dir=str(output_dir), command=command)
    try:
        completed = subprocess.run(command, cwd=repo_root, text=True, capture_output=True, timeout=600, check=False)
    except Exception as exc:  # subprocess/environment failure, not strategy logic
        result.status = "FAILED"
        result.error_message = f"Backtest failed before completion: {exc}"
        return result
    if completed.returncode != 0:
        result.status = "FAILED"
        stderr = (completed.stderr or completed.stdout or "BACKTEST_PROCESS_FAILED").strip()
        if "HistoricalDataError" in stderr or "Historical coverage" in stderr or "No candles returned" in stderr or "Insufficient candles" in stderr:
            detail = stderr[-1200:]
            result.error_message = f"{INSUFFICIENT_BINANCE_DATA_MESSAGE} Details: {detail}"
        else:
            result.error_message = stderr[-1200:]
        return result

    summary_path = output_dir / "order_backtest_summary.csv"
    lifecycle_path = output_dir / "order_lifecycle.csv"
    rejected_path = output_dir / "rejected_orders.csv"
    summary = _read_first_csv_row(summary_path)
    lifecycle_rows = _read_csv_rows(lifecycle_path)
    rejected_rows = _read_csv_rows(rejected_path)
    result.status = "COMPLETED"
    result.summary_path = str(summary_path) if summary_path.exists() else None
    result.lifecycle_path = str(lifecycle_path) if lifecycle_path.exists() else None
    result.rejected_path = str(rejected_path) if rejected_path.exists() else None
    result.total_candidates = _safe_int(summary.get("total_candidates"))
    result.accepted_trades = _safe_int(summary.get("accepted_count"))
    result.rejected_signals = _safe_int(summary.get("rejected_count") or summary.get("total_rejected"))
    result.win_count = _safe_int(summary.get("tp_hits"))
    result.loss_count = _safe_int(summary.get("sl_hits"))
    result.open_count = _safe_int(summary.get("open_at_end"))
    result.net_pnl = summary.get("total_net_pnl_usdt")
    result.total_return_pct = summary.get("total_pnl_pct")
    result.max_drawdown = None
    diagnostics = _rejection_diagnostics(rejected_rows)
    result.top_rejection_reasons = diagnostics["top_rejection_reasons"]
    result.signal_rows_count = diagnostics["signal_rows_count"]
    result.symbol_selector_reject_count = diagnostics["symbol_selector_reject_count"]
    result.score_distribution = diagnostics["score_distribution"]
    result.rr_distribution = diagnostics["rr_distribution"]
    result.effective_rr_distribution = diagnostics["effective_rr_distribution"]
    result.pre_later_gate_pass_count = diagnostics["pre_later_gate_pass_count"]
    if result.total_candidates is None or result.rejected_signals is None:
        result.lifecycle_warning = "Lifecycle/reject metrics unavailable from generated backtest artifacts; values are shown as unavailable, not zero."
    unavailable_markers = {"", "UNAVAILABLE_BACKTEST", "None", "null"}
    if not lifecycle_rows or any(str(row.get("spread_pct", "")).strip() in unavailable_markers for row in lifecycle_rows[:50]):
        result.execution_context_warning = "Execution context is incomplete for at least part of this backtest; unknown spread/slippage/funding is unavailable, not assumed zero."
    return result
