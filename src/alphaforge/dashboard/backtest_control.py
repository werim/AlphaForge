from __future__ import annotations

import csv
import subprocess
import sys
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
        if "HistoricalDataError" in stderr or "Historical coverage" in stderr or "No candles returned" in stderr:
            result.error_message = INSUFFICIENT_BINANCE_DATA_MESSAGE
        else:
            result.error_message = stderr[-1200:]
        return result

    summary_path = output_dir / "order_backtest_summary.csv"
    lifecycle_path = output_dir / "order_lifecycle.csv"
    rejected_path = output_dir / "rejected_orders.csv"
    summary = _read_first_csv_row(summary_path)
    lifecycle_rows = _read_csv_rows(lifecycle_path)
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
    if result.total_candidates is None or result.rejected_signals is None:
        result.lifecycle_warning = "Lifecycle/reject metrics unavailable from generated backtest artifacts; values are shown as unavailable, not zero."
    unavailable_markers = {"", "UNAVAILABLE_BACKTEST", "None", "null"}
    if not lifecycle_rows or any(str(row.get("spread_pct", "")).strip() in unavailable_markers for row in lifecycle_rows[:50]):
        result.execution_context_warning = "Execution context is incomplete for at least part of this backtest; unknown spread/slippage/funding is unavailable, not assumed zero."
    return result
