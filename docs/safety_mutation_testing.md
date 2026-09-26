# Targeted safety mutation gate

Issue #421 P2-B protects selected production safety guards with deliberate,
single-source mutations. Run locally with:

```bash
python scripts/run_safety_mutations.py
```

The runner copies `src/` and `tests/` into a disposable directory under the
repository, verifies the selected tests pass on unchanged code, then applies one
mutation at a time to the copy. It never edits the checkout or opens a campaign
database. The directory is removed when the run ends.

Each mutation has an exact, unique source anchor and a named regression test.
Only an assertion failure in a test that passed at baseline counts as `KILLED`.
A surviving mutation, changed/ambiguous source anchor, syntax error, import or
collection error, timeout, or other harness failure fails the gate. The JSON
result in CI logs includes the source commit SHA, baseline test count, each
mutation and its classification. Normal full-suite and offline backtest CI
checks still run separately.

The current protected set covers effective RR, unknown execution evidence,
each execution hard gate, expected-fill geometry, entry-slippage accounting,
campaign/run/release and readiness mode scope, diagnostic shadow authority,
accepted-position candle-window completeness, reconciliation CLEAN, final
kill-switch reread, runtime portfolio-risk inputs, and future market timestamps.
The coverage is intentionally targeted; it is not a global mutation score or
a claim of LIVE readiness. P2-C seeded property contracts live in
`tests/test_issue421_safety_properties.py`. P2-D replays all 23 frozen golden
scenarios through the production evidence chain in
`tests/test_issue421_full_chain_replay.py`. P2-E bounded-load checks live in
`tests/test_issue421_bounded_load.py`. Fresh exact-final-SHA FAST and public
six-hour SOAK qualification remain separate release evidence.
