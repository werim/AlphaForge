from __future__ import annotations

import argparse
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from alphaforge.config import load_config_from_env
from alphaforge.env_contract import bootstrap_environment
from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.commands import RemoteControlCommand, RemoteControlResult
from alphaforge.remote_control.telegram_adapter import TelegramRemoteControlConfig
from alphaforge.remote_control.telegram_control import SQLiteTelegramControlStore, execute_paper_action
from alphaforge.remote_control.telegram_observability import execute_telegram_observability
from alphaforge.remote_control.telegram_state import SQLiteTelegramTransportStateStore
from alphaforge.remote_control.telegram_transport import (
    UrlLibTelegramHttpClient,
    poll_telegram_once,
    retry_pending_telegram_responses,
)


DEFAULT_SUMMARY_INTERVAL_SECONDS = 12 * 60 * 60
DEFAULT_LOOP_BACKOFF_SECONDS = 2.0


@dataclass(slots=True)
class TelegramServiceExecutor:
    """Carry confirmed-control dependencies through the existing transport API."""
    telegram_control_store: SQLiteTelegramControlStore
    telegram_action_executor: Any = execute_paper_action

    def __call__(self, command: RemoteControlCommand, **kwargs: Any) -> RemoteControlResult:
        config = kwargs.get("config")
        if not isinstance(config, Mapping) or command.name not in {"STATUS", "HEALTH"}:
            return RemoteControlResult(command.name, 1, "", "LEGACY_EXECUTOR_DISABLED")
        return execute_telegram_observability(
            RemoteControlCommand(command.name, (command.name,)),
            config=config,
            timeout=float(kwargs.get("timeout", 5.0)),
            max_output_chars=int(kwargs.get("max_output_chars", 3900)),
        )


def _trusted_remote_config() -> dict[str, str]:
    from alphaforge.dashboard.control_center import ControlCenterService, ControlError

    service = ControlCenterService.from_environment()
    service.validate()
    try:
        active = service.active()
    except ControlError as exc:
        if exc.code == "NO_ACTIVE_CAMPAIGN":
            raise RuntimeError("NO_ACTIVE_CAMPAIGN") from None
        raise
    campaign_id = str(active.get("campaign_id") or "")
    run_id = str(active.get("active_run_id") or "")
    if not campaign_id or not run_id:
        raise RuntimeError("ACTIVE_IDENTITY_UNAVAILABLE")
    return {
        "remote_control_db_path": str(service.db_path),
        "remote_control_campaign_id": campaign_id,
        "remote_control_run_id": run_id,
    }


def _safe_remote_config() -> dict[str, str]:
    try:
        return _trusted_remote_config()
    except Exception:
        raw_db = os.getenv("ALPHAFORGE_DB_PATH", "").strip()
        return {
            "remote_control_db_path": raw_db or "/__alphaforge_database_unavailable__",
            "remote_control_campaign_id": "UNAVAILABLE",
            "remote_control_run_id": "UNAVAILABLE",
        }


def _telegram_settings() -> tuple[str, str]:
    notifications = load_config_from_env().notifications
    token = str(notifications.telegram_bot_token or "").strip()
    chat_id = str(notifications.telegram_chat_id or "").strip()
    if not notifications.telegram_enabled:
        raise RuntimeError("TELEGRAM_CONTROL_DISABLED")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_CONTROL_CONFIGURATION_MISSING")
    if not chat_id.lstrip("-").isdigit():
        raise RuntimeError("TELEGRAM_CHAT_ID_INVALID")
    return token, chat_id


def _send_text(http_client: Any, *, bot_token: str, chat_id: str, text: str) -> bool:
    try:
        result = http_client.send_message(bot_token=bot_token, chat_id=chat_id, text=text[:3900])
        return isinstance(result, Mapping) and result.get("ok") is True
    except Exception:
        return False


