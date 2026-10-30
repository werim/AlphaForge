"""Lossless candidate detail storage; tiny offline fixtures, never capacity proof."""
import base64
import json
import zlib

import pytest
from sqlalchemy import text

from alphaforge.persistence import init_db
from alphaforge.universe_evidence import load_persisted_universe_selection, persist_universe_selection
from test_issue622_universe_compact_evidence import selection_at


def test_complete_candidate_evidence_uses_less_than_quarter_logical_storage(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'batch.db'}")
    selection = selection_at(1_800_000_100.0, count=522)
    try:
        with engine.begin() as conn:
            persist_universe_selection(conn, selection)
        with engine.connect() as conn:
            assert load_persisted_universe_selection(conn, selection.cycle_id) == selection.as_dict()
            stored = conn.execute(text('''SELECT (SELECT SUM(LENGTH(payload_json)) FROM universe_selection_cycles)
                + SUM(LENGTH(observed_inputs_json)+LENGTH(evidence_availability_json))
                FROM universe_selection_candidates''')).scalar_one()
            raw = sum(len(json.dumps([c.observed_inputs, c.evidence_availability], sort_keys=True,
                                     separators=(',', ':'), allow_nan=False)) for c in selection.candidates)
            assert stored < raw / 4
            assert conn.execute(text('SELECT COUNT(*) FROM universe_selection_candidates')).scalar_one() == 522
            assert conn.execute(text('PRAGMA foreign_key_check')).all() == []
        engine.dispose()
        engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'batch.db'}")
        with engine.begin() as conn:
            assert persist_universe_selection(conn, selection)
            assert load_persisted_universe_selection(conn, selection.cycle_id) == selection.as_dict()
    finally:
        engine.dispose()


@pytest.mark.parametrize('fault', ['digest', 'truncated', 'trailing', 'too_large', 'unknown_codec', 'wrong_reference',
                                 'short_length', 'bool_length', 'base64', 'missing_batch', 'cycle_codec', 'shape'])
def test_batch_corruption_never_recovers_from_another_authority(tmp_path, fault):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'damaged.db'}")
    selection = selection_at(1_800_000_101.0, count=12)
    try:
        with engine.begin() as conn:
            persist_universe_selection(conn, selection)
            payload = json.loads(conn.execute(text('SELECT payload_json FROM universe_selection_cycles')).scalar_one())
            details = payload['candidate_details']
            if fault == 'digest': details['sha256'] = '0' * 64
            if fault == 'truncated': details['data'] = details['data'][:-8]
            if fault == 'trailing': details['data'] = base64.b64encode(base64.b64decode(details['data']) + b'extra').decode()
            if fault == 'too_large': details['uncompressed_bytes'] = 2**40
            if fault == 'unknown_codec': details['codec'] = 'unknown'
            if fault == 'short_length': details['uncompressed_bytes'] = 1
            if fault == 'bool_length': details['uncompressed_bytes'] = True
            if fault == 'base64': details['data'] = 'not-valid!'
            if fault == 'missing_batch': payload.pop('candidate_details')
            if fault == 'cycle_codec': payload['_storage_codec'] = 'unknown'
            if fault == 'shape':
                import hashlib
                raw = b'{}'
                details.update(uncompressed_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                               data=base64.b64encode(zlib.compress(raw)).decode())
            conn.execute(text('DROP TRIGGER trg_universe_selection_cycles_no_update'))
            conn.execute(text('UPDATE universe_selection_cycles SET payload_json=:p'), {'p': json.dumps(payload)})
            if fault == 'wrong_reference':
                conn.execute(text('DROP TRIGGER trg_universe_selection_candidates_no_update'))
                conn.execute(text("UPDATE universe_selection_candidates SET observed_inputs_json='{}' WHERE candidate_index=0"))
        with engine.connect() as conn:
            with pytest.raises(ValueError, match='UNIVERSE_SELECTION_EVIDENCE_INVALID'):
                load_persisted_universe_selection(conn, selection.cycle_id)
        with pytest.raises(RuntimeError, match='IDEMPOTENCY_CONFLICT'):
            with engine.begin() as conn:
                persist_universe_selection(conn, selection)
    finally:
        engine.dispose()


