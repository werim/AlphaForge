from pathlib import Path

from alphaforge.persistence import init_db


EXPECTED_REPORTS = {
    "00_scope.sql",
    "01_phase_decomposition.sql",
    "02_canonical_decision_totals.sql",
    "03_final_decisions_by_symbol.sql",
    "04_reject_reason_quality.sql",
    "05_rejected_field_completeness.sql",
    "06_duplicate_decision_id.sql",
    "07_conflicting_final_signal_decisions.sql",
    "08_score_rr_variability.sql",
    "09_execution_context_availability.sql",
    "10_json_execution_context_availability.sql",
    "11_lifecycle_state_distribution.sql",
    "12_rejected_without_rejection_event.sql",
    "13_order_placed_without_accepted_decision.sql",
    "14_terminal_state_without_order_placed.sql",
    "15_verdict_input_counts.sql",
}


def _query_text(path: Path) -> str:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("--")]
    return "\n".join(lines).strip()


def test_job19_reports_are_one_read_only_query_and_execute_on_runtime_schema() -> None:
    report_dir = Path("sql/diagnostics/job19")
    files = sorted(report_dir.glob("*.sql"))
    assert {file.name for file in files} == EXPECTED_REPORTS

    engine = init_db("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        for path in files:
            query = _query_text(path)
            assert query.upper().startswith(("SELECT", "WITH")), path.name
            assert query.count(";") == 1, path.name
            connection.exec_driver_sql(query).fetchall()


def test_job19_legacy_entrypoints_are_exporter_safe() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    paths = [
        Path("sql/paper_runtime_decision_audit.sql"),
        Path("sql/diagnostics/job19_paper_reject_rate_decision_quality_audit.sql"),
    ]
    with engine.connect() as connection:
        for path in paths:
            query = _query_text(path)
            assert query.upper().startswith("SELECT"), path.as_posix()
            assert query.count(";") == 1, path.as_posix()
            connection.exec_driver_sql(query).fetchall()
