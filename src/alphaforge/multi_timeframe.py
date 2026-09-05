"""Fail-closed, closed-candle multi-timeframe evidence for the PAPER runtime."""
from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib import parse, request

from alphaforge.signal_geometry import build_regime_guided_geometry_with_diagnostics

MTF_REJECT_REASONS = (
    "MTF_REGIME_UNAVAILABLE", "MTF_SETUP_UNAVAILABLE", "MTF_EXECUTION_UNAVAILABLE",
    "MTF_CONTEXT_STALE", "MTF_REGIME_SETUP_MISMATCH", "MTF_SETUP_EXECUTION_MISMATCH",
    "MTF_DIRECTION_MISMATCH", "MTF_NO_VALID_SETUP", "MTF_EXECUTION_NOT_CONFIRMED",
    "MTF_EXECUTION_COUNTER_REGIME", "MTF_GUIDED_SETUP_SIDE_MISMATCH",
    "MTF_GUIDED_GEOMETRY_UNAVAILABLE",
)

REGIME_GUIDED_SETUP_PHASES = ("CONTINUATION", "PULLBACK", "REENTRY_READY")

_TF_SECONDS = {"1m": 60, "15m": 900, "1h": 3600}
DEFAULT_DIRECTION_THRESHOLD = 0.0005
DEFAULT_SETUP_DIRECTION_THRESHOLD = 0.0003


def _valid_ohlc(candles: list[dict[str, Any]], minimum_rows: int) -> bool:
    """Reject malformed/non-finite MTF evidence before deriving direction."""
    if len(candles) < minimum_rows:
        return False
    try:
        for candle in candles:
            required = tuple(float(candle[name]) for name in ("high", "low", "close"))
            optional_open = (() if "open" not in candle else (float(candle["open"]),))
            values = (*required, *optional_open)
            if not all(math.isfinite(value) and value > 0.0 for value in values):
                return False
            if float(candle["low"]) > float(candle["high"]):
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def closed_candles(rows: Any, *, timeframe: str, decision_ts_ms: int) -> list[dict[str, float | int]]:
    """Normalize only candles whose provider close time is at/before the decision instant."""
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        try:
            candle = {"open_ts": int(row[0]), "open": float(row[1]), "high": float(row[2]),
                      "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
                      "close_ts": int(row[6])}
        except (TypeError, ValueError):
            continue
        if candle["close_ts"] <= decision_ts_ms:
            out.append(candle)
    return sorted(out, key=lambda c: c["open_ts"])


def _direction(candles: list[dict[str, Any]], fast: int, slow: int, *,
               neutral_threshold: float = 0.0005) -> tuple[str, float | None]:
    if len(candles) < slow:
        return "UNKNOWN", None
    closes = [float(c["close"]) for c in candles]
    fast_ma, slow_ma = sum(closes[-fast:]) / fast, sum(closes[-slow:]) / slow
    delta = (fast_ma - slow_ma) / slow_ma if slow_ma else 0.0
    if abs(delta) < neutral_threshold:
        return "NEUTRAL", abs(delta)
    return ("LONG" if delta > 0 else "SHORT"), abs(delta)


def build_regime_context(candles: list[dict[str, Any]], timeframe: str, *, direction_threshold: float = DEFAULT_DIRECTION_THRESHOLD) -> dict[str, Any]:
    threshold = float(direction_threshold)
    valid_ohlc = _valid_ohlc(candles, 20)
    direction, strength = (_direction(candles, 8, 20, neutral_threshold=threshold)
                           if valid_ohlc else ("UNKNOWN", None))
    complete = direction not in {"UNKNOWN", "NEUTRAL"}
    returns = ([abs(float(candles[i]["close"]) / float(candles[i-1]["close"]) - 1)
                for i in range(1, len(candles)) if candles[i-1]["close"]]
               if valid_ohlc else [])
    volatility = None if not returns else sum(returns[-20:]) / min(20, len(returns))
    return {"timeframe": timeframe, "regime": "TRENDING" if complete else ("UNKNOWN" if direction == "UNKNOWN" else "CHOPPY"),
            "direction": direction, "trend_strength": strength, "ma_delta_strength": strength,
            "direction_threshold": threshold, "volatility_regime": None if volatility is None else ("HIGH" if volatility > .02 else "MODERATE"),
            "structure_state": "MA_TREND" if complete else "UNCONFIRMED", "confidence": strength,
            "last_closed_candle_ts": _iso(int(candles[-1]["close_ts"])) if candles else None,
            "last_closed_candle_ms": int(candles[-1]["close_ts"]) if candles else None,
            "evidence_status": "COMPLETE" if complete else "INCOMPLETE"}


