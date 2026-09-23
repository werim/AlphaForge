"""Run focused #421 safety mutations against disposable source/test copies.

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
        Mutation("planned_entry_as_fill", RUNTIME,
                 'fill = entry * (1.0 + slippage_pct if side == "LONG" else 1.0 - slippage_pct)',
                 "fill = entry", "tests/test_paper_rr_geometry.py::test_two_bps_fill_collapses_tight_stop_rr_symmetrically"),
        Mutation("double_count_entry_slippage", RUNTIME,
                 "model.total_penalty - model.slippage_penalty / 2.0, 0.0)",
                 "model.total_penalty, 0.0)",
                 "tests/test_execution_cost_semantics.py::test_expected_entry_movement_is_not_deducted_twice_from_effective_rr"),
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
