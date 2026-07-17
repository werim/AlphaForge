"""Canonical Phase 8/9 burn-in campaign identity construction.

Campaign and runtime preflight use this module so a PAPER deployment cannot be
rejected merely because the two callers serialized equivalent configuration
differently.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def _canonical_json(value: Any) -> str:
    """Serialize JSON-compatible configuration deterministically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def build_phase8_config_payload(
    *,
    execution_mode: str,
    runtime_limits_active: bool = False,
    runtime_limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the complete, hashable Phase 8/9 runtime configuration payload.

    Limits are intentionally absent while inactive: inactive values cannot
    affect decisions and therefore must not cause config-hash drift.  The
    activation flag is always retained so enabling the gate is mode-aware
    configuration drift even when its limits happen to equal defaults.
    """
    mode = str(execution_mode).upper().strip()
    if not mode:
        raise ValueError("execution_mode is required")
    payload: dict[str, Any] = {
        "execution_mode": mode,
        "RUNTIME_LIMITS_ACTIVE": bool(runtime_limits_active),
    }
    if runtime_limits_active:
        payload["runtime_limits"] = dict(runtime_limits or {})
    return payload


def build_phase8_campaign_identity(
    *,
    release_id: str,
    strategy_config: Mapping[str, Any],
    universe: Sequence[str] | Mapping[str, Any],
    execution_cost_config: Mapping[str, Any],
    execution_mode: str,
    runtime_limits_active: bool = False,
    runtime_limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the canonical identity shared by campaign and runtime preflight."""
    config_payload = build_phase8_config_payload(
        execution_mode=execution_mode,
        runtime_limits_active=runtime_limits_active,
        runtime_limits=runtime_limits,
    )
    return {
        "release_id": str(release_id),
        "strategy_config_hash": _hash(dict(strategy_config)),
        "universe_hash": _hash(universe),
        "execution_cost_config_hash": _hash(dict(execution_cost_config)),
        "execution_mode": config_payload["execution_mode"],
        "config_hash": _hash(config_payload),
        # Persisting the source payload makes a mismatch auditable without
        # attempting to reverse a SHA-256 hash during a burn-in incident.
        "config_payload": config_payload,
    }