def _maybe_send_proactive(
    *,
    http_client: Any,
    bot_token: str,
    chat_id: str,
    config: Mapping[str, str],
    control_store: SQLiteTelegramControlStore,
    summary_interval_seconds: float,
) -> None:
    try:
        health = execute_telegram_observability(
            RemoteControlCommand("HEALTH", ("HEALTH",)),
            config=config,
        )
        report = execute_telegram_observability(
            RemoteControlCommand("REPORT", ("REPORT",)),
            config=config,
        )
        errors = execute_telegram_observability(
            RemoteControlCommand("ERRORS", ("ERRORS",)),
            config=config,
        )
    except Exception:
        return

    now = time.time()
    _, last_summary_at = control_store.notification_state("scheduled_summary")
    if last_summary_at is None or now - last_summary_at.timestamp() >= summary_interval_seconds:
        summary = f"ALPHAFORGE SUMMARY | {health.stdout} | {report.stdout}"
        if _send_text(http_client, bot_token=bot_token, chat_id=chat_id, text=summary):
            control_store.record_notification("scheduled_summary", summary)

    action = "CONTINUE"
    for part in health.stdout.split():
        if part.startswith("action="):
            action = part.split("=", 1)[1]
            break
    alert = f"{health.stdout} | {errors.stdout}"
    digest = hashlib.sha256(alert.encode("utf-8")).hexdigest()
    previous_digest, _ = control_store.notification_state("exception_alert")
    if action != "CONTINUE" and digest != previous_digest:
        if _send_text(http_client, bot_token=bot_token, chat_id=chat_id, text=f"ALPHAFORGE ALERT | {alert}"):
            control_store.record_notification("exception_alert", alert)


def run_service(
    *,
    state_dir: Path,
    summary_interval_seconds: float = DEFAULT_SUMMARY_INTERVAL_SECONDS,
    poll_timeout: int = 10,
    http_client: Any | None = None,
    sleep: Any = time.sleep,
    iterations: int | None = None,
) -> int:
    bot_token, chat_id = _telegram_settings()
    telegram_config = TelegramRemoteControlConfig.from_allowlists(allowed_chat_ids=(chat_id,))
    state_dir = state_dir.expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    http = http_client or UrlLibTelegramHttpClient()
    replay = SQLiteReplayStore(state_dir / "replay.sqlite3")
    transport = SQLiteTelegramTransportStateStore(state_dir / "transport.sqlite3")
    control = SQLiteTelegramControlStore(state_dir / "control.sqlite3")
    executor = TelegramServiceExecutor(control)
    offset: int | None = None
    count = 0
    try:
        retry_pending_telegram_responses(bot_token=bot_token, http_client=http, state_store=transport)
        while iterations is None or count < iterations:
            config = _safe_remote_config()
            result = poll_telegram_once(
                bot_token=bot_token,
                offset=offset,
                telegram_config=telegram_config,
                remote_config=config,
                replay_store=replay,
                executor=executor,
                http_client=http,
                poll_timeout=poll_timeout,
                state_store=transport,
            )
            offset = result.next_offset
            _maybe_send_proactive(
                http_client=http,
                bot_token=bot_token,
                chat_id=chat_id,
                config=config,
                control_store=control,
                summary_interval_seconds=max(60.0, float(summary_interval_seconds)),
            )
            count += 1
            if result.transport_error:
                sleep(DEFAULT_LOOP_BACKOFF_SECONDS)
        return 0
    finally:
        control.close()
        transport.close()
        replay.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m alphaforge.remote_control.telegram_service",
        description="Run the isolated Telegram Control Center service.",
    )
    parser.add_argument(
        "--state-dir",
        default=str(Path.home() / ".alphaforge" / "telegram-control"),
        help="Isolated Telegram control-state directory; never use the campaign DB path.",
    )
    parser.add_argument(
        "--summary-interval-seconds",
        type=float,
        default=DEFAULT_SUMMARY_INTERVAL_SECONDS,
    )
    parser.add_argument("--poll-timeout", type=int, default=10)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    bootstrap_environment()
    args = build_parser().parse_args(argv)
    return run_service(
        state_dir=Path(args.state_dir),
        summary_interval_seconds=args.summary_interval_seconds,
        poll_timeout=args.poll_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
