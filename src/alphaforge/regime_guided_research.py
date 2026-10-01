"""Evidence-first, non-authoritative REGIME_GUIDED research for issue #565.

This module derives deterministic closed-candle features, persists them in a
separate research database, and summarizes frozen outcome rows. Nothing here
changes ACCEPT/REJECT, side, geometry, sizing, portfolio risk, order flow, or
LIVE authorization.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "regime_guided_research_v1"
REPORT_VERSION = "regime_guided_research_report_v1"
SEGMENT_ROLES = frozenset({"CALIBRATION", "OOS", "UNTOUCHED_HOLDOUT"})


@dataclass(frozen=True, slots=True)
class ResearchStack:
    stack_id: str
    regime_timeframe: str
    setup_timeframe: str
    execution_timeframe: str
    macro_context_timeframe: str | None = None


RESEARCH_STACKS: Mapping[str, ResearchStack] = {
    "1h-15m-1m": ResearchStack("1h-15m-1m", "1h", "15m", "1m"),
    "4h-1h-15m": ResearchStack("4h-1h-15m", "4h", "1h", "15m"),
    "1d-4h-1h": ResearchStack("1d-4h-1h", "1d", "4h", "1h"),
    "1d-context-4h-1h-15m": ResearchStack(
        "1d-context-4h-1h-15m", "4h", "1h", "15m", "1d"
    ),
}


@dataclass(frozen=True, slots=True)
class FrozenResearchIdentity:
    git_sha: str
    config_hash: str
    data_hash: str
    universe_hash: str
    experiment_id: str
    stack_id: str
    segment_role: str
    segment_id: str
    minimum_segment_samples: int = 30

    def validate(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if len(self.git_sha) != 40 or any(c not in "0123456789abcdefABCDEF" for c in self.git_sha):
            blockers.append("INVALID_GIT_SHA")
        for name, value in (
            ("config_hash", self.config_hash),
            ("data_hash", self.data_hash),
            ("universe_hash", self.universe_hash),
        ):
            if len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
                blockers.append(f"INVALID_{name.upper()}")
        if self.stack_id not in RESEARCH_STACKS:
            blockers.append("UNKNOWN_STACK_ID")
        if self.segment_role not in SEGMENT_ROLES:
            blockers.append("INVALID_SEGMENT_ROLE")
        if not str(self.experiment_id).strip():
            blockers.append("MISSING_EXPERIMENT_ID")
        if not str(self.segment_id).strip():
            blockers.append("MISSING_SEGMENT_ID")
        if int(self.minimum_segment_samples) < 1:
            blockers.append("INVALID_MINIMUM_SEGMENT_SAMPLES")
        return tuple(sorted(set(blockers)))


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_positive(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0.0 else None


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def closed_only(
    candles: Sequence[Mapping[str, Any]] | None,
    *,
    decision_ts_ms: int,
) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    for raw in candles or ():
        if not isinstance(raw, Mapping):
            continue
        close_ts = raw.get("close_ts")
        if isinstance(close_ts, bool) or not isinstance(close_ts, int) or close_ts > decision_ts_ms:
            continue
        values = {key: _safe_positive(raw.get(key)) for key in ("open", "high", "low", "close")}
        if any(value is None for value in values.values()):
            continue
        if float(values["low"]) > float(values["high"]):
            continue
        row = dict(raw)
        row.update(values)
        out.append(row)
    out.sort(key=lambda row: int(row.get("open_ts") or 0))
    return tuple(out)


def _ma(candles: Sequence[Mapping[str, Any]], period: int) -> float | None:
    if len(candles) < period:
        return None
    closes = [_safe_positive(row.get("close")) for row in candles[-period:]]
    if any(value is None for value in closes):
        return None
    return sum(float(value) for value in closes if value is not None) / period


def atr(candles: Sequence[Mapping[str, Any]], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    window = candles[-(period + 1):]
    for index in range(1, len(window)):
        current = window[index]
        previous = window[index - 1]
        high = _safe_positive(current.get("high"))
        low = _safe_positive(current.get("low"))
        previous_close = _safe_positive(previous.get("close"))
        if high is None or low is None or previous_close is None:
            return None
        trs.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(trs) / len(trs) if trs else None


def _structural_state(candles: Sequence[Mapping[str, Any]]) -> str | None:
    if len(candles) < 12:
        return None
    prior, recent = candles[-12:-6], candles[-6:]
    prior_high = max(float(row["high"]) for row in prior)
    prior_low = min(float(row["low"]) for row in prior)
    recent_high = max(float(row["high"]) for row in recent)
    recent_low = min(float(row["low"]) for row in recent)
    if recent_high > prior_high and recent_low > prior_low:
        return "HH_HL"
    if recent_high < prior_high and recent_low < prior_low:
        return "LH_LL"
    return "MIXED"


def _bars_since_extreme(candles: Sequence[Mapping[str, Any]], key: str) -> int | None:
    if not candles:
        return None
    values = [float(row[key]) for row in candles]
    extreme = min(values) if key == "low" else max(values)
    for age, value in enumerate(reversed(values)):
        if value == extreme:
            return age
    return None


def _execution_variants(
    candles: Sequence[Mapping[str, Any]],
    *,
    trade_side: str,
) -> dict[str, bool | None]:
    side = str(trade_side or "").upper()
    if side not in {"LONG", "SHORT"} or len(candles) < 3:
        return {
            "reclaim_confirmation": None,
            "two_bar_persistence_confirmation": None,
        }
    last, previous, before = candles[-1], candles[-2], candles[-3]
    last_close = float(last["close"])
    previous_close = float(previous["close"])
    before_close = float(before["close"])
    if side == "LONG":
        reclaim = last_close > float(previous["high"])
        persistence = last_close > previous_close > before_close
    else:
        reclaim = last_close < float(previous["low"])
        persistence = last_close < previous_close < before_close
    return {
        "reclaim_confirmation": reclaim,
        "two_bar_persistence_confirmation": persistence,
    }


def compute_research_features(
    *,
    regime_candles: Sequence[Mapping[str, Any]] | None,
    setup_candles: Sequence[Mapping[str, Any]] | None,
    execution_candles: Sequence[Mapping[str, Any]] | None,
    decision_ts_ms: int,
    trade_side: str,
    executable_entry: Any = None,
    structural_stop: Any = None,
) -> dict[str, Any]:
    """Compute H1-H4 features from closed candles only.

    Missing evidence stays None. This function has no dependency on production
    thresholds or mutable decision objects.
    """
    regime = closed_only(regime_candles, decision_ts_ms=decision_ts_ms)
    setup = closed_only(setup_candles, decision_ts_ms=decision_ts_ms)
    execution = closed_only(execution_candles, decision_ts_ms=decision_ts_ms)
    side = str(trade_side or "").upper()

    fast = _ma(regime, 8)
    slow = _ma(regime, 20)
    atr_regime = atr(regime)
    raw_ma_delta = None
    normalized_regime_strength = None
    if fast is not None and slow is not None and slow > 0:
        raw_ma_delta = (fast - slow) / slow
    if fast is not None and slow is not None and atr_regime is not None and atr_regime > 0:
        normalized_regime_strength = abs(fast - slow) / atr_regime

    atr_setup = atr(setup)
    last_setup = setup[-1] if setup else None
    recent_setup = setup[-12:] if len(setup) >= 12 else ()
    support = min((float(row["low"]) for row in recent_setup), default=None)
    resistance = max((float(row["high"]) for row in recent_setup), default=None)
    pullback_depth_atr = None
    support_distance_atr = None
    resistance_distance_atr = None
    swing_age = None
    range_compression_ratio = None
    if atr_setup is not None and atr_setup > 0 and last_setup is not None and recent_setup:
        close = float(last_setup["close"])
        if side == "LONG":
            pullback_depth_atr = (float(resistance) - close) / atr_setup if resistance is not None else None
            swing_age = _bars_since_extreme(recent_setup, "low")
        elif side == "SHORT":
            pullback_depth_atr = (close - float(support)) / atr_setup if support is not None else None
            swing_age = _bars_since_extreme(recent_setup, "high")
        if support is not None:
            support_distance_atr = (close - float(support)) / atr_setup
        if resistance is not None:
            resistance_distance_atr = (float(resistance) - close) / atr_setup
        full_range = float(resistance) - float(support)
        recent4 = recent_setup[-4:]
        short_range = max(float(row["high"]) for row in recent4) - min(float(row["low"]) for row in recent4)
        if full_range > 0:
            range_compression_ratio = short_range / full_range

    entry = _safe_positive(executable_entry)
    stop = _safe_positive(structural_stop)
    stop_noise_ratio = None
    if entry is not None and stop is not None and atr_setup is not None and atr_setup > 0:
        stop_noise_ratio = abs(entry - stop) / atr_setup

    variants = _execution_variants(execution, trade_side=side)
    return {
        "evidence_schema": SCHEMA_VERSION,
        "decision_timestamp": _iso_ms(decision_ts_ms),
        "trade_side": side if side in {"LONG", "SHORT"} else None,
        "h1_regime_strength": {
            "raw_ma_delta": raw_ma_delta,
            "atr": atr_regime,
            "normalized_strength": normalized_regime_strength,
            "source_rows": len(regime),
        },
        "h2_setup_structure": {
            "structural_state": _structural_state(setup),
            "atr": atr_setup,
            "pullback_depth_atr": pullback_depth_atr,
            "support_distance_atr": support_distance_atr,
            "resistance_distance_atr": resistance_distance_atr,
            "swing_age_bars": swing_age,
            "range_compression_ratio": range_compression_ratio,
            "source_rows": len(setup),
        },
        "h3_stop_noise": {
            "executable_entry": entry,
            "structural_stop": stop,
            "stop_noise_ratio": stop_noise_ratio,
        },
        "h4_execution_confirmation": {
            **variants,
            "source_rows": len(execution),
        },
        "provenance": {
            "closed_candles_only": True,
            "decision_ts_ms": int(decision_ts_ms),
            "future_rows_excluded": True,
            "authoritative": False,
        },
    }


def calibration_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    bucket_width: float = 0.1,
) -> dict[str, Any]:
    """H5 calibration diagnostics from resolved, attributable labels only."""
    width = _finite(bucket_width)
    if width is None or width <= 0.0 or width > 1.0:
        raise ValueError("bucket_width must be in (0,1]")
    usable: list[tuple[float, int, str, str, str]] = []
    excluded = 0
    for row in rows:
        p = _finite(row.get("p_win"))
        resolved = row.get("resolved_win")
        complete = row.get("evidence_complete") is True
        ambiguous = row.get("ambiguous") is True
        if (
            p is None or p < 0.0 or p > 1.0
            or not isinstance(resolved, bool)
            or not complete
            or ambiguous
        ):
            excluded += 1
            continue
        usable.append((
            p,
            1 if resolved else 0,
            str(row.get("side") or "UNKNOWN").upper(),
            str(row.get("setup_phase") or "UNKNOWN").upper(),
            str(row.get("regime") or "UNKNOWN").upper(),
        ))
    if not usable:
        return {
            "status": "INCOMPLETE",
            "sample_count": 0,
            "excluded_count": excluded,
            "brier_score": None,
            "expected_calibration_error": None,
            "buckets": [],
            "breakdowns": [],
        }

    bucket_count = max(1, int(math.ceil(1.0 / width)))
    buckets: list[dict[str, Any]] = []
    weighted_error = 0.0
    for index in range(bucket_count):
        low = index * width
        high = min(1.0, low + width)
        members = [
            item for item in usable
            if item[0] >= low and (item[0] < high or (index == bucket_count - 1 and item[0] <= high))
        ]
        if not members:
            continue
        predicted = sum(item[0] for item in members) / len(members)
        observed = sum(item[1] for item in members) / len(members)
        ci_low, ci_high = _wilson_interval(sum(item[1] for item in members), len(members))
        error = abs(predicted - observed)
        weighted_error += error * len(members)
        buckets.append({
            "low": low,
            "high": high,
            "n": len(members),
            "mean_predicted": predicted,
            "observed_rate": observed,
            "observed_rate_ci95": [ci_low, ci_high],
            "absolute_calibration_error": error,
        })

    breakdowns: list[dict[str, Any]] = []
    for dimension, position in (("side", 2), ("setup_phase", 3), ("regime", 4)):
        grouped: dict[str, list[tuple[float, int, str, str, str]]] = {}
        for item in usable:
            grouped.setdefault(item[position], []).append(item)
        for key, members in sorted(grouped.items()):
            breakdowns.append({
                "dimension": dimension,
                "value": key,
                "n": len(members),
                "mean_predicted": sum(item[0] for item in members) / len(members),
                "observed_rate": sum(item[1] for item in members) / len(members),
            })
    return {
        "status": "COMPLETE",
        "sample_count": len(usable),
        "excluded_count": excluded,
        "brier_score": sum((p - outcome) ** 2 for p, outcome, *_ in usable) / len(usable),
        "expected_calibration_error": weighted_error / len(usable),
        "buckets": buckets,
        "breakdowns": breakdowns,
    }


def _wilson_interval(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denominator = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def build_research_observation(
    *,
    identity: FrozenResearchIdentity,
    symbol: str,
    signal_id: str,
    decision_ts_ms: int,
    authoritative: Mapping[str, Any],
    features: Mapping[str, Any],
    observed_at: str | None = None,
) -> dict[str, Any]:
    blockers = identity.validate()
    if blockers:
        raise ValueError("invalid frozen research identity: " + ",".join(blockers))
    stack = RESEARCH_STACKS[identity.stack_id]
    baseline = {
        key: authoritative.get(key)
        for key in (
            "decision", "reject_reason", "side", "entry", "sl", "tp",
            "raw_rr", "effective_rr", "score", "risk_scale",
        )
    }
    identity_payload = {**asdict(identity), "stack": asdict(stack)}
    research_id = "rgr_" + _hash([
        identity_payload, str(symbol).upper(), signal_id, int(decision_ts_ms)
    ])[:32]
    return {
        "research_id": research_id,
        "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
        "symbol": str(symbol).upper(),
        "signal_id": str(signal_id),
        "decision_timestamp": _iso_ms(int(decision_ts_ms)),
        "experiment_id": identity.experiment_id,
        "segment_role": identity.segment_role,
        "segment_id": identity.segment_id,
        "stack_id": stack.stack_id,
        "regime_timeframe": stack.regime_timeframe,
        "setup_timeframe": stack.setup_timeframe,
        "execution_timeframe": stack.execution_timeframe,
        "macro_context_timeframe": stack.macro_context_timeframe,
        "identity_hash": _hash(identity_payload),
        "identity": identity_payload,
        "authoritative_snapshot": baseline,
        "features": dict(features),
        "evidence_complete": not _feature_blockers(features),
        "source_provenance": {
            "shadow_only": True,
            "production_authority_changed": False,
            "live_authorization_changed": False,
        },
    }


def _feature_blockers(features: Mapping[str, Any]) -> tuple[str, ...]:
    blockers: list[str] = []
    h1 = features.get("h1_regime_strength")
    h2 = features.get("h2_setup_structure")
    h3 = features.get("h3_stop_noise")
    h4 = features.get("h4_execution_confirmation")
    if not isinstance(h1, Mapping) or h1.get("normalized_strength") is None:
        blockers.append("REGIME_NORMALIZATION_UNAVAILABLE")
    if not isinstance(h2, Mapping) or h2.get("atr") is None:
        blockers.append("SETUP_STRUCTURE_EVIDENCE_UNAVAILABLE")
    if not isinstance(h3, Mapping) or h3.get("stop_noise_ratio") is None:
        blockers.append("STOP_NOISE_EVIDENCE_UNAVAILABLE")
    if not isinstance(h4, Mapping) or (
        h4.get("reclaim_confirmation") is None
        or h4.get("two_bar_persistence_confirmation") is None
    ):
        blockers.append("EXECUTION_VARIANT_EVIDENCE_UNAVAILABLE")
    return tuple(blockers)


class RegimeGuidedResearchStore:
    """Separate idempotent research store; campaign databases are refused."""

    def __init__(self, database: str | Path | sqlite3.Connection):
        self.database = database

    def _connection(self) -> tuple[sqlite3.Connection, bool]:
        if isinstance(self.database, sqlite3.Connection):
            self.database.row_factory = sqlite3.Row
            return self.database, False
        path = Path(self.database)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn, True

    @staticmethod
    def _bootstrap(conn: sqlite3.Connection) -> None:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('burnin_campaigns','burnin_runs') LIMIT 1"
        ).fetchone():
            raise ValueError("research output must not share the campaign database")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS regime_guided_research_observations (
                research_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                signal_id TEXT NOT NULL,
                decision_timestamp TEXT NOT NULL,
                experiment_id TEXT NOT NULL,
                segment_role TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                stack_id TEXT NOT NULL,
                regime_timeframe TEXT NOT NULL,
                setup_timeframe TEXT NOT NULL,
                execution_timeframe TEXT NOT NULL,
                macro_context_timeframe TEXT,
                identity_hash TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                authoritative_json TEXT NOT NULL,
                feature_json TEXT NOT NULL,
                evidence_complete INTEGER NOT NULL,
                source_provenance_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                schema_version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_rgr_segment
              ON regime_guided_research_observations(experiment_id,segment_role,segment_id,stack_id);
            CREATE TABLE IF NOT EXISTS regime_guided_research_outcomes (
                research_id TEXT PRIMARY KEY,
                outcome_status TEXT NOT NULL,
                net_r REAL,
                mfe_r REAL,
                mae_r REAL,
                execution_cost_drag_r REAL,
                hold_duration_seconds REAL,
                resolved_win INTEGER,
                ambiguous INTEGER NOT NULL DEFAULT 0,
                evidence_complete INTEGER NOT NULL DEFAULT 0,
                outcome_json TEXT NOT NULL,
                resolved_at TEXT,
                schema_version TEXT NOT NULL
            );
            """
        )

    def record(self, observation: Mapping[str, Any]) -> str:
        required = {
            "research_id", "observed_at", "symbol", "signal_id", "decision_timestamp",
            "experiment_id", "segment_role", "segment_id", "stack_id",
            "regime_timeframe", "setup_timeframe", "execution_timeframe",
            "identity_hash", "identity", "authoritative_snapshot", "features",
            "evidence_complete", "source_provenance",
        }
        missing = sorted(required - set(observation))
        if missing:
            raise ValueError("missing research fields: " + ",".join(missing))
        conn, owned = self._connection()
        try:
            self._bootstrap(conn)
            values = {
                **dict(observation),
                "identity_json": _canonical_json(observation["identity"]),
                "authoritative_json": _canonical_json(observation["authoritative_snapshot"]),
                "feature_json": _canonical_json(observation["features"]),
                "source_provenance_json": _canonical_json(observation["source_provenance"]),
                "evidence_complete": int(bool(observation["evidence_complete"])),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "schema_version": SCHEMA_VERSION,
            }
            columns = (
                "research_id observed_at symbol signal_id decision_timestamp experiment_id "
                "segment_role segment_id stack_id regime_timeframe setup_timeframe "
                "execution_timeframe macro_context_timeframe identity_hash identity_json "
                "authoritative_json feature_json evidence_complete source_provenance_json "
                "updated_at schema_version"
            ).split()
            placeholders = ",".join(f":{name}" for name in columns)
            updates = ",".join(
                f"{name}=excluded.{name}"
                for name in columns
                if name not in {"research_id", "observed_at"}
            )
            conn.execute(
                f"INSERT INTO regime_guided_research_observations ({','.join(columns)}) "
                f"VALUES ({placeholders}) ON CONFLICT(research_id) DO UPDATE SET {updates}",
                values,
            )
            conn.commit()
            return str(observation["research_id"])
        finally:
            if owned:
                conn.close()

    def record_outcome(
        self,
        research_id: str,
        *,
        outcome_status: str,
        net_r: Any = None,
        mfe_r: Any = None,
        mae_r: Any = None,
        execution_cost_drag_r: Any = None,
        hold_duration_seconds: Any = None,
        resolved_win: bool | None = None,
        ambiguous: bool = False,
        evidence_complete: bool = False,
        outcome: Mapping[str, Any] | None = None,
        resolved_at: str | None = None,
    ) -> None:
        conn, owned = self._connection()
        try:
            self._bootstrap(conn)
            if conn.execute(
                "SELECT 1 FROM regime_guided_research_observations WHERE research_id=?",
                (research_id,),
            ).fetchone() is None:
                raise ValueError("unknown research_id")
            conn.execute(
                """
                INSERT INTO regime_guided_research_outcomes(
                    research_id,outcome_status,net_r,mfe_r,mae_r,execution_cost_drag_r,
                    hold_duration_seconds,resolved_win,ambiguous,evidence_complete,
                    outcome_json,resolved_at,schema_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(research_id) DO UPDATE SET
                    outcome_status=excluded.outcome_status,
                    net_r=excluded.net_r,mfe_r=excluded.mfe_r,mae_r=excluded.mae_r,
                    execution_cost_drag_r=excluded.execution_cost_drag_r,
                    hold_duration_seconds=excluded.hold_duration_seconds,
                    resolved_win=excluded.resolved_win,
                    ambiguous=excluded.ambiguous,
                    evidence_complete=excluded.evidence_complete,
                    outcome_json=excluded.outcome_json,
                    resolved_at=excluded.resolved_at,
                    schema_version=excluded.schema_version
                """,
                (
                    research_id,
                    str(outcome_status),
                    _finite(net_r),
                    _finite(mfe_r),
                    _finite(mae_r),
                    _finite(execution_cost_drag_r),
                    _finite(hold_duration_seconds),
                    None if resolved_win is None else int(resolved_win),
                    int(bool(ambiguous)),
                    int(bool(evidence_complete)),
                    _canonical_json(outcome or {}),
                    resolved_at,
                    SCHEMA_VERSION,
                ),
            )
            conn.commit()
        finally:
            if owned:
                conn.close()


