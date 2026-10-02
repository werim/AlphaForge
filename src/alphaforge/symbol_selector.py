from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any, Mapping, Protocol, Sequence


UNIVERSE_SCHEMA_VERSION = "universe_selection_v1"
RANKING_VERSION = "opportunity_ranking_v1"
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{3,32}$")


class EvidenceState(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class MarketCapEvidence:
    value_usd: float | None
    observed_at: float | str | None
    source: str
    status: str


class MarketCapSnapshotProvider(Protocol):
    """Batch boundary for timestamp-bound market-cap snapshots.

    AlphaForge deliberately does not ship a synthetic market-cap source. A
    production adapter must return immutable evidence with source and time.
    """

    def snapshot(
        self, symbols: Sequence[str], *, observed_at_or_before: float,
    ) -> Mapping[str, MarketCapEvidence]: ...


def bind_market_cap_evidence(
    candidates: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, MarketCapEvidence],
) -> list[dict[str, Any]]:
    """Bind one provider snapshot without filling absent values with zero."""
    bound: list[dict[str, Any]] = []
    for candidate in candidates:
        row = dict(candidate)
        symbol = str(row.get("symbol") or "").strip().upper()
        evidence = snapshot.get(symbol)
        if evidence is None:
            row.update(
                market_cap_usd=None,
                market_cap_observed_at=None,
                market_cap_source="UNAVAILABLE",
                market_cap_status="UNAVAILABLE",
            )
        else:
            row.update(
                market_cap_usd=evidence.value_usd,
                market_cap_observed_at=evidence.observed_at,
                market_cap_source=evidence.source,
                market_cap_status=evidence.status,
            )
        bound.append(row)
    return bound


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp_seconds(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str) and not value.strip().replace(".", "", 1).isdigit():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result = parsed.timestamp()
        else:
            result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or result < 0:
        return None
    return result / 1000.0 if result > 10_000_000_000 else result


def _finite_nonnegative(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "INVALID_NON_FINITE"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _symbols(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    values = raw if isinstance(raw, (list, tuple, set, frozenset)) else re.split(r"[\s,]+", str(raw))
    normalized = tuple(sorted({str(value).strip().upper() for value in values if str(value).strip()}))
    invalid = [symbol for symbol in normalized if not _SYMBOL_PATTERN.fullmatch(symbol)]
    if invalid:
        raise ValueError(f"Malformed excluded symbol(s): {', '.join(invalid)}")
    return normalized


@dataclass(frozen=True, slots=True)
class UniverseConstraints:
    min_market_cap_usd: float | None = None
    max_market_cap_usd: float | None = None
    min_volume_24h_usd: float = 5_000_000.0
    candidate_pool_top_n_volume: int = 50
    max_active_symbols: int = 5
    excluded_symbols: tuple[str, ...] = ()
    max_evidence_age_sec: float = 120.0
    max_spread_pct: float = 0.05
    max_expected_slippage_pct: float = 0.05
    max_abs_funding_rate_pct: float = 0.001
    min_liquidity_score: float = 0.30
    quote_asset: str = "USDT"

    def __post_init__(self) -> None:
        numeric = {
            "min_market_cap_usd": self.min_market_cap_usd,
            "max_market_cap_usd": self.max_market_cap_usd,
            "min_volume_24h_usd": self.min_volume_24h_usd,
            "max_evidence_age_sec": self.max_evidence_age_sec,
            "max_spread_pct": self.max_spread_pct,
            "max_expected_slippage_pct": self.max_expected_slippage_pct,
            "max_abs_funding_rate_pct": self.max_abs_funding_rate_pct,
            "min_liquidity_score": self.min_liquidity_score,
        }
        for name, value in numeric.items():
            if value is not None and _finite_nonnegative(value) is None:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.min_market_cap_usd is not None and self.max_market_cap_usd is not None:
            if self.min_market_cap_usd > self.max_market_cap_usd:
                raise ValueError("min_market_cap_usd must not exceed max_market_cap_usd")
        if not isinstance(self.candidate_pool_top_n_volume, int) or self.candidate_pool_top_n_volume <= 0:
            raise ValueError("candidate_pool_top_n_volume must be a positive integer")
        if not isinstance(self.max_active_symbols, int) or self.max_active_symbols <= 0:
            raise ValueError("max_active_symbols must be a positive integer")
        if self.max_active_symbols > self.candidate_pool_top_n_volume:
            raise ValueError("max_active_symbols must not exceed candidate_pool_top_n_volume")
        if self.min_liquidity_score > 1.0:
            raise ValueError("min_liquidity_score must not exceed 1.0")
        canonical_excluded = _symbols(self.excluded_symbols)
        object.__setattr__(self, "excluded_symbols", canonical_excluded)
        quote_asset = str(self.quote_asset).strip().upper()
        if not quote_asset or not re.fullmatch(r"[A-Z0-9]{2,12}", quote_asset):
            raise ValueError("quote_asset must be an uppercase alphanumeric asset code")
        object.__setattr__(self, "quote_asset", quote_asset)

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any] | None = None) -> "UniverseConstraints":
        cfg = dict(config or {})

        def value(*names: str, default: Any) -> Any:
            return next((cfg[name] for name in names if name in cfg), default)

        def optional_float(*names: str) -> float | None:
            raw = value(*names, default=None)
            return None if raw in (None, "") else float(raw)

        return cls(
            min_market_cap_usd=optional_float("universe_min_market_cap_usd", "min_market_cap_usd"),
            max_market_cap_usd=optional_float("universe_max_market_cap_usd", "max_market_cap_usd"),
            min_volume_24h_usd=float(value("universe_min_volume_24h_usd", "min_volume_24h_usd", "min_volume_24h_usdt", default=5_000_000.0)),
            candidate_pool_top_n_volume=int(value("universe_candidate_pool_top_n_volume", "candidate_pool_top_n_volume", default=50)),
            max_active_symbols=int(value("max_active_symbols", "max_symbols_per_scan", default=5)),
            excluded_symbols=_symbols(value("universe_excluded_symbols", "excluded_symbols", default=())),
            max_evidence_age_sec=float(value("universe_max_evidence_age_sec", "max_evidence_age_sec", "stale_market_data_sec", default=120.0)),
            max_spread_pct=float(value("max_spread_pct", "MAX_SPREAD_PCT", default=0.05)),
            max_expected_slippage_pct=float(value("max_expected_slippage_pct", "MAX_EXPECTED_SLIPPAGE_PCT", "MAX_SLIPPAGE_PCT", default=0.05)),
            max_abs_funding_rate_pct=float(value("max_abs_funding_rate_pct", "MAX_ABS_FUNDING_RATE_PCT", default=0.001)),
            min_liquidity_score=float(value("min_liquidity_score", "MIN_LIQUIDITY_SCORE", default=0.30)),
            quote_asset=str(value("universe_quote_asset", "quote_asset", default="USDT")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_market_cap_usd": self.min_market_cap_usd,
            "max_market_cap_usd": self.max_market_cap_usd,
            "min_volume_24h_usd": self.min_volume_24h_usd,
            "candidate_pool_top_n_volume": self.candidate_pool_top_n_volume,
            "max_active_symbols": self.max_active_symbols,
            "excluded_symbols": list(self.excluded_symbols),
            "max_evidence_age_sec": self.max_evidence_age_sec,
            "max_spread_pct": self.max_spread_pct,
            "max_expected_slippage_pct": self.max_expected_slippage_pct,
            "max_abs_funding_rate_pct": self.max_abs_funding_rate_pct,
            "min_liquidity_score": self.min_liquidity_score,
            "quote_asset": self.quote_asset,
        }

    @property
    def config_hash(self) -> str:
        return _canonical_hash(self.as_dict())


@dataclass(frozen=True, slots=True)
class UniverseCandidateDecision:
    symbol: str
    state: EvidenceState
    reasons: tuple[str, ...]
    observed_inputs: dict[str, Any]
    ranking_components: dict[str, float | str | None]
    score: float | None
    rank: int | None
    selected: bool
    evidence_availability: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "state": self.state.value,
            "reasons": list(self.reasons),
            "observed_inputs": self.observed_inputs,
            "ranking_components": self.ranking_components,
            "score": self.score,
            "rank": self.rank,
            "selected": self.selected,
            "evidence_availability": self.evidence_availability,
        }


