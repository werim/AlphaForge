import pytest
from sqlalchemy import create_engine, text

from alphaforge.release_canary_evidence import persist_campaign_canary_evidence
from alphaforge.release_gates import ensure_release_gate_schema


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    ensure_release_gate_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE burnin_campaigns(
                campaign_id TEXT PRIMARY KEY,
                release_id TEXT NOT NULL,
                git_commit TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO burnin_campaigns(campaign_id,release_id,git_commit)
            VALUES ('camp','rel','abc')
        """))
    return engine


def _runner(commit="abc", status=""):
    def run(args, cwd):
        if args == ["rev-parse", "HEAD"]:
            return commit
        if args == ["status", "--porcelain=v1"]:
            return status
        raise AssertionError(args)
    return run


def test_campaign_canary_evidence_is_bound_to_exact_clean_checkout(tmp_path):
    engine = _engine()
    result = persist_campaign_canary_evidence(
        engine,
        campaign_id="camp",
        repo_path=tmp_path,
        git_runner=_runner(),
    )
    assert result["status"] == "PASS"
    assert result["campaign_git_commit"] == "abc"
    assert result["checkout_git_commit"] == "abc"
    assert result["evidence"]["git_commit"] == "abc"


def test_campaign_canary_evidence_rejects_commit_mismatch_before_write(tmp_path):
    engine = _engine()
    with pytest.raises(ValueError, match="CAMPAIGN_CHECKOUT_COMMIT_MISMATCH"):
        persist_campaign_canary_evidence(
            engine,
            campaign_id="camp",
            repo_path=tmp_path,
            git_runner=_runner("different"),
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM canary_run_events")).scalar_one() == 0


def test_campaign_canary_evidence_rejects_dirty_checkout_before_write(tmp_path):
    engine = _engine()
    with pytest.raises(ValueError, match="WORKTREE_DIRTY"):
        persist_campaign_canary_evidence(
            engine,
            campaign_id="camp",
            repo_path=tmp_path,
            git_runner=_runner("abc", "?? local.py"),
        )