def build_setup_context(candles: list[dict[str, Any]], timeframe: str, *,
                        regime: Mapping[str, Any] | None = None,
                        direction_threshold: float = DEFAULT_SETUP_DIRECTION_THRESHOLD) -> dict[str, Any]:
    threshold = float(direction_threshold)
    valid_ohlc = _valid_ohlc(candles, 12)
    direction, quality = (_direction(candles, 5, 12, neutral_threshold=threshold)
                          if valid_ohlc else ("UNKNOWN", None))
    complete = direction in {"LONG", "SHORT"} and len(candles) >= 12
    identity_last = (candles[-1] if candles
                     and isinstance(candles[-1].get("close_ts"), int) else None)
    last = candles[-1] if valid_ohlc else None
    recent = candles[-12:] if valid_ohlc else []
    span = (max(float(c["high"]) for c in recent) - min(float(c["low"]) for c in recent)) if recent else None
    overextended = None if not recent or not last or not last["close"] else span / float(last["close"]) > .08
    if regime is None:
        if overextended:
            complete = False
        return {"timeframe": timeframe, "setup_type": "TREND_PULLBACK" if complete else None,
            "direction": direction if direction != "NEUTRAL" else "NONE", "structure_quality": quality,
            "ma_delta_strength": quality, "direction_threshold": threshold,
            "momentum_state": "CONFIRMED" if complete else "UNCONFIRMED", "overextended": overextended,
            "entry_zone": None if not last else [last["low"], last["high"]], "structural_stop": None,
            "structural_target": None, "last_closed_candle_ts": _iso(int(identity_last["close_ts"])) if identity_last else None,
            "last_closed_candle_ms": int(identity_last["close_ts"]) if identity_last else None,
            "evidence_status": "COMPLETE" if complete else "INCOMPLETE"}

    regime_direction = str(regime.get("direction") or "UNKNOWN").upper()
    observed_direction = direction if direction != "NEUTRAL" else "NONE"
    recent_direction, recent_strength = (_direction(candles, 2, 5, neutral_threshold=threshold)
                                         if valid_ohlc else ("UNKNOWN", None))
    evidence_complete = (valid_ohlc and direction != "UNKNOWN"
                         and regime_direction in {"LONG", "SHORT"})
    if not evidence_complete:
        phase, setup_type = "INVALID", None
    elif overextended:
        phase, setup_type = "OVEREXTENDED", None
    elif observed_direction == "NONE":
        phase, setup_type = "NO_SETUP", None
    elif observed_direction == regime_direction:
        phase, setup_type = "CONTINUATION", f"{regime_direction}_CONTINUATION"
    elif recent_direction == regime_direction:
        phase, setup_type = "REENTRY_READY", f"{regime_direction}_REENTRY"
    else:
        phase, setup_type = "PULLBACK", f"{regime_direction}_PULLBACK"
    return {"timeframe": timeframe, "setup_type": setup_type,
            # ``direction`` is retained as observed 15m MA direction for old
            # exports. It is diagnostic and no longer selects trade side.
            "direction": observed_direction, "observed_direction": observed_direction,
            "trade_side": regime_direction if regime_direction in {"LONG", "SHORT"} else None,
            "regime_direction": regime_direction, "phase": phase,
            "generation_mode": "REGIME_GUIDED",
            "candidate_ready": phase in REGIME_GUIDED_SETUP_PHASES,
            "structure_quality": quality, "recent_direction": recent_direction,
            "recent_direction_strength": recent_strength,
            "ma_delta_strength": quality, "direction_threshold": threshold,
            "momentum_state": ("COUNTER_REGIME_PULLBACK" if phase == "PULLBACK"
                               else "REGIME_ALIGNED" if phase in {"CONTINUATION", "REENTRY_READY"}
                               else "UNCONFIRMED"),
            "overextended": overextended,
            "entry_zone": None if not last else [last["low"], last["high"]],
            "structural_stop": None, "structural_target": None,
            "last_closed_candle_ts": _iso(int(identity_last["close_ts"])) if identity_last else None,
            "last_closed_candle_ms": int(identity_last["close_ts"]) if identity_last else None,
            "evidence_status": "COMPLETE" if evidence_complete else "INCOMPLETE"}


