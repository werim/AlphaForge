"""Fail-closed historical walk-forward validation evidence.

This module defines an evidence contract.  It deliberately does not tune a
strategy, launch PAPER, or provide a second implementation of execution
economics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
import math
from typing import Any, Callable, Mapping, Sequence, TypeVar

from .burnin import canonical_hash, config_hash, universe_hash
from .decision_invariant import validate_pre_submit_semantics
from .execution import (
    EXECUTION_COST_PERCENTAGE_DENOMINATOR,
    EXECUTION_COST_REFERENCE_PRICE,
    EXECUTION_COST_SIGN_CONVENTION,
)


class SegmentRole(StrEnum):
    CALIBRATION = "TRAIN_CALIBRATION"
    OOS_VALIDATION = "VALIDATION_OOS"
    UNTOUCHED_TEST = "UNTOUCHED_TEST"


class UniverseMode(StrEnum):
    POINT_IN_TIME = "TIMESTAMP_CORRECT_MEMBERSHIP"
    FIXED = "FIXED_UNIVERSE"


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"invalid UTC timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Membership:
    symbol: str
    start: str
    end: str | None = None

    def contains(self, timestamp: str) -> bool:
        point = _timestamp(timestamp)
        return _timestamp(self.start) <= point and (
            self.end is None or point < _timestamp(self.end)
        )


@dataclass(frozen=True, slots=True)
class UniverseProvenance:
    mode: UniverseMode
    identity: str
    source_identity: str
    symbols: tuple[str, ...]
    memberships: tuple[Membership, ...]
    survivorship_bias_protected: bool

    def __post_init__(self) -> None:
        if not self.identity or not self.source_identity or not self.symbols:
            raise ValueError("universe provenance requires identity and symbols")
        if self.mode == UniverseMode.FIXED and (
            self.memberships or self.survivorship_bias_protected
        ):
            raise ValueError("fixed universe cannot claim survivorship-bias protection")
        if self.mode == UniverseMode.POINT_IN_TIME and (
            not self.memberships or not self.survivorship_bias_protected
        ):
            raise ValueError("point-in-time universe requires timestamped memberships")

    @classmethod
    def fixed(cls, symbols: Sequence[str], *, source_identity: str) -> "UniverseProvenance":
        normalized = tuple(sorted({str(symbol) for symbol in symbols}))
        if not normalized or not source_identity:
            raise ValueError("fixed universe requires symbols and source identity")
        return cls(
            mode=UniverseMode.FIXED,
            identity=canonical_hash(
                {"mode": UniverseMode.FIXED, "source": source_identity, "symbols": normalized}
            ),
            source_identity=source_identity,
            symbols=normalized,
            memberships=(),
            survivorship_bias_protected=False,
        )

    @classmethod
    def point_in_time(
        cls, memberships: Sequence[Membership], *, source_identity: str
    ) -> "UniverseProvenance":
        normalized = tuple(sorted(memberships, key=lambda item: (item.symbol, item.start, item.end or "")))
        if not normalized or not source_identity:
            raise ValueError("point-in-time universe requires memberships and source identity")
        for membership in normalized:
            _timestamp(membership.start)
            if membership.end is not None and _timestamp(membership.end) <= _timestamp(membership.start):
                raise ValueError("universe membership end must follow start")
        symbols = tuple(sorted({item.symbol for item in normalized}))
        return cls(
            mode=UniverseMode.POINT_IN_TIME,
            identity=canonical_hash(
                {"mode": UniverseMode.POINT_IN_TIME, "source": source_identity, "memberships": [asdict(item) for item in normalized]}
            ),
            source_identity=source_identity,
            symbols=symbols,
            memberships=normalized,
            survivorship_bias_protected=True,
        )

    def admits(self, symbol: str, timestamp: str) -> bool:
        if self.mode == UniverseMode.FIXED:
            return symbol in self.symbols
        return any(item.symbol == symbol and item.contains(timestamp) for item in self.memberships)


@dataclass(frozen=True, slots=True)
class Segment:
    segment_id: str
    window_id: str
    role: SegmentRole
    start: str
    end: str
    git_sha: str
    strategy_config_hash: str
    config_hash: str
    data_hash: str
    universe_identity: str
    frozen_at: str
    calibration_segment_ids: tuple[str, ...]

    @classmethod
    def freeze(
        cls,
        *,
        segment_id: str,
        window_id: str,
        role: SegmentRole,
        start: str,
        end: str,
        git_sha: str,
        strategy_config: Mapping[str, Any],
        config: Mapping[str, Any],
        data_identity: Mapping[str, Any],
        universe: UniverseProvenance,
        frozen_at: str,
        calibration_segment_ids: Sequence[str] = (),
    ) -> "Segment":
        if not all((segment_id, window_id, git_sha, data_identity, universe.identity)):
            raise ValueError("segment provenance is incomplete")
        if len(git_sha) != 40 or any(character not in "0123456789abcdefABCDEF" for character in git_sha):
            raise ValueError("segment git SHA must be a full immutable commit identity")
        if _timestamp(end) <= _timestamp(start):
            raise ValueError("segment end must follow start")
        if _timestamp(frozen_at) > _timestamp(start):
            raise ValueError("configuration must be frozen before the segment starts")
        return cls(
            segment_id=segment_id,
            window_id=window_id,
            role=role,
            start=start,
            end=end,
            git_sha=git_sha,
            strategy_config_hash=config_hash(strategy_config),
            config_hash=config_hash(config),
            data_hash=canonical_hash(dict(data_identity)),
            universe_identity=universe.identity,
            frozen_at=frozen_at,
            calibration_segment_ids=tuple(sorted(map(str, calibration_segment_ids))),
        )

    @property
    def identity(self) -> str:
        return canonical_hash(asdict(self))

    def contains(self, timestamp: str) -> bool:
        return _timestamp(self.start) <= _timestamp(timestamp) < _timestamp(self.end)


@dataclass(frozen=True, slots=True)
class HistoricalRow:
    row_id: str
    timestamp: str
    symbol: str
    net_return: float
    candidate_rr: float
    executable_rr: float
    remaining_execution_penalty: float
    effective_rr: float
    rr_basis: str
    execution_cost_semantics: Mapping[str, Any]
    regime: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class WalkForwardContract:
    segments: tuple[Segment, ...]
    universe: UniverseProvenance

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("walk-forward validation requires segments")
        for segment in self.segments:
            immutable_hashes = (
                segment.strategy_config_hash,
                segment.config_hash,
                segment.data_hash,
                segment.universe_identity,
            )
            if (
                len(segment.git_sha) != 40
                or any(character not in "0123456789abcdefABCDEF" for character in segment.git_sha)
                or any(len(value) != 64 for value in immutable_hashes)
                or _timestamp(segment.frozen_at) > _timestamp(segment.start)
            ):
                raise ValueError("segment immutable provenance is invalid")
        ordered = sorted(self.segments, key=lambda segment: _timestamp(segment.start))
        if list(self.segments) != ordered:
            raise ValueError("segments must be chronological")
        if any(segment.universe_identity != self.universe.identity for segment in self.segments):
            raise ValueError("segment universe provenance does not match the contract")
        if len({segment.segment_id for segment in self.segments}) != len(self.segments):
            raise ValueError("segment ids must be unique")
        for previous, current in zip(self.segments, self.segments[1:]):
            if _timestamp(current.start) < _timestamp(previous.end):
                raise ValueError("segments must not overlap")
        if not any(segment.role == SegmentRole.CALIBRATION for segment in self.segments):
            raise ValueError("calibration evidence is required")
        if not any(segment.role == SegmentRole.OOS_VALIDATION for segment in self.segments):
            raise ValueError("OOS validation evidence is required")
        by_id = {segment.segment_id: segment for segment in self.segments}
        for segment in self.segments:
            if segment.role == SegmentRole.CALIBRATION:
                if segment.calibration_segment_ids:
                    raise ValueError("calibration segments cannot have fitted-data sources")
                continue
            if not segment.calibration_segment_ids:
                raise ValueError("OOS/test segments require explicit calibration sources")
            for source_id in segment.calibration_segment_ids:
                source = by_id.get(source_id)
                if source is None or source.role != SegmentRole.CALIBRATION:
                    raise ValueError("validation/test data cannot be a calibration source")
                if _timestamp(source.end) > _timestamp(segment.start):
                    raise ValueError("future rows cannot influence an earlier OOS segment")
        windows = {segment.window_id for segment in self.segments}
        for window_id in windows:
            window_segments = [segment for segment in self.segments if segment.window_id == window_id]
            calibration = [segment for segment in window_segments if segment.role == SegmentRole.CALIBRATION]
            evaluation = [segment for segment in window_segments if segment.role != SegmentRole.CALIBRATION]
            if evaluation and not calibration:
                raise ValueError("each OOS/test window requires prior calibration")
            if calibration and evaluation and max(map(lambda item: _timestamp(item.end), calibration)) > min(
                map(lambda item: _timestamp(item.start), evaluation)
            ):
                raise ValueError("future rows cannot influence an earlier OOS segment")

    @property
    def identity(self) -> str:
        return canonical_hash(
            {"segments": [asdict(segment) for segment in self.segments], "universe": asdict(self.universe)}
        )

    def partition(self, rows: Sequence[HistoricalRow]) -> dict[str, tuple[HistoricalRow, ...]]:
        partitioned: dict[str, list[HistoricalRow]] = {segment.segment_id: [] for segment in self.segments}
        seen: set[str] = set()
        for row in rows:
            if not row.row_id or row.row_id in seen:
                raise ValueError("historical row identity must be present and unique")
            seen.add(row.row_id)
            matches = [segment for segment in self.segments if segment.contains(row.timestamp)]
            if len(matches) != 1:
                raise ValueError(f"row {row.row_id} does not belong to exactly one segment")
            if not self.universe.admits(row.symbol, row.timestamp):
                raise ValueError(f"row {row.row_id} is outside the declared universe")
            partitioned[matches[0].segment_id].append(row)
        return {key: tuple(value) for key, value in partitioned.items()}

    def calibrate(
        self,
        rows: Sequence[HistoricalRow],
        *,
        target_segment_id: str,
        calibrator: Callable[[tuple[HistoricalRow, ...]], "T"],
    ) -> "T":
        partitioned = self.partition(rows)
        target = next(
            (segment for segment in self.segments if segment.segment_id == target_segment_id),
            None,
        )
        if target is None or target.role == SegmentRole.CALIBRATION:
            raise ValueError("calibration target must be an OOS/test segment")
        calibration_rows = tuple(
            row
            for segment in self.segments
            if segment.segment_id in target.calibration_segment_ids
            for row in partitioned[segment.segment_id]
        )
        return calibrator(calibration_rows)


T = TypeVar("T")


def build_validation_report(
    contract: WalkForwardContract,
    rows: Sequence[HistoricalRow],
    *,
    min_effective_rr: float,
) -> dict[str, Any]:
    """Build role-separated evidence from canonical pre-submit economics."""

    partitioned = contract.partition(rows)
    for segment in contract.segments:
        if not partitioned[segment.segment_id]:
            raise ValueError(f"segment {segment.segment_id} has no evidence rows")
    details: list[dict[str, Any]] = []
    for segment in contract.segments:
        for row in partitioned[segment.segment_id]:
            rr_values = (
                row.candidate_rr,
                row.executable_rr,
                row.remaining_execution_penalty,
                row.effective_rr,
            )
            if any(not math.isfinite(float(value)) or float(value) < 0 for value in rr_values):
                raise ValueError(f"row {row.row_id} has invalid RR evidence")
            semantic_payload = {
                "candidate_rr": row.candidate_rr,
                "executable_raw_rr": row.executable_rr,
                "remaining_execution_penalty": row.remaining_execution_penalty,
                "effective_rr": row.effective_rr,
                "rr_basis": row.rr_basis,
                "execution_cost_semantics": row.execution_cost_semantics,
            }
            violations = validate_pre_submit_semantics(semantic_payload)
            if violations:
                codes = ",".join(violation.code for violation in violations)
                raise ValueError(f"row {row.row_id} violates canonical decision semantics: {codes}")
            if not row.rr_basis or not row.execution_cost_semantics:
                raise ValueError(f"row {row.row_id} lacks canonical execution provenance")
            canonical_cost_contract = {
                "reference_price": EXECUTION_COST_REFERENCE_PRICE,
                "percentage_denominator": EXECUTION_COST_PERCENTAGE_DENOMINATOR,
                "sign_convention": EXECUTION_COST_SIGN_CONVENTION,
                "fee_treatment": "SEPARATE_NOT_INCLUDED",
            }
            if any(
                row.execution_cost_semantics.get(key) != expected
                for key, expected in canonical_cost_contract.items()
            ):
                raise ValueError(f"row {row.row_id} does not use canonical execution-cost semantics")
            if row.effective_rr < min_effective_rr:
                # Preserve the measured value and its gate result; never coerce it
                # upward merely because the report is historical validation.
                effective_rr_gate = "FAIL"
            else:
                effective_rr_gate = "PASS"
            details.append(
                {
                    "row_id": row.row_id,
                    "segment_id": segment.segment_id,
                    "window_id": segment.window_id,
                    "role": segment.role,
                    "symbol": row.symbol,
                    "regime": row.regime,
                    "net_return": float(row.net_return),
                    "candidate_rr": float(row.candidate_rr),
                    "executable_rr": float(row.executable_rr),
                    "remaining_execution_penalty": float(row.remaining_execution_penalty),
                    "effective_rr": float(row.effective_rr),
                    "effective_rr_gate": effective_rr_gate,
                    "rr_basis": row.rr_basis,
                    "execution_cost_semantics": dict(row.execution_cost_semantics),
                }
            )

    def grouped(keys: tuple[str, ...]) -> list[dict[str, Any]]:
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for detail in details:
            groups.setdefault(tuple(detail[key] for key in keys), []).append(detail)
        return [
            {
                **dict(zip(keys, group_key)),
                "row_count": len(group_rows),
                "net_return": round(sum(row["net_return"] for row in group_rows), 10),
            }
            for group_key, group_rows in sorted(groups.items(), key=lambda item: tuple(map(str, item[0])))
        ]

    segment_summary = grouped(("role", "window_id", "segment_id"))
    negative_oos = [
        item
        for item in segment_summary
        if item["role"] in {SegmentRole.OOS_VALIDATION, SegmentRole.UNTOUCHED_TEST}
        and item["net_return"] < 0
    ]
    return {
        "schema_version": "walk_forward_validation_v1",
        "contract_identity": contract.identity,
        "segments": [asdict(segment) for segment in contract.segments],
        "universe": {
            "mode": contract.universe.mode,
            "identity": contract.universe.identity,
            "source_identity": contract.universe.source_identity,
            "symbols_hash": universe_hash(contract.universe.symbols),
            "survivorship_bias_protected": contract.universe.survivorship_bias_protected,
        },
        "summaries": {
            "by_role_window_segment": segment_summary,
            "by_role_symbol": grouped(("role", "symbol")),
            "by_role_regime": grouped(("role", "regime")),
        },
        "rows": details,
        "historical_validation": "FAIL" if negative_oos else "PASS",
        "negative_oos_segments": negative_oos,
        "promotion_evidence": {
            "historical_calibration": "PRESENT",
            "historical_oos": "FAIL" if negative_oos else "PASS",
            "future_paper": "REQUIRED_NOT_PROVIDED",
            "oos_replaces_fresh_paper": False,
            "live_authorized": False,
        },
    }
