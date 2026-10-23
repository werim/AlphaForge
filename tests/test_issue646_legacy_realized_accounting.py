"""Real production legacy closure versus the persisted PAPER cash ledger."""
import pytest
import importlib.util
from pathlib import Path
_spec = importlib.util.spec_from_file_location("backtest_order", Path(__file__).resolve().parents[1] / "backtest_order.py")
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)


def candidate(side="LONG", stop_distance=10):
    sign = 1 if side == "LONG" else -1
    return legacy.CandidateOrder(1767225600000, "BTCUSDT", side, 100, 100-sign*stop_distance,
                                 100+sign*20, 2, "TEST", "TEST", "TRENDING", 9, "MARKET")


def close(c=None, ctx=None, exit_price=120, reason="TP_HIT", duration=3600000):
    c = c or candidate()
    return legacy.finalize(c, "POSITION_OPENED", "POSITION_CLOSED", 100, exit_price, reason,
                          (exit_price-100)*(1 if c.side == "LONG" else -1), 1000, 20,
                          c.timestamp, c.timestamp+duration, ctx or {})


def test_missing_realized_cost_evidence_is_not_net_profit():
    row = close()
    assert row.net_pnl_usdt is None


def test_identical_sql_cash_facts_not_risk_budget_times_price_percent(tmp_path):
    from test_phase8_position_resolver import setup
    from alphaforge.burnin_resolver import persist_pending_position, resolve_position_closure
    conn, campaign, run = setup(tmp_path)
    persist_pending_position(conn, trade_id="legacy", campaign_id=campaign.campaign_id,
        burnin_run_id=run["burnin_run_id"], signal_id="legacy", symbol="BTCUSDT", side="LONG",
        entry_time="2026-01-01T00:00:00Z", planned_entry=100, simulated_fill=100,
        stop=90, target=120, quantity=2, notional=200, entry_spread=1, entry_slippage=0,
        entry_fee=1, regime="TRENDING", source_provenance={"execution_cost_unit":"USD"})
    costs = dict(exit_spread=1, exit_slippage=1, exit_fee=1, funding=1,
                 latency_impact_penalty=0, cost_unit="USD")
    resolve_position_closure(conn, trade_id="legacy", exit_time="2026-01-01T01:00:00Z",
                             exit_price=120, exit_reason="TP_HIT", exit_costs=costs)
    expected = dict(conn.execute("SELECT * FROM burnin_pending_position_outcomes WHERE trade_id='legacy'").fetchone())
    row = close(ctx={"realized_costs_usd": dict(entry_spread=1, entry_slippage=0, entry_fee=1,
                    **{k:v for k,v in costs.items() if k != 'cost_unit'}),
                    "realized_cost_source":"FROZEN_TEST_LEDGER"})
    assert row.net_pnl_usdt == pytest.approx(expected["net_pnl"])


def measured_costs(scale=1, funding=0):
    return dict(entry_spread=.1*scale, entry_slippage=0, entry_fee=.2*scale,
                exit_spread=.3*scale, exit_slippage=.4*scale, exit_fee=.5*scale,
                funding=funding*scale, latency_impact_penalty=.6*scale)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("outcome", ["TP_HIT", "SL_HIT", "TIMEOUT"])
@pytest.mark.parametrize("fill_offset,stop_distance,quantity,duration,funding", [
    (-1, 5, 1, 60000, -.2), (0, 10, 2, 3600000, 0), (1, 15, 3, 28800000, .2)])
