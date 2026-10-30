"""Observation-only transport of breakout inputs; never trading authority.

Uses the historical receipt convention: local observation and remote retrieval
are separate. Local hashes bind supplied bytes, not publisher authentication.
The label authority remains breakout_failure_labels.freeze_breakout.
"""
from copy import deepcopy
from datetime import datetime
import math

from .backtest_source_evidence import digest
from .signal_geometry import build_breakout_geometry_with_diagnostics

VERSION = 'breakout-input-receipt-v1'


def capture_breakout_receipt(rows, *, symbol, timeframe, source_url, interval_ms, observed_ms):
    """Retain the actual fetched pair, including original open/close timestamps.

    Failure affects only this diagnostic. Do not repair absent exchange fields
    from timeframe assumptions, invent remote receipts, or change geometry.
    """
    try:
        if type(observed_ms) is not int or observed_ms < 0:
            raise ValueError('invalid observation')
        if type(interval_ms) is not int or interval_ms <= 0:
            raise ValueError('invalid interval')
        pair = deepcopy(rows[-3:-1])
        if len(pair) != 2 or any(not isinstance(r, list) or len(r) < 7 for r in pair):
            raise ValueError('missing original candle timestamps')
        for r in pair:
            if type(r[0]) is not int or type(r[6]) is not int or not r[0] <= r[6] < observed_ms:
                raise ValueError('unclosed/unverifiable candle')
            if r[0] % interval_ms or r[6]+1-r[0] != interval_ms:
                raise ValueError('unverifiable candle scope')
            o,h,l,c = map(float,r[1:5])
            if not all(math.isfinite(v) and v > 0 for v in (o,h,l,c)) or not l <= min(o,c) <= max(o,c) <= h:
                raise ValueError('invalid source OHLC')
        geometry, reason = build_breakout_geometry_with_diagnostics(
            dict(zip(('open','high','low','close'),pair[1][1:5])),
            dict(zip(('open','high','low','close'),pair[0][1:5])))
        if reason or not symbol or not timeframe or not source_url:
            raise ValueError('missing source scope')
        if pair[0][6]+1 != pair[1][0]:
            raise ValueError('noncontiguous source pair')
        # Copy of the canonical previous-high/low reference, not a new rule.
        level = float(pair[0][2 if geometry['side'] == 'LONG' else 3])
        crossed = geometry['entry'] > level if geometry['side'] == 'LONG' else geometry['entry'] < level
        return dict(version=VERSION,status='RECORDED_LOCAL_OBSERVATION',
            symbol=symbol,timeframe=timeframe,interval_ms=interval_ms,url=source_url,source_hash=digest(pair),
            local_read_observed_ms=observed_ms,remote_retrieval_observed_ms=None,
            raw_candles=pair,geometry=geometry,breakout_level=level,actual_crossing=crossed,
            hash_basis='CANONICAL_JSON_DECODED_CANDLE_PAIR',
            timestamp_basis='UTC_EPOCH_MS_OPEN_AND_INCLUSIVE_CLOSE',
            provenance_status='LOCAL_OBSERVATION_NOT_PUBLISHER_AUTHENTICATED')
    except (ValueError,TypeError,KeyError,IndexError,OverflowError):
        return dict(version=VERSION,status='UNAVAILABLE',reason='SOURCE_RECEIPT_INCOMPLETE')


def _decision_ms(value):
    if type(value) is int:
        return value
    if not isinstance(value,str):
        raise ValueError('missing decision time')
    d = datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None:
        raise ValueError('unscoped decision time')
    return int(d.timestamp()*1000)


def associate_breakout_receipt(receipt, candidate_id):
    """Bind when the existing runtime ID is assigned; never invent or replace it.

    The cached source object is untouched. An existing different association
    remains visible and will be refused by the persistence adapter.
    """
    if not isinstance(receipt,dict):
        return receipt
    associated = deepcopy(receipt)
    if not associated.get('candidate_id'):
        associated['candidate_id'] = candidate_id
    return associated


