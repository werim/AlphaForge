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
    "provider_configured", "evidence_status", "observability_evidence_source",
    "alert_delivery_verified", "delivery_attempted", "delivery_acknowledged",
    "non_trading_probe_verified", "probe_id", "endpoint_origin", "blocking_reasons",
)


@dataclass(frozen=True, slots=True)
class AlertDeliveryProbeConfig:
    endpoint_url: str
    bearer_token: str | None = None
    timeout_sec: float = 2.0


class WebhookAlertDeliveryEvidenceProvider:
    """Emit a non-trading diagnostic alert and require acknowledgement of its probe id."""

    def __init__(self, config: AlertDeliveryProbeConfig, *, transport: Transport | None = None, probe_id_factory: Callable[[], str] | None = None) -> None:
        self.config = config
        self._transport = transport or self._post_json
        self._probe_id_factory = probe_id_factory or (lambda: f"alert-probe-{uuid.uuid4().hex}")

    def snapshot(self) -> dict[str, Any]:
        origin = self._origin()
        probe_id = str(self._probe_id_factory()).strip()
        if not probe_id:
            return self._incomplete("EMPTY_PROBE_ID", "UNAVAILABLE")
        if origin is None:
            return self._incomplete("INVALID_OR_INSECURE_ENDPOINT", "UNAVAILABLE", probe_id)
        payload = {"event_type": "ALPHAFORGE_LIVE_READINESS_ALERT_PROBE", "probe_id": probe_id, "severity": "INFO", "non_trading_probe": True, "requires_acknowledgement": True}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        token = str(self.config.bearer_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = dict(self._transport(self.config.endpoint_url, json.dumps(payload).encode("utf-8"), headers, float(self.config.timeout_sec)))
        except Exception as exc:
            return self._incomplete(f"DELIVERY_EXCEPTION:{exc.__class__.__name__}", origin, probe_id, attempted=True)
        acknowledged = bool(response.get("acknowledged", False)) and str(response.get("probe_id") or "") == probe_id and str(response.get("status") or "").upper() in {"ACKNOWLEDGED", "DELIVERED"}
        if not acknowledged:
            return self._incomplete("ACKNOWLEDGEMENT_NOT_VERIFIED", origin, probe_id, attempted=True)
        return {"provider_configured": True, "evidence_status": "COMPLETE", "observability_evidence_source": "MEASURED_PROBE", "alert_delivery_verified": True, "delivery_attempted": True, "delivery_acknowledged": True, "non_trading_probe_verified": True, "probe_id": probe_id, "endpoint_origin": origin, "blocking_reasons": []}

    def _origin(self) -> str | None:
        parsed = urlsplit(str(self.config.endpoint_url or "").strip())
        if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password:
            return None
        return f"https://{parsed.netloc}"

    @staticmethod
    def _incomplete(reason: str, origin: str, probe_id: str | None = None, *, attempted: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {"provider_configured": True, "evidence_status": "INCOMPLETE", "observability_evidence_source": "MEASURED_PROBE", "alert_delivery_verified": False, "delivery_attempted": attempted, "delivery_acknowledged": False, "non_trading_probe_verified": True, "endpoint_origin": origin, "blocking_reasons": [reason]}
        if probe_id:
            result["probe_id"] = probe_id
        return result

    @staticmethod
    def _post_json(url: str, payload: bytes, headers: Mapping[str, str], timeout_sec: float) -> Mapping[str, Any]:
        request = Request(url, data=payload, headers=dict(headers), method="POST")
        with urlopen(request, timeout=timeout_sec) as response:
            if not 200 <= int(response.status) < 300:
                return {"status": "HTTP_ERROR", "acknowledged": False}
            parsed = json.loads(response.read().decode("utf-8") or "{}")
        return parsed if isinstance(parsed, Mapping) else {"status": "INVALID_RESPONSE", "acknowledged": False}


def _safe_payload(evidence: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: evidence.get(key) for key in _ALLOWED_FIELDS if key in evidence}
    result["provider_configured"] = bool(result.get("provider_configured", False))
    result["evidence_status"] = str(result.get("evidence_status") or "INCOMPLETE").upper()
    result["observability_evidence_source"] = str(result.get("observability_evidence_source") or "UNVERIFIED").upper()
    for field in ("alert_delivery_verified", "delivery_attempted", "delivery_acknowledged", "non_trading_probe_verified"):
        result[field] = bool(result.get(field, False))
    result["endpoint_origin"] = str(result.get("endpoint_origin") or "UNAVAILABLE")
    reasons = result.get("blocking_reasons") or []
    result["blocking_reasons"] = [str(item)[:100] for item in (reasons if isinstance(reasons, list) else [reasons])]
    if "probe_id" in result:
        result["probe_id"] = str(result["probe_id"])[:120]
    return result


def persist_alert_delivery_evidence(engine: Engine, evidence: Mapping[str, Any]) -> dict[str, Any]:
    safe = _safe_payload(evidence)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS live_alert_delivery_evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL, evidence_status TEXT NOT NULL, alert_delivery_verified INTEGER NOT NULL, evidence_payload TEXT NOT NULL)"))
        conn.execute(text("INSERT INTO live_alert_delivery_evidence(recorded_at, evidence_status, alert_delivery_verified, evidence_payload) VALUES (:ts, :status, :verified, :payload)"), {"ts": canonical_utc_timestamp(), "status": safe["evidence_status"], "verified": 1 if safe["alert_delivery_verified"] else 0, "payload": json.dumps(safe, sort_keys=True)})
    return safe


def latest_persisted_alert_delivery_evidence(engine: Engine) -> dict[str, Any]:
    missing = {"observability_evidence_source": "UNVERIFIED", "observability_evidence_persisted": False, "alert_delivery_verified": False, "alert_delivery_evidence_status": "INCOMPLETE", "alert_delivery_blocking_reasons": ["ALERT_DELIVERY_EVIDENCE_MISSING"]}
    with engine.begin() as conn:
        exists = conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='live_alert_delivery_evidence'")).scalar_one()
        if not int(exists):
            return missing
        row = conn.execute(text("SELECT evidence_status, alert_delivery_verified, evidence_payload FROM live_alert_delivery_evidence ORDER BY id DESC LIMIT 1")).mappings().first()
    if row is None:
        return missing
    try:
        payload = json.loads(str(row["evidence_payload"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {**missing, "observability_evidence_persisted": True, "alert_delivery_blocking_reasons": ["ALERT_DELIVERY_EVIDENCE_INVALID"]}
    valid = isinstance(payload, dict) and str(row["evidence_status"]).upper() == "COMPLETE" and int(row["alert_delivery_verified"]) == 1 and str(payload.get("observability_evidence_source") or "").upper() == "MEASURED_PROBE" and bool(payload.get("alert_delivery_verified", False)) and bool(payload.get("delivery_attempted", False)) and bool(payload.get("delivery_acknowledged", False)) and bool(payload.get("non_trading_probe_verified", False))
    return {"observability_evidence_source": "MEASURED_PROBE" if valid else "UNVERIFIED", "observability_evidence_persisted": True, "alert_delivery_verified": valid, "alert_delivery_evidence_status": "COMPLETE" if valid else "INCOMPLETE", "alert_delivery_blocking_reasons": [] if valid else ["ALERT_DELIVERY_EVIDENCE_INVALID"]}
