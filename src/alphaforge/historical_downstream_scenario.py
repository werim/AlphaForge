"""Isolated downstream research gates; incomplete evidence never authorizes risk."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .backtest_chronology import _simulation_module
from .backtest_source_evidence import digest
from .backtest_strategy_adapter import STEP
from .execution import build_execution_context, build_decision_rr_metrics, build_execution_cost_model
from .historical_execution_scenario import (ExecutionScenario, OBSERVED_NATIVE,
    MODELED_HISTORICAL_V1, evaluate_scenario)
from . import historical_scoring_evidence
from .historical_scoring_evidence import ScoringAssumptions, input_evidence
from .order import evaluate_signal_decision
from .portfolio_risk import PortfolioRiskSnapshot, evaluate_portfolio_risk, scale_candidate_exposure

VERSION = 'historical-downstream-scenario-v1'
GATE_ORDER = ['SCORING', 'SIGNAL_CONFIRMATION', 'SHARED_QUALITY_AND_EXECUTION_COST', 'REQUESTED_NOTIONAL', 'PORTFOLIO_RISK', 'SIZING']


def _unknown(*reasons):
    return dict(status='NOT_EVALUABLE', reasons=list(reasons))


def _outcome(gates):
    """Only supported canonical rejections/passes can decide research eligibility."""
    if set(gates)!=set(GATE_ORDER):
        raise ValueError('incomplete downstream gate population')
    for name in GATE_ORDER:
        status=gates[name]['status']
        if status=='REJECT':
            return 'DOWNSTREAM_REJECT'
        if status=='NOT_EVALUABLE':
            return 'NOT_EVALUABLE'
        if status!='PASS':
            raise ValueError('invalid downstream gate status')
    return 'SCENARIO_DOWNSTREAM_ALIGNED'


def _evaluate_aligned(sim, data, scenario, native, modeled, scoring_assumptions=None):
    profile = data['strategy_evidence']['profile']
    bars = data['strategy_evidence']['feature_bars']+data['candles']['BTCUSDT']
    prefix = [b for b in bars if b['timestamp']+STEP <= native['decision_ms']]
    if digest(prefix)!=native['prefix_hash'] or native['source_available_ms']>modeled['decision_ms']:
        raise ValueError('noncausal downstream prefix')
    candles = [sim.Candle(**b) for b in prefix]
    quotes = dict(data['strategy_evidence']['quote_volumes'])
    turnover = sum(quotes[b['timestamp']] for b in prefix if b['timestamp']>=native['decision_ms']-86400000)
    market = sim._build_market_ctx(candles[-1],candles[-2],dict(quoteVolume=turnover),candles[-21:])
    mtf = deepcopy(modeled['mtf'])
    geometry = mtf['generation']['candidate']
    if not geometry or mtf['generation']['evidence_status']!='COMPLETE':
        raise ValueError('aligned scenario has no canonical geometry')
    market.update(geometry)
    market.update(symbol='BTCUSDT', mode='BACKTEST', regime=mtf['regime']['regime'],
        setup=geometry.get('setup_type'),timeframe=profile['execution_timeframe'],mtf=mtf,
        decision_timestamp=modeled['decision_ms'],MIN_EFFECTIVE_RR=profile['min_effective_rr'])
    # Explicit scenario estimates only; optional scoring assumptions remain
    # separately tagged. No account values or observed records are supplied.
    for field,value in dict(spread_pct=scenario.spread_pct,expected_slippage_pct=scenario.expected_slippage_pct,
        fee_pct=scenario.fee_pct,liquidity_score=scenario.liquidity_score,
        latency_ms=scenario.order_latency_ms,market_data_latency_ms=scenario.market_data_latency_ms).items():
        market[field]=value
    for stem in ('spread','slippage','fee','liquidity','latency','market_data_latency'):
        market[stem+'_status']='MODEL_ESTIMATE'
        market[stem+'_source']=scenario.scenario_id
    evidence = input_evidence(scenario, scoring_assumptions)
    for field, record in evidence.items():
        if record["kind"] == "MODELED":
            market[field] = record["value"]
    if evidence["funding_rate_pct"]["kind"] == "MODELED":
        market.update(funding_status="MODEL_ESTIMATE", funding_source=scoring_assumptions.policy_id)
    execution = build_execution_context(market)
    market.update(execution)
    # Match native BACKTEST's source-bound stateless scoring and pure pre-submit
    # sequence. These calls submit nothing. Native missing-input arithmetic is
    # diagnostic only: suppress all dependent aggregate scores/RR/decisions below.
    scoring = sim._historical_authoritative_score('BTCUSDT',candles,len(candles)-1,market,
        min_accept_score=profile['ai_min_accept_score'])
    market.update(score=scoring['score'],expectancy=scoring['expectancy'],expectancy_bucket=scoring['expectancy_bucket'])
    config = dict(MODE='BACKTEST',DISABLED_BACKTEST_FILTERS=tuple(profile['disabled_backtest_filters']),
        MIN_TRADE_SCORE=profile['ai_min_accept_score'],**profile['portfolio_config'])
    shared = evaluate_signal_decision(market,config,dict(recent_stats={}),execution,'BACKTEST')
    cost_model = build_execution_cost_model(execution)
    rr = build_decision_rr_metrics(market['rr'],market,execution,mode='BACKTEST')
    missing_score = list(scoring['diagnostics']['historical_scoring_context']['missing_inputs'])
    missing_score += [k for k in ('funding_rate','funding_rate_pct','fakeout_risk') if market.get(k) is None]
    missing_score = sorted(set(missing_score))
    missing_cost = sorted(set(cost_model.missing_fields) | set(shared.diagnostics.get('execution_safety',{}).get('missing_fields',[])))
    scoring_complete = not missing_score
    cost_complete = not missing_cost
    gates = dict(
        SCORING=dict(status=('PASS' if scoring['accepted'] else 'REJECT') if scoring_complete else 'NOT_EVALUABLE',
            reasons=([scoring['reject_reason']] if scoring_complete and not scoring['accepted'] else missing_score),
            score=scoring['score'] if scoring_complete else None,diagnostic_only=not scoring_complete,
            expectancy_after_costs=scoring['expectancy'] if scoring_complete else None,
            # Source-supported components remain useful without promoting the
            # native scorer's defaults for missing funding/fakeout into evidence.
            features={k:(None if k in missing_score else v) for k,v in scoring['diagnostics']['authoritative_components'].items()
                      if k in {'setup_quality','regime_alignment','momentum_confirmation','liquidity_quality','volatility_fit','risk_reward_quality'}},
            threshold=profile['ai_min_accept_score'],history_basis='CANONICAL_FRESH_RUN_PRIOR_NOT_MODELED_PORTFOLIO',
            diagnostic_reason_flags=scoring['diagnostics']['authoritative_reason_flags']),
        SIGNAL_CONFIRMATION=dict(status=('PASS' if scoring['accepted'] else 'REJECT') if scoring_complete else 'NOT_EVALUABLE',
            reasons=[] if scoring_complete and scoring['accepted'] else ['SCORING_NOT_CONFIRMED'],
            execution_order_type=None),
        SHARED_QUALITY_AND_EXECUTION_COST=dict(
            status=('PASS' if shared.decision=='ACCEPT' else 'REJECT') if scoring_complete and cost_complete else 'NOT_EVALUABLE',
            reasons=([shared.reject_reason] if scoring_complete and cost_complete and shared.decision!='ACCEPT'
                     else sorted(set(missing_score+missing_cost))),
            decision_flow='AFTER_SCORING_CONFIRMATION',
            diagnostic_only=not(scoring_complete and scoring['accepted'] and cost_complete),
            diagnostic_primary_reject=shared.reject_reason,
            diagnostic_failed_quality_predicates=shared.diagnostics.get('all_failed_gates',[]),
            diagnostic_execution_flags=shared.diagnostics.get('execution_flags',[]),
            expected_fill_estimate=rr['expected_fill'], candidate_rr=rr['candidate_rr'],
            executable_raw_rr=rr['executable_raw_rr'],effective_rr=rr['effective_rr'] if cost_complete else None,
            cost_feasibility='EVALUABLE' if cost_complete else 'NOT_EVALUABLE',missing_cost_fields=missing_cost,
            estimated_components={k:(None if k=='funding_penalty' and execution.get('funding_rate_pct') is None else v)
                                  for k,v in asdict(cost_model).items() if k.endswith('_penalty') and k!='total_penalty'},
            total_penalty=cost_model.total_penalty if cost_complete else None,
            remaining_execution_penalty=rr['remaining_execution_penalty'] if cost_complete else None,
            actual_fill=None,booked_fee=None),
    )
    # The native flat audit is NOT a counterfactual position sequence. Do not
    # construct BacktestPortfolioState or substitute opening capital/final cash.
    gates['REQUESTED_NOTIONAL'] = dict(**_unknown('MISSING_ATTRIBUTABLE_CURRENT_BALANCE'),
        authority='BacktestPortfolioState.notional_for',notional=None,
        decision_flow='BEFORE_PORTFOLIO_SNAPSHOT_NO_STATE_OBJECT_AVAILABLE')
    snapshot = PortfolioRiskSnapshot(mode='BACKTEST',
        timestamp=datetime.fromtimestamp(modeled['decision_ms']/1000,timezone.utc).isoformat(),
        risk_state_complete=False,risk_state_source='UNAVAILABLE_MODELED_CHRONOLOGICAL_STATE',
        risk_state_missing_fields=['equity','available_balance','positions','cash_flows','risk_history'])
    risk = evaluate_portfolio_risk(geometry,snapshot,config,mode='BACKTEST')
    size = scale_candidate_exposure(original_notional=None,risk_scale=None,require_scale=True)
    gates['PORTFOLIO_RISK'] = dict(**_unknown('MISSING_CHRONOLOGICAL_MODELED_PORTFOLIO_STATE'),
        canonical_reject_reason=risk.reject_reason,canonical_risk_flags=risk.risk_flags,
        decision_flow='REQUIRES_SUPPORTED_SCORING_AND_PRE_SUBMIT',
        equity=None,available_balance=None,open_position_count=None,notional=None)
    gates['SIZING'] = dict(**_unknown('MISSING_ATTRIBUTABLE_BALANCE_AND_CANDIDATE_NOTIONAL'),
        canonical_projection=size,effective_notional=None,quantity=None,risk_scale=None,
        decision_flow='REQUIRES_SUPPORTED_RISK_AND_ATTRIBUTABLE_NOTIONAL')
    return dict(status=_outcome(gates),gates=gates,gate_order=list(GATE_ORDER),
        unavailable_evidence={k:g['reasons'] for k,g in gates.items() if g['status']=='NOT_EVALUABLE'},
        gate_input_hash=digest(dict(prefix=prefix,scenario=asdict(scenario),profile=profile,scoring_evidence=evidence)),
        prefix_hash=digest(prefix),model_decision_ms=modeled['decision_ms'],
        measured_market_data_latency_ms=None,measured_order_latency_ms=None,
        native_authorization='DISABLED',executed_intent=None,
        unsupported_exit_reasons=list(modeled['unsupported_exit_reasons']),
        scoring_input_evidence=evidence,
        inputs=dict(funding_rate_pct=execution.get('funding_rate_pct'),orderbook_imbalance=execution.get('orderbook_imbalance'),
                    fee_pct=execution['fee_pct'],expected_slippage_pct=execution['expected_slippage_pct'],
                    latency_ms=execution['latency_ms'],evidence_kind='SCENARIO_ASSUMPTION'))


def evaluate_downstream(data, *, mode=OBSERVED_NATIVE, scenario=None, scoring_assumptions=None):
    if scoring_assumptions is not None:
        if mode != MODELED_HISTORICAL_V1 or scenario is None or type(scoring_assumptions) is not ScoringAssumptions:
            raise ValueError("scoring assumptions require explicit modeled scenario")
        scoring_assumptions.bind(scenario)
    parent = evaluate_scenario(data,mode=mode,scenario=scenario)
    rows=[]
    sim=_simulation_module() if any(m['status']=='MTF_ALIGNED' for m in parent['modeled']) else None
    for native,modeled in zip(parent['observed_native'],parent['modeled']):
        evaluation = (_evaluate_aligned(sim,data,scenario,native,modeled,scoring_assumptions) if modeled['status']=='MTF_ALIGNED'
                      else dict(status=modeled['status'],gates={},gate_order=[],reasons=list(modeled['reasons'])))
        rows.append(dict(native_decision_id=native['decision_id'],scenario_decision_id=modeled['decision_id'],
            observed_native_status=native['native_status'],observed_native_reason=native['native_reason'],
            modeled_mtf_status=modeled['status'],**evaluation))
    return dict(scoring_assumptions=None if scoring_assumptions is None else asdict(scoring_assumptions),
        scoring_policy_id=None if scoring_assumptions is None else scoring_assumptions.policy_id,
        schema=VERSION,mode=parent['mode'],mtf_evaluation=parent,downstream=rows,
        parent_result_hash=digest(parent),input_hash=digest(data),scenario_id=parent['scenario_id'],
        code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        scoring_evidence_code_hash=hashlib.sha256(Path(historical_scoring_evidence.__file__).read_bytes()).hexdigest(),
        git_sha=parent['git_sha'],
        consumer_admission='DISABLED',order_admission='DISABLED',capital_source_status='NOT_PRODUCED',
        capital_return_pct=None,max_drawdown_pct=None)


def persist_downstream(path,data,*,mode=OBSERVED_NATIVE,scenario=None,scoring_assumptions=None):
    result=evaluate_downstream(data,mode=mode,scenario=scenario,scoring_assumptions=scoring_assumptions)
    packet=dict(result=result,result_hash=digest(result))
    with Path(path).open('x') as f:
        json.dump(packet,f,indent=2,sort_keys=True,allow_nan=False);f.write('\n')
    return packet


def replay_downstream(packet,data,*,expected_input_hash,scenario=None,scoring_assumptions=None):
    if expected_input_hash!=digest(data):
        raise ValueError('downstream input pin mismatch')
    mode=OBSERVED_NATIVE if scenario is None else MODELED_HISTORICAL_V1
    expected=evaluate_downstream(data,mode=mode,scenario=scenario,scoring_assumptions=scoring_assumptions)
    if packet!=dict(result=expected,result_hash=digest(expected)):
        raise ValueError('downstream replay mismatch/promotion')
    return expected


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True);p.add_argument('--scenario');p.add_argument('--output',required=True)
    p.add_argument('--scoring-assumptions')
    a=p.parse_args(argv);data=json.loads(Path(a.input).read_text())
    scenario=None if a.scenario is None else ExecutionScenario.from_dict(json.loads(Path(a.scenario).read_text()))
    assumptions=None if a.scoring_assumptions is None else ScoringAssumptions.from_dict(json.loads(Path(a.scoring_assumptions).read_text()))
    mode=OBSERVED_NATIVE if scenario is None else MODELED_HISTORICAL_V1
    packet=persist_downstream(a.output,data,mode=mode,scenario=scenario,scoring_assumptions=assumptions)
    replay_downstream(packet,data,expected_input_hash=digest(data),scenario=scenario,scoring_assumptions=assumptions)
    print(json.dumps(dict(mode=mode,statuses=dict(Counter(r['status'] for r in packet['result']['downstream'])),
                         git_sha=packet['result']['git_sha'],order_admission='DISABLED'),sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
