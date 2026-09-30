from alphaforge.burnin_qualification import BurnInQualificationEngine, BurnInThresholds
from alphaforge.multi_timeframe import build_regime_context, closed_candles
from alphaforge.regime_authority import (
    ACTIVE_REGIME_STATES,
    REGIME_AUTHORITY_VERSION,
    REGIME_STATES,
    REGIME_TRANSITION_POLICY,
    is_qualifiable_regime,
    regime_definition,
)

DECISION_MS = 2_000_000_000_000


def _candles_for_delta(delta: float, *, rows: int = 20):
    assert rows <= 20
    recent = 60.0 * (1.0 + delta) / (0.6 - 0.4 * delta)
    closes = ([100.0] * 12 + [recent] * 8)[-rows:]
    candles = []
    for index, close in enumerate(closes):
        close_ts = DECISION_MS - (len(closes) - index) * 3_600_000
        candles.append({
            "open_ts": close_ts - 3_599_999,
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": 100.0,
            "close_ts": close_ts,
        })
    return candles


def _metric(regime: str, samples: int = 20):
    return {
        "regime": regime,
        "sample_count": samples,
        "lower_confidence_bound_expectancy": 0.1,
        "status": "PASS",
    }


def test_regime_contract_declares_vocabulary_without_promoting_unvalidated_states():
    assert set(REGIME_STATES) == {
        "TRENDING", "MEAN_REVERTING", "CHOPPY", "PANIC", "LOW_LIQUIDITY",
        "BREAKOUT", "SHORT_SQUEEZE", "RANGE_COMPRESSION", "NEWS_DRIVEN", "UNKNOWN",
    }
    assert ACTIVE_REGIME_STATES == {"TRENDING", "CHOPPY"}
    for state in set(REGIME_STATES) - ACTIVE_REGIME_STATES - {"UNKNOWN"}:
        assert is_qualifiable_regime(state) is False
        assert regime_definition(state)["trade_policy"] == "NO_TRADE_UNTIL_VALIDATED"
    assert regime_definition("NEWS_DRIVEN")["activation_status"] == "UNAVAILABLE_NO_AUTHORITATIVE_EVENT_SOURCE"
    assert REGIME_TRANSITION_POLICY["clock"] == "CLOSED_CANDLE_ONLY"
    assert REGIME_TRANSITION_POLICY["future_evidence"] == "REJECT"


def test_classifier_preserves_legacy_trending_and_choppy_but_separates_classification_completeness():
    trending = build_regime_context(_candles_for_delta(0.001), "1h")
    choppy = build_regime_context(_candles_for_delta(0.0001), "1h")
    unavailable = build_regime_context(_candles_for_delta(0.001, rows=19), "1h")

    assert trending["regime"] == "TRENDING"
    assert trending["direction"] == "LONG"
    assert trending["classification_evidence_status"] == "COMPLETE"
    assert trending["evidence_status"] == "COMPLETE"
    assert trending["qualification_eligible"] is True
    assert trending["trade_policy"] == "REGIME_GUIDED_LEGACY"

    assert choppy["regime"] == "CHOPPY"
    assert choppy["direction"] == "NEUTRAL"
    assert choppy["classification_evidence_status"] == "COMPLETE"
    assert choppy["evidence_status"] == "INCOMPLETE"
    assert choppy["qualification_eligible"] is True
    assert choppy["trade_policy"] == "NO_TRADE"

    assert unavailable["regime"] == "UNKNOWN"
    assert unavailable["classification_evidence_status"] == "INCOMPLETE"
    assert unavailable["qualification_eligible"] is False
    assert unavailable["trade_policy"] == "NO_TRADE_FAIL_CLOSED"


def test_regime_boundary_uses_existing_threshold_and_declares_no_new_hysteresis_band():
    below = build_regime_context(_candles_for_delta(0.00049), "1h")
    above = build_regime_context(_candles_for_delta(0.00051), "1h")

    assert below["regime"] == "CHOPPY"
    assert above["regime"] == "TRENDING"
    assert below["hysteresis_policy"] == "LEGACY_SINGLE_DIRECTION_THRESHOLD_NO_SECONDARY_BAND"
    assert above["hysteresis_policy"] == below["hysteresis_policy"]


def test_future_candle_cannot_change_regime_and_news_is_not_fabricated():
    baseline = _candles_for_delta(0.001)
    provider_rows = [
        [c["open_ts"], c["open"], c["high"], c["low"], c["close"], c["volume"], c["close_ts"]]
        for c in baseline
    ]
    provider_rows.append([
        DECISION_MS + 1, 100.0, 10_000.0, 1.0, 9_999.0, 999_999.0, DECISION_MS + 3_600_000,
    ])

    selected = closed_candles(provider_rows, timeframe="1h", decision_ts_ms=DECISION_MS)
    projected = build_regime_context(selected, "1h")
    expected = build_regime_context(baseline, "1h")

    assert projected == expected
    assert projected["regime"] != "NEWS_DRIVEN"
    assert projected["regime_authority_version"] == REGIME_AUTHORITY_VERSION


def test_frozen_closed_candle_event_projects_identically_for_applicable_modes():
    frozen = _candles_for_delta(-0.001)
    paper_projection = build_regime_context([dict(c) for c in frozen], "1h")
    live_precheck_projection = build_regime_context([dict(c) for c in frozen], "1h")

    assert paper_projection == live_precheck_projection
    assert paper_projection["direction"] == "SHORT"


def test_qualification_counts_only_authoritative_regime_coverage():
    engine = BurnInQualificationEngine(
        None,
        BurnInThresholds(minimum_regime_sample=20, minimum_regime_coverage=3),
    )
    blockers = []
    metrics = {}

    status = engine._check_regimes(
        [_metric("TRENDING"), _metric("CHOPPY"), _metric("BREAKOUT")],
        blockers,
        metrics,
    )

    assert status == "INSUFFICIENT"
    assert metrics["regime_authority_version"] == REGIME_AUTHORITY_VERSION
    assert metrics["qualifiable_regime_coverage"] == 2
    assert metrics["qualifiable_regimes"] == ["CHOPPY", "TRENDING"]
    assert metrics["excluded_regime_samples"] == {"BREAKOUT": 20}
    assert "UNQUALIFIED_REGIME_AUTHORITY:BREAKOUT" in blockers
    assert "INSUFFICIENT_REGIME_COVERAGE" in blockers
