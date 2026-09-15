from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import alphaforge.autonomous_qualification as qualification_module
from alphaforge.autonomous_qualification import (
    FAULT_NAMES,
    AutonomousQualificationHarness,
    main,
)


def test_fast_qualification_is_isolated_complete_and_machine_readable(tmp_path: Path) -> None:
    historical = tmp_path / "POST363.db"
    historical.write_bytes(b"historical-sentinel")
    before = historical.read_bytes()

    harness = AutonomousQualificationHarness(mode="FAST", output_root=tmp_path)
    report = harness.run()

    assert report["overall_verdict"] == "PASS"
    assert set(FAULT_NAMES).issubset(set(report["tests_executed"]))
    assert report["invariant_failures"] == []
    assert report["unexplained_state_transitions"] == []
    assert report["persistence_gaps"] == []
    assert report["campaign_run_lineage_consistency"] is True
    assert report["isolation"] == {
        "database": str(harness.db_path),
        "artifact_directory": str(harness.artifact_dir),
        "database_created_for_run": True,
        "paper_only": True,
        "live_order_submission": False,
        "production_db_discovery": False,
        "active_runtime_reuse": False,
        "market_data_source": "SYNTHETIC",
    }
    assert harness.db_path.parent == harness.run_dir
    assert harness.artifact_dir.parent == harness.run_dir
    assert historical.read_bytes() == before
    assert all(item["verdict"] == "PASS" for item in report["faults_injected"])
    assert all(item["injected_at"] and item["expected_behavior"] and item["observed_behavior"]
               and item["invariant_checks"] and item["db_evidence"]
               for item in report["faults_injected"])
    assert len({item["name"] for item in report["faults_injected"]}) == len(report["faults_injected"])

    machine = json.loads(Path(report["report_paths"]["json"]).read_text())
    human = Path(report["report_paths"]["markdown"]).read_text()
    assert machine["overall_verdict"] == "PASS"
    assert machine["report_paths"] == report["report_paths"]
    assert "## Invariant matrix" in human
    assert "## Evidence references" in human
    assert "Overall verdict: **PASS**" in human


def test_each_harness_instance_gets_new_database_and_artifact_directory(tmp_path: Path) -> None:
    first = AutonomousQualificationHarness(mode="FAST", output_root=tmp_path)
    second = AutonomousQualificationHarness(mode="FAST", output_root=tmp_path)
    try:
        assert first.run_id != second.run_id
        assert first.run_dir != second.run_dir
        assert first.db_path != second.db_path
        assert first.artifact_dir != second.artifact_dir
        assert first.db_path.exists() and second.db_path.exists()
    finally:
        first.close(); second.close()


def test_soak_requires_six_to_twenty_four_hours(tmp_path: Path) -> None:
    for hours in (5.99, 24.01):
        with pytest.raises(ValueError, match="between 6 and 24"):
            AutonomousQualificationHarness(mode="SOAK", output_root=tmp_path, soak_hours=hours)


def test_soak_uses_scheduled_faults_and_same_invariants_without_wall_clock_wait(tmp_path: Path) -> None:
    sleeps: list[float] = []
    harness = AutonomousQualificationHarness(mode="SOAK", output_root=tmp_path,
                                             soak_hours=6, sleep=sleeps.append)
    report = harness.run()
    assert report["overall_verdict"] == "PASS"
    assert report["mode"] == "SOAK"
    assert sum(sleeps) == pytest.approx(6 * 3600)
    assert all(delay > 0 for delay in sleeps)
    assert set(FAULT_NAMES).issubset(report["tests_executed"])
    soak = next(item for item in report["faults_injected"] if item["name"] == "soak_normal_market_data")
    assert soak["verdict"] == "PASS"
    assert soak["observed_behavior"]["probe_count"] == len(sleeps)


def test_cli_rejects_short_soak_as_blocked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--mode", "soak", "--soak-hours", "1", "--output-root", str(tmp_path)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["overall_verdict"] == "BLOCKED"


def test_product_scenario_exception_is_reported_and_torn_down(tmp_path: Path) -> None:
    class DefectHarness(AutonomousQualificationHarness):
        def _run_stale_heartbeat(self):  # type: ignore[no-untyped-def]
            self._new_context("stale_heartbeat")
            raise RuntimeError("injected product defect")

    harness = DefectHarness(mode="FAST", output_root=tmp_path)
    report = harness.run()

    failure = next(item for item in report["faults_injected"] if item["name"] == "stale_heartbeat")
    assert report["overall_verdict"] == "NEEDS_FIX"
    assert failure["error"] == "RuntimeError:injected product defect"
    assert failure["classification"] == "UNKNOWN"
    assert report["unexplained_state_transitions"] == []
    with sqlite3.connect(harness.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM burnin_campaign_runs WHERE status='RUNNING'"
        ).fetchone()[0] == 0


def test_public_soak_uses_the_production_market_scanner_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    async def public_scan(config):  # type: ignore[no-untyped-def]
        observed["config"] = config
        return [{"symbol": "BTCUSDT", "source_exchange": "binance"}]

    monkeypatch.setattr(qualification_module, "scan_exchange_markets", public_scan)
    harness = AutonomousQualificationHarness(
        mode="SOAK", output_root=tmp_path, soak_hours=6, market_data_source="PUBLIC",
    )
    try:
        assert harness._normal_market_data() == [
            {"symbol": "BTCUSDT", "source_exchange": "binance"},
        ]
        assert observed["config"].exchange.hyperliquid.enabled is False
    finally:
        harness.close()
