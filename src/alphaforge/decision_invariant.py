from __future__ import annotations

from dataclasses import dataclass
import json
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
    remaining_execution_penalty: float | None
    effective_rr: float | None
    rr_basis: str
    execution_cost_semantics: str
    threshold_provenance: tuple[tuple[str, str], ...]
    execution_evidence_status: str
    portfolio_decision: str
    original_notional: float | None
    risk_scale: float | None
    effective_notional: float | None
    stop_distance_basis: str
    geometry_status: str
    geometry_source: str
    lifecycle_pre_submit_terminal_state: str


@dataclass(frozen=True)
class InvariantMismatch:
    field: str
    expected: Any
    observed: Any


@dataclass(frozen=True)
class SemanticInvariantViolation:
    code: str
    detail: str


def _number(value: Any) -> float | None:
    if value in (None, "", "UNKNOWN", "UNAVAILABLE"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _identity(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, Mapping):
        return json.dumps(dict(value), sort_keys=True, default=str)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return json.dumps(list(value), sort_keys=True, default=str)
    return str(value).strip()


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
        remaining_execution_penalty=_number(payload.get("remaining_execution_penalty")),
        effective_rr=_number(payload.get("effective_rr")),
        rr_basis=str(payload.get("rr_basis") or "").strip().upper(),
        execution_cost_semantics=_identity(payload.get("execution_cost_semantics")),
        threshold_provenance=_threshold_provenance(payload),
        execution_evidence_status=str(evidence_status or "UNKNOWN").strip().upper(),
        portfolio_decision=_decision(portfolio_value),
        original_notional=_number(payload.get("original_notional")),
        risk_scale=_number(payload.get("risk_scale")),
        effective_notional=_number(payload.get("effective_notional")),
        stop_distance_basis=str(payload.get("stop_distance_basis") or "").strip().upper(),
        geometry_status=str(payload.get("geometry_status") or "").strip().upper(),
        geometry_source=str(payload.get("geometry_source") or "").strip().upper(),
        lifecycle_pre_submit_terminal_state=str(lifecycle or "").strip().upper(),
    )