def build_execution_context(candles: list[dict[str, Any]], timeframe: str, market: Mapping[str, Any], *,
                            trade_side: str | None = None,
                            direction_threshold: float = DEFAULT_DIRECTION_THRESHOLD) -> dict[str, Any]:
    threshold = float(direction_threshold)
    valid_ohlc = _valid_ohlc(candles, 5)
    direction, strength = (_direction(candles, 2, 5, neutral_threshold=threshold)
                           if valid_ohlc else ("UNKNOWN", None))
    last = candles[-1] if valid_ohlc else None
    trigger = direction in {"LONG", "SHORT"} and len(candles) >= 5
    # MTF execution confirmation is based on market evidence available before
    # an order exists. Public market-data latency belongs here; order execution
    # latency is a separate pre-submit/cost-model concern.
    required = (
        market.get("spread_pct"),
        market.get("expected_slippage_pct"),
        market.get("market_data_latency_ms"),
        market.get("liquidity_score"),
    )
    # Evidence availability and trigger confirmation are deliberately independent:
    # a valid neutral observation is evidence, but can never authorize an entry.
    market_complete = all(isinstance(v, (int, float)) and not isinstance(v, bool)
                          and math.isfinite(float(v)) and float(v) >= 0 for v in required)
    complete = valid_ohlc and direction != "UNKNOWN" and market_complete
    normalized_side = str(trade_side or "").upper()
    confirmed_for_side = trigger and (not normalized_side or direction == normalized_side)
    side_confirmation = ("CONFIRMED" if normalized_side and direction == normalized_side and trigger
                         else "COUNTER_REGIME" if normalized_side and direction in {"LONG", "SHORT"}
                         else "UNCONFIRMED")
    return {"timeframe": timeframe, "direction": direction, "trigger": "MOMENTUM_CONFIRMED" if trigger else None,
            "trade_side": normalized_side or None, "confirmed_for_side": confirmed_for_side,
            "side_confirmation": side_confirmation,
            "ma_delta_strength": strength, "direction_threshold": threshold,
            "spread_pct": market.get("spread_pct"),
            "expected_slippage_pct": market.get("expected_slippage_pct"),
            "market_data_latency_ms": market.get("market_data_latency_ms"),
            "latency_ms": market.get("latency_ms"),
            "liquidity_score": market.get("liquidity_score"),
            "orderbook_imbalance": market.get("orderbook_imbalance"), "execution_regime": market.get("volatility_regime"),
            "effective_rr": market.get("effective_rr"), "last_closed_candle_ts": _iso(int(last["close_ts"])) if last else None,
            "last_closed_candle_ms": int(last["close_ts"]) if last else None,
            "evidence_status": "COMPLETE" if complete else "INCOMPLETE"}


