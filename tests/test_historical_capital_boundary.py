"""The actual SQL/export/manifest path cannot certify an absent capital producer."""
from dataclasses import asdict, replace
import csv
import hashlib
import json

import pytest
from sqlalchemy import text

from alphaforge.historical_validation import MANIFEST_SCHEMA, generate_backtest_walk_forward_report
from test_issue506_walk_forward import _contract, _production_backtest_lifecycle, bo
from test_walk_forward_cash_integrity import ms, sql_rows


def export_report(tmp_path, rows, *, claim, contract=None):
    contract = contract or _contract()
    evidence = tmp_path / "decisions.csv"
    with evidence.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "contract": {
            "segments": [asdict(segment) for segment in contract.segments],
            "universe": asdict(contract.universe),
        },
        "decision_evidence_csv": evidence.name,
        "decision_evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        "min_effective_rr": 1.6,
        # Deliberately unsupported declarations, not a capital evidence API.
        "capital_evidence": claim,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return generate_backtest_walk_forward_report(path, tmp_path / "report.json")


def assert_capital_unavailable(result):
    for group in result["summaries"].values():
        for summary in group:
            assert summary["capital_evidence_status"] == "INCOMPLETE"
            assert summary["capital_return_pct"] is None
            assert summary["max_drawdown_pct"] is None
            assert summary["equity_curve"] is None
    assert result["promotion_evidence"]["future_paper"] == "REQUIRED_NOT_PROVIDED"
    assert result["promotion_evidence"]["live_authorized"] is False


@pytest.mark.parametrize("defect", [
    "opening_literal", "empty_flows", "missing_marks", "foreign_scope",
    "future_valuation", "asserted_complete",
])
def test_sql_risk_balances_and_manifest_claims_cannot_supply_capital(tmp_path, defect):
    lifecycle = _production_backtest_lifecycle(ms(1, 15))
    lifecycle += _production_backtest_lifecycle(ms(2, 15), duration_ms=3*86400_000)
    lifecycle += _production_backtest_lifecycle(ms(2, 16), duration_ms=3*86400_000)
    for row in lifecycle:
        row.portfolio_equity = 1000.0
        row.available_balance = 1000.0
    db = f"sqlite+pysqlite:///{tmp_path / 'risk-balances.sqlite'}"
    bo._persist_lifecycle_rows(lifecycle, database_url=db, run_id="capital-boundary")
    engine = bo.init_db(db)
    try:
        with engine.connect() as conn:
            persisted = conn.execute(text("""
                SELECT portfolio_equity,available_balance FROM decision_evidence
                WHERE lifecycle_state_after='POSITION_CLOSED'
            """)).all()
        assert len(persisted) == 3
        assert all(tuple(row) == (1000.0, 1000.0) for row in persisted)
    finally:
        engine.dispose()
    rows = bo._decision_evidence_rows(db, run_id="capital-boundary")
    claim = {
        "status": "COMPLETE", "opening_capital": 1000.0,
        "external_flows": [], "closure_population_complete": True,
        "run_id": "capital-boundary", "config_hash": _contract().segments[1].config_hash,
        "marks": [{"at": "2026-02-16T00:00:00Z", "equity": 1001.0}],
        "capital_return_pct": 99.0, "max_drawdown_pct": 0.0,
    }
    if defect == "opening_literal": claim = {"opening_capital": 1000.0}
    if defect == "empty_flows": claim.pop("external_flows")
    if defect == "missing_marks": claim["marks"] = []
    if defect == "foreign_scope": claim.update(run_id="other", config_hash="foreign")
    if defect == "future_valuation": claim["marks"][0]["at"] = "2027-01-01T00:00:00Z"
    result = export_report(tmp_path, rows, claim=claim)
    assert result["historical_validation"] == "INCOMPLETE"
    assert_capital_unavailable(result)


@pytest.mark.parametrize("reused_oos", [False, True])
@pytest.mark.parametrize("omit_loss", [False, True])
def test_rehashed_sql_subset_is_not_closure_population_proof(tmp_path, reused_oos, omit_loss):
    rows = sql_rows(tmp_path)
    contract = _contract()
    if reused_oos:
        evaluation = contract.segments[1]
        lineage = replace(evaluation.search_selection_lineage,
                          influenced_selection=True, declared_fresh=False)
        contract = replace(contract, segments=(contract.segments[0],
                           replace(evaluation, search_selection_lineage=lineage)))
    complete_rows = len(rows)
    if omit_loss:
        # Omit a real loss, including its nonterminal lifecycle rows. The CSV
        # digest is recomputed correctly; integrity is not exhaustiveness.
        loss = next(row for row in rows if row["lifecycle_state_after"] == "POSITION_CLOSED"
                    and row["net_pnl_usdt"] < 0)
        rows = [row for row in rows if row["position_id"] != loss["position_id"]]
        assert len(rows) < complete_rows
    result = export_report(tmp_path, rows, contract=contract, claim={
        "status": "COMPLETE", "closure_population_complete": True,
        "closure_count": sum(row["lifecycle_state_after"] == "POSITION_CLOSED" for row in rows),
        "opening_capital": 1000.0, "external_flows": [], "equity_curve": [1000, 1001],
    })
    assert result["historical_validation"] == (
        "INADMISSIBLE_REUSED_OOS" if reused_oos else "INCOMPLETE" if omit_loss else "FAIL")
    oos = result["summaries"]["by_role_window_segment"][1]
    assert (oos["net_pnl_usdt"] > 0) if omit_loss else (oos["net_pnl_usdt"] < 0)
    assert_capital_unavailable(result)
