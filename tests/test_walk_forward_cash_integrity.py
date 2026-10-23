"""SQL-backed production closures must not become unweighted OOS returns."""
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from test_issue506_walk_forward import bo, _contract, _production_backtest_lifecycle, _row
from alphaforge.historical_validation import historical_rows_from_backtest_decisions
from alphaforge.walk_forward import build_validation_report


def ms(month, day):
    return int(datetime(2026, month, day, tzinfo=timezone.utc).timestamp()*1000)


def sql_rows(tmp_path, positions=((1, "TP", 1), (10, "SL", 1))):
    lifecycle = _production_backtest_lifecycle(ms(1, 15))
    for day, (risk_pct, outcome, scale) in enumerate(positions, 15):
        lifecycle += _production_backtest_lifecycle(ms(2, day), risk_pct=risk_pct,
                                                    outcome=outcome, risk_scale=scale)
    db = f"sqlite+pysqlite:///{tmp_path / 'closures.sqlite'}"
    bo._persist_lifecycle_rows(lifecycle, database_url=db, run_id="cash-regression")
    return bo._decision_evidence_rows(db, run_id="cash-regression")


def report(rows):
    return build_validation_report(_contract(), historical_rows_from_backtest_decisions(rows),
                                   min_effective_rr=1.6)


def oos(result):
    return result["summaries"]["by_role_window_segment"][1]


def test_positive_percentage_sum_cannot_hide_negative_sql_cash(tmp_path):
    rows = sql_rows(tmp_path)
    terminal = [r for r in rows if r["lifecycle_state_after"] == "POSITION_CLOSED"]
    assert sum(r["net_pnl_pct"] for r in terminal[1:]) > 0
    assert sum(r["net_pnl_usdt"] for r in terminal[1:]) == pytest.approx(-.7048)
    result = report(rows)
    assert result["historical_validation"] == "FAIL"
    assert oos(result)["net_pnl_usdt"] == pytest.approx(-.7048)
    assert oos(result)["capital_return_pct"] is None
    assert oos(result)["net_return"] is None


def test_positive_cash_without_window_capital_is_incomplete(tmp_path):
    result = report(sql_rows(tmp_path, ((10, "TP", 1), (1, "SL", 1))))
    assert result["historical_validation"] == "INCOMPLETE"
    assert result["promotion_evidence"]["historical_oos"] == "INCOMPLETE"
    assert oos(result)["net_pnl_usdt"] == pytest.approx(2.8952)
    assert oos(result)["capital_return_pct"] is None
    assert oos(result)["equity_curve"] is None
    assert oos(result)["max_drawdown_pct"] is None
    assert result["schema_version"] == "walk_forward_validation_v2"


@pytest.mark.parametrize("positions", [
    ((1, "TP", 1), (1, "TP", 1)),
    ((1, "TP", 1), (10, "TP", 1)),
    ((1, "TP", 1), (10, "SL", 1)),
    ((10, "TP", 1), (1, "SL", 1)),
    ((10, "TP", 1), (1, "SL", 1), (1, "SL", 1), (1, "SL", 1)),
    ((1, "TP", .5), (10, "SL", 1)),
    ((10, "TP", 1), (1, "SL", .5)),
])
def test_sql_matrix_uses_only_cash_not_percentages_or_mean_r(tmp_path, positions):
    rows = sql_rows(tmp_path, positions)
    terminal = [r for r in rows if r["lifecycle_state_after"] == "POSITION_CLOSED"][1:]
    expected = sum(r["net_pnl_usdt"] for r in terminal)
    result = report(rows)
    assert oos(result)["net_pnl_usdt"] == pytest.approx(expected)
    assert result["historical_validation"] == ("FAIL" if expected < 0 else "INCOMPLETE")
    assert all(item["capital_return_pct"] is None
               for group in result["summaries"].values() for item in group)
    for actual, persisted in zip(result["rows"][1:], terminal):
        proof = json.loads(persisted["diagnostics_json"])
        assert actual["position_net_return_pct"] == persisted["net_pnl_pct"]
        assert actual["net_r"] == proof["net_r"]
        assert actual["position_notional_usdt"] == pytest.approx(
            persisted["trigger_price"]*proof["filled_quantity"])
    if len(positions) == 4:
        assert sum(r["net_pnl_pct"] for r in terminal) < 0 < expected


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("scale", [0, .5, 1])
@pytest.mark.parametrize("stop_distance,duration_ms", [(.75, 60_000), (1.5, 3_600_000)])
def test_exposure_risk_and_holding_duration_reach_sql_report(tmp_path, side, scale, stop_distance, duration_ms):
    rows = _production_backtest_lifecycle(ms(1, 15))
    rows += _production_backtest_lifecycle(ms(2, 15), risk_pct=10, side=side,
        risk_scale=scale, stop_distance=stop_distance, duration_ms=duration_ms)
    db = f"sqlite+pysqlite:///{tmp_path / 'sizing.sqlite'}"
    bo._persist_lifecycle_rows(rows, database_url=db, run_id="sizing")
    exported = bo._decision_evidence_rows(db, run_id="sizing")
    if scale == 0:
        with pytest.raises(ValueError, match="missing net_pnl_pct"):
            report(exported)
        return
    result = report(exported)
    closed = result["rows"][-1]
    assert closed["position_notional_usdt"] == pytest.approx(100*scale)
    assert closed["initial_risk_usd"] == pytest.approx(stop_distance*scale)
    assert closed["net_pnl_usdt"] == pytest.approx(3*scale-.0024)
    assert closed["net_r"] == pytest.approx(closed["net_pnl_usdt"]/(stop_distance*scale))
    assert result["historical_validation"] == "INCOMPLETE"


