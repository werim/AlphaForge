# PR #307 merged dev HEAD audit

- Audit time: 2026-07-28 UTC
- Audited merge commit: `4d1cdd7a64df14b8034dc8b356c12a01f11aaf88`
- Commit subject: `Merge pull request #307 from werim/codex/update-unicode-symbol-handling-rules`
- Verdict: **local source suite PASS; reproducible GitHub Actions parity and run identity NOT VERIFIED**
- LIVE verdict: **NOT LIVE READY**

## Scope and method

The audit made no runtime, strategy, sizing, threshold, lifecycle, schema, persistence,
export, or LIVE-control change. Constant RR and constant score were not reopened: the
current suite disclosed no failure asserting recurrence, and this audit did not create
or claim a new runtime distribution sample.

The dependency commands from `.github/workflows/test.yml` were executed verbatim under
the available Python 3.11.15 interpreter. The exact workflow install did **not** complete:
the proxy rejected PyPI with HTTP 403, `pip install -r requirements.txt` entered the
workflow's fallback, and `pip install sqlalchemy pytest` also failed. This dependency
failure is retained rather than hidden. The requested checks were then executed under
the container's already provisioned Python 3.12.13 environment; therefore their results
are strong local source evidence but are not a clean-room reproduction of the Actions
Python 3.11 job.

## Results

| Check | Result | Evidence |
|---|---:|---|
| Exact Actions dependency install, Python 3.11.15 | **FAIL** (network 403; requirements and fallback both failed) | `01-github-actions-dependency-install.log` |
| `python -c "import alembic; print(alembic.__version__)"` | **PASS**, Alembic `1.18.5` | `02-head-environment-alembic.log` |
| `python -m pytest -q -rs` | **PASS**: **1072 passed, 0 failed, 3 skipped**, 244 warnings | `03-pytest-q-rs.log` |
| `python -m compileall -q src tests alembic` | **PASS** | `04-compileall.log` |
| `python -m alphaforge.config_check` | **FAIL**: package is not installed and `src` is not on `sys.path` | `05-config-check.log` |
| `git diff --check` | **PASS** | `06-git-diff-check.log` |
| Diagnostic `PYTHONPATH=src python -m alphaforge.config_check` | **PASS** | `07-config-check-pythonpath-diagnostic.log` |
| GitHub Actions API lookup | **UNAVAILABLE**: proxy tunnel HTTP 403 | `08-github-actions-api.log` |

The exact test counts are **1072 passed, 0 failed, and 3 skipped**.

## Every skip and reason

1. `tests/test_exchange_connectivity.py:191` — `Set ALPHAFORGE_RUN_EXCHANGE_INTEGRATION=1 to run live integration checks`.
2. `tests/test_exchange_connectivity.py:200` — `Set ALPHAFORGE_RUN_EXCHANGE_INTEGRATION=1 to run live integration checks`.
3. `tests/test_timesfm_futures.py:216` — `set ALPHAFORGE_RUN_TIMESFM_INTEGRATION=1 to run the optional real TimesFM smoke test`.

No skip is interpreted as LIVE, exchange, or TimesFM acceptance evidence.

## Failures grouped by JOB-01 through JOB-14

The repository does not provide a canonical JOB-01–JOB-14 test-to-job manifest, so this
table reports failures without inventing ownership. “No pytest failure” means only that
no failed test node was available to assign; it does not convert skipped external checks
into evidence.

| Job | Pytest failures | Audit/check failures or limitations |
|---|---:|---|
| JOB-01 | 0 | None assigned |
| JOB-02 | 0 | None assigned |
| JOB-03 | 0 | None assigned |
| JOB-04 | 0 | No constant-RR recurrence evidence; not reopened |
| JOB-05 | 0 | None assigned |
| JOB-06 | 0 | None assigned |
| JOB-07 | 0 | None assigned |
| JOB-08 | 0 | None assigned |
| JOB-09 | 0 | Two exchange integration skips; no credentialed acceptance claim |
| JOB-10 | 0 | None assigned |
| JOB-11 | 0 | None assigned |
| JOB-12 | 0 | None assigned |
| JOB-13 | 0 | None assigned |
| JOB-14 | 0 | Exact dependency install failed; exact config command failed; Actions identity unavailable |

## GitHub Actions run ID

**Unavailable / not verified.** The immutable-sha API query for repository
`werim/AlphaForge`, branch `dev`, and the audited head SHA failed with `Tunnel connection
failed: 403 Forbidden`. No run ID is guessed from local state. This is a release-evidence
blocker, not a passing result.

## Immutable evidence

Evidence root:

`docs/audits/evidence/pr307-dev-4d1cdd7a64df14b8034dc8b356c12a01f11aaf88/`

The evidence directory is keyed by the full audited SHA. `SHA256SUMS` binds each captured
log and exit-code sidecar. The manifest itself is committed with this report; changing a
captured artifact will invalidate its recorded digest.

## Impact and recommendation

- Runtime/lifecycle/persistence/export/schema impact: none; documentation and captured audit evidence only.
- Compatibility and migration impact: none.
- Remaining risks: Actions run ID cannot be verified, clean Python 3.11 dependency setup was blocked, exact config invocation fails without package installation, and three external integrations remain deliberately skipped.
- Push recommendation: acceptable as an audit record only. Do not treat it as full GitHub Actions parity, external integration acceptance, or LIVE-readiness evidence.
