from __future__ import annotations

import json

from alphaforge.alert_delivery import latest_persisted_alert_delivery_evidence
from alphaforge.persistence import init_db
from alphaforge.telegram_alert_delivery import (
    TelegramAlertDeliveryConfig,
    TelegramAlertDeliveryEvidenceProvider,
    capture_telegram_alert_delivery_evidence_from_env,
)


def test_telegram_send_confirmation_is_persisted_without_credentials(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def transport(url: str, payload: bytes, headers, timeout: float):
        observed["url"] = url
        observed["payload"] = json.loads(payload.decode("utf-8"))
        return {"ok": True, "result": {"message_id": 41}}

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-placeholder")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-placeholder")
    engine = init_db("sqlite+pysqlite:///:memory:")
    result = capture_telegram_alert_delivery_evidence_from_env(
        engine,
        transport=transport,
        probe_id_factory=lambda: "probe-telegram-001",
    )
    stored = latest_persisted_alert_delivery_evidence(engine)

    assert result["alert_delivery_verified"] is True
    assert result["endpoint_origin"] == "https://api.telegram.org"
    assert stored["alert_delivery_verified"] is True
    payload_text = json.dumps(result)
    assert "token-placeholder" not in payload_text
    assert "chat-placeholder" not in payload_text
    assert "probe-telegram-001" in str(observed["payload"])


def test_telegram_failed_response_is_fail_closed() -> None:
    provider = TelegramAlertDeliveryEvidenceProvider(
        TelegramAlertDeliveryConfig(bot_token="configured", chat_id="configured"),
        transport=lambda url, payload, headers, timeout: {"ok": False},
        probe_id_factory=lambda: "probe-telegram-002",
    )
    result = provider.snapshot()
    assert result["evidence_status"] == "INCOMPLETE"
    assert result["alert_delivery_verified"] is False
    assert result["blocking_reasons"] == ["TELEGRAM_SEND_NOT_CONFIRMED"]


def test_telegram_missing_configuration_does_not_send() -> None:
    calls: list[bool] = []
    provider = TelegramAlertDeliveryEvidenceProvider(
        TelegramAlertDeliveryConfig(bot_token="", chat_id=""),
        transport=lambda url, payload, headers, timeout: calls.append(True) or {},
        probe_id_factory=lambda: "probe-telegram-003",
    )
    result = provider.snapshot()
    assert calls == []
    assert result["provider_configured"] is False
    assert result["alert_delivery_verified"] is False