@pytest.mark.parametrize("field", ["net_pnl_usdt", "net_pnl_pct", "trigger_price", "sl", "close_price", "hold_minutes"])
@pytest.mark.parametrize("value", [None, "", float("nan"), float("inf"), True, "INVALID"])
def test_invalid_typed_evidence_is_never_zero_or_pass(tmp_path, field, value):
    rows = sql_rows(tmp_path)
    for row in rows:
        if row["lifecycle_state_after"] == "POSITION_CLOSED":
            row[field] = value
    with pytest.raises(ValueError):
        report(rows)


@pytest.mark.parametrize("field", ["gross_pnl", "initial_risk_usd", "total_cost_usd", "net_r", "filled_quantity", "closed_timestamp"])
@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True, -999])
def test_invalid_or_inconsistent_persisted_money_is_rejected(tmp_path, field, value):
    rows = sql_rows(tmp_path)
    for row in rows:
        proof = json.loads(row["diagnostics_json"])
        proof[field] = value
        row["diagnostics_json"] = json.dumps(proof)
    with pytest.raises((ValueError, OverflowError)):
        report(rows)


@pytest.mark.parametrize("mutation", ["run", "profile", "source", "duplicate_position", "duplicate_row", "missing_position", "missing_run", "missing_funding", "negative_fee", "double_slippage", "cross_boundary", "wrong_close"])
def test_provenance_costs_replay_and_cross_window_guards(tmp_path, mutation):
    rows = sql_rows(tmp_path)
    row = [r for r in rows if r["lifecycle_state_after"] == "POSITION_CLOSED"][-1]
    proof = json.loads(row["diagnostics_json"])
    if mutation == "run": row["run_id"] = "other"
    if mutation == "profile": row["profile_name"] = "other"
    if mutation == "source": proof["realized_cost_source"] = "other"
    if mutation == "missing_position": row["position_id"] = ""
    if mutation == "missing_run": row["run_id"] = ""
    if mutation in {"missing_funding", "negative_fee", "double_slippage"}:
        costs = json.loads(proof["realized_costs_json"])
        if mutation == "missing_funding": costs.pop("funding")
        if mutation == "negative_fee": costs["entry_fee"] = -1
        if mutation == "double_slippage": costs["entry_slippage"] = 1
        proof["realized_costs_json"] = json.dumps(costs)
    if mutation == "cross_boundary": row["hold_minutes"] = 60*24*20
    if mutation == "wrong_close": proof["closed_timestamp"] += 1
    row["diagnostics_json"] = json.dumps(proof)
    if mutation in {"duplicate_position", "duplicate_row"}:
        duplicate = dict(row)
        if mutation == "duplicate_position": duplicate["evidence_id"] += "-replay"
        rows.append(duplicate)
    with pytest.raises(ValueError):
        report(rows)


def test_legacy_percent_only_rows_cannot_inherit_cash_or_capital():
    rows = [replace(_row("train", "2026-01-15T00:00:00Z", 1), realized_cash_evidence=None),
            replace(_row("oos", "2026-02-15T00:00:00Z", 100), realized_cash_evidence=None)]
    result = build_validation_report(_contract(), rows, min_effective_rr=1.6)
    assert oos(result)["net_pnl_usdt"] is None
    assert result["historical_validation"] == "INCOMPLETE"
    assert result["promotion_evidence"]["live_authorized"] is False


def test_empty_segments_and_all_open_outcomes_cannot_pass(tmp_path):
    with pytest.raises(ValueError, match="no evidence rows"):
        build_validation_report(_contract(), [], min_effective_rr=1.6)
    rows = sql_rows(tmp_path)
    for row in rows:
        row["lifecycle_state_after"] = "POSITION_OPENED"
    with pytest.raises(ValueError, match="no terminal"):
        report(rows)


