from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

PARITY_FIELDS: tuple[str, ...] = (
    "decision",
    "reason",
    "order_type",
    "confidence",
    "score",
    "effective_rr",
)


@dataclass(frozen=True)
class ParityComparison:
    sample_id: str
    paper: dict[str, Any]
    live_precheck: dict[str, Any]
    missing_fields: tuple[str, ...]
    mismatch_fields: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.missing_fields and not self.mismatch_fields

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "paper": self.paper,
            "live_precheck": self.live_precheck,
            "missing_fields": list(self.missing_fields),
            "mismatch_fields": list(self.mismatch_fields),
            "passed": self.passed,
        }


def compare_paper_live_precheck(
    sample_id: str,
    paper_eval: Mapping[str, Any],
    live_precheck_eval: Mapping[str, Any],
    *,
    fields: Sequence[str] = PARITY_FIELDS,
) -> ParityComparison:
    missing = tuple(
        field
        for field in fields
        if field not in paper_eval or field not in live_precheck_eval
    )
    mismatch = tuple(
        field
        for field in fields
        if field in paper_eval
        and field in live_precheck_eval
        and paper_eval[field] != live_precheck_eval[field]
    )
    return ParityComparison(
        sample_id=str(sample_id),
        paper={field: paper_eval.get(field) for field in fields},
        live_precheck={field: live_precheck_eval.get(field) for field in fields},
        missing_fields=missing,
        mismatch_fields=mismatch,
    )


def summarize_parity(comparisons: Sequence[ParityComparison], *, min_sample_count: int = 3) -> dict[str, Any]:
    mismatch_count = sum(len(row.mismatch_fields) for row in comparisons)
    missing_field_count = sum(len(row.missing_fields) for row in comparisons)
    enough_samples = len(comparisons) >= int(min_sample_count)
    complete = enough_samples and mismatch_count == 0 and missing_field_count == 0
    return {
        "evidence_status": "COMPLETE" if complete else "INCOMPLETE",
        "sample_count": len(comparisons),
        "min_sample_count": int(min_sample_count),
        "mismatch_count": mismatch_count,
        "missing_field_count": missing_field_count,
        "no_order_submission_verified": True,
        "comparison_fields": list(PARITY_FIELDS),
        "samples": [row.as_dict() for row in comparisons],
    }
