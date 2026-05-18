from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text

logger = logging.getLogger(__name__)


def classify_expectancy_bucket(expectancy: float | None) -> str:
    if expectancy is None:
        return "UNKNOWN"
    if expectancy < 0:
        return "NEGATIVE"
    if expectancy < 0.01:
        return "LOW"
    if expectancy < 0.03:
        return "MEDIUM"
    return "HIGH"


def calculate_reject_accuracy(rejected_rows: list[Mapping[str, Any]]) -> float | None:
    labeled = [r for r in rejected_rows if r.get("reject_correct") is not None]
    if not labeled:
        return None
    correct = sum(1 for r in labeled if bool(r.get("reject_correct")))
    return correct / len(labeled)


def record_closed_trade_review(session: Any, **payload: Any) -> bool:
    fields = {
        "trade_id": None, "symbol": None, "setup_type": None, "regime": None, "side": None, "entry_price": None, "exit_price": None,
        "raw_rr": None, "effective_rr": None, "score": None, "net_pnl_pct": None, "fee_pct": None, "spread_pct": None,
        "expected_slippage_pct": None, "actual_slippage_pct": None, "liquidity_score": None, "volatility_regime": None,
        "close_reason": None, "tp_hit": None, "sl_hit": None, "hold_minutes": None,
    }
    fields.update(payload)
    try:
        session.execute(text("""
            INSERT INTO closed_trade_reviews (
                trade_id, symbol, setup_type, regime, side, entry_price, exit_price, raw_rr, effective_rr, score,
                net_pnl_pct, fee_pct, spread_pct, expected_slippage_pct, actual_slippage_pct, liquidity_score,
                volatility_regime, close_reason, tp_hit, sl_hit, hold_minutes, created_at, payload_json
            ) VALUES (
                :trade_id, :symbol, :setup_type, :regime, :side, :entry_price, :exit_price, :raw_rr, :effective_rr, :score,
                :net_pnl_pct, :fee_pct, :spread_pct, :expected_slippage_pct, :actual_slippage_pct, :liquidity_score,
                :volatility_regime, :close_reason, :tp_hit, :sl_hit, :hold_minutes, :created_at, :payload_json
            )
        """), {**fields, "created_at": _now(), "payload_json": _dump(payload.get("payload_json", payload))})
        return True
    except Exception as exc:
        logger.warning("record_closed_trade_review_failed: %s", exc)
        return False


def record_rejected_signal_review(session: Any, **payload: Any) -> bool:
    fields = {
        "signal_id": None, "symbol": None, "setup_type": None, "regime": None, "side": None, "reject_reason": None, "score": None,
        "raw_rr": None, "effective_rr": None, "expectancy_bucket": None, "volume_24h_usdt": None, "spread_pct": None,
        "expected_slippage_pct": None, "funding_rate_pct": None, "liquidity_score": None, "volatility_regime": None,
        "forward_window_bars": None, "would_have_hit_tp": None, "would_have_hit_sl": None, "max_favorable_excursion_pct": None,
        "max_adverse_excursion_pct": None, "reject_correct": None,
    }
    fields.update(payload)
    try:
        session.execute(text("""
            INSERT INTO rejected_signal_reviews (
                signal_id, symbol, setup_type, regime, side, reject_reason, score, raw_rr, effective_rr, expectancy_bucket,
                volume_24h_usdt, spread_pct, expected_slippage_pct, funding_rate_pct, liquidity_score, volatility_regime,
                forward_window_bars, would_have_hit_tp, would_have_hit_sl, max_favorable_excursion_pct,
                max_adverse_excursion_pct, reject_correct, created_at, payload_json
            ) VALUES (
                :signal_id, :symbol, :setup_type, :regime, :side, :reject_reason, :score, :raw_rr, :effective_rr, :expectancy_bucket,
                :volume_24h_usdt, :spread_pct, :expected_slippage_pct, :funding_rate_pct, :liquidity_score, :volatility_regime,
                :forward_window_bars, :would_have_hit_tp, :would_have_hit_sl, :max_favorable_excursion_pct,
                :max_adverse_excursion_pct, :reject_correct, :created_at, :payload_json
            )
        """), {**fields, "created_at": _now(), "payload_json": _dump(payload.get("payload_json", payload))})
        return True
    except Exception as exc:
        logger.warning("record_rejected_signal_review_failed: %s", exc)
        return False


