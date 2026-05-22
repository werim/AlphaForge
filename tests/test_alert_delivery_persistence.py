from __future__ import annotations

import json

from sqlalchemy import text

from alphaforge.alert_delivery import persist_alert_delivery_evidence
from alphaforge.persistence import init_db


def test_persist_alert_delivery_evidence_stores_only_allowed_fields() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    evidence = {
        "provider_configured": True,
        "evidence_status": "COMPLETE",
        "observability_evidence_source": "MEASURED_PROBE",
        "alert_delivery_verified": True,
        "delivery_attempted": True,
        "delivery_acknowledged": True,
        "non_trading_probe_verified": True,
        "probe_id": "probe-001",
        "endpoint_origin": "https://alerts.example.test",
        "blocking_reasons": [],
        "unexpected_detail": "must-not-be-stored",
    }

    persist_alert_delivery_evidence(engine, evidence)

    with engine.begin() as conn:
        row = conn.execute(text("SELECT evidence_status, alert_delivery_verified, endpoint_origin, evidence_payload FROM live_alert_delivery_evidence")).mappings().one()
    payload = json.loads(row["evidence_payload"])
    assert row["evidence_status"] == "COMPLETE"
    assert row["alert_delivery_verified"] == 1
    assert row["endpoint_origin"] == "https://alerts.example.test"
    assert payload["probe_id"] == "probe-001"
    assert "unexpected_detail" not in payload
    assert "must-not-be-stored" not in row["evidence_payload"]


def test_persist_alert_delivery_evidence_normalizes_incomplete_result() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    persist_alert_delivery_evidence(engine, {
        "evidence_status": "incomplete",
        "alert_delivery_verified": False,
        "endpoint_origin": "UNAVAILABLE",
        "blocking_reasons": ["NO_ACK"],
    })

    with engine.begin() as conn:
        row = conn.execute(text("SELECT evidence_status, alert_delivery_verified, evidence_payload FROM live_alert_delivery_evidence")).mappings().one()
    payload = json.loads(row["evidence_payload"])
    assert row["evidence_status"] == "INCOMPLETE"
    assert row["alert_delivery_verified"] == 0
    assert payload["alert_delivery_verified"] is False
    assert payload["blocking_reasons"] == ["NO_ACK"]
