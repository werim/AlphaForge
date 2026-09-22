import json
import sqlite3

import pytest

from alphaforge.adaptive_decision_calibration import HealthEvidence, calibrate_campaign
from alphaforge.burnin import BurnInRun, persist_burnin_observation, persist_burnin_reject_outcome, persist_burnin_run, persist_burnin_trade_outcome
from alphaforge.burnin_campaign import bootstrap_campaign_schema
from alphaforge.burnin_resolver import persist_pending_position, persist_pending_reject_label
from alphaforge.order import OrderCandidate, evaluate_trade_quality


HEALTHY = HealthEvidence(True, True, True, True)
BASE = {"MIN_TRADE_SCORE": 0.62, "MIN_EFFECTIVE_RR": 1.6}


@pytest.fixture
def dbs():
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    output = sqlite3.connect(":memory:")
    output.row_factory = sqlite3.Row
    bootstrap_campaign_schema(source)
    persist_burnin_run(source, BurnInRun("run", "rel", phase="PHASE8", git_commit="g",
                                      config_hash="c", strategy_config_hash="s", universe_hash="u",
                                      source_provenance={"provider": "PAPER"}))
    source.execute("""INSERT INTO burnin_campaigns
      (campaign_id,release_id,campaign_status,created_at,config_hash,strategy_config_hash,
       universe_hash,git_commit,source_provenance_json,symbols_json,intervals_json,schema_version)
      VALUES ('camp','rel','COMPLETED','2026-01-01T00:00:00Z','c','s','u','g','{}','[]','[]','v')""")
    source.execute("""INSERT INTO burnin_campaign_runs
      (campaign_id,burnin_run_id,continuation_sequence,status,started_at,created_at,schema_version)
      VALUES ('camp','run',0,'COMPLETED','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','v')""")
    yield source, output
    source.close()
    output.close()


def _stamp(i):
    return f"2026-01-{1 + i // 100:02d}T{i % 100 // 60:02d}:{i % 60:02d}:00Z"


def _reject(source, i, net, *, reason="LOW_SCORE", symbol="ETHUSDT", side="LONG",
            regime="TRENDING", attributable=True, ambiguous=False, invalidated=False,
            complete=True, score=0.61, outcome=True):
    decision_id = f"reject-{i}"
    stamp = _stamp(i)
    persist_burnin_observation(source, observation_id=f"obs-{decision_id}", burnin_run_id="run",
        release_id="rel", execution_mode="PAPER", observed_at=stamp, symbol=symbol,
        regime=regime, decision="REJECTED", lifecycle_state="SIGNAL_REJECTED",
        metrics={"reject_decision_id": decision_id, "signal_id": f"signal-{i}",
                 "primary_reject_reason": reason, "score": score, "effective_rr": 1.7,
                 "campaign_id": "camp"})
    pending_id = persist_pending_reject_label(source, campaign_id="camp", burnin_run_id="run",
        reject_decision_id=decision_id, signal_id=f"signal-{i}", symbol=symbol, side=side,
        decision_timestamp=stamp, entry=100, stop=110 if side == "SHORT" else 90,
        target=80 if side == "SHORT" else 120, horizon_seconds=60,
        execution_cost_assumptions={}, regime=regime, reject_reason=reason,
        source_provenance={"reject_execution_basis": "EXPECTED_FILL_RUNTIME_PARITY",
                           "reject_quality_attributable": attributable})
    if outcome:
        persist_burnin_reject_outcome(source, reject_outcome_id=f"rout_{decision_id}",
            burnin_run_id="run", release_id="rel", reject_reason=reason, symbol=symbol,
            regime=regime, decision_time=stamp, evidence_horizon=stamp,
            forward_label="AMBIGUOUS" if ambiguous else ("TP_BEFORE_SL" if net > 0 else "SL_BEFORE_TP"),
            would_tp=net > 0, would_sl=net < 0, ambiguous=ambiguous,
            hypothetical_net_r_after_costs=net, execution_invalidated=invalidated,
            evidence_complete=complete,
            payload={"campaign_id": "camp", "burnin_run_id": "run",
                     "reject_decision_id": decision_id, "pending_label_id": pending_id,
                     "reject_quality_attributable": attributable,
                     "reject_execution_basis": "EXPECTED_FILL_RUNTIME_PARITY",
                     "execution_aligned": True})


