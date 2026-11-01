"""Standalone, non-authoritative failed-breakout research labels.

No default hypothesis/horizon, fitted probability, Git identity or runtime hook.
"""
from dataclasses import asdict, dataclass, field
import json
import math
import hashlib
from pathlib import Path

from .backtest_source_evidence import digest
from .signal_geometry import build_breakout_geometry_with_diagnostics

VERSION = 'breakout-failure-label-v1'
STRICT_VERSION = 'breakout-failure-label-v2'
CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
RULES = ('RETURN_TO_LEVEL', 'CLOSE_BACK_INSIDE')


def _integer(value, name, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError('invalid ' + name)


def _hash(value):
    if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('invalid source/config hash')


@dataclass(frozen=True)
class Bar:
    start_ms: int
    end_ms: int  # exclusive boundary at which the candle is complete
    available_ms: int | None
    open: float
    high: float
    low: float
    close: float
    symbol: str
    source_id: str


def _validate_bar(bar, interval):
    if type(bar) is not Bar:
        raise ValueError('unsupported bar')
    if any(type(v) is not str or not v.strip() for v in (bar.symbol, bar.source_id)):
        raise ValueError('missing bar scope')
    for field in ('start_ms', 'end_ms'):
        _integer(getattr(bar, field), field)
    if bar.start_ms % interval or bar.end_ms - bar.start_ms != interval:
        raise ValueError('bar interval mismatch')
    if bar.available_ms is not None:
        _integer(bar.available_ms, 'availability')
        if bar.available_ms < bar.end_ms:
            raise ValueError('bar available before completion')
    values = (bar.open, bar.high, bar.low, bar.close)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError('invalid OHLC')
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.low > bar.high:
        raise ValueError('inconsistent OHLC')


@dataclass(frozen=True)
class Seed:
    candidate_id: str
    symbol: str
    setup_type: str
    side: str
    decision_ms: int
    interval_ms: int
    level: float
    previous: Bar
    current: Bar
    context_json: str

    @property
    def seed_id(self):
        return digest(asdict(self))


def freeze_breakout(*, candidate_id, symbol, setup_type, side, decision_ms,
                    interval_ms, previous, current, context):
    """Bind an actual canonical crossing to data available at candidate creation.

    Context identifies source bytes/venue/product and availability basis; a hash
    binds caller evidence but cannot independently authenticate its publisher.
    """
    _integer(decision_ms, 'decision')
    _integer(interval_ms, 'interval', positive=True)
    if any(type(v) is not str or not v.strip() for v in (candidate_id, symbol)):
        raise ValueError('missing candidate/symbol identity')
    for bar in (previous, current):
        _validate_bar(bar, interval_ms)
        if bar.available_ms is None or bar.available_ms > decision_ms or bar.end_ms > decision_ms:
            raise ValueError('candidate input unavailable at decision')
    if previous.end_ms != current.start_ms or current.end_ms != decision_ms // interval_ms * interval_ms:
        raise ValueError('candidate prefix incomplete/stale')
    required = {'source_id', 'source_hash', 'config_hash', 'venue', 'product', 'availability_basis'}
    if type(context) is not dict or set(context) != required:
        raise ValueError('missing/unknown source context')
    if any(type(v) is not str or not v.strip() for v in context.values()):
        raise ValueError('empty source context')
    _hash(context['source_hash']); _hash(context['config_hash'])
    if any(b.symbol != symbol or b.source_id != context['source_id'] for b in (previous, current)):
        raise ValueError('candidate source scope mismatch')
    if context['availability_basis'] not in ('RECORDED_SOURCE_AVAILABILITY', 'DECLARED_BOUNDARY_MODEL', 'SYNTHETIC_TEST'):
        raise ValueError('unsupported availability basis')
    geometry, reason = build_breakout_geometry_with_diagnostics(asdict(current), asdict(previous))
    if reason or geometry.get('setup_type') != setup_type or geometry.get('side') != side:
        raise ValueError('not a canonical breakout setup/side')
    level = previous.high if side == 'LONG' else previous.low
    crossed = current.close > level if side == 'LONG' else current.close < level
    if not crossed:
        raise ValueError('setup name without actual breakout crossing')
    return Seed(candidate_id, symbol, setup_type, side, decision_ms, interval_ms,
                level, previous, current, json.dumps(context, sort_keys=True, allow_nan=False))


@dataclass(frozen=True)
class LabelPolicy:
    invalidation: str  # mandatory explicit research hypothesis, never defaulted
    horizon_bars: int | None  # None explicitly means research horizon unresolved

    def __post_init__(self):
        if self.invalidation not in RULES:
            raise ValueError('unsupported invalidation hypothesis')
        if self.horizon_bars is not None:
            _integer(self.horizon_bars, 'horizon', positive=True)
            if self.horizon_bars > 10000:
                raise ValueError('research resource bound exceeded')


@dataclass(frozen=True)
class CloseBackInside3Policy:
    """Explicit initial research target, never an optimized/runtime default."""
    timeframe_ms: int
    schema: str = field(default='close-back-inside-3-v1', init=False)
    invalidation: str = field(default='CLOSE_BACK_INSIDE', init=False)
    horizon_bars: int = field(default=3, init=False)
    equality: str = field(default='BOUNDARY_ONLY', init=False)
    window_anchor: str = field(default='AFTER_BREAKOUT_CANDLE', init=False)

    def __post_init__(self):
        _integer(self.timeframe_ms, 'timeframe', positive=True)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or 'timeframe_ms' not in value:
            raise ValueError('missing strict-close policy')
        policy = cls(value['timeframe_ms'])
        if digest(value) != digest(asdict(policy)):
            raise ValueError('strict-close policy identity mismatch')
        return policy


def label_breakout(seed, policy, bars, *, asof_ms):
    """Compute labels on contiguous available forward candles, never a score.

    A single return event needs no TP/SL ordering. Legacy partial decision
    candles remain ambiguous; v2 anchors to the completed confirming candle.
    No intrabar event time is invented. Failure can mature early;
    survival requires all horizon bars. No-return is not profitability/success.
    """
    strict = type(policy) is CloseBackInside3Policy
    if type(seed) is not Seed or type(policy) not in (LabelPolicy, CloseBackInside3Policy):
        raise ValueError('unsupported seed/policy')
    # Validate even caller-constructed or dataclasses.replace-modified seeds.
    expected = freeze_breakout(candidate_id=seed.candidate_id, symbol=seed.symbol,
        setup_type=seed.setup_type, side=seed.side, decision_ms=seed.decision_ms,
        interval_ms=seed.interval_ms, previous=seed.previous, current=seed.current,
        context=json.loads(seed.context_json))
    if digest(asdict(expected)) != digest(asdict(seed)):
        raise ValueError('seed identity mismatch')
    if strict and digest(asdict(policy)) != digest(asdict(CloseBackInside3Policy(seed.interval_ms))):
        raise ValueError('strict-close policy/timeframe mismatch')
    _integer(asof_ms, 'asof')
    if asof_ms < seed.decision_ms:
        raise ValueError('asof before prediction')
    if type(bars) not in (list, tuple) or len(bars) > 10000:
        raise ValueError('unsupported/excessive forward population')
    consumed = []
    boundary_closes = []
    return_touch = None
    window_start = seed.current.end_ms if strict else seed.decision_ms
    due = None if policy.horizon_bars is None else window_start + policy.horizon_bars * seed.interval_ms

    def result(status, failure=None, event_bar=None, missing_start=None):
        prefix_available = max((b.available_ms for b in consumed), default=None)
        payload = dict(schema=STRICT_VERSION if strict else VERSION, seed_id=seed.seed_id, candidate_id=seed.candidate_id,
            algorithm_hash=CODE_HASH,
            policy=asdict(policy), policy_id=digest(asdict(policy)), status=status,
            failed_breakout=failure, decision_ms=seed.decision_ms, due_ms=due,
            event_interval=None if event_bar is None else [event_bar.start_ms, event_bar.end_ms],
            resolved_available_ms=prefix_available if not strict or failure is not None else None,
            consumed_bars=len(consumed), label_source_hash=digest([asdict(b) for b in consumed]),
            probability=None, probability_status='UNAVAILABLE', dependent_evaluation='NOT_EVALUABLE',
            runtime_admission='DISABLED', source_context=json.loads(seed.context_json),
            provenance_status='CALLER_BOUND_NOT_PUBLISHER_AUTHENTICATED')
        if strict:
            payload.update(window_start_ms=window_start, frozen_breakout_level=seed.level,
                policy_selection_status='INITIAL_RESEARCH_ASSUMPTION_NOT_OPTIMIZED',
                native_decision_authority='DISABLED', order_admission='DISABLED',
                observed_prefix_available_ms=prefix_available,
                missing_interval=None if missing_start is None else [missing_start, missing_start+seed.interval_ms],
                boundary_equal_closes=boundary_closes,
                return_to_level=dict(diagnostic_only=True,
                    observed=True if return_touch else (False if failure is not None else None),
                    event_interval=None if return_touch is None else return_touch['event_interval'],
                    available_ms=None if return_touch is None else return_touch['available_ms']))
        return dict(result=payload, result_hash=digest(payload))

    if due is None:
        return result('HORIZON_UNSET')
    if not strict and seed.decision_ms % seed.interval_ms:
        return result('AMBIGUOUS_PREDICTION_BOUNDARY')
    grouped = {}
    for bar in bars:
        if type(bar) is not Bar:
            raise ValueError('unsupported forward bar')
        _integer(bar.start_ms, 'bar start')
        if not window_start <= bar.start_ms < due:
            continue  # warmup and outside-horizon evidence cannot affect labels
        if strict:
            # Validate only the required prefix. Later conflicting/malformed
            # rows cannot reopen an already decisive completed close.
            grouped.setdefault(bar.start_ms // seed.interval_ms * seed.interval_ms, []).append(bar)
            continue
        if bar.available_ms is None:
            continue
        _integer(bar.available_ms, 'availability')
        _integer(bar.end_ms, 'bar end')
        if bar.available_ms > asof_ms or bar.end_ms > asof_ms:
            continue  # future labels must never change an earlier as-of result
        if bar.start_ms % seed.interval_ms:
            raise ValueError('off-grid forward evidence')
        if bar.symbol != seed.symbol or bar.source_id != json.loads(seed.context_json)['source_id']:
            raise ValueError('forward source scope mismatch')
        grouped.setdefault(bar.start_ms, []).append(bar)
    for start in range(window_start, due, seed.interval_ms):
        rows = grouped.get(start, [])
        if strict:
            visible = []
            for row in rows:
                if row.available_ms is None:
                    continue
                _integer(row.available_ms, 'availability')
                _integer(row.end_ms, 'bar end')
                if row.available_ms > asof_ms or row.end_ms > asof_ms:
                    continue
                if row.symbol != seed.symbol or row.source_id != json.loads(seed.context_json)['source_id']:
                    raise ValueError('forward source scope mismatch')
                visible.append(row)
            rows = visible
        if not rows:
            if strict:
                if asof_ms < start+seed.interval_ms:
                    return result('PENDING')
                return result('UNAVAILABLE', missing_start=start)
            return result('PENDING' if asof_ms < due else 'INCOMPLETE_WINDOW')
        for row in rows:
            _validate_bar(row, seed.interval_ms)
        if len({digest(asdict(row)) for row in rows}) != 1:
            return result('AMBIGUOUS_DUPLICATE_SOURCE')
        bar = rows[0]
        consumed.append(bar)
        if strict:
            available = max(b.available_ms for b in consumed)
            if bar.close == seed.level:
                boundary_closes.append(dict(event_interval=[bar.start_ms, bar.end_ms], close=bar.close,
                                            available_ms=available, status='LEVEL_EQUAL_CLOSE'))
            touched = bar.low <= seed.level if seed.side == 'LONG' else bar.high >= seed.level
            if touched and return_touch is None:
                return_touch = dict(event_interval=[bar.start_ms, bar.end_ms], available_ms=available)
        price = bar.close if policy.invalidation == 'CLOSE_BACK_INSIDE' else (bar.low if seed.side == 'LONG' else bar.high)
        failed = ((price < seed.level if seed.side == 'LONG' else price > seed.level) if strict
                  else (price <= seed.level if seed.side == 'LONG' else price >= seed.level))
        if failed:
            return result('FAILED_BREAKOUT', True, bar)
    return result('NO_FAILURE_WITHIN_HORIZON', False)


def replay_label(packet, seed, policy, bars, *, asof_ms, expected_seed_id):
    if expected_seed_id != seed.seed_id:
        raise ValueError('independent seed pin mismatch')
    expected = label_breakout(seed, policy, bars, asof_ms=asof_ms)
    if digest(packet) != digest(expected):
        raise ValueError('label replay mismatch')
    return expected
