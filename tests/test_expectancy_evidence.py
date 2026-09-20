from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.expectancy_evidence import fetch_expectancy_as_of, record_expectancy_evidence
from alphaforge.persistence import init_db
from alphaforge.scoring_context import empty_stats_context


def _session() -> Session:
    return Session(init_db("sqlite+pysqlite:///:memory:"))


def _record(session: Session, evidence_id: str, *, resolved_at: str | None,
            evidence_type: str = "ACCEPTED_TRADE", run_id: str = "run-a",
            campaign_id: str = "camp-a", release_id: str = "release-a",
            symbol: str = "BTCUSDT", setup_type: str | None = "BREAKOUT_UP",
            regime: str | None = "TREND", net_r: float = 1.0) -> bool:
    return record_expectancy_evidence(
        session, evidence_id=evidence_id, source_decision_id=f"decision:{evidence_id}",
        evidence_type=evidence_type, decision_time="2026-01-01T00:00:00Z",
        resolved_at=resolved_at, symbol=symbol, side="LONG", setup_type=setup_type,
        regime=regime, reject_reason="LOW_CONFIDENCE" if evidence_type == "REJECT_FORWARD" else None,
        net_r=net_r, run_id=run_id, campaign_id=campaign_id, release_id=release_id,
    )


def _fetch(session: Session, as_of: str, **scope: str) -> dict:
    return fetch_expectancy_as_of(
        session, as_of=as_of, symbol="BTCUSDT", setup_type="BREAKOUT_UP",
        regime="TREND", **scope,
    )


def test_as_of_excludes_future_and_unresolved_accepted_evidence() -> None:
    with _session() as session:
        assert _record(session, "future", resolved_at="2026-01-01T02:00:00Z")
        assert not _record(session, "unresolved", resolved_at=None)
        session.commit()
        assert _fetch(session, "2026-01-01T01:00:00Z") == empty_stats_context()


def test_as_of_includes_resolution_at_inclusive_boundary() -> None:
    with _session() as session:
        assert _record(session, "boundary", resolved_at="2026-01-01T01:00:00Z", net_r=.75)
        session.commit()
        stats = _fetch(session, "2026-01-01T01:00:00Z")
        assert stats["setup"] == {"BREAKOUT_UP": .75}
        assert stats["regime"] == {"TREND": .75}
        assert stats["symbol"] == {"BTCUSDT": .75}
        assert stats["sample_size"] == 1


def test_reject_evidence_is_eligible_only_after_resolution() -> None:
    with _session() as session:
        assert not _record(session, "reject-unresolved", evidence_type="REJECT_FORWARD", resolved_at=None)
        assert _record(session, "reject-resolved", evidence_type="REJECT_FORWARD", resolved_at="2026-01-01T03:00:00Z", net_r=-1.0)
        session.commit()
        assert _fetch(session, "2026-01-01T02:59:59Z") == empty_stats_context()
        assert _fetch(session, "2026-01-01T03:00:00Z")["sample_size"] == 1


def test_future_rows_cannot_change_prior_as_of_result_and_scope_isolated() -> None:
    with _session() as session:
        assert _record(session, "known", resolved_at="2026-01-01T01:00:00Z", net_r=.5)
        session.commit()
        before = _fetch(session, "2026-01-01T01:00:00Z", run_id="run-a", campaign_id="camp-a", release_id="release-a")
        assert _record(session, "future", resolved_at="2026-01-01T04:00:00Z", net_r=-5.0)
        assert _record(session, "other-scope", resolved_at="2026-01-01T01:00:00Z", run_id="run-b", campaign_id="camp-b", release_id="release-b", net_r=-5.0)
        session.commit()
        after = _fetch(session, "2026-01-01T01:00:00Z", run_id="run-a", campaign_id="camp-a", release_id="release-a")
        assert after == before
        assert after == _fetch(session, "2026-01-01T01:00:00Z", run_id="run-a", campaign_id="camp-a", release_id="release-a")


def test_reader_is_sql_only_and_does_not_use_created_at_for_eligibility(monkeypatch) -> None:
    with _session() as session:
        assert _record(session, "known", resolved_at="2026-01-01T01:00:00Z")
        session.commit()
        session.execute(text("UPDATE expectancy_evidence SET created_at='2099-01-01T00:00:00Z' WHERE evidence_id='known'"))
        session.commit()
        monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")))
        assert _fetch(session, "2026-01-01T01:00:00Z")["sample_size"] == 1