def bind_breakout_provenance(payload):
    """Attach an observed input to the already assigned canonical signal ID.

    Guided candidates never inherit the legacy scanner's breakout evidence.
    This records observability, not research or trading admission. Label input
    validation, independent provenance and calibration remain separate gates.
    """
    mtf = payload.get('mtf') if isinstance(payload.get('mtf'),dict) else {}
    generation = mtf.get('generation') if isinstance(mtf.get('generation'),dict) else {}
    shadow_evidence = mtf.get('shadow_evidence') if isinstance(mtf.get('shadow_evidence'),dict) else {}
    shadow = shadow_evidence.get('candidate') or payload.get('legacy_shadow_geometry') or {}
    if not isinstance(shadow,dict):
        shadow = {}
    guided = generation.get('mode') == 'REGIME_GUIDED'
    subject = 'LEGACY_SCANNER_SHADOW_CANDIDATE' if guided else 'NATIVE_BREAKOUT_CANDIDATE'
    candidate = shadow if guided else payload
    receipt = candidate.get('breakout_input_receipt')
    if receipt is None:
        receipt = payload.get('breakout_input_receipt')
    if receipt is None and not guided:
        subject = 'LEGACY_CANDIDATE_WITHOUT_RECEIPT'
    result = dict(version=VERSION,candidate_id=payload.get('signal_id'),subject=subject,
                  receipt=deepcopy(receipt),status='UNAVAILABLE',
                  research_eligibility='NOT_EVALUABLE',probability_status='UNAVAILABLE')
    try:
        if not isinstance(receipt,dict) or receipt.get('status') != 'RECORDED_LOCAL_OBSERVATION':
            raise ValueError('SOURCE_RECEIPT_UNAVAILABLE')
        if not isinstance(result['candidate_id'],str) or not result['candidate_id'].strip():
            raise ValueError('CANDIDATE_ID_UNAVAILABLE')
        if receipt.get('candidate_id') != result['candidate_id']:
            raise ValueError('SOURCE_CANDIDATE_ID_MISMATCH')
        # Recompute through the same producer function; retain the original
        # recorded observation time, never replace it with persistence time.
        rows = receipt['raw_candles'] + [[]]
        expected = capture_breakout_receipt(rows,symbol=receipt['symbol'],timeframe=receipt['timeframe'],
            source_url=receipt['url'],interval_ms=receipt['interval_ms'],observed_ms=receipt['local_read_observed_ms'])
        if digest(expected) != digest({k:v for k,v in receipt.items() if k != 'candidate_id'}):
            raise ValueError('SOURCE_RECEIPT_INTEGRITY_MISMATCH')
        if payload.get('symbol') != receipt['symbol'] or payload.get('timeframe') != receipt['timeframe']:
            raise ValueError('SOURCE_SCOPE_MISMATCH')
        fields = ('side','setup_type','entry','sl','tp')
        if not candidate or digest({k:candidate.get(k) for k in fields}) != digest({k:receipt['geometry'].get(k) for k in fields}):
            raise ValueError('CANDIDATE_GEOMETRY_MISMATCH')
        decision = _decision_ms(payload.get('decision_time') or payload.get('decision_timestamp'))
        if receipt['local_read_observed_ms'] > decision:
            raise ValueError('INPUT_UNAVAILABLE_AT_DECISION')
        if not receipt['actual_crossing']:
            raise ValueError('NO_ACTUAL_BREAKOUT_CROSSING')
        result.update(status='BOUND_LOCAL_OBSERVATION',decision_ms=decision)
    except (ValueError,TypeError,KeyError,IndexError,OverflowError) as exc:
        result['reason'] = str(exc) if isinstance(exc,ValueError) else 'MALFORMED_SOURCE_RECEIPT'
    return result