def _accept(source, i, net, *, symbol="ETHUSDT", side="LONG", regime="TRENDING",
            score=0.63, outcome=True):
    signal_id = f"accepted-{i}"
    stamp = _stamp(i)
    persist_burnin_observation(source, observation_id=f"obs-{signal_id}", burnin_run_id="run",
        release_id="rel", execution_mode="PAPER", observed_at=stamp, symbol=symbol,
        regime=regime, decision="ACCEPTED", lifecycle_state="POSITION_OPENED",
        metrics={"signal_id": signal_id, "score": score, "effective_rr": 1.8,
                 "campaign_id": "camp"})
    pending_id = persist_pending_position(source, trade_id=f"trade-{i}", campaign_id="camp",
        burnin_run_id="run", signal_id=signal_id, symbol=symbol, side=side,
        entry_time=stamp, planned_entry=100, simulated_fill=100, stop=90, target=120,
        quantity=1, notional=100, entry_spread=0.002, entry_slippage=0.002,
        entry_fee=0.002, regime=regime, source_provenance={"execution_cost_unit": "R"})
    if outcome:
        costs = {field: 0.001 for field in ("spread_cost", "entry_slippage_cost",
                 "exit_slippage_cost", "fee_cost", "funding_cost", "latency_cost")}
        persist_burnin_trade_outcome(source, outcome_id=f"tout_trade-{i}",
            burnin_run_id="run", release_id="rel", trade_id=f"trade-{i}",
            symbol=symbol, regime=regime, closed_at=stamp, gross_r=net+0.006,
            costs=costs, net_r=net, exit_reason="TP_HIT" if net > 0 else "SL_HIT",
            payload={"signal_id": signal_id, "pending_position_id": pending_id})


def _scope(output, path, reason, **dimensions):
    for row in output.execute("SELECT * FROM adaptive_decision_calibration WHERE decision_dimension=? AND reason_or_gate=?", (path, reason)):
        scope = json.loads(row["scope_json"])
        if scope == {"reason": reason, **dimensions}:
            return row
    raise AssertionError("scope missing")


def _run(dbs, *, health=HEALTHY, now="2026-02-01T00:00:00Z"):
    return calibrate_campaign(*dbs, "camp", base_thresholds=BASE, health=health, now=now)


def test_accepted_and_rejected_share_net_r_framework_and_shadow_is_separate(dbs):
    source, output = dbs
    for i in range(35):
        _reject(source, i, 0.4)
        _accept(source, i, -0.4)
    before = tuple(source.execute("SELECT COUNT(*) FROM burnin_observations").fetchone())
    result = _run(dbs)
    assert result == {"decisions": 70, "diagnostic_outcomes": 0, "scopes": 8, "attributable_outcomes": 70}
    reject = _scope(output, "REJECTED", "LOW_SCORE", symbol="ETHUSDT", regime="TRENDING", side="LONG")
    accept = _scope(output, "ACCEPTED", "ACCEPTED", symbol="ETHUSDT", regime="TRENDING", side="LONG")
    assert (reject["calibration_state"], accept["calibration_state"]) == ("RELAXED", "TIGHT")
    assert reject["lower_confidence_bound"] > 0 and accept["upper_confidence_bound"] < 0
    assert reject["shadow_value"] == pytest.approx(0.60)
    assert accept["shadow_value"] == pytest.approx(0.64)
    assert output.execute("SELECT COUNT(*) FROM adaptive_shadow_decisions WHERE adaptive_shadow_decision='WOULD_REJECT_UNDER_TIGHTENED_THRESHOLD'").fetchone()[0] == 35
    assert output.execute("SELECT COUNT(*) FROM adaptive_shadow_decisions WHERE adaptive_shadow_decision='WOULD_PASS_CALIBRATED_GATE_OTHER_GATES_UNVERIFIED'").fetchone()[0] == 35
    assert tuple(source.execute("SELECT COUNT(*) FROM burnin_observations").fetchone()) == before
    assert source.execute("SELECT name FROM sqlite_master WHERE name LIKE 'adaptive_%'").fetchone() is None


def test_insufficient_and_crossing_zero_never_adjust(dbs):
    source, output = dbs
    for i in range(29):
        _reject(source, i, 0.5)
    _run(dbs)
    assert _scope(output, "REJECTED", "LOW_SCORE")["calibration_state"] == "INSUFFICIENT_SAMPLE"
    for i in range(29, 58):
        _reject(source, i, -0.5)
    _run(dbs, now="2026-02-02T00:00:00Z")
    assert _scope(output, "REJECTED", "LOW_SCORE")["calibration_state"] == "NORMAL"


