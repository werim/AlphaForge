from __future__ import annotations

import json

from alphaforge.alert_delivery import AlertDeliveryProbeConfig, WebhookAlertDeliveryEvidenceProvider


def test_acknowledged_alert_probe_produces_complete_sanitized_evidence() -> None:
    observed: dict[str, object] = {}

    def transport(url: str, payload: bytes, headers, timeout_sec: float):
        observed["url"] = url
        observed["headers"] = dict(headers)
        observed["timeout"] = timeout_sec
        request_payload = json.loads(payload.decode("utf-8"))
        observed["payload"] = request_payload
        return {"status": "ACKNOWLEDGED", "acknowledged": True, "probe_id": request_payload["probe_id"]}

    provider = WebhookAlertDeliveryEvidenceProvider(
        AlertDeliveryProbeConfig(
            endpoint_url="https://alerts.example.test/readiness?route=opaque-test-value",
            bearer_token="credential-placeholder",
            timeout_sec=1.5,
        ),
        transport=transport,
        probe_id_factory=lambda: "probe-001",
    )
    evidence = provider.snapshot()

    assert evidence["evidence_status"] == "COMPLETE"
    assert evidence["alert_delivery_verified"] is True
    assert evidence["delivery_acknowledged"] is True
    assert evidence["non_trading_probe_verified"] is True
    assert evidence["probe_id"] == "probe-001"
    assert evidence["endpoint_origin"] == "https://alerts.example.test"
    assert evidence["blocking_reasons"] == []
    assert observed["headers"]["Authorization"] == "Bearer credential-placeholder"
    assert observed["payload"] == {
        "event_type": "ALPHAFORGE_LIVE_READINESS_ALERT_PROBE",
        "probe_id": "probe-001",
        "severity": "INFO",
        "non_trading_probe": True,
        "requires_acknowledgement": True,
    }
    evidence_text = json.dumps(evidence)
    assert "credential-placeholder" not in evidence_text
    assert "opaque-test-value" not in evidence_text
    assert "symbol" not in evidence_text
    assert "order" not in evidence_text.lower()


def test_alert_probe_fails_closed_without_matching_acknowledgement() -> None:
    def transport(url: str, payload: bytes, headers, timeout_sec: float):
        return {"status": "ACKNOWLEDGED", "acknowledged": True, "probe_id": "wrong-probe"}

    provider = WebhookAlertDeliveryEvidenceProvider(
        AlertDeliveryProbeConfig(endpoint_url="https://alerts.example.test/readiness"),
        transport=transport,
        probe_id_factory=lambda: "probe-expected",
    )
    evidence = provider.snapshot()

    assert evidence["evidence_status"] == "INCOMPLETE"
    assert evidence["alert_delivery_verified"] is False
    assert evidence["delivery_attempted"] is True
    assert evidence["delivery_acknowledged"] is False
    assert evidence["blocking_reasons"] == ["ACKNOWLEDGEMENT_NOT_VERIFIED"]


def test_alert_probe_rejects_insecure_endpoint_without_transport_call() -> None:
    called = False

    def transport(url: str, payload: bytes, headers, timeout_sec: float):
        nonlocal called
        called = True
        return {"status": "ACKNOWLEDGED", "acknowledged": True, "probe_id": "probe-001"}

    provider = WebhookAlertDeliveryEvidenceProvider(
        AlertDeliveryProbeConfig(endpoint_url="http://alerts.example.test/readiness"),
        transport=transport,
        probe_id_factory=lambda: "probe-001",
    )
    evidence = provider.snapshot()

    assert called is False
    assert evidence["evidence_status"] == "INCOMPLETE"
    assert evidence["alert_delivery_verified"] is False
    assert evidence["blocking_reasons"] == ["INVALID_OR_INSECURE_ENDPOINT"]


def test_alert_probe_exception_evidence_does_not_leak_details() -> None:
    def transport(url: str, payload: bytes, headers, timeout_sec: float):
        raise RuntimeError("transport-private-detail")

    provider = WebhookAlertDeliveryEvidenceProvider(
        AlertDeliveryProbeConfig(endpoint_url="https://alerts.example.test/readiness?route=opaque-url-value", bearer_token="credential-placeholder"),
        transport=transport,
        probe_id_factory=lambda: "probe-001",
    )
    evidence = provider.snapshot()
    evidence_text = json.dumps(evidence)

    assert evidence["evidence_status"] == "INCOMPLETE"
    assert evidence["alert_delivery_verified"] is False
    assert evidence["blocking_reasons"] == ["DELIVERY_EXCEPTION:RuntimeError"]
    assert "credential-placeholder" not in evidence_text
    assert "opaque-url-value" not in evidence_text
    assert "transport-private-detail" not in evidence_text