@dataclass(frozen=True, slots=True)
class SelectedUniverse:
    cycle_id: str
    decision_timestamp: float
    execution_mode: str
    constraints: UniverseConstraints
    candidates: tuple[UniverseCandidateDecision, ...]
    selected_symbols: tuple[str, ...]
    config_hash: str
    strategy_config_hash: str | None
    universe_hash: str
    evidence_hash: str
    git_sha: str
    source_provenance: tuple[str, ...]
    schema_version: str = UNIVERSE_SCHEMA_VERSION
    ranking_version: str = RANKING_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "decision_timestamp": self.decision_timestamp,
            "execution_mode": self.execution_mode,
            "constraints": self.constraints.as_dict(),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "selected_symbols": list(self.selected_symbols),
            "config_hash": self.config_hash,
            "strategy_config_hash": self.strategy_config_hash,
            "universe_hash": self.universe_hash,
            "evidence_hash": self.evidence_hash,
            "git_sha": self.git_sha,
            "source_provenance": list(self.source_provenance),
            "schema_version": self.schema_version,
            "ranking_version": self.ranking_version,
        }


def _candidate_base_decision(
    item: Mapping[str, Any], constraints: UniverseConstraints, decision_ts: float,
) -> tuple[str, EvidenceState, list[str], dict[str, Any], dict[str, str]]:
    symbol = str(item.get("symbol") or "").strip().upper()
    reasons: list[str] = []
    availability: dict[str, str] = {}
    state = EvidenceState.ELIGIBLE

    if not _SYMBOL_PATTERN.fullmatch(symbol):
        return symbol or "UNKNOWN", EvidenceState.INVALID, ["MALFORMED_SYMBOL"], _json_safe(dict(item)), {"symbol": "INVALID"}
    if symbol in constraints.excluded_symbols:
        reasons.append("EXPLICITLY_EXCLUDED")
        state = EvidenceState.INELIGIBLE
    candidate_quote = str(item.get("quote_asset") or item.get("quoteAsset") or "").strip().upper()
    if (candidate_quote and candidate_quote != constraints.quote_asset) or not symbol.endswith(constraints.quote_asset):
        reasons.append("QUOTE_ASSET_MISMATCH")
        state = EvidenceState.INELIGIBLE
    instrument_status = str(item.get("instrument_status") or item.get("status") or "TRADING").upper()
    if instrument_status not in {"TRADING", "ACTIVE"}:
        reasons.append("INSTRUMENT_NOT_TRADING")
        state = EvidenceState.INELIGIBLE
    contract_type = str(item.get("contract_type") or item.get("contractType") or "PERPETUAL").upper()
    if contract_type not in {"PERPETUAL", "SWAP"}:
        reasons.append("INELIGIBLE_INSTRUMENT_TYPE")
        state = EvidenceState.INELIGIBLE
    if item.get("snapshot_complete") is False:
        reasons.append("INCOMPLETE_CANDIDATE_SNAPSHOT")
        state = EvidenceState.UNAVAILABLE

    observed_at = _timestamp_seconds(item.get("market_observed_at", item.get("market_ts")))
    if observed_at is None:
        reasons.append("MARKET_EVIDENCE_UNAVAILABLE")
        availability["market_observed_at"] = "UNAVAILABLE"
        state = EvidenceState.UNAVAILABLE
    elif observed_at > decision_ts + 1.0:
        reasons.append("MARKET_EVIDENCE_INVALID_FUTURE_TIMESTAMP")
        availability["market_observed_at"] = "INVALID"
        state = EvidenceState.INVALID
    elif decision_ts - observed_at > constraints.max_evidence_age_sec:
        reasons.append("MARKET_EVIDENCE_STALE")
        availability["market_observed_at"] = "STALE"
        state = EvidenceState.STALE
    else:
        availability["market_observed_at"] = "AVAILABLE"

    required_metrics = (("volume_24h_usd", item.get("volume_24h_usd", item.get("volume_24h_usdt"))),
                        ("spread_pct", item.get("spread_pct")),
                        ("volatility_pct", item.get("volatility_pct")))
    parsed: dict[str, float | None] = {}
    for name, raw in required_metrics:
        parsed[name] = _finite_nonnegative(raw)
        status_key = {
            "volume_24h_usd": "volume_24h_status",
            "spread_pct": "spread_status",
            "volatility_pct": "volatility_status",
        }[name]
        explicit_status = str(item.get(status_key) or "").strip().upper()
        if raw is None:
            reasons.append(f"{name.upper()}_UNAVAILABLE")
            availability[name] = "UNAVAILABLE"
            if state not in {EvidenceState.INVALID, EvidenceState.STALE}:
                state = EvidenceState.UNAVAILABLE
        elif parsed[name] is None:
            reasons.append(f"{name.upper()}_INVALID")
            availability[name] = "INVALID"
            state = EvidenceState.INVALID
        else:
            availability[name] = "AVAILABLE"
            if explicit_status in {"UNAVAILABLE", "UNKNOWN"}:
                reasons.append(f"{name.upper()}_UNAVAILABLE_STATUS")
                availability[name] = "UNAVAILABLE"
                if state not in {EvidenceState.INVALID, EvidenceState.STALE}:
                    state = EvidenceState.UNAVAILABLE
            elif explicit_status == "STALE":
                reasons.append(f"{name.upper()}_STALE")
                availability[name] = "STALE"
                state = EvidenceState.STALE
            elif explicit_status == "INVALID":
                reasons.append(f"{name.upper()}_INVALID_STATUS")
                availability[name] = "INVALID"
                state = EvidenceState.INVALID
        metric_ts = _timestamp_seconds(item.get({
            "volume_24h_usd": "volume_24h_observed_at",
            "spread_pct": "spread_observed_at",
            "volatility_pct": "volatility_observed_at",
        }[name]))
        if item.get({
            "volume_24h_usd": "volume_24h_observed_at",
            "spread_pct": "spread_observed_at",
            "volatility_pct": "volatility_observed_at",
        }[name]) is not None:
            if metric_ts is None or metric_ts > decision_ts + 1.0:
                reasons.append(f"{name.upper()}_INVALID_TIMESTAMP")
                availability[name] = "INVALID"
                state = EvidenceState.INVALID
            elif decision_ts - metric_ts > constraints.max_evidence_age_sec:
                reasons.append(f"{name.upper()}_STALE")
                availability[name] = "STALE"
                state = EvidenceState.STALE

    volume = parsed["volume_24h_usd"]
    spread = parsed["spread_pct"]
    if volume is not None and volume < constraints.min_volume_24h_usd:
        reasons.append("BELOW_MIN_VOLUME_24H")
        if state is EvidenceState.ELIGIBLE:
            state = EvidenceState.INELIGIBLE
    if spread is not None and spread > constraints.max_spread_pct:
        reasons.append("UNSAFE_SPREAD")
        if state is EvidenceState.ELIGIBLE:
            state = EvidenceState.INELIGIBLE

    slippage_raw = item.get("expected_slippage_pct")
    slippage = _finite_nonnegative(slippage_raw)
    slippage_status = str(item.get("slippage_status") or "").strip().upper()
    if slippage_status in {"UNAVAILABLE", "UNKNOWN", "STALE"}:
        slippage = None
        availability["expected_slippage_pct"] = f"{slippage_status}_OPTIONAL"
    elif slippage_raw is None:
        availability["expected_slippage_pct"] = "UNAVAILABLE_OPTIONAL"
    elif slippage is None:
        reasons.append("EXPECTED_SLIPPAGE_PCT_INVALID")
        availability["expected_slippage_pct"] = "INVALID"
        state = EvidenceState.INVALID
    else:
        availability["expected_slippage_pct"] = "AVAILABLE"
        if slippage > constraints.max_expected_slippage_pct:
            reasons.append("UNSAFE_EXPECTED_SLIPPAGE")
            if state is EvidenceState.ELIGIBLE:
                state = EvidenceState.INELIGIBLE

    liquidity_raw = item.get("liquidity_score")
    liquidity = _finite_nonnegative(liquidity_raw)
    if liquidity_raw is None:
        availability["liquidity_score"] = "UNAVAILABLE_OPTIONAL"
    elif liquidity is None or liquidity > 1.0:
        reasons.append("LIQUIDITY_SCORE_INVALID")
        availability["liquidity_score"] = "INVALID"
        state = EvidenceState.INVALID
    else:
        availability["liquidity_score"] = "AVAILABLE"
        if liquidity < constraints.min_liquidity_score:
            reasons.append("LOW_LIQUIDITY")
            if state is EvidenceState.ELIGIBLE:
                state = EvidenceState.INELIGIBLE

    funding_raw = item.get("funding_rate_pct")
    funding_status = str(item.get("funding_status") or "").strip().upper()
    try:
        funding = None if funding_raw is None or isinstance(funding_raw, bool) else float(funding_raw)
    except (TypeError, ValueError):
        funding = math.nan
    if funding_status in {"UNAVAILABLE", "UNKNOWN", "STALE"} or funding_raw is None:
        funding = None
        availability["funding_rate_pct"] = f"{funding_status or 'UNAVAILABLE'}_OPTIONAL"
    elif not math.isfinite(funding):
        reasons.append("FUNDING_RATE_PCT_INVALID")
        availability["funding_rate_pct"] = "INVALID"
        state = EvidenceState.INVALID
    else:
        availability["funding_rate_pct"] = "AVAILABLE"
        if abs(funding) > constraints.max_abs_funding_rate_pct:
            reasons.append("FUNDING_ANOMALY")
            if state is EvidenceState.ELIGIBLE:
                state = EvidenceState.INELIGIBLE

    market_cap_required = constraints.min_market_cap_usd is not None or constraints.max_market_cap_usd is not None
    market_cap_raw = item.get("market_cap_usd")
    market_cap = _finite_nonnegative(market_cap_raw)
    if market_cap_required:
        cap_observed_at = _timestamp_seconds(item.get("market_cap_observed_at"))
        cap_source = str(item.get("market_cap_source") or "").strip()
        cap_status = str(item.get("market_cap_status") or ("MEASURED" if market_cap_raw is not None else "UNAVAILABLE")).upper()
        if market_cap_raw is None or cap_observed_at is None or not cap_source or cap_status in {"UNAVAILABLE", "UNKNOWN"}:
            reasons.append("MARKET_CAP_UNAVAILABLE")
            availability["market_cap_usd"] = "UNAVAILABLE"
            if state not in {EvidenceState.INVALID, EvidenceState.STALE}:
                state = EvidenceState.UNAVAILABLE
        elif market_cap is None:
            reasons.append("MARKET_CAP_INVALID")
            availability["market_cap_usd"] = "INVALID"
            state = EvidenceState.INVALID
        elif cap_observed_at > decision_ts + 1.0:
            reasons.append("MARKET_CAP_INVALID_FUTURE_TIMESTAMP")
            availability["market_cap_usd"] = "INVALID"
            state = EvidenceState.INVALID
        elif decision_ts - cap_observed_at > constraints.max_evidence_age_sec:
            reasons.append("MARKET_CAP_STALE")
            availability["market_cap_usd"] = "STALE"
            state = EvidenceState.STALE
        else:
            availability["market_cap_usd"] = "AVAILABLE"
            if constraints.min_market_cap_usd is not None and market_cap < constraints.min_market_cap_usd:
                reasons.append("BELOW_MIN_MARKET_CAP")
                if state is EvidenceState.ELIGIBLE:
                    state = EvidenceState.INELIGIBLE
            if constraints.max_market_cap_usd is not None and market_cap > constraints.max_market_cap_usd:
                reasons.append("ABOVE_MAX_MARKET_CAP")
                if state is EvidenceState.ELIGIBLE:
                    state = EvidenceState.INELIGIBLE
    else:
        availability["market_cap_usd"] = "NOT_REQUIRED" if market_cap_raw is None else ("AVAILABLE" if market_cap is not None else "INVALID_OPTIONAL")

    inputs = {
        **_json_safe(dict(item)),
        "symbol": symbol,
        "volume_24h_usd": volume,
        "spread_pct": spread,
        "volatility_pct": parsed["volatility_pct"],
        "expected_slippage_pct": slippage,
        "liquidity_score": liquidity,
        "funding_rate_pct": funding,
        "market_cap_usd": market_cap,
        "market_observed_at": observed_at,
    }
    return symbol, state, reasons, inputs, availability


