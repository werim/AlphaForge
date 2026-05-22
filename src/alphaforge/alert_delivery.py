from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine

from alphaforge.contracts import canonical_utc_timestamp


Transport = Callable[[str, bytes, Mapping[str, str], float], Mapping[str, Any]]
_ALLOWED_FIELDS = (
    "provider_configured",
    "evidence_status",
    "observability_evidence_source",
    "alert_delivery_verified",
    "delivery_attempted",
    "delivery_acknowledged",
    "non_trading_probe_verified",
    "probe_id",
    "endpoint_origin",
    "blocking_reasons",
)


@dataclass(frozen=True, slots=True)
class AlertDeliveryProbeConfig:
    endpoint_url: str
    bearer_token: str | None = None
    timeout_sec: float = 2.0


class WebhookAlertDeliveryEvidenceProvider:
    """Emit a diagnostic alert probe and require acknowledgement of its id."""

    def __init__(
        self,
        config: AlertDeliveryProbeConfig,
        *,
        transport: Transport | None = None,
        probe_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or self._post_json
        self._probe_id_factory = probe_id_factory or (lambda: f"alert-probe-{uuid.uuid4().hex}")

    def snapshot(self) -> dict[str, Any]:
        endpoint_origin = self._validated_endpoint_origin()
        probe_id = str(self._probe_id_factory()).strip()
        if not probe_id:
            return self._incomplete("EMPTY_PROBE_ID", endpoint_origin="UNAVAILABLE")
        if endpoint_origin is None:
            return self._incomplete("INVALID_OR_INSECURE_ENDPOINT", endpoint_origin="UNAVAILABLE", probe_id=probe_id)
        payload = {
            "event_type": "ALPHAFORGE_LIVE_READINESS_ALERT_PROBE",
            "probe_id": probe_id,
            "severity": "INFO",
            "non_trading_probe": True,
            "requires_acknowledgement": True,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        token = str(self.config.bearer_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = dict(self._transport(self.config.endpoint_url, json.dumps(payload, separators=(",", ":")).encode("utf-8"), headers, float(self.config.timeout_sec)))
        except Exception as exc:
            return self._incomplete(f"DELIVERY_EXCEPTION:{exc.__class__.__name__}", endpoint_origin=endpoint_origin, probe_id=probe_id, attempted=True)
        acknowledged = (
            bool(response.get("acknowledged", False))
            and str(response.get("probe_id") or "") == probe_id
            and str(response.get("status") or "").strip().upper() in {"ACKNOWLEDGED", "DELIVERED"}
        )
        if not acknowledged:
            return self._incomplete("ACKNOWLEDGEMENT_NOT_VERIFIED", endpoint_origin=endpoint_origin, probe_id=probe_id, attempted=True)
        return {
            "provider_configured": True,
            "evidence_status": "COMPLETE",
            "observability_evidence_source": "MEASURED_PROBE",
            "alert_delivery_verified": True,
            "delivery_attempted": True,
            "delivery_acknowledged": True,
            "non_trading_probe_verified": True,
            "probe_id": probe_id,
            "endpoint_origin": endpoint_origin,
            "blocking_reasons": [],
        }

    def _validated_endpoint_origin(self) -> str | None:
        parts = urlsplit(str(self.config.endpoint_url or "").strip())
        if parts.scheme.lower() != "https" or not parts.netloc or parts.username or parts.password:
            return None
        return f"https://{parts.netloc}"

    @staticmethod
    def _incomplete(reason: str, *, endpoint_origin: str, probe_id: str | None = None, attempted: bool = False) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "provider_configured": True,
            "evidence_status": "INCOMPLETE",
            "observability_evidence_source": "MEASURED_PROBE",
            "alert_delivery_verified": False,
            "delivery_attempted": attempted,
            "delivery_acknowledged": False,
            "non_trading_probe_verified": True,
            "endpoint_origin": endpoint_origin,
            "blocking_reasons": [reason],
        }
        if probe_id:
            evidence["probe_id"] = probe_id
        return evidence

    @staticmethod
    def _post_json(url: str, payload: bytes, headers: Mapping[str, str], timeout_sec: float) -> Mapping[str, Any]:
        request = Request(url, data=payload, headers=dict(headers), method="POST")
        with urlopen(request, timeout=timeout_sec) as response:
            status_code = int(getattr(response, "status", response.getcode()))
            if status_code < 200 or status_code >= 300:
                return {"status": f"HTTP_{status_code}", "acknowledged": False}
            body = response.read().decode("utf-8")
        parsed = json.loads(body or "{}")
        return parsed if isinstance(parsed, Mapping) else {"acknowledged": False, "status": "INVALID_RESPONSE"}


def _allowed_payload(evidence: Mapping[str, Any]) -> dict[str, Any]:
    payload = {key: evidence.get(key) for key in _ALLOWED_FIELDS if key in evidence}
    payload["provider_configured"] = bool(payload.get("provider_configured", False))
    payload["evidence_status"] = str(payload.get("evidence_status") or "INCOMPLETE").upper()
    payload["observability_evidence_source"] = str(payload.get("observability_evidence_source") or "UNVERIFIED").upper()
    payload["alert_delivery_verified"] = bool(payload.get("alert_delivery_verified", False))
    payload["delivery_attempted"] = bool(payload.get("delivery_attempted", False))
    payload["delivery_acknowledged"] = bool(payload.get("delivery_acknowledged", False))
    payload["non_trading_probe_verified"] = bool(payload.get("non_trading_probe_verified", False))
    payload["endpoint_origin"] = str(payload.get("endpoint_origin") or "UNAVAILABLE")
    reasons = payload.get("blocking_reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    payload["blocking_reasons"] = [str(reason)[:100] for reason in reasons]
    if payload.get("probe_id") is not None:
        payload["probe_id"] = str(payload["probe_id"])[:120]
    return payload


def persist_alert_delivery_evidence(engine: Engine, evidence: Mapping[str, Any]) -> None:
    payload = _allowed_payload(evidence)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS live_alert_delivery_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                probe_id TEXT,
                evidence_status TEXT NOT NULL,
                alert_delivery_verified INTEGER NOT NULL,
                endpoint_origin TEXT NOT NULL,
                evidence_payload TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO live_alert_delivery_evidence(recorded_at, probe_id, evidence_status, alert_delivery_verified, endpoint_origin, evidence_payload)
            VALUES (:recorded_at, :probe_id, :evidence_status, :verified, :endpoint_origin, :payload)
        """), {
            "recorded_at": canonical_utc_timestamp(),
            "probe_id": payload.get("probe_id"),
            "evidence_status": payload["evidence_status"],
            "verified": 1 if payload["alert_delivery_verified"] else 0,
            "endpoint_origin": payload["endpoint_origin"],
            "payload": json.dumps(payload, sort_keys=True),
        })