@pytest.mark.parametrize('legacy', [None, 'candidate-rows-v2'])
def test_legacy_layout_replay_never_rewrites_historical_rows(tmp_path, monkeypatch, legacy):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    selection = selection_at(1_800_000_102.0, count=12)
    try:
        with engine.begin() as conn:
            persist_universe_selection(conn, selection)
            payload = selection.as_dict()
            if legacy:
                payload.pop('candidates')
                payload['_storage_codec'] = legacy
            conn.execute(text('DROP TRIGGER trg_universe_selection_cycles_no_update'))
            conn.execute(text('DROP TRIGGER trg_universe_selection_candidates_no_update'))
            conn.execute(text('UPDATE universe_selection_cycles SET payload_json=:p'),
                         {'p': json.dumps(payload, sort_keys=True, separators=(',', ':'))})
            for i, c in enumerate(selection.candidates):
                conn.execute(text('UPDATE universe_selection_candidates SET observed_inputs_json=:o, '
                                  'evidence_availability_json=:a WHERE candidate_index=:i'),
                             {'o': json.dumps(c.observed_inputs, sort_keys=True, separators=(',', ':')),
                              'a': json.dumps(c.evidence_availability, sort_keys=True), 'i': i})
            before = conn.execute(text('SELECT * FROM universe_selection_candidates ORDER BY candidate_index')).all()
            before_cycle = conn.execute(text('SELECT * FROM universe_selection_cycles')).one()
            from alphaforge import universe_evidence as module
            monkeypatch.setattr(module, 'MAX_CANDIDATE_DETAILS_BYTES', 1)
            assert persist_universe_selection(conn, selection)
            assert load_persisted_universe_selection(conn, selection.cycle_id) == selection.as_dict()
            assert conn.execute(text('SELECT * FROM universe_selection_candidates ORDER BY candidate_index')).all() == before
            assert conn.execute(text('SELECT * FROM universe_selection_cycles')).one() == before_cycle
    finally:
        engine.dispose()


def test_candidate_write_failure_rolls_back_batch_metadata_and_all_rows(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'atomic.db'}")
    selection = selection_at(1_800_000_103.0, count=12)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TRIGGER fixture_failure BEFORE INSERT ON universe_selection_candidates "
                              "WHEN NEW.candidate_index=2 BEGIN SELECT RAISE(ABORT,'fixture crash'); END"))
        with pytest.raises(Exception, match='fixture crash'):
            with engine.begin() as conn:
                persist_universe_selection(conn, selection)
        with engine.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM universe_selection_cycles')).scalar_one() == 0
            assert conn.execute(text('SELECT COUNT(*) FROM universe_selection_candidates')).scalar_one() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize('mode', ['PAPER', 'BACKTEST', 'LIVE_PRECHECK'])
def test_storage_keeps_mode_and_canonical_semantics(tmp_path, mode):
    from dataclasses import replace
    selection = selection_at(1_800_000_104.0, count=12)
    selection = replace(selection, execution_mode=mode,
                        cycle_id=f'universe:{mode}:{selection.evidence_hash}')
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'mode.db'}")
    try:
        with engine.begin() as conn:
            persist_universe_selection(conn, selection)
            assert load_persisted_universe_selection(conn, selection.cycle_id) == selection.as_dict()
    finally:
        engine.dispose()


def test_encoder_bound_fails_before_cycle_insertion(tmp_path, monkeypatch):
    from alphaforge import universe_evidence as module
    selection = selection_at(1_800_000_105.0, count=12)
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'bound.db'}")
    monkeypatch.setattr(module, 'MAX_CANDIDATE_DETAILS_BYTES', 1)
    try:
        with pytest.raises(ValueError, match='CANDIDATE_DETAILS_BOUND'):
            with engine.begin() as conn:
                persist_universe_selection(conn, selection)
        with engine.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM universe_selection_cycles')).scalar_one() == 0
    finally:
        engine.dispose()
