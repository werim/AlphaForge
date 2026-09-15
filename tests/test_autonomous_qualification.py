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
    assert soak["observed_behavior"]["heartbeat_fresh_probes"] == len(sleeps)
    assert soak["observed_behavior"]["resolver_ok_probes"] == len(sleeps)
    assert soak["observed_behavior"]["lineage_ok_probes"] == len(sleeps)
    assert soak["invariant_checks"]["continuous_safety_invariants"] is True
    assert soak["invariant_checks"]["resource_growth_bounded"] is True
    assert report["soak_resources"]["sample_count"] == len(sleeps)
    assert report["soak_sample_invariant_failures"] == []
    assert Path(report["soak_resources"]["sample_artifact"]).is_file()


def test_soak_audits_and_recovers_isolated_empty_public_feed_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = AutonomousQualificationHarness(
        mode="SOAK", output_root=tmp_path, soak_hours=6,
        market_data_source="PUBLIC", sleep=lambda _seconds: None,
    )
    scans = 0
    def public_scan() -> list[dict[str, str]]:
        nonlocal scans
        scans += 1
        return [] if scans in {2, 5} else [{"symbol": "BTCUSDT"}]
    monkeypatch.setattr(harness, "_normal_market_data", public_scan)
    report = harness.run()
    soak = next(item for item in report["faults_injected"] if item["name"] == "soak_normal_market_data")
    observed = soak["observed_behavior"]
    assert report["overall_verdict"] == "PASS"
    assert observed["empty_market_data_probes"] == 2
    assert observed["max_consecutive_empty_market_data_probes"] == 1
    assert len(observed["market_data_recoveries"]) == 2
    assert all(item["db_event_id"] > 0 for item in observed["market_data_probes"])
    assert all(item["db_event_id"] > 0 for item in observed["market_data_recoveries"])
    assert soak["invariant_checks"]["empty_market_data_never_executed"] is True


def test_consecutive_public_feed_gaps_fail_release_gate(tmp_path: Path) -> None:
    harness = AutonomousQualificationHarness(
        mode="SOAK", output_root=tmp_path, soak_hours=6,
        market_data_source="PUBLIC", sleep=lambda _seconds: None,
    )
    ctx = harness._new_context("market_data_probe_regression", qualification_targets=True)
    try:
        harness._record_market_data_probe(ctx, [{"symbol": "BTCUSDT"}], 0.1)
        harness._record_market_data_probe(ctx, [], 0.1)
        harness._record_market_data_probe(ctx, [], 0.1)
        harness._record_market_data_probe(ctx, [{"symbol": "BTCUSDT"}], 0.1)
        assert harness._market_data_max_empty_streak == 2
        assert len(harness._market_data_recoveries) == 1
        assert harness._market_data_recoveries[0]["empty_probe_count"] == 2
        with harness.engine.connect() as conn:
            assert conn.exec_driver_sql(
                "SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=? "
                "AND event_type='QUALIFICATION_MARKET_DATA_PROBE'", (ctx.campaign_id,)
            ).scalar_one() == 4
    finally:
        harness._terminalize(ctx)
        harness.close()


def test_soak_rejects_sustained_public_feed_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = AutonomousQualificationHarness(
        mode="SOAK", output_root=tmp_path, soak_hours=6,
        market_data_source="PUBLIC", sleep=lambda _seconds: None,
    )
    scans = 0
    def public_scan() -> list[dict[str, str]]:
        nonlocal scans
        scans += 1
        return [] if scans in {2, 3} else [{"symbol": "BTCUSDT"}]
    monkeypatch.setattr(harness, "_normal_market_data", public_scan)
    report = harness.run()
    soak = next(item for item in report["faults_injected"] if item["name"] == "soak_normal_market_data")
    assert report["overall_verdict"] == "NEEDS_FIX"
    assert soak["invariant_checks"]["normal_market_data_observed"] is False
    assert soak["invariant_checks"]["empty_market_data_never_executed"] is True
    assert soak["observed_behavior"]["max_consecutive_empty_market_data_probes"] == 2


def test_resource_gate_flags_sustained_accumulation(tmp_path: Path) -> None:
    harness = AutonomousQualificationHarness(mode="SOAK", output_root=tmp_path,
                                             soak_hours=6, sleep=lambda _seconds: None)
    try:
        harness._soak_samples = [
            {"elapsed_seconds": index * 30,
             "rss_high_water_bytes": 80 * 1024 * 1024 + index * 1024 * 1024,
             "db_bytes": 1_000_000 + index * 100,
             "artifact_bytes": index * 100,
             "queue_depths": {"shadow": index, "reconciliation_deferred": 0},
             "pending_resolver_backlog": index, "pending_reject_backlog": 0,
             "sqlite_lock_retry_exhaustions": 0,
             "reconciliation_latency_seconds": 0.002,
             "scan_latency_seconds": None}
            for index in range(40)
        ]
        flags = harness._soak_resource_summary()["growth_flags"]
        assert "SUSTAINED_MONOTONIC_RSS_GROWTH" in flags
        assert "UNBOUNDED_QUEUE_OR_BACKLOG" in flags
    finally:
        harness.close()


def test_resource_gate_allows_bounded_audit_growth_but_flags_excess(tmp_path: Path) -> None:
    harness = AutonomousQualificationHarness(mode="SOAK", output_root=tmp_path,
                                             soak_hours=6, sleep=lambda _seconds: None)
    try:
        def samples(bytes_per_probe: int) -> list[dict[str, object]]:
            return [
                {"elapsed_seconds": index * 30, "rss_high_water_bytes": 80 * 1024 * 1024,
                 "db_bytes": 1_000_000 + index * bytes_per_probe,
                 "artifact_bytes": index * 100,
                 "queue_depths": {"shadow": 0, "reconciliation_deferred": 0},
                 "pending_resolver_backlog": 0, "pending_reject_backlog": 0,
                 "sqlite_lock_retry_exhaustions": 0,
                 "reconciliation_latency_seconds": 0.002,
                 "scan_latency_seconds": None}
                for index in range(720)
            ]
        harness._soak_samples = samples(100 * 1024)
        assert "DB_GROWTH_ABOVE_AUDIT_BUDGET" not in harness._soak_resource_summary()["growth_flags"]
        harness._soak_samples = samples(250 * 1024)
        assert "DB_GROWTH_ABOVE_AUDIT_BUDGET" in harness._soak_resource_summary()["growth_flags"]
    finally:
        harness.close()


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
