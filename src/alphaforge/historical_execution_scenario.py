"""Opt-in MTF scenario research; never an order or capital-evidence producer."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import math
from pathlib import Path
import subprocess

from .backtest_chronology import _simulation_module, validate_input
from .backtest_source_evidence import digest
from .backtest_strategy_adapter import STEP, validate_strategy_evidence
from .multi_timeframe import build_mtf_candidate_context

OBSERVED_NATIVE = 'OBSERVED_NATIVE'
MODELED_HISTORICAL_V1 = 'MODELED_HISTORICAL_V1'
VERSION = 'historical-execution-scenario-v1'
LIMITATIONS = [
    'MTF_PREDICATES_AND_GEOMETRY_ONLY', 'DOWNSTREAM_SCORING_RISK_AND_COST_GATES_NOT_EVALUATED',
    'FEE_DECLARED_NOT_BOOKED', 'NO_HISTORICAL_FILL_ACCURACY', 'NO_EXECUTABLE_NATIVE_SL_TP',
    'NO_CAPITAL_CONSUMER_ADMISSION', 'NO_MEASURED_LATENCY_OR_ORDERBOOK',
    'MARKET_RTT_AND_ORDER_LATENCY_EACH_LIMITED_TO_LESS_THAN_ONE_MINUTE',
]


@dataclass(frozen=True)
class ExecutionScenario:
    schema: str
    name: str
    source_provider: str
    venue: str
    product: str
    instrument: str
    input_hash: str
    native_config_hash: str
    start_ms: int
    end_ms: int
    assumptions_effective_from_ms: int
    market_data_latency_ms: int
    order_latency_ms: int
    spread_pct: float
    expected_slippage_pct: float
    liquidity_score: float
    fee_pct: float

    def __post_init__(self):
        if (self.schema != VERSION or type(self.name) is not str or not self.name.strip()
                or len(self.name) > 100 or self.source_provider != 'BINANCE_PUBLIC_SPOT_ARCHIVE'
                or (self.venue, self.product, self.instrument) != ('BINANCE', 'SPOT', 'BTCUSDT')):
            raise ValueError('unsupported scenario identity/scope')
        for value in (self.input_hash, self.native_config_hash):
            if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('invalid scenario hash')
        for value in (self.start_ms, self.end_ms, self.assumptions_effective_from_ms,
                      self.market_data_latency_ms, self.order_latency_ms):
            if type(value) is not int or value < 0:
                raise ValueError('invalid scenario timestamp/latency milliseconds')
        if (self.start_ms % STEP or self.end_ms % STEP or self.end_ms <= self.start_ms
                or self.end_ms-self.start_ms > 240*STEP or self.assumptions_effective_from_ms > self.start_ms
                or self.market_data_latency_ms >= STEP or self.order_latency_ms >= STEP):
            raise ValueError('inconsistent/unsupported scenario timing')
        for value in (self.spread_pct, self.expected_slippage_pct, self.liquidity_score, self.fee_pct):
            if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
                raise ValueError('scenario rates must be finite fractions in [0,1]')
        if max(self.spread_pct, self.expected_slippage_pct, self.fee_pct) >= 1:
            raise ValueError('scenario cost rates must be less than one')

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
            raise ValueError('missing/unknown scenario fields')
        return cls(**value)

    @property
    def scenario_id(self):
        return digest(asdict(self))


def _bind(data, scenario):
    validate_input(data)
    validate_strategy_evidence(data, recompute=True)
    e = data['strategy_evidence']
    p = e['provenance']
    if scenario is not None and (scenario.input_hash != digest(data)
            or scenario.native_config_hash != digest(e['profile'])
            or (scenario.start_ms, scenario.end_ms) != (data['start_ms'], data['end_ms'])
            or p.get('provider') != scenario.source_provider or p.get('symbol') != scenario.instrument
            or p.get('interval_ms') != STEP):
        raise ValueError('scenario source/input/config/window mismatch')


def evaluate_scenario(data, *, mode=OBSERVED_NATIVE, scenario=None):
    """Keep original decisions intact; model only canonical MTF, never ACCEPT orders.

    Caller pins input provenance. Hashes bind bytes, not independent authenticity.
    A frozen audit is verified by canonical native replay before any modeled result.
    """
    if mode not in (OBSERVED_NATIVE, MODELED_HISTORICAL_V1):
        raise ValueError('unknown evaluation mode')
    if (mode == OBSERVED_NATIVE and scenario is not None) or (mode == MODELED_HISTORICAL_V1 and type(scenario) is not ExecutionScenario):
        raise ValueError('scenario requires explicit modeled opt-in')
    _bind(data, scenario)
    e = data['strategy_evidence']
    observed = deepcopy(e['records'])
    result = dict(schema=VERSION, mode=mode, input_hash=digest(data), native_config_hash=digest(e['profile']),
        observed_native=observed, modeled=[], scenario=None, scenario_id=None,
        historical_source=deepcopy(e['provenance']), strategy_authority=e['authority'],
        source_window=dict(start_ms=data['start_ms'],end_ms=data['end_ms'],
                           feature_only_rows=len(e['feature_bars'])),
        consumer_admission='DISABLED', order_admission='DISABLED', capital_source_status='NOT_PRODUCED',
        capital_return_pct=None, max_drawdown_pct=None, limitations=list(LIMITATIONS),
        unsupported_fields=['funding_rate_pct', 'orderbook_imbalance', 'submit_ack_latency_ms'],
        assumption_basis='EXPLICIT_SCENARIO_NOT_EMPIRICALLY_CALIBRATED',
        units=dict(latency='MILLISECONDS', rates='FRACTIONS', time='UTC_EPOCH_MILLISECONDS'),
        source_authentication='CALLER_PINNED_INPUT_REQUIRES_INDEPENDENT_SOURCE_VERIFICATION',
        git_sha=subprocess.check_output(['git','rev-parse','HEAD'],
            cwd=Path(__file__).resolve().parents[2], text=True).strip(),
        code_hash=digest({n:hashlib.sha256((Path(__file__).resolve().parents[2]/n).read_bytes()).hexdigest()
                          for n in ('backtest_order.py', *('src/alphaforge/'+m+'.py' for m in
                          ('historical_execution_scenario','multi_timeframe','backtest_strategy_adapter',
                           'ai_brain','order','symbol_selector','execution','portfolio_risk')))}))
    if scenario is None:
        return result
    result.update(scenario=asdict(scenario), scenario_id=scenario.scenario_id,
        timing_policy='ASSUMED_CLOSE_PLUS_MARKET_RTT_THEN_STRICT_NEXT_BOUNDARY_AFTER_ORDER_LATENCY')
    sim = _simulation_module()
    profile = e['profile']
    bars = e['feature_bars'] + data['candles']['BTCUSDT']
    for native in observed:
        at = native['decision_ms']
        modeled_at = at + scenario.market_data_latency_ms
        earliest = ((modeled_at+scenario.order_latency_ms)//STEP+1)*STEP
        record = dict(downstream_status='NOT_EVALUATED', decision_id=digest([scenario.scenario_id,native['decision_id']]),
            native_decision_id=native['decision_id'], signal_ms=native['signal_ms'],
            source_release_ms=at, assumed_available_ms=modeled_at, decision_ms=modeled_at,
            earliest_execution_ms=earliest, execution_ms=None, mapped_intent_id=None,
            evidence_kind='SCENARIO_ASSUMPTION', measured_market_data_latency_ms=None,
            measured_order_latency_ms=None, status='UPSTREAM_REJECT', reasons=[native['native_reason']],
            mtf=None, unsupported_exit_reasons=['UNSUPPORTED_NATIVE_PROTECTIVE_EXITS',
                                              'MISSING_NATIVE_SCHEDULED_EXIT_CONTRACT'],
            execution_boundary_in_window=earliest < data['end_ms'])
        # A selector/earlier gate rejection must not be bypassed by MTF modeling.
        if native.get('diagnostics', {}).get('mtf') is not None:
            prefix = [b for b in bars if b['timestamp']+STEP <= at]
            if digest(prefix) != native['prefix_hash']:
                raise ValueError('noncausal modeled prefix')
            layers = sim._closed_mtf_layers_from_1m([sim.Candle(**b) for b in prefix],
                decision_ts_ms=at-1, regime_timeframe=profile['regime_timeframe'],
                setup_timeframe=profile['setup_timeframe'], execution_timeframe=profile['execution_timeframe'])
            market = dict(spread_pct=scenario.spread_pct, expected_slippage_pct=scenario.expected_slippage_pct,
                market_data_latency_ms=scenario.market_data_latency_ms, liquidity_score=scenario.liquidity_score,
                latency_ms=None, orderbook_imbalance=None, fee_pct=scenario.fee_pct,
                market_data_latency_status='MODEL_ESTIMATE', market_data_latency_source=scenario.scenario_id)
            mtf = build_mtf_candidate_context(layers, execution_ctx=market, decision_ts_ms=modeled_at,
                regime_timeframe=profile['regime_timeframe'], setup_timeframe=profile['setup_timeframe'],
                execution_timeframe=profile['execution_timeframe'],
                state_direction_resolution_enabled=profile['enable_state_direction_resolution'],
                regime_direction_threshold=profile['regime_direction_threshold'],
                setup_direction_threshold=profile['setup_direction_threshold'],
                execution_direction_threshold=profile['execution_direction_threshold'],
                guided_signal_generation_enabled=profile['mtf_guided_signal_generation_enabled'],
                provider='MODELED_HISTORICAL_V1')
            reasons = list(mtf['alignment']['reasons'])
            if mtf['generation'].get('reason') and mtf['generation']['reason'] not in reasons:
                reasons.append(mtf['generation']['reason'])
            mtf['execution'].update(evidence_kind='SCENARIO_ASSUMPTION',
                predicate_input_status=mtf['execution']['evidence_status'],
                evidence_status='MODELED_NOT_OBSERVED', market_data_latency_status='MODEL_ESTIMATE',
                market_data_latency_source=scenario.scenario_id, measured_market_data_latency_ms=None)
            record.update(status='MTF_ALIGNED' if mtf['alignment']['aligned'] else 'MTF_REJECT',
                reasons=reasons, mtf=mtf)
        result['modeled'].append(record)
    return result


def persist_scenario(path, data, *, mode=OBSERVED_NATIVE, scenario=None):
    result = evaluate_scenario(data, mode=mode, scenario=scenario)
    packet = dict(result=result, result_hash=digest(result))
    # Separate research artifact: never write into a native/source evidence packet.
    with Path(path).open('x') as f:
        json.dump(packet, f, indent=2, sort_keys=True, allow_nan=False)
        f.write('\n')
    return packet


def replay_scenario(packet, data, *, expected_input_hash, scenario=None):
    if expected_input_hash != digest(data):
        raise ValueError('independent scenario input pin mismatch')
    mode = OBSERVED_NATIVE if scenario is None else MODELED_HISTORICAL_V1
    expected = evaluate_scenario(data, mode=mode, scenario=scenario)
    # Python equality aliases JSON booleans/numbers; artifact types are identity.
    if digest(packet) != digest(dict(result=expected, result_hash=digest(expected))):
        raise ValueError('scenario replay mismatch/promotion')
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='Frozen causal native input.json')
    parser.add_argument('--scenario', help='Explicit versioned assumption JSON; omitted = observed only')
    parser.add_argument('--output', required=True, help='New research JSON file; never overwritten')
    a = parser.parse_args(argv)
    data = json.loads(Path(a.input).read_text())
    scenario = None if a.scenario is None else ExecutionScenario.from_dict(json.loads(Path(a.scenario).read_text()))
    mode = OBSERVED_NATIVE if scenario is None else MODELED_HISTORICAL_V1
    p = persist_scenario(a.output, data, mode=mode, scenario=scenario)
    replay_scenario(p, data, expected_input_hash=digest(data), scenario=scenario)
    print(json.dumps(dict(mode=mode, input_hash=digest(data), scenario_id=p['result']['scenario_id'],
        observed=dict(Counter(r['native_reason'] for r in p['result']['observed_native'])),
        modeled=dict(Counter(r['status'] for r in p['result']['modeled'])),
        order_admission='DISABLED', consumer_admission='DISABLED'), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
