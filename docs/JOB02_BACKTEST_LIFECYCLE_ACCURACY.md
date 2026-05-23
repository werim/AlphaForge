# JOB-02 — Backtest Lifecycle Accuracy Fix

Status: safety-first implementation note and diagnostics. No threshold loosening. No trade-frequency increase.

## Objective

Make the BACKTEST export behave like a real order-decision lifecycle simulator rather than a compressed trade-result simulator.

A correct backtest lifecycle must preserve both accepted and rejected branches:

```text
SYMBOL_REJECTED

SIGNAL_CREATED
  -> SIGNAL_REJECTED

SIGNAL_CREATED
  -> SIGNAL_ACCEPTED
  -> WAITING_ENTRY_ZONE
  -> ENTRY_TRIGGERED
  -> ORDER_PLACED
  -> POSITION_OPENED
  -> POSITION_CLOSED
```

## Current code-backed status

The current `dev` branch already contains substantial lifecycle realism:

- `backtest_order.py::scan_symbol_backtest(...)` routes candidate quality through `run_order_cycle(...)`.
- `backtest_order.py::process_backtest_result(...)` emits `SIGNAL_CREATED`, `SIGNAL_REJECTED`, and `ORDER_REJECTED` rows.
- `backtest_order.py::simulate_candidate(...)` emits the accepted branch through `SIGNAL_CREATED -> SIGNAL_ACCEPTED -> WAITING_ENTRY_ZONE -> ENTRY_TRIGGERED -> ORDER_PLACED -> POSITION_OPENED -> POSITION_CLOSED`.
- `_persist_lifecycle_rows(...)` persists lifecycle rows through `save_trade_lifecycle_event(...)` and exports SQL-backed rows into `order_lifecycle.csv`.

## Remaining accuracy gaps found

### 1. Reject rows do not share one finalization helper

Accepted rows from `simulate_candidate(...)` receive deterministic:

- `signal_id`,
- `lifecycle_id`,
- `order_id`,
- `position_id`,
- `lifecycle_seq`.

However, rejected rows created by `process_backtest_result(...)`, symbol-selector rejects, and offline synthetic rejects are not normalized through the same lifecycle finalization path before CSV/SQL export.

Impact: CSV-visible reject rows may be less complete than accepted rows, even if `_persist_lifecycle_rows(...)` fills some fallback IDs for SQL persistence.

### 2. Signal reject rows may store effective RR as raw RR fallback

For direct `SIGNAL_REJECTED` rows, `effective_rr` can be absent and later fall back to `rr` in persistence.

Impact: backtest rejected rows can appear execution-adjusted when they are not. This weakens JOB-04 effective-RR canonicalization.

### 3. Symbol-selector reject rows are lifecycle-real but not signal-real

`SYMBOL_REJECTED` is useful and should remain, but it should be treated as pre-signal lifecycle. It should not be counted as a full scored signal unless a `SIGNAL_CREATED` row exists.

Impact: reject-rate denominators must distinguish symbol prefilter rejects from true signal/order rejects.

### 4. Offline fixture has synthetic acceptance/rejection

Offline mode creates deterministic acceptance and rejection rows for CI smoke coverage. This is useful, but reports must mark those rows as fixture-generated and should not be used as real selectivity evidence.

## Minimal safe patch to apply

### Patch A — Add shared lifecycle finalizer

Add a helper in `backtest_order.py`:

```python
def finalize_lifecycle_identity(rows: list[LifecycleRow]) -> list[LifecycleRow]:
    counters: dict[str, int] = {}
    for row in rows:
        signal_id = row.signal_id or f"{row.symbol}:{row.timestamp}"
        row.signal_id = signal_id
        row.lifecycle_id = row.lifecycle_id or str(uuid5(NAMESPACE_URL, f"backtest:lifecycle:{signal_id}"))
        counters[signal_id] = counters.get(signal_id, 0) + 1
        row.lifecycle_seq = row.lifecycle_seq or counters[signal_id]
        if row.status_after in {"ORDER_PLACED", "POSITION_OPENED", "POSITION_CLOSED"} and not row.order_id:
            row.order_id = str(uuid5(NAMESPACE_URL, f"backtest:order:{signal_id}:{row.entry}:{row.sl}:{row.tp}"))
        if row.status_after in {"POSITION_OPENED", "POSITION_CLOSED"} and not row.position_id:
            row.position_id = str(uuid5(NAMESPACE_URL, f"backtest:position:{signal_id}:{row.side}"))
        if row.effective_rr is None:
            row.effective_rr = row.rr
    return rows
```

Call it once before:

- `build_forward_evaluations_from_lifecycle(...)`,
- `_persist_lifecycle_rows(...)`,
- `_derive_backtest_counts(...)`,
- CSV export.

### Patch B — Compute effective_rr on rejected signal rows where context exists

When `process_backtest_result(...)` handles `result.status == rejected`, compute:

```python
effective_rr, _flags, _breakdown = _execution_reject_flags(rr, mctx)
```

and set that on the `SIGNAL_REJECTED` lifecycle row and rejected export row unless the diagnostics explicitly already contain an effective_rr.

### Patch C — Strengthen export integrity

`verify_export_integrity(...)` should additionally fail when:

- any lifecycle CSV row has empty `signal_id`,
- any lifecycle CSV row has empty `lifecycle_id`,
- any lifecycle row has `lifecycle_seq <= 0`,
- any rejected lifecycle row has missing `reject_reason`,
- any row has missing `effective_rr`.

### Patch D — Add tests

Required tests:

1. `process_backtest_result(...)` rejected rows receive `signal_id`, `lifecycle_id`, `lifecycle_seq`, and `effective_rr` after finalization.
2. Symbol-selector `SYMBOL_REJECTED` rows remain pre-signal and do not create fake `SIGNAL_CREATED` rows.
3. Accepted branch order remains:
   `SIGNAL_CREATED -> SIGNAL_ACCEPTED -> WAITING_ENTRY_ZONE -> ENTRY_TRIGGERED -> ORDER_PLACED -> POSITION_OPENED -> POSITION_CLOSED`.
4. `order_lifecycle.csv` contains rejected rows with non-empty `reject_reason` and `signal_id`.
5. Backtest summary counts `ORDER_PLACED` rows from unique placed lifecycle keys, not from candidate count shortcuts.
6. `effective_rr` differs from `rr` when execution penalties are non-zero.

## Diagnostic interpretation

A healthy JOB-02 backtest artifact should show:

- rejected rows present in `order_lifecycle.csv`,
- accepted rows present only after lifecycle progression,
- no direct `CREATED -> TP_HIT` shortcut,
- no blank `signal_id`,
- no blank `reject_reason` on rejected lifecycle rows,
- non-constant score/RR distributions when sample size is adequate,
- execution context either populated or explicitly marked unavailable.

## Production stance

This job does not make AlphaForge live-ready. It improves the reliability of historical lifecycle evidence so JOB-03 through JOB-06 can audit reject persistence, effective RR, execution context, and PAPER runtime DB health without reading smoke-scented tea leaves.