def build_selected_universe(
    candidates: list[Mapping[str, Any]],
    constraints: UniverseConstraints,
    *,
    decision_timestamp: Any,
    execution_mode: str,
    git_sha: str = "UNKNOWN_GIT_COMMIT",
    strategy_config_hash: str | None = None,
) -> SelectedUniverse:
    """Canonical pure universe authority shared by every execution mode."""
    decision_ts = _timestamp_seconds(decision_timestamp)
    if decision_ts is None:
        raise ValueError("decision_timestamp must be an explicit finite timestamp")

    evaluated = [_candidate_base_decision(item, constraints, decision_ts) for item in candidates]
    eligible_by_symbol: dict[str, list[int]] = {}
    for index, row in enumerate(evaluated):
        if row[1] is EvidenceState.ELIGIBLE:
            eligible_by_symbol.setdefault(row[0], []).append(index)
    for duplicate_indices in eligible_by_symbol.values():
        if len(duplicate_indices) <= 1:
            continue
        winner = min(
            duplicate_indices,
            key=lambda index: (
                -float(evaluated[index][3]["volume_24h_usd"]),
                float(evaluated[index][3]["spread_pct"]),
                str(evaluated[index][3].get("source_exchange") or evaluated[index][3].get("market_data_source") or ""),
                _canonical_hash(evaluated[index][3]),
            ),
        )
        for index in duplicate_indices:
            if index == winner:
                continue
            symbol, _state, reasons, inputs, availability = evaluated[index]
            evaluated[index] = (
                symbol,
                EvidenceState.INELIGIBLE,
                [*reasons, "DUPLICATE_SYMBOL_LOWER_QUALITY"],
                inputs,
                availability,
            )
    eligible_indices = [index for index, row in enumerate(evaluated) if row[1] is EvidenceState.ELIGIBLE]
    liquid_order = sorted(
        eligible_indices,
        key=lambda index: (-float(evaluated[index][3]["volume_24h_usd"]), evaluated[index][0]),
    )
    retained = set(liquid_order[: constraints.candidate_pool_top_n_volume])
    normalized: list[tuple[str, EvidenceState, list[str], dict[str, Any], dict[str, str]]] = []
    for index, row in enumerate(evaluated):
        symbol, state, reasons, inputs, availability = row
        if state is EvidenceState.ELIGIBLE and index not in retained:
            state = EvidenceState.INELIGIBLE
            reasons = [*reasons, "OUTSIDE_TOP_N_VOLUME"]
        normalized.append((symbol, state, reasons, inputs, availability))

    rankable = [row for row in normalized if row[1] is EvidenceState.ELIGIBLE]
    max_volume = max((float(row[3]["volume_24h_usd"]) for row in rankable), default=1.0)
    max_volatility = max((float(row[3]["volatility_pct"]) for row in rankable), default=1.0)
    scored: list[tuple[float, str, tuple[str, EvidenceState, list[str], dict[str, Any], dict[str, str]], dict[str, float | str | None]]] = []
    for row in rankable:
        symbol, _, _, inputs, availability = row
        volume = float(inputs["volume_24h_usd"])
        spread = float(inputs["spread_pct"])
        volatility = float(inputs["volatility_pct"])
        volume_quality = 10.0 if max_volume <= 0 else 10.0 * math.log1p(volume) / math.log1p(max_volume)
        spread_quality = 10.0 if constraints.max_spread_pct == 0 and spread == 0 else max(0.0, 10.0 * (1.0 - spread / max(constraints.max_spread_pct, 1e-12)))
        volatility_quality = 0.0 if max_volatility <= 0 else 10.0 * volatility / max_volatility
        slippage = inputs.get("expected_slippage_pct")
        slippage_penalty = (1.0 if slippage is None else min(2.0, 2.0 * float(slippage) / max(constraints.max_expected_slippage_pct, 1e-12)))
        optional_uncertainty_penalty = 0.5 if slippage is None else 0.0
        liquidity = inputs.get("liquidity_score")
        liquidity_quality = 10.0 * float(liquidity) if liquidity is not None else 0.0
        if liquidity is None:
            optional_uncertainty_penalty += 0.5
        if inputs.get("funding_rate_pct") is None:
            optional_uncertainty_penalty += 0.25
        trend = _finite_nonnegative(inputs.get("trend_strength"))
        chop = _finite_nonnegative(inputs.get("chop_score"))
        regime_fit = (0.5 * min(1.0, trend) if trend is not None else 0.0) - (0.5 * min(1.0, chop) if chop is not None else 0.0)
        if trend is None or chop is None:
            optional_uncertainty_penalty += 0.25
            availability["regime_compatibility"] = "UNAVAILABLE_OPTIONAL"
        else:
            availability["regime_compatibility"] = "AVAILABLE"
        score = round(max(0.0, min(10.0, volume_quality * 0.40 + spread_quality * 0.30 + liquidity_quality * 0.15 + volatility_quality * 0.15 + regime_fit - slippage_penalty - optional_uncertainty_penalty)), 8)
        components: dict[str, float | str | None] = {
            "volume_quality": round(volume_quality, 8),
            "spread_quality": round(spread_quality, 8),
            "liquidity_quality": round(liquidity_quality, 8),
            "volatility_opportunity": round(volatility_quality, 8),
            "regime_fit": round(regime_fit, 8),
            "slippage_penalty": round(slippage_penalty, 8),
            "evidence_uncertainty_penalty": round(optional_uncertainty_penalty, 8),
            "ranking_version": RANKING_VERSION,
        }
        scored.append((score, symbol, row, components))
    scored.sort(key=lambda item: (-item[0], item[1]))

    rank_by_identity = {id(row): (rank, score, components) for rank, (score, _symbol, row, components) in enumerate(scored, start=1)}
    selected_identities = {id(row) for _score, _symbol, row, _components in scored[: constraints.max_active_symbols]}
    decisions: list[UniverseCandidateDecision] = []
    for row in normalized:
        symbol, state, reasons, inputs, availability = row
        rank_info = rank_by_identity.get(id(row))
        selected = id(row) in selected_identities
        if state is EvidenceState.ELIGIBLE and not selected:
            reasons = [*reasons, "RANKED_BELOW_MAX_ACTIVE"]
        decisions.append(UniverseCandidateDecision(
            symbol=symbol,
            state=state,
            reasons=tuple(reasons),
            observed_inputs=inputs,
            ranking_components={} if rank_info is None else rank_info[2],
            score=None if rank_info is None else rank_info[1],
            rank=None if rank_info is None else rank_info[0],
            selected=selected,
            evidence_availability=dict(sorted(availability.items())),
        ))
    decisions.sort(key=lambda item: (
        item.rank is None,
        item.rank or 0,
        item.symbol,
        item.state.value,
        str(item.observed_inputs.get("source_exchange") or item.observed_inputs.get("market_data_source") or ""),
        _canonical_hash(item.observed_inputs),
    ))
    selected_symbols = tuple(item.symbol for item in decisions if item.selected)
    evidence_payload = {
        "decision_timestamp": decision_ts,
        "constraints": constraints.as_dict(),
        "candidates": [item.as_dict() for item in decisions],
        "git_sha": str(git_sha or "UNKNOWN_GIT_COMMIT"),
        "strategy_config_hash": strategy_config_hash,
        "ranking_version": RANKING_VERSION,
        "schema_version": UNIVERSE_SCHEMA_VERSION,
    }
    evidence_hash = _canonical_hash(evidence_payload)
    mode = str(execution_mode).upper()
    cycle_id = f"universe:{mode}:{evidence_hash}"
    providers = tuple(sorted({str(item.observed_inputs.get("source_exchange") or item.observed_inputs.get("market_data_source") or "UNKNOWN") for item in decisions}))
    return SelectedUniverse(
        cycle_id=cycle_id,
        decision_timestamp=decision_ts,
        execution_mode=mode,
        constraints=constraints,
        candidates=tuple(decisions),
        selected_symbols=selected_symbols,
        config_hash=constraints.config_hash,
        strategy_config_hash=strategy_config_hash,
        universe_hash=_canonical_hash({"symbols": list(selected_symbols), "evidence_hash": evidence_hash}),
        evidence_hash=evidence_hash,
        git_sha=str(git_sha or "UNKNOWN_GIT_COMMIT"),
        source_provenance=providers,
    )