@pytest.mark.parametrize("hazard", ["diagnostic", "ambiguous", "invalidated", "incomplete"])
def test_bad_forward_evidence_cannot_loosen(dbs, hazard):
    source, output = dbs
    for i in range(35):
        _reject(source, i, 0.4, **({"attributable": False} if hazard == "diagnostic" and i == 0 else
                                  {"ambiguous": True} if hazard == "ambiguous" and i == 0 else
                                  {"invalidated": True} if hazard == "invalidated" and i == 0 else
                                  {"complete": False} if hazard == "incomplete" and i == 0 else {}))
    _run(dbs)
    row = _scope(output, "REJECTED", "LOW_SCORE")
    assert row["calibration_state"] == "FROZEN"
    assert row["sample_count"] == 34
    assert row["shadow_value"] == BASE["MIN_TRADE_SCORE"]


@pytest.mark.parametrize("health", [
    HealthEvidence(False, True, True, True), HealthEvidence(True, False, True, True),
    HealthEvidence(True, True, False, True), HealthEvidence(True, True, True, False),
    HealthEvidence(),
])
def test_system_health_blocks_loosening(dbs, health):
    source, output = dbs
    for i in range(35):
        _reject(source, i, 0.4)
    _run(dbs, health=health)
    assert _scope(output, "REJECTED", "LOW_SCORE")["calibration_state"] == "FROZEN"


def test_scope_fallback_isolation_bounds_and_replay(dbs):
    source, output = dbs
    for i in range(30):
        _reject(source, i, 0.4, symbol="ETHUSDT", side="LONG")
    for i in range(30, 60):
        _reject(source, i, -0.4, symbol="BTCUSDT", side="SHORT")
    _reject(source, 60, 0.4, symbol="ETHUSDT", side="SHORT")
    first = _run(dbs)
    assert first["decisions"] == 61
    assert _scope(output, "REJECTED", "LOW_SCORE", symbol="ETHUSDT", regime="TRENDING", side="LONG")["calibration_state"] == "RELAXED"
    assert _scope(output, "REJECTED", "LOW_SCORE", symbol="BTCUSDT", regime="TRENDING", side="SHORT")["calibration_state"] == "TIGHT"
    fallback = output.execute("SELECT calibration_key FROM adaptive_shadow_decisions WHERE decision_id='reject-60'").fetchone()[0]
    scope = json.loads(output.execute("SELECT scope_json FROM adaptive_decision_calibration WHERE calibration_key=?", (fallback,)).fetchone()[0])
    assert scope == {"reason": "LOW_SCORE", "symbol": "ETHUSDT", "regime": "TRENDING"}
    assert _scope(output, "REJECTED", "LOW_SCORE", symbol="ETHUSDT", regime="TRENDING", side="LONG")["shadow_value"] >= 0.57
    counts = [output.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in
              ("adaptive_decision_observations", "adaptive_decision_calibration_events", "adaptive_shadow_decisions")]
    _run(dbs)
    assert counts == [output.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in
                      ("adaptive_decision_observations", "adaptive_decision_calibration_events", "adaptive_shadow_decisions")]


def test_orphan_reject_and_unclosed_accept_are_monitored_without_attribution(dbs):
    source, output = dbs
    _reject(source, 1, 0.5, outcome=False)
    _accept(source, 2, -0.5, outcome=False)
    _run(dbs)
    assert output.execute("SELECT COUNT(*) FROM adaptive_decision_observations").fetchone()[0] == 2
    assert output.execute("SELECT SUM(attributable) FROM adaptive_decision_observations").fetchone()[0] == 0
    assert output.execute("SELECT SUM(sample_count) FROM adaptive_decision_calibration").fetchone()[0] == 0


def test_orphan_outcome_is_diagnostic_and_cannot_influence_scope(dbs):
    source, output = dbs
    for i in range(35):
        _reject(source, i, -0.4)
    persist_burnin_reject_outcome(source, reject_outcome_id="rout_orphan", burnin_run_id="run",
        release_id="rel", reject_reason="LOW_SCORE", symbol="ETHUSDT", regime="TRENDING",
        forward_label="TP_BEFORE_SL", hypothetical_net_r_after_costs=100,
        evidence_complete=True, payload={"campaign_id":"camp", "burnin_run_id":"run",
                                         "reject_decision_id":"orphan", "pending_label_id":"missing"})
    result = _run(dbs)
    assert result["diagnostic_outcomes"] == 1
    assert _scope(output, "REJECTED", "LOW_SCORE")["calibration_state"] == "TIGHT"
    assert output.execute("SELECT attributable FROM adaptive_decision_observations WHERE decision_id='diagnostic:rout_orphan'").fetchone()[0] == 0


