"""Safe, multi-error configuration provenance audit for operators."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from alphaforge.config import normalize_binance_market_type
from alphaforge.config_registry import CONFIG_REGISTRY, effective_config_subset
from alphaforge.env_contract import resolve_binance_environment

SECRET_NAMES = {setting.env_name for setting in CONFIG_REGISTRY if setting.secret}


def _safe_error(setting: str, exc: Exception, raw: object = None) -> dict[str, Any]:
    message = str(exc)
    reason = "invalid_setting_value"
    if "alias conflict" in message:
        reason = "conflicting_env_alias"
    elif "below minimum" in message:
        reason = "setting_below_minimum"
    elif "above maximum" in message:
        reason = "setting_above_maximum"
    elif setting == "BINANCE_DEFAULT_MARKET_TYPE":
        reason = "unsupported_market_type"
    elif setting == "ALPHAFORGE_RECONCILIATION_POSITION_EPSILON":
        reason = "invalid_decimal"
    row: dict[str, Any] = {"type": type(exc).__name__, "reason": reason, "setting": setting}
    registry = next((item for item in CONFIG_REGISTRY if item.env_name == setting), None)
    if registry and (registry.min_value is not None or registry.max_value is not None):
        row["allowed_range"] = {"minimum": registry.min_value, "maximum": registry.max_value}
    if setting == "ALPHAFORGE_MAX_DAILY_LOSS_PCT":
        row["expected_unit"] = "fraction"
        row["migration"] = "use 0.02 for two percent; 2.0 is intentionally invalid"
    if setting not in SECRET_NAMES and raw is not None:
        cleaned = str(raw).split("#", 1)[0].strip().strip("'\"")
        row["provided_value"] = cleaned if re.fullmatch(r"[-+]?[0-9]+(?:\.[0-9]+)?", cleaned) else "REDACTED"
    return row


def audit_settings(*, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    if env is None:
        env = os.environ
    errors: list[dict[str, Any]] = []
    settings: dict[str, dict[str, Any]] = {}
    for setting in CONFIG_REGISTRY:
        try:
            result = effective_config_subset((setting.env_name,), env=env, fail_on_alias_conflict=True)[setting.env_name]
            value = result["value"]
            if setting.env_name == "BINANCE_DEFAULT_MARKET_TYPE":
                value = normalize_binance_market_type(value)
            if setting.env_name == "ALPHAFORGE_RECONCILIATION_POSITION_EPSILON":
                from decimal import Decimal, InvalidOperation
                try:
                    parsed = Decimal(str(value))
                    if not parsed.is_finite() or parsed < 0:
                        raise InvalidOperation
                except InvalidOperation:
                    raise ValueError("invalid reconciliation epsilon") from None
            row = {"source": str(result["source"]).upper()}
            if setting.secret:
                row["is_set"] = bool(value)
            else:
                row["value"] = value
                if setting.env_name == "ALPHAFORGE_MAX_DAILY_LOSS_PCT":
                    row["unit"] = "fraction"
            settings[setting.env_name] = row
        except (TypeError, ValueError) as exc:
            raw = env.get(setting.env_name)
            errors.append(_safe_error(setting.env_name, exc, raw))
    try:
        endpoint_values = effective_config_subset(("BINANCE_ENVIRONMENT", "BINANCE_BASE_URL", "BINANCE_WS_URL"), env=env)
        endpoint_env = {name: str(endpoint_values[name]["value"]) for name in endpoint_values if endpoint_values[name]["value"]}
        if str(endpoint_values["BINANCE_ENVIRONMENT"]["source"]).startswith("alias (BINANCE_TESTNET)"):
            endpoint_env["BINANCE_TESTNET"] = endpoint_env.pop("BINANCE_ENVIRONMENT")
        resolve_binance_environment(endpoint_env)
    except ValueError:
        errors.append({"type": "ValueError", "reason": "environment_resolution_failed", "setting": "BINANCE_ENVIRONMENT"})
    return {"status": "FAIL" if errors else "PASS", "errors": errors, "settings": settings,
            "precedence": ["DEFAULT", "DOTENV", "DOTENV_LOCAL", "DASHBOARD", "PROCESS_ENV"]}


def main() -> int:
    result = audit_settings()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
