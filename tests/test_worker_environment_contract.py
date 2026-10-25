from __future__ import annotations

from alphaforge.config_audit import audit_config


def test_launcher_context_is_known_but_unknown_names_still_fail():
    env = {
        "ALPHAFORGE_RELEASE_ID": "worker-release-wt-123456789abc",
        "ALPHAFORGE_BURNIN_DATABASE_PATH": "/quota/experiment/laboratory.db",
    }
    report = audit_config(env=env)
    assert report["status"] != "FAIL", report["errors"]
    assert not report["unknown_process_variables"]
    typo = "ALPHAFORGE_BURNIN_DATABASE_PATTH"
    invalid = audit_config(env={**env, typo: env["ALPHAFORGE_BURNIN_DATABASE_PATH"]})
    assert invalid["status"] == "FAIL"
    assert invalid["unknown_process_variables"] == [typo]


def test_operator_profile_does_not_inject_blank_launcher_context(tmp_path):
    from pathlib import Path
    from alphaforge.env_contract import bootstrap_environment
    from alphaforge.config_registry import CONTRACT_BY_NAME, REGISTRY_BY_ENV

    root = Path(__file__).resolve().parents[1]
    (tmp_path / ".env").write_text((root / ".env.example").read_text())
    env = {}
    bootstrap_environment(tmp_path, environ=env)
    for name in ("ALPHAFORGE_RELEASE_ID", "ALPHAFORGE_BURNIN_DATABASE_PATH"):
        assert name not in env
        assert CONTRACT_BY_NAME[name].launcher_owned is True
        assert name not in REGISTRY_BY_ENV  # no competing dashboard/config authority


def test_release_context_reaches_runtime_identity(monkeypatch, tmp_path):
    from sqlalchemy.orm import Session
    from alphaforge.ai_brain import AIBrain
    from alphaforge.persistence import init_db
    from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator

    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}")
    with Session(engine) as session:
        runtime = RuntimeOrchestrator(
            config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            ai_brain=AIBrain(session, min_accept_score=0.62),
            market_scanner=lambda: [], persistence_engine=engine,
        )
        monkeypatch.setenv("ALPHAFORGE_RELEASE_ID", "owned-worker-release")
        identity = runtime._phase8_runtime_hashes(["BTCUSDT"], ["1h"])
        assert identity["release_id"] == "owned-worker-release"
        assert identity["release_id_source"] == "environment:ALPHAFORGE_RELEASE_ID"
        monkeypatch.delenv("ALPHAFORGE_RELEASE_ID")
        fallback = runtime._phase8_runtime_hashes(["BTCUSDT"], ["1h"])
        assert fallback["release_id"] == runtime.config.phase7_burnin_release_id
        assert fallback["release_id_source"] == "runtime_config:phase7_burnin_release_id"
        assert identity["config_hash"] == fallback["config_hash"]
    engine.dispose()


def test_burnin_db_context_resolves_readonly_without_creating_target(monkeypatch, tmp_path):
    from alphaforge.db_doctor.targets import ENV_TARGETS, resolve_database_targets

    for key in ENV_TARGETS:
        monkeypatch.delenv(key, raising=False)
    target = tmp_path / "absent" / "worker.db"
    monkeypatch.setenv("ALPHAFORGE_BURNIN_DATABASE_PATH", str(target))
    report = resolve_database_targets()
    assert report["selected"]["source"] == "ALPHAFORGE_BURNIN_DATABASE_PATH"
    assert report["selected"]["canonical_path"] == str(target.resolve())
    assert report["conflict"] is False
    assert not target.parent.exists()
    monkeypatch.setenv("ALPHAFORGE_DATABASE_URL", f"sqlite:///{tmp_path / 'other.db'}")
    conflict = resolve_database_targets()
    assert conflict["conflict"] is True
    assert conflict["selected"] is None
    assert not target.parent.exists()
