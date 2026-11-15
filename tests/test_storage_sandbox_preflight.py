"""Non-destructive sandbox/attempt boundaries; never a physical quota test."""
import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from alphaforge import storage_volume as v


def test_inner_sandbox_failure_stops_before_claim_or_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "inspect_volume", lambda *a: {"mount": str(tmp_path), "cap_bytes": 10_000_000})
    monkeypatch.setattr(v, "container_free", lambda *a: 10**12)
    monkeypatch.setattr(v.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=71, stdout=b"", stderr=b"sandbox-exec: sandbox_apply: Operation not permitted"))
    writes = []
    def no_fill(*args):
        writes.append(True)
        raise OSError(errno.EPERM, "fixture refuses every fill")
    monkeypatch.setattr(v.os, "write", no_fill)
    try:
        v.probe(tmp_path, 10_000_000, "a"*40, 100)
    except ValueError:
        pass
    assert not writes, "failed canonical sandbox verification must stop before any fill"
    assert not (tmp_path / ".af-capacity-proof.control").exists(), "preflight must precede consumption"


def success(command, **kwargs):
    import json, sys
    assert command[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert command[3:5] == [sys.executable, "-c"]
    assert command[5] == v._SANDBOX_CHECK
    assert kwargs == {"capture_output": True, "timeout": 10}
    nonce, inside, outside = command[-3:]
    assert command[2] == v.sandbox_profile(Path(inside).parent)
    Path(inside).write_text(nonce)
    return SimpleNamespace(returncode=0, stdout=json.dumps({"nonce":nonce,"outside_errno":errno.EPERM}).encode(), stderr=b"")


def test_canonical_preflight_executes_inner_sandbox_and_cleans_only_own_challenges(tmp_path, monkeypatch):
    monkeypatch.setattr(v.subprocess, "run", success)
    unrelated = tmp_path / "keep"
    unrelated.write_text("immutable")
    inode = unrelated.stat().st_ino
    result = v.sandbox_preflight(tmp_path, "a"*40)
    assert v.sandbox_verified(result, tmp_path, "a"*40)
    assert result["status"] == "SANDBOX_VERIFIED"
    assert result["inside_write"] and result["outside_absent"]
    assert not (tmp_path / ".af-capacity-proof.control").exists()
    assert list(tmp_path.iterdir()) == [unrelated]
    assert unrelated.read_text() == "immutable" and unrelated.stat().st_ino == inode


@pytest.mark.parametrize("failure", ["nested", "missing_binary", "permission", "timeout", "interrupt",
    "malformed", "extra", "wrong_nonce", "missing_errno", "bool_errno", "wrong_errno", "list",
    "missing_inside", "outer_pass_inner_fail", "nonzero_after_inside", "contradictory_stderr", "outside_allowed"])
def test_unsupported_or_ambiguous_context_never_consumes_or_fills(tmp_path, monkeypatch, failure):
    import json, subprocess
    monkeypatch.setattr(v, "inspect_volume", lambda *a: {"mount":str(tmp_path),"cap_bytes":10_000_000})
    monkeypatch.setattr(v, "container_free", lambda *a: 10**12)
    def run(command, **kwargs):
        if failure == "missing_binary": raise FileNotFoundError(errno.ENOENT, "missing")
        if failure == "permission": raise PermissionError(errno.EACCES, "spawn denied")
        if failure == "timeout": raise subprocess.TimeoutExpired(command, 10, output=b"partial", stderr=b"timeout")
        if failure == "interrupt": raise KeyboardInterrupt()
        result = success(command, **kwargs)
        payload = json.loads(result.stdout)
        if failure == "nested":
            Path(command[-2]).unlink()
            result.returncode=71; result.stdout=b""; result.stderr=b"sandbox-exec: sandbox_apply: Operation not permitted"
        if failure == "malformed": result.stdout=b"not JSON"
        if failure == "extra": payload["outer_status"]="PASS"
        if failure == "wrong_nonce": payload["nonce"]="0"*32
        if failure == "missing_errno": payload.pop("outside_errno")
        if failure == "bool_errno": payload["outside_errno"]=True
        if failure == "wrong_errno": payload["outside_errno"]=errno.ENOSPC
        if failure == "list": payload=[]
        if failure == "missing_inside": Path(command[-2]).unlink()
        if failure == "outer_pass_inner_fail":
            result.returncode=71; result.stdout=b'{"outer_sandbox":"PASS"}'
        if failure == "nonzero_after_inside": result.returncode=1
        if failure == "contradictory_stderr": result.stderr=b"sandbox_apply: Operation not permitted"
        if failure == "outside_allowed": Path(command[-1]).write_text(command[-3])
        if failure not in {"nested","malformed","outer_pass_inner_fail"}: result.stdout=json.dumps(payload).encode()
        return result
    monkeypatch.setattr(v.subprocess, "run", run)
    monkeypatch.setattr(v.os, "write", lambda *a: pytest.fail("no quota-fill write is allowed"))
    with pytest.raises(v.SandboxPreflightError) as caught:
        v.probe(tmp_path, 10_000_000, "a"*40, 100)
    evidence = caught.value.evidence
    assert evidence["status"] == "FAILED"
    assert evidence["source_sha"] == "a"*40
    assert not v.sandbox_verified(evidence, tmp_path, "a"*40)
    assert not list(tmp_path.iterdir())
    if failure == "nested": assert "sandbox_apply" in evidence["stderr"]
    if failure == "timeout": assert evidence["stdout"] == "partial"


@pytest.mark.parametrize("field", ["sandbox_verification", "status", "source_sha", "profile_sha256", "script_sha256", "stdout", "inside_write", "outside_errno"])
def test_missing_or_altered_isolation_evidence_cannot_be_promoted(tmp_path, monkeypatch, field):
    from test_storage_measurement import physical_fixture
    from alphaforge.burnin import canonical_hash
    _, result = physical_fixture(tmp_path, monkeypatch)
    path = tmp_path / ".af-capacity-proof.control"
    if field == "sandbox_verification": result.pop(field)
    else: result["sandbox_verification"].pop(field)
    control = v.ControlFile(path); control.put(1, result); control.close()
    before = (path.read_bytes(), path.stat().st_ino)
    with pytest.raises(ValueError, match="PHYSICAL_BACKEND"):
        v.verify(tmp_path, 10_000_000, canonical_hash(result), "a"*40)
    assert (path.read_bytes(), path.stat().st_ino) == before


def test_failed_physical_attempt_remains_nonadmissible_and_immutable(tmp_path, monkeypatch):
    from test_storage_measurement import physical_fixture
    from alphaforge.burnin import canonical_hash
    _, result = physical_fixture(tmp_path, monkeypatch)
    result.update(status="FAILED", sandbox_denial=False)
    path = tmp_path / ".af-capacity-proof.control"
    control = v.ControlFile(path); control.put(1,result); control.close()
    before = (path.read_bytes(),path.stat().st_ino)
    with pytest.raises(ValueError): v.verify(tmp_path,10_000_000,canonical_hash(result),"a"*40)
    with pytest.raises(ValueError,match="EMPTY_DEDICATED"): v.probe(tmp_path,10_000_000,"a"*40,100)
    assert (path.read_bytes(),path.stat().st_ino) == before


def test_cli_preflight_is_not_a_physical_proof_or_launch(tmp_path, monkeypatch, capsys):
    import json
    from alphaforge import storage_measurement as m
    monkeypatch.setattr(m, "source_identity", lambda: {"sha":"a"*40,"dirty":False})
    monkeypatch.setattr(v, "inspect_volume", lambda *a: {"mount":str(tmp_path)})
    monkeypatch.setattr(v.subprocess,"run",success)
    assert m.main(["sandbox-preflight","--mount",str(tmp_path),"--cap-bytes","10000000"]) == 0
    result=json.loads(capsys.readouterr().out)
    assert result["admission_eligible"] is False and result["physical_acceptance"] == "NOT_RUN"
    assert result["verification"]["status"] == "SANDBOX_VERIFIED"
    assert not list(tmp_path.iterdir())


def test_cli_probe_keeps_failure_diagnostics_without_physical_attempt(tmp_path, monkeypatch, capsys):
    import json
    from alphaforge import storage_measurement as m
    monkeypatch.setattr(m,"source_identity",lambda:{"sha":"a"*40,"dirty":False})
    monkeypatch.setattr(v,"inspect_volume",lambda *a:{"mount":str(tmp_path),"cap_bytes":10_000_000})
    monkeypatch.setattr(v,"container_free",lambda *a:10**12)
    monkeypatch.setattr(v.subprocess,"run",lambda *a,**k:SimpleNamespace(returncode=71,
        stdout=b'{"outer":"PASS"}',stderr=b'sandbox_apply: Operation not permitted'))
    assert m.main(["probe","--mount",str(tmp_path),"--cap-bytes","10000000",
                   "--host-reserve-bytes","100"]) == 3
    result=json.loads(capsys.readouterr().out)
    assert result["status"] == "FAILED" and result["physical_attempt_started"] is False
    assert result["admission_eligible"] is False
    assert "sandbox_apply" in result["sandbox_verification"]["stderr"]
    assert result["sandbox_verification"]["stdout"] == '{"outer":"PASS"}'
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("sha", [None, "", "UNKNOWN", "a"*39, "g"*40])
def test_missing_source_cannot_become_verification(tmp_path, monkeypatch, sha):
    monkeypatch.setattr(v.subprocess,"run",lambda *a,**k:pytest.fail("invalid source before spawn"))
    with pytest.raises(ValueError,match="SOURCE_SHA_INVALID"): v.sandbox_preflight(tmp_path,sha)
    assert not list(tmp_path.iterdir())


def test_failure_diagnostics_remain_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(v.subprocess,"run",lambda *a,**k:SimpleNamespace(returncode=71,
        stdout=b'x'*100000,stderr=b'y'*100000))
    result=v.sandbox_preflight(tmp_path,"a"*40)
    assert result["status"] == "FAILED"
    assert len(result["stdout"]) <= 65536 and len(result["stderr"]) <= 65536
    assert not list(tmp_path.iterdir())


def test_conflicting_top_level_sandbox_returncode_cannot_override_inner_evidence(tmp_path, monkeypatch):
    from test_storage_measurement import physical_fixture
    from alphaforge.burnin import canonical_hash
    _, result = physical_fixture(tmp_path, monkeypatch)
    result['sandbox_returncode'] = 71
    path = tmp_path / '.af-capacity-proof.control'
    control = v.ControlFile(path); control.put(1, result); control.close()
    with pytest.raises(ValueError): v.verify(tmp_path, 10_000_000, canonical_hash(result), 'a'*40)
