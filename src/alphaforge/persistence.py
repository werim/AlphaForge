from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from alphaforge.db.base import Base
from alphaforge.models import ai_schema, schema  # noqa: F401

logger = logging.getLogger(__name__)


def init_db(database_url: str):
    engine = create_engine(database_url, future=True)
    Base.metadata.create_all(engine)
    return engine


def _safe_write(session: Session, query: str, values: Mapping[str, Any]) -> Any:
    try:
        result = session.execute(text(query), values)
        session.commit()
        return result
    except Exception as exc:  # pragma: no cover
        session.rollback()
        logger.warning("Persistence write failed: %s", exc)
        return None


def save_signal(session: Session, **payload: Any) -> int | None:
    result = _safe_write(session, "INSERT INTO signals (symbol, side, timeframe, payload, created_at) VALUES (:symbol,:side,:timeframe,:payload,:created_at) RETURNING id", {
        "symbol": str(payload.get("symbol", "UNKNOWN")), "side": str(payload.get("side", "N/A")), "timeframe": str(payload.get("timeframe", "NA")), "payload": dict(payload), "created_at": _now(),
    })
    return result.scalar_one() if result is not None else None


def save_order_decision(session: Session, **payload: Any) -> int | None:
    result = _safe_write(session, "INSERT INTO order_decisions (signal_id,phase,decision,order_type,confidence,explanation,order_payload,created_at) VALUES (:signal_id,:phase,:decision,:order_type,:confidence,:explanation,:order_payload,:created_at) RETURNING id", {**payload, "created_at": _now()})
    return result.scalar_one() if result is not None else None


def save_ai_decision_features(session: Session, **payload: Any) -> None:
    _safe_write(session, "INSERT INTO ai_decision_features (decision_id,features,penalties,reason_flags,created_at) VALUES (:decision_id,:features,:penalties,:reason_flags,:created_at)", {**payload, "created_at": _now()})


def save_trade_lifecycle_event(session: Session, **payload: Any) -> None:
    _safe_write(session, "INSERT INTO trade_lifecycle_events (signal_id,event_type,payload,created_at) VALUES (:signal_id,:event_type,:payload,:created_at)", {**payload, "created_at": _now()})


def save_closed_trade_review(session: Session, **payload: Any) -> None:
    _safe_write(session, "INSERT INTO closed_trade_reviews (trade_id,symbol,review_payload,created_at) VALUES (:trade_id,:symbol,:review_payload,:created_at)", {**payload, "created_at": _now()})


def upsert_expectancy_stats(session: Session, table: str, key_col: str, key_val: str, pnl: float) -> None:
    _safe_write(session, f"INSERT INTO {table} ({key_col}, samples, win_count, total_pnl, expectancy, updated_at) VALUES (:key_val,1,:win_count,:pnl,:pnl,:updated_at) ON CONFLICT ({key_col}) DO UPDATE SET samples={table}.samples+1, win_count={table}.win_count+:win_count, total_pnl={table}.total_pnl+:pnl, expectancy=({table}.total_pnl+:pnl)/NULLIF({table}.samples+1,0), updated_at=:updated_at", {
        "key_val": key_val, "win_count": 1 if pnl > 0 else 0, "pnl": pnl, "updated_at": _now(),
    })


def save_cooldown_state(session: Session, **payload: Any) -> None:
    _safe_write(session, "INSERT INTO cooldown_states (scope,scope_key,active_until_ts,reason,payload,updated_at) VALUES (:scope,:scope_key,:active_until_ts,:reason,:payload,:updated_at) ON CONFLICT(scope,scope_key) DO UPDATE SET active_until_ts=:active_until_ts, reason=:reason, payload=:payload, updated_at=:updated_at", {**payload, "updated_at": _now()})


def make_session_factory(database_url: str):
    engine = init_db(database_url)
    return sessionmaker(engine, class_=Session, expire_on_commit=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)
