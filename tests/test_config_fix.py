import json
from dataclasses import asdict
from pathlib import Path

import pytest

from alphaforge.config_fix import build_plan, run
from alphaforge.env_contract import parse_dotenv


def _root(tmp_path: Path, content: str) -> Path:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "src" / "alphaforge").mkdir(parents=True)
    (tmp_path / ".env").write_text(content, encoding="utf-8", newline="")
    return tmp_path


def _audit(**kwargs):
    return {"status": "PASS", "errors": [], "warnings": []}


def test_dry_run_never_changes_file_and_is_deterministic(tmp_path):
    root = _root(tmp_path, "BINANCE_DEFAULT_MARKET_TYPE=USDT_M\n")
    before = (root / ".env").read_bytes()
    first, code = run(root=root, process_env={}, audit_fn=_audit)
    second, _ = run(root=root, process_env={}, audit_fn=_audit)
    assert code == 1 and first == second
    assert first["final_status"] == "CHANGES_PROPOSED"
    assert (root / ".env").read_bytes() == before


def test_apply_backup_atomic_idempotent_and_restoreable(tmp_path):
    root = _root(tmp_path, "# operator\r\nBINANCE_DEFAULT_MARKET_TYPE=USD-M\r\n")
    original = (root / ".env").read_bytes()
    report, code = run(apply=True, root=root, process_env={}, audit_fn=_audit)
    assert code == 0 and report["final_status"] == "APPLIED_AND_PASS"
    backup = root / report["backup_path"]
    assert backup.read_bytes() == original
    assert b"\r\n" in (root / ".env").read_bytes()
    assert parse_dotenv(root / ".env")["BINANCE_DEFAULT_MARKET_TYPE"] == "USD_M"
    again, code = run(apply=True, root=root, process_env={}, audit_fn=_audit)
    assert code == 0 and again["applied_changes"] == [] and again["backup_path"] is None
    (root / ".env").write_bytes(backup.read_bytes())
    assert (root / ".env").read_bytes() == original


def test_alias_conflict_preserves_canonical_and_multiple_fixes(tmp_path):
    root = _root(tmp_path, "ALPHAFORGE_BINANCE_RECV_WINDOW_MS=30000\nBINANCE_RECV_WINDOW_MS=5000\nBINANCE_DEFAULT_MARKET_TYPE=USDT-M\n")
    report, _ = run(apply=True, root=root, process_env={}, audit_fn=_audit)
    values = parse_dotenv(root / ".env")
    assert values["ALPHAFORGE_BINANCE_RECV_WINDOW_MS"] == "30000"
    assert "BINANCE_RECV_WINDOW_MS" not in values
    assert values["BINANCE_DEFAULT_MARKET_TYPE"] == "USD_M"
    assert len(report["applied_changes"]) == 2


def test_process_conflict_is_manual_and_never_edits_file(tmp_path):
    root = _root(tmp_path, "BINANCE_TESTNET=false\n")
    before = (root / ".env").read_bytes()
    report, code = run(apply=True, root=root, process_env={"BINANCE_TESTNET": "true"}, audit_fn=_audit)
    assert code == 1 and report["final_status"] == "MANUAL_REVIEW_REQUIRED"
    assert report["next_safe_commands"] == ["Remove-Item Env:BINANCE_TESTNET -ErrorAction SilentlyContinue"]
    assert (root / ".env").read_bytes() == before


def test_secret_value_never_appears_in_plan_or_output(tmp_path):
    secret = "super-secret-value"
    root = _root(tmp_path, "BINANCE_API_SECRET=file-secret\n")
    report, _ = run(root=root, process_env={"BINANCE_API_SECRET": secret}, audit_fn=_audit)
    payload = json.dumps(report)
    assert secret not in payload and "file-secret" not in payload
    assert report["next_safe_commands"] == []


@pytest.mark.parametrize(("raw", "classification"), [("5.0", "AUTO_FIX_SAFE"), ("7.0", "MANUAL_REVIEW_REQUIRED")])
def test_daily_loss_conversion_only_when_unambiguous(tmp_path, raw, classification):
    root = _root(tmp_path, f"ALPHAFORGE_MAX_DAILY_LOSS_PCT={raw}\n")
    changes, unresolved = build_plan(root / ".env", process_env={})
    rows = [*map(asdict, changes), *unresolved]
    assert rows[0]["classification"] == classification


def test_identical_duplicates_are_commented_and_highest_definition_kept(tmp_path):
    root = _root(tmp_path, "BINANCE_DEFAULT_MARKET_TYPE=USD_M\n# note\nBINANCE_DEFAULT_MARKET_TYPE=USD_M\n")
    report, code = run(apply=True, root=root, process_env={}, audit_fn=_audit)
    assert code == 0 and report["iterations"] == 1
    assert (root / ".env").read_text().count("BINANCE_DEFAULT_MARKET_TYPE=USD_M") == 2
    assert len([line for line in (root / ".env").read_text().splitlines() if line.startswith("BINANCE_DEFAULT")]) == 1


def test_invalid_syntax_does_not_change_or_backup(tmp_path):
    root = _root(tmp_path, "NOT VALID\n")
    before = (root / ".env").read_bytes()
    report, code = run(apply=True, root=root, process_env={}, audit_fn=_audit)
    assert code == 3 and report["final_status"] == "FAILED"
    assert (root / ".env").read_bytes() == before
    assert not list(root.glob(".env.backup-*"))


def test_stall_detection_for_new_finding_without_progress(tmp_path):
    root = _root(tmp_path, "BINANCE_DEFAULT_MARKET_TYPE=USDT_M\n")
    calls = 0
    def audit(**kwargs):
        nonlocal calls
        calls += 1
        return {"status": "FAIL", "errors": ["persistent"], "warnings": []}
    report, code = run(apply=True, root=root, process_env={}, audit_fn=audit)
    assert code == 1
    assert report["final_status"] == "STALLED"
    assert calls == 2


def test_iteration_limit_enforced(tmp_path, monkeypatch):
    root = _root(tmp_path, "BINANCE_DEFAULT_MARKET_TYPE=USDT_M\n")
    # A limit of zero is an explicit no-mutation safety bound.
    report, code = run(apply=True, root=root, process_env={}, max_iterations=0, audit_fn=_audit)
    assert code == 1 and report["final_status"] == "STALLED"
    assert parse_dotenv(root / ".env")["BINANCE_DEFAULT_MARKET_TYPE"] == "USDT_M"