def update_adaptive_stats(session: Any, scope_type: str, scope_key: str) -> bool:
    try:
        row = session.execute(text("""
            SELECT
              COUNT(*) AS sample_size,
              AVG(CASE WHEN net_pnl_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
              AVG(net_pnl_pct) AS avg_net_pnl_pct,
              AVG(effective_rr) AS avg_effective_rr,
              AVG(spread_pct) AS avg_spread_pct,
              AVG(COALESCE(actual_slippage_pct, expected_slippage_pct)) AS avg_slippage_pct
            FROM closed_trade_reviews
            WHERE (:scope_type='GLOBAL')
               OR (:scope_type='SYMBOL' AND symbol=:scope_key)
               OR (:scope_type='REGIME' AND regime=:scope_key)
               OR (:scope_type='SETUP' AND setup_type=:scope_key)
        """), {"scope_type": scope_type, "scope_key": scope_key}).mappings().one()
        rejects = session.execute(text("""
            SELECT reject_correct FROM rejected_signal_reviews
            WHERE (:scope_type='GLOBAL')
               OR (:scope_type='SYMBOL' AND symbol=:scope_key)
               OR (:scope_type='REGIME' AND regime=:scope_key)
               OR (:scope_type='SETUP' AND setup_type=:scope_key)
        """), {"scope_type": scope_type, "scope_key": scope_key}).mappings().all()
        reject_accuracy = calculate_reject_accuracy(rejects)
        sample_size = int(row["sample_size"] or 0)
        expectancy = float(row["avg_net_pnl_pct"] or 0.0)
        confidence = min(1.0, sample_size / 200.0)
        session.execute(text("""
            INSERT INTO adaptive_stats (
              scope_type, scope_key, sample_size, win_rate, avg_net_pnl_pct, avg_effective_rr, avg_spread_pct,
              avg_slippage_pct, reject_accuracy, expectancy, confidence, updated_at, payload_json
            ) VALUES (
              :scope_type, :scope_key, :sample_size, :win_rate, :avg_net_pnl_pct, :avg_effective_rr, :avg_spread_pct,
              :avg_slippage_pct, :reject_accuracy, :expectancy, :confidence, :updated_at, :payload_json
            )
            ON CONFLICT(scope_type, scope_key) DO UPDATE SET
              sample_size=excluded.sample_size, win_rate=excluded.win_rate, avg_net_pnl_pct=excluded.avg_net_pnl_pct,
              avg_effective_rr=excluded.avg_effective_rr, avg_spread_pct=excluded.avg_spread_pct,
              avg_slippage_pct=excluded.avg_slippage_pct, reject_accuracy=excluded.reject_accuracy,
              expectancy=excluded.expectancy, confidence=excluded.confidence, updated_at=excluded.updated_at, payload_json=excluded.payload_json
        """), {"scope_type": scope_type, "scope_key": scope_key, "sample_size": sample_size, "win_rate": row["win_rate"], "avg_net_pnl_pct": row["avg_net_pnl_pct"], "avg_effective_rr": row["avg_effective_rr"], "avg_spread_pct": row["avg_spread_pct"], "avg_slippage_pct": row["avg_slippage_pct"], "reject_accuracy": reject_accuracy, "expectancy": expectancy, "confidence": confidence, "updated_at": _now(), "payload_json": _dump({"scope_type": scope_type, "scope_key": scope_key})})
        return True
    except Exception as exc:
        logger.warning("update_adaptive_stats_failed: %s", exc)
        return False


def get_adaptive_stats(session: Any, scope_type: str, scope_key: str) -> Mapping[str, Any] | None:
    return session.execute(text("SELECT * FROM adaptive_stats WHERE scope_type=:scope_type AND scope_key=:scope_key"), {"scope_type": scope_type, "scope_key": scope_key}).mappings().first()


def compute_shadow_thresholds(base: Mapping[str, float], stats: Mapping[str, Any] | None, config: Mapping[str, Any]) -> dict[str, Any]:
    min_samples = int(config.get("ADAPTIVE_MIN_SAMPLE_SIZE", 50))
    max_score_adj = float(config.get("ADAPTIVE_MAX_SCORE_ADJUSTMENT", 0.05))
    max_rr_adj = float(config.get("ADAPTIVE_MAX_EFFECTIVE_RR_ADJUSTMENT", 0.15))
    allow_loosen = bool(config.get("ADAPTIVE_ALLOW_LOOSENING_GATES", False))
    if not stats or int(stats.get("sample_size") or 0) < min_samples:
        return {**base, "source": "STATIC", "reason": "INSUFFICIENT_SAMPLE_SIZE"}
    expectancy = float(stats.get("expectancy") or 0.0)
    score_adj = -max_score_adj if expectancy < 0 else (max_score_adj if allow_loosen else 0.0)
    rr_adj = max_rr_adj if expectancy < 0 else (-max_rr_adj if allow_loosen else 0.0)
    return {
        "min_score": max(0.0, min(1.0, float(base["min_score"]) + score_adj)),
        "min_effective_rr": max(0.0, float(base["min_effective_rr"]) + rr_adj),
        "max_spread_pct": min(float(base["max_spread_pct"]), float(stats.get("avg_spread_pct") or base["max_spread_pct"])),
        "max_expected_slippage_pct": float(base["max_expected_slippage_pct"]),
        "min_liquidity_score": max(float(base["min_liquidity_score"]), float(stats.get("confidence") or base["min_liquidity_score"])),
        "source": "SHADOW_ADAPTIVE",
        "reason": f"EXPECTANCY_{'NEGATIVE' if expectancy < 0 else 'POSITIVE'}_SAMPLES_{int(stats.get('sample_size') or 0)}",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str)
