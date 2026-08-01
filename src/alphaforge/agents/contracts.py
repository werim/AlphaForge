"""Immutable, deterministic contracts for the Phase A shadow agent graph."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping


class AgentStage(str, Enum):
    MARKET = "MARKET"
    SIGNAL = "SIGNAL"
    QUALITY = "QUALITY"
    RISK = "RISK"
    EXECUTION = "EXECUTION"
    VERIFICATION = "VERIFICATION"
    REFLECTION = "REFLECTION"
    PORTFOLIO = "PORTFOLIO"


class DecisionStatus(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    DEFER = "DEFER"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"value of type {type(value).__name__} is not JSON-serializable")


def canonical_json(value: Any) -> str:
    """Return compact, key-sorted JSON; reject lossy/fallback serialization."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=_json_default)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_reason_codes(reason_codes: Any) -> tuple[str, ...]:
    if reason_codes is None:
        return ()
    if isinstance(reason_codes, str):
        reason_codes = (reason_codes,)
    normalized = {str(code).strip().upper() for code in reason_codes if str(code).strip()}
    return tuple(sorted(normalized))


def _freeze_json(value: Any) -> Any:
    # Validation happens first so unsupported objects never become opaque strings.
    canonical_json(value)
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_json(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(v) for v in value)
    return value


def _parse_utc(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} must be UTC")
    return parsed


@dataclass(frozen=True, slots=True)
class DecisionEnvelope:
    decision_id: str
    correlation_id: str
    symbol: str | None
    execution_mode: str
    stage: AgentStage
    status: DecisionStatus
    primary_reason: str = ""
    reason_codes: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    input_hash: str = field(default_factory=lambda: stable_hash(None))
    config_hash: str = field(default_factory=lambda: stable_hash(None))
    agent_version: str = "phase-a"
    started_at: str = field(default_factory=utc_now_iso)
    completed_at: str = field(default_factory=utc_now_iso)
    duration_ms: float = 0.0
    retry_count: int = 0
    skipped_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", AgentStage(self.stage))
        object.__setattr__(self, "status", DecisionStatus(self.status))
        object.__setattr__(self, "reason_codes", normalize_reason_codes(self.reason_codes))
        object.__setattr__(self, "evidence", _freeze_json(self.evidence))
        validate_decision_envelope(self)

    @property
    def hard_reject(self) -> bool:
        return self.status is DecisionStatus.REJECT and bool(self.evidence.get("hard_reject", False))


@dataclass(frozen=True, slots=True)
class StageInput:
    decision_id: str
    correlation_id: str
    execution_mode: str
    stage: AgentStage
    symbol: str | None = None
    payload: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    prior_results: tuple[DecisionEnvelope, ...] = ()
    retry_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", AgentStage(self.stage))
        object.__setattr__(self, "payload", _freeze_json(self.payload))
        if not self.decision_id or not self.correlation_id:
            raise ValueError("decision_id and correlation_id must be non-empty")


@dataclass(frozen=True, slots=True)
class StageOutput:
    envelope: DecisionEnvelope


@dataclass(frozen=True, slots=True)
class AgentTrace:
    correlation_id: str
    stage_results: tuple[DecisionEnvelope, ...]


def validate_decision_envelope(envelope: DecisionEnvelope) -> None:
    if not envelope.decision_id.strip() or not envelope.correlation_id.strip():
        raise ValueError("decision_id and correlation_id must be non-empty")
    if not envelope.execution_mode.strip():
        raise ValueError("execution_mode must be non-empty")
    if envelope.status in {DecisionStatus.REJECT, DecisionStatus.DEFER, DecisionStatus.ERROR, DecisionStatus.SKIPPED} and not envelope.primary_reason.strip():
        raise ValueError(f"primary_reason is required for {envelope.status.value}")
    if envelope.duration_ms < 0:
        raise ValueError("duration_ms cannot be negative")
    if envelope.retry_count < 0:
        raise ValueError("retry_count cannot be negative")
    started = _parse_utc(envelope.started_at, "started_at")
    completed = _parse_utc(envelope.completed_at, "completed_at")
    if completed < started:
        raise ValueError("completed_at cannot precede started_at")
    for name in ("input_hash", "config_hash"):
        digest = getattr(envelope, name)
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError(f"{name} must be a lowercase SHA-256 hash")
    canonical_json(envelope.evidence)


def envelope_to_dict(envelope: DecisionEnvelope) -> dict[str, Any]:
    return {
        "decision_id": envelope.decision_id, "correlation_id": envelope.correlation_id,
        "symbol": envelope.symbol, "execution_mode": envelope.execution_mode,
        "stage": envelope.stage.value, "status": envelope.status.value,
        "primary_reason": envelope.primary_reason, "reason_codes": list(envelope.reason_codes),
        "evidence": json.loads(canonical_json(envelope.evidence)), "input_hash": envelope.input_hash,
        "config_hash": envelope.config_hash, "agent_version": envelope.agent_version,
        "started_at": envelope.started_at, "completed_at": envelope.completed_at,
        "duration_ms": envelope.duration_ms, "retry_count": envelope.retry_count,
        "skipped_reason": envelope.skipped_reason,
    }


def envelope_from_dict(value: Mapping[str, Any]) -> DecisionEnvelope:
    return DecisionEnvelope(**dict(value))