def test_opposite_state_requires_normal_hysteresis_step(dbs):
    source, output = dbs
    for i in range(35):
        _reject(source, i, 0.4)
    _run(dbs)
    for i in range(35):
        source.execute("UPDATE burnin_reject_outcomes SET hypothetical_net_r_after_costs=-0.4,forward_label='SL_BEFORE_TP' WHERE reject_outcome_id=?", (f"rout_reject-{i}",))
    _run(dbs, now="2026-02-03T00:00:00Z")
    row = _scope(output, "REJECTED", "LOW_SCORE")
    assert row["calibration_state"] == "NORMAL"
    assert "HYSTERESIS_OPPOSITE_STATE" in row["guardrails_json"]
    assert output.execute("SELECT COUNT(*) FROM adaptive_decision_observations").fetchone()[0] == 70


def test_same_file_source_and_output_is_rejected(tmp_path):
    path = tmp_path / "same.db"
    source = sqlite3.connect(path)
    output = sqlite3.connect(path)
    try:
        with pytest.raises(ValueError, match="separate database files"):
            calibrate_campaign(source, output, "camp")
    finally:
        source.close()
        output.close()


def test_existing_campaign_output_is_rejected(dbs):
    source, output = dbs
    output.execute("CREATE TABLE burnin_campaigns (campaign_id TEXT)")
    with pytest.raises(ValueError, match="contains campaign tables"):
        _run(dbs)


@pytest.mark.parametrize("mode", ["BACKTEST", "PAPER", "LIVE"])
def test_shadow_calibration_does_not_change_shared_decision_path(dbs, mode):
    source, output = dbs
    for i in range(35):
        _reject(source, i, 0.4)
        _accept(source, i, -0.4)
    candidate = OrderCandidate(symbol="ETHUSDT", side="LONG", setup_type="BREAKOUT_UP",
        setup_reason="MEASURED", regime="BREAKOUT", score=0.8, rr=2.0,
        expectancy=0.2, entry=100, sl=99, tp=103)
    market = {"regime":"BREAKOUT", "volatility_regime":"normal", "effective_rr":1.9,
              "spread_pct":0.0005, "expected_slippage_pct":0.0005, "atr_pct":0.5,
              "liquidity_score":0.9, "orderbook_imbalance":0.1, "funding_rate_pct":0.0001}
    config = {"MODE":mode, "MIN_TRADE_SCORE":0.62, "MIN_RR":1.2,
              "MIN_EFFECTIVE_RR":1.6, "BLOCK_UNKNOWN_EXPECTANCY":True}
    before = evaluate_trade_quality(candidate, market, {}, config)
    _run(dbs)
    after = evaluate_trade_quality(candidate, market, {}, config)
    assert (after.accepted, after.reject_reason, after.diagnostics) == (before.accepted, before.reject_reason, before.diagnostics)


def test_effective_rr_shadow_step_is_bounded_and_mtf_gate_stays_static(dbs):
    source, output = dbs
    for i in range(35):
        _reject(source, i, 0.4, reason="LOW_EFFECTIVE_RR")
        _reject(source, i+100, 0.4, reason="MTF_EXECUTION_COUNTER_REGIME")
    _run(dbs)
    rr = _scope(output, "REJECTED", "LOW_EFFECTIVE_RR")
    mtf = _scope(output, "REJECTED", "MTF_EXECUTION_COUNTER_REGIME")
    assert rr["calibration_state"] == "RELAXED" and rr["shadow_value"] == pytest.approx(1.55)
    assert mtf["calibration_state"] == "RELAXED" and mtf["shadow_value"] is None
    assert "NO_SAFE_NUMERIC_GATE" in mtf["guardrails_json"]
    assert output.execute("SELECT COUNT(*) FROM adaptive_shadow_decisions WHERE reason_or_gate='MTF_EXECUTION_COUNTER_REGIME' AND adaptive_shadow_decision != 'UNCHANGED'").fetchone()[0] == 0


def test_cooldown_blocks_rapid_reentry_after_freeze(dbs):
    source, output = dbs
    for i in range(35):
        _reject(source, i, 0.4)
    _run(dbs, now="2026-02-01T00:00:00Z")
    _run(dbs, health=HealthEvidence(False, True, True, True), now="2026-02-01T01:00:00Z")
    assert _scope(output, "REJECTED", "LOW_SCORE")["calibration_state"] == "FROZEN"
    _run(dbs, now="2026-02-01T02:00:00Z")
    row = _scope(output, "REJECTED", "LOW_SCORE")
    assert row["calibration_state"] == "FROZEN"
    assert "ADJUSTMENT_COOLDOWN" in row["guardrails_json"]
