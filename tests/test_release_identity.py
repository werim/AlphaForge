from pathlib import Path

import pytest

from alphaforge.release_identity import checkout_identity, require_campaign_checkout


def _runner(commit="abc", status=""):
    def run(args, cwd: Path):
        if args == ["rev-parse", "HEAD"]:
            return commit
        if args == ["status", "--porcelain=v1"]:
            return status
        raise AssertionError(args)
    return run


def test_checkout_identity_reports_clean_exact_commit(tmp_path):
    identity = checkout_identity(tmp_path, git_runner=_runner("abc", ""))
    assert identity["status"] == "PASS"
    assert identity["git_commit"] == "abc"
    assert identity["clean"] is True


def test_require_campaign_checkout_rejects_commit_mismatch(tmp_path):
    with pytest.raises(ValueError, match="CAMPAIGN_CHECKOUT_COMMIT_MISMATCH"):
        require_campaign_checkout("campaign-sha", repo_path=tmp_path, git_runner=_runner("other-sha", ""))


def test_require_campaign_checkout_rejects_dirty_worktree(tmp_path):
    with pytest.raises(ValueError, match="WORKTREE_DIRTY"):
        require_campaign_checkout("abc", repo_path=tmp_path, git_runner=_runner("abc", " M changed.py"))


def test_require_campaign_checkout_rejects_unavailable_git(tmp_path):
    def broken(args, cwd):
        raise RuntimeError("git unavailable")

    with pytest.raises(ValueError, match="GIT_IDENTITY_UNAVAILABLE"):
        require_campaign_checkout("abc", repo_path=tmp_path, git_runner=broken)


def test_require_campaign_checkout_rejects_missing_campaign_commit(tmp_path):
    with pytest.raises(ValueError, match="CAMPAIGN_GIT_COMMIT_MISSING"):
        require_campaign_checkout("", repo_path=tmp_path, git_runner=_runner())
