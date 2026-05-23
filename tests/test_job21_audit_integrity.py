from pathlib import Path

from alphaforge.contracts import LifecycleEventType, validate_transition

ROOT = Path(__file__).resolve().parents[1]


def test_new_signal_can_follow_rejected_observation() -> None:
    assert validate_transition(LifecycleEventType.SIGNAL_REJECTED.value, LifecycleEventType.SIGNAL_CREATED.value)
    assert validate_transition(LifecycleEventType.SIGNAL_CREATED.value, LifecycleEventType.SIGNAL_REJECTED.value)


def test_new_signal_can_follow_error_observation() -> None:
    assert validate_transition(LifecycleEventType.ERROR.value, LifecycleEventType.SIGNAL_CREATED.value)


def test_execution_diagnostics_use_decision_cost_surface() -> None:
    job05 = (ROOT / 'sql/diagnostics/job05_execution_context_population.sql').read_text(encoding='utf-8')
    job09 = (ROOT / 'sql/diagnostics/job09_exchange_safety_gates.sql').read_text(encoding='utf-8')
    assert 'FROM order_decisions' in job05
    assert 'FROM order_decisions' in job09
    assert 'spread_source' not in job05


def test_canonical_audit_entrypoint_is_read_only_runner_safe() -> None:
    sql = (ROOT / 'sql/diagnostics/job19_paper_reject_rate_decision_quality_audit.sql').read_text(encoding='utf-8')
    assert '.headers' not in sql
    assert '.mode' not in sql
    executable = '\n'.join(line for line in sql.splitlines() if not line.strip().startswith('--')).lstrip().lower()
    assert executable.startswith('with')
    assert 'lifecycle_integrity_failure' in executable


def test_paper_classification_fails_closed_when_evidence_is_broken() -> None:
    sql = (ROOT / 'sql/diagnostics/job06_paper_runtime_db_audit.sql').read_text(encoding='utf-8').lower()
    assert 'lifecycle_integrity_failure' in sql
    assert 'execution_context_unverified' in sql
    assert 'no_accepted_sample' in sql
