from datetime import datetime, timezone

import alphaforge.process_liveness as liveness


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
