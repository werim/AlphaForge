"""Prospective simulation source capture, never historical performance proof."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from alphaforge.backtest_source_evidence import (
    BacktestSourceProducer, SourceIdentity, digest, export_source_evidence, validate_source_packet,
)
from alphaforge.portfolio_risk import BacktestPortfolioState
from test_issue506_walk_forward import bo, _production_backtest_lifecycle
from test_walk_forward_cash_integrity import ms
from test_historical_capital_boundary import export_report, assert_capital_unavailable


START = ms(2, 15)


def setup_source(tmp_path, *, candles=None, initial=1000.0):
    candles = candles if candles is not None else {'BTCUSDT': [
        bo.Candle(START, 100., 100.5, 99.5, 100., 1000.),
        bo.Candle(START+60_000, 100., 104., 99.5, 103., 1000.),
    ]}
    raw = {s: [asdict(c) for c in rows] for s, rows in candles.items()}
    identity = SourceIdentity('source-run', 'window-source', START, START+120_000,
        60_000, 'a'*40, *([digest({})]*4), digest(sorted(raw)), digest(raw))
    url = f'sqlite+pysqlite:///{tmp_path / "source.sqlite"}'
    engine = bo.init_db(url)
    producer = BacktestSourceProducer(engine, identity, candles,
        provenance=dict(configuration={}, strategy={}, costs={}, source_files={}))
    state = BacktestPortfolioState(initial_equity=initial, evidence_producer=producer)
    return producer, state, url


def packet(producer):
    with producer.engine.connect() as conn:
        return export_source_evidence(conn, producer.identity.run_id)


def trade(tmp_path, monkeypatch, **kwargs):
    producer, state, url = setup_source(tmp_path)
    original = bo.process_backtest_result
    def routed(*args, **kw):
        return original(*args, **kw, portfolio_state=state)
    monkeypatch.setattr(bo, 'process_backtest_result', routed)
    rows = _production_backtest_lifecycle(START, **kwargs)
    bo._persist_lifecycle_rows(rows, database_url=url, run_id=producer.identity.run_id)
    return producer, state, url, rows


def test_attributable_flat_run_reconciles_and_exports_deterministically(tmp_path):
    producer, state, _ = setup_source(tmp_path)
    result = producer.finish()
    proof = packet(producer)
    assert result['status'] == 'COMPLETE'
    assert result['zero_flow_coverage'] == 'OWNED_CLOSE_ONLY'
    assert result['position_population'] == []
    assert result['closing_cash_usdt'] == 1000.
    assert all(v['marked_equity_usdt'] == 1000. for v in result['valuations'])
    assert proof['initialization']['capital_origin'] == 'BACKTEST_PORTFOLIO_CONSTRUCTOR'
    assert proof == packet(producer)
    assert producer.finish() == result  # idempotent finalization
    assert result['capital_return_pct'] is None


def test_real_simulator_persistence_cash_and_mark_inputs(tmp_path, monkeypatch):
    producer, state, url, rows = trade(tmp_path, monkeypatch)
    result = producer.finish()
    closed = next(r for r in rows if r.status_after == 'POSITION_CLOSED')
    assert state.current_equity == pytest.approx(1000+closed.net_pnl_usdt)
    assert result['valuations'][-1]['settled_cash_usdt'] == pytest.approx(state.current_equity)
    assert result['status'] == 'INCOMPLETE'
    assert 'CANDLE_LABEL_EVENT_TIME_ONLY' in result['gaps']
    marked = result['valuations'][1]
    assert len(marked['positions']) == 1
    assert marked['positions'][0]['quantity'] == closed.filled_quantity
    assert marked['positions'][0]['mark'] == 100.
    assert marked['unrealized_gross_usdt'] == 0.
    assert marked['marked_equity_usdt'] is None  # no observed accrued open costs
    assert 'CANONICAL_CLOSURE_CASH_INCOMPLETE' not in result['gaps']
    assert 'CASH_POPULATION_MISMATCH' not in result['gaps']
    assert packet(producer)['completion'] == result


@pytest.mark.parametrize('value', [None, True, '1000', float('nan'), float('inf'), -1., 0.])
def test_missing_or_invalid_opening_never_inherits_final_balance(tmp_path, value):
    with pytest.raises((ValueError, TypeError)):
        setup_source(tmp_path, initial=value)


def test_missing_source_bars_preserve_unknown_mark(tmp_path, monkeypatch):
    producer, state, url, rows = trade(tmp_path, monkeypatch)
    # Missing opening row is a different immutable source, not fabricated coverage.
    producer.source['BTCUSDT'] = producer.source['BTCUSDT'][1:]
    from alphaforge.backtest_source_evidence import _valuations
    snapshots, gaps = _valuations(producer.identity, packet(producer)['initialization'],
        [e['payload'] for e in packet(producer)['events'] if e['kind']=='LIFECYCLE'], producer.source)
    assert 'SOURCE_CANDLE_COVERAGE_INCOMPLETE' in gaps
    assert 'MISSING_OR_STALE_MARK' in gaps
    assert snapshots[1]['positions'][0]['mark'] is None
    assert snapshots[1]['marked_equity_usdt'] is None


def test_missing_cash_cannot_prove_conservation(tmp_path, monkeypatch):
    producer, state, url, rows = trade(tmp_path, monkeypatch)
    # Simulates interrupted observation before source cash recording.
    with producer.engine.begin() as conn:
        conn.execute(text('DROP TRIGGER backtest_source_events_no_delete'))
        conn.execute(text("DELETE FROM backtest_source_events WHERE kind='CASH_CLOSED'"))
    result = producer.finish()
    assert result['status'] == 'INCOMPLETE'
    assert {'CASH_POPULATION_MISMATCH','CLOSING_CASH_MISMATCH'}.issubset(result['gaps'])


def test_missing_sql_position_population_rejected(tmp_path, monkeypatch):
    producer, state, url, rows = trade(tmp_path, monkeypatch)
    with producer.engine.begin() as conn:
        conn.execute(text("DELETE FROM decision_evidence WHERE lifecycle_state_after='POSITION_OPENED'"))
    with pytest.raises(ValueError, match='position population'):
        producer.finish()


def test_duplicate_position_rejected_even_with_new_event_key(tmp_path, monkeypatch):
    producer, state, url, rows = trade(tmp_path, monkeypatch)
    opening = next(e for e in packet(producer)['events'] if e['kind']=='LIFECYCLE' and e['payload']['status_after']=='POSITION_OPENED')
    producer._append([('duplicate', opening['kind'], opening['effective_ms'], opening['payload'])])
    with pytest.raises(ValueError, match='population|duplicate'):
        producer.finish()


def test_duplicate_flow_is_idempotent_or_conflicting_never_double_cash(tmp_path, monkeypatch):
    producer, state, url, rows = trade(tmp_path, monkeypatch)
    movement = next(e for e in packet(producer)['events'] if e['kind']=='CASH_CLOSED')
    before = packet(producer)
    producer._append([(movement['event_key'], movement['kind'], movement['effective_ms'], movement['payload'])])
    assert packet(producer) == before
    altered = {**movement['payload'], 'after': 9000}
    with pytest.raises(ValueError, match='replay conflict'):
        producer._append([(movement['event_key'], movement['kind'], movement['effective_ms'], altered)])
    producer._append([('cash:duplicate', movement['kind'], movement['effective_ms'], movement['payload'])])
    with pytest.raises(ValueError, match='duplicate cash'):
        producer.finish()


def test_missing_costs_keep_equity_unknown(tmp_path, monkeypatch):
    producer, state, url, rows = trade(tmp_path, monkeypatch, with_realized_costs=False)
    result = producer.finish()
    assert result['status'] == 'INCOMPLETE'
    assert 'CANONICAL_CLOSURE_CASH_INCOMPLETE' in result['gaps']
    assert result['valuations'][-1]['settled_cash_usdt'] is None
    assert result['valuations'][-1]['marked_equity_usdt'] is None


def test_negative_cash_and_consumer_boundaries_preserved(tmp_path, monkeypatch):
    producer, state, url, rows = trade(tmp_path, monkeypatch, outcome='SL', cost_multiplier=1_000_000)
    result = producer.finish()
    assert 'NEGATIVE_SETTLED_CASH' in result['gaps']
    # Existing real SQL/export consumer gate still rejects loss, even with packet.
    monkeypatch.undo()
    train = _production_backtest_lifecycle(ms(1, 15))
    bo._persist_lifecycle_rows(train, database_url=url, run_id=producer.identity.run_id)
    report = export_report(tmp_path, bo._decision_evidence_rows(url, run_id=producer.identity.run_id), claim=packet_claim(result))
    assert report['historical_validation'] == 'FAIL'
    assert_capital_unavailable(report)


def packet_claim(result):
    return {'source_evidence': result}


def test_corrupt_or_incomplete_packet_and_scope_rejected(tmp_path):
    producer, _, _ = setup_source(tmp_path)
    interrupted = packet(producer)
    assert interrupted['status'] == 'INTERRUPTED'
    producer.finish()
    proof = packet(producer)
    for field, replacement in [('initialization', {}), ('identity_hash', 'c'*64), ('events', []), ('completion', None)]:
        bad = deepcopy(proof)
        bad[field] = replacement
        if bad == proof:
            continue
        with pytest.raises((ValueError, KeyError)):
            validate_source_packet(bad, expected_identity_hash=proof['identity_hash'], expected_completion_hash=proof['completion_hash'])
    with producer.engine.connect() as conn, pytest.raises(ValueError, match='identity mismatch'):
        export_source_evidence(conn, producer.identity.run_id, expected_identity=replace(producer.identity, run_id='foreign'))


def test_immutable_source_no_backfill_or_external_flows(tmp_path):
    producer, state, _ = setup_source(tmp_path)
    with producer.engine.begin() as conn, pytest.raises(Exception, match='immutable'):
        conn.execute(text('UPDATE backtest_source_runs SET initialization_json=\'{}\''))
    with pytest.raises(ValueError, match='unsupported'):
        producer.external_flow(100)
    state.current_equity += 1
    assert 'UNSUPPORTED_CAPITAL_MUTATION' in producer.finish()['gaps']


def test_offline_cli_produces_source_packet(tmp_path):
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([sys.executable, str(root/'backtest_order.py'), '--offline',
        '--source-evidence', '--interval', '1m', '--start', '2026-02-15', '--end', '2026-02-16',
        '--output-dir', str(tmp_path/'cli')], cwd=root, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr[-3000:]
    proof = json.loads((tmp_path/'cli/backtest_source_evidence.json').read_text())
    assert proof['initialization']['capital_origin'] == 'BACKTEST_PORTFOLIO_CONSTRUCTOR'
    assert proof['status'] == 'INCOMPLETE'  # fixture has 30 minutes, requested 24h
    assert 'SOURCE_CANDLE_COVERAGE_INCOMPLETE' in proof['completion']['gaps']


def test_unobserved_position_cannot_be_mistaken_for_flat(tmp_path):
    producer, state, _ = setup_source(tmp_path)
    state.open_position(position_id='unobserved', symbol='BTCUSDT', side='LONG',
                        notional=10, entry_price=100, timestamp=START)
    result = producer.finish()
    assert result['status'] == 'INCOMPLETE'
    assert 'PORTFOLIO_POSITION_POPULATION_MISMATCH' in result['gaps']


def test_sealed_sql_population_drift_rejected(tmp_path, monkeypatch):
    producer, state, url, rows = trade(tmp_path, monkeypatch)
    producer.finish()
    with producer.engine.begin() as conn:
        conn.execute(text("UPDATE decision_evidence SET trigger_price=101 WHERE lifecycle_state_after='POSITION_CLOSED'"))
    with pytest.raises(ValueError, match='snapshot drift'):
        packet(producer)


def test_restart_replays_source_once_and_rejects_foreign_initialization(tmp_path):
    producer, _, _ = setup_source(tmp_path)
    repeated, _, _ = setup_source(tmp_path)
    assert packet(producer) == packet(repeated)
    with pytest.raises(ValueError, match='initialization replay conflict'):
        setup_source(tmp_path, initial=2000.)
    assert repeated.finish()['status'] == 'COMPLETE'


@pytest.mark.parametrize('defect', ['duplicate', 'unordered', 'future', 'empty'])
def test_bad_source_coverage_never_becomes_complete(tmp_path, defect):
    candles = [bo.Candle(START, 100., 101., 99., 100., 10.),
               bo.Candle(START+60_000, 100., 101., 99., 100., 10.)]
    if defect == 'duplicate': candles.append(candles[-1])
    if defect == 'unordered': candles.reverse()
    if defect == 'future': candles.append(replace(candles[-1], timestamp=START+120_000))
    if defect == 'empty': candles = []
    producer, _, _ = setup_source(tmp_path, candles={'BTCUSDT': candles})
    if defect in {'duplicate', 'unordered'}:
        with pytest.raises(ValueError, match='unique ordered'):
            producer.finish()
    else:
        assert producer.finish()['status'] == 'INCOMPLETE'


def test_missing_opening_and_subset_rehash_cannot_forge_receipt(tmp_path, monkeypatch):
    producer, state, url, rows = trade(tmp_path, monkeypatch)
    producer.finish()
    proof = packet(producer)
    subset = deepcopy(proof)
    subset['events'] = subset['events'][:-1]
    subset['completion']['source_event_count'] = len(subset['events'])
    subset['completion']['source_digest'] = digest(subset['events'])
    with pytest.raises(ValueError, match='completion population'):
        validate_source_packet(subset, expected_identity_hash=proof['identity_hash'],
                               expected_completion_hash=proof['completion_hash'])
    bad = deepcopy(proof)
    bad['initialization']['initial_equity_usdt'] = None
    with pytest.raises(ValueError, match='opening'):
        validate_source_packet(bad, expected_identity_hash=proof['identity_hash'],
                               expected_completion_hash=proof['completion_hash'])



def test_source_configuration_excludes_credentials_in_transport_urls():
    from alphaforge.backtest_source_evidence import evidence_settings
    safe = dict(env_name='ALPHAFORGE_RISK_PCT', secret=False, current_value=1.)
    rows = [safe, dict(env_name='ALPHAFORGE_DATABASE_URL', secret=False,
                      current_value='postgresql://user:password@host/db'),
            dict(env_name='BINANCE_API_KEY', secret=True, current_value='key')]
    assert evidence_settings(rows) == [safe]
