"""Deterministic metamorphic checks for #421 P2-C safety contracts."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from alphaforge.burnin_resolver import evaluate_forward_outcome
from alphaforge.effective_rr import calculate_effective_rr
from alphaforge.execution import weighted_average_fill_price
from alphaforge.expectancy_evidence import fetch_expectancy_as_of, record_expectancy_evidence
from alphaforge.persistence import init_db
from alphaforge.runtime import RuntimeOrchestrator


def test_seeded_geometry_keeps_finite_nonnegative_rr_and_adverse_fill_never_improves_it():
    rng = random.Random(421)
    for case in range(96):
        side = "LONG" if case % 2 == 0 else "SHORT"
        entry = rng.uniform(10.0, 100_000.0)
        risk = entry * rng.uniform(0.001, 0.12)
        reward = risk * rng.uniform(0.3, 6.0)
        stop = entry - risk if side == "LONG" else entry + risk
        target = entry + reward if side == "LONG" else entry - reward
        adverse_fill = entry + reward * 0.2 if side == "LONG" else entry - reward * 0.2
        before = RuntimeOrchestrator._fill_adjusted_raw_rr(
            side=side, fill=entry, stop=stop, target=target,
        )
        after = RuntimeOrchestrator._fill_adjusted_raw_rr(
            side=side, fill=adverse_fill, stop=stop, target=target,
        )
        assert before is not None and after is not None, case
        assert math.isfinite(before) and math.isfinite(after), case
        assert 0 < after <= before, case


def test_extreme_but_finite_geometry_cannot_publish_infinite_rr():
    rr = RuntimeOrchestrator._fill_adjusted_raw_rr(
        side="LONG", fill=1e-100, stop=1e-100 - 1e-110, target=1e300,
    )
    assert rr == 0.0  # Unrepresentable ratio fails closed at the RR boundary.


def test_seeded_partial_fill_reordering_and_splitting_preserve_price():
    rng = random.Random(422)
    for case in range(96):
        rows = [{"qty": rng.uniform(0.001, 100), "price": rng.uniform(1, 100_000)}
                for _ in range(rng.randint(2, 12))]
        original = weighted_average_fill_price(rows)
        shuffled = list(rows)
        rng.shuffle(shuffled)
        split = [dict(row) for row in rows[1:]] + [
            {"qty": rows[0]["qty"] / 2, "price": rows[0]["price"]},
            {"qty": rows[0]["qty"] / 2, "price": rows[0]["price"]},
        ]
        assert original is not None and math.isfinite(original), case
        expected = math.fsum(row["price"] * row["qty"] for row in rows) / math.fsum(
            row["qty"] for row in rows)
        assert original == pytest.approx(expected, rel=1e-12), case
        assert min(row["price"] for row in rows) <= original <= max(row["price"] for row in rows), case
        assert weighted_average_fill_price(shuffled) == pytest.approx(original, rel=1e-12), case
        assert weighted_average_fill_price(split) == pytest.approx(original, rel=1e-12), case
        assert sum(row["qty"] for row in split) == pytest.approx(
            sum(row["qty"] for row in rows), rel=1e-12,
        ), case


def test_large_finite_partial_fills_do_not_turn_into_nan():
    result = weighted_average_fill_price([
        {"qty": 1e308, "price": 1e308},
        {"qty": 1e308, "price": 1e308},
    ])
    assert result == 1e308
    assert math.isfinite(result)


@pytest.mark.parametrize("field", ["qty", "price"])
def test_boolean_fill_ledger_value_is_invalid_not_numeric_one(field):
    row = {"qty": 1.0, "price": 100.0, field: True}
    with pytest.raises(ValueError, match="finite positive"):
        weighted_average_fill_price([row])


def test_seeded_adverse_execution_cost_cannot_improve_effective_rr():
    rng = random.Random(423)
    for case in range(96):
        ctx = {
            "spread_pct": rng.uniform(0.0001, 0.001),
            "expected_slippage_pct": rng.uniform(0.0001, 0.001),
            "latency_ms": rng.uniform(1, 200),
            "funding_rate_pct": rng.uniform(0.00001, 0.0005),
            "fee_pct": rng.uniform(0.0001, 0.001),
            "liquidity_score": rng.uniform(0.4, 0.95),
            "volatility_regime": "normal",
        }
        baseline = calculate_effective_rr(5.0, ctx).effective_rr
        assert math.isfinite(baseline) and baseline > 0, case
        adverse_changes = {
            "spread_pct": ctx["spread_pct"] + 0.0002,
            "expected_slippage_pct": ctx["expected_slippage_pct"] + 0.0002,
            "latency_ms": ctx["latency_ms"] + 100,
            "funding_rate_pct": ctx["funding_rate_pct"] + 0.0002,
            "fee_pct": ctx["fee_pct"] + 0.0002,
            "liquidity_score": ctx["liquidity_score"] - 0.1,
            "volatility_regime": "high",
        }
        for field, adverse in adverse_changes.items():
            changed = calculate_effective_rr(5.0, {**ctx, field: adverse}).effective_rr
            assert changed <= baseline, (case, field)


def test_seeded_future_candles_cannot_change_current_resolver_outcome():
    rng = random.Random(424)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for case in range(48):
        side = "LONG" if case % 2 == 0 else "SHORT"
        entry = rng.uniform(50, 500)
        stop = entry - 10 if side == "LONG" else entry + 10
        target = entry + 20 if side == "LONG" else entry - 20
        candles = [
            {"timestamp": (start + timedelta(minutes=index)).isoformat(),
             "high": entry + 2, "low": entry - 2}
            for index in (1, 2)
        ]
        future = {"timestamp": (start + timedelta(minutes=3)).isoformat(),
                  "high": entry + 25, "low": entry - 25}
        before_decision = {"timestamp": (start - timedelta(minutes=1)).isoformat(),
                           "high": entry + 25, "low": entry - 25}
        args = dict(side=side, entry=entry, stop=stop, target=target,
                    decision_timestamp=start.isoformat(),
                    due_at=(start + timedelta(minutes=2)).isoformat(),
                    timeframe="1m", horizon_bars=2)
        before = evaluate_forward_outcome(**args, candles=candles)
        after = evaluate_forward_outcome(**args, candles=candles + [future])
        assert before == after, case
        assert before == evaluate_forward_outcome(
            **args, candles=[before_decision, *candles]), case


def test_seeded_foreign_identity_rows_cannot_change_scoped_expectancy():
    rng = random.Random(425)
    scope = {"run_id": "run-a", "campaign_id": "camp-a", "release_id": "release-a"}
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        def record(case: str, net_r: float, identity: dict[str, str]) -> None:
            assert record_expectancy_evidence(
                session, evidence_id=case, source_decision_id="decision:" + case,
                evidence_type="ACCEPTED_TRADE", decision_time="2026-01-01T00:00:00Z",
                resolved_at="2026-01-01T01:00:00Z", symbol="BTCUSDT", side="LONG",
                setup_type="BREAKOUT_UP", regime="TREND", reject_reason=None,
                net_r=net_r, **identity,
            )

        record("current", 0.5, scope)
        for case in range(48):
            foreign = dict(scope)
            field = rng.choice(tuple(scope))
            foreign[field] = f"foreign-{case}"
            record(f"foreign-{case}", rng.uniform(-10, 10), foreign)
        session.commit()
        stats = fetch_expectancy_as_of(
            session, as_of="2026-01-01T01:00:00Z", symbol="BTCUSDT",
            setup_type="BREAKOUT_UP", regime="TREND", **scope,
        )
        assert stats["sample_size"] == 1
        assert stats["symbol"] == {"BTCUSDT": 0.5}
    engine.dispose()
