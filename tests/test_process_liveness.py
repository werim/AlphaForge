from datetime import datetime, timezone
import alphaforge.process_liveness as liveness
import alphaforge.burnin_ops as burnin_ops


def test_windows_liveness_is_query_only_and_preserves_legitimate_worker(monkeypatch):
    creation=datetime(2026,8,31,tzinfo=timezone.utc).timestamp()
    calls=[]
    monkeypatch.setattr(liveness.os, 'name', 'nt')
    monkeypatch.setattr(liveness, '_windows_process', lambda pid: (calls.append(pid) or True, creation))
    monkeypatch.setattr(liveness.os, 'kill', lambda *_: (_ for _ in ()).throw(AssertionError('Windows probe must never call os.kill')))
    assert liveness.process_is_alive(42, expected_command_parts=('alphaforge','campaign'), expected_started_at='2026-08-31T00:00:00Z')
    assert calls == [42]


def test_windows_recycled_pid_fails_identity_without_termination(monkeypatch):
    monkeypatch.setattr(liveness.os, 'name', 'nt')
    monkeypatch.setattr(liveness, '_windows_process', lambda _pid: (True, datetime(2026,8,30,tzinfo=timezone.utc).timestamp()))
    monkeypatch.setattr(liveness.os, 'kill', lambda *_: (_ for _ in ()).throw(AssertionError('must not terminate recycled PID')))
    assert not liveness.process_is_alive(43, expected_command_parts=('alphaforge',), expected_started_at='2026-08-31T00:00:00Z')


def test_windows_dead_pid_reports_not_alive(monkeypatch):
    monkeypatch.setattr(liveness.os, 'name', 'nt')
    monkeypatch.setattr(liveness, '_windows_process', lambda _pid: (False, None))
    assert not liveness.process_is_alive(44)


def test_macos_identity_uses_native_command_and_start_time(monkeypatch):
    monkeypatch.setattr(liveness.os, 'name', 'posix')
    monkeypatch.setattr(liveness.sys, 'platform', 'darwin')
    monkeypatch.setattr(liveness.os, 'kill', lambda *_: None)
    creation = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    command = '/usr/bin/python -m alphaforge.burnin_cli worker --campaign-id campaign'
    monkeypatch.setattr(liveness, '_darwin_native_identity', lambda _pid: (creation, command))
    alive, creation, command = liveness._darwin_process(45)
    assert alive
    assert creation == datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    assert command == '/usr/bin/python -m alphaforge.burnin_cli worker --campaign-id campaign'


def test_macos_alive_pid_does_not_fail_when_identity_is_unavailable(monkeypatch):
    monkeypatch.setattr(liveness.os, 'name', 'posix')
    monkeypatch.setattr(liveness.sys, 'platform', 'darwin')
    monkeypatch.setattr(liveness, '_darwin_process', lambda _pid: (True, None, None))
    assert liveness.process_is_alive(45, expected_command_parts=('alphaforge', 'campaign'), expected_started_at='2026-09-01T00:00:00Z')


def test_macos_alive_campaign_worker_is_conservatively_alive(monkeypatch):
    monkeypatch.setattr(liveness.os, 'name', 'posix')
    monkeypatch.setattr(liveness.sys, 'platform', 'darwin')
    monkeypatch.setattr(liveness, '_darwin_process', lambda _pid: (True, None, None))
    campaign={'campaign_id':'camp_macos','worker_pid':46,'worker_started_at':'2026-09-01T00:00:00Z'}
    assert burnin_ops._campaign_worker_alive(campaign)


def test_macos_recycled_pid_fails_command_and_start_identity(monkeypatch):
    expected=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(liveness.os, 'name', 'posix')
    monkeypatch.setattr(liveness.sys, 'platform', 'darwin')
    monkeypatch.setattr(liveness, '_darwin_process', lambda _pid: (True, expected + 3600, '/usr/bin/unrelated'))
    assert not liveness.process_is_alive(47, expected_command_parts=('alphaforge','camp_macos'), expected_started_at='2026-09-01T00:00:00Z')


def test_macos_matching_worker_passes_command_and_start_identity(monkeypatch):
    expected=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(liveness.os, 'name', 'posix')
    monkeypatch.setattr(liveness.sys, 'platform', 'darwin')
    monkeypatch.setattr(liveness, '_darwin_process', lambda _pid: (True, expected, '/usr/bin/python -m alphaforge.burnin_cli worker --campaign-id camp_macos'))
    assert liveness.process_is_alive(48, expected_command_parts=('alphaforge','camp_macos'), expected_started_at='2026-09-01T00:00:00Z')


def test_linux_inspectable_wrong_command_and_recycled_pid_fail(monkeypatch):
    expected=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(liveness.os, 'name', 'posix')
    monkeypatch.setattr(liveness.sys, 'platform', 'linux')
    monkeypatch.setattr(liveness, '_posix_process', lambda _pid: (True, expected - 3600, '/usr/bin/unrelated'))
    assert not liveness.process_is_alive(47, expected_command_parts=('alphaforge','camp_linux'), expected_started_at='2026-09-01T00:00:00Z')


def test_posix_dead_pid_still_fails(monkeypatch):
    monkeypatch.setattr(liveness.os, 'name', 'posix')
    monkeypatch.setattr(liveness.sys, 'platform', 'linux')
    monkeypatch.setattr(liveness, '_posix_process', lambda _pid: (False, None, None))
    assert not liveness.process_is_alive(48, expected_started_at='2026-09-01T00:00:00Z')
