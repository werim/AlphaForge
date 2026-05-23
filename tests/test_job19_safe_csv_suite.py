from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

# The exporter is a repository-root CLI module, while CI's test path exposes
# the application package under src/. Add the repository root explicitly so
# this regression test exercises the real CLI implementation.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from run_sql_audits import run_all_sql_audits


def _build_job19_fixture(db_path: Path) -> None:
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


def test_job19_split_suite_exports_without_error_files(tmp_path: Path) -> None:
    db_path = tmp_path / "paper_runtime.db"
    out_dir = tmp_path / "reports"
    sql_dir = Path("sql/diagnostics/job19")
    _build_job19_fixture(db_path)

    failures = run_all_sql_audits(db_path, sql_dir, out_dir, "all_reports_summary.csv")
    assert failures == 0
    assert not list(out_dir.glob("*__ERROR.csv"))

    with (out_dir / "all_reports_summary.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == len(list(sql_dir.glob("*.sql")))
    assert {row["status"] for row in rows} == {"OK"}
    assert {row["statement_index"] for row in rows} == {"1"}
    assert {row["statement_count"] for row in rows} == {"1"}


def test_legacy_job19_entrypoint_exports_as_single_result(tmp_path: Path) -> None:
    db_path = tmp_path / "paper_runtime.db"
    out_dir = tmp_path / "reports"
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()

    source = Path("sql/paper_runtime_decision_audit.sql").read_text(encoding="utf-8")
    (sql_dir / "paper_runtime_decision_audit.sql").write_text(source, encoding="utf-8")

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE order_decisions (mode TEXT, phase TEXT, decision TEXT)")
        conn.execute("INSERT INTO order_decisions VALUES ('PAPER', 'final', 'REJECTED')")

    failures = run_all_sql_audits(db_path, sql_dir, out_dir, "all_reports_summary.csv")
    assert failures == 0
    assert (out_dir / "paper_runtime_decision_audit.csv").exists()
    assert not list(out_dir.glob("*__ERROR.csv"))


def test_multi_statement_audit_exports_each_result_query_in_order(tmp_path: Path) -> None:
    db_path = tmp_path / "paper_runtime.db"
    out_dir = tmp_path / "reports"
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    _build_job19_fixture(db_path)

    (sql_dir / "job04_effective_rr.sql").write_text(
        """
        -- JOB-04 Effective RR Canonicalization Diagnostics
        -- 1. The semicolon in this comment does not split a query;
        SELECT mode, COUNT(*) AS rows
        FROM order_decisions
        GROUP BY mode;

        /* 2. a block comment; remains harmless */
        SELECT 'literal;not-a-delimiter' AS note, COUNT(*) AS low_rr_rows
        FROM order_decisions
        WHERE effective_rr < 1.1;

        -- 3. execution completeness
        SELECT execution_ctx_missing, COUNT(*) AS rows
        FROM order_decisions
        GROUP BY execution_ctx_missing;
        """,
        encoding="utf-8",
    )

    failures = run_all_sql_audits(db_path, sql_dir, out_dir, "all_reports_summary.csv")
    assert failures == 0
    assert (out_dir / "job04_effective_rr__01.csv").exists()
    assert (out_dir / "job04_effective_rr__02.csv").exists()
    assert (out_dir / "job04_effective_rr__03.csv").exists()
    assert not (out_dir / "job04_effective_rr.csv").exists()
    assert not list(out_dir.glob("*__ERROR.csv"))

    with (out_dir / "all_reports_summary.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["statement_index"] for row in rows] == ["1", "2", "3"]
    assert {row["statement_count"] for row in rows} == {"3"}
    assert {row["status"] for row in rows} == {"OK"}


def test_file_with_mutating_statement_is_rejected_before_any_select_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "paper_runtime.db"
    out_dir = tmp_path / "reports"
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    _build_job19_fixture(db_path)

    (sql_dir / "unsafe.sql").write_text(
        "SELECT COUNT(*) AS before_count FROM order_decisions; DELETE FROM order_decisions;",
        encoding="utf-8",
    )

    failures = run_all_sql_audits(db_path, sql_dir, out_dir, "all_reports_summary.csv")
    assert failures == 1
    assert (out_dir / "unsafe__ERROR.csv").exists()
    assert not (out_dir / "unsafe__01.csv").exists()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM order_decisions").fetchone()[0] == 2
