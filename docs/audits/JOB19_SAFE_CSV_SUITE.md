# JOB19 exporter-safe SQL suite

The PAPER audit export failure was caused by a SQL entrypoint that used multiple statements and temporary objects, while `run_sql_audits.py` accepts one result query per file.

## Run commands

Run the repaired default report:

```bash
python run_sql_audits.py --db auto --sql-dir sql --out-dir reports/sql_csv
```

Run the split JOB19 audit reports:

```bash
python run_sql_audits.py --db auto --sql-dir sql/diagnostics/job19 --out-dir reports/job19_csv
```

Each file in `sql/diagnostics/job19/` contains a single read-only query and produces one CSV row-set plus an entry in `all_reports_summary.csv`.

## Evidence boundary

This patch repairs report execution and provides diagnostic extracts. It does not prove PAPER decision quality until the queries are executed against the completed PAPER runtime database and the generated CSV reports are evaluated.

## Safety posture

No runtime trading behavior, persistence write path, schema, thresholds, score or RR calculation, lifecycle emission, exchange action, or LIVE readiness logic is changed.
