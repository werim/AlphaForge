# AlphaForge Database Doctor v1

The doctor identifies the exact SQLite file, inspects it read-only, and never
converts unknown state into a healthy result. Use it before PAPER startup:

```bash
python -m alphaforge.db_doctor --db /absolute/path/alphaforge.db diagnose
python -m alphaforge.db_doctor --db /absolute/path/alphaforge.db plan --json
python -m alphaforge.db_doctor --db /absolute/path/alphaforge.db repair
python -m alphaforge.db_doctor --db /absolute/path/alphaforge.db certify --json
```

`repair` first creates and integrity-checks a SQLite online backup (including
committed WAL content without checkpointing the source), then runs the canonical Alembic migration. Ambiguous
identity duplicates and integrity failures stop without deleting rows.
`certify` additionally invokes real AlphaForge lifecycle, decision, heartbeat,
and runtime-state writers against an isolated copy of the exact schema; schema
inspection alone cannot produce `DATABASE_CERTIFIED`.

Repair reports `REPAIRED` only after both structural diagnosis and those real
writer probes pass. Unknown lifecycle columns, indexes, triggers, checks,
uniques, or foreign keys block before table replacement unless the migration
explicitly understands and preserves their semantics. Retain the reported
backup whenever repair or post-migration verification fails.

Repair does not enable LIVE execution and does not change trading behavior.