def stack_summary(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(
            (str(row.get("segment_role") or "UNKNOWN"), str(row.get("stack_id") or "UNKNOWN")),
            [],
        ).append(row)
    output: list[dict[str, Any]] = []
    for (segment_role, stack_id), members in sorted(grouped.items()):
        complete = [
            row for row in members
            if row.get("evidence_complete") is True
            and row.get("ambiguous") is not True
            and _finite(row.get("net_r")) is not None
        ]
        net = [float(row["net_r"]) for row in complete]
        mfe = [float(value) for row in complete if (value := _finite(row.get("mfe_r"))) is not None]
        mae = [float(value) for row in complete if (value := _finite(row.get("mae_r"))) is not None]
        cost = [
            float(value)
            for row in complete
            if (value := _finite(row.get("execution_cost_drag_r"))) is not None
        ]
        output.append({
            "segment_role": segment_role,
            "stack_id": stack_id,
            "candidate_count": len(members),
            "complete_outcome_count": len(complete),
            "total_net_r": sum(net) if net else None,
            "mean_net_r": statistics.fmean(net) if net else None,
            "median_net_r": statistics.median(net) if net else None,
            "max_drawdown_r": _max_drawdown(net) if net else None,
            "tail_loss_r": min(net) if net else None,
            "mean_mfe_r": statistics.fmean(mfe) if mfe else None,
            "mean_mae_r": statistics.fmean(mae) if mae else None,
            "mean_execution_cost_drag_r": statistics.fmean(cost) if cost else None,
        })
    return output


def _max_drawdown(values: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def research_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    baseline_stack_id: str = "1h-15m-1m",
    candidate_stack_id: str | None = None,
    minimum_segment_samples: int = 30,
) -> dict[str, Any]:
    materialized = tuple(dict(row) for row in rows)
    summaries = stack_summary(materialized)
    blockers: list[str] = []
    if baseline_stack_id not in RESEARCH_STACKS:
        blockers.append("UNKNOWN_BASELINE_STACK")
    if candidate_stack_id is not None and candidate_stack_id not in RESEARCH_STACKS:
        blockers.append("UNKNOWN_CANDIDATE_STACK")
    if minimum_segment_samples < 1:
        blockers.append("INVALID_MINIMUM_SEGMENT_SAMPLES")

    by_key = {
        (item["segment_role"], item["stack_id"]): item
        for item in summaries
    }
    verdict = "INCONCLUSIVE"
    if not blockers and candidate_stack_id is not None:
        required = [
            ("OOS", baseline_stack_id),
            ("OOS", candidate_stack_id),
            ("UNTOUCHED_HOLDOUT", baseline_stack_id),
            ("UNTOUCHED_HOLDOUT", candidate_stack_id),
        ]
        missing = [
            f"MISSING_{role}_{stack}"
            for role, stack in required
            if (role, stack) not in by_key
        ]
        blockers.extend(missing)
        if not missing:
            insufficient = [
                f"INSUFFICIENT_{role}_{stack}"
                for role, stack in required
                if int(by_key[(role, stack)]["complete_outcome_count"]) < minimum_segment_samples
            ]
            blockers.extend(insufficient)
        if not blockers:
            base_oos = by_key[("OOS", baseline_stack_id)]
            cand_oos = by_key[("OOS", candidate_stack_id)]
            base_hold = by_key[("UNTOUCHED_HOLDOUT", baseline_stack_id)]
            cand_hold = by_key[("UNTOUCHED_HOLDOUT", candidate_stack_id)]
            supported = (
                _strictly_better_expectancy(cand_oos, base_oos)
                and _strictly_better_expectancy(cand_hold, base_hold)
                and _not_worse_drawdown(cand_oos, base_oos)
                and _not_worse_drawdown(cand_hold, base_hold)
            )
            verdict = (
                "SUPPORTED_FOR_SEPARATE_PRODUCTION_CHANGE"
                if supported else "PASS_WITH_FINDINGS"
            )

    return {
        "schema_version": REPORT_VERSION,
        "research_issue": 565,
        "production_semantics_changed": False,
        "live_authorized": False,
        "baseline_stack_id": baseline_stack_id,
        "candidate_stack_id": candidate_stack_id,
        "minimum_segment_samples": minimum_segment_samples,
        "summaries": summaries,
        "blockers": tuple(sorted(set(blockers))),
        "verdict": verdict,
    }


def _strictly_better_expectancy(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> bool:
    c = _finite(candidate.get("mean_net_r"))
    b = _finite(baseline.get("mean_net_r"))
    return c is not None and b is not None and c > b


def _not_worse_drawdown(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> bool:
    c = _finite(candidate.get("max_drawdown_r"))
    b = _finite(baseline.get("max_drawdown_r"))
    return c is not None and b is not None and c <= b


def stack_env(stack_id: str) -> dict[str, str]:
    stack = RESEARCH_STACKS.get(stack_id)
    if stack is None:
        raise ValueError("unknown research stack")
    out = {
        "ALPHAFORGE_REGIME_TIMEFRAME": stack.regime_timeframe,
        "ALPHAFORGE_SETUP_TIMEFRAME": stack.setup_timeframe,
        "ALPHAFORGE_EXECUTION_TIMEFRAME": stack.execution_timeframe,
    }
    if stack.macro_context_timeframe:
        out["ALPHAFORGE_RESEARCH_MACRO_CONTEXT_TIMEFRAME"] = stack.macro_context_timeframe
    return out