def evaluate_mtf_alignment(regime: Mapping[str, Any] | None, setup: Mapping[str, Any] | None,
                           execution: Mapping[str, Any] | None, *, decision_ts_ms: int,
                           max_age_intervals: int = 2) -> dict[str, Any]:
    contexts = (("regime", regime, "MTF_REGIME_UNAVAILABLE"), ("setup", setup, "MTF_SETUP_UNAVAILABLE"),
                ("execution", execution, "MTF_EXECUTION_UNAVAILABLE"))
    reasons: list[str] = []
    for _, ctx, missing_reason in contexts:
        if not isinstance(ctx, Mapping) or ctx.get("evidence_status") != "COMPLETE":
            reasons.append(missing_reason)
            continue
        tf, close_ms = str(ctx.get("timeframe") or ""), ctx.get("last_closed_candle_ms")
        if tf not in _TF_SECONDS or not isinstance(close_ms, int) or close_ms > decision_ts_ms:
            reasons.append("MTF_CONTEXT_STALE")
        elif decision_ts_ms - close_ms > max_age_intervals * _TF_SECONDS[tf] * 1000:
            reasons.append("MTF_CONTEXT_STALE")
    rd = None if not regime else regime.get("direction")
    sd = None if not setup else setup.get("direction")
    ed = None if not execution else execution.get("direction")
    guided = bool(setup and (setup.get("generation_mode") == "REGIME_GUIDED" or setup.get("phase")))
    if guided:
        phase = str((setup or {}).get("phase") or "INVALID").upper()
        setup_side = (setup or {}).get("trade_side")
        if phase not in REGIME_GUIDED_SETUP_PHASES:
            reasons.append("MTF_NO_VALID_SETUP")
        if rd in {"LONG", "SHORT"} and setup_side != rd:
            reasons.append("MTF_GUIDED_SETUP_SIDE_MISMATCH")
        if execution and not execution.get("trigger"):
            reasons.append("MTF_EXECUTION_NOT_CONFIRMED")
        elif rd in {"LONG", "SHORT"} and ed in {"LONG", "SHORT"} and rd != ed:
            reasons.append("MTF_EXECUTION_COUNTER_REGIME")
        elif execution and "confirmed_for_side" in execution and not execution.get("confirmed_for_side"):
            reasons.append("MTF_EXECUTION_NOT_CONFIRMED")
        execution_confirmed = bool(execution and (
            execution.get("confirmed_for_side")
            if "confirmed_for_side" in execution
            else execution.get("trigger") and ed == rd
        ))
        valid = (rd in {"LONG", "SHORT"} and setup_side == rd
                 and phase in REGIME_GUIDED_SETUP_PHASES and execution_confirmed)
        reasons = list(dict.fromkeys(reasons))
        return {"aligned": not reasons and valid, "direction": rd if not reasons and valid else None,
                "generation_mode": "REGIME_GUIDED", "setup_phase": phase,
                "regime_alignment": "PASS" if not any(r in {"MTF_REGIME_UNAVAILABLE", "MTF_CONTEXT_STALE"} for r in reasons) else "FAIL",
                "setup_alignment": "PASS" if not any("SETUP" in r for r in reasons) else "FAIL",
                "execution_alignment": "PASS" if not any("EXECUTION" in r for r in reasons) else "FAIL",
                "reasons": reasons, "timeframes": {"regime": (regime or {}).get("timeframe"),
                "setup": (setup or {}).get("timeframe"), "execution": (execution or {}).get("timeframe")}}
    if sd in {"NONE", "NEUTRAL"}:
        reasons.append("MTF_NO_VALID_SETUP")
    if rd in {"LONG", "SHORT"} and sd in {"LONG", "SHORT"} and rd != sd:
        reasons.append("MTF_REGIME_SETUP_MISMATCH")
    if sd in {"LONG", "SHORT"} and ed in {"LONG", "SHORT"} and sd != ed:
        reasons.append("MTF_SETUP_EXECUTION_MISMATCH")
    if execution and not execution.get("trigger"):
        reasons.append("MTF_EXECUTION_NOT_CONFIRMED")
    valid = rd == sd == ed and rd in {"LONG", "SHORT"}
    if not valid and not reasons and all(x is not None for x in (rd, sd, ed)):
        reasons.append("MTF_DIRECTION_MISMATCH")
    reasons = list(dict.fromkeys(reasons))
    return {"aligned": not reasons and valid, "direction": rd if not reasons and valid else None,
            "regime_alignment": "PASS" if not any(r.startswith("MTF_REGIME") for r in reasons) else "FAIL",
            "setup_alignment": "PASS" if not any("SETUP" in r for r in reasons) else "FAIL",
            "execution_alignment": "PASS" if not any("EXECUTION" in r for r in reasons) else "FAIL",
            "reasons": reasons, "timeframes": {"regime": (regime or {}).get("timeframe"),
            "setup": (setup or {}).get("timeframe"), "execution": (execution or {}).get("timeframe")}}


