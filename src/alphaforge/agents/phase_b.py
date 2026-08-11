"""Deterministic, read-only Phase-B adapters.

The adapters consume only the immutable shadow snapshot.  They deliberately do
not own an exchange, a persistence session, or an execution adapter.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from alphaforge.order import OrderCandidate, evaluate_trade_quality

from .contracts import (AgentStage, DecisionEnvelope, DecisionStatus, StageInput,
                        stable_hash, utc_now_iso)

VERSION = "phase-b-1"
_REGIMES = {"TRENDING", "MEAN_REVERTING", "CHOPPY", "PANIC", "LOW_LIQUIDITY",
            "BREAKOUT", "SHORT_SQUEEZE", "RANGE_COMPRESSION", "NEWS_DRIVEN"}


def _number(value: Any) -> float | None:
    if value in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flat(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Merge nested observational contexts without manufacturing defaults."""
    result = dict(payload)
    for name in ("market_ctx", "market_context", "execution_ctx", "signal", "signal_payload"):
        nested = payload.get(name)
        if isinstance(nested, Mapping):
            for key, value in nested.items():
                if key not in result or result[key] in (None, ""):
                    result[key] = value
    return result


def _envelope(value: StageInput, status: DecisionStatus, reason: str,
              evidence: Mapping[str, Any], reasons: list[str] | None = None) -> DecisionEnvelope:
    now = utc_now_iso()
    return DecisionEnvelope(
        value.decision_id, value.correlation_id, value.symbol, value.execution_mode,
        value.stage, status, reason, tuple(reasons or ([reason] if reason else [])),
        evidence, stable_hash(value.payload), stable_hash({"phase": "B", "shadow": True}),
        VERSION, now, now, 0.0, value.retry_count,
    )


