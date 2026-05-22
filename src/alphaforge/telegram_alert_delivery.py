from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen
import uuid

from sqlalchemy.engine import Engine

from alphaforge.alert_delivery import capture_alert_delivery_evidence


TelegramTransport = Callable[[str, bytes, Mapping[str, str], float], Mapping[str, Any]]
_TELEGRAM_API_ORIGIN = "https://api.telegram.org"


@dataclass(frozen=True, slots=True)
class TelegramAlertDeliveryConfig:
    bot_token: str
    chat_id: str
    enabled: bool = False
    timeout_sec: float = 2.0


class TelegramAlertDeliveryEvidenceProvider:
    """Send one non-trading Telegram probe and convert API acceptance to evidence."""

    def __init__(
        self,
        config: TelegramAlertDeliveryConfig,
        *,
        transport: TelegramTransport | None = None,
        probe_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or self._post_json
        self._probe_id_factory = probe_id_factory or (lambda: f"telegram-alert-probe-{uuid.uuid4().hex}")

    def snapshot(self) -> dict[str, Any]:
        token = str(self.config.bot_token or "").strip()
        chat_id = str(self.config.chat_id or "").strip()
        probe_id = str(self._probe_id_factory()).strip()
        if not self.config.enabled:
            return self._incomplete("TELEGRAM_DELIVERY_DISABLED", probe_id=probe_id or None, configured=False)
        if not token or not chat_id:
            return self._incomplete("TELEGRAM_CONFIGURATION_MISSING", probe_id=probe_id or None, configured=False)
        if not probe_id:
            return self._incomplete("EMPTY_PROBE_ID")

        message = (
            "AlphaForge LIVE readiness alert-delivery probe received.\n\n"
            f"Probe ID: {probe_id}\n"
            "Type: Non-trading diagnostic check\n"
            "Action: No order was submitted."
        )
        payload = {"chat_id": chat_id, "text": message}
        url = f"{_TELEGRAM_API_ORIGIN}/bot{token}/sendMessage"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            response = dict(self._transport(url, json.dumps(payload).encode("utf-8"), headers, float(self.config.timeout_sec)))
        except Exception as exc:
            return self._incomplete(f"TELEGRAM_DELIVERY_EXCEPTION:{exc.__class__.__name__}", probe_id=probe_id, attempted=True)

        result = response.get("result")
        message_id = result.get("message_id") if isinstance(result, Mapping) else None
        accepted = response.get("ok") is True and isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0
        if not accepted:
            return self._incomplete("TELEGRAM_SEND_NOT_CONFIRMED", probe_id=probe_id, attempted=True)
        return {
            "provider_configured": True,
            "evidence_status": "COMPLETE",
            "observability_evidence_source": "MEASURED_PROBE",
            "alert_delivery_verified": True,
            "delivery_attempted": True,
            "delivery_acknowledged": True,
            "non_trading_probe_verified": True,
            "probe_id": probe_id,
            "endpoint_origin": _TELEGRAM_API_ORIGIN,
            "blocking_reasons": [],
        }

    @staticmethod
    def _incomplete(
        reason: str,
        *,
        probe_id: str | None = None,
        configured: bool = True,
        attempted: bool = False,
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "provider_configured": configured,
            "evidence_status": "INCOMPLETE",
            "observability_evidence_source": "MEASURED_PROBE",
            "alert_delivery_verified": False,
            "delivery_attempted": attempted,
            "delivery_acknowledged": False,
            "non_trading_probe_verified": True,
            "endpoint_origin": _TELEGRAM_API_ORIGIN,
            "blocking_reasons": [reason],
        }
        if probe_id:
            evidence["probe_id"] = probe_id
        return evidence

    @staticmethod
    def _post_json(url: str, payload: bytes, headers: Mapping[str, str], timeout_sec: float) -> Mapping[str, Any]:
        request = Request(url, data=payload, headers=dict(headers), method="POST")
        with urlopen(request, timeout=timeout_sec) as response:
            if not 200 <= int(response.status) < 300:
                return {"ok": False}
            parsed = json.loads(response.read().decode("utf-8") or "{}")
        return parsed if isinstance(parsed, Mapping) else {"ok": False}


def telegram_alert_provider_from_env(
    *,
    transport: TelegramTransport | None = None,
    probe_id_factory: Callable[[], str] | None = None,
) -> TelegramAlertDeliveryEvidenceProvider:
    return TelegramAlertDeliveryEvidenceProvider(
        TelegramAlertDeliveryConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            enabled=os.getenv("ALPHAFORGE_ENABLE_TELEGRAM", "").strip().lower() == "true",
        ),
        transport=transport,
        probe_id_factory=probe_id_factory,
    )


def capture_telegram_alert_delivery_evidence_from_env(
    engine: Engine,
    *,
    transport: TelegramTransport | None = None,
    probe_id_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    provider = telegram_alert_provider_from_env(transport=transport, probe_id_factory=probe_id_factory)
    return capture_alert_delivery_evidence(engine, provider)
