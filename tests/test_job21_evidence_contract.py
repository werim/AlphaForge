from alphaforge.persistence import init_db
from alphaforge.rollback_evidence import latest_persisted_rollback_evidence


def test_job21_evidence_absence_is_not_verified() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    result = latest_persisted_rollback_evidence(engine)
    assert result["rollback_evidence_verified"] is False
