"""Execution-model evidence; deterministic data is not historical market proof."""
from copy import deepcopy
import json
import pytest

from alphaforge.backtest_chronology import run_boundary_backtest

START = 1771113600000


def dataset():
    return dict(schema='candle-boundary-backtest-v1', run_id='chronology', start_ms=START,
        end_ms=START+240000, interval_ms=60000, initial_capital_usdt=1000.,
        data_origin='SYNTHETIC_DETERMINISTIC_EXECUTION_INPUT',
        policy=dict(product='USDT_LINEAR_NO_FUNDING', fee_rate=.001,
                    slippage_bps=0., fill_model='NEXT_BOUNDARY_OPEN_FULL_FILL',
                    decision_basis='PRECOMMITTED_INTENTS', valuation='COMPLETED_CANDLE_CLOSE'),
        candles={symbol: [dict(timestamp=START+i*60000, open=o, high=max(o,c)+1,
                               low=min(o,c)-1, close=c, volume=1000.)
                          for i,(o,c) in enumerate(bars)]
                 for symbol,bars in {'BTCUSDT':[(100.,102.),(103.,104.),(105.,106.),(107.,108.)],
                                     'ETHUSDT':[(200.,198.),(197.,196.),(195.,194.),(193.,192.)]}.items()},
        intents=[dict(intent_id='long', symbol='BTCUSDT', side='LONG', order_type='MARKET',
                      entry_ms=START, exit_ms=START+60000, notional_usdt=100., risk_stop=99., close_fraction=1.),
                 dict(intent_id='short', symbol='ETHUSDT', side='SHORT', order_type='MARKET',
                      entry_ms=START, exit_ms=START+120000, notional_usdt=200., risk_stop=201., close_fraction=1.)])


def test_traded_portfolio_complete_with_concurrent_long_short_and_cash_conservation(tmp_path):
    proof = run_boundary_backtest(dataset(), tmp_path/'run')
    assert proof['status'] == 'COMPLETE'
    done = proof['completion']
    assert done['gaps'] == []
    assert len(done['closure_population']) == 2
    snapshots = done['valuations']
    assert [v['effective_ms'] for v in snapshots] == list(range(START, START+240001, 60000))
    assert snapshots[0]['marked_equity_usdt'] == 1000.
    assert len(snapshots[1]['positions']) == 2
    # Independently computed: cash + 2 long + 2 short - (.1+.2) incurred entry fees.
    assert snapshots[1]['marked_equity_usdt'] == pytest.approx(1003.7)
    assert snapshots[1]['accrued_costs_usdt'] == pytest.approx(.3)
    # Long: 3-(.1+.103), short: 5-(.2+.195). No second fee debit.
    assert done['closing_cash_usdt'] == pytest.approx(1007.402)
    assert snapshots[-1]['marked_equity_usdt'] == pytest.approx(1007.402)
    assert done['capital_return_pct'] is None and done['max_drawdown_pct'] is None
    assert [e['effective_ms'] for e in proof['events']] == sorted(e['effective_ms'] for e in proof['events'])
    assert all(v['evidence_status']=='COMPLETE' for v in snapshots)
    assert (tmp_path/'run/backtest_source_evidence.json').exists()


@pytest.mark.parametrize('defect', ['partial_close','partial_fill','limit','missing_bar','missing_mark'])
def test_unsupported_or_missing_source_never_promotes_equity(tmp_path, defect):
    data = dataset()
    expected = 'UNSUPPORTED_FILL_OR_PARTIAL_CLOSE'
    if defect == 'partial_fill': data['intents'][0]['fill_fraction'] = .5
    if defect == 'partial_close': data['intents'][0]['close_fraction'] = .5
    if defect == 'limit': data['intents'][0]['order_type'] = 'LIMIT'
    if defect == 'missing_bar':
        data['candles']['BTCUSDT'].pop(1)
        expected = 'SOURCE_CANDLE_COVERAGE_INCOMPLETE'
    if defect == 'missing_mark':
        data['candles']['BTCUSDT'][0]['close'] = None
        expected = 'MISSING_OR_STALE_MARK'
    proof = run_boundary_backtest(data, tmp_path/defect)
    assert proof['status'] == 'INCOMPLETE'
    assert expected in proof['completion']['gaps']
    assert all(v['marked_equity_usdt'] is None for v in proof['completion']['valuations'])


