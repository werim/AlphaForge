"""Causal native BACKTEST decisions; unsupported exits never become MARKET trades."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import time
import zipfile

from .backtest_chronology import _simulation_module, run_boundary_backtest
from .backtest_source_evidence import digest, evidence_settings
from .config_registry import config_snapshot
from .portfolio_risk import BacktestPortfolioState
from .realized_accounting import finite_cash

VERSION = 'causal-backtest-strategy-v1'
STEP = 60000


def _jsonable(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f'unsupported strategy evidence type {type(value).__name__}')


def _safe(value):
    return json.loads(json.dumps(value,default=_jsonable,allow_nan=False))


def read_historical_archives(directory):
    """Read checksum-bound public 1m spot archives without network or extraction."""
    directory = Path(directory)
    receipts = json.loads((directory/'receipts.json').read_text())
    bars, quotes = [], {}
    if not receipts or len(receipts)>7:
        raise ValueError('missing/excessive historical source receipts')
    observed = time.time_ns()//1000000
    for receipt in receipts:
        name = receipt['filename']
        if Path(name).name != name or not name.startswith('BTCUSDT-1m-') or not name.endswith('.zip'):
            raise ValueError('unsupported historical archive scope')
        expected_url = 'https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1m/'+name
        if receipt['url'] != expected_url or receipt['checksum_url'] != expected_url+'.CHECKSUM':
            raise ValueError('historical receipt URL/scope mismatch')
        raw = (directory/name).read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        published = (directory/(name+'.CHECKSUM')).read_text().split()
        if sha != receipt['sha256'] or not published or published[0]!=sha:
            raise ValueError('historical archive checksum mismatch')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members)!=1 or members[0].filename != name[:-4]+'.csv' or members[0].file_size>2000000:
                raise ValueError('unsupported historical archive population')
            rows = list(csv.reader(io.StringIO(archive.read(members[0]).decode())))
        if len(rows)!=1440:
            raise ValueError('historical daily population incomplete')
        expected_day = int(datetime.strptime(name[len('BTCUSDT-1m-'):-4],'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()*1000)
        if [int(row[0]) for row in rows]!=list(range(expected_day,expected_day+86400000,STEP)):
            raise ValueError('historical date/row population mismatch')
        for row in rows:
            at = int(row[0])
            if str(at)!=row[0] or at%STEP or int(row[6])!=at+STEP-1:
                raise ValueError('invalid historical candle timestamps/units')
            values = [float(row[i]) for i in (1,2,3,4,5,7)]
            if any(finite_cash(v) is None for v in values) or min(values[:4])<=0 or min(values[4:])<0:
                raise ValueError('invalid historical OHLC/volume')
            o,h,l,c,v,q = values
            if h<max(o,c) or l>min(o,c) or h<l or at in quotes:
                raise ValueError('invalid/duplicate historical source row')
            bars.append(dict(timestamp=at,open=o,high=h,low=l,close=c,volume=v))
            quotes[at] = q
    bars.sort(key=lambda b:b['timestamp'])
    labels = [b['timestamp'] for b in bars]
    if labels != list(range(labels[0],labels[-1]+STEP,STEP)):
        raise ValueError('historical source coverage gap')
    return bars, quotes, dict(provider='BINANCE_PUBLIC_SPOT_ARCHIVE', symbol='BTCUSDT',
        interval_ms=STEP, timestamp_basis='UTC_EPOCH_MS_OPEN_AND_INCLUSIVE_CLOSE',
        local_read_observed_ms=observed,remote_retrieval_observed_ms=None,receipts=receipts, normalized_data_hash=digest(bars),
        quote_volume_hash=digest(sorted(quotes.items())), coverage_start_ms=labels[0],coverage_end_ms=labels[-1]+STEP)


def representation_reasons(candidate):
    """Native protective SL/TP is not the executor's reference-stop/scheduled exit."""
    if candidate is None:
        return ['NO_NATIVE_CANDIDATE']
    reasons = []
    if candidate.order_type!='MARKET':
        reasons.append('UNSUPPORTED_NATIVE_ORDER_TYPE')
    if finite_cash(candidate.sl) is not None or finite_cash(candidate.tp) is not None:
        reasons.append('UNSUPPORTED_NATIVE_PROTECTIVE_EXITS')
    # No native strategy contract supplies the scheduled full close required by v1.
    reasons.append('MISSING_NATIVE_SCHEDULED_EXIT_CONTRACT')
    return reasons


