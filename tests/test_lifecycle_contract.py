from pathlib import Path

import pytest

from alphaforge.lifecycle_contract import (
    CANONICAL_LIFECYCLE_EVENTS,
    normalize_lifecycle_event,
    is_canonical_lifecycle_event,
    is_valid_lifecycle_transition,
)
from alphaforge.persistence import init_db, save_trade_lifecycle_event
from sqlalchemy import text
from sqlalchemy.orm import Session


def test_canonical_lifecycle_states_are_accepted() -> None:
    for state in CANONICAL_LIFECYCLE_EVENTS:
        assert normalize_lifecycle_event(state, allow_legacy=False) == state
        assert is_canonical_lifecycle_event(state)


def test_unknown_lifecycle_state_is_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_lifecycle_event("CREATED_AND_FILLED", allow_legacy=False)
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        assert save_trade_lifecycle_event(session, signal_id="s-unknown", symbol="BTCUSDT", lifecycle_state="CREATED_AND_FILLED") is None


def test_created_is_legacy_mapped_but_not_canonical() -> None:
    assert normalize_lifecycle_event("CREATED") == "SIGNAL_CREATED"
    assert not is_canonical_lifecycle_event("CREATED")

    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        assert save_trade_lifecycle_event(session, event_id="e-created", signal_id="s-created", symbol="BTCUSDT", mode="BACKTEST", lifecycle_state="CREATED") is True
        row = session.execute(text("SELECT lifecycle_state FROM trade_lifecycle_events WHERE event_id='e-created'")).one()
    assert row.lifecycle_state == "SIGNAL_CREATED"


def test_invalid_lifecycle_transitions_are_testable() -> None:
    assert is_valid_lifecycle_transition(None, "SIGNAL_CREATED")
    assert not is_valid_lifecycle_transition(None, "ENTRY_TRIGGERED")
    assert is_valid_lifecycle_transition("SIGNAL_CREATED", "SIGNAL_REJECTED")
    assert not is_valid_lifecycle_transition("SIGNAL_REJECTED", "ORDER_PLACED")


def test_docs_expected_states_match_code_constants() -> None:
    doc = Path("docs/decision_lifecycle_contract.md").read_text()
    for state in CANONICAL_LIFECYCLE_EVENTS:
        assert f"`{state}`" in doc