def _age_seconds(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        if isinstance(raw, str) and not raw.replace(".", "", 1).isdigit():
            observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            stamp = float(raw)
            if stamp > 10_000_000_000:
                stamp /= 1000.0
            observed = datetime.fromtimestamp(stamp, timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())
    except (TypeError, ValueError, OSError):
        return None


class MarketAgent:
    def __init__(self, *, stale_after_seconds: float = 300.0) -> None:
        self.stale_after_seconds = stale_after_seconds

    def run(self, value: StageInput) -> DecisionEnvelope:
        data = _flat(value.payload)
        timestamp = data.get("market_timestamp", data.get("timestamp"))
        age = _age_seconds(timestamp)
        volatility = _number(data.get("atr_pct", data.get("volatility")))
        trend_strength = _number(data.get("trend_strength", data.get("adx")))
        liquidity = _number(data.get("liquidity_score", data.get("liquidity_quality")))
        raw_regime = str(data.get("regime", "") or "").upper()
        aliases = {"TREND": "TRENDING", "RANGE": "MEAN_REVERTING", "CHOP": "CHOPPY"}
        regime = aliases.get(raw_regime, raw_regime if raw_regime in _REGIMES else None)
        unsupported_regime = None
        if raw_regime and regime is None:
            unsupported_regime = raw_regime
        metrics = {
            "volatility": volatility, "trend_strength": trend_strength,
            "spread": _number(data.get("spread_pct")),
            "expected_slippage": _number(data.get("expected_slippage_pct")),
            "liquidity": liquidity, "funding": _number(data.get("funding_rate_pct", data.get("funding_rate"))),
            "volume_24h": _number(data.get("volume_24h_usdt", data.get("volume_24h"))),
            "orderbook_imbalance": _number(data.get("orderbook_imbalance")),
            "absorption_score": _number(data.get("absorption_score")),
            "spoof_risk": _number(data.get("spoof_risk")),
        }
        availability = {key: metric is not None for key, metric in metrics.items()}
        evidence = {"symbol": value.symbol or data.get("symbol"), "interval": data.get("interval", data.get("timeframe")),
                    "timestamp": timestamp, "freshness_seconds": age, "trend_direction": data.get("trend_direction"),
                    "volatility_regime": data.get("volatility_regime"), "regime": regime,
                    **metrics, "availability": availability,
                    # AlphaForge has deterministic volatility/trend feature builders,
                    # but no pure canonical classifier for the issue-309 vocabulary.
                    # Therefore this stage observes/normalizes supplied provenance;
                    # it does not infer a regime from thresholds of its own.
                    "regime_source": "OBSERVED_NORMALIZED" if regime else "UNAVAILABLE",
                    "regime_classification_supported": False,
                    "unsupported_regime": unsupported_regime,
                    "proxy_fields": list(data.get("proxy_fields", [])) if value.execution_mode == "BACKTEST" else []}
        if timestamp is not None and age is None:
            return _envelope(value, DecisionStatus.REJECT, "INVALID_MARKET_TIMESTAMP", evidence)
        if age is not None and age > self.stale_after_seconds:
            return _envelope(value, DecisionStatus.DEFER, "STALE_MARKET_DATA", evidence)
        if not value.symbol and not data.get("symbol"):
            return _envelope(value, DecisionStatus.REJECT, "INVALID_MARKET_SNAPSHOT", evidence)
        reason = "MARKET_CONTEXT_OBSERVED" if any(availability.values()) or regime else "MARKET_CONTEXT_INCOMPLETE"
        return _envelope(value, DecisionStatus.PASS if reason.endswith("OBSERVED") else DecisionStatus.DEFER, reason, evidence)


class SignalAgent:
    def run(self, value: StageInput) -> DecisionEnvelope:
        data = _flat(value.payload)
        entry = _number(data.get("entry", data.get("entry_price")))
        sl = _number(data.get("sl", data.get("stop_loss")))
        tp = _number(data.get("tp", data.get("take_profit")))
        side = str(data.get("side", "") or "").upper() or None
        canonical_components = data.get("score_components", data.get("components"))
        components = ({str(name): float(number) for name, number in canonical_components.items()
                       if _number(number) is not None}
                      if isinstance(canonical_components, Mapping) else {})
        # The immutable runtime snapshot contains the score already calculated
        # by AIBrain. Recomputing it here would create a second scoring model.
        score = _number(data.get("score", data.get("total_score")))
        score_source = "CANONICAL_SNAPSHOT" if score is not None else "UNAVAILABLE"
        raw_rr = None
        geometry_reason = None
        if None not in (entry, sl, tp) and entry and side in {"LONG", "SHORT"}:
            risk = entry - sl if side == "LONG" else sl - entry
            reward = tp - entry if side == "LONG" else entry - tp
            if risk > 0 and reward > 0:
                raw_rr = round(reward / risk, 10)
            else:
                geometry_reason = "INVALID_SIGNAL_GEOMETRY"
        elif any(item is not None for item in (entry, sl, tp)):
            geometry_reason = "INCOMPLETE_SIGNAL_GEOMETRY"
        evidence = {"lifecycle_state": "SIGNAL_CREATED" if side and not geometry_reason else "SIGNAL_REJECTED",
                    "signal_side": side, "setup_type": data.get("setup_type", data.get("setup")),
                    "score": score, "score_components": components, "score_source": score_source,
                    "score_components_available": bool(components), "raw_rr": raw_rr,
                    "entry": entry, "sl": sl, "tp": tp,
                    "regime_compatibility_claim": data.get("regime_compatible"),
                    "no_signal_reason": geometry_reason, "source": "deterministic_observed_components"}
        if geometry_reason:
            return _envelope(value, DecisionStatus.REJECT, geometry_reason, evidence)
        if not side or entry is None or sl is None or tp is None:
            evidence["no_signal_reason"] = "NO_COMPLETE_SIGNAL_CANDIDATE"
            return _envelope(value, DecisionStatus.DEFER, "NO_COMPLETE_SIGNAL_CANDIDATE", evidence)
        if score is None or not components:
            return _envelope(value, DecisionStatus.DEFER, "SIGNAL_SCORE_COMPONENTS_UNAVAILABLE", evidence)
        return _envelope(value, DecisionStatus.PASS, "SIGNAL_CANDIDATE_GENERATED", evidence)


class QualityAgent:
    def run(self, value: StageInput) -> DecisionEnvelope:
        data = _flat(value.payload)
        signal = next((result.evidence for result in reversed(value.prior_results)
                       if result.stage is AgentStage.SIGNAL), {})
        score = _number(signal.get("score"))
        rr = _number(signal.get("raw_rr"))
        legacy_decision = str(data.get("decision", "") or "").upper()
        legacy_reason = str(data.get("reject_reason", data.get("reason", "")) or "").upper() or None
        unavailable = []
        if score is None: unavailable.append("score")
        if rr is None: unavailable.append("raw_rr")
        for field in ("spread_pct", "expected_slippage_pct", "liquidity_score"):
            if _number(data.get(field)) is None: unavailable.append(field)
        reasons: list[str] = []
        quality_score = None
        diagnostics: Mapping[str, Any] = {}
        if score is not None and rr is not None and all(_number(signal.get(key)) is not None for key in ("entry", "sl", "tp")):
            candidate = OrderCandidate(str(value.symbol or data.get("symbol") or "UNKNOWN"),
                str(signal.get("signal_side")), str(signal.get("setup_type") or "GENERIC"), "SHADOW_OBSERVATION",
                str(data.get("regime") or "UNKNOWN"), score, rr, _number(data.get("expectancy")),
                float(signal["entry"]), float(signal["sl"]), float(signal["tp"]), "SHADOW_ONLY")
            market = dict(data)
            effective_rr = _number(data.get("effective_rr"))
            market["effective_rr"] = rr if effective_rr is None else effective_rr
            decision = evaluate_trade_quality(candidate, market, data.get("recent_stats", {}) if isinstance(data.get("recent_stats"), Mapping) else {},
                                              {"MODE": value.execution_mode})
            quality_score, diagnostics = decision.quality_score, decision.diagnostics
            reasons = [str(reason).upper() for reason in diagnostics.get("all_failed_gates", [])]
            if decision.reject_reason:
                reasons.insert(0, decision.reject_reason.upper())
        if legacy_decision in {"REJECT", "REJECTED"} and legacy_reason and legacy_reason not in reasons:
            reasons.append(legacy_reason)
        reasons = list(dict.fromkeys(reasons))
        executed_checks = []
        if score is not None: executed_checks.append("score_integrity")
        if all(_number(signal.get(key)) is not None for key in ("entry", "sl", "tp")):
            executed_checks.append("geometry")
        if rr is not None: executed_checks.append("raw_rr")
        if _number(data.get("spread_pct")) is not None: executed_checks.append("spread")
        if _number(data.get("expected_slippage_pct")) is not None: executed_checks.append("slippage")
        if _number(data.get("liquidity_score")) is not None: executed_checks.append("liquidity_availability")
        execution_missing = any(field in unavailable for field in
                                ("spread_pct", "expected_slippage_pct", "liquidity_score"))
        graph_status = "REJECT" if reasons else ("DEFER" if unavailable else "PASS")
        if not legacy_decision or graph_status == "DEFER": parity = "UNAVAILABLE"
        elif (legacy_decision in {"REJECT", "REJECTED"}) == (graph_status == "REJECT"):
            parity = "MATCH" if not legacy_reason or legacy_reason in reasons else "PARTIAL_MATCH"
        else: parity = "MISMATCH"
        primary = reasons[0] if reasons else ("QUALITY_PREREQUISITES_UNAVAILABLE" if unavailable else "QUALITY_CHECKS_PASS")
        evidence = {"quality_score": quality_score, "expectancy_bucket": data.get("expectancy_bucket"),
                    "primary_reject_reason": reasons[0] if reasons else None, "all_reject_reasons": reasons,
                    "unavailable_checks": unavailable, "quality_diagnostics": diagnostics,
                    "checks_executed": executed_checks,
                    "execution_context_complete": not execution_missing,
                    "execution_quality_status": "UNAVAILABLE" if execution_missing else "EVALUATED",
                    "legacy_decision": legacy_decision or None, "legacy_primary_reject_reason": legacy_reason,
                    "graph_quality_status": graph_status, "reason_code_overlap": bool(legacy_reason and legacy_reason in reasons),
                    "score_difference": (round(score - float(data["score"]), 10) if score is not None and _number(data.get("score")) is not None else None),
                    "rr_difference": (round(rr - float(data["rr"]), 10) if rr is not None and _number(data.get("rr")) is not None else None),
                    "parity_status": parity, "shadow_only": True}
        status = DecisionStatus(graph_status)
        return _envelope(value, status, primary, evidence, reasons or [primary])


def register_phase_b_handlers(orchestrator: Any) -> None:
    orchestrator.register_handler(AgentStage.MARKET, MarketAgent())
    orchestrator.register_handler(AgentStage.SIGNAL, SignalAgent())
    orchestrator.register_handler(AgentStage.QUALITY, QualityAgent())