@dataclass
class SymbolSelectionResult:
    symbol: str
    tradable: bool
    symbol_score: float
    regime_hint: str
    liquidity_score: float
    volatility_score: float
    trend_score: float
    spread_score: float
    volume_score: float
    reject_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


DEFAULT_CONFIG: dict[str, Any] = {
    "min_volume_24h_usdt": 2_000_000.0,
    "max_spread_pct": 0.0025,
    "min_liquidity_score": 0.45,
    "max_volatility_pct": 8.0,
    "max_chop_score": 0.72,
    "panic_score_reject": 0.85,
    "min_trend_strength": 0.25,
    "range_edge_bonus_chop_limit": 0.55,
    "max_spoof_risk": 0.70,
    "max_fakeout_risk": 0.65,
    "max_abs_funding_rate_pct": 0.0010,
    "min_abs_orderbook_imbalance": 0.0,
    "include_rejected": False,
}


def _safe_float(data: Mapping[str, Any], key: str, default: float, diagnostics: dict[str, Any], warnings: list[str]) -> float:
    raw = data.get(key)
    if raw is None:
        diagnostics.setdefault("defaults_used", {})[key] = default
        warnings.append(f"missing_{key}")
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        diagnostics.setdefault("defaults_used", {})[key] = default
        diagnostics.setdefault("invalid_fields", {})[key] = raw
        warnings.append(f"invalid_{key}")
        return default