@pytest.mark.parametrize("scale", [0, .5, 1])
def test_full_closure_matrix_matches_sql(tmp_path, side, outcome, fill_offset, stop_distance, quantity, duration, funding, scale):
    from datetime import datetime, timezone
    from test_phase8_position_resolver import setup
    from alphaforge.burnin_resolver import persist_pending_position, resolve_position_closure
    c = candidate(side, stop_distance)
    sign = 1 if side == "LONG" else -1
    fill = 100 + fill_offset*sign
    exit_price = c.tp if outcome == "TP_HIT" else c.sl if outcome == "SL_HIT" else 100+sign*3
    costs = measured_costs(scale, funding)
    ctx = dict(notional=fill*quantity, risk_scale=scale,
               realized_cost_source="FROZEN_FULL_HOLDING_PERIOD_OBSERVATIONS", realized_costs_usd=costs)
    row = legacy.finalize(c, "POSITION_OPENED", "POSITION_CLOSED", fill, exit_price, outcome,
        20, 1000, 20, c.timestamp, c.timestamp+duration, ctx)
    assert row.filled_quantity == pytest.approx(quantity*scale)
    assert row.initial_risk_usd == pytest.approx(abs(fill-c.sl)*quantity*scale)
    assert row.gross_pnl_usd == pytest.approx((exit_price-fill)*quantity*scale*sign)
    if not scale:
        assert row.net_pnl_usdt is None and row.net_r is None
        return  # Zero exposure cannot be persisted as an executed PAPER position.
    conn, campaign, run = setup(tmp_path)
    entry = {k:costs[k] for k in ("entry_spread", "entry_slippage", "entry_fee")}
    exit_costs = {k:v for k,v in costs.items() if k not in entry}
    ts = lambda n: datetime.fromtimestamp(n/1000, timezone.utc).isoformat()
    persist_pending_position(conn, trade_id="matrix", campaign_id=campaign.campaign_id,
        burnin_run_id=run["burnin_run_id"], signal_id="matrix", symbol=c.symbol, side=side,
        entry_time=ts(c.timestamp), planned_entry=c.entry, simulated_fill=fill,
        stop=c.sl, target=c.tp, quantity=quantity*scale, notional=fill*quantity*scale,
        **entry, regime=c.regime, source_provenance={"execution_cost_unit":"USD"})
    resolve_position_closure(conn, trade_id="matrix", exit_time=ts(c.timestamp+duration),
        exit_price=exit_price, exit_reason=outcome, exit_costs={**exit_costs, "cost_unit":"USD"})
    expected = dict(conn.execute("SELECT * FROM burnin_pending_position_outcomes WHERE trade_id='matrix'").fetchone())
    for actual, key in [(row.gross_pnl_usd,"gross_pnl"), (row.total_cost_usd,"total_execution_cost"),
                        (row.net_pnl_usdt,"net_pnl"), (row.gross_r,"gross_r"), (row.net_r,"net_r")]:
        assert actual == pytest.approx(expected[key])
    assert row.hold_minutes*60 == expected["hold_duration_seconds"] == duration/1000
    assert row.total_cost_usd == pytest.approx((2.1+funding)*scale)
    assert row.net_pnl_usdt == pytest.approx(row.gross_pnl_usd-row.total_cost_usd)
    assert row.net_r * row.initial_risk_usd == pytest.approx(row.net_pnl_usdt)
    curve, metrics = legacy.build_equity_curve_metrics([row], 1000)
    assert curve[-1]["equity"] == metrics["ending_equity"] == pytest.approx(1000+expected["net_pnl"])
    assert resolve_position_closure(conn, trade_id="matrix", exit_time=ts(c.timestamp+duration),
        exit_price=1, exit_reason="REPLAY", exit_costs={})["status"] == "IDEMPOTENT"


def test_cash_identities_and_no_false_positive_expectancy():
    row = close(ctx={"realized_cost_source":"TEST", "realized_costs_usd":measured_costs()})
    assert row.gross_pnl_usd == 40
    assert row.initial_risk_usd == 20
    assert row.total_cost_usd == pytest.approx(2.1)
    assert row.net_pnl_usdt == pytest.approx(37.9)
    assert row.net_r == pytest.approx(1.895)
    assert row.net_pnl_pct == pytest.approx(18.95)
    costly = measured_costs(); costly["exit_fee"] = 41
    loss = close(ctx={"realized_cost_source":"TEST", "realized_costs_usd":costly})
    assert loss.net_pnl_usdt < 0 and loss.net_r < 0
    curve, metrics = legacy.build_equity_curve_metrics([loss], 1000)
    assert curve[0]["equity"] < 1000 and metrics["max_drawdown"] < 0
    assert metrics["profit_factor"] == 0


