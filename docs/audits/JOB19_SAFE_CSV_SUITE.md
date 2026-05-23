# JOB19 split SQL report suite

The JOB19 report queries are now split under `sql/diagnostics/job19/`.

Each numbered file contains one read-only result query for CSV export. The compatibility entry files return the suite path only.

Canonical decision totals and rejection rate are produced by `02_canonical_decision_totals.sql`. `03_final_decisions_by_symbol.sql` is a supplemental symbol row-count report.

This change is diagnostic only. It does not change runtime behavior, persistence writes, scoring, RR, thresholds, schema, lifecycle emission, or LIVE controls.

A PAPER health verdict requires execution of the numbered reports against the completed runtime database and review of their generated CSV results.