def _profile(simulation):
    cfg = simulation.load_config_from_env()
    runtime = cfg.runtime
    return dict(settings=evidence_settings(config_snapshot(mode='BACKTEST',root=simulation.ROOT_DIR)),
        risk_pct=cfg.backtest.risk_pct,
        portfolio_config=simulation.resolve_backtest_portfolio_config(cfg.backtest.initial_balance,root=simulation.ROOT_DIR),
        disabled_backtest_filters=list(simulation._disabled_backtest_filters()),
        min_effective_rr=runtime.min_effective_rr, ai_min_accept_score=runtime.min_signal_score,
        regime_timeframe=runtime.regime_timeframe,setup_timeframe=runtime.setup_timeframe,
        execution_timeframe=runtime.execution_timeframe,
        mtf_guided_signal_generation_enabled=runtime.mtf_guided_signal_generation_enabled,
        regime_direction_threshold=runtime.regime_direction_threshold,
        setup_direction_threshold=runtime.setup_direction_threshold,
        execution_direction_threshold=runtime.execution_direction_threshold,
        enable_state_direction_resolution=runtime.enable_state_direction_resolution)


def evaluate_prefix(simulation, bars, quotes, decision_ms, state, profile, run_id, *, end_ms=None):
    """Only already closed rows reach either selector or native strategy authority."""
    prefix = [b for b in bars if b['timestamp']+STEP<=decision_ms]
    signal_ms = prefix[-1]['timestamp'] if prefix else None
    source_hash = digest(prefix)
    record = dict(decision_id=digest([run_id,'BTCUSDT',decision_ms,source_hash]),symbol='BTCUSDT',
        signal_ms=signal_ms,source_available_ms=None if signal_ms is None else signal_ms+STEP,
        decision_ms=decision_ms,earliest_execution_ms=decision_ms+STEP,execution_ms=None,
        canonical_signal_id=None if signal_ms is None else f'BTCUSDT:{signal_ms}',
        native_scoring_label_ms=signal_ms,
        prefix_hash=source_hash,prefix_count=len(prefix),observed_ms=time.time_ns()//1000000,
        portfolio=dict(cash=state.current_equity,open_positions={k:asdict(v) for k,v in state.open_positions.items()}),
        candidate=None,native_status='REJECT',native_reason='',adapter_reasons=[],mapped_intent_id=None)
    if len(prefix)<3 or [b['timestamp'] for b in prefix]!=list(range(bars[0]['timestamp'],decision_ms,STEP)):
        record.update(native_reason='INCOMPLETE_CLOSED_PREFIX',adapter_reasons=['INCOMPLETE_CLOSED_PREFIX'])
        return record
    # Quote turnover is measured in the same closed historical rows, never a current ticker.
    day = [b for b in prefix if b['timestamp']>=decision_ms-86400000]
    if len(day)!=1440 or any(b['timestamp'] not in quotes for b in day):
        record.update(native_reason='INCOMPLETE_HISTORICAL_24H_VOLUME',adapter_reasons=['INCOMPLETE_HISTORICAL_24H_VOLUME'])
        return record
    meta = dict(symbol='BTCUSDT',quoteVolume=sum(quotes[b['timestamp']] for b in day))
    candles = [simulation.Candle(**b) for b in prefix]
    selector_market = simulation._build_symbol_market_data(meta,candles,len(candles)-1)
    selector = simulation.select_symbol('BTCUSDT',selector_market,
        {'disabled_backtest_filters':tuple(profile['disabled_backtest_filters'])})
    if not selector.tradable:
        record['native_reason'] = selector.reject_reasons[0] if selector.reject_reasons else 'SYMBOL_FILTER_REJECTED'
        return record
    context = {k:v for k,v in profile.items() if k not in ('settings','disabled_backtest_filters')}
    context.update(mode='BACKTEST',balance=state.current_equity,risk_pct=profile['risk_pct'],
        recent_stats={},symbol_meta=meta,disabled_backtest_filters=tuple(profile['disabled_backtest_filters']),
        portfolio_state=state,mtf_source_candles=candles,simulation_timeframe='1m',causal_decision_ms=decision_ms)
    simulation.scan_symbol_backtest('BTCUSDT',candles,len(candles)-1,context)
    result = context.get('last_result',{})
    candidate = result.get('candidate')
    record.update(native_status='ACCEPT' if result.get('accepted') else 'REJECT',
        native_reason=result.get('reject_reason',result.get('reason','STRATEGY_RESULT_UNAVAILABLE')),
        candidate=None if candidate is None else asdict(candidate),
        diagnostics=result.get('diagnostics',{}))
    if record['native_status']=='ACCEPT':
        record['adapter_reasons'] = representation_reasons(candidate)
        if end_ms is not None and decision_ms+STEP>=end_ms:
            record['adapter_reasons'].append('EARLIEST_EXECUTION_OUTSIDE_WINDOW')
        if candidate is not None:
            # Preserve canonical notional semantics; never size from a final balance.
            record['proposed_notional_usdt'] = state.notional_for(entry=candidate.entry,balance=state.current_equity,
                risk_pct=profile['risk_pct'],risk_scale=result.get('diagnostics',{}).get('risk_scale',1.),
                notional=context.get('market_ctx',{}).get('notional'))
    return _safe(record)