def select_symbol(symbol: str, market_data: dict, config: dict | None = None) -> SymbolSelectionResult:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    diagnostics: dict[str, Any] = {"inputs": dict(market_data or {})}
    warnings: list[str] = []
    reject_reasons: list[str] = []

    disabled = {str(r).upper() for r in cfg.get("disabled_backtest_filters", [])}
    advisory = {str(r).upper() for r in cfg.get("advisory_reasons", [])}
    bypassed_reject_reasons: list[str] = []
    advisory_reasons: list[str] = []

    def _append_reject(reason: str) -> None:
        normalized = str(reason).upper()
        if normalized in disabled:
            bypassed_reject_reasons.append(normalized)
        elif normalized in advisory:
            advisory_reasons.append(normalized)
            warnings.append(f"advisory_{normalized.lower()}")
        else:
            reject_reasons.append(normalized)

    for required_key in ("volume_24h_usdt", "spread_pct", "volatility_pct", "liquidity_score"):
        raw_required = market_data.get(required_key)
        if raw_required is None:
            _append_reject(f"{required_key.upper()}_UNAVAILABLE")
        elif _finite_nonnegative(raw_required) is None:
            _append_reject(f"{required_key.upper()}_INVALID")

    volume_24h_usdt = _safe_float(market_data, "volume_24h_usdt", cfg["min_volume_24h_usdt"] * 0.5, diagnostics, warnings)
    spread_pct = _safe_float(market_data, "spread_pct", cfg["max_spread_pct"] * 1.1, diagnostics, warnings)
    if spread_pct > max(cfg["max_spread_pct"] * 2.0, 0.005):
        spread_pct = spread_pct / 100.0
        diagnostics["spread_normalized_from_percent_points"] = True
    volatility_pct = _safe_float(market_data, "volatility_pct", cfg["max_volatility_pct"] * 0.7, diagnostics, warnings)
    trend_strength = _safe_float(market_data, "trend_strength", 0.2, diagnostics, warnings)
    liquidity_score_raw = _safe_float(market_data, "liquidity_score", cfg["min_liquidity_score"] * 0.9, diagnostics, warnings)
    if liquidity_score_raw > 1.0 and cfg["min_liquidity_score"] <= 1.0:
        liquidity_score_raw = liquidity_score_raw / 10.0
        diagnostics["liquidity_score_normalized_from_0_10"] = True
    recent_volume_change_pct = _safe_float(market_data, "recent_volume_change_pct", 0.0, diagnostics, warnings)
    chop_score = _safe_float(market_data, "chop_score", 0.65, diagnostics, warnings)
    panic_score = _safe_float(market_data, "panic_score", 0.0, diagnostics, warnings) if "panic_score" in market_data else 0.0
    spoof_risk = _safe_float(market_data, "spoof_risk", 0.0, diagnostics, warnings) if "spoof_risk" in market_data else 0.0
    fakeout_risk = _safe_float(market_data, "fakeout_risk", 0.25, diagnostics, warnings) if "fakeout_risk" in market_data else 0.25
    funding_rate_pct = _safe_float(market_data, "funding_rate_pct", 0.0, diagnostics, warnings) if "funding_rate_pct" in market_data else 0.0
    orderbook_imbalance = _safe_float(market_data, "orderbook_imbalance", 0.0, diagnostics, warnings) if "orderbook_imbalance" in market_data else 0.0

    if volume_24h_usdt < cfg["min_volume_24h_usdt"]:
        _append_reject("LOW_VOLUME")
    if spread_pct > cfg["max_spread_pct"]:
        _append_reject("WIDE_SPREAD")
    if liquidity_score_raw < cfg["min_liquidity_score"]:
        _append_reject("LOW_LIQUIDITY")
    if volatility_pct > cfg["max_volatility_pct"]:
        _append_reject("EXCESSIVE_VOLATILITY")
    if chop_score > cfg["max_chop_score"]:
        _append_reject("TOO_CHOPPY")
    if panic_score >= cfg["panic_score_reject"]:
        _append_reject("PANIC_CONDITIONS")
    if spoof_risk > cfg["max_spoof_risk"]:
        _append_reject("SPOOF_RISK")
    if fakeout_risk > cfg["max_fakeout_risk"]:
        _append_reject("FAKEOUT_RISK")
    if abs(funding_rate_pct) > cfg["max_abs_funding_rate_pct"]:
        _append_reject("FUNDING_ANOMALY")
    if abs(orderbook_imbalance) < cfg["min_abs_orderbook_imbalance"]:
        _append_reject("LOW_ORDERBOOK_ALIGNMENT")

    has_clean_trend = trend_strength >= cfg["min_trend_strength"] and chop_score <= cfg["max_chop_score"]
    has_range_edge = chop_score <= cfg["range_edge_bonus_chop_limit"] and abs(recent_volume_change_pct) <= 20.0
    if not has_clean_trend and not has_range_edge:
        _append_reject("WEAK_TREND_AND_NO_RANGE_EDGE")

    volume_score = max(0.0, min(10.0, (volume_24h_usdt / cfg["min_volume_24h_usdt"]) * 5.0))
    spread_ratio = spread_pct / max(cfg["max_spread_pct"], 1e-9)
    spread_score = max(0.0, min(10.0, 10.0 * (1.0 - spread_ratio)))
    liquidity_score = max(0.0, min(10.0, liquidity_score_raw * 10.0))
    volatility_score = max(0.0, min(10.0, 10.0 - max(0.0, volatility_pct - 1.0) * 1.2))
    trend_score = max(0.0, min(10.0, trend_strength * 10.0))

    if has_range_edge:
        trend_score = min(10.0, trend_score + 1.0)

    microstructure_penalty = 0.0
    if "SPOOF_RISK" in reject_reasons:
        microstructure_penalty += 1.5
    if "FAKEOUT_RISK" in reject_reasons:
        microstructure_penalty += 1.2
    if "FUNDING_ANOMALY" in reject_reasons:
        microstructure_penalty += 0.8

    symbol_score = (
        volume_score * 0.2
        + spread_score * 0.2
        + liquidity_score * 0.25
        + volatility_score * 0.15
        + trend_score * 0.2
    )
    if "TOO_CHOPPY" in {*reject_reasons, *advisory_reasons}:
        symbol_score -= 1.0
    if "PANIC_CONDITIONS" in reject_reasons:
        symbol_score -= 1.5
    symbol_score -= microstructure_penalty

    symbol_score = round(max(0.0, min(10.0, symbol_score)), 2)
    if panic_score >= cfg["panic_score_reject"]:
        regime_hint = "PANIC"
    elif has_clean_trend:
        regime_hint = "TREND"
    elif has_range_edge:
        regime_hint = "RANGE"
    else:
        regime_hint = "UNFAVORABLE"

    diagnostics.update(
        {
            "disabled_filters": sorted(disabled),
            "advisory_reasons": advisory_reasons,
            "bypassed_reject_reasons": bypassed_reject_reasons,
            "disabled_filter_bypass_count": len(bypassed_reject_reasons),
            "filter_switch_experiment_active": bool(disabled),
            "metrics": {
                "volume_24h_usdt": volume_24h_usdt,
                "spread_pct": spread_pct,
                "volatility_pct": volatility_pct,
                "trend_strength": trend_strength,
                "liquidity_score": liquidity_score_raw,
                "recent_volume_change_pct": recent_volume_change_pct,
                "chop_score": chop_score,
                "panic_score": panic_score,
                "spoof_risk": spoof_risk,
                "fakeout_risk": fakeout_risk,
                "funding_rate_pct": funding_rate_pct,
                "orderbook_imbalance": orderbook_imbalance,
            },
            "sub_scores": {
                "volume_score": round(volume_score, 2),
                "spread_score": round(spread_score, 2),
                "liquidity_score": round(liquidity_score, 2),
                "volatility_score": round(volatility_score, 2),
                "trend_score": round(trend_score, 2),
                "microstructure_penalty": round(microstructure_penalty, 2),
            },
        }
    )

    return SymbolSelectionResult(
        symbol=symbol,
        tradable=len(reject_reasons) == 0,
        symbol_score=symbol_score,
        regime_hint=regime_hint,
        liquidity_score=round(max(0.0, min(1.0, liquidity_score_raw)), 6),
        volatility_score=round(volatility_score, 2),
        trend_score=round(trend_score, 2),
        spread_score=round(spread_score, 2),
        volume_score=round(volume_score, 2),
        reject_reasons=reject_reasons,
        warnings=warnings,
        diagnostics=diagnostics,
    )


