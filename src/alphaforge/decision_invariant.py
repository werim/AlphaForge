from __future__ import annotations

from dataclasses import dataclass
from math import isclose
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class PreSubmitInvariant:
    """Authoritative semantic projection used to detect cross-surface drift.

    The projection deliberately excludes side effects (exchange submission,
    simulated fills, persistence IDs) and compares only the protected decision
    semantics that BACKTEST, PAPER and LIVE_PRECHECK must agree on for the same
    frozen event and configuration.
    """

    decision: str
    primary_reject_reason: str
    failed_gates: tuple[str, ...]
    score: float | None
    candidate_rr: float | None
    executable_raw_rr: float | None
    effective_rr: float | None
    threshold_provenance: tuple[tuple[str, str], ...]
    execution_evidence_status: str
    portfolio_decision: str
    lifecycle_pre_submit_terminal_state: str


@dataclass(frozen=True)
class InvariantMismatch:
    field: str
    expected: Any
    observed: Any


def _number(value: Any) -> float | None:
    if value in (None, "", "UNKNOWN", "UNAVAILABLE"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decision(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"ACCEPT", "ACCEPTED", "EXECUTED", "ALLOW", "ALLOWED"}:
        return "ACCEPT"
    if text in {"REJECT", "REJECTED", "BLOCK", "BLOCKED"}:
        return "REJECT"
    return text or "UNKNOWN"


def _failed_gates(payload: Mapping[str, Any]) -> tuple[str, ...]:
    execution = payload.get("execution_safety")
    execution = execution if isinstance(execution, Mapping) else {}
    values: list[str] = []
    for source in (
        payload.get("all_failed_gates"),
        payload.get("failed_gates"),
        execution.get("all_failed_gates"),
        payload.get("risk_flags"),
    ):
        if isinstance(source, str):
            values.extend(x.strip() for x in source.split("|") if x.strip())
        elif isinstance(source, Sequence):
            values.extend(str(x).strip() for x in source if str(x).strip())
    return tuple(sorted(set(values)))


def _threshold_provenance(payload: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    raw = payload.get("threshold_provenance")
    if not isinstance(raw, Mapping):
        raw = {}
    # Values are stringified intentionally: provenance is identity evidence,
    # not a second threshold implementation.
    return tuple(sorted((str(k), str(v)) for k, v in raw.items()))


def project_pre_submit_invariant(payload: Mapping[str, Any]) -> PreSubmitInvariant:
    """Project a surface-specific result into the protected P1-B contract."""

    execution = payload.get("execution_safety")
    execution = execution if isinstance(execution, Mapping) else {}
    portfolio = payload.get("portfolio_decision")
    if isinstance(portfolio, Mapping):
        portfolio_value = portfolio.get("decision")
        if portfolio_value is None:
            portfolio_value = "ACCEPT" if portfolio.get("accepted") is True else (
                "REJECT" if portfolio.get("accepted") is False else "UNKNOWN"
            )
    else:
        portfolio_value = portfolio

    primary = (
        payload.get("primary_reject_reason")
        or payload.get("reject_reason")
        or payload.get("reason")
        or ""
    )
    lifecycle = (
        payload.get("lifecycle_pre_submit_terminal_state")
        or payload.get("pre_submit_terminal_state")
        or payload.get("lifecycle_state")
        or ""
    )
    evidence_status = (
        payload.get("execution_evidence_status")
        or execution.get("execution_evidence_status")
        or payload.get("evidence_status")
        or ""
    )
    return PreSubmitInvariant(
        decision=_decision(payload.get("decision") or payload.get("status")),
        primary_reject_reason=str(primary or "").strip().upper(),
        failed_gates=_failed_gates(payload),
        score=_number(payload.get("score")),
        candidate_rr=_number(payload.get("candidate_rr", payload.get("rr"))),
        executable_raw_rr=_number(payload.get("executable_raw_rr")),
        effective_rr=_number(payload.get("effective_rr")),
        threshold_provenance=_threshold_provenance(payload),
        execution_evidence_status=str(evidence_status or "UNKNOWN").strip().upper(),
        portfolio_decision=_decision(portfolio_value),
        lifecycle_pre_submit_terminal_state=str(lifecycle or "").strip().upper(),
    )


def compare_pre_submit_invariants(
    expected: PreSubmitInvariant,
    observed: PreSubmitInvariant,
    *,
    numeric_abs_tol: float = 1e-9,
) -> tuple[InvariantMismatch, ...]:
    """Return every semantic mismatch; callers must fail closed on non-empty."""

    mismatches: list[InvariantMismatch] = []
    numeric_fields = {"score", "candidate_rr", "executable_raw_rr", "effective_rr"}
    for field in PreSubmitInvariant.__dataclass_fields__:
        left = getattr(expected, field)
        right = getattr(observed, field)
        if field in numeric_fields and left is not None and right is not None:
            equal = isclose(float(left), float(right), rel_tol=0.0, abs_tol=numeric_abs_tol)
        else:
            equal = left == right
        if not equal:
            mismatches.append(InvariantMismatch(field, left, right))
    return tuple(mismatches)


def _incomplete_fields(value: PreSubmitInvariant) -> tuple[str, ...]:
    missing: list[str] = []
    for field in ("score", "candidate_rr", "executable_raw_rr", "effective_rr"):
        if getattr(value, field) is None:
            missing.append(field)
    if not value.threshold_provenance:
        missing.append("threshold_provenance")
    if value.execution_evidence_status in {"", "UNKNOWN", "UNAVAILABLE", "INCOMPLETE"}:
        missing.append("execution_evidence_status")
    if value.portfolio_decision == "UNKNOWN":
        missing.append("portfolio_decision")
    if not value.lifecycle_pre_submit_terminal_state:
        missing.append("lifecycle_pre_submit_terminal_state")
    if value.decision == "UNKNOWN":
        missing.append("decision")
    return tuple(missing)


def assert_pre_submit_invariant_parity(
    reference: Mapping[str, Any],
    *surfaces: Mapping[str, Any],
) -> PreSubmitInvariant:
    """Fail closed on incomplete evidence or any protected semantic drift."""

    expected = project_pre_submit_invariant(reference)
    expected_missing = _incomplete_fields(expected)
    if expected_missing:
        raise ValueError(
            "DECISION_PARITY_EVIDENCE_INCOMPLETE: " + ",".join(expected_missing)
        )
    for payload in surfaces:
        observed = project_pre_submit_invariant(payload)
        observed_missing = _incomplete_fields(observed)
        if observed_missing:
            raise ValueError(
                "DECISION_PARITY_EVIDENCE_INCOMPLETE: " + ",".join(observed_missing)
            )
        mismatches = compare_pre_submit_invariants(expected, observed)
        if mismatches:
            detail = "; ".join(
                f"{item.field}: expected={item.expected!r} observed={item.observed!r}"
                for item in mismatches
            )
            raise ValueError(f"DECISION_PARITY_MISMATCH: {detail}")
    return expected
