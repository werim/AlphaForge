"""Host serialization regressions; never physical quota/workload evidence."""
import hashlib
import itertools
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest
from alphaforge import storage_volume as v

UUID = '00112233-4455-6677-8899-AaBbCcDdEeFf'
HEADER = '+-o fixture  <class IOPlatformExpertDevice, id 0x100, registered, active>'


def output(properties, *, newline='\n'):
    return (newline.join([HEADER, '  {', *('    '+line for line in properties), '  }', ''])).encode()


def result(raw):
    return SimpleNamespace(stdout=raw, returncode=0)


@pytest.mark.parametrize('order', list(itertools.permutations(range(3))))
@pytest.mark.parametrize('uuid', [UUID, UUID.lower()])
def test_host_hash_preserves_legacy_property_value_across_serialization(monkeypatch, order, uuid):
    props = ['"Unrelated" = "ignored"', f'"IOPlatformUUID"\t=  "{uuid}"', '"Other" = {"nested"="data"}']
    raw = output([props[i] for i in order], newline='\r\n')
    # Reference the actual OLD hash definition, not a normalized UUID surrogate.
    archive = plistlib.dumps([{'Unrelated':'ignored', 'IOPlatformUUID':uuid, 'Other':{'nested':'data'}}])
    legacy = hashlib.sha256(plistlib.loads(archive)[0]['IOPlatformUUID'].encode()).hexdigest()
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw)); return result(raw)
    monkeypatch.setattr(v.subprocess, 'run', run)
    assert v.host_identity() == legacy
    assert calls == [(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice', '-w0'],
                      {'capture_output':True, 'check':True, 'timeout':15})]
    assert legacy != hashlib.sha256(uuid.swapcase().encode()).hexdigest()


@pytest.mark.parametrize('raw', [
    b'', b'not ioreg', b'\xff',
    output(['"Other" = "missing UUID"']),
    output(['"IOPlatformUUID" = ""']),
    output(['"IOPlatformUUID" = 123']),
    output(['"IOPlatformUUID" = <00112233>']),
    output(['"IOPlatformUUID" = "not-a-uuid"']),
    output([f'"IOPlatformUUID" = " {UUID}"']),
    output([f'"IOPlatformUUID" = "{UUID}" trailing']),
    output([f'"IOPlatformUUID" = "{UUID}']),
    output([f'"IOPlatformUUID" = "{UUID}"', f'"IOPlatformUUID" = "{UUID}"']),
    output([f'"IOPlatformUUID" = "{UUID}"'])[:-4],  # missing closing property block
    output([f'"IOPlatformUUID" = "{UUID}"']) + output([f'"IOPlatformUUID" = "{UUID}"']),
    f'"IOPlatformUUID" = "{UUID}"'.encode(),  # no IORegistry object
])
def test_unknown_or_malformed_identity_never_hashes_a_default(monkeypatch, raw):
    monkeypatch.setattr(v.subprocess, 'run', lambda *a, **kw: result(raw))
    with pytest.raises(ValueError, match='HOST_HARDWARE_ID_UNAVAILABLE'):
        v.host_identity()


@pytest.mark.parametrize('error', [subprocess.CalledProcessError(1, ['ioreg']),
                                  subprocess.TimeoutExpired(['ioreg'], 15)])
def test_host_command_failure_and_timeout_never_fall_back(monkeypatch, error):
    calls=[]
    def run(*a, **kw):
        calls.append((a, kw)); raise error
    monkeypatch.setattr(v.subprocess, 'run', run)
    with pytest.raises(type(error)):
        v.host_identity()
    assert len(calls) == 1
    assert calls[0][1]['check'] is True and calls[0][1]['timeout'] == 15


@pytest.mark.skipif(platform.system() != 'Darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
                    reason='actual macOS read-only serialization integration')
def test_actual_canonical_identity_in_and_out_of_unchanged_sandbox(tmp_path):
    # Uses no APFS volume, control/claim, DB, worker or measurement. This verifies
    # actual serialization on THIS host, not universal physical backend readiness.
    legacy = v.plist(['/usr/sbin/ioreg', '-a', '-rd1', '-c', 'IOPlatformExpertDevice'])
    expected = hashlib.sha256(legacy[0]['IOPlatformUUID'].encode()).hexdigest()
    outside = v.host_identity()
    env={**os.environ, 'PYTHONPATH':str(Path(v.__file__).resolve().parents[1]), 'PYTHONDONTWRITEBYTECODE':'1'}
    child = subprocess.run(['/usr/bin/sandbox-exec', '-p', v.sandbox_profile(tmp_path),
                            sys.executable, '-B', '-c',
                            'from alphaforge.storage_volume import host_identity;print(host_identity())'],
                           capture_output=True, timeout=20, check=True, env=env)
    assert child.stdout.decode().strip() == outside == expected
