"""Fail-closed Phase 9 burn-in preflight checks."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alphaforge.burnin_campaign import build_phase8_campaign_identity

_CRITICAL_IDENTITY_FIELDS = (
    "release_id", "strategy_config_hash", "universe_hash",
    "execution_cost_config_hash", "execution_mode", "config_hash",
)


def _candidate_identity(**identity_args: Any) -> dict[str, Any]:
    return build_phase8_campaign_identity(**identity_args)


def _actual_runtime_identity(**identity_args: Any) -> dict[str, Any]:
    # Deliberately use the same builder as campaign creation; do not duplicate
    # configuration selection or hashing logic here.
    return build_phase8_campaign_identity(**identity_args)


def runtime_identity_matches_campaign_identity(
    candidate_identity: Mapping[str, Any], runtime_identity: Mapping[str, Any],
) -> bool:
    """Require exact parity for every preflight-critical identity component."""
    return all(candidate_identity.get(key) == runtime_identity.get(key) for key in _CRITICAL_IDENTITY_FIELDS)


def identity_differences(
    candidate_identity: Mapping[str, Any], runtime_identity: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return auditable differing values, including unhashed config payloads."""
    keys = (*_CRITICAL_IDENTITY_FIELDS, "config_payload")
    return {
        key: {"candidate": candidate_identity.get(key), "runtime": runtime_identity.get(key)}
        for key in keys
        if candidate_identity.get(key) != runtime_identity.get(key)
    }


def preflight(
    candidate_identity: Mapping[str, Any],
    runtime_identity: Mapping[str, Any],
    *,
    critical_checks_pass: bool = True,
) -> dict[str, Any]:
    """Return PASS only when checks and identity parity both hold.

    Any missing or drifting critical component is FAIL_CLOSED; this function is
    intentionally not a compatibility or migration escape hatch.
    """
    differences = identity_differences(candidate_identity, runtime_identity)
    passed = bool(critical_checks_pass) and runtime_identity_matches_campaign_identity(candidate_identity, runtime_identity)
    return {
        "status": "PASS" if passed else "FAIL_CLOSED",
        "passed": passed,
        "identity_differences": differences,
    }
