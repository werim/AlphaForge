"""Deterministic, fail-closed verification planning for AlphaForge.

The router selects the minimum *permitted* deterministic verification tier and
an advisory reasoning alias. Deterministic test results remain authoritative:
this module cannot authorize LIVE, relax a safety gate, or convert FAIL to PASS.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any, Iterable, Mapping, Sequence

POLICY_VERSION = "system-verification.v1"
ALL_TIERS = ("T0", "T1", "T2", "T3", "T4")


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ModelAlias(StrEnum):
    NO_LLM = "NO_LLM"
    CHEAP_TRIAGE = "CHEAP_TRIAGE"
    MEDIUM_REASONING = "MEDIUM_REASONING"
    HIGH_REASONING = "HIGH_REASONING"
    MAX_REASONING = "MAX_REASONING"


_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}
_TIERS_BY_RISK = {
    RiskLevel.LOW: ("T0",),
    RiskLevel.MEDIUM: ("T0", "T1", "T2"),
    RiskLevel.HIGH: ("T0", "T1", "T2", "T3"),
    RiskLevel.CRITICAL: ALL_TIERS,
}
_MODEL_BY_RISK = {
    RiskLevel.LOW: ModelAlias.NO_LLM,
    RiskLevel.MEDIUM: ModelAlias.MEDIUM_REASONING,
    RiskLevel.HIGH: ModelAlias.HIGH_REASONING,
    RiskLevel.CRITICAL: ModelAlias.HIGH_REASONING,
}

_TESTS_BY_INVARIANT: dict[str, tuple[str, ...]] = {
    "BUILD_DEPENDENCY_INTEGRITY": ("tests/test_ci_branch_contract.py",),
    "CONFIG_AUTHORITY": (
        "tests/test_issue491_paper_candidate_notional_ssot.py",
        "tests/test_issue500_correlation_risk_config_ssot.py",
        "tests/test_issue501_runtime_default_authority.py",
    ),
    "DECISION_PARITY": (
        "tests/test_issue490_protected_mode_parity.py",
        "tests/test_issue421_decision_invariant.py",
    ),
    "DECISION_SEMANTICS": ("tests/test_multi_timeframe.py",),
    "EXECUTION_SAFETY": (
        "tests/test_issue421_execution_safety.py",
        "tests/test_execution_cost_semantics.py",
    ),
    "FAIL_CLOSED_PROMOTION": ("tests/test_live_readiness.py",),
    "KILL_SWITCH_AUTHORIZATION": ("tests/test_runtime_live_authorization.py",),
    "LIVE_ORDER_BOUNDARY": (
        "tests/test_runtime_live_authorization.py",
        "tests/test_trading_modes.py",
        "tests/test_phase6_release_gates.py",
    ),
    "MARKET_DATA_INTEGRITY": (
        "tests/test_exchange_market_scanner.py",
        "tests/test_issue438_market_data_supervision.py",
    ),
    "NO_LOOKAHEAD": ("tests/test_multi_timeframe.py",),
    "PERSISTENCE_ATOMICITY": (
        "tests/test_issue550_atomic_reject_persistence.py",
        "tests/test_issue450_sqlite_concurrency.py",
    ),
    "PORTFOLIO_RISK": (
        "tests/test_issue496_portfolio_risk_authority.py",
        "tests/test_portfolio_risk_engine.py",
    ),
    "READINESS_EVIDENCE": ("tests/test_live_readiness.py",),
    "RECONCILIATION_INTEGRITY": (
        "tests/test_reconciliation_sqlite_contention.py",
        "tests/test_runtime_live_authorization.py",
    ),
    "RESOLVER_INTEGRITY": (
        "tests/test_issue421_position_window_integrity.py",
        "tests/test_issue421_full_chain_replay.py",
    ),
    "RESTART_RECOVERY": ("tests/test_issue421_full_chain_replay.py",),
    "RUNTIME_AUTHORITY": ("tests/test_runtime.py",),
    "TIME_INTEGRITY": ("tests/test_issue421_market_time.py",),
    "UNKNOWN_PRODUCTION_CHANGE": (
        "tests/test_issue421_decision_invariant.py",
        "tests/test_issue421_full_chain_replay.py",
        "tests/test_issue498_cross_component_mutations.py",
    ),
    "VERIFICATION_GATE_INTEGRITY": (
        "tests/test_ci_branch_contract.py",
        "tests/test_issue498_cross_component_mutations.py",
    ),
}


@dataclass(frozen=True, slots=True)
class PathAssessment:
    path: str
    risk_level: RiskLevel
    affected_invariants: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    base_sha: str
    head_sha: str
    policy_version: str
    changed_files: tuple[str, ...]
    risk_level: RiskLevel
    affected_invariants: tuple[str, ...]
    required_tiers: tuple[str, ...]
    recommended_model: ModelAlias
    llm_required_during_execution: bool
    escalate_on_failure: bool
    selected_tests: tuple[str, ...]
    skipped_tiers: tuple[str, ...]
    reasons: tuple[str, ...]
    plan_id: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["risk_level"] = self.risk_level.value
        payload["recommended_model"] = self.recommended_model.value
        return payload


@dataclass(frozen=True, slots=True)
class FailureRoute:
    deterministic_result: str
    recommended_model: ModelAlias
    reason: str
    llm_authoritative: bool = False
    may_relax_safety: bool = False
    may_authorize_live: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["recommended_model"] = self.recommended_model.value
        return payload


def _normalize_path(raw_path: str) -> str:
    candidate = str(raw_path).replace("\\", "/").strip()
    if not candidate:
        raise ValueError("changed file path must be non-empty")
    path = PurePosixPath(candidate)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"changed file path is outside repository: {raw_path!r}")
    normalized = path.as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _assessment(
    path: str,
    risk: RiskLevel,
    invariants: Sequence[str],
    reason: str,
) -> PathAssessment:
    return PathAssessment(path, risk, tuple(sorted(set(invariants))), reason)


def assess_path(raw_path: str) -> PathAssessment:
    path = _normalize_path(raw_path)
    name = PurePosixPath(path).name
    lower = path.lower()

    if lower.startswith("docs/") or name in {"README.md", "CHANGELOG.md", "VERSION.md"}:
        return _assessment(path, RiskLevel.LOW, ("DOCUMENTATION_ONLY",), "documentation-only change")

    if path == ".github/workflows/test.yml" or path == "scripts/run_safety_mutations.py":
        return _assessment(
            path,
            RiskLevel.HIGH,
            ("VERIFICATION_GATE_INTEGRITY",),
            "protected verification authority changed",
        )
    if lower.startswith("tests/") and (
        "issue421" in lower
        or "issue498" in lower
        or "safety" in lower
        or "mutation" in lower
        or "live_readiness" in lower
        or "runtime_live_authorization" in lower
    ):
        return _assessment(
            path,
            RiskLevel.HIGH,
            ("VERIFICATION_GATE_INTEGRITY",),
            "protected safety regression changed",
        )
    if lower.startswith("tests/"):
        return _assessment(path, RiskLevel.LOW, ("TEST_COVERAGE",), "isolated test change")

    if name in {"pyproject.toml", "requirements.txt"}:
        return _assessment(
            path,
            RiskLevel.HIGH,
            ("BUILD_DEPENDENCY_INTEGRITY",),
            "runtime dependency or build contract changed",
        )
    if lower.startswith(("alembic/", "migrations/")):
        return _assessment(
            path,
            RiskLevel.HIGH,
            ("PERSISTENCE_ATOMICITY", "RESTART_RECOVERY"),
            "persistent schema contract changed",
        )

    if lower.startswith("src/alphaforge/"):
        if name in {
            "order.py",
            "release_gates.py",
            "release_operator_ack.py",
            "release_safety_evidence.py",
        } or "live_authorization" in lower or "kill_switch" in lower:
            return _assessment(
                path,
                RiskLevel.CRITICAL,
                ("EXECUTION_SAFETY", "KILL_SWITCH_AUTHORIZATION", "LIVE_ORDER_BOUNDARY"),
                "LIVE/order authorization boundary changed",
            )

        high_rules: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
            (("execution.py",), ("EXECUTION_SAFETY", "DECISION_PARITY"), "execution economics/safety changed"),
            (("runtime.py",), ("RUNTIME_AUTHORITY", "RESTART_RECOVERY"), "authoritative runtime changed"),
            (("decision_invariant.py",), ("DECISION_PARITY",), "cross-mode decision invariant changed"),
            (("portfolio_risk",), ("PORTFOLIO_RISK",), "portfolio-risk authority changed"),
            (("burnin_resolver.py",), ("RESOLVER_INTEGRITY", "TIME_INTEGRITY"), "resolver authority changed"),
            (("persistence.py",), ("PERSISTENCE_ATOMICITY", "RESTART_RECOVERY"), "authoritative persistence changed"),
            (("runtime_state.py",), ("RECONCILIATION_INTEGRITY", "RESTART_RECOVERY"), "runtime/reconciliation state changed"),
            (("live_readiness.py",), ("READINESS_EVIDENCE", "FAIL_CLOSED_PROMOTION"), "readiness authority changed"),
            (("burnin_qualification.py",), ("READINESS_EVIDENCE", "FAIL_CLOSED_PROMOTION"), "qualification authority changed"),
            (("config_registry.py", "config.py"), ("CONFIG_AUTHORITY",), "canonical configuration authority changed"),
            (("reconciliation",), ("RECONCILIATION_INTEGRITY", "RESTART_RECOVERY"), "reconciliation authority changed"),
        )
        for needles, invariants, reason in high_rules:
            if any(needle in lower for needle in needles):
                return _assessment(path, RiskLevel.HIGH, invariants, reason)

        medium_rules: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
            (("exchange_market_scanner.py",), ("MARKET_DATA_INTEGRITY",), "market scanner changed"),
            (("multi_timeframe.py",), ("DECISION_SEMANTICS", "NO_LOOKAHEAD"), "MTF decision logic changed"),
            (("ai_brain.py", "scoring_context.py"), ("DECISION_SEMANTICS",), "score/decision logic changed"),
            (("report", "dashboard"), ("OBSERVABILITY",), "reporting/observability changed"),
        )
        for needles, invariants, reason in medium_rules:
            if any(needle in lower for needle in needles):
                return _assessment(path, RiskLevel.MEDIUM, invariants, reason)

        return _assessment(
            path,
            RiskLevel.HIGH,
            ("UNKNOWN_PRODUCTION_CHANGE",),
            "unmapped production source escalated fail-closed",
        )

    if lower.endswith(".py"):
        return _assessment(
            path,
            RiskLevel.MEDIUM,
            ("APPLICATION_LOGIC",),
            "non-production Python logic changed",
        )
    return _assessment(path, RiskLevel.LOW, ("REPOSITORY_METADATA",), "non-executable repository metadata changed")


def _highest_risk(assessments: Sequence[PathAssessment]) -> RiskLevel:
    if not assessments:
        return RiskLevel.LOW
    return max((item.risk_level for item in assessments), key=_RISK_ORDER.__getitem__)


def _selected_tests(
    assessments: Sequence[PathAssessment],
    changed_files: Sequence[str],
) -> tuple[str, ...]:
    tests: set[str] = {
        path for path in changed_files if path.startswith("tests/") and path.endswith(".py")
    }
    for item in assessments:
        for invariant in item.affected_invariants:
            tests.update(_TESTS_BY_INVARIANT.get(invariant, ()))
    return tuple(sorted(tests))


def _plan_identity(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_changed_files(
    changed_files: Iterable[str],
    *,
    base_sha: str,
    head_sha: str,
    policy_version: str = POLICY_VERSION,
) -> VerificationPlan:
    normalized = tuple(sorted({_normalize_path(path) for path in changed_files}))
    assessments = tuple(assess_path(path) for path in normalized)
    risk = _highest_risk(assessments)
    invariants = tuple(sorted({
        invariant
        for item in assessments
        for invariant in item.affected_invariants
    }))
    required_tiers = _TIERS_BY_RISK[risk]
    skipped_tiers = tuple(tier for tier in ALL_TIERS if tier not in required_tiers)
    selected_tests = _selected_tests(assessments, normalized)
    reasons = tuple(sorted({f"{item.path}: {item.reason}" for item in assessments}))
    identity_payload = {
        "base_sha": str(base_sha),
        "head_sha": str(head_sha),
        "policy_version": policy_version,
        "changed_files": normalized,
        "risk_level": risk.value,
        "affected_invariants": invariants,
        "required_tiers": required_tiers,
        "recommended_model": _MODEL_BY_RISK[risk].value,
        "selected_tests": selected_tests,
    }
    return VerificationPlan(
        base_sha=str(base_sha),
        head_sha=str(head_sha),
        policy_version=policy_version,
        changed_files=normalized,
        risk_level=risk,
        affected_invariants=invariants,
        required_tiers=required_tiers,
        recommended_model=_MODEL_BY_RISK[risk],
        llm_required_during_execution=False,
        escalate_on_failure=risk != RiskLevel.LOW,
        selected_tests=selected_tests,
        skipped_tiers=skipped_tiers,
        reasons=reasons,
        plan_id=_plan_identity(identity_payload),
    )


def _validate_git_ref(value: str) -> str:
    ref = str(value).strip()
    if not ref or ref.startswith("-") or any(character.isspace() for character in ref):
        raise ValueError(f"invalid git ref: {value!r}")
    return ref


def changed_files_between(
    base: str,
    head: str,
    *,
    repo_root: str | Path | None = None,
) -> tuple[str, ...]:
    base_ref = _validate_git_ref(base)
    head_ref = _validate_git_ref(head)
    cwd = Path(repo_root) if repo_root is not None else Path.cwd()
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base_ref}...{head_ref}"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(
        sorted({_normalize_path(line) for line in result.stdout.splitlines() if line.strip()})
    )


def route_failure(failure: Mapping[str, Any]) -> FailureRoute:
    result = str(failure.get("deterministic_result") or "UNKNOWN").upper()
    failure_class = str(failure.get("failure_class") or "").upper()
    risk_raw = str(failure.get("risk_level") or RiskLevel.HIGH.value).upper()
    try:
        risk = RiskLevel(risk_raw)
    except ValueError:
        risk = RiskLevel.HIGH
    invariants = tuple(str(value) for value in (failure.get("affected_invariants") or ()))
    nondeterministic = bool(failure.get("nondeterministic")) or any(
        token in failure_class
        for token in (
            "RACE",
            "CRASH",
            "SIGKILL",
            "NONDETERMINISTIC",
            "ARCHITECTURAL_CONTRADICTION",
        )
    )
    cross_boundary = bool(failure.get("cross_boundary")) or len(set(invariants)) > 1

    if result == "PASS":
        return FailureRoute(
            result,
            ModelAlias.NO_LLM,
            "deterministic evidence passed; no reasoning model required",
        )
    if failure_class in {"COLLECTION", "CONFIG", "INFRA", "INFRASTRUCTURE"}:
        return FailureRoute(
            result,
            ModelAlias.CHEAP_TRIAGE,
            "collection/config/infrastructure triage",
        )
    if nondeterministic:
        return FailureRoute(
            result,
            ModelAlias.MAX_REASONING,
            "unresolved race/crash/nondeterministic failure is escalation-eligible",
        )
    if cross_boundary or risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
        return FailureRoute(
            result,
            ModelAlias.HIGH_REASONING,
            "cross-boundary or protected semantic failure",
        )
    return FailureRoute(
        result,
        ModelAlias.MEDIUM_REASONING,
        "local deterministic semantic failure",
    )


def _bounded_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(value, sort_keys=True, default=str)
    if len(rendered) <= limit:
        return rendered
    return rendered[: max(0, limit - 16)] + "...<truncated>"


def build_failure_packet(
    failure: Mapping[str, Any],
    *,
    traceback_limit: int = 4000,
    evidence_limit: int = 2000,
) -> dict[str, Any]:
    route = route_failure(failure)
    return {
        "schema_version": "system-verification-failure.v1",
        "base_sha": failure.get("base_sha"),
        "head_sha": failure.get("head_sha"),
        "config_hash": failure.get("config_hash"),
        "failed_invariant": failure.get("failed_invariant"),
        "failing_tests": tuple(sorted(map(str, failure.get("failing_tests") or ()))),
        "expected": _bounded_text(failure.get("expected"), evidence_limit),
        "actual": _bounded_text(failure.get("actual"), evidence_limit),
        "changed_files": tuple(sorted(map(str, failure.get("changed_files") or ()))),
        "traceback": _bounded_text(failure.get("traceback"), traceback_limit),
        "persisted_evidence": _bounded_text(
            failure.get("persisted_evidence"), evidence_limit
        ),
        "routing": route.to_dict(),
    }


def build_verification_manifest(
    plan: VerificationPlan,
    *,
    deterministic_result: str,
    tests_executed: int,
    invariants_passed: int,
    invariants_total: int,
    config_hash: str | None = None,
) -> dict[str, Any]:
    result = str(deterministic_result).upper()
    if result not in {"PASS", "FAIL", "ERROR", "BLOCKED"}:
        raise ValueError("deterministic_result must be PASS/FAIL/ERROR/BLOCKED")
    if tests_executed < 0 or invariants_passed < 0 or invariants_total < 0:
        raise ValueError("verification counts cannot be negative")
    if invariants_passed > invariants_total:
        raise ValueError("invariants_passed cannot exceed invariants_total")
    return {
        "schema_version": "system-verification-manifest.v1",
        "plan_id": plan.plan_id,
        "base_sha": plan.base_sha,
        "head_sha": plan.head_sha,
        "policy_version": plan.policy_version,
        "risk_level": plan.risk_level.value,
        "required_tiers": plan.required_tiers,
        "affected_invariants": plan.affected_invariants,
        "recommended_model": plan.recommended_model.value,
        "llm_required_during_execution": False,
        "deterministic_result": result,
        "tests_executed": tests_executed,
        "invariants_passed": invariants_passed,
        "invariants_total": invariants_total,
        "config_hash": config_hash,
        "llm_authoritative": False,
        "may_relax_safety": False,
        "may_authorize_live": False,
    }


def _write_or_print(payload: Mapping[str, Any], output: str | None) -> None:
    rendered = json.dumps(payload, sort_keys=True, indent=2, default=str)
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n")
    print(rendered)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic AlphaForge verification router"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--base", required=True)
    plan.add_argument("--head", required=True)
    plan.add_argument("--changed-file", action="append", default=[])
    plan.add_argument("--repo-root")
    plan.add_argument("--output")

    route = subparsers.add_parser("route-failure")
    route.add_argument("input_json")
    route.add_argument("--output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        changed = tuple(args.changed_file) or changed_files_between(
            args.base, args.head, repo_root=args.repo_root
        )
        plan = plan_changed_files(changed, base_sha=args.base, head_sha=args.head)
        _write_or_print(plan.to_dict(), args.output)
        return 0
    failure = json.loads(Path(args.input_json).read_text())
    packet = build_failure_packet(failure)
    _write_or_print(packet, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
