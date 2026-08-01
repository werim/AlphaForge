from dataclasses import FrozenInstanceError
import pytest

from alphaforge.agents.contracts import *


def envelope(**changes):
    values = dict(decision_id="d1", correlation_id="c1", symbol="BTCUSDT", execution_mode="PAPER",
        stage=AgentStage.RISK, status=DecisionStatus.PASS, input_hash=stable_hash({"a": 1}),
        config_hash=stable_hash({"b": 2}), started_at="2026-08-01T00:00:00Z",
        completed_at="2026-08-01T00:00:01Z", duration_ms=1000, evidence={})
    values.update(changes)
    return DecisionEnvelope(**values)


def test_stable_hash_and_canonical_json_are_deterministic():
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_contracts_are_frozen_and_reasons_normalized():
    item = envelope(reason_codes=(" low_effective_rr ", "HIGH_SPREAD", "LOW_EFFECTIVE_RR"))
    assert item.reason_codes == ("HIGH_SPREAD", "LOW_EFFECTIVE_RR")
    with pytest.raises(FrozenInstanceError):
        item.status = DecisionStatus.REJECT
    with pytest.raises(TypeError):
        item.evidence["new"] = True


def test_reject_requires_reason_and_negative_duration_fails():
    with pytest.raises(ValueError, match="primary_reason"):
        envelope(status=DecisionStatus.REJECT)
    with pytest.raises(ValueError, match="negative"):
        envelope(duration_ms=-0.1)


def test_serialization_round_trip_preserves_hard_reject():
    original = envelope(status=DecisionStatus.REJECT, primary_reason="HIGH_SPREAD",
        reason_codes=("HIGH_SPREAD",), evidence={"hard_reject": True, "spread_pct": None})
    restored = envelope_from_dict(envelope_to_dict(original))
    assert restored == original
    assert restored.hard_reject
    assert restored.evidence["spread_pct"] is None


def test_non_json_evidence_fails_clearly():
    with pytest.raises(TypeError, match="not JSON-serializable"):
        envelope(evidence={"bad": object()})
