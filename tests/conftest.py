from __future__ import annotations

import pytest


_WATCHDOG_BACKLOG_TESTS = {
    "test_watch_transient_backlog_growth_keeps_fresh_live_campaign_running",
    "test_watch_sustained_overdue_backlog_growth_still_fail_closes",
}


@pytest.fixture(autouse=True)
def _isolate_watchdog_backlog_tests_from_host_process_identity(request, monkeypatch):
    """Keep backlog-state tests focused on watchdog severity, not pytest PID identity."""
    if request.node.name in _WATCHDOG_BACKLOG_TESTS:
        monkeypatch.setattr("alphaforge.burnin_ops._pid_alive", lambda _pid: True)
