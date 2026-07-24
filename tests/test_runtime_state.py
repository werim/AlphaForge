from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from alphaforge.persistence import init_db
from alphaforge.runtime_state import evaluate_runtime_recovery


def _engine(tmp_path: Path, name: str):
    return init_db(f"sqlite+pysqlite:///{tmp_path / name}")


def test_runtime_recovery_blocks_unknown_position_state(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "unknown-position.db")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO positions(position_id,symbol,qty,status) VALUES('p1','BTCUSDT',1,NULL)"))
    result = evaluate_runtime_recovery(engine, mode="PAPER", campaign_id="fresh")
    assert result["blocked"] is True
    assert result["availability"]["active_positions_available"] is False
    assert any("UNKNOWN_EXPOSURE_STATE" in error for error in result["local_exposure_query_errors"])


def test_runtime_recovery_blocks_unknown_order_state(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "unknown-order.db")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO orders(order_id,symbol,status) VALUES('o1','BTCUSDT','UNKNOWN')"))
    result = evaluate_runtime_recovery(engine, mode="PAPER", campaign_id="fresh")
    assert result["blocked"] is True
    assert result["availability"]["pending_orders_available"] is False
    assert any("UNKNOWN_EXPOSURE_STATE" in error for error in result["local_exposure_query_errors"])


def test_runtime_recovery_distinguishes_terminal_and_active_exposure(tmp_path: Path) -> None:
    terminal = _engine(tmp_path, "terminal.db")
    with terminal.begin() as conn:
        conn.execute(text("INSERT INTO positions(position_id,status) VALUES('p1','CLOSED')"))
        conn.execute(text("INSERT INTO orders(order_id,status) VALUES('o1','FILLED')"))
    terminal_result = evaluate_runtime_recovery(terminal, mode="PAPER", campaign_id="fresh")
    assert terminal_result["availability"]["active_positions_available"] is True
    assert terminal_result["availability"]["pending_orders_available"] is True
    assert terminal_result["current_exposure_check"]["active_positions"] == 0
    assert terminal_result["current_exposure_check"]["pending_orders"] == 0

    active = _engine(tmp_path, "active.db")
    with active.begin() as conn:
        conn.execute(text("INSERT INTO positions(position_id,status) VALUES('p1','OPEN')"))
    active_result = evaluate_runtime_recovery(active, mode="PAPER", campaign_id="fresh")
    assert active_result["blocked"] is True
    assert active_result["scope"] == "GLOBAL_EXECUTION_RISK"
    assert active_result["current_exposure_check"]["active_positions"] == 1
