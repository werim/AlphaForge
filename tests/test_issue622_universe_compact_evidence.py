"""#622: immutable, replayable universe selection without duplicated cycle payload."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from alphaforge.persistence import init_db
from alphaforge.symbol_selector import UniverseConstraints, build_selected_universe
from alphaforge.universe_evidence import (
    UNIVERSE_CYCLE_STORAGE_CODEC,
    load_persisted_universe_selection,
    persist_universe_selection,
)


def selection_at(ts: float, count: int = 300):
    candidates = [
        {
            "symbol": f"S{i:04d}USDT",
            "source_exchange": "binance",
            "market_ts": ts,
            "market_observed_at": ts,
            "market_data_source": "BINANCE_PUBLIC",
            "contract_type": "PERPETUAL",
            "quote_asset": "USDT",
            "instrument_status": "TRADING",
            "volume_24h_usdt": 200_000_000 - i * 10000,
            "spread_pct": 0.0001 + i * 0.000001,
            "spread_status": "MEASURED",
            "expected_slippage_pct": 0.0002,
            "volatility_pct": 0.03,
            "volatility_status": "MEASURED",
            "funding_rate_pct": 0.0,
            "funding_status": "MEASURED",
            "trend_strength": 0.8,
            "chop_score": 0.1,
            "liquidity_score": 0.7,
            "timeframe": "1m",
        }
        for i in range(count)
    ]
    return build_selected_universe(
        candidates, UniverseConstraints(max_active_symbols=5),
        decision_timestamp=ts, execution_mode="PAPER",
        git_sha="a" * 40, strategy_config_hash="strategy-622",
    )


def test_candidate_rows_are_canonical_and_cycle_payload_is_compact(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'compact.db'}")
    selection = selection_at(1_800_000_000.0)
    try:
        with engine.begin() as conn:
            assert persist_universe_selection(conn, selection)
            assert persist_universe_selection(conn, selection)
        with engine.connect() as conn:
            stored = conn.execute(text(
                "SELECT cycle_id,payload_json,candidate_count,evidence_hash "
                "FROM universe_selection_cycles"
            )).mappings().one()
            raw = json.loads(stored["payload_json"])
            assert raw["_storage_codec"] == UNIVERSE_CYCLE_STORAGE_CODEC
            assert "candidates" not in raw
            assert stored["candidate_count"] == 300
            assert stored["evidence_hash"] == selection.evidence_hash
            canonical = json.dumps(selection.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
            assert len(stored["payload_json"]) < len(canonical) / 10
            assert load_persisted_universe_selection(conn, selection.cycle_id) == selection.as_dict()
            assert conn.execute(text("SELECT COUNT(*) FROM universe_selection_candidates")).scalar_one() == 300
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
            with pytest.raises(Exception, match="immutable"):
                conn.execute(text("UPDATE universe_selection_cycles SET evidence_hash='bad'"))
    finally:
        engine.dispose()


def test_legacy_full_cycle_remains_readable_and_retryable(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    selection = selection_at(1_800_000_001.0, count=8)
    legacy_json = json.dumps(selection.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
    try:
        with engine.begin() as conn:
            # Model a complete pre-#622 cycle, including its normalized rows.
            # Fixture-level trigger bypass is solely for version simulation:
            # production data is never rewritten in place.
            persist_universe_selection(conn, selection)
            conn.execute(text("DROP TRIGGER trg_universe_selection_cycles_no_update"))
            conn.execute(text(
                "UPDATE universe_selection_cycles SET payload_json=:payload "
                "WHERE cycle_id=:cycle"
            ), {"payload": legacy_json, "cycle": selection.cycle_id})
        with engine.connect() as conn:
            assert load_persisted_universe_selection(conn, selection.cycle_id) == selection.as_dict()
            assert conn.execute(text(
                "SELECT payload_json FROM universe_selection_cycles"
            )).scalar_one() == legacy_json
        with engine.begin() as conn:
            assert persist_universe_selection(conn, selection)
        with engine.connect() as conn:
            assert load_persisted_universe_selection(conn, selection.cycle_id) == selection.as_dict()
    finally:
        engine.dispose()


@pytest.mark.parametrize("fault", ["modified", "missing"])
def test_corrupted_candidate_projection_fails_closed(tmp_path, fault):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'corrupted.db'}")
    selection = selection_at(1_800_000_002.0, count=12)
    try:
        with engine.begin() as conn:
            persist_universe_selection(conn, selection)
            # Simulate a damaged/externally altered DB: production triggers stay enabled.
            conn.execute(text("DROP TRIGGER trg_universe_selection_candidates_no_update"))
            conn.execute(text("DROP TRIGGER trg_universe_selection_candidates_no_delete"))
            if fault == "modified":
                conn.execute(text(
                    "UPDATE universe_selection_candidates SET eligibility_state='INVALID' "
                    "WHERE cycle_id=:cycle AND candidate_index=0"
                ), {"cycle": selection.cycle_id})
            else:
                conn.execute(text(
                    "DELETE FROM universe_selection_candidates "
                    "WHERE cycle_id=:cycle AND candidate_index=0"
                ), {"cycle": selection.cycle_id})
        with engine.connect() as conn:
            with pytest.raises(ValueError, match="UNIVERSE_SELECTION_EVIDENCE_INVALID"):
                load_persisted_universe_selection(conn, selection.cycle_id)
        with pytest.raises(RuntimeError, match="UNIVERSE_SELECTION_CANDIDATE_"):
            with engine.begin() as conn:
                persist_universe_selection(conn, selection)
    finally:
        engine.dispose()


def test_new_cycle_idempotent_after_restart_and_metadata_tamper_detection(tmp_path):
    path = tmp_path / "restart.db"
    selection = selection_at(1_800_000_003.0, count=12)
    engine = init_db(f"sqlite+pysqlite:///{path}")
    with engine.begin() as conn:
        persist_universe_selection(conn, selection)
    engine.dispose()
    engine = init_db(f"sqlite+pysqlite:///{path}")
    try:
        with engine.begin() as conn:
            assert persist_universe_selection(conn, selection)
        with engine.connect() as conn:
            assert load_persisted_universe_selection(conn, selection.cycle_id) == selection.as_dict()
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER trg_universe_selection_cycles_no_update"))
            conn.execute(text(
                "UPDATE universe_selection_cycles SET git_sha='other' "
                "WHERE cycle_id=:cycle"
            ), {"cycle": selection.cycle_id})
        with engine.connect() as conn:
            with pytest.raises(ValueError, match="UNIVERSE_SELECTION_EVIDENCE_INVALID"):
                load_persisted_universe_selection(conn, selection.cycle_id)
    finally:
        engine.dispose()


def test_300_candidate_multi_cycle_growth_does_not_duplicate_payload(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'growth.db'}")
    selections = [selection_at(1_800_000_010.0 + t) for t in range(10)]
    try:
        with engine.begin() as conn:
            for selection in selections:
                persist_universe_selection(conn, selection)
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT cycle_id,payload_json,candidate_count,evidence_hash "
                "FROM universe_selection_cycles ORDER BY decision_timestamp"
            )).all()
            assert len(rows) == 10
            assert conn.execute(text("SELECT COUNT(*) FROM universe_selection_candidates")).scalar_one() == 3000
            canonical_bytes = sum(len(json.dumps(
                s.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
            )) for s in selections)
            stored_cycle_bytes = sum(len(row[1]) for row in rows)
            assert stored_cycle_bytes < canonical_bytes / 10
            for selection in selections:
                assert load_persisted_universe_selection(conn, selection.cycle_id) == selection.as_dict()
    finally:
        engine.dispose()