def test_open_position_at_window_end_has_complete_costs_and_marks(tmp_path):
    data = dataset()
    data['intents'][1]['exit_ms'] = None
    proof = run_boundary_backtest(data, tmp_path/'open')
    assert proof['status'] == 'COMPLETE'
    last = proof['completion']['valuations'][-1]
    assert len(last['positions']) == 1
    # Closed long cash + short gross (200-192)*1 - its .2 entry-fee liability.
    assert last['marked_equity_usdt'] == pytest.approx(1002.797+8-.2)
    assert last['positions'][0]['mark_effective_ms'] == data['end_ms']


def test_boundary_tie_order_is_model_owned_close_then_sorted_opens(tmp_path):
    data = dataset()
    data['intents'].append({**data['intents'][0], 'intent_id':'new-long',
        'entry_ms':START+60000, 'exit_ms':None, 'risk_stop':100.})
    proof = run_boundary_backtest(data, tmp_path/'tie')
    events = [e for e in proof['events'] if e['effective_ms']==START+60000]
    assert [e['kind'] for e in events] == ['SNAPSHOT','LIFECYCLE','CASH_CLOSED','LIFECYCLE','COST_ACCRUED']
    assert proof['status'] == 'COMPLETE'


def test_later_bars_cannot_change_earlier_fills_or_marks(tmp_path):
    first = run_boundary_backtest(dataset(), tmp_path/'first')
    changed = dataset()
    changed['candles']['ETHUSDT'][2].update(open=150., high=9000., low=1., close=149.)
    second = run_boundary_backtest(changed, tmp_path/'second')
    before = lambda p: [(e['event_key'],e['payload']) for e in p['events'] if e['effective_ms']<START+120000]
    assert before(first) == before(second)
    assert first['completion']['closing_cash_usdt'] != second['completion']['closing_cash_usdt']


def test_modeled_slippage_is_in_fill_not_a_second_fee(tmp_path):
    data = dataset()
    data['policy']['slippage_bps'] = 10.
    proof = run_boundary_backtest(data, tmp_path/'slippage')
    assert proof['status']=='COMPLETE'
    closes = [e['payload'] for e in proof['events'] if e['kind']=='LIFECYCLE' and e['payload']['status_after']=='POSITION_CLOSED']
    total = 1000.
    for p in closes:
        costs = json.loads(p['realized_costs_json'])
        assert costs['entry_slippage']==costs['exit_slippage']==0.
        sign = 1 if p['side']=='LONG' else -1
        total += sign*(p['close_price']-p['trigger_price'])*p['filled_quantity'] - costs['entry_fee']-costs['exit_fee']
    assert proof['completion']['closing_cash_usdt']==pytest.approx(total)


@pytest.mark.parametrize('defect', ['cost_missing','mark_changed','duplicate','out_of_order','cash_changed','missing_snapshot'])
def test_independent_replay_refuses_unreconciled_primary_evidence(tmp_path, defect):
    from alphaforge.backtest_source_evidence import SourceIdentity
    from alphaforge.backtest_chronology import replay_boundary_evidence
    proof = run_boundary_backtest(dataset(), tmp_path/'valid')
    events = deepcopy(proof['events'])
    if defect=='cost_missing': events = [e for e in events if e['kind']!='COST_ACCRUED']
    if defect=='mark_changed':
        snap = next(e for e in events if e['kind']=='SNAPSHOT' and e['payload']['positions'])
        snap['payload']['positions'][0]['mark'] = 999.
    if defect=='duplicate': events.insert(1,deepcopy(events[1]))
    if defect=='out_of_order': events[1],events[2] = events[2],events[1]
    if defect=='cash_changed': next(e for e in events if e['kind']=='CASH_CLOSED')['payload']['after'] += 1
    if defect=='missing_snapshot': events = [e for e in events if e['event_key']!=f'snapshot:{START+60000}']
    with pytest.raises(ValueError):
        replay_boundary_evidence(SourceIdentity(**proof['identity']), proof['initialization'], events)


@pytest.mark.parametrize('defect', ['missing_policy','bool_fee','future_entry','same_boundary','duplicate_id','funding_product'])
def test_invalid_policy_or_causality_refused_before_execution(tmp_path, defect):
    data = dataset()
    if defect=='missing_policy': data['policy'].pop('fee_rate')
    if defect=='bool_fee': data['policy']['fee_rate']=True
    if defect=='future_entry': data['intents'][0]['entry_ms']=data['end_ms']
    if defect=='same_boundary': data['intents'][0]['exit_ms']=START
    if defect=='duplicate_id': data['intents'][1]['intent_id']='long'
    if defect=='funding_product': data['policy']['product']='UNSPECIFIED_PERPETUAL'
    with pytest.raises(ValueError): run_boundary_backtest(data, tmp_path/'invalid')
    assert not (tmp_path/'invalid/backtest.sqlite').exists()