def select_symbols(candidates: list[dict], config: dict | None = None) -> list[SymbolSelectionResult]:
    """Compatibility projection over the canonical batch universe authority."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    constraints = UniverseConstraints.from_mapping(cfg)
    explicit_ts = cfg.get("selection_decision_timestamp")
    if explicit_ts is None:
        observed = [
            timestamp for timestamp in
            (_timestamp_seconds(item.get("market_observed_at", item.get("market_ts"))) for item in (candidates or []))
            if timestamp is not None
        ]
        explicit_ts = max(observed, default=0.0)
    selection = build_selected_universe(
        candidates or [], constraints,
        decision_timestamp=explicit_ts,
        execution_mode=str(cfg.get("execution_mode") or cfg.get("MODE") or "PAPER"),
        git_sha=str(cfg.get("git_sha") or "UNKNOWN_GIT_COMMIT"),
        strategy_config_hash=cfg.get("strategy_config_hash"),
    )
    return selection_results(selection, cfg)


def selection_results(
    selection: SelectedUniverse, config: Mapping[str, Any] | None = None,
) -> list[SymbolSelectionResult]:
    """Project canonical evidence into the legacy runtime result shape."""
    cfg = {**DEFAULT_CONFIG, **dict(config or {})}
    results: list[SymbolSelectionResult] = []
    for candidate in selection.candidates:
        inputs = candidate.observed_inputs
        components = candidate.ranking_components
        volume_quality = float(components.get("volume_quality") or 0.0)
        spread_quality = float(components.get("spread_quality") or 0.0)
        volatility_quality = float(components.get("volatility_opportunity") or 0.0)
        trend = _finite_nonnegative(inputs.get("trend_strength")) or 0.0
        chop = _finite_nonnegative(inputs.get("chop_score"))
        liquidity_raw = _finite_nonnegative(inputs.get("liquidity_score"))
        advisory_config = {str(reason).upper() for reason in cfg.get("advisory_reasons", ())}
        advisory_reasons: list[str] = []
        if "TOO_CHOPPY" in advisory_config and chop is not None and chop > float(cfg["max_chop_score"]):
            advisory_reasons.append("TOO_CHOPPY")
        has_clean_trend = trend >= float(cfg["min_trend_strength"]) and (chop is None or chop <= float(cfg["max_chop_score"]))
        raw_volume_change = inputs.get("recent_volume_change_pct")
        try:
            recent_volume_change = abs(float(raw_volume_change)) if raw_volume_change is not None else 0.0
        except (TypeError, ValueError):
            recent_volume_change = math.inf
        has_range_edge = chop is not None and chop <= float(cfg["range_edge_bonus_chop_limit"]) and recent_volume_change <= 20.0
        if "WEAK_TREND_AND_NO_RANGE_EDGE" in advisory_config and not has_clean_trend and not has_range_edge:
            advisory_reasons.append("WEAK_TREND_AND_NO_RANGE_EDGE")
        unavailable_warnings = [f"{key.lower()}_{state.lower()}" for key, state in candidate.evidence_availability.items() if "UNAVAILABLE" in state]
        results.append(SymbolSelectionResult(
            symbol=candidate.symbol,
            tradable=candidate.state is EvidenceState.ELIGIBLE,
            symbol_score=float(candidate.score or 0.0),
            regime_hint=str(inputs.get("regime") or inputs.get("volatility_regime") or "UNKNOWN").upper(),
            liquidity_score=min(1.0, liquidity_raw) if liquidity_raw is not None else min(1.0, volume_quality / 10.0),
            volatility_score=round(volatility_quality, 2),
            trend_score=round(min(10.0, trend * 10.0), 2),
            spread_score=round(spread_quality, 2),
            volume_score=round(volume_quality, 2),
            reject_reasons=list(candidate.reasons),
            warnings=[*unavailable_warnings, *(f"advisory_{reason.lower()}" for reason in advisory_reasons)],
            diagnostics={
                "inputs": dict(inputs),
                "metrics": dict(inputs),
                "sub_scores": dict(components),
                "eligibility_state": candidate.state.value,
                "evidence_availability": dict(candidate.evidence_availability),
                "rank": candidate.rank,
                "selected": candidate.selected,
                "cycle_id": selection.cycle_id,
                "universe_hash": selection.universe_hash,
                "ranking_version": selection.ranking_version,
                "advisory_reasons": advisory_reasons,
            },
        ))
    if not cfg.get("include_rejected", False):
        results = [result for result in results if result.tradable]
    return results
