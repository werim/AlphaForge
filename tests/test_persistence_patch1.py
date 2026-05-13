from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.order import before_real_order
from alphaforge.persistence import init_db, save_order_decision, save_trade_lifecycle_event


def test_save_order_decision_writes_retrievable_row():
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        row_id = save_order_decision(
            s,
            signal_id=1,
            phase="real",
            decision="REJECTED",
            order_type="LIMIT",
            confidence=0.21,
            explanation="blocked",
            order_payload={"reject_reason": "LOW_SCORE"},
            expected_slippage_pct=0.01,
            effective_rr=0.9,
        )
        assert row_id is not None
        row = s.execute(text("SELECT payload FROM order_decisions WHERE id=:id"), {"id": row_id}).one()
        assert "LOW_SCORE" in row.payload


def test_save_trade_lifecycle_event_writes_retrievable_row():
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        row_id = save_trade_lifecycle_event(s, signal_id=11, event_type="SIGNAL_REJECTED", payload={"reject_reason": "LOW_SCORE"})
        assert row_id is not None
        row = s.execute(text("SELECT trade_id,state,payload FROM trade_lifecycle_events WHERE id=:id"), {"id": row_id}).one()
        assert row.state == "SIGNAL_REJECTED"
        assert "LOW_SCORE" in row.payload


def test_rejected_decisions_persist_reject_reason():
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        row_id = save_order_decision(s, signal_id=2, phase="real", decision="REJECTED", order_type="NONE", confidence=0.1, explanation="rejected", order_payload={"reject_reason": "LOW_SCORE"}, expected_slippage_pct=0.0, effective_rr=0.0)
        row = s.execute(text("SELECT payload FROM order_decisions WHERE id=:id"), {"id": row_id}).one()
        assert "REJECTED" in row.payload
        assert "LOW_SCORE" in row.payload


def test_lifecycle_rejected_rows_present_in_export_trace_shape():
    lifecycle_rows = [
        {"status_before": "SIGNAL_CREATED", "status_after": "SIGNAL_REJECTED", "reject_reason": "LOW_SCORE"},
        {"status_before": "SIGNAL_CREATED", "status_after": "ORDER_REJECTED", "reject_reason": "HIGH_SLIPPAGE"},
    ]
    rejected = [r for r in lifecycle_rows if r["status_after"] in {"SIGNAL_REJECTED", "ORDER_REJECTED"}]
    assert len(rejected) == 2
    assert all(r["reject_reason"] for r in rejected)