@pytest.mark.parametrize("key", ["entry_spread","entry_slippage","entry_fee","exit_spread",
                                 "exit_slippage","exit_fee","funding","latency_impact_penalty"])
@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf"), "UNKNOWN"])
def test_missing_or_invalid_costs_never_become_zero(key, invalid):
    costs = measured_costs(); costs[key] = invalid
    row = close(ctx={"realized_cost_source":"TEST", "realized_costs_usd":costs})
    assert row.net_r is None and row.net_pnl_usdt is None and row.total_cost_usd is None
    assert row.realized_evidence_status == "INCOMPLETE"
    curve, metrics = legacy.build_equity_curve_metrics([row, close()], 1000)
    assert all(r["equity"] is None for r in curve)
    assert metrics["max_drawdown"] is None and metrics["profit_factor"] is None


def test_zero_costs_and_slippage_already_embedded_in_fill():
    costs = {k:0 for k in measured_costs()}
    row = close(ctx={"realized_cost_source":"TEST_EXPLICIT_ZERO", "realized_costs_usd":costs})
    assert row.net_pnl_usdt == row.gross_pnl_usd == 40
    assert row.net_r == 2
    costs["entry_slippage"] = 1
    assert close(ctx={"realized_cost_source":"TEST", "realized_costs_usd":costs}).net_r is None


def test_ambiguous_candle_is_diagnostic_not_favorable_realization():
    c = candidate()
    rows = legacy.simulate_candidate(c, [legacy.Candle(c.timestamp,100,125,85,110,1)], 0,1000,20,
        {"realized_cost_source":"TEST", "realized_costs_usd":measured_costs()})
    terminal = rows[-1]
    assert terminal.close_reason == "SL_HIT"
    assert terminal.net_r is None and terminal.net_pnl_usdt is None


def test_portfolio_unknown_cash_and_replay_are_fail_closed():
    from alphaforge.portfolio_risk import BacktestPortfolioState
    state = BacktestPortfolioState(1000)
    kwargs = dict(position_id="one", symbol="BTCUSDT", timestamp=1767225600000, close_reason="TP_HIT")
    state.close_position(**kwargs, net_pnl_usdt=10)
    state.close_position(**kwargs, net_pnl_usdt=10)
    assert state.current_equity == 1010
    state.close_position(**{**kwargs,"position_id":"two"}, net_pnl_usdt=None)
    assert state.current_equity is None
    snap = state.snapshot(mode="BACKTEST", symbol="BTCUSDT")
    assert snap.risk_state_complete is False and snap.daily_realized_pnl is None
    state.close_position(**{**kwargs,"position_id":"three"}, net_pnl_usdt=100)
    assert state.current_equity is None


def test_equity_uses_close_order_and_deduplicates_position_replay():
    win = close(ctx={"realized_cost_source":"TEST", "realized_costs_usd":measured_costs()}, duration=7200000)
    loss = close(ctx={"realized_cost_source":"TEST", "realized_costs_usd":measured_costs()}, exit_price=90, reason="SL_HIT")
    win.position_id="win"; loss.position_id="loss"
    curve, metrics = legacy.build_equity_curve_metrics([win, loss, win], 1000)
    assert len(curve) == 2
    assert curve[0]["net_pnl_usdt"] == pytest.approx(-22.1)
    assert metrics["max_drawdown"] == pytest.approx(-22.1)
    assert metrics["ending_equity"] == pytest.approx(1015.8)


def test_profile_comparison_does_not_rank_unknown_net_as_zero(tmp_path):
    from alphaforge.dashboard.backtest_control import _comparison_metrics
    (tmp_path/"order_backtest_summary.csv").write_text("accepted_count,total_net_pnl_usdt,max_drawdown\n1,,\n")
    row = _comparison_metrics("DEFAULT_FILTERS", tmp_path, 1000)
    assert row["objective_score"]["raw_net_pnl"] is None
    assert row["objective_score"]["final_objective_score"] is None
    assert "REALIZED_NET_EVIDENCE_INCOMPLETE" in row["warnings"]