def test_producer_identity_receipt_and_restart_conflict(tmp_path):
    from alphaforge.backtest_source_evidence import validate_source_packet
    data = dataset()
    proof = run_boundary_backtest(data,tmp_path/'same')
    validate_source_packet(proof,expected_identity_hash=proof['identity_hash'],expected_completion_hash=proof['completion_hash'])
    changed = dataset()
    changed['initial_capital_usdt'] = 2000.
    with pytest.raises(ValueError,match='replay conflict'):
        run_boundary_backtest(changed,tmp_path/'same')


def test_negative_cash_source_and_existing_consumers_stay_closed(tmp_path):
    data = dataset()
    data['policy']['fee_rate'] = .9
    data['intents'][0]['notional_usdt'] = 2000.
    proof = run_boundary_backtest(data,tmp_path/'negative')
    assert proof['status']=='INCOMPLETE'
    assert 'NEGATIVE_SETTLED_CASH' in proof['completion']['gaps']
    assert proof['completion']['capital_return_pct'] is None
    assert proof['completion']['max_drawdown_pct'] is None


def test_exact_retry_is_idempotent_and_interruption_cannot_claim_coverage(tmp_path, monkeypatch):
    from alphaforge.backtest_source_evidence import BacktestSourceProducer, export_source_evidence
    from alphaforge.persistence import init_db
    first = run_boundary_backtest(dataset(),tmp_path/'retry')
    assert run_boundary_backtest(dataset(),tmp_path/'retry') == first
    original = BacktestSourceProducer._append
    def interrupted(self, entries, conn=None):
        if any(kind=='COST_ACCRUED' for _,kind,_,_ in entries):
            raise RuntimeError('injected source interruption')
        return original(self,entries,conn)
    monkeypatch.setattr(BacktestSourceProducer,'_append',interrupted)
    with pytest.raises(RuntimeError,match='interruption'):
        run_boundary_backtest(dataset(),tmp_path/'crash')
    engine = init_db('sqlite+pysqlite:///'+str(tmp_path/'crash/backtest.sqlite'))
    with engine.connect() as conn:
        assert export_source_evidence(conn,'chronology')['status']=='INTERRUPTED'
    engine.dispose()
    monkeypatch.undo()
    recovered = run_boundary_backtest(dataset(),tmp_path/'crash')
    assert recovered['status']=='COMPLETE'
    assert len({e['event_key'] for e in recovered['events']})==len(recovered['events'])


def test_complete_source_packet_does_not_unlock_historical_capital_metrics(tmp_path):
    from test_historical_capital_boundary import export_report, assert_capital_unavailable
    from test_walk_forward_cash_integrity import sql_rows
    proof = run_boundary_backtest(dataset(),tmp_path/'complete')
    rows = sql_rows(tmp_path)
    loss = next(r for r in rows if r['lifecycle_state_after']=='POSITION_CLOSED' and r['net_pnl_usdt']<0)
    rows = [r for r in rows if r['position_id']!=loss['position_id']]
    report = export_report(tmp_path,rows,claim=proof)
    assert report['historical_validation']=='INCOMPLETE'
    assert_capital_unavailable(report)


def test_production_cli_reads_frozen_input_and_retains_source_receipt(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable,'-m','alphaforge.backtest_chronology',
        '--input',str(root/'tests/fixtures/candle_boundary_backtest.json'),
        '--output-dir',str(tmp_path/'cli')], cwd=root,
        env={**os.environ,'PYTHONPATH':str(root/'src')}, capture_output=True,text=True)
    assert result.returncode==0,result.stderr[-3000:]
    proof = json.loads((tmp_path/'cli/backtest_source_evidence.json').read_text())
    assert json.loads(result.stdout)['status']=='COMPLETE'
    assert proof['identity']['version']=='backtest-boundary-v1'
    assert proof['initialization']['provenance']['configuration']['data_origin']=='SYNTHETIC_DETERMINISTIC_EXECUTION_INPUT'
    assert len(proof['completion']['closure_population'])==2
