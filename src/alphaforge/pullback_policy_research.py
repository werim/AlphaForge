"""Research-only comparison for #549 PULLBACK vs REENTRY_READY eligibility.

This module never changes production MTF eligibility. It evaluates already-frozen
research evidence and fails closed when untouched OOS or required economics are
missing/reused.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from alphaforge.multi_timeframe import REGIME_GUIDED_SETUP_PHASES
from alphaforge.walk_forward import SegmentRole

MANIFEST_SCHEMA = "pullback_reentry_research_v1"
BASELINE_VARIANT = "CURRENT_PRODUCTION_GUIDED_PHASES"
STRICT_VARIANT = "WAIT_PULLBACK_REQUIRE_REENTRY"
BASELINE_PHASES = tuple(REGIME_GUIDED_SETUP_PHASES)
STRICT_PHASES = ("CONTINUATION", "REENTRY_READY")
_REQUIRED_HASH_LEN = 64


class ResearchVerdict(StrEnum):
    PASS_STRICT = "PASS_STRICT"
    RETAIN_BASELINE = "RETAIN_BASELINE"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class PolicyRow:
    row_id: str
    segment_id: str
    role: SegmentRole
    symbol: str
    side: str
    regime: str
    setup_type: str
    setup_phase: str
    baseline_decision: str
    net_r: float
    mfe_r: float
    mae_r: float
    would_have_hit_tp: bool | None = None
    would_have_hit_sl: bool | None = None


@dataclass(frozen=True, slots=True)
class FrozenResearchManifest:
    git_sha: str
    config_hash: str
    data_hash: str
    universe_hash: str
    source_artifact_sha256: str
    search_lineage_id: str
    candidate_variant_ids: tuple[str, ...]
    evaluation_count: int
    influenced_selection: bool
    declared_fresh: bool
    untouched_segment_ids: tuple[str, ...]

    def validate(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if len(self.git_sha) != 40 or any(c not in "0123456789abcdefABCDEF" for c in self.git_sha):
            blockers.append("INVALID_GIT_SHA")
        for name, value in (
            ("config_hash", self.config_hash),
            ("data_hash", self.data_hash),
            ("universe_hash", self.universe_hash),
            ("source_artifact_sha256", self.source_artifact_sha256),
        ):
            if len(value) != _REQUIRED_HASH_LEN or any(c not in "0123456789abcdefABCDEF" for c in value):
                blockers.append(f"INVALID_{name.upper()}")
        if not self.search_lineage_id:
            blockers.append("MISSING_SEARCH_LINEAGE")
        if not self.candidate_variant_ids:
            blockers.append("MISSING_VARIANT_LINEAGE")
        if self.evaluation_count < len(self.candidate_variant_ids):
            blockers.append("INVALID_EVALUATION_COUNT")
        if self.declared_fresh and (
            self.evaluation_count > 1
            or len(self.candidate_variant_ids) > 1
            or self.influenced_selection
        ):
            blockers.append("FALSE_FRESHNESS_CLAIM")
        if not self.untouched_segment_ids:
            blockers.append("MISSING_UNTOUCHED_TEST_SEGMENT")
        return tuple(sorted(set(blockers)))

    @property
    def untouched_admissible(self) -> bool:
        return (
            not self.validate()
            and self.declared_fresh
            and not self.influenced_selection
            and self.evaluation_count == 1
            and len(self.candidate_variant_ids) == 1
        )


def _finite_non_negative(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not math.isfinite(parsed) or parsed < 0.0:
        raise ValueError(f"invalid {field}")
    return parsed


def _finite(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"invalid {field}")
    return parsed


def _bool_or_none(value: Any, field: str) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise ValueError(f"invalid {field}")


def policy_row_from_mapping(row: Mapping[str, Any]) -> PolicyRow:
    required_text = (
        "row_id", "segment_id", "role", "symbol", "side", "regime",
        "setup_type", "setup_phase", "baseline_decision",
    )
    missing = [key for key in required_text if not str(row.get(key) or "").strip()]
    if missing:
        raise ValueError("missing research row fields: " + ",".join(missing))
    phase = str(row["setup_phase"]).upper()
    if phase not in set(BASELINE_PHASES) | {"NO_SETUP", "INVALID", "OVEREXTENDED"}:
        raise ValueError(f"unknown setup_phase: {phase}")
    decision = str(row["baseline_decision"]).upper()
    if decision not in {"ACCEPT", "REJECT"}:
        raise ValueError("baseline_decision must be ACCEPT or REJECT")
    return PolicyRow(
        row_id=str(row["row_id"]),
        segment_id=str(row["segment_id"]),
        role=SegmentRole(str(row["role"])),
        symbol=str(row["symbol"]).upper(),
        side=str(row["side"]).upper(),
        regime=str(row["regime"]),
        setup_type=str(row["setup_type"]),
        setup_phase=phase,
        baseline_decision=decision,
        net_r=_finite(row.get("net_r"), "net_r"),
        mfe_r=_finite_non_negative(row.get("mfe_r"), "mfe_r"),
        mae_r=_finite_non_negative(row.get("mae_r"), "mae_r"),
        would_have_hit_tp=_bool_or_none(row.get("would_have_hit_tp"), "would_have_hit_tp"),
        would_have_hit_sl=_bool_or_none(row.get("would_have_hit_sl"), "would_have_hit_sl"),
    )


def _strict_decision(row: PolicyRow) -> str:
    if row.baseline_decision != "ACCEPT":
        return "REJECT"
    return "ACCEPT" if row.setup_phase in STRICT_PHASES else "REJECT"


def _max_drawdown_r(returns: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _variant_metrics(rows: Sequence[PolicyRow], variant: str) -> dict[str, Any]:
    accepted: list[PolicyRow] = []
    newly_rejected: list[PolicyRow] = []
    for row in rows:
        decision = row.baseline_decision if variant == BASELINE_VARIANT else _strict_decision(row)
        if decision == "ACCEPT":
            accepted.append(row)
        elif variant == STRICT_VARIANT and row.baseline_decision == "ACCEPT":
            newly_rejected.append(row)

    returns = [row.net_r for row in accepted]
    counterfactual_authoritative = [
        row for row in newly_rejected
        if row.would_have_hit_tp is not None and row.would_have_hit_sl is not None
    ]
    saved_losses = sum(
        1 for row in counterfactual_authoritative
        if row.would_have_hit_sl and not row.would_have_hit_tp
    )
    missed_winners = sum(
        1 for row in counterfactual_authoritative
        if row.would_have_hit_tp and not row.would_have_hit_sl
    )
    return {
        "variant": variant,
        "eligible_phases": BASELINE_PHASES if variant == BASELINE_VARIANT else STRICT_PHASES,
        "accepted_count": len(accepted),
        "net_r": round(sum(returns), 10),
        "expectancy_r": (round(sum(returns) / len(returns), 10) if returns else None),
        "max_drawdown_r": round(_max_drawdown_r(returns), 10),
        "tail_loss_r": (round(min(returns), 10) if returns else None),
        "mean_mfe_r": (
            round(sum(row.mfe_r for row in accepted) / len(accepted), 10)
            if accepted else None
        ),
        "mean_mae_r": (
            round(sum(row.mae_r for row in accepted) / len(accepted), 10)
            if accepted else None
        ),
        "newly_rejected_count": len(newly_rejected),
        "authoritative_counterfactual_count": len(counterfactual_authoritative),
        "reject_saved_loss_count": saved_losses,
        "reject_missed_winner_count": missed_winners,
    }


def _group(rows: Sequence[PolicyRow], keys: Sequence[str]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[PolicyRow]] = {}
    for row in rows:
        grouped.setdefault(tuple(str(getattr(row, key)) for key in keys), []).append(row)
    out = []
    for key, members in sorted(grouped.items()):
        entry = dict(zip(keys, key))
        entry["baseline"] = _variant_metrics(members, BASELINE_VARIANT)
        entry["strict"] = _variant_metrics(members, STRICT_VARIANT)
        out.append(entry)
    return out


def evaluate_policy_rows(
    rows: Iterable[PolicyRow],
    manifest: FrozenResearchManifest,
) -> dict[str, Any]:
    materialized = tuple(rows)
    blockers = list(manifest.validate())
    if not materialized:
        blockers.append("NO_RESEARCH_ROWS")

    row_ids = [row.row_id for row in materialized]
    if len(row_ids) != len(set(row_ids)):
        blockers.append("DUPLICATE_ROW_ID")

    untouched = tuple(
        row for row in materialized
        if row.role == SegmentRole.UNTOUCHED_TEST
        and row.segment_id in manifest.untouched_segment_ids
    )
    if not untouched:
        blockers.append("NO_UNTOUCHED_TEST_ROWS")
    if any(
        row.role == SegmentRole.UNTOUCHED_TEST
        and row.segment_id not in manifest.untouched_segment_ids
        for row in materialized
    ):
        blockers.append("UNDECLARED_UNTOUCHED_SEGMENT")

    baseline_untouched = _variant_metrics(untouched, BASELINE_VARIANT)
    strict_untouched = _variant_metrics(untouched, STRICT_VARIANT)

    verdict = ResearchVerdict.INCONCLUSIVE
    if not blockers and manifest.untouched_admissible:
        b_exp = baseline_untouched["expectancy_r"]
        s_exp = strict_untouched["expectancy_r"]
        b_dd = baseline_untouched["max_drawdown_r"]
        s_dd = strict_untouched["max_drawdown_r"]
        if b_exp is None or s_exp is None:
            blockers.append("INSUFFICIENT_UNTOUCHED_ACCEPTED_ROWS")
        elif s_exp > b_exp and s_dd <= b_dd:
            verdict = ResearchVerdict.PASS_STRICT
        else:
            verdict = ResearchVerdict.RETAIN_BASELINE

    return {
        "schema_version": "pullback_reentry_policy_report_v1",
        "research_issue": 549,
        "production_policy_changed": False,
        "live_authorized": False,
        "baseline_variant": {
            "name": BASELINE_VARIANT,
            "eligible_phases": BASELINE_PHASES,
        },
        "strict_variant": {
            "name": STRICT_VARIANT,
            "eligible_phases": STRICT_PHASES,
            "pullback_behavior": "WAIT_EVIDENCE_ONLY",
        },
        "manifest": asdict(manifest),
        "verdict": verdict.value,
        "blockers": tuple(sorted(set(blockers))),
        "overall": {
            "baseline": _variant_metrics(materialized, BASELINE_VARIANT),
            "strict": _variant_metrics(materialized, STRICT_VARIANT),
        },
        "untouched_test": {
            "baseline": baseline_untouched,
            "strict": strict_untouched,
        },
        "by_role_setup_regime_side": _group(
            materialized, ("role", "setup_phase", "regime", "side")
        ),
    }


def manifest_from_mapping(payload: Mapping[str, Any]) -> FrozenResearchManifest:
    if payload.get("schema_version") != MANIFEST_SCHEMA:
        raise ValueError("unsupported #549 research manifest schema")
    raw = payload.get("frozen_research")
    if not isinstance(raw, Mapping):
        raise ValueError("manifest requires frozen_research")
    return FrozenResearchManifest(
        git_sha=str(raw.get("git_sha") or ""),
        config_hash=str(raw.get("config_hash") or ""),
        data_hash=str(raw.get("data_hash") or ""),
        universe_hash=str(raw.get("universe_hash") or ""),
        source_artifact_sha256=str(raw.get("source_artifact_sha256") or ""),
        search_lineage_id=str(raw.get("search_lineage_id") or ""),
        candidate_variant_ids=tuple(map(str, raw.get("candidate_variant_ids") or ())),
        evaluation_count=int(raw.get("evaluation_count") or 0),
        influenced_selection=bool(raw.get("influenced_selection")),
        declared_fresh=bool(raw.get("declared_fresh")),
        untouched_segment_ids=tuple(map(str, raw.get("untouched_segment_ids") or ())),
    )


def report_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    manifest = manifest_from_mapping(payload)
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError("manifest rows must be a list")
    rows = tuple(policy_row_from_mapping(row) for row in raw_rows)
    canonical_rows = json.dumps(raw_rows, sort_keys=True, separators=(",", ":"), default=str)
    actual = hashlib.sha256(canonical_rows.encode("utf-8")).hexdigest()
    if actual != manifest.source_artifact_sha256:
        report = evaluate_policy_rows(rows, manifest)
        report["blockers"] = tuple(sorted(set((*report["blockers"], "SOURCE_ARTIFACT_HASH_MISMATCH"))))
        report["verdict"] = ResearchVerdict.INCONCLUSIVE.value
        return report
    return evaluate_policy_rows(rows, manifest)


def write_report(payload_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(payload_path).read_text())
    if not isinstance(payload, Mapping):
        raise ValueError("research payload must be an object")
    report = report_from_payload(payload)
    Path(output_path).write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return report
