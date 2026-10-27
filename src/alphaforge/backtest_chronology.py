"""Prospective candle-boundary execution, not reconstruction of intrabar history.

Execution intents are frozen before stepping; this is an execution/accounting
BACKTEST, not evidence that an external strategy selected its intents causally.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from uuid import NAMESPACE_URL, uuid5

from .backtest_source_evidence import (BOUNDARY_VERSION, BacktestSourceProducer,
    SourceIdentity, digest, export_source_evidence)
from .portfolio_risk import BacktestPortfolioState
from .realized_accounting import (candle_boundary_costs, filled_position_quantity,
    finite_cash, marked_portfolio_equity, position_cash_geometry, realized_cash_ledger, sum_cash)

SCHEMA = 'candle-boundary-backtest-v1'
COST_SOURCE = 'CANDLE_BOUNDARY_EXPLICIT_FEE_MODEL_V1'
ROOT = Path(__file__).resolve().parents[2]


def _number(value, *, positive=False):
    if type(value) not in (float, int) or finite_cash(value) is None or (positive and value <= 0):
        raise ValueError('boundary input requires finite typed numbers')
    return value


def _timestamp(value):
    if type(value) is not int or value < 0:
        raise ValueError('boundary timestamps require nonnegative integer UTC milliseconds')
    return value


def validate_input(data):
    fields = {'schema', 'run_id', 'start_ms', 'end_ms', 'interval_ms', 'initial_capital_usdt',
              'data_origin', 'policy', 'candles', 'intents'}
    if set(data)-{'strategy_evidence'} != fields or data['schema'] != SCHEMA:
        raise ValueError('unsupported boundary input schema/fields')
    start, end, interval = (_timestamp(data[k]) for k in ('start_ms','end_ms','interval_ms'))
    if not interval or end <= start or start % interval or end % interval or (end-start)//interval > 10000:
        raise ValueError('invalid/budget-exceeded boundary schedule')
    _number(data['initial_capital_usdt'], positive=True)
    if any(type(data[k]) is not str or not data[k].strip() for k in ('run_id','data_origin')):
        raise ValueError('missing boundary source identity')
    p = data['policy']
    if set(p) != {'product','fee_rate','slippage_bps','fill_model','decision_basis','valuation'}:
        raise ValueError('missing/unsupported boundary policy')
    if (p['product'] != 'USDT_LINEAR_NO_FUNDING' or p['fill_model'] != 'NEXT_BOUNDARY_OPEN_FULL_FILL'
            or p['decision_basis'] != ('CAUSAL_NATIVE_STRATEGY' if 'strategy_evidence' in data else 'PRECOMMITTED_INTENTS') or p['valuation'] != 'COMPLETED_CANDLE_CLOSE'):
        raise ValueError('unsupported boundary product/execution assumptions')
    if not 0 <= _number(p['fee_rate']) < 1 or not 0 <= _number(p['slippage_bps']) < 10000:
        raise ValueError('invalid explicit fee/slippage policy')
    if not isinstance(data['candles'], dict) or not data['candles'] or sum(map(len, data['candles'].values())) > 100000:
        raise ValueError('missing/budget-exceeded source universe')
    for symbol, bars in data['candles'].items():
        if type(symbol) is not str or not symbol.endswith('USDT'):
            raise ValueError('unsupported settlement symbol')
        labels = [_timestamp(b['timestamp']) for b in bars]
        if labels != sorted(set(labels)) or any(t < start or t >= end or t % interval for t in labels):
            raise ValueError('ambiguous/outside-window candle labels')
        for b in bars:
            if set(b) != {'timestamp','open','high','low','close','volume'}:
                raise ValueError('invalid source candle fields')
            for field in ('open','close'):
                if b[field] is not None:
                    _number(b[field], positive=True)
    seen = set()
    if not isinstance(data['intents'], list) or len(data['intents']) > 1000 or len(data['intents'])*((end-start)//interval+1)>100000:
        raise ValueError('invalid/budget-exceeded boundary intents')
    for order in data['intents']:
        if set(order)-{'fill_fraction'} != {'intent_id','symbol','side','order_type','entry_ms','exit_ms',
                          'notional_usdt','risk_stop','close_fraction'}:
            raise ValueError('unsupported intent fields')
        key = order['intent_id']
        if type(key) is not str or not key.strip() or key in seen:
            raise ValueError('missing/duplicate intent identity')
        seen.add(key)
        if order['symbol'] not in data['candles'] or order['side'] not in {'LONG','SHORT'}:
            raise ValueError('cross-scoped/unsupported intent')
        for field in ('entry_ms','exit_ms'):
            if order[field] is None and field == 'exit_ms':
                continue
            t = _timestamp(order[field])
            if not start <= t < end or t % interval:
                raise ValueError('intent outside half-open/aligned window')
        if order['exit_ms'] is not None and order['exit_ms'] <= order['entry_ms']:
            raise ValueError('ambiguous/same-boundary roundtrip unsupported')
        _number(order['notional_usdt'], positive=True)
        _number(order['risk_stop'], positive=True)
        _number(order['close_fraction'], positive=True)
        if 'fill_fraction' in order:
            _number(order['fill_fraction'], positive=True)
    # Force strict serialization, including unused OHLC fields (no NaN/Infinity).
    digest(data)
    if 'strategy_evidence' in data:
        from .backtest_strategy_adapter import validate_strategy_evidence
        validate_strategy_evidence(data)


def _ids(run_id, order):
    lifecycle = str(uuid5(NAMESPACE_URL, f'boundary:{run_id}:{order["intent_id"]}'))
    return lifecycle, str(uuid5(NAMESPACE_URL, lifecycle+':position'))


def _key(run_id, order, phase):
    lifecycle, _ = _ids(run_id, order)
    return f'lifecycle:{lifecycle}:{1 if phase == "open" else 2}:POSITION_{"OPENED" if phase == "open" else "CLOSED"}'


def _strategy_records(data,at):
    return [r for r in data.get('strategy_evidence',{}).get('records',[]) if r['decision_ms']==at]


def _supported_order(order):
    return order['order_type']=='MARKET' and order['close_fraction']==1 and order.get('fill_fraction',1)==1


def _actions(data, at):
    for phase, field in (('close','exit_ms'), ('open','entry_ms')):
        for order in sorted(data['intents'], key=lambda o:o['intent_id']):
            if order[field] == at and _supported_order(order):
                yield phase, order


def _bars(data):
    return {symbol:{b['timestamp']:b for b in bars} for symbol,bars in data['candles'].items()}


def _fill(data, bars, order, at, phase):
    bar = bars[order['symbol']].get(at)
    if bar is None or bar['open'] is None:
        return None
    direction = 1 if order['side'] == 'LONG' else -1
    if phase == 'close':
        direction *= -1
    return bar['open'] * (1 + direction*data['policy']['slippage_bps']/10000)


def _snapshot(data, bars, at, inventory, cash):
    positions, gaps = [], []
    for pid, opened in sorted(inventory.items()):
        bar = bars[opened['symbol']].get(at-data['interval_ms'])
        mark = None if bar is None else bar['close']
        if mark is None:
            gaps.append('MISSING_OR_STALE_MARK')
        gross = None if mark is None else position_cash_geometry(fill=opened['trigger_price'],
            stop=opened['sl'], quantity=opened['filled_quantity'], exit_price=mark, side=opened['side'])['gross_pnl_usd']
        accrued = opened.get('accrued_cost_usdt')
        if accrued is None:
            gaps.append('OPEN_POSITION_ACCRUED_COSTS_UNAVAILABLE')
        positions.append(dict(position_id=pid, symbol=opened['symbol'], side=opened['side'],
            quantity=opened['filled_quantity'], fill=opened['trigger_price'], mark=mark,
            mark_effective_ms=None if mark is None else at, mark_available_ms=None if mark is None else at,
            mark_source_hash=None if mark is None else digest(dict(symbol=opened['symbol'],
                candle_timestamp=bar['timestamp'], field='close', value=mark)),
            open_event_key=opened['open_event_key'], cost_event_key='cost:'+pid,
            accrued_cost_usdt=accrued, unrealized_gross_usdt=gross))
    return dict(effective_ms=at, phase='BEFORE_BOUNDARY_EVENTS', positions=positions,
        settled_cash_usdt=cash, unrealized_gross_usdt=sum_cash(p['unrealized_gross_usdt'] for p in positions),
        accrued_costs_usdt=sum_cash(p['accrued_cost_usdt'] for p in positions),
        marked_equity_usdt=marked_portfolio_equity(cash=cash, positions=positions)), gaps


def _action_problem(data, bars, order, at, phase, inventory):
    _, pid = _ids(data['run_id'], order)
    fill = _fill(data, bars, order, at, phase)
    if fill is None:
        return 'MISSING_FILL_SOURCE'
    if phase == 'close' and pid not in inventory:
        return 'MISSING_OPEN_POSITION'
    if phase == 'open' and ((order['side']=='LONG' and order['risk_stop'] >= fill)
                           or (order['side']=='SHORT' and order['risk_stop'] <= fill)):
        return 'INVALID_REFERENCE_RISK_GEOMETRY'
    return None


def _simulation_module():
    spec = importlib.util.spec_from_file_location('boundary_backtest_order', ROOT/'backtest_order.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_boundary_backtest(data, output_dir):
    """Execute a new prospective simulation; never upgrade existing source runs."""
    validate_input(data)
    # Freeze caller-owned mutable data before any decisions or state mutation.
    data = json.loads(json.dumps(data))
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    simulation = _simulation_module()
    candles = {s:[simulation.Candle(**b) for b in bars] for s,bars in data['candles'].items()}
    files = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
        'backtest_order.py', 'src/alphaforge/backtest_source_evidence.py',
        'src/alphaforge/backtest_chronology.py', 'src/alphaforge/realized_accounting.py', 'src/alphaforge/portfolio_risk.py')}
    if 'strategy_evidence' in data:
        for name in ('backtest_strategy_adapter','order','ai_brain','multi_timeframe','symbol_selector'):
            path = f'src/alphaforge/{name}.py'
            files[path] = hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
    strategy = data.get('strategy_evidence',data['intents'])
    configuration = {k:v for k,v in data.items() if k != 'candles'}
    identity = SourceIdentity(run_id=data['run_id'], window_id=f'{data["start_ms"]}:{data["end_ms"]}',
        start_ms=data['start_ms'], end_ms=data['end_ms'], interval_ms=data['interval_ms'],
        git_sha=subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
        code_hash=digest(files), config_hash=digest(configuration), strategy_hash=digest(strategy),
        cost_hash=digest(data['policy']), universe_hash=digest(sorted(candles)),
        data_hash=digest(data['candles']), version=BOUNDARY_VERSION)
    database_url = 'sqlite+pysqlite:///'+str((output/'backtest.sqlite').resolve())
    engine = simulation.init_db(database_url)
    producer = BacktestSourceProducer(engine, identity, candles, provenance=dict(configuration=configuration,
        strategy=strategy, costs=data['policy'], source_files=files))
    state = BacktestPortfolioState(initial_equity=data['initial_capital_usdt'], evidence_producer=producer)
    inventory, bars = {}, _bars(data)
    try:
        for at in range(data['start_ms'], data['end_ms']+1, data['interval_ms']):
            if set(state.open_positions) != set(inventory) or any(
                    state.open_positions[pid].entry_price != p['trigger_price']
                    or state.open_positions[pid].notional != p['filled_quantity']*p['trigger_price']
                    or state.open_positions[pid].symbol != p['symbol']
                    or state.open_positions[pid].side != p['side'] for pid,p in inventory.items()):
                raise ValueError('boundary portfolio position conservation mismatch')
            snapshot, _ = _snapshot(data, bars, at, inventory, state.current_equity)
            producer._append([(f'snapshot:{at}', 'SNAPSHOT', at, snapshot)])
            if at == data['end_ms']:
                break
            for record in _strategy_records(data,at):
                producer._append([('strategy:'+record['decision_id'],'STRATEGY_DECISION',at,record)])
            if at == data['start_ms']:
                for order in sorted(data['intents'], key=lambda o:o['intent_id']):
                    if not _supported_order(order):
                        producer._append([('unsupported:'+order['intent_id'], 'MODEL_DIAGNOSTIC', at,
                            dict(reason='UNSUPPORTED_FILL_OR_PARTIAL_CLOSE', intent_id=order['intent_id']))])
            for phase, order in _actions(data, at):
                lifecycle, pid = _ids(data['run_id'], order)
                problem = _action_problem(data, bars, order, at, phase, inventory)
                if problem:
                    producer._append([(f'problem:{order["intent_id"]}:{phase}', 'MODEL_DIAGNOSTIC', at,
                                       dict(reason=problem, intent_id=order['intent_id']))])
                    continue
                fill = _fill(data, bars, order, at, phase)
                if phase == 'open':
                    qty = filled_position_quantity(balance=state.current_equity, risk_pct=0,
                        fill=fill, notional=order['notional_usdt'])
                    row = simulation.LifecycleRow(at, order['symbol'], order['side'], 'BOUNDARY_INTENT',
                        'PRECOMMITTED_EXECUTION_ONLY', 'NOT_STRATEGY_EVIDENCE', None, None,
                        fill, order['risk_stop'], fill, 'ORDER_PLACED', 'POSITION_OPENED',
                        trigger_price=fill, filled_quantity=qty, signal_id=lifecycle, lifecycle_id=lifecycle,
                        lifecycle_seq=1, position_id=pid, order_id=lifecycle+':order', order_type='MARKET')
                    costs = candle_boundary_costs(entry_fill=fill, quantity=qty, fee_rate=data['policy']['fee_rate'])
                    producer.observe_simulation([row], state.current_equity, 0, dict(notional=order['notional_usdt']))
                    cost_payload = dict(position_id=pid, amount_usdt=costs['entry_fee'],
                        cause_event_key=_key(data['run_id'], order, 'open'), cost_source=COST_SOURCE)
                    producer._append([('cost:'+pid, 'COST_ACCRUED', at, cost_payload)])
                    inventory[pid] = dict(symbol=order['symbol'], side=order['side'], trigger_price=fill,
                        sl=order['risk_stop'], filled_quantity=qty, accrued_cost_usdt=costs['entry_fee'],
                        open_event_key=_key(data['run_id'], order, 'open'))
                    state.open_position(position_id=pid, symbol=order['symbol'], side=order['side'],
                        notional=qty*fill, entry_price=fill, timestamp=at)
                else:
                    opened = inventory[pid]
                    entry, qty = opened['trigger_price'], opened['filled_quantity']
                    costs = candle_boundary_costs(entry_fill=entry, quantity=qty,
                        fee_rate=data['policy']['fee_rate'], exit_fill=fill)
                    candidate = simulation.CandidateOrder(order['entry_ms'], order['symbol'], order['side'],
                        entry, order['risk_stop'], entry, 0., 'BOUNDARY_INTENT',
                        'PRECOMMITTED_EXECUTION_ONLY', 'NOT_STRATEGY_EVIDENCE', 0., 'MARKET')
                    row = simulation.finalize(candidate, 'POSITION_OPENED', 'POSITION_CLOSED', entry,
                        fill, 'TIMEOUT', 0., state.current_equity, 0, order['entry_ms'], at,
                        market_ctx=dict(notional=order['notional_usdt'], realized_costs_usd=costs,
                                        realized_cost_source=COST_SOURCE))
                    row.signal_id = row.lifecycle_id = lifecycle
                    row.lifecycle_seq, row.position_id, row.order_id = 2, pid, lifecycle+':order'
                    producer.observe_simulation([row], state.current_equity, 0, dict(notional=order['notional_usdt']))
                    state.close_position(position_id=pid, symbol=order['symbol'], timestamp=at,
                        net_pnl_usdt=row.net_pnl_usdt, close_reason='TIMEOUT')
                    del inventory[pid]
                simulation._persist_lifecycle_rows([row], database_url=database_url, run_id=data['run_id'],
                                                   profile_name='CANDLE_BOUNDARY_EXECUTION_ONLY')
        producer.finish()
        with engine.connect() as conn:
            packet = export_source_evidence(conn, data['run_id'], expected_identity=identity)
        (output/'backtest_source_evidence.json').write_text(json.dumps(packet, indent=2, sort_keys=True)+'\n')
        (output/'input.json').write_text(json.dumps(data, indent=2, sort_keys=True)+'\n')
        return packet
    finally:
        engine.dispose()


def replay_boundary_evidence(identity, initialization, events):
    """Independent causal source replay; no trust in snapshot/population claims."""
    data = {**initialization['provenance']['configuration'], 'candles':initialization['source_candles']}
    validate_input(data)
    if (identity.run_id != data['run_id'] or identity.start_ms != data['start_ms']
            or identity.end_ms != data['end_ms'] or identity.interval_ms != data['interval_ms']
            or initialization['initial_equity_usdt'] != data['initial_capital_usdt']):
        raise ValueError('boundary source scope mismatch')
    if (initialization.get('timing_semantics') != 'SIMULATED_CANDLE_BOUNDARIES_NOT_OBSERVED_INTRABAR'
            or initialization.get('cost_recognition') != 'ENTRY_COST_ACCRUAL_NET_CASH_ON_FULL_CLOSE'):
        raise ValueError('boundary timing/cost recognition contract missing')
    if (identity.window_id != f'{data["start_ms"]}:{data["end_ms"]}'
            or initialization['product_semantics'] != data['policy']['product']
            or initialization['flow_authority'] != 'INSTRUMENTED_CLOSE_ONLY_V1'
            or initialization['provenance']['costs'] != data['policy']
            or initialization['provenance']['strategy'] != data.get('strategy_evidence',data['intents'])):
        raise ValueError('boundary product/policy/intent provenance mismatch')
    bars, inventory, snapshots, gaps = _bars(data), {}, [], []
    cash, cursor = initialization['initial_equity_usdt'], 0

    def take(key, kind, at):
        nonlocal cursor
        if cursor >= len(events):
            raise ValueError('boundary event population incomplete')
        event = events[cursor]
        cursor += 1
        if (event['event_key'], event['kind'], event['effective_ms']) != (key,kind,at):
            raise ValueError('boundary causal sequence mismatch')
        return event['payload']

    if 'strategy_evidence' in data:
        gaps.append('NO_SUPPORTED_CAUSAL_STRATEGY_EXECUTION')
    expected_times = set(range(data['start_ms'],data['end_ms'],data['interval_ms']))
    if any(set(bars[s]) != expected_times for s in bars):
        gaps.append('SOURCE_CANDLE_COVERAGE_INCOMPLETE')
    for at in range(data['start_ms'],data['end_ms']+1,data['interval_ms']):
        expected, missing = _snapshot(data, bars, at, inventory, cash)
        actual = take(f'snapshot:{at}', 'SNAPSHOT', at)
        if actual != expected:
            raise ValueError('boundary marked-equity conservation mismatch')
        snapshots.append(expected)
        gaps.extend(missing)
        if at == data['end_ms']:
            break
        for record in _strategy_records(data,at):
            if take('strategy:'+record['decision_id'],'STRATEGY_DECISION',at)!=record:
                raise ValueError('strategy source decision mismatch')
        if at == data['start_ms']:
            for order in sorted(data['intents'], key=lambda o:o['intent_id']):
                if not _supported_order(order):
                    actual = take('unsupported:'+order['intent_id'], 'MODEL_DIAGNOSTIC', at)
                    if actual != dict(reason='UNSUPPORTED_FILL_OR_PARTIAL_CLOSE', intent_id=order['intent_id']):
                        raise ValueError('boundary unsupported diagnostic mismatch')
                    gaps.append('UNSUPPORTED_FILL_OR_PARTIAL_CLOSE')
        for phase, order in _actions(data, at):
            lifecycle, pid = _ids(data['run_id'], order)
            problem = _action_problem(data, bars, order, at, phase, inventory)
            if problem:
                actual = take(f'problem:{order["intent_id"]}:{phase}', 'MODEL_DIAGNOSTIC', at)
                if actual != dict(reason=problem, intent_id=order['intent_id']):
                    raise ValueError('boundary source diagnostic mismatch')
                gaps.append(problem)
                continue
            fill = _fill(data, bars, order, at, phase)
            p = take(_key(data['run_id'], order, phase), 'LIFECYCLE', at)
            if any(p[k] != v for k,v in dict(position_id=pid, lifecycle_id=lifecycle, symbol=order['symbol'],
                    side=order['side'], timestamp=at, sl=order['risk_stop']).items()):
                raise ValueError('boundary lifecycle scope mismatch')
            if phase == 'open':
                qty = filled_position_quantity(balance=cash, risk_pct=0, fill=fill, notional=order['notional_usdt'])
                if p['trigger_price'] != fill or p['filled_quantity'] != qty or p['status_after'] != 'POSITION_OPENED' or p['lifecycle_seq'] != 1:
                    raise ValueError('boundary opening geometry mismatch')
                costs = candle_boundary_costs(entry_fill=fill, quantity=qty, fee_rate=data['policy']['fee_rate'])
                accrued = take('cost:'+pid, 'COST_ACCRUED', at)
                if accrued != dict(position_id=pid, amount_usdt=costs['entry_fee'],
                        cause_event_key=_key(data['run_id'],order,'open'), cost_source=COST_SOURCE):
                    raise ValueError('boundary open cost accrual mismatch')
                inventory[pid] = {**p, 'accrued_cost_usdt':accrued['amount_usdt'],
                                  'open_event_key':_key(data['run_id'],order,'open')}
            else:
                opened = inventory[pid]
                qty, entry = opened['filled_quantity'], opened['trigger_price']
                costs = candle_boundary_costs(entry_fill=entry, quantity=qty,
                    fee_rate=data['policy']['fee_rate'], exit_fill=fill)
                geometry = position_cash_geometry(fill=entry, stop=order['risk_stop'], quantity=qty,
                                                  exit_price=fill, side=order['side'])
                ledger = realized_cash_ledger(gross_pnl=geometry['gross_pnl_usd'], initial_risk=geometry['initial_risk_usd'],
                    costs={'funding_cost' if k=='funding' else k:v for k,v in costs.items()})
                expected = dict(trigger_price=entry, close_price=fill, filled_quantity=qty,
                    gross_pnl_usd=geometry['gross_pnl_usd'], initial_risk_usd=geometry['initial_risk_usd'],
                    total_cost_usd=ledger['total_cost_usd'], net_r=ledger['net_r'], net_pnl_usdt=ledger['net_pnl_usd'],
                    realized_cost_source=COST_SOURCE, realized_evidence_status='COMPLETE',
                    status_after='POSITION_CLOSED', lifecycle_seq=2)
                if any(p[k] != v for k,v in expected.items()) or json.loads(p['realized_costs_json']) != costs:
                    raise ValueError('boundary canonical closure mismatch')
                movement = take('cash:'+pid, 'CASH_CLOSED', at)
                after = sum_cash([cash, ledger['net_pnl_usd']])
                if movement != dict(position_id=pid, net_pnl_usdt=ledger['net_pnl_usd'], before=cash, after=after):
                    raise ValueError('boundary cash conservation mismatch')
                cash = after
                del inventory[pid]
    if cursor != len(events):
        raise ValueError('unscoped/duplicate boundary events')
    return snapshots, gaps


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args(argv)
    packet = run_boundary_backtest(json.loads(Path(args.input).read_text()), args.output_dir)
    print(json.dumps(dict(run_id=packet['identity']['run_id'], status=packet['status'],
                         completion_hash=packet['completion_hash'], gaps=packet['completion']['gaps'])))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
