"""Run focused #421/#498 safety mutations against disposable source/test copies.

No production file or campaign database is edited. A mutation counts as killed
only when a previously passing selected test fails an assertion.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
EXECUTION = "src/alphaforge/execution.py"
RUNTIME = "src/alphaforge/runtime.py"
SCOPE = "src/alphaforge/expectancy_evidence.py"
SAFETY_TEST = "tests/test_issue421_execution_safety.py"
RISK_TEST = "tests/test_issue421_portfolio_risk_state.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    before: str
    after: str
    test: str


def mutations() -> list[Mutation]:
    cases = [
        Mutation("effective_rr", EXECUTION,
                 "if effective is None or effective < float(min_effective_rr):",
                 "if False:", SAFETY_TEST + "::test_each_protected_execution_gate_independently_kills_acceptance"),
        Mutation("unknown_execution", EXECUTION,
                 "if (reject_unknown and missing_fields) or invalid_numeric_fields:",
                 "if invalid_numeric_fields:", SAFETY_TEST + "::test_high_raw_rr_cannot_bypass_unknown_execution_context"),
    ]
    gates = {
        "spread": "if spread is not None and spread > max_spread:",
        "slippage": "if slippage is not None and slippage > max_slippage:",
        "total_cost": "if total_explicit_cost > max_total_cost:",
        "liquidity": "if liquidity is not None and liquidity < min_liquidity:",
        "latency": "if latency is not None and latency > max_latency:",
        "volatility": "if model.volatility_penalty > max_volatility_penalty:",
        "funding": "if funding is not None and abs(funding) > max_funding:",
    }
    for name, before in gates.items():
        anchored = "    " + before + "\n        fail"
        cases.append(Mutation(name, EXECUTION, anchored, "    if False:\n        fail",
                              SAFETY_TEST + "::test_each_protected_execution_gate_independently_kills_acceptance"))
    cases.extend([
        Mutation("planned_entry_as_fill", EXECUTION,
                 'fill = entry_price * (1.0 + slip if normalized_side == "LONG" else 1.0 - slip)',
                 "fill = entry_price", "tests/test_paper_rr_geometry.py::test_two_bps_fill_collapses_tight_stop_rr_symmetrically"),
        Mutation("double_count_entry_slippage", EXECUTION,
                 "model.total_penalty - model.slippage_penalty / 2.0, 0.0)",
                 "model.total_penalty, 0.0)",
                 "tests/test_execution_cost_semantics.py::test_expected_entry_movement_is_not_deducted_twice_from_effective_rr"),
        Mutation("risk_scale_not_applied", RUNTIME,
                 'candidate_notional = float(sizing_projection["effective_notional"])',
                 'candidate_notional = float(sizing_projection["original_notional"])',
                 "tests/test_issue489_risk_scale_sizing.py::test_paper_softened_wide_stop_executes_scaled_notional_and_quantity"),
        Mutation("planned_entry_stop_basis", "src/alphaforge/order.py",
                 "planned_entry=candidate.entry,\n        expected_fill=expected_fill_for_stop,\n        stop=candidate.sl,",
                 "planned_entry=candidate.entry,\n        expected_fill=candidate.entry,\n        stop=candidate.sl,",
                 "tests/test_issue492_executable_stop_risk.py::test_trade_quality_uses_expected_fill_not_planned_entry_for_stop_gate"),
        Mutation("missing_execution_metrics_pass", "src/alphaforge/burnin_qualification.py",
                 "if missing or invalid_numeric:\n            evidence_gaps",
                 'if missing or invalid_numeric:\n            return "PASS"\n        if False:\n            evidence_gaps',
                 "tests/test_phase7_qualification.py::test_execution_metrics_missing_measurements_are_insufficient"),
        Mutation("missing_drawdown_coalesced_resolved", "src/alphaforge/burnin_qualification.py",
                 'if missing:\n                gaps.append(f"{event_id}:" + ",".join(sorted(set(missing))))\n                continue\n            valid_rows.append({',
                 'if False:\n                gaps.append(f"{event_id}:" + ",".join(sorted(set(missing))))\n                continue\n            valid_rows.append({',
                 "tests/test_phase7_qualification.py::test_synthetic_zero_drawdown_placeholder_is_insufficient"),
        Mutation("rr_stage_writer_swap", "src/alphaforge/persistence.py",
                 "payload = {column: evidence.get(column) for column in DECISION_EVIDENCE_COLUMNS}\n    # Compatibility contract:",
                 'payload = {column: evidence.get(column) for column in DECISION_EVIDENCE_COLUMNS}\n    payload["candidate_raw_rr"], payload["executable_raw_rr"] = (\n        payload["executable_raw_rr"], payload["candidate_raw_rr"]\n    )\n    # Compatibility contract:',
                 "tests/test_issue495_rr_stage_evidence.py::test_raw_rr_is_candidate_alias_and_rr_stages_round_trip"),
        Mutation("sizing_removed_from_parity_projection", "src/alphaforge/decision_invariant.py",
                 'risk_scale=_number(payload.get("risk_scale")),\n        effective_notional=_number(payload.get("effective_notional")),',
                 'risk_scale=1.0,\n        effective_notional=_number(payload.get("original_notional")),',
                 "tests/test_issue490_protected_mode_parity.py::test_mode_parity_fails_on_capital_sizing_drift"),
        Mutation("semantic_validation_bypass", "src/alphaforge/decision_invariant.py",
                 "violations = validate_pre_submit_semantics(\n        payload, numeric_abs_tol=numeric_abs_tol\n    )",
                 "return project_pre_submit_invariant(payload)\n    violations = validate_pre_submit_semantics(\n        payload, numeric_abs_tol=numeric_abs_tol\n    )",
                 "tests/test_issue486_semantic_decision_invariants.py::test_identically_wrong_surfaces_do_not_pass_parity"),
    ])
    for field in ("run_id", "campaign_id", "release_id"):
        cases.append(Mutation(field + "_scope", SCOPE,
                              f"          AND (:{field} IS NULL OR {field}=:{field})\n", "",
                              "tests/test_expectancy_evidence.py::test_each_expectancy_scope_filter_independently_excludes_foreign_evidence"))
    cases.append(Mutation("mode_scope", "src/alphaforge/live_readiness.py",
                          'clauses = ["UPPER(COALESCE(decision_evidence.mode,\'\'))=:readiness_mode"]',
                          'clauses = ["1=1"]',
                          "tests/test_live_readiness.py::test_readiness_mode_scope_ignores_backtest_only_poison"))
    cases.extend([
        Mutation("shadow_authority", "src/alphaforge/burnin_qualification.py",
                 'if payload.get("forward_label_subject") == "LEGACY_SCANNER_SHADOW_CANDIDATE":',
                 "if False:",
                 "tests/test_phase7_qualification.py::test_non_attributable_shadow_and_infrastructure_outcomes_are_diagnostic_only"),
        Mutation("accepted_window", "src/alphaforge/burnin_resolver.py",
                 "if not complete:\n            diagnostics=['incomplete_market_window'",
                 "if False:\n            diagnostics=['incomplete_market_window'",
                 "tests/test_issue421_position_window_integrity.py::test_missing_middle_candle_before_tp_remains_pending"),
        Mutation("reconciliation_clean", RUNTIME,
                 'self._reconciliation_status == "CLEAN"\n            and not self._reconciliation_persistence_unhealthy',
                 'True\n            and not self._reconciliation_persistence_unhealthy',
                 "tests/test_runtime_live_authorization.py::test_runtime_live_authorization_is_authoritative_and_refreshed"),
        Mutation("final_kill_switch_reread", "src/alphaforge/order.py",
                 "authorization = dict(provider() or {})",
                 'authorization = dict(ctx.storage.get("live_authorization") or provider() or {})',
                 "tests/test_runtime_live_authorization.py::test_runtime_live_authorization_is_authoritative_and_refreshed"),
    ])
    risk_tests = {
        "daily_realized_pnl": "test_runtime_rejects_max_daily_loss_from_persisted_paper_state",
        "trades_today_symbol": "test_runtime_daily_trade_caps_use_canonical_runtime_config_names",
        "trades_today_global": "test_runtime_daily_trade_caps_use_canonical_runtime_config_names",
        "consecutive_loss_count": "test_runtime_rejects_symbol_and_global_loss_clusters",
        "rolling_drawdown_pct": "test_runtime_rejects_rolling_drawdown_when_daily_pnl_is_positive",
    }
    for field, test in risk_tests.items():
        cases.append(Mutation(field + "_risk_input", RUNTIME,
                              f'{field}=historical_risk.get("{field}"),',
                              f"{field}=None,", RISK_TEST + "::" + test))
    cases.append(Mutation("future_market_timestamp", RUNTIME,
                          "if future_delta_sec > allowed_future_skew_sec:", "if False:",
                          "tests/test_issue421_market_time.py::test_future_timestamp_beyond_allowed_clock_skew_fails_closed"))
    accounting_test = "tests/test_issue646_legacy_realized_accounting.py"
    cases.extend([
        Mutation("realized_net_subtracts_costs", "src/alphaforge/realized_accounting.py",
                 "gross_pnl - total", "gross_pnl", accounting_test + "::test_cash_identities_and_no_false_positive_expectancy"),
        Mutation("realized_risk_includes_quantity", "src/alphaforge/realized_accounting.py",
                 "risk = abs(fill - stop) * quantity", "risk = abs(fill - stop)",
                 accounting_test + "::test_cash_identities_and_no_false_positive_expectancy"),
        Mutation("missing_cash_is_not_zero", "src/alphaforge/realized_accounting.py",
                 "total = None if absent else sum(parsed.values())", "total = sum(parsed.values())",
                 accounting_test + "::test_missing_realized_cost_evidence_is_not_net_profit"),
        Mutation("legacy_cash_applies_risk_scale", "backtest_order.py",
                 "base_notional * scale / fill", "base_notional / fill",
                 accounting_test + "::test_full_closure_matrix_matches_sql"),
        Mutation("cash_replay_is_idempotent", "src/alphaforge/portfolio_risk.py",
                 "if position_id in self.closed_position_ids:", "if False:",
                 accounting_test + "::test_portfolio_unknown_cash_and_replay_are_fail_closed"),
        Mutation("historical_cash_provenance_is_required", "src/alphaforge/historical_validation.py",
                 'or proof.get("realized_accounting_version") != "CASH_LEDGER_V1"', 'or False',
                 "tests/test_issue506_walk_forward.py::test_numeric_legacy_return_cannot_inherit_cost_complete_provenance"),
        Mutation("quality_r_is_not_cash", "backtest_order.py",
                 '"baseline_plus_quality_gate_net_pnl": None,',
                 '"baseline_plus_quality_gate_net_pnl": sum_cash([baseline_cash, sized_expected]),',
                 "tests/test_quality_gate_accounting_integrity.py::test_two_r_cannot_be_added_to_one_hundred_usdt"),
        Mutation("quality_baseline_missing_is_not_zero", "backtest_order.py",
                 "baseline_cash = finite_cash(baseline_net_pnl)",
                 "baseline_cash = finite_cash(baseline_net_pnl) or 0.0",
                 "tests/test_quality_gate_accounting_integrity.py::test_missing_baseline_never_becomes_zero"),
        Mutation("quality_future_outcomes_cannot_promote", "backtest_order.py",
                 '"quality_gate_promotion_eligible": False,',
                 '"quality_gate_promotion_eligible": True,',
                 "tests/test_quality_gate_accounting_integrity.py::test_signed_realized_cash_is_invariant_and_diagnostic_only"),
    ])
    walk_test = "tests/test_walk_forward_cash_integrity.py"
    cases.extend([
        Mutation("walk_forward_cash_not_percentage_sign", "src/alphaforge/walk_forward.py",
                 'and item["net_pnl_usdt"] is not None and item["net_pnl_usdt"] < 0',
                 'and item["net_pnl_usdt"] is not None and item["net_pnl_usdt"] > 0',
                 walk_test + "::test_positive_percentage_sum_cannot_hide_negative_sql_cash"),
        Mutation("walk_forward_missing_capital_cannot_pass", "src/alphaforge/walk_forward.py",
                 'else "FAIL" if negative_oos else "INCOMPLETE"',
                 'else "FAIL" if negative_oos else "PASS"',
                 walk_test + "::test_positive_cash_without_window_capital_is_incomplete"),
        Mutation("walk_forward_duplicate_cash", "src/alphaforge/walk_forward.py",
                 'if position in realized_positions:', 'if False:',
                 walk_test + "::test_provenance_costs_replay_and_cross_window_guards[duplicate_position]"),
        Mutation("walk_forward_missing_legacy_cash_is_not_zero", "src/alphaforge/walk_forward.py",
                 'if proof is None:\n        return None', 'if proof is None:\n        return 0.0',
                 walk_test + "::test_legacy_percent_only_rows_cannot_inherit_cash_or_capital"),
    ])
    cases.extend([
        Mutation("readiness_invalid_identity_never_passes", "src/alphaforge/live_readiness_agent.py",
                 'not valid_git_commit(m.get("commit")) or not valid_git_commit(c.get("git_commit"))',
                 'not m.get("commit")',
                 "tests/test_live_readiness_agent.py::test_missing_or_invalid_git_identity_never_qualifies_even_when_equal"),
        Mutation("readiness_source_snapshot_consistent", "src/alphaforge/live_readiness_agent.py",
                 '\"git_commit\":m.get(\"commit\")', '\"git_commit\":self.gm(self.repo).get(\"commit\")',
                 "tests/test_live_readiness_agent.py::test_readiness_report_uses_one_source_identity_snapshot"),
        Mutation("readiness_unknown_status_not_clean", "src/alphaforge/live_readiness_agent.py",
                 'if isinstance(dirty,bool) else', 'if True else',
                 "tests/test_live_readiness_agent.py::test_readiness_refuses_unknown_checkout_cleanliness"),
    ])
    cases.extend([
        Mutation("delivery_forbidden_never_claims_published", "scripts/verify_codex_delivery.py",
                 'return "COMMENT_WRITE_FORBIDDEN "', 'return "CREATED "',
                 "tests/test_verify_codex_delivery.py::test_forbidden_comment_preserves_summary_receipt_and_no_permission_escalation"),
        Mutation("delivery_requires_actual_checkout", "scripts/verify_codex_delivery.py",
                 'return "UNAVAILABLE_TESTED_CHECKOUT"', 'return "EXACT_HEAD_SHA"',
                 "tests/test_verify_codex_delivery.py::test_actual_tested_tree_is_required_even_when_metadata_matches_head"),
        Mutation("delivery_stale_head_not_published", "scripts/verify_codex_delivery.py",
                 'latest["head"]["sha"] != sha', 'False',
                 "tests/test_verify_codex_delivery.py::test_head_moves_during_log_inspection_never_publishes"),
    ])
    return cases


def run_test(workspace: Path, nodes: list[str]) -> tuple[int, list[str], int, str]:
    junit = workspace / "mutation-junit.xml"
    junit.unlink(missing_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace / "src")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--disable-warnings",
         "--junitxml=" + str(junit), *nodes],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=120,
    )
    if not junit.exists():
        return result.returncode, [], -1, (result.stdout + result.stderr)[-1200:]
    tree = ElementTree.parse(junit)
    failures = [failure.get("message", "") + " " + (failure.text or "")
                for failure in tree.iter("failure")]
    errors = sum(1 for _ in tree.iter("error"))
    return result.returncode, failures, errors, (result.stdout + result.stderr)[-1200:]


def main() -> int:
    cases = mutations()
    with tempfile.TemporaryDirectory(prefix=".safety-mutations-", dir=ROOT) as directory:
        work = Path(directory)
        shutil.copytree(ROOT / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "tests", work / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "pyproject.toml", work / "pyproject.toml")
        shutil.copy2(ROOT / "backtest_order.py", work / "backtest_order.py")
        (work / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/verify_codex_delivery.py", work / "scripts/verify_codex_delivery.py")
        (work / ".github/workflows").mkdir(parents=True)
        shutil.copy2(ROOT / ".github/workflows/codex-delivery-verification.yml", work / ".github/workflows/codex-delivery-verification.yml")
        baseline = sorted({case.test for case in cases})
        code, failures, errors, output = run_test(work, baseline)
        if code or failures or errors:
            print("BASELINE_ERROR", output, file=sys.stderr)
            return 2

        results = []
        for case in cases:
            path = work / case.file
            original = (ROOT / case.file).read_text()
            if original.count(case.before) != 1:
                results.append({"mutation": case.name, "status": "ERROR", "reason": "anchor_not_unique"})
                continue
            mutated = original.replace(case.before, case.after, 1)
            try:
                compile(mutated, str(path), "exec")
            except SyntaxError:
                results.append({"mutation": case.name, "status": "ERROR", "reason": "syntax"})
                continue
            path.write_text(mutated)
            try:
                code, failures, errors, output = run_test(work, [case.test])
                assertion = any("AssertionError" in failure or "DID NOT RAISE" in failure
                                for failure in failures)
                status = "KILLED" if code == 1 and errors == 0 and assertion else (
                    "SURVIVED" if code == 0 else "ERROR")
                results.append({"mutation": case.name, "status": status,
                                "test": case.test, "detail": "" if status == "KILLED" else output})
            except subprocess.TimeoutExpired:
                results.append({"mutation": case.name, "status": "ERROR", "reason": "timeout"})
            finally:
                path.write_text(original)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    counts = {status: sum(row["status"] == status for row in results)
              for status in ("KILLED", "SURVIVED", "ERROR")}
    print(json.dumps({"source_sha": sha, "baseline_nodes": len(baseline),
                      "counts": counts, "results": results}, indent=2))
    return 0 if counts["KILLED"] == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