def build_strategy_input(bars, quotes, provenance, *, start_ms, end_ms, run_id):
    if type(start_ms) is not int or type(end_ms) is not int or start_ms%STEP or end_ms%STEP or end_ms<=start_ms:
        raise ValueError('invalid strategy window timestamps')
    if end_ms-start_ms>240*STEP or len(bars)>10000:
        raise ValueError('causal strategy capture budget exceeded')
    labels=[b['timestamp'] for b in bars]
    if (not labels or any(type(t) is not int or t<0 or t%STEP for t in labels)
            or labels!=[labels[0]+i*STEP for i in range(len(labels))]):
        raise ValueError('missing/duplicate/out-of-order strategy source candles')
    if any(type(b['timestamp']) is not int for b in bars) or bars[0]['timestamp']>start_ms-86400000 or bars[-1]['timestamp']+STEP<end_ms:
        raise ValueError('insufficient historical feature/window coverage')
    simulation = _simulation_module()
    profile = _profile(simulation)
    capital = simulation.load_config_from_env().backtest.initial_balance
    state = BacktestPortfolioState(initial_equity=capital)
    records = [evaluate_prefix(simulation,bars,quotes,t,state,profile,run_id,end_ms=end_ms) for t in range(start_ms,end_ms,STEP)]
    evidence = dict(schema=VERSION,authority='backtest_order.scan_symbol_backtest',
        feature_bars=[b for b in bars if b['timestamp']<start_ms],
        quote_volumes=[[b['timestamp'],quotes[b['timestamp']]] for b in bars if b['timestamp']<end_ms],
        profile=profile,provenance=provenance,records=records,
        execution_admission='BLOCKED_NATIVE_EXIT_SEMANTICS',scope='NO_EXECUTABLE_NATIVE_INTENTS',
        chronology='CLOSED_PREFIX_DECISION_THEN_STRICT_NEXT_BOUNDARY',
        feature_role='WARMUP_ONLY_NO_EXECUTION',stats_scope='NEW_ISOLATED_SIMULATION_EMPTY_HISTORY')
    data = dict(schema='candle-boundary-backtest-v1',run_id=run_id,start_ms=start_ms,end_ms=end_ms,
        interval_ms=STEP,initial_capital_usdt=capital,data_origin='HISTORICAL_BINANCE_SPOT_ARCHIVE_WITH_NATIVE_STRATEGY',
        policy=dict(product='USDT_LINEAR_NO_FUNDING',fee_rate=.001,slippage_bps=0.,
                    fill_model='NEXT_BOUNDARY_OPEN_FULL_FILL',decision_basis='CAUSAL_NATIVE_STRATEGY',valuation='COMPLETED_CANDLE_CLOSE'),
        candles={'BTCUSDT':[b for b in bars if start_ms<=b['timestamp']<end_ms]},intents=[],strategy_evidence=evidence)
    validate_strategy_evidence(data)
    return data


