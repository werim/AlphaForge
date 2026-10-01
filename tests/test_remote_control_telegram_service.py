from __future__ import annotations

from datetime import datetime, timezone

from alphaforge.remote_control.commands import RemoteControlResult
from alphaforge.remote_control.telegram_control import SQLiteTelegramControlStore
from alphaforge.remote_control import telegram_service


class FakeHttp:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, *, bot_token: str, chat_id: str, text: str):
        assert bot_token == "token"
        assert chat_id == "9001"
        self.messages.append(text)
        return {"ok": True, "result": {"message_id": len(self.messages)}}


def test_proactive_summary_and_exception_alert_are_restart_deduplicated(tmp_path, monkeypatch) -> None:
    clock_now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    store = SQLiteTelegramControlStore(tmp_path / "control.db", clock=lambda: clock_now)
    http = FakeHttp()

    def query(command, **kwargs):
        del kwargs
        outputs = {
            "HEALTH": "HEALTH blockers=MARKET_DATA_UNAVAILABLE action=INSPECT",
            "REPORT": "REPORT decisions=10 accepted=1 rejected=9",
            "ERRORS": "ERRORS last_error=NONE recent_incidents=none",
        }
        return RemoteControlResult(command.name, 0, outputs[command.name], "")

    monkeypatch.setattr(telegram_service, "execute_telegram_observability", query)
    monkeypatch.setattr(telegram_service.time, "time", lambda: clock_now.timestamp())
    try:
        for _ in range(2):
            telegram_service._maybe_send_proactive(
                http_client=http,
                bot_token="token",
                chat_id="9001",
                config={
                    "remote_control_db_path": "x",
                    "remote_control_campaign_id": "c",
                    "remote_control_run_id": "r",
                },
                control_store=store,
                summary_interval_seconds=43200,
            )
    finally:
        store.close()

    assert len(http.messages) == 2
    assert http.messages[0].startswith("ALPHAFORGE SUMMARY")
    assert http.messages[1].startswith("ALPHAFORGE ALERT")