def _gate_evidence(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = payload.get("failed_gate_evidence")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        execution = payload.get("execution_safety")
        execution = execution if isinstance(execution, Mapping) else {}
        raw = execution.get("failed_gate_evidence")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def validate_pre_submit_semantics(
    payload: Mapping[str, Any],
    *,
    numeric_abs_tol: float = 1e-6,
) -> tuple[SemanticInvariantViolation, ...]:
    """Validate relationships inside one canonical decision payload.

    This is deliberately relationship-only: it consumes recorded values and
    provenance and never reimplements strategy thresholds.
    """

    violations: list[SemanticInvariantViolation] = []
    projected = project_pre_submit_invariant(payload)
    geometry_status = str(payload.get("geometry_status") or "").strip().upper()

    if geometry_status in {"UNAVAILABLE", "GUIDED_CANDIDATE_UNAVAILABLE"}:
        authoritative_geometry = {
            "entry": payload.get("entry"),
            "sl": payload.get("sl"),
            "stop": payload.get("stop"),
            "tp": payload.get("tp"),
            "target": payload.get("target"),
            "candidate_rr": payload.get("candidate_rr"),
            "executable_raw_rr": payload.get("executable_raw_rr"),
            "effective_rr": payload.get("effective_rr"),
        }
        leaked = tuple(k for k, v in authoritative_geometry.items() if _number(v) is not None)
        if leaked:
            violations.append(SemanticInvariantViolation(
                "UNAVAILABLE_GEOMETRY_HAS_AUTHORITATIVE_VALUES",
                f"geometry_status={geometry_status} numeric_fields={leaked!r}",
            ))

    executable_raw_rr = _number(payload.get("executable_raw_rr"))
    remaining_penalty = _number(payload.get("remaining_execution_penalty"))
    effective_rr = _number(payload.get("effective_rr"))
    if (
        executable_raw_rr is not None
        and remaining_penalty is not None
        and effective_rr is not None
    ):
        expected_effective_rr = max(0.0, executable_raw_rr - remaining_penalty)
        if not isclose(
            effective_rr, expected_effective_rr, rel_tol=0.0, abs_tol=numeric_abs_tol
        ):
            violations.append(SemanticInvariantViolation(
                "EFFECTIVE_RR_ARITHMETIC_MISMATCH",
                "effective_rr="
                f"{effective_rr!r} executable_raw_rr={executable_raw_rr!r} "
                f"remaining_execution_penalty={remaining_penalty!r} "
                f"expected={expected_effective_rr!r}",
            ))

    if projected.risk_scale is not None and not (0.0 <= projected.risk_scale <= 1.0):
        violations.append(SemanticInvariantViolation(
            "RISK_SCALE_OUT_OF_RANGE",
            f"risk_scale={projected.risk_scale!r}",
        ))
    if (
        projected.original_notional is not None
        and projected.risk_scale is not None
        and projected.effective_notional is not None
    ):
        expected_notional = projected.original_notional * projected.risk_scale
        if not isclose(
            projected.effective_notional,
            expected_notional,
            rel_tol=0.0,
            abs_tol=numeric_abs_tol,
        ):
            violations.append(SemanticInvariantViolation(
                "EFFECTIVE_NOTIONAL_SCALING_MISMATCH",
                f"original_notional={projected.original_notional!r} "
                f"risk_scale={projected.risk_scale!r} "
                f"effective_notional={projected.effective_notional!r} "
                f"expected={expected_notional!r}",
            ))

    failed_gates = set(projected.failed_gates)
    gate_evidence = _gate_evidence(payload)
    evidence_gates = {
        str(item.get("gate") or item.get("name") or "").strip().upper()
        for item in gate_evidence
        if str(item.get("gate") or item.get("name") or "").strip()
    }
    if gate_evidence and evidence_gates != failed_gates:
        violations.append(SemanticInvariantViolation(
            "FAILED_GATE_SET_MISMATCH",
            f"all_failed_gates={sorted(failed_gates)!r} "
            f"failed_gate_evidence={sorted(evidence_gates)!r}",
        ))

    observed_authority = {
        "LOW_EFFECTIVE_RR": effective_rr,
        "RR_TOO_LOW": projected.candidate_rr,
        "LOW_SCORE": projected.score,
    }
    for item in gate_evidence:
        gate = str(item.get("gate") or item.get("name") or "").strip().upper()
        if gate not in observed_authority:
            continue
        observed = _number(item.get("observed"))
        authority = observed_authority[gate]
        if observed is not None and authority is not None and not isclose(
            observed, authority, rel_tol=0.0, abs_tol=numeric_abs_tol
        ):
            violations.append(SemanticInvariantViolation(
                "GATE_OBSERVED_VALUE_MISMATCH",
                f"gate={gate} gate_observed={observed!r} authoritative={authority!r}",
            ))

    if projected.decision == "ACCEPT" and failed_gates:
        violations.append(SemanticInvariantViolation(
            "ACCEPT_WITH_FAILED_GATES",
            f"failed_gates={sorted(failed_gates)!r}",
        ))
    if projected.decision == "REJECT":
        if not projected.primary_reject_reason:
            violations.append(SemanticInvariantViolation(
                "REJECT_WITHOUT_PRIMARY_REASON", "primary_reject_reason is empty"
            ))
        if not failed_gates:
            violations.append(SemanticInvariantViolation(
                "REJECT_WITHOUT_FAILED_GATE", "all_failed_gates is empty"
            ))
        elif (
            projected.primary_reject_reason
            and projected.primary_reject_reason not in failed_gates
        ):
            violations.append(SemanticInvariantViolation(
                "PRIMARY_REJECT_REASON_NOT_FAILED",
                f"primary={projected.primary_reject_reason!r} "
                f"failed_gates={sorted(failed_gates)!r}",
            ))

    provenance = payload.get("source_provenance")
    provenance = provenance if isinstance(provenance, Mapping) else payload
    basis = str(provenance.get("reject_execution_basis") or "").strip().upper()
    attributable = provenance.get("reject_quality_attributable")
    if basis == "EXPECTED_FILL_RUNTIME_PARITY":
        executable_entry = _number(provenance.get("executable_entry"))
        hypothetical_entry = _number(
            provenance.get("hypothetical_entry", payload.get("hypothetical_entry"))
        )
        if (
            executable_entry is not None
            and hypothetical_entry is not None
            and not isclose(
                executable_entry,
                hypothetical_entry,
                rel_tol=0.0,
                abs_tol=numeric_abs_tol,
            )
        ):
            violations.append(SemanticInvariantViolation(
                "EXPECTED_FILL_ENTRY_PARITY_MISMATCH",
                f"executable_entry={executable_entry!r} "
                f"hypothetical_entry={hypothetical_entry!r}",
            ))
        embedded = provenance.get("entry_slippage_embedded_in_fill") is True
        costs = payload.get("execution_cost_assumptions")
        costs = costs if isinstance(costs, Mapping) else {}
        entry_slippage_cost = _number(costs.get("entry_slippage_cost"))
        if embedded and entry_slippage_cost not in (None, 0.0):
            violations.append(SemanticInvariantViolation(
                "ENTRY_SLIPPAGE_DOUBLE_COUNT",
                f"entry_slippage_embedded_in_fill=True "
                f"entry_slippage_cost={entry_slippage_cost!r}",
            ))
        required = (
            executable_entry,
            _number(provenance.get("executable_raw_rr")),
            _number(provenance.get("remaining_execution_penalty")),
            _number(provenance.get("effective_rr_at_decision")),
        )
        if attributable is True and any(value is None for value in required):
            violations.append(SemanticInvariantViolation(
                "ATTRIBUTABLE_REJECT_PARITY_EVIDENCE_INCOMPLETE",
                "EXPECTED_FILL_RUNTIME_PARITY marked attributable with missing "
                "executable_entry/RR/penalty/effective-RR evidence",
            ))

    return tuple(violations)


def assert_pre_submit_semantics(
    payload: Mapping[str, Any],
    *,
    numeric_abs_tol: float = 1e-6,
) -> PreSubmitInvariant:
    violations = validate_pre_submit_semantics(
        payload, numeric_abs_tol=numeric_abs_tol
    )
    if violations:
        detail = "; ".join(f"{item.code}: {item.detail}" for item in violations)
        raise ValueError(f"DECISION_SEMANTIC_INVARIANT_VIOLATION: {detail}")
    return project_pre_submit_invariant(payload)


def compare_pre_submit_invariants(
    expected: PreSubmitInvariant,
    observed: PreSubmitInvariant,
    *,
    numeric_abs_tol: float = 1e-9,
) -> tuple[InvariantMismatch, ...]:
    """Return every semantic mismatch; callers must fail closed on non-empty."""

    mismatches: list[InvariantMismatch] = []
    numeric_fields = {
        "score", "candidate_rr", "executable_raw_rr",
        "remaining_execution_penalty", "effective_rr",
        "original_notional", "risk_scale", "effective_notional",
    }
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
    for field in (
        "score", "candidate_rr", "executable_raw_rr",
        "remaining_execution_penalty", "effective_rr",
        "original_notional", "risk_scale", "effective_notional",
    ):
        if getattr(value, field) is None:
            missing.append(field)
    for field in (
        "rr_basis", "execution_cost_semantics",
        "stop_distance_basis", "geometry_status", "geometry_source",
    ):
        if not getattr(value, field):
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
    numeric_abs_tol: float = 1e-9,
) -> PreSubmitInvariant:
    """Fail closed on incomplete, contradictory, or cross-surface drift."""

    assert_pre_submit_semantics(reference, numeric_abs_tol=numeric_abs_tol)
    expected = project_pre_submit_invariant(reference)
    expected_missing = _incomplete_fields(expected)
    if expected_missing:
        raise ValueError(
            "DECISION_PARITY_EVIDENCE_INCOMPLETE: " + ",".join(expected_missing)
        )
    for payload in surfaces:
        assert_pre_submit_semantics(payload, numeric_abs_tol=numeric_abs_tol)
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