def validate_strategy_evidence(data, *, recompute=False):
    e = data['strategy_evidence']
    if (e['schema']!=VERSION or e['authority']!='backtest_order.scan_symbol_backtest'
            or e['execution_admission']!='BLOCKED_NATIVE_EXIT_SEMANTICS' or data['intents']
            or data['interval_ms']!=STEP or set(data['candles'])!={'BTCUSDT'}):
        raise ValueError('unsupported/unverified strategy admission')
    bars = e['feature_bars']+data['candles']['BTCUSDT']
    labels = [b['timestamp'] for b in bars]
    if (not labels or len(labels)>10000 or len(e['feature_bars'])<1440
            or any(type(t) is not int or t<0 or t%STEP for t in labels)
            or labels!=[labels[0]+i*STEP for i in range(len(labels))]
            or labels[-1]+STEP!=data['end_ms']):
        raise ValueError('strategy feature/window coverage mismatch')
    if any(b['timestamp']>=data['start_ms'] for b in e['feature_bars']):
        raise ValueError('cross-window strategy feature population')
    if len(bars)>10000 or len(e['records'])>240:
        raise ValueError('strategy verification budget exceeded')
    expected_times = list(range(data['start_ms'],data['end_ms'],STEP))
    if [r['decision_ms'] for r in e['records']]!=expected_times:
        raise ValueError('strategy decision population incomplete/duplicate')
    simulation = _simulation_module() if recompute else None
    if recompute and _profile(simulation)!=e['profile']:
        raise ValueError('strategy configuration drift')
    state = BacktestPortfolioState(initial_equity=data['initial_capital_usdt'])
    quotes = dict(e['quote_volumes'])
    if (len(quotes)!=len(e['quote_volumes']) or set(quotes)!=set(labels)
            or any(type(t) is not int or type(v) not in (int,float) or finite_cash(v) is None or v<0
                   for t,v in e['quote_volumes'])):
        raise ValueError('strategy quote population incomplete/duplicate')
    for record in e['records']:
        t = record['decision_ms']
        prefix = [b for b in bars if b['timestamp']+STEP<=t]
        signal = prefix[-1]['timestamp'] if prefix else None
        if any(type(record[k]) is not int for k in ('decision_ms','signal_ms','source_available_ms','earliest_execution_ms')):
            raise ValueError('invalid strategy timestamp types')
        if (record['prefix_hash']!=digest(prefix) or record['prefix_count']!=len(prefix)
                or record['signal_ms']!=signal or record['source_available_ms']!=signal+STEP
                or record['earliest_execution_ms']!=t+STEP or record['execution_ms'] is not None
                or record['decision_id']!=digest([data['run_id'],'BTCUSDT',t,digest(prefix)])
                or record['mapped_intent_id'] is not None or type(record['observed_ms']) is not int):
            raise ValueError('noncausal/cross-scoped strategy decision')
        if record['native_status']=='ACCEPT':
            candidate = record['candidate']
            if not candidate or not record['adapter_reasons'] or 'MISSING_NATIVE_SCHEDULED_EXIT_CONTRACT' not in record['adapter_reasons']:
                raise ValueError('native order was silently admitted')
        if recompute:
            actual = evaluate_prefix(simulation,bars,quotes,t,state,e['profile'],data['run_id'],end_ms=data['end_ms'])
            if {k:v for k,v in actual.items() if k!='observed_ms'}!={k:v for k,v in record.items() if k!='observed_ms'}:
                raise ValueError('canonical strategy replay mismatch')
    digest(e)


def run_historical_strategy(directory,output_dir,*,start_ms,end_ms,run_id):
    bars, quotes, provenance = read_historical_archives(directory)
    data = build_strategy_input(bars,quotes,provenance,start_ms=start_ms,end_ms=end_ms,run_id=run_id)
    validate_strategy_evidence(data,recompute=True)
    proof = run_boundary_backtest(data,output_dir)
    summary = dict(result='PARTIAL',run_id=run_id,source_status=proof['status'],
        decision_count=len(data['strategy_evidence']['records']),
        native_statuses=dict(Counter(r['native_status'] for r in data['strategy_evidence']['records'])),
        native_reasons=dict(Counter(r['native_reason'] for r in data['strategy_evidence']['records'])),
        adapter_reasons=dict(Counter(reason for r in data['strategy_evidence']['records'] for reason in r['adapter_reasons'])),
        executed_positions=len(proof['completion']['position_population']),
        gaps=proof['completion']['gaps'],git_sha=proof['identity']['git_sha'],
        source_identity_hash=proof['identity_hash'],completion_hash=proof['completion_hash'],
        historical_provenance=provenance,consumer_admission='DISABLED',
        blocker='Canonical native protective exits have no supported boundary-executor representation; no fixed-horizon substitute.')
    Path(output_dir,'strategy_run_summary.json').write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n')
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archives',required=True)
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--start-ms',type=int,required=True)
    parser.add_argument('--end-ms',type=int,required=True)
    parser.add_argument('--run-id',required=True)
    a=parser.parse_args(argv)
    summary=run_historical_strategy(a.archives,a.output_dir,start_ms=a.start_ms,end_ms=a.end_ms,run_id=a.run_id)
    print(json.dumps(summary,sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
