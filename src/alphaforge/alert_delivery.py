from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid


Transport = Callable[[str, bytes, Mapping[str, str], float], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class AlertDeliveryProbeConfig:
    """Configuration for an explicit non-trading alert delivery readiness probe."""

    endpoint_url: str
    bearer_token: str | None = None
    timeout_sec: float = 2.0


class WebhookAlertDeliveryEvidenceProvider:
    """Produces measured LIVE readiness evidence from an acknowledged diagnostic alert.

    The provider is intentionally narrow: it only emits a diagnostic probe payload and
    never submits, cancels, or alters exchange orders. A HTTP success alone is not
    treated as delivery proof. The remote sink must acknowledge the matching probe id.
    """

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
            response = dict(
                self._transport(
                    self.config.endpoint_url,
                    json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                    headers,
                    float(self.config.timeout_sec),
                )
            )
        except Exception as exc:
            return self._incomplete(
                f"DELIVERY_EXCEPTION:{exc.__class__.__name__}",
                endpoint_origin=endpoint_origin,
                probe_id=probe_id,
                attempted=True,
            )

        echoed_probe_id = str(response.get("probe_id") or "")
        status = str(response.get("status") or "").strip().upper()
        acknowledged = bool(response.get("acknowledged", False)) and echoed_probe_id == probe_id and status in {"ACKNOWLEDGED", "DELIVERED"}
        if not acknowledged:
            return self._incomplete(
                "ACKNOWLEDGEMENT_NOT_VERIFIED",
                endpoint_origin=endpoint_origin,
                probe_id=probe_id,
                attempted=True,
            )
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
        if parts.scheme.lower() != "https" or not parts.netloc:
            return None
        return f"https://{parts.netloc}"

    @staticmethod
    def _incomplete(
        reason: str,
        *,
        endpoint_origin: str,
        probe_id: str | None = None,
        attempted: bool = False,
    ) -> dict[str, Any]:
        evidence = {
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
        with urlopen(request, timeout=timeout_sec) as response:  # nosec B310 - URL must be explicit HTTPS.
            status_code = int(getattr(response, "status", response.getcode()))
            if status_code < 200 or status_code >= 300:
                return {"status": f"HTTP_{status_code}", "acknowledged": False}
            body = response.read().decode("utf-8")
        parsed = json.loads(body or "{}")
        return parsed if isinstance(parsed, Mapping) else {"acknowledged": False, "status": "INVALID_RESPONSE"}