@pytest.mark.parametrize("after", ["OPEN_AT_END", "POSITION_OPENED", "ENTRY_TIMEOUT"])
def test_unrealized_lifecycle_never_creates_net_profit(after):
    c = candidate()
    row = legacy.finalize(c, "ORDER_PLACED", after, 100, 120, "", 20,1000,20,
        c.timestamp,c.timestamp+3600000,{"realized_cost_source":"TEST","realized_costs_usd":measured_costs()})
    assert row.net_pnl_usdt is None and row.net_r is None


@pytest.mark.parametrize("field", ["exit_fee", "volatility_penalty", "liquidity_penalty"])
@pytest.mark.parametrize("bad", [None, "UNKNOWN", float("inf"), float("nan"), -1, True])
def test_sql_invalid_mandatory_cash_cannot_be_complete(tmp_path, bad, field):
    from test_phase8_position_resolver import setup
    from alphaforge.burnin_resolver import persist_pending_position, resolve_position_closure
    conn, campaign, run = setup(tmp_path)
    persist_pending_position(conn, trade_id="bad", campaign_id=campaign.campaign_id,
        burnin_run_id=run["burnin_run_id"], signal_id="bad", symbol="BTCUSDT", side="LONG",
        entry_time="2026-01-01T00:00:00Z", planned_entry=100, simulated_fill=100,
        stop=90,target=120,quantity=2,notional=200,entry_spread=0,entry_slippage=0,entry_fee=2,
        regime="TRENDING",source_provenance={"execution_cost_unit":"USD"})
    costs = dict(exit_spread=0,exit_slippage=0,exit_fee=0,funding=0,latency_impact_penalty=0,cost_unit="USD")
    costs[field] = bad
    try:
        result = resolve_position_closure(conn,trade_id="bad",exit_time="2026-01-01T01:00:00Z",
            exit_price=120,exit_reason="TP_HIT",exit_costs=costs)
    except (ValueError, TypeError):
        return  # Rejection before persistence also fails closed.
    assert result["evidence_complete"] is False
    row = conn.execute("SELECT net_pnl,net_r FROM burnin_pending_position_outcomes WHERE trade_id='bad'").fetchone()
    assert row["net_pnl"] is None and row["net_r"] is None


@pytest.mark.parametrize("complete", [False, True])
def test_legacy_sql_and_csv_export_preserve_cash_units_and_nulls(tmp_path, complete):
    import json
    from dataclasses import asdict
    from sqlalchemy import create_engine, text
    ctx = {"realized_cost_source":"TEST", "realized_costs_usd":measured_costs()} if complete else {}
    row = close(ctx=ctx)
    url = "sqlite:///" + str(tmp_path/"legacy.db")
    exported = legacy._persist_lifecycle_rows([row], database_url=url, run_id="accounting-test")
    assert exported[0]["net_pnl_usdt"] == row.net_pnl_usdt
    assert exported[0]["realized_accounting_version"] == "CASH_LEDGER_V1"
    assert exported[0]["total_cost_usd"] == row.total_cost_usd
    legacy._persist_lifecycle_rows([row], database_url=url, run_id="accounting-test")
    with create_engine(url).connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM trade_lifecycle_events")).scalar_one() == 1
        persisted = conn.execute(text("SELECT net_pnl_usdt FROM decision_evidence")).mappings().one()
        decision = conn.execute(text("SELECT execution_ctx FROM trade_lifecycle_events")).scalar_one()
    decision = json.loads(decision) if isinstance(decision,str) else decision
    assert persisted["net_pnl_usdt"] == row.net_pnl_usdt
    assert decision["gross_pnl"] == row.gross_pnl_usd == 40
    assert decision["net_pnl"] == row.net_pnl_usdt
    assert decision["net_r"] == row.net_r
    assert decision["realized_accounting_version"] == "CASH_LEDGER_V1"
    assert decision["realized_evidence_status"] == ("COMPLETE" if complete else "INCOMPLETE")
    assert decision["fees"] == (pytest.approx(.7) if complete else None)  # Actual cash, never RR.
    assert asdict(row)["net_pnl_usdt"] == (pytest.approx(37.9) if complete else None)
