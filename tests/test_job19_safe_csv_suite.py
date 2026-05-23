from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from run_sql_audits import run_all_sql_audits


def test_job19_split_suite_exports_without_error_files(tmp_path: Path) -> None:
    db_path = tmp_path / "paper_runtime.db"
    out_dir = tmp_path / "reports"
    sql_dir = Path("sql/diagnostics/job19")

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE order_decisions (
                decision_id TEXT,
                signal_id TEXT,
                symbol TEXT,
                mode TEXT,
                phase TEXT,
                decision TEXT,
                reject_reason TEXT,
                score REAL,
                rr REAL,
                effective_rr REAL,
                execution_ctx_missing INTEGER,
                created_at TEXT
            );
            CREATE TABLE trade_lifecycle_events (
                signal_id TEXT,
                symbol TEXT,
                order_id TEXT,
                mode TEXT,
                lifecycle_state TEXT,
                created_at TEXT
            );
            INSERT INTO order_decisions VALUES
                ('d-1', 's-1', 'BTCUSDT', 'PAPER', 'final', 'ACCEPTED', NULL, 0.81, 2.1, 1.9, 0, '2026-05-23T06:00:00Z'),
                ('d-2', 's-2', 'ETHUSDT', 'PAPER', 'final', 'REJECTED', 'SCORE_BELOW_THRESHOLD', 0.31, 1.2, 1.0, 1, '2026-05-23T06:01:00Z');
            INSERT INTO trade_lifecycle_events VALUES
                ('s-1', 'BTCUSDT', 'o-1', 'PAPER', 'ORDER_PLACED', '2026-05-23T06:00:02Z'),
                ('s-2', 'ETHUSDT', NULL, 'PAPER', 'SIGNAL_REJECTED', '2026-05-23T06:01:01Z');
            """
        )

    failures = run_all_sql_audits(db_path, sql_dir, out_dir, "all_reports_summary.csv")
    assert failures == 0
    assert not list(out_dir.glob("*__ERROR.csv"))

    with (out_dir / "all_reports_summary.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == len(list(sql_dir.glob("*.sql")))
    assert {row["status"] for row in rows} == {"OK"}


def test_legacy_job19_entrypoint_exports_as_single_result(tmp_path: Path) -> None:
    db_path = tmp_path / "paper_runtime.db"
    out_dir = tmp_path / "reports"
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()

    source = Path("sql/paper_runtime_decision_audit.sql").read_text(encoding="utf-8")
    (sql_dir / "paper_runtime_decision_audit.sql").write_text(source, encoding="utf-8")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE order_decisions (mode TEXT, phase TEXT, decision TEXT)"
        )
        conn.execute(
            "INSERT INTO order_decisions VALUES ('PAPER', 'final', 'REJECTED')"
        )

    failures = run_all_sql_audits(db_path, sql_dir, out_dir, "all_reports_summary.csv")
    assert failures == 0
    assert (out_dir / "paper_runtime_decision_audit.csv").exists()
    assert not list(out_dir.glob("*__ERROR.csv"))