def test_no_single_capital_denominator_is_invented_across_windows(tmp_path):
    from test_issue506_walk_forward import _segment, SegmentRole, WalkForwardContract
    base = _contract()
    calibration = _segment(SegmentRole.CALIBRATION, "2026-03-01T00:00:00Z",
        "2026-04-01T00:00:00Z", "is2", base.universe, window="window-2")
    evaluation = replace(_segment(SegmentRole.OOS_VALIDATION, "2026-04-01T00:00:00Z",
        "2026-05-01T00:00:00Z", "next", base.universe, window="window-2"),
        calibration_segment_ids=("segment-is2",))
    contract = WalkForwardContract((*base.segments, calibration, evaluation), base.universe)
    adapted = list(historical_rows_from_backtest_decisions(sql_rows(tmp_path)))
    for month in (3, 4):
        timestamp = f"2026-{month:02}-15T00:00:00Z"
        cash = dict(adapted[-1].realized_cash_evidence, position_id=f"position-{month}",
                    closed_timestamp=timestamp, portfolio_equity=5000 if month == 4 else 1000)
        adapted.append(replace(adapted[-1], row_id=f"row-{month}", timestamp=timestamp,
                               realized_cash_evidence=cash))
    result = build_validation_report(contract, adapted, min_effective_rr=1.6)
    summaries = result["summaries"]["by_role_window_segment"]
    assert {item["window_id"] for item in summaries} == {"window-1", "window-2"}
    assert all(segment["capital_return_pct"] is None for segment in summaries)
    assert result["historical_validation"] == "FAIL"


def test_symbols_regimes_overlapping_holdings_cannot_invent_equity(tmp_path):
    from test_issue506_walk_forward import UniverseProvenance, WalkForwardContract
    lifecycle = _production_backtest_lifecycle(ms(1, 15))
    lifecycle += _production_backtest_lifecycle(ms(2, 15), duration_ms=3*86400_000)
    lifecycle += _production_backtest_lifecycle(ms(2, 16), symbol="ETHUSDT", duration_ms=3*86400_000)
    for row in lifecycle:
        if row.symbol == "ETHUSDT": row.regime = "RANGING"
    db = f"sqlite+pysqlite:///{tmp_path / 'overlapping.sqlite'}"
    bo._persist_lifecycle_rows(lifecycle, database_url=db, run_id="overlapping")
    adapted = historical_rows_from_backtest_decisions(bo._decision_evidence_rows(db, run_id="overlapping"))
    base = _contract()
    universe = UniverseProvenance.fixed(["BTCUSDT", "ETHUSDT"], source_identity="test-frozen-universe")
    contract = WalkForwardContract(tuple(replace(s, universe_identity=universe.identity)
                                        for s in base.segments), universe)
    result = build_validation_report(contract, adapted, min_effective_rr=1.6)
    assert oos(result)["net_pnl_usdt"] == pytest.approx(.5952)
    assert {item["symbol"] for item in result["summaries"]["by_role_symbol"]} == {"BTCUSDT", "ETHUSDT"}
    assert {item["regime"] for item in result["summaries"]["by_role_regime"]} == {"TREND", "RANGING"}
    assert result["historical_validation"] == "INCOMPLETE"
    assert oos(result)["equity_curve"] is None
    assert result == build_validation_report(contract, list(reversed(adapted)), min_effective_rr=1.6)


def test_equal_position_percentages_do_not_imply_equal_cash(tmp_path):
    lifecycle = _production_backtest_lifecycle(ms(1, 15))
    for day, size in ((15, 1), (16, 10)):
        lifecycle += _production_backtest_lifecycle(ms(2, day), risk_pct=size, cost_multiplier=size)
    db = f"sqlite+pysqlite:///{tmp_path / 'same-percent.sqlite'}"
    bo._persist_lifecycle_rows(lifecycle, database_url=db, run_id="same-percent")
    result = report(bo._decision_evidence_rows(db, run_id="same-percent"))
    first, second = result["rows"][1:]
    assert first["position_net_return_pct"] == pytest.approx(second["position_net_return_pct"])
    assert second["net_pnl_usdt"] == pytest.approx(first["net_pnl_usdt"]*10)
    assert oos(result)["net_pnl_usdt"] == pytest.approx(3.2736)
    assert oos(result)["net_return"] is None


def test_partial_unknown_cash_cannot_produce_a_partial_favorable_sum():
    first = _row("train", "2026-01-15T00:00:00Z", 1)
    second = _row("oos", "2026-02-15T00:00:00Z", 1)
    unknown = replace(_row("unknown", "2026-02-16T00:00:00Z", -20), realized_cash_evidence=None)
    result = build_validation_report(_contract(), [first, second, unknown], min_effective_rr=1.6)
    assert oos(result)["net_pnl_usdt"] is None
    assert result["historical_validation"] == "INCOMPLETE"


def test_frozen_manifest_cannot_upgrade_missing_or_conflicting_run_config(tmp_path):
    rows = sql_rows(tmp_path, ((10, "TP", 1),))
    for row in rows:
        proof = json.loads(row["diagnostics_json"])
        proof.update(git_sha="b"*40, config_hash="foreign-config", portfolio_equity=1000,
                     capital_return_pct=99, capital_evidence_status="COMPLETE")
        row["diagnostics_json"] = json.dumps(proof)
    result = report(rows)
    assert result["historical_validation"] == "INCOMPLETE"
    assert "RUN_RELEASE_CONFIG_BINDING_UNVERIFIED" in oos(result)["capital_evidence_gaps"]
    assert oos(result)["capital_return_pct"] is None
