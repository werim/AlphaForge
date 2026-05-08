from __future__ import annotations

from pathlib import Path
import runpy


def test_fetch_expectancy_stat_exported() -> None:
    from alphaforge.persistence import fetch_expectancy_stat

    assert callable(fetch_expectancy_stat)


def test_backtest_order_bootstraps_src_path() -> None:
    module_globals = runpy.run_path(str(Path(__file__).resolve().parents[1] / "backtest_order.py"))
    assert "main" in module_globals
