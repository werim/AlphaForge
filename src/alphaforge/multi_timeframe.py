"""Fail-closed, closed-candle multi-timeframe evidence for the PAPER runtime."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib import parse, request

MTF_REJECT_REASONS = (
    "MTF_REGIME_UNAVAILABLE", "MTF_SETUP_UNAVAILABLE", "MTF_EXECUTION_UNAVAILABLE",
    "MTF_CONTEXT_STALE", "MTF_REGIME_SETUP_MISMATCH", "MTF_SETUP_EXECUTION_MISMATCH",
    "MTF_DIRECTION_MISMATCH", "MTF_NO_VALID_SETUP", "MTF_EXECUTION_NOT_CONFIRMED",
)

_TF_SECONDS = {"1m": 60, "15m": 900, "1h": 3600}


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


def _direction(candles: list[dict[str, Any]], fast: int, slow: int) -> tuple[str, float | None]:
    if len(candles) < slow:
        return "UNKNOWN", None
    closes = [float(c["close"]) for c in candles]
    fast_ma, slow_ma = sum(closes[-fast:]) / fast, sum(closes[-slow:]) / slow
    delta = (fast_ma - slow_ma) / slow_ma if slow_ma else 0.0
    if abs(delta) < 0.0005:
        return "NEUTRAL", abs(delta)
    return ("LONG" if delta > 0 else "SHORT"), abs(delta)


def build_regime_context(candles: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    direction, strength = _direction(candles, 8, 20)
    complete = direction not in {"UNKNOWN", "NEUTRAL"}
    returns = [abs(float(candles[i]["close"]) / float(candles[i-1]["close"]) - 1)
               for i in range(1, len(candles)) if candles[i-1]["close"]]
    volatility = None if not returns else sum(returns[-20:]) / min(20, len(returns))
    return {"timeframe": timeframe, "regime": "TRENDING" if complete else ("UNKNOWN" if direction == "UNKNOWN" else "CHOPPY"),
            "direction": direction, "trend_strength": strength, "volatility_regime": None if volatility is None else ("HIGH" if volatility > .02 else "MODERATE"),
            "structure_state": "MA_TREND" if complete else "UNCONFIRMED", "confidence": strength,
            "last_closed_candle_ts": _iso(int(candles[-1]["close_ts"])) if candles else None,
            "last_closed_candle_ms": int(candles[-1]["close_ts"]) if candles else None,
            "evidence_status": "COMPLETE" if complete else "INCOMPLETE"}


def build_setup_context(candles: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    direction, quality = _direction(candles, 5, 12)
    complete = direction in {"LONG", "SHORT"} and len(candles) >= 12
    last = candles[-1] if candles else None
    recent = candles[-12:] if len(candles) >= 12 else []
    span = (max(float(c["high"]) for c in recent) - min(float(c["low"]) for c in recent)) if recent else None
    overextended = None if not recent or not last or not last["close"] else span / float(last["close"]) > .08
    if overextended:
        complete = False
    return {"timeframe": timeframe, "setup_type": "TREND_PULLBACK" if complete else None,
            "direction": direction if direction != "NEUTRAL" else "NONE", "structure_quality": quality,
            "momentum_state": "CONFIRMED" if complete else "UNCONFIRMED", "overextended": overextended,
            "entry_zone": None if not last else [last["low"], last["high"]], "structural_stop": None,
            "structural_target": None, "last_closed_candle_ts": _iso(int(last["close_ts"])) if last else None,
            "last_closed_candle_ms": int(last["close_ts"]) if last else None,
            "evidence_status": "COMPLETE" if complete else "INCOMPLETE"}


def build_execution_context(candles: list[dict[str, Any]], timeframe: str, market: Mapping[str, Any]) -> dict[str, Any]:
    direction, _ = _direction(candles, 2, 5)
    last = candles[-1] if candles else None
    trigger = direction in {"LONG", "SHORT"} and len(candles) >= 5
    required = (market.get("spread_pct"), market.get("expected_slippage_pct"),
                market.get("latency_ms"), market.get("liquidity_score"))
    complete = trigger and all(v is not None for v in required)
    return {"timeframe": timeframe, "direction": direction, "trigger": "MOMENTUM_CONFIRMED" if trigger else None,
            "spread_pct": market.get("spread_pct"), "expected_slippage_pct": market.get("expected_slippage_pct"),
            "latency_ms": market.get("latency_ms"), "liquidity_score": market.get("liquidity_score"),
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
        regime = build_regime_context(values.get("regime", []), regime_timeframe)
        setup = build_setup_context(values.get("setup", []), setup_timeframe)
        # Runtime's canonical execution builder owns normalization and modelling.
        # Do not re-read raw scanner aliases or manufacture a second cost model.
        execution = build_execution_context(values.get("execution", []), execution_timeframe, execution_ctx)
        alignment = evaluate_mtf_alignment(regime, setup, execution, decision_ts_ms=decision_ts_ms)
        return {"regime": regime, "setup": setup, "execution": execution, "alignment": alignment,
                "decision_timestamp": _iso(decision_ts_ms), "provider": "BINANCE_FUTURES_CLOSED_KLINES"}
