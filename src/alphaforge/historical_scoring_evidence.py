"""Explicit research assumptions, never observations or product exemptions."""
from dataclasses import asdict, dataclass, fields
import math

from .backtest_source_evidence import digest

VERSION = 'historical-scoring-assumptions-v1'


@dataclass(frozen=True)
class ScoringAssumptions:
    schema: str
    scenario_id: str
    effective_from_ms: int
    funding_rate_fraction: float | None
    fakeout_risk: float | None
    rationale: str

    def __post_init__(self):
        if self.schema != VERSION or not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError('unsupported assumptions schema/rationale')
        if (type(self.scenario_id) is not str or len(self.scenario_id) != 64
                or any(c not in '0123456789abcdef' for c in self.scenario_id)):
            raise ValueError('invalid scenario identity')
        if type(self.effective_from_ms) is not int or self.effective_from_ms < 0:
            raise ValueError('invalid assumptions timing')
        for name, value in (('funding', self.funding_rate_fraction), ('fakeout', self.fakeout_risk)):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)
                    or (not -1 < value < 1 if name == 'funding' else not 0 <= value <= 1)):
                raise ValueError('invalid signed funding fraction/fakeout probability')
        if self.funding_rate_fraction is None and self.fakeout_risk is None:
            raise ValueError('empty assumptions')

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
            raise ValueError('missing/unknown assumptions fields')
        return cls(**value)

    @property
    def policy_id(self):
        return digest(asdict(self))

    def bind(self, scenario):
        if self.scenario_id != scenario.scenario_id or self.effective_from_ms > scenario.start_ms:
            raise ValueError('assumptions scenario/scope mismatch or future availability')


def input_evidence(scenario, policy):
    """No observed producer exists for this SPOT archive. Null stays unknown."""
    if policy is not None:
        policy.bind(scenario)
    records = {}
    for field, value, units, reason in (
        ('funding_rate', None if policy is None else policy.funding_rate_fraction,
         'SIGNED_FRACTION', 'USD_M_SOURCE_INCOMPATIBLE_WITH_SPOT_NO_NATIVE_EXEMPTION'),
        ('funding_rate_pct', None if policy is None else policy.funding_rate_fraction,
         'SIGNED_FRACTION', 'USD_M_SOURCE_INCOMPATIBLE_WITH_SPOT_NO_NATIVE_EXEMPTION'),
        ('fakeout_risk', None if policy is None else policy.fakeout_risk,
         'PROBABILITY_0_1', 'NO_CANONICAL_CAUSAL_FAKEOUT_PRODUCER')):
        records[field] = dict(value=value, kind='UNAVAILABLE' if value is None else 'MODELED', units=units,
            source=None if value is None else policy.policy_id,
            available_from_ms=None if value is None else policy.effective_from_ms,
            observed_at_ms=None, reason=reason if value is None else 'EXPLICIT_RESEARCH_ASSUMPTION',
            venue=scenario.venue, product=scenario.product, instrument=scenario.instrument,
            input_hash=scenario.input_hash, scenario_id=scenario.scenario_id)
    return records