@dataclass
class BinanceMTFProvider:
    base_url: str = "https://fapi.binance.com"
    timeout_sec: float = 2.0
    regime_direction_threshold: float = DEFAULT_DIRECTION_THRESHOLD
    setup_direction_threshold: float = DEFAULT_SETUP_DIRECTION_THRESHOLD
    execution_direction_threshold: float = DEFAULT_DIRECTION_THRESHOLD
    guided_signal_generation_enabled: bool = True
    _cache: dict[tuple[str, str, int, str], dict[str, Any]] = field(default_factory=dict)

    def _fetch(self, symbol: str, timeframe: str) -> Any:
        query = parse.urlencode({"symbol": symbol, "interval": timeframe, "limit": 64})
        with request.urlopen(f"{self.base_url.rstrip('/')}/fapi/v1/klines?{query}", timeout=self.timeout_sec) as response:
            return json.loads(response.read().decode("utf-8"))

    async def build(self, symbol: str, market: Mapping[str, Any], *, execution_ctx: Mapping[str, Any], decision_ts_ms: int,
                    regime_timeframe: str, setup_timeframe: str, execution_timeframe: str) -> dict[str, Any]:
        layers = (("regime", regime_timeframe), ("setup", setup_timeframe), ("execution", execution_timeframe))
        async def one(layer: str, tf: str) -> tuple[str, list[dict[str, Any]]]:
            boundary = (decision_ts_ms // (_TF_SECONDS[tf] * 1000)) * (_TF_SECONDS[tf] * 1000) - 1
            key = (symbol, tf, boundary, self.base_url)
            if key not in self._cache:
                rows = await asyncio.to_thread(self._fetch, symbol, tf)
                self._cache[key] = {"candles": closed_candles(rows, timeframe=tf, decision_ts_ms=decision_ts_ms), "built_at": time.time()}
            return layer, self._cache[key]["candles"]
        try:
            values = dict(await asyncio.gather(*(one(layer, tf) for layer, tf in layers)))
        except Exception:
            values = {}
        regime = build_regime_context(values.get("regime", []), regime_timeframe, direction_threshold=self.regime_direction_threshold)
        setup = build_setup_context(values.get("setup", []), setup_timeframe,
            regime=regime if self.guided_signal_generation_enabled else None,
            direction_threshold=self.setup_direction_threshold)
        # Runtime's canonical execution builder owns normalization and modelling.
        # Do not re-read raw scanner aliases or manufacture a second cost model.
        execution = build_execution_context(values.get("execution", []), execution_timeframe, execution_ctx,
            trade_side=regime.get("direction") if self.guided_signal_generation_enabled else None,
            direction_threshold=self.execution_direction_threshold)
        alignment = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=decision_ts_ms)
        generation: dict[str, Any] = {"mode": "LEGACY_VETO", "evidence_status": "NOT_APPLICABLE",
                                      "candidate": None, "reason": None}
        if self.guided_signal_generation_enabled:
            candidate: dict[str, Any] = {}
            geometry_reason: str | None = None
            if alignment.get("aligned") and len(values.get("execution", [])) >= 2:
                phase = str(setup.get("phase") or "INVALID")
                candidate, geometry_reason = build_regime_guided_geometry_with_diagnostics(
                    values["execution"][-1], values["execution"][-2],
                    side=str(regime.get("direction") or ""),
                    setup_type=str(setup.get("setup_type") or "REGIME_GUIDED"),
                    setup_phase=phase,
                )
            elif alignment.get("aligned"):
                geometry_reason = "KLINE_INSUFFICIENT_ROWS"
            generation = {"mode": "REGIME_GUIDED",
                          "evidence_status": "COMPLETE" if candidate else "INCOMPLETE",
                          "candidate": candidate or None, "reason": geometry_reason,
                          "trade_side_source": "1h_regime", "setup_phase_source": "15m_regime_guided",
                          "timing_source": "1m_execution_confirmation"}
            if alignment.get("aligned") and not candidate:
                alignment = {**alignment, "aligned": False, "direction": None,
                    "reasons": list(dict.fromkeys([*(alignment.get("reasons") or []),
                                                    "MTF_GUIDED_GEOMETRY_UNAVAILABLE"]))}
        return {"regime": regime, "setup": setup, "execution": execution,
                "alignment": alignment, "generation": generation,
                "decision_timestamp": _iso(decision_ts_ms), "provider": "BINANCE_FUTURES_CLOSED_KLINES"}
