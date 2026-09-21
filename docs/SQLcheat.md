# AlphaForge SQL/operator cheat sheet

## 1. Purpose

This is the read-only SQL reference for AlphaForge PAPER, BURN-IN, qualification, reject-forward-outcome, runtime-health, and execution-quality audits. It records the current repository schema and the joins that are safe to use when inspecting a campaign.

The source-of-truth audit covered:

- Runtime/core persistence: `src/alphaforge/persistence.py`, `runtime_state.py`, `runtime_heartbeat.py`, `reconciliation.py`, `release_gates.py`, `rollback_evidence.py`, `alert_delivery.py`.
- Burn-in/campaign persistence: `burnin.py`, `burnin_campaign.py`, `burnin_ops.py`, `burnin_resolver.py`.
- Shadow/readiness persistence: `adaptive_decision_calibration.py`, `agents/persistence.py`, `live_readiness.py`, remote-control stores.
- Schema history: `alembic/versions/` and the SQLite `schema_migrations` bookkeeping path.
- Current SQLite metadata: a read-only inspection of `data/campaign/M0_2109T03.db`. That database was observed running; no write, migration, lock, or bootstrap operation was performed.

The inspected campaign schema contained 65 non-internal tables and 84 indexes. The repository defines 82 real table names when separate shadow/transport/readiness stores and the conditional legacy `runtime_*` adapters are included. The inventory below includes both sets and labels tables that are not expected in every campaign DB.

## 2. Safety / read-only rules

Use a copy or a read-only connection for audits. Do not run `init_db`, `bootstrap_*_schema`, migrations, repair commands, or application startup against the active PAPER database.

Safe inspection form:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name;"
```

## 3. Quick start

### 3.1 Variables

Set these only after discovery. `CID` is the campaign ID; `RID` is a burn-in run ID, not a release ID.

```bash
DB="/absolute/path/to/identified/campaign.db"
CID="camp_..."
RID="camp_..._run_0000"
```

Find candidate databases without opening them for writing:

```bash
find data -type f \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \) -print
```

List campaigns in one candidate:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT campaign_id,release_id,campaign_status,active_run_id,created_at,started_at,last_heartbeat_at,worker_pid,qualification_status,evidence_completeness_status,last_error FROM burnin_campaigns ORDER BY created_at DESC;"
```

List database files and their campaign status safely from zsh:

```bash
find data -type f -name '*.db' -print | while IFS= read -r db; do printf '%s\n' "--- $db"; sqlite3 -readonly -header -column "$db" "SELECT campaign_id,campaign_status,active_run_id,last_heartbeat_at,last_error FROM burnin_campaigns ORDER BY created_at DESC LIMIT 5;" 2>/dev/null; done
```

Confirm the selected campaign/run relationship:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT c.campaign_id,c.release_id,c.campaign_status,c.active_run_id,cr.burnin_run_id,cr.continuation_sequence,cr.status AS run_status,c.worker_pid,c.last_heartbeat_at FROM burnin_campaigns c LEFT JOIN burnin_campaign_runs cr ON cr.campaign_id=c.campaign_id AND cr.burnin_run_id=c.active_run_id WHERE c.campaign_id='$CID';"
```

## 4. Compact table of contents

1. [Schema map](#5-schema-map)
2. [Table inventory](#6-table-inventory)
3. [Canonical relationships and joins](#7-canonical-relationships-and-joins)
4. [Campaign queries](#8-campaign-queries)
5. [Reject analysis](#9-reject-analysis)
6. [Accepted trades, positions, and PnL](#10-accepted-trades-positions-and-pnl)
7. [Score, RR, expectancy, and adaptive state](#11-score-rr-expectancy-and-adaptive-state)
8. [Execution quality](#12-execution-quality)
9. [MTF](#13-mtf)
10. [Lifecycle](#14-lifecycle)
11. [Evidence quality](#15-evidence-quality)
12. [Resolver](#16-resolver)
13. [Qualification](#17-qualification)
14. [Runtime health](#18-runtime-health)
15. [Integrity and orphan checks](#19-integrity-and-orphan-checks)
16. [Concentration](#20-symbol-regime-and-side-concentration)
17. [Configuration verification](#21-configuration-and-threshold-verification)
18. [zsh-safe patterns](#22-zsh-safe-query-patterns)
19. [Troubleshooting](#23-common-troubleshooting)
20. [Query index](#24-query-index)

## 5. Schema map

### 5.1 Ownership and authority

| Surface | Canonical owner | Operator meaning |
|---|---|---|
| Signals and decisions | `signals`, `order_decisions` | One signal and its final decision/reject context. `order_decisions.decision_id` is unique; `signals.signal_id` is indexed/unique in current schemas. |
| Lifecycle | `trade_lifecycle_events` | Append-only event history for signal, order, position, close, cancel, error, and reconciliation transitions. This is the canonical timeline. |
| PAPER execution | `orders`, `fills`, `positions`, `paper_events` | Runtime/PAPER exposure and execution records. These tables do not carry `campaign_id`; scope them through signal/run identity. |
| Burn-in campaign | `burnin_campaigns`, `burnin_campaign_runs`, `burnin_runs` | Campaign identity, continuation lineage, release/config hashes, and run-level counters. |
| Burn-in decisions | `burnin_observations` | Campaign/run-scoped decision evidence. `metrics_json` carries `signal_id`, `reject_decision_id`, MTF, and provenance. Use the canonical-decision predicate when counting KPI rows because diagnostic observations can coexist. |
| Reject forward outcome | `burnin_pending_reject_labels` → `burnin_reject_outcomes` | Pending labels are campaign/run scoped. Resolved outcomes are run scoped and carry campaign/pending/reject identity in `payload_json`. |
| Accepted trade outcome | `burnin_pending_position_outcomes` → `burnin_trade_outcomes` | Pending positions are campaign/run scoped; realized trade outcomes are run scoped and include cost, net-R, PnL, hold, and exit evidence. |
| Qualification | `burnin_qualification_snapshots` | Historical, immutable-looking qualification snapshots keyed by `qualification_id`; current campaign linkage is direct in current schemas. |
| Runtime health | `runtime_heartbeats`, `runtime_state_snapshots`, `runtime_recovery_events`, `exchange_reconciliation_events` | Operational liveness, recovery, fail-closed, kill-switch, and exchange reconciliation evidence. |
| Readiness/release | `release_gate_snapshots`, `operator_acknowledgements`, `canary_run_events`, `rollback_verification_events`, `runbook_evidence`, `live_readiness_reports` | Release/readiness evidence; not a substitute for campaign decision/lifecycle evidence. |
| Shadow/adaptive | `adaptive_*`, `state_direction_shadow_*`, `agent_*` | Non-authoritative calibration or agent traces. Adaptive shadow output is deliberately separate from runtime thresholds. |
| Backtest | `backtest_runs`, `backtest_events`, `symbol_snapshots`, `calibration_labels` | Backtest/run-scoped data. Do not combine with PAPER rows without an explicit `mode`/`run_id` filter. |

### 5.2 Migration and schema-family notes

- Additive runtime bootstrap/migration code is in `persistence.py`; the repository migration history is under `alembic/versions/`.
- Observed campaign databases use `schema_migrations` and did not expose an `alembic_version` table. Do not assume Alembic revision metadata is present in a campaign DB.
- The observed current campaign had additive columns beyond the original DDL, including worker identity on `burnin_campaigns`, campaign linkage on qualification/runtime snapshots, normalized lifecycle identifiers, and timestamp-bounded expectancy evidence columns.
- `trade_lifecycle_events__doctor_new` is a temporary Alembic repair-table name, not a canonical operator table. It should not remain after a successful migration.
- `runtime_positions` and `runtime_orders` are conditional legacy compatibility adapters created by the schema doctor only for trusted legacy shapes. Prefer canonical `positions` and `orders`.
- Separate stores may contain only shadow/transport tables: the adaptive shadow DB, agent shadow DB, remote-control replay DB, and Telegram transport-state DB. Their tables are not expected in the campaign DB.

## 6. Table inventory

The following is the current column inventory. `id` is the ordinary row primary key unless another key is stated. Columns ending in `_json`, `payload`, `execution_ctx`, `evidence_json`, or `diagnostics_json` are serialized JSON/text and should be inspected with `json_valid`/`json_extract` before assuming a shape.

### 6.1 Core decision, execution, and evidence tables

| Table | Class / purpose | Primary/logical keys and important joins | Columns (current observed order) | Code |
|---|---|---|---|---|
| `signals` | Canonical signal registry; runtime/PAPER/backtest | `signal_id`; joins to decisions/lifecycle/orders/positions/fills by `signal_id` | `id, signal_id, symbol, side, timeframe, mode, score, rr, effective_rr, expectancy_bucket, created_at, updated_at` | `persistence.py` |
| `order_decisions` | Canonical decision/reject artifact | `decision_id`; `signal_id`; reject labels use `reject_decision_id → decision_id` | `id, decision_id, signal_id, order_id, symbol, timeframe, mode, decision, reject_reason, score, rr, effective_rr, expectancy_bucket, payload, execution_ctx, execution_ctx_missing, input_snapshot_hash, no_submit_verified, parity_result, created_at, updated_at, phase, order_type, confidence, explanation, order_payload, expected_slippage_pct, spread_pct, latency_ms, orderbook_imbalance, funding_rate_pct, execution_regime, volatility_regime, portfolio_reject_reason, portfolio_risk_state, portfolio_diagnostics_json, risk_flags` | `persistence.py` |
| `ai_decision_features` | Feature/penalty sidecar | `decision_id`; unique index in current DB | `id, decision_id, features, penalties, reason_flags, execution_features, created_at` | `persistence.py` |
| `trade_lifecycle_events` | Canonical lifecycle timeline | `event_id`; current unique index on `(signal_id,event_ts,lifecycle_state)`; joins by `signal_id`, `order_id`, `trade_id`, `lifecycle_id` | `id, event_id, signal_id, order_id, symbol, mode, trade_id, lifecycle_state, state, event_type, payload, decision, reject_reason, score, rr, effective_rr, expectancy_bucket, execution_ctx, execution_ctx_missing, event_ts, created_at, lifecycle_seq, cancel_reason, lifecycle_id, failure_reason, reconciliation_reason, incident_payload` | `persistence.py`, `lifecycle_contract.py` |
| `decision_evidence` | SQL-backed readiness/export evidence surface | `evidence_id`; optional `run_id`, `signal_id`, `order_id`, `position_id`, `lifecycle_id` | `id, evidence_id, run_id, profile_id, profile_name, mode, timestamp, symbol, side, setup_type, setup_reason, regime, lifecycle_state_before, lifecycle_state_after, decision, score, raw_rr, effective_rr, expectancy, expectancy_bucket, reject_reason, cancel_reason, close_reason, entry, sl, tp, trigger_price, close_price, net_pnl_pct, net_pnl_usdt, hold_minutes, volume_24h_usdt, spread_pct, funding_rate_pct, expected_slippage_pct, liquidity_score, volatility_regime, cost_penalty, total_cost_pct, total_explicit_cost_pct, spread_source, slippage_source, fee_pct, fee_source, funding_source, latency_ms, latency_source, liquidity_status, volatility_penalty_pct, volatility_source, reject_flags, unavailable_fields, diagnostics_json, portfolio_equity, available_balance, open_position_count, max_open_positions, total_notional_exposure, max_notional_exposure, symbol_notional_exposure, max_symbol_notional, side_exposure_long, side_exposure_short, net_exposure, gross_exposure, daily_realized_pnl, daily_loss_pct, max_daily_loss_pct, rolling_drawdown_pct, consecutive_loss_count, correlation_group, correlation_group_exposure, correlated_position_count, risk_flags, portfolio_reject_reason, portfolio_risk_state, portfolio_diagnostics_json, signal_id, order_id, position_id, lifecycle_id, lifecycle_seq, created_at` | `persistence.py`, `live_readiness.py` |
| `orders` | Runtime/PAPER order state | `order_id`; joins by `signal_id`, `position_id` | `id, order_id, signal_id, position_id, symbol, timeframe, mode, side, status, created_at, updated_at` | `persistence.py` |
| `fills` | Fill ledger | `fill_id`; joins by `order_id`, `position_id`, `signal_id` | `id, fill_id, order_id, position_id, signal_id, symbol, side, qty, price, fee, filled_at, created_at` | `persistence.py` |
| `positions` | Runtime/PAPER position state | `position_id`; joins by `signal_id` and order/fill position IDs | `id, position_id, signal_id, symbol, timeframe, mode, side, qty, entry_price, status, created_at, updated_at` | `persistence.py` |
| `paper_events` | PAPER event stream/convenience record | `event_id`; joins by `signal_id`, `order_id`, `position_id` | `id, event_id, signal_id, order_id, position_id, event_type, symbol, timeframe, mode, payload_json, created_at` | `persistence.py` |
| `rejected_signal_reviews` | Rejected-signal artifact/review | partial unique `reject_decision_id`; joins to `order_decisions` and pending labels | `id, reject_decision_id, signal_id, symbol, setup_type, regime, side, reject_reason, score, raw_rr, effective_rr, expectancy_bucket, volume_24h_usdt, spread_pct, expected_slippage_pct, funding_rate_pct, liquidity_score, volatility_regime, forward_window_bars, would_have_hit_tp, would_have_hit_sl, max_favorable_excursion_pct, max_adverse_excursion_pct, reject_correct, execution_invalidated, outcome_ambiguous, evidence_complete, created_at, payload_json` | `persistence.py`, `runtime.py` |
| `closed_trade_reviews` | Closed-trade review convenience surface | `trade_id`; joins to lifecycle/position and burn-in outcome by trade identity | `id, trade_id, symbol, setup_type, regime, side, entry_price, exit_price, raw_rr, effective_rr, score, net_pnl_pct, fee_pct, spread_pct, expected_slippage_pct, actual_slippage_pct, liquidity_score, volatility_regime, close_reason, tp_hit, sl_hit, hold_minutes, created_at, payload_json, review_payload, execution_metrics` | `persistence.py` |
| `expectancy_evidence` | Timestamp-bounded expectancy evidence | `evidence_id`; `source_decision_id`, `run_id`, `campaign_id`, `release_id` | `evidence_id, source_decision_id, evidence_type, decision_time, resolved_at, symbol, side, setup_type, regime, reject_reason, net_r, run_id, campaign_id, release_id, evidence_complete, created_at` | `expectancy_evidence.py` |
| `calibration_snapshots` | Per-signal forward calibration | unique `(signal_id,forward_window_minutes)` in canonical DDL | `id, signal_id, predicted_quality, realized_outcome, score, rr, effective_rr, regime, setup_type, rejection_reason, forward_window_minutes, mfe_pct, mae_pct, would_have_hit_tp, would_have_hit_sl, reject_correct, created_at` | `persistence.py` |
| `calibration_labels` | Backtest/calibration labels | `signal_id`, `run_id` | `id, signal_id, run_id, symbol, timeframe, mode, label, payload_json, created_at` | `persistence.py` |

### 6.2 Campaign, burn-in, resolver, and qualification tables

| Table | Class / purpose | Primary/logical keys and joins | Columns |
|---|---|---|---|
| `burnin_campaigns` | Canonical campaign identity and current status | unique `(campaign_id,release_id)`; `active_run_id → burnin_campaign_runs.burnin_run_id` logically | `id, campaign_id, release_id, campaign_status, created_at, started_at, completed_at, expected_duration_seconds, observed_duration_seconds, target_decisions, target_closed_trades, target_reject_forward_outcomes, active_run_id, config_hash, strategy_config_hash, universe_hash, git_commit, execution_cost_config_hash, source_provenance_json, symbols_json, intervals_json, restart_count, last_heartbeat_at, last_error, qualification_status, latest_qualification_id, evidence_completeness_status, schema_version, worker_pid, worker_started_at, last_operator_activity_at` |
| `burnin_campaign_runs` | Campaign-to-run lineage and continuation sequence | unique `(campaign_id,burnin_run_id)` and `(campaign_id,continuation_sequence)` | `id, campaign_id, burnin_run_id, continuation_sequence, status, started_at, ended_at, created_at, schema_version` |
| `burnin_campaign_events` | Campaign operations/recovery events | `event_id`; campaign/run logical keys | `id, event_id, campaign_id, burnin_run_id, event_type, event_time, details_json, schema_version` |
| `burnin_campaign_exports` | Export manifest/checksum record | `export_id`; `campaign_id` | `id, export_id, campaign_id, output_dir, manifest_path, generated_at, evidence_hash, checksums_json, status, schema_version` |
| `burnin_runs` | Run-level burn-in counters and provenance | unique `burnin_run_id`; unique `(release_id,execution_mode,continuation_sequence)` | `id, burnin_run_id, release_id, phase, execution_mode, parent_burnin_run_id, parent_qualification_id, continuation_sequence, start_time, end_time, status, git_commit, config_hash, strategy_config_hash, universe_hash, source_provenance_json, symbols_json, intervals_json, expected_duration_seconds, observed_duration_seconds, sample_count, accepted_count, rejected_count, closed_trade_count, open_trade_count, data_completeness_status, evidence_completeness_status, generated_at, schema_version` |
| `burnin_observations` | Run-scoped decision/evidence observations | unique `observation_id`; `burnin_run_id`; campaign through `burnin_campaign_runs` | `id, observation_id, burnin_run_id, release_id, observed_at, execution_mode, symbol, interval, regime, decision, lifecycle_state, evidence_complete, missing_fields_json, metrics_json, source_provenance_json, schema_version` |
| `burnin_pending_reject_labels` | Reject resolver queue | unique `pending_label_id` and `reject_decision_id`; direct campaign/run joins; `reject_decision_id → order_decisions.decision_id` | `id, pending_label_id, campaign_id, burnin_run_id, reject_decision_id, signal_id, symbol, side, decision_timestamp, timeframe, horizon_bars, entry, stop, target, horizon_seconds, execution_cost_assumptions_json, regime, reject_reason, source_provenance_json, due_at, status, evidence_complete, last_error, claim_token, claimed_at, created_at, resolved_at, schema_version` |
| `burnin_reject_outcomes` | Canonical resolved reject forward outcome | unique `reject_outcome_id`; `burnin_run_id`; explicit identities in `payload_json` | `id, reject_outcome_id, burnin_run_id, release_id, reject_reason, symbol, regime, decision_time, hypothetical_entry, hypothetical_stop, hypothetical_target, forward_label, would_tp, would_sl, timeout, ambiguous, hypothetical_gross_r, hypothetical_net_r_after_costs, avoided_loss, missed_profit, execution_invalidated, evidence_horizon, evidence_complete, payload_json, schema_version` |
| `burnin_pending_position_outcomes` | Accepted PAPER position/outcome queue | unique `pending_position_id` and `trade_id`; direct campaign/run joins; `source_decision_id` | `id, pending_position_id, trade_id, campaign_id, burnin_run_id, signal_id, source_decision_id, decision_time, symbol, side, setup_type, entry_time, planned_entry, simulated_fill, stop, target, quantity, notional, entry_spread, entry_slippage, entry_fee, regime, source_provenance_json, status, exit_time, exit_price, exit_reason, gross_pnl, gross_r, exit_spread, exit_slippage, exit_fee, funding, latency_impact_penalty, total_execution_cost, net_pnl, net_r, hold_duration_seconds, mfe, mae, evidence_complete, missing_fields_json, created_at, resolved_at, schema_version` |
| `burnin_trade_outcomes` | Canonical realized burn-in trade outcome | unique `outcome_id`; `burnin_run_id` | `id, outcome_id, burnin_run_id, release_id, trade_id, symbol, regime, closed_at, gross_r, gross_pnl, spread_cost, entry_slippage_cost, exit_slippage_cost, fee_cost, funding_cost, latency_cost, volatility_penalty, liquidity_penalty, total_execution_cost, net_r, net_pnl, effective_rr_at_entry, realized_effective_rr, hold_duration_seconds, mfe, mae, exit_reason, evidence_complete, missing_cost_fields_json, payload_json, schema_version` |
| `burnin_regime_metrics` | Run/regime derived qualification metrics | unique `(burnin_run_id,regime)` | `id, burnin_run_id, release_id, regime, sample_count, accepted_count, rejected_count, mean_net_r, lower_confidence_bound_expectancy, max_drawdown, cost_drag, slippage_distribution_json, reject_accuracy, execution_failure_count, status, generated_at, schema_version` |
| `burnin_execution_metrics` | Run/window execution-quality aggregate | run/window logical key | `id, burnin_run_id, release_id, metric_window, spread_baseline, spread_current, slippage_baseline, slippage_current, latency_baseline, latency_current, fill_probability_baseline, fill_probability_current, liquidity_depth_baseline, liquidity_depth_current, timeout_rate, execution_rejects, stale_data_count, reconciliation_quality, funding_cost, price_impact_proxy, status, generated_at, schema_version` |
| `burnin_calibration_metrics` | Run/scope calibration aggregate | run/scope logical key | `id, burnin_run_id, release_id, scope, sample_count, brier_score, log_loss, calibration_error, expected_calibration_error, reliability_buckets_json, observed_vs_predicted_json, status, generated_at, schema_version` |
| `burnin_drawdown_events` | Drawdown/rolling-risk evidence | unique `drawdown_event_id`; run/release | `id, drawdown_event_id, burnin_run_id, release_id, peak_equity, trough_equity, drawdown_start, drawdown_end, drawdown_pct, drawdown_duration_seconds, recovery_duration_seconds, consecutive_losses, rolling_loss_cluster_json, rolling_expectancy, rolling_cost_drag, rolling_slippage, rolling_reject_accuracy, resolved, payload_json, schema_version` |
| `burnin_qualification_snapshots` | Qualification verdict and blockers | unique `qualification_id`; current schema includes `campaign_id` and `source_run_ids_json` | `id, qualification_id, burnin_run_id, release_id, generated_at, status, sample_status, expectancy_status, execution_status, regime_status, reject_quality_status, calibration_status, drawdown_status, concentration_status, reconciliation_status, evidence_completeness_status, blockers_json, warnings_json, thresholds_json, metrics_json, evidence_hash, schema_version, campaign_id, source_run_ids_json, aggregate_evidence_hash` |
| `burnin_suspension_events` | Fail-closed/suspension history | unique `suspension_event_id`; release/run | `id, suspension_event_id, release_id, burnin_run_id, timestamp, reason_codes_json, observed_values_json, thresholds_json, evidence_payload_json, schema_version` |
| `burnin_ops_incidents` | Campaign operations incidents | unique `incident_id`; campaign | `id, incident_id, campaign_id, incident_type, severity, status, detected_at, details_json, schema_version` |
| `burnin_preflight_reports` | Preflight result | unique `preflight_id`; campaign/release | `id, preflight_id, campaign_id, release_id, generated_at, status, blockers_json, checks_json, output_dir, schema_version` |
| `burnin_health_history` | Periodic campaign health history | unique `health_id`; campaign | `id, health_id, campaign_id, generated_at, status, unhealthy_reasons_json, payload_json, schema_version` |
| `burnin_recovery_drills` | Recovery drill evidence | unique `drill_id`; campaign | `id, drill_id, campaign_id, generated_at, status, checks_json, before_json, after_json, schema_version` |
| `burnin_integrity_audits` | Persisted integrity audit | unique `audit_id`; campaign | `id, audit_id, campaign_id, generated_at, status, violations_json, checks_json, aggregate_evidence_hash, schema_version` |
| `burnin_release_decisions` | Release decision package | unique `decision_id`; campaign | `id, decision_id, campaign_id, generated_at, decision, blockers_json, package_dir, checksums_json, schema_version` |
| `burnin_source_evidence_hashes` | Immutable source-row baseline/hash | unique `(campaign_id,burnin_run_id)` | `id, campaign_id, burnin_run_id, captured_at, evidence_hash, row_ids_json, run_status, baseline_reason, schema_version` |
| `burnin_ops_write_probe` | Schema-doctor/write-probe sentinel | none; diagnostic only | `x` | `burnin_ops.py`; do not use as evidence |

### 6.3 Runtime, health, release, and readiness tables

| Table | Class / purpose | Keys / important joins | Columns |
|---|---|---|---|
| `runtime_heartbeats` | Measured runtime heartbeat | `runtime_instance_id`, `heartbeat_ts`; no direct campaign key | `id, runtime_instance_id, execution_mode, heartbeat_ts, scanner_source, runtime_state, last_scan_ts, last_decision_ts, active_positions_count, pending_orders_count, evidence_status, payload_json` |
| `runtime_state_snapshots` | Append-only runtime state/exposure snapshot | `instance_id`, `startup_id`; current schema has `campaign_id,burnin_run_id,release_id` | `id, timestamp, instance_id, startup_id, process_id, mode, requested_mode, actual_mode, runtime_status, heartbeat_age_sec, last_start_time, last_shutdown_time, last_error, kill_switch_active, kill_switch_reason, active_symbols, active_position_count, active_positions, pending_order_count, pending_orders, cooldown_symbols, stale_market_data_symbols, unreconciled_symbols, orphan_order_count, orphan_orders, orphan_position_count, unknown_exchange_state, exchange_connectivity_status, exchange_read_only_status, reconciliation_status, reconciliation_mismatch_count, recovery_action_required, fail_closed_reason, runtime_flags, diagnostics_json, created_at, campaign_id, burnin_run_id, release_id` |
| `runtime_recovery_events` | Recovery/restart evidence | `instance_id,startup_id,event_ts` | `id, event_ts, instance_id, startup_id, mode, status, reason, diagnostics_json` |
| `exchange_reconciliation_events` | Read-only exchange/local reconciliation | unique `cycle_id` in current schema | `id, event_ts, instance_id, startup_id, mode, status, mismatch_count, orphan_order_count, orphan_position_count, exchange_read_only_status, diagnostics_json, cycle_id` |
| `reconciliation_incidents` | Persisted reconciliation findings | logical `lifecycle_ref`; indexed by time/symbol/severity | `id, incident_type, severity, symbol, lifecycle_ref, remediation_status, operator_acknowledged, fail_closed, forensic_payload, created_at` |
| `runtime_control_state` | Current requested/running mode and kill switch | singleton `id=1` | `id, mode_requested, mode_running, kill_switch_active, kill_switch_source, kill_switch_updated_at, runtime_status, last_error, created_at, updated_at` |
| `runtime_control_audit_events` | Mode/control audit trail | `event_ts`; no declared unique event key | `id, event_ts, action, requested_mode, previous_mode, success, reason, source, kill_switch_active, readiness_status, operator_acknowledged` |
| `cooldown_states` | Per-symbol cooldown state | primary key `symbol` | `symbol, cooldown_remaining_sec` |
| `release_gate_snapshots` | Release gate verdict | release/phase; latest by `id`/`generated_at` | `id, release_id, phase, status, generated_at, canary_ready, rollback_verified, runbook_verified, operator_acknowledged, mutation_attempt_count, blocking_reasons, evidence_json` |
| `operator_acknowledgements` | Release operator ACK | unique `ack_id`; release/phase | `id, ack_id, release_id, phase, acknowledged_at, valid_until, operator_id, acknowledgement_text, evidence_json` |
| `canary_run_events` | Shadow/canary mutation evidence | unique `event_id`; release/phase | `id, event_id, release_id, phase, event_type, event_ts, shadow_mode, canary_mode, mutation_attempted, mutation_blocked, evidence_json` |
| `rollback_verification_events` | Rollback verification | unique `verification_id`; release/phase | `id, verification_id, release_id, phase, verified_at, status, evidence_json` |
| `runbook_evidence` | Runbook evidence | unique `evidence_id`; release/phase | `id, evidence_id, release_id, phase, recorded_at, status, evidence_json` |
| `live_rollback_validation_evidence` | Non-mutating rollback validation | unique `validation_id` | `id, validation_id, recorded_at, evidence_status, rollback_evidence_source, kill_switch_block_verified, no_submit_on_kill_switch_verified, fail_closed_reconciliation_verified, repair_actions_non_mutating_verified, execution_mutation_attempt_count, blocking_reasons, evidence_payload` |
| `live_alert_delivery_evidence` | Sanitized alert-probe evidence | time-ordered `id` | `id, recorded_at, evidence_status, alert_delivery_verified, evidence_payload` |
| `live_readiness_reports` | Readiness report history | time-ordered `id` | `id, generated_at, qualified, deployment_state, acknowledgement_required, report_payload, readiness_inputs_json` |

### 6.4 Backtest, adaptive, statistics, and model evidence tables

| Table | Class / purpose | Keys / joins | Columns |
|---|---|---|---|
| `backtest_runs` | Backtest run identity | unique `run_id` | `id, run_id, mode, started_at, completed_at, payload_json, created_at, updated_at` |
| `backtest_events` | Backtest event stream | unique `event_id`; run/signal/order/position logical keys | `id, event_id, run_id, signal_id, order_id, position_id, event_type, symbol, timeframe, mode, payload_json, created_at` |
| `symbol_snapshots` | Run/symbol market snapshot | run/symbol/time | `id, run_id, symbol, timeframe, mode, snapshot_ts, payload_json, created_at` |
| `optimizer_runs` | Optimizer run state | unique `run_id` | `id, run_id, status, payload_json, created_at, updated_at` |
| `adaptive_stats` | Derived adaptive statistics | unique `(scope_type,scope_key)` | `id, scope_type, scope_key, sample_size, win_rate, avg_net_pnl_pct, avg_effective_rr, avg_spread_pct, avg_slippage_pct, reject_accuracy, expectancy, confidence, updated_at, payload_json` |
| `adaptive_threshold_snapshots` | Shadow/adaptive threshold snapshots | scope/time; not a runtime threshold mutation | `id, scope_type, scope_key, min_score, min_effective_rr, max_spread_pct, max_expected_slippage_pct, min_liquidity_score, reason, source, created_at, payload_json` |
| `setup_expectancy_stats` | Derived setup aggregate | primary key `setup` | `setup, samples, win_count, total_pnl, expectancy, updated_at` |
| `regime_expectancy_stats` | Derived regime aggregate | primary key `regime` | `regime, samples, win_count, total_pnl, expectancy, updated_at` |
| `symbol_expectancy_stats` | Derived symbol aggregate | primary key `symbol` | `symbol, samples, win_count, total_pnl, expectancy, updated_at` |
| `timesfm_forecast_evidence` | Forecast/model evidence | unique `forecast_id`; optional forward-label join | `id, forecast_id, timestamp, forecast_timestamp, symbol, timeframe, horizon, point_forecast, quantiles_json, current_price, forecast_p10, forecast_p50, forecast_p90, side, expected_rr, rejection_reason, mode, model_provider, model_name, model_version, no_lookahead_input_end_ts, payload_json, created_at, updated_at` |
| `timesfm_forward_outcome_labels` | Forecast forward labels | unique `forecast_id` | `id, forecast_id, outcome, mfe, mae, expected_r, realized_r, labeled_at, payload_json` |
| `signal_id_state` | Signal ID generator state | unique `scope` | `id, scope, last_signal_id, signal_id, symbol, timeframe, mode, created_at, updated_at` |

### 6.5 Separate shadow, agent, transport, and conditional tables

These are source-defined but are not required in every campaign DB. Locate their DB path from configuration/code before querying them.

| Table | Store / class | Columns |
|---|---|---|
| `adaptive_decision_observations` | Separate adaptive shadow output; campaign/run/decision evidence | `id, event_id, campaign_id, burnin_run_id, decision_id, actual_decision, reason_or_gate, symbol, side, regime, setup_type, outcome_label, net_r, attributable, evidence_complete, execution_invalidated, observed_at, context_json, schema_version` |
| `adaptive_decision_calibration` | Separate adaptive calibration aggregate | `calibration_key, campaign_id, decision_dimension, reason_or_gate, scope_json, sample_count, observation_count, tp_count, sl_count, ambiguous_count, timeout_count, execution_invalidated_count, evidence_attributable_count, avg_net_r, total_net_r, ewma_net_r, recent_expectancy, long_expectancy, lower_confidence_bound, upper_confidence_bound, base_value, shadow_value, calibration_state, guardrails_json, evidence_hash, last_adjusted_at, updated_at, schema_version` |
| `adaptive_decision_calibration_events` | Adaptive proposed-state audit | `event_id, calibration_key, evidence_hash, previous_state, proposed_state, previous_value, proposed_value, sample_count, recent_expectancy, long_expectancy, lower_confidence_bound, upper_confidence_bound, reason, blocked_by_guardrail, evidence_window_json, created_at, schema_version` |
| `adaptive_shadow_decisions` | Non-authoritative adaptive comparison | `decision_key, campaign_id, burnin_run_id, decision_id, actual_decision, adaptive_shadow_decision, calibration_key, reason_or_gate, observed_net_r, retrospective_in_sample, context_json, updated_at, schema_version` |
| `state_direction_shadow_decisions` | Non-authoritative state-direction experiment | `shadow_decision_id, observed_at, symbol, decision_timestamp, signal_id, base_exec_direction, regime_direction, setup_direction, resolved_state, shadow_final_direction, shadow_reason, actual_decision, actual_side, actual_reject_reason, entry, base_sl, base_tp, shadow_geometry_type, shadow_sl, shadow_tp, geometry_valid, spread, expected_slippage, latency, fee_assumption, funding_assumption, raw_rr_if_executed, effective_rr_if_executed, execution_costs_json, regime, setup_phase, timeframe, horizon_bars, due_at, source_provenance_json, evidence_complete, updated_at, schema_version` |
| `state_direction_shadow_outcomes` | Shadow forward outcomes | primary key `shadow_outcome_id`; unique/FK-like `shadow_decision_id` | `shadow_outcome_id, shadow_decision_id, outcome_status, forward_label, mfe, mae, gross_r, cost_adjusted_net_r, total_cost_drag, avoided_loss, missed_profit, ambiguous, evidence_complete, resolved_at, evidence_json, schema_version` |
| `agent_runs` | Isolated Phase-A agent trace | unique `correlation_id` | `id, correlation_id, decision_id, execution_mode, symbol, shadow_only, graph_status, started_at, completed_at, duration_ms, legacy_decision_reference, config_hash, orchestrator_version, created_at` |
| `agent_stage_events` | Isolated agent stage trace | unique `(decision_id,stage,retry_count)` | `id, correlation_id, decision_id, stage, status, primary_reason, reason_codes_json, evidence_json, input_hash, config_hash, agent_version, started_at, completed_at, duration_ms, retry_count, skipped_reason, created_at` |
| `agent_phase_b_evidence` | Isolated Phase-B parity/evidence | primary key `correlation_id` | `correlation_id, decision_id, symbol, market_status, regime, volatility, trend_strength, spread, expected_slippage, liquidity, funding, availability_json, signal_status, signal_side, setup_type, score, score_components_json, raw_rr, entry, sl, tp, no_signal_reason, quality_status, quality_score, expectancy_bucket, primary_reject_reason, reject_reasons_json, legacy_decision, legacy_primary_reject_reason, score_difference, rr_difference, reason_code_overlap, parity_status, created_at` |
| `replay_message_ids` | Remote-control replay guard | primary key `message_id` | `message_id, first_seen_at` |
| `remote_control_audit` | Remote-control command audit | row `id`; time/transport | `id, transport, update_id, authorized_identity, normalized_command, result, rejection_reason, created_at` |
| `telegram_transport_state` | Telegram offset state | primary key `state_key` | `state_key, state_value, updated_at` |
| `telegram_pending_deliveries` | Telegram pending delivery queue | primary key `update_id` | `update_id, chat_id, response_text, status, failure_reason, retry_count, created_at, updated_at` |
| `runtime_positions` | Conditional legacy exposure adapter | compatibility-only; prefer `positions` | `id, symbol, qty, status` |
| `runtime_orders` | Conditional legacy order adapter | compatibility-only; prefer `orders` | `id, order_id, symbol, status, created_at` |

### 6.6 Schema bookkeeping

| Table | Class / purpose | Keys / columns |
|---|---|---|
| `schema_migrations` | SQLite additive migration bookkeeping; observed campaign DB surface | primary key `version`; `version, applied_at, notes, name, checksum, success, details_json` |

## 7. Canonical relationships and joins

### 7.1 Campaign identity

```text
burnin_campaigns.campaign_id
  └─ burnin_campaign_runs.campaign_id
       └─ burnin_campaign_runs.burnin_run_id = burnin_runs.burnin_run_id
            ├─ burnin_observations.burnin_run_id
            ├─ burnin_pending_reject_labels.burnin_run_id
            ├─ burnin_reject_outcomes.burnin_run_id
            ├─ burnin_pending_position_outcomes.burnin_run_id
            ├─ burnin_trade_outcomes.burnin_run_id
            ├─ burnin_*_metrics.burnin_run_id
            └─ burnin_qualification_snapshots.burnin_run_id
```

`release_id` is persisted on campaign, run, and most derived evidence tables. It is a provenance key, not a replacement for `campaign_id` or `burnin_run_id`. Continuation runs are linked by `parent_burnin_run_id` and ordered by `continuation_sequence`.

`order_decisions`, `signals`, `orders`, `positions`, `fills`, `paper_events`, and lifecycle rows do not have a direct `campaign_id` in the current canonical schema. Scope them through one of these verified routes:

1. `burnin_observations` → `metrics_json` → `signal_id` → core tables.
2. `burnin_pending_reject_labels.reject_decision_id` → `order_decisions.decision_id` for rejected decisions.
3. `burnin_pending_position_outcomes.signal_id/source_decision_id` → core tables for accepted PAPER positions.
4. `runtime_state_snapshots.campaign_id/burnin_run_id` for runtime-health lineage.

Do not invent `campaign_id` columns or join all decisions by timestamp alone.

### 7.2 Reject flow

```text
signals.signal_id
  → order_decisions.signal_id / decision_id
  → rejected_signal_reviews.reject_decision_id
  → burnin_pending_reject_labels.reject_decision_id
  → burnin_reject_outcomes.payload_json:
       campaign_id, burnin_run_id, reject_decision_id, pending_label_id
  → expectancy_evidence.source_decision_id/evidence_type='REJECT_FORWARD'
```

The exact current canonical resolver identity was verified in the campaign DB and source:

```text
burnin_pending_reject_labels.reject_decision_id
  = order_decisions.decision_id
```

The pending label is the authoritative queue row. `rejected_signal_reviews` is the immediate reject artifact/review. `burnin_reject_outcomes` is the mature forward result; it may legitimately be empty while labels are still pending, not due, incomplete, or unavailable because the evidence window has not completed.

### 7.3 Trade flow

```text
signals.signal_id
  → order_decisions.signal_id
  → orders.signal_id/order_id
  → fills.order_id/position_id/signal_id
  → positions.position_id/signal_id
  → trade_lifecycle_events.signal_id/order_id/trade_id
  → burnin_pending_position_outcomes.source_decision_id/signal_id/trade_id
  → burnin_trade_outcomes.trade_id
  → closed_trade_reviews.trade_id (review convenience surface)
```

`burnin_pending_position_outcomes` and `burnin_trade_outcomes` are the best campaign-scoped PAPER outcome surfaces. A row in `orders` or `positions` alone is not a closed trade and is not a realized outcome.

### 7.4 Lifecycle and evidence

Expected lifecycle ordering is:

```text
SIGNAL_CREATED
→ SIGNAL_VALIDATED
→ SIGNAL_REJECTED | WAITING_ENTRY_ZONE
→ ENTRY_TRIGGERED
→ ORDER_PLACED
→ PARTIAL_FILL
→ FILLED
→ TP_HIT | SL_HIT | CANCELLED | OPEN_AT_END
```

The canonical timeline is `trade_lifecycle_events`. `order_decisions` explains the decision; `decision_evidence` is a SQL-backed readiness/export surface; `expectancy_evidence` is the compact timestamp-bounded evidence surface; `rejected_signal_reviews` and `closed_trade_reviews` are review projections; burn-in outcomes are the campaign qualification surfaces.

Current source inspection found the `decision_evidence` table DDL and readiness reads, but no production `INSERT INTO decision_evidence` writer under `src/alphaforge`. Therefore an empty table is not automatically harmless: it can be expected for a database that has not run the export path, but the live-readiness checks treat missing rows as a readiness failure. Always report table existence and row count separately.

### 7.5 JSON/MTF evidence location

Runtime MTF details are primarily in `burnin_observations.metrics_json` and copied provenance JSON, not dedicated SQL columns. Useful paths include:

```text
$.mtf.alignment.timeframes.regime   -- usually 1h
$.mtf.alignment.timeframes.setup    -- usually 15m
$.mtf.alignment.timeframes.execution -- usually 1m
$.mtf.alignment.resolved_state
$.mtf.alignment.execution_alignment
$.mtf.alignment.regime_alignment
$.mtf.alignment.setup_alignment
$.mtf.alignment.final_direction
$.mtf.execution.confirmed_for_side
$.mtf.execution.execution_regime
$.mtf.regime.regime
$.mtf.setup.setup_type
```

## 8. Campaign queries

### 8.1 Database discovery and schema metadata

Show every table, all columns, indexes, or one table's DDL:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name;"
sqlite3 -readonly -header -column "$DB" "PRAGMA table_info(order_decisions);"
sqlite3 -readonly -header -column "$DB" "SELECT name,tbl_name FROM sqlite_schema WHERE type='index' ORDER BY tbl_name,name;"
sqlite3 -readonly -header -column "$DB" "SELECT sql FROM sqlite_schema WHERE type='table' AND name='order_decisions';"
```

Search the schema for a keyword in table/column names. This is metadata-only and avoids scanning row data:

```bash
sqlite3 -readonly -header -column "$DB" "WITH cols AS (SELECT m.name AS table_name,p.name AS column_name FROM sqlite_schema m JOIN pragma_table_info(m.name) p ON 1=1 WHERE m.type='table') SELECT table_name,column_name FROM cols WHERE lower(table_name||' '||column_name) LIKE '%reject%' OR lower(table_name||' '||column_name) LIKE '%decision%' OR lower(table_name||' '||column_name) LIKE '%reason%' OR lower(table_name||' '||column_name) LIKE '%score%' OR lower(table_name||' '||column_name) LIKE '%rr%' OR lower(table_name||' '||column_name) LIKE '%campaign%' OR lower(table_name||' '||column_name) LIKE '%run%' OR lower(table_name||' '||column_name) LIKE '%lifecycle%' OR lower(table_name||' '||column_name) LIKE '%evidence%' OR lower(table_name||' '||column_name) LIKE '%symbol%' OR lower(table_name||' '||column_name) LIKE '%position%';"
```

Count rows in important surfaces. This is intentionally global; add `WHERE` clauses from later sections for campaign counts.

```bash
sqlite3 -readonly -header -column "$DB" "SELECT 'signals' AS table_name,COUNT(*) AS n FROM signals UNION ALL SELECT 'order_decisions',COUNT(*) FROM order_decisions UNION ALL SELECT 'trade_lifecycle_events',COUNT(*) FROM trade_lifecycle_events UNION ALL SELECT 'decision_evidence',COUNT(*) FROM decision_evidence UNION ALL SELECT 'rejected_signal_reviews',COUNT(*) FROM rejected_signal_reviews UNION ALL SELECT 'burnin_observations',COUNT(*) FROM burnin_observations UNION ALL SELECT 'burnin_pending_reject_labels',COUNT(*) FROM burnin_pending_reject_labels UNION ALL SELECT 'burnin_reject_outcomes',COUNT(*) FROM burnin_reject_outcomes UNION ALL SELECT 'burnin_pending_position_outcomes',COUNT(*) FROM burnin_pending_position_outcomes UNION ALL SELECT 'burnin_trade_outcomes',COUNT(*) FROM burnin_trade_outcomes UNION ALL SELECT 'runtime_heartbeats',COUNT(*) FROM runtime_heartbeats;"
```

### 8.2 List campaigns, runs, release, and qualification

Newest campaigns first, including active continuation and worker metadata:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT c.campaign_id,c.release_id,c.campaign_status,c.created_at,c.started_at,c.completed_at,c.active_run_id,c.restart_count,c.worker_pid,c.worker_started_at,c.last_heartbeat_at,c.qualification_status,c.latest_qualification_id,c.evidence_completeness_status,c.last_error FROM burnin_campaigns c ORDER BY c.created_at DESC;"
```

Compact status table with current run counts. `decisions` uses canonical burn-in observations; the other counts are direct campaign/run-linked surfaces:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT c.campaign_id,c.release_id,c.campaign_status,c.active_run_id,c.last_heartbeat_at,c.worker_pid,c.qualification_status,c.evidence_completeness_status,(SELECT COUNT(*) FROM burnin_observations o JOIN burnin_campaign_runs x ON x.burnin_run_id=o.burnin_run_id WHERE x.campaign_id=c.campaign_id) AS observations,(SELECT COUNT(*) FROM burnin_pending_reject_labels p WHERE p.campaign_id=c.campaign_id) AS pending_rejects,(SELECT COUNT(*) FROM burnin_pending_position_outcomes p WHERE p.campaign_id=c.campaign_id) AS pending_positions,(SELECT COUNT(*) FROM burnin_qualification_snapshots q WHERE q.campaign_id=c.campaign_id) AS qualification_snapshots,c.last_error FROM burnin_campaigns c ORDER BY c.created_at DESC;"
```

List all continuation runs for one campaign:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT cr.campaign_id,cr.burnin_run_id,cr.continuation_sequence,cr.status,cr.started_at,cr.ended_at,r.release_id,r.phase,r.execution_mode,r.sample_count,r.accepted_count,r.rejected_count,r.closed_trade_count,r.open_trade_count FROM burnin_campaign_runs cr JOIN burnin_runs r ON r.burnin_run_id=cr.burnin_run_id WHERE cr.campaign_id='$CID' ORDER BY cr.continuation_sequence;"
```

Latest qualification snapshot for one campaign, with JSON gate fields left visible for exact inspection:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT q.qualification_id,q.burnin_run_id,q.release_id,q.generated_at,q.status,q.sample_status,q.expectancy_status,q.execution_status,q.regime_status,q.reject_quality_status,q.calibration_status,q.drawdown_status,q.concentration_status,q.reconciliation_status,q.evidence_completeness_status,q.blockers_json,q.warnings_json,q.thresholds_json,q.metrics_json,q.evidence_hash FROM burnin_qualification_snapshots q WHERE q.campaign_id='$CID' OR q.burnin_run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') ORDER BY q.generated_at DESC,q.id DESC LIMIT 1;"
```

Runtime lineage and persisted PID for one campaign:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT campaign_id,burnin_run_id,release_id,instance_id,startup_id,process_id,mode,actual_mode,runtime_status,timestamp,last_error,kill_switch_active,reconciliation_status,recovery_action_required FROM runtime_state_snapshots WHERE campaign_id='$CID' ORDER BY id DESC LIMIT 10;"
```

The PID can be checked without touching the process; do not use `kill`, `pkill`, restart, attach, or resume commands during an audit:

```bash
PID="$(sqlite3 -readonly "$DB" "SELECT worker_pid FROM burnin_campaigns WHERE campaign_id='$CID';")"; [ -n "$PID" ] && ps -p "$PID" -o pid=,etime=,command=
```

### 8.3 Current campaign totals

The following use `burnin_observations` as the campaign-scoped decision source. If diagnostic observations are present, replace the simple observation predicate with the canonical predicate in [9.1](#91-canonical-campaign-scoping) before reporting qualification KPIs.

```bash
sqlite3 -readonly -header -column "$DB" "WITH cr AS (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') SELECT 'total decisions' AS metric,COUNT(*) AS n FROM burnin_observations o JOIN cr ON cr.burnin_run_id=o.burnin_run_id UNION ALL SELECT 'accepted decisions',COUNT(*) FROM burnin_observations o JOIN cr ON cr.burnin_run_id=o.burnin_run_id WHERE UPPER(COALESCE(o.decision,'')) IN ('ACCEPT','ACCEPTED') UNION ALL SELECT 'rejected decisions',COUNT(*) FROM burnin_observations o JOIN cr ON cr.burnin_run_id=o.burnin_run_id WHERE UPPER(COALESCE(o.decision,''))='REJECTED' UNION ALL SELECT 'open positions',COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id='$CID' AND UPPER(status)='OPEN' UNION ALL SELECT 'closed positions',COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id='$CID' AND UPPER(status) IN ('CLOSED','RESOLVED') UNION ALL SELECT 'pending reject labels',COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id='$CID' AND UPPER(status) IN ('PENDING','READY','RESOLVING') UNION ALL SELECT 'resolved reject labels',COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id='$CID' AND UPPER(status) IN ('RESOLVED','AMBIGUOUS') UNION ALL SELECT 'failed reject labels',COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id='$CID' AND UPPER(status)='FAILED' UNION ALL SELECT 'expired reject labels',COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id='$CID' AND UPPER(status)='EXPIRED';"
```

Last decision, scan, and heartbeat timestamps:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT (SELECT MAX(o.observed_at) FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID') AS last_decision_ts,(SELECT MAX(h.last_scan_ts) FROM runtime_heartbeats h) AS last_scan_ts,(SELECT MAX(h.last_decision_ts) FROM runtime_heartbeats h) AS last_runtime_decision_ts,(SELECT MAX(h.heartbeat_ts) FROM runtime_heartbeats h) AS last_heartbeat_ts,(SELECT last_error FROM burnin_campaigns WHERE campaign_id='$CID') AS last_campaign_error;"
```

## 9. Reject analysis

### 9.1 Canonical campaign scoping

The exact campaign/run relationship is:

```sql
WITH campaign_run_ids AS (
  SELECT burnin_run_id
  FROM burnin_campaign_runs
  WHERE campaign_id = '$CID'
),
canonical_campaign_observations AS (
  SELECT o.*
  FROM burnin_observations AS o
  JOIN campaign_run_ids AS r ON r.burnin_run_id = o.burnin_run_id
  WHERE json_valid(COALESCE(o.metrics_json,''))
    AND UPPER(COALESCE(json_extract(o.metrics_json,'$.observation_kind'),'CANONICAL_DECISION')) = 'CANONICAL_DECISION'
    AND NOT EXISTS (
      SELECT 1
      FROM burnin_observations AS newer
      WHERE newer.burnin_run_id = o.burnin_run_id
        AND newer.id < o.id
        AND COALESCE(json_extract(newer.metrics_json,'$.reject_decision_id'),json_extract(newer.metrics_json,'$.signal_id'),newer.observation_id)
          = COALESCE(json_extract(o.metrics_json,'$.reject_decision_id'),json_extract(o.metrics_json,'$.signal_id'),o.observation_id)
        AND UPPER(COALESCE(json_extract(newer.metrics_json,'$.observation_kind'),'CANONICAL_DECISION')) = 'CANONICAL_DECISION'
    )
)
SELECT decision,COUNT(*) AS n
FROM canonical_campaign_observations
GROUP BY decision
ORDER BY n DESC;
```

The simpler `burnin_observations JOIN burnin_campaign_runs` form is sufficient for a first look. The canonical form above mirrors `canonical_decision_sql` in `burnin.py` and prevents a diagnostic row from inflating qualification counts.

For core decision fields, campaign-scoped signals are the bridge. There is no direct campaign column on `order_decisions`:

```sql
WITH campaign_signals AS (
  SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') AS signal_id
  FROM burnin_observations AS o
  JOIN burnin_campaign_runs AS cr ON cr.burnin_run_id = o.burnin_run_id
  WHERE cr.campaign_id = '$CID'
    AND json_extract(o.metrics_json,'$.signal_id') IS NOT NULL
)
SELECT d.*
FROM order_decisions AS d
JOIN campaign_signals AS s ON s.signal_id = d.signal_id
ORDER BY d.created_at,d.id;
```

### 9.2 Reject reason distribution and concentration

Reject reason distribution as `reject_reason | n | pct`:

```bash
sqlite3 -readonly -header -column "$DB" "WITH r AS (SELECT COALESCE(d.reject_reason,json_extract(o.metrics_json,'$.primary_reject_reason'),json_extract(o.metrics_json,'$.reject_reason'),'UNKNOWN') AS reject_reason FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id LEFT JOIN order_decisions d ON d.decision_id=json_extract(o.metrics_json,'$.reject_decision_id') WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,''))='REJECTED'),g AS (SELECT reject_reason,COUNT(*) AS n FROM r GROUP BY reject_reason) SELECT reject_reason,n,ROUND(100.0*n/SUM(n) OVER (),2) AS pct FROM g ORDER BY n DESC,reject_reason;"
```

Rejects by symbol, side, regime, and the combined dimensions:

```bash
sqlite3 -readonly -header -column "$DB" "WITH r AS (SELECT COALESCE(json_extract(o.metrics_json,'$.symbol'),o.symbol) AS symbol,COALESCE(s.side,json_extract(o.metrics_json,'$.side')) AS side,COALESCE(o.regime,json_extract(o.metrics_json,'$.volatility_regime'),'UNKNOWN') AS regime,COALESCE(d.reject_reason,json_extract(o.metrics_json,'$.primary_reject_reason'),'UNKNOWN') AS reject_reason FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id LEFT JOIN order_decisions d ON d.decision_id=json_extract(o.metrics_json,'$.reject_decision_id') LEFT JOIN signals s ON s.signal_id=json_extract(o.metrics_json,'$.signal_id') WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,''))='REJECTED') SELECT symbol,side,regime,reject_reason,COUNT(*) AS n FROM r GROUP BY symbol,side,regime,reject_reason ORDER BY n DESC,symbol,side,regime,reject_reason;"
```

### 9.3 Reject score distribution and LOW_SCORE statistics

Score distribution for campaign rejects, using the decision row where the exact reject decision ID is available:

```bash
sqlite3 -readonly -header -column "$DB" "WITH r AS (SELECT d.score FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id LEFT JOIN order_decisions d ON d.decision_id=json_extract(o.metrics_json,'$.reject_decision_id') WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,''))='REJECTED') SELECT COUNT(*) AS n,MIN(score) AS min_score,AVG(score) AS avg_score,MAX(score) AS max_score,COUNT(score) AS non_null_score FROM r;"
```

LOW_SCORE count, range, and pass-at-threshold checks. `pass_at_*` means `score >= threshold`; it does not assert that the threshold was actually configured.

```bash
sqlite3 -readonly -header -column "$DB" "WITH r AS (SELECT d.score FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id LEFT JOIN order_decisions d ON d.decision_id=json_extract(o.metrics_json,'$.reject_decision_id') WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,''))='REJECTED' AND COALESCE(d.reject_reason,json_extract(o.metrics_json,'$.primary_reject_reason'))='LOW_SCORE') SELECT COUNT(*) AS count,MIN(score) AS min_score,AVG(score) AS avg_score,MAX(score) AS max_score,SUM(score>=0.10) AS pass_at_010,SUM(score>=0.48) AS pass_at_048,SUM(score>=0.50) AS pass_at_050,SUM(score>=0.55) AS pass_at_055,SUM(score>=0.62) AS pass_at_062 FROM r;"
```

Score buckets for all campaign decisions:

```bash
sqlite3 -readonly -header -column "$DB" "WITH s AS (SELECT d.score FROM order_decisions d JOIN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') AS signal_id FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID') x ON x.signal_id=d.signal_id WHERE d.score IS NOT NULL) SELECT CASE WHEN score<0.10 THEN '<0.10' WHEN score<0.20 THEN '0.10-0.19' WHEN score<0.30 THEN '0.20-0.29' WHEN score<0.40 THEN '0.30-0.39' WHEN score<0.50 THEN '0.40-0.49' WHEN score<0.60 THEN '0.50-0.59' WHEN score<0.70 THEN '0.60-0.69' WHEN score<0.80 THEN '0.70-0.79' WHEN score<0.90 THEN '0.80-0.89' ELSE '0.90+' END AS bucket,COUNT(*) AS n FROM s GROUP BY bucket ORDER BY MIN(score);"
```

### 9.4 MTF reject analysis

MTF reject reasons specifically requested, with timeframe, side, regime, and timestamp from the persisted JSON:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COALESCE(d.reject_reason,json_extract(o.metrics_json,'$.primary_reject_reason')) AS reject_reason,COALESCE(json_extract(o.metrics_json,'$.symbol'),o.symbol) AS symbol,COALESCE(s.side,json_extract(o.metrics_json,'$.side')) AS side,COALESCE(json_extract(o.metrics_json,'$.regime'),o.regime,json_extract(o.metrics_json,'$.volatility_regime')) AS regime,json_extract(o.metrics_json,'$.mtf.alignment.timeframes.regime') AS regime_tf,json_extract(o.metrics_json,'$.mtf.alignment.timeframes.setup') AS setup_tf,json_extract(o.metrics_json,'$.mtf.alignment.timeframes.execution') AS execution_tf,o.observed_at FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id LEFT JOIN order_decisions d ON d.decision_id=json_extract(o.metrics_json,'$.reject_decision_id') LEFT JOIN signals s ON s.signal_id=json_extract(o.metrics_json,'$.signal_id') WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,''))='REJECTED' AND COALESCE(d.reject_reason,json_extract(o.metrics_json,'$.primary_reject_reason')) IN ('MTF_EXECUTION_NOT_CONFIRMED','MTF_EXECUTION_COUNTER_REGIME') ORDER BY o.observed_at;"
```

Break down MTF rejects by symbol, side, regime, and reason:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COALESCE(json_extract(o.metrics_json,'$.symbol'),o.symbol) AS symbol,COALESCE(s.side,json_extract(o.metrics_json,'$.side')) AS side,COALESCE(json_extract(o.metrics_json,'$.regime'),o.regime,'UNKNOWN') AS regime,COALESCE(d.reject_reason,json_extract(o.metrics_json,'$.primary_reject_reason')) AS reject_reason,COUNT(*) AS n FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id LEFT JOIN order_decisions d ON d.decision_id=json_extract(o.metrics_json,'$.reject_decision_id') LEFT JOIN signals s ON s.signal_id=json_extract(o.metrics_json,'$.signal_id') WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,''))='REJECTED' AND COALESCE(d.reject_reason,json_extract(o.metrics_json,'$.primary_reject_reason')) LIKE 'MTF_%' GROUP BY 1,2,3,4 ORDER BY n DESC;"
```

### 9.5 Reject persistence reconciliation

Compare canonical rejected observations, decision rows, review artifacts, pending labels, and resolved outcomes. The final two columns identify missing identity links; they are audit signals, not write instructions.

```bash
sqlite3 -readonly -header -column "$DB" "WITH rejects AS (SELECT DISTINCT json_extract(o.metrics_json,'$.reject_decision_id') AS reject_decision_id,json_extract(o.metrics_json,'$.signal_id') AS signal_id,o.burnin_run_id FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,''))='REJECTED' AND json_extract(o.metrics_json,'$.reject_decision_id') IS NOT NULL) SELECT COUNT(*) AS rejected_observations,COUNT(d.decision_id) AS decisions_persisted,COUNT(v.reject_decision_id) AS reviews_persisted,COUNT(p.pending_label_id) AS pending_labels,COUNT(ro.reject_outcome_id) AS outcomes_persisted,SUM(d.decision_id IS NULL) AS missing_decisions,SUM(p.pending_label_id IS NULL) AS missing_pending_labels FROM rejects r LEFT JOIN order_decisions d ON d.decision_id=r.reject_decision_id LEFT JOIN rejected_signal_reviews v ON v.reject_decision_id=r.reject_decision_id LEFT JOIN burnin_pending_reject_labels p ON p.reject_decision_id=r.reject_decision_id AND p.campaign_id='$CID' LEFT JOIN burnin_reject_outcomes ro ON ro.burnin_run_id=r.burnin_run_id AND json_extract(ro.payload_json,'$.reject_decision_id')=r.reject_decision_id AND json_extract(ro.payload_json,'$.campaign_id')='$CID';"
```

List the missing/orphan links row by row:

```bash
sqlite3 -readonly -header -column "$DB" "WITH rejects AS (SELECT DISTINCT json_extract(o.metrics_json,'$.reject_decision_id') AS reject_decision_id,json_extract(o.metrics_json,'$.signal_id') AS signal_id,o.burnin_run_id FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,''))='REJECTED') SELECT r.reject_decision_id,r.signal_id,CASE WHEN d.decision_id IS NULL THEN 'MISSING_DECISION' WHEN p.pending_label_id IS NULL THEN 'MISSING_PENDING_LABEL' WHEN ro.reject_outcome_id IS NULL THEN 'NOT_RESOLVED_OR_MISSING_OUTCOME' ELSE 'COMPLETE' END AS reconciliation_state FROM rejects r LEFT JOIN order_decisions d ON d.decision_id=r.reject_decision_id LEFT JOIN burnin_pending_reject_labels p ON p.reject_decision_id=r.reject_decision_id AND p.campaign_id='$CID' LEFT JOIN burnin_reject_outcomes ro ON ro.burnin_run_id=r.burnin_run_id AND json_extract(ro.payload_json,'$.reject_decision_id')=r.reject_decision_id AND json_extract(ro.payload_json,'$.campaign_id')='$CID' ORDER BY reconciliation_state,r.reject_decision_id;"
```

### 9.6 Mature reject quality

Mature outcomes show forward label, TP/SL/ambiguous state, reject correctness, avoided loss, missed profit, net-R after costs, and evidence completeness:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT ro.reject_outcome_id,ro.reject_reason,ro.symbol,ro.regime,ro.decision_time,ro.forward_label,ro.would_tp,ro.would_sl,ro.timeout,ro.ambiguous,ro.hypothetical_net_r_after_costs,ro.avoided_loss,ro.missed_profit,CASE WHEN ro.ambiguous=1 THEN 'AMBIGUOUS' WHEN ro.evidence_complete=0 THEN 'INCOMPLETE' WHEN ro.hypothetical_net_r_after_costs<=0 THEN 'REJECT_CORRECT' ELSE 'MISSED_PROFIT' END AS reject_quality,ro.execution_invalidated,ro.evidence_complete,json_extract(ro.payload_json,'$.missing_cost_fields') AS missing_cost_fields FROM burnin_reject_outcomes ro JOIN burnin_campaign_runs cr ON cr.burnin_run_id=ro.burnin_run_id WHERE cr.campaign_id='$CID' ORDER BY ro.decision_time;"
```

Reject-quality summary for mature rows:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COUNT(*) AS mature_outcomes,SUM(would_tp=1) AS would_tp,SUM(would_sl=1) AS would_sl,SUM(ambiguous=1) AS ambiguous,SUM(evidence_complete=1 AND ambiguous=0 AND hypothetical_net_r_after_costs<=0) AS reject_correct,SUM(evidence_complete=1 AND ambiguous=0 AND hypothetical_net_r_after_costs>0) AS missed_profit,SUM(evidence_complete=1) AS evidence_complete,AVG(hypothetical_net_r_after_costs) AS avg_hypothetical_net_r,SUM(hypothetical_net_r_after_costs) AS total_hypothetical_net_r FROM burnin_reject_outcomes ro JOIN burnin_campaign_runs cr ON cr.burnin_run_id=ro.burnin_run_id WHERE cr.campaign_id='$CID';"
```

`sqlite3 -readonly` prevents database writes. The queries in this document are `SELECT`/metadata queries only. Do not remove `-readonly`, and do not paste operator SQL into a connection that is also used by a runtime writer.

Important boundaries:

- A running campaign can change between two read-only queries. Use one query/transaction for a reported snapshot and label volatile results with the observation time.
- Never infer the active campaign from a filename. Read `burnin_campaigns` and confirm `campaign_status`, `active_run_id`, heartbeat, release, and worker identity.
- Main campaign tables use logical keys rather than declared foreign keys. A successful `JOIN` is evidence of an identity match, not proof that SQLite enforces referential integrity.
- Do not use global `orders`, `positions`, `fills`, `paper_events`, or `trade_lifecycle_events` counts as campaign counts without scoping through signal/run identity.

## 10. Accepted trades, positions, and PnL

### 10.1 Accepted decision analysis

Accepted decisions from the campaign observation surface, joined to signals for side/score/RR and to order decisions for execution context:

```bash
sqlite3 -readonly -header -column "$DB" "WITH accepted AS (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') AS signal_id,o.observed_at,o.regime FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,'')) IN ('ACCEPT','ACCEPTED')) SELECT a.observed_at,a.signal_id,s.symbol,s.side,s.timeframe,s.score,s.rr,s.effective_rr,s.expectancy_bucket,d.mode,d.decision,d.expected_slippage_pct,d.spread_pct,d.latency_ms,d.funding_rate_pct,d.execution_regime,d.volatility_regime,d.portfolio_risk_state FROM accepted a LEFT JOIN signals s ON s.signal_id=a.signal_id LEFT JOIN order_decisions d ON d.signal_id=a.signal_id ORDER BY a.observed_at;"
```

Accepted decisions by symbol, side, and regime:

```bash
sqlite3 -readonly -header -column "$DB" "WITH accepted AS (SELECT json_extract(o.metrics_json,'$.signal_id') AS signal_id,COALESCE(o.regime,'UNKNOWN') AS regime FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,'')) IN ('ACCEPT','ACCEPTED')) SELECT s.symbol,s.side,a.regime,COUNT(DISTINCT a.signal_id) AS n FROM accepted a LEFT JOIN signals s ON s.signal_id=a.signal_id GROUP BY s.symbol,s.side,a.regime ORDER BY n DESC;"
```

### 10.2 Open and closed positions

Campaign-scoped open/closed PAPER position queue:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT pending_position_id,trade_id,signal_id,source_decision_id,symbol,side,status,entry_time,planned_entry,simulated_fill,stop,target,quantity,notional,regime,evidence_complete,created_at,resolved_at FROM burnin_pending_position_outcomes WHERE campaign_id='$CID' ORDER BY entry_time,id;"
```

Runtime exposure (global unless `runtime_state_snapshots.campaign_id` is available):

```bash
sqlite3 -readonly -header -column "$DB" "SELECT position_id,signal_id,symbol,timeframe,mode,side,qty,entry_price,status,created_at,updated_at FROM positions WHERE UPPER(COALESCE(status,'')) IN ('OPEN','POSITION_OPENED','ACTIVE') ORDER BY created_at;"
```

Accepted campaign signals with no corresponding runtime position or burn-in pending position are review candidates. This is not automatically an error because a valid accepted decision can still be waiting for entry:

```bash
sqlite3 -readonly -header -column "$DB" "WITH accepted AS (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') AS signal_id FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,'')) IN ('ACCEPT','ACCEPTED')) SELECT a.signal_id,CASE WHEN p.signal_id IS NOT NULL THEN 'PENDING_POSITION' WHEN pos.signal_id IS NOT NULL THEN 'RUNTIME_POSITION' WHEN l.signal_id IS NOT NULL THEN 'LIFECYCLE_ONLY' ELSE 'NO_DOWNSTREAM_ROW' END AS downstream_state FROM accepted a LEFT JOIN burnin_pending_position_outcomes p ON p.signal_id=a.signal_id AND p.campaign_id='$CID' LEFT JOIN positions pos ON pos.signal_id=a.signal_id LEFT JOIN trade_lifecycle_events l ON l.signal_id=a.signal_id GROUP BY a.signal_id,downstream_state ORDER BY a.signal_id;"
```

### 10.3 Closed trade counts and PnL

Canonical realized burn-in outcome summary:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COUNT(*) AS closed_trades,SUM(net_r IS NOT NULL) AS net_r_rows,SUM(net_pnl IS NOT NULL) AS net_pnl_rows,SUM(net_r>0) AS wins,SUM(net_r<=0) AS losses,ROUND(100.0*SUM(net_r>0)/NULLIF(SUM(net_r IS NOT NULL),0),2) AS win_rate,AVG(net_r) AS avg_net_r,SUM(net_r) AS total_net_r,AVG(net_pnl) AS avg_net_pnl_usdt,SUM(net_pnl) AS total_net_pnl_usdt,AVG(hold_duration_seconds)/60.0 AS avg_hold_minutes FROM burnin_trade_outcomes t JOIN burnin_campaign_runs cr ON cr.burnin_run_id=t.burnin_run_id WHERE cr.campaign_id='$CID';"
```

Exit reason distribution and realized TP/SL flags:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT exit_reason,COUNT(*) AS n,SUM(evidence_complete=1) AS evidence_complete,AVG(net_r) AS avg_net_r,SUM(net_r) AS total_net_r FROM burnin_trade_outcomes t JOIN burnin_campaign_runs cr ON cr.burnin_run_id=t.burnin_run_id WHERE cr.campaign_id='$CID' GROUP BY exit_reason ORDER BY n DESC,exit_reason;"
```

Cost decomposition for closed trades:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COUNT(*) AS n,AVG(gross_r) AS avg_gross_r,AVG(spread_cost) AS avg_spread_cost,AVG(entry_slippage_cost+exit_slippage_cost) AS avg_slippage_cost,AVG(fee_cost) AS avg_fee_cost,AVG(funding_cost) AS avg_funding_cost,AVG(latency_cost) AS avg_latency_cost,AVG(volatility_penalty+liquidity_penalty) AS avg_market_penalty,AVG(total_execution_cost) AS avg_total_cost,AVG(net_r) AS avg_net_r FROM burnin_trade_outcomes t JOIN burnin_campaign_runs cr ON cr.burnin_run_id=t.burnin_run_id WHERE cr.campaign_id='$CID';"
```

Closed review rows are a convenience projection; compare with canonical burn-in outcomes before qualification:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT r.trade_id,r.symbol,r.side,r.entry_price,r.exit_price,r.close_reason,r.tp_hit,r.sl_hit,r.effective_rr,r.net_pnl_pct,r.hold_minutes,t.net_r AS canonical_net_r,t.evidence_complete AS canonical_evidence_complete FROM closed_trade_reviews r LEFT JOIN burnin_trade_outcomes t ON t.trade_id=r.trade_id JOIN burnin_campaign_runs cr ON cr.burnin_run_id=t.burnin_run_id WHERE cr.campaign_id='$CID' ORDER BY r.created_at;"
```

## 11. Score, RR, expectancy, and adaptive state

### 11.1 Score/RR summary

Campaign decision score, raw RR, effective RR, and cost/context summaries:

```bash
sqlite3 -readonly -header -column "$DB" "WITH ids AS (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') AS signal_id FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID') SELECT COUNT(*) AS rows,COUNT(DISTINCT d.decision_id) AS decisions,MIN(d.score) AS min_score,AVG(d.score) AS avg_score,MAX(d.score) AS max_score,MIN(d.rr) AS min_raw_rr,AVG(d.rr) AS avg_raw_rr,MAX(d.rr) AS max_raw_rr,MIN(d.effective_rr) AS min_effective_rr,AVG(d.effective_rr) AS avg_effective_rr,MAX(d.effective_rr) AS max_effective_rr,AVG(d.rr-d.effective_rr) AS avg_rr_drag,AVG(d.expected_slippage_pct) AS avg_expected_slippage_pct,AVG(d.spread_pct) AS avg_spread_pct,AVG(d.latency_ms) AS avg_latency_ms,AVG(d.funding_rate_pct) AS avg_funding_rate_pct FROM order_decisions d JOIN ids ON ids.signal_id=d.signal_id;"
```

Raw-versus-effective RR by decision, including the cost drag:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT d.decision_id,d.signal_id,d.symbol,d.decision,d.reject_reason,d.score,d.rr AS raw_rr,d.effective_rr,CASE WHEN d.rr IS NOT NULL AND d.effective_rr IS NOT NULL THEN d.rr-d.effective_rr END AS rr_drag,d.expectancy_bucket,d.execution_ctx_missing,d.expected_slippage_pct,d.spread_pct,d.funding_rate_pct,d.latency_ms FROM order_decisions d WHERE d.signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID') ORDER BY d.created_at,d.id;"
```

Null expectancy buckets and their decisions:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT d.decision,d.reject_reason,COUNT(*) AS n FROM order_decisions d WHERE d.signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID') AND (d.expectancy_bucket IS NULL OR TRIM(d.expectancy_bucket)='') GROUP BY d.decision,d.reject_reason ORDER BY n DESC;"
```

### 11.2 Adaptive and shadow state

Adaptive statistics and threshold snapshots are derived/shadow evidence. An empty `adaptive_threshold_snapshots` table is valid when adaptation has not emitted a snapshot; it is not evidence that no static threshold was used.

```bash
sqlite3 -readonly -header -column "$DB" "SELECT scope_type,scope_key,sample_size,win_rate,avg_net_pnl_pct,avg_effective_rr,avg_spread_pct,avg_slippage_pct,reject_accuracy,expectancy,confidence,updated_at FROM adaptive_stats ORDER BY updated_at DESC; SELECT scope_type,scope_key,min_score,min_effective_rr,max_spread_pct,max_expected_slippage_pct,min_liquidity_score,reason,source,created_at FROM adaptive_threshold_snapshots ORDER BY created_at DESC;"
```

Adaptive decision calibration is written to a separate shadow DB by default. Set `ADAPTIVE_DB` to that file; do not assume the campaign DB also contains these tables:

```bash
ADAPTIVE_DB="${ADAPTIVE_DB:-data/runtime/alphaforge_adaptive_shadow.db}"; sqlite3 -readonly -header -column "$ADAPTIVE_DB" "SELECT COUNT(*) AS adaptive_observations FROM adaptive_decision_observations WHERE campaign_id='$CID'; SELECT COUNT(*) AS shadow_decisions FROM adaptive_shadow_decisions WHERE campaign_id='$CID'; SELECT decision_dimension,reason_or_gate,sample_count,calibration_state,base_value,shadow_value,lower_confidence_bound,upper_confidence_bound,updated_at FROM adaptive_decision_calibration WHERE campaign_id='$CID' ORDER BY updated_at DESC;"
```

Expectancy aggregates by setup, regime, and symbol:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT 'setup' AS dimension,setup AS key,samples,win_count,total_pnl,expectancy,updated_at FROM setup_expectancy_stats UNION ALL SELECT 'regime',regime,samples,win_count,total_pnl,expectancy,updated_at FROM regime_expectancy_stats UNION ALL SELECT 'symbol',symbol,samples,win_count,total_pnl,expectancy,updated_at FROM symbol_expectancy_stats ORDER BY dimension,samples DESC;"
```

## 12. Execution quality

### 12.1 Availability and distributions

Decision-level execution fields are in `order_decisions`; richer source/status fields are in `execution_ctx` JSON or `decision_evidence` when populated.

```bash
sqlite3 -readonly -header -column "$DB" "WITH d AS (SELECT * FROM order_decisions WHERE signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID')) SELECT COUNT(*) AS n,AVG(spread_pct) AS avg_spread_pct,MIN(spread_pct) AS min_spread_pct,MAX(spread_pct) AS max_spread_pct,AVG(expected_slippage_pct) AS avg_expected_slippage_pct,MIN(expected_slippage_pct) AS min_expected_slippage_pct,MAX(expected_slippage_pct) AS max_expected_slippage_pct,AVG(latency_ms) AS avg_latency_ms,MIN(latency_ms) AS min_latency_ms,MAX(latency_ms) AS max_latency_ms,AVG(funding_rate_pct) AS avg_funding_rate_pct,MIN(funding_rate_pct) AS min_funding_rate_pct,MAX(funding_rate_pct) AS max_funding_rate_pct,COUNT(*)-COUNT(spread_pct) AS missing_spread,COUNT(*)-COUNT(expected_slippage_pct) AS missing_slippage,COUNT(*)-COUNT(latency_ms) AS missing_latency,COUNT(*)-COUNT(funding_rate_pct) AS missing_funding,SUM(COALESCE(execution_ctx_missing,0)=1) AS execution_ctx_missing FROM d;"
```

Evidence availability and source/provenance in `decision_evidence`:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COUNT(*) AS rows,COUNT(*)-COUNT(spread_pct) AS missing_spread,COUNT(*)-COUNT(expected_slippage_pct) AS missing_slippage,COUNT(*)-COUNT(latency_ms) AS missing_latency,COUNT(*)-COUNT(funding_rate_pct) AS missing_funding,COUNT(*)-COUNT(liquidity_score) AS missing_liquidity,COUNT(*)-COUNT(volatility_regime) AS missing_volatility,COUNT(*)-COUNT(spread_source) AS missing_spread_source,COUNT(*)-COUNT(slippage_source) AS missing_slippage_source,COUNT(*)-COUNT(unavailable_fields) AS missing_unavailable_fields FROM decision_evidence WHERE run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') OR signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID');"
```

Measured versus estimated/unavailable execution sources where the export surface has them:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COALESCE(spread_source,'NULL') AS spread_source,COALESCE(slippage_source,'NULL') AS slippage_source,COALESCE(fee_source,'NULL') AS fee_source,COALESCE(funding_source,'NULL') AS funding_source,COALESCE(latency_source,'NULL') AS latency_source,COUNT(*) AS n FROM decision_evidence WHERE run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') OR signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID') GROUP BY spread_source,slippage_source,fee_source,funding_source,latency_source ORDER BY n DESC;"
```

### 12.2 Suspicious constant/fake-zero fields

This detects constant values and explicit zeroes; zero is not automatically fake, so inspect `execution_ctx_missing`, source, and unavailable fields alongside it.

```bash
sqlite3 -readonly -header -column "$DB" "WITH d AS (SELECT * FROM order_decisions WHERE signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID')) SELECT 'score' AS field,COUNT(*) AS n,COUNT(score) AS non_null,COUNT(DISTINCT score) AS distinct_values,SUM(score=0) AS zero_values,MIN(score) AS min_value,MAX(score) AS max_value FROM d UNION ALL SELECT 'rr',COUNT(*),COUNT(rr),COUNT(DISTINCT rr),SUM(rr=0),MIN(rr),MAX(rr) FROM d UNION ALL SELECT 'effective_rr',COUNT(*),COUNT(effective_rr),COUNT(DISTINCT effective_rr),SUM(effective_rr=0),MIN(effective_rr),MAX(effective_rr) FROM d UNION ALL SELECT 'spread_pct',COUNT(*),COUNT(spread_pct),COUNT(DISTINCT spread_pct),SUM(spread_pct=0),MIN(spread_pct),MAX(spread_pct) FROM d UNION ALL SELECT 'expected_slippage_pct',COUNT(*),COUNT(expected_slippage_pct),COUNT(DISTINCT expected_slippage_pct),SUM(expected_slippage_pct=0),MIN(expected_slippage_pct),MAX(expected_slippage_pct) FROM d UNION ALL SELECT 'latency_ms',COUNT(*),COUNT(latency_ms),COUNT(DISTINCT latency_ms),SUM(latency_ms=0),MIN(latency_ms),MAX(latency_ms) FROM d UNION ALL SELECT 'funding_rate_pct',COUNT(*),COUNT(funding_rate_pct),COUNT(DISTINCT funding_rate_pct),SUM(funding_rate_pct=0),MIN(funding_rate_pct),MAX(funding_rate_pct) FROM d;"
```

### 12.3 JSON execution context

Check JSON validity before extracting execution fields:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COUNT(*) AS rows,SUM(json_valid(COALESCE(execution_ctx,''))) AS valid_json,SUM(NOT json_valid(COALESCE(execution_ctx,''))) AS invalid_json,SUM(json_extract(execution_ctx,'$.evidence_status') IS NULL) AS missing_status,SUM(json_extract(execution_ctx,'$.evidence_status') IN ('UNAVAILABLE','UNKNOWN')) AS unavailable_status FROM order_decisions WHERE signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID');"
```

## 13. MTF

MTF diagnostics live in JSON, primarily `burnin_observations.metrics_json`; `order_decisions` carries execution/volatility fields but not the full regime/setup/execution hierarchy.

Show the persisted timeframe, direction, alignment, confirmation, and setup fields:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT json_extract(o.metrics_json,'$.symbol') AS symbol,json_extract(o.metrics_json,'$.side') AS side,json_extract(o.metrics_json,'$.mtf.alignment.timeframes.regime') AS regime_tf,json_extract(o.metrics_json,'$.mtf.alignment.timeframes.setup') AS setup_tf,json_extract(o.metrics_json,'$.mtf.alignment.timeframes.execution') AS execution_tf,json_extract(o.metrics_json,'$.mtf.alignment.base_exec_direction') AS base_direction,json_extract(o.metrics_json,'$.mtf.alignment.final_direction') AS final_direction,json_extract(o.metrics_json,'$.mtf.alignment.resolved_state') AS resolved_state,json_extract(o.metrics_json,'$.mtf.alignment.regime_alignment') AS regime_alignment,json_extract(o.metrics_json,'$.mtf.alignment.setup_alignment') AS setup_alignment,json_extract(o.metrics_json,'$.mtf.alignment.execution_alignment') AS execution_alignment,json_extract(o.metrics_json,'$.mtf.execution.confirmed_for_side') AS confirmed_for_side,json_extract(o.metrics_json,'$.mtf.execution.execution_regime') AS execution_regime,json_extract(o.metrics_json,'$.mtf.setup.setup_type') AS setup_type,o.observed_at FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID' AND json_valid(o.metrics_json) ORDER BY o.observed_at;"
```

MTF reject counts by reason, side, and regime:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT json_extract(o.metrics_json,'$.symbol') AS symbol,COALESCE(json_extract(o.metrics_json,'$.side'),s.side) AS side,COALESCE(json_extract(o.metrics_json,'$.mtf.regime.regime'),o.regime,'UNKNOWN') AS regime,COALESCE(d.reject_reason,json_extract(o.metrics_json,'$.primary_reject_reason')) AS reject_reason,COUNT(*) AS n FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id LEFT JOIN order_decisions d ON d.decision_id=json_extract(o.metrics_json,'$.reject_decision_id') LEFT JOIN signals s ON s.signal_id=json_extract(o.metrics_json,'$.signal_id') WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,''))='REJECTED' AND COALESCE(d.reject_reason,json_extract(o.metrics_json,'$.primary_reject_reason')) LIKE 'MTF_%' GROUP BY 1,2,3,4 ORDER BY n DESC;"
```

Long/short asymmetry and regime concentration:

```bash
sqlite3 -readonly -header -column "$DB" "WITH x AS (SELECT COALESCE(json_extract(o.metrics_json,'$.side'),s.side,'UNKNOWN') AS side,COALESCE(json_extract(o.metrics_json,'$.mtf.regime.regime'),o.regime,'UNKNOWN') AS regime,UPPER(COALESCE(o.decision,'')) AS decision FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id LEFT JOIN signals s ON s.signal_id=json_extract(o.metrics_json,'$.signal_id') WHERE cr.campaign_id='$CID') SELECT side,regime,decision,COUNT(*) AS n,ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER (PARTITION BY side),2) AS pct_within_side FROM x GROUP BY side,regime,decision ORDER BY side,n DESC;"
```

If `json_extract` errors or returns no useful values, check the SQLite JSON1 capability and row validity; do not infer MTF state from a missing value:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COUNT(*) AS rows,SUM(json_valid(metrics_json)) AS valid_metrics_json,SUM(NOT json_valid(metrics_json)) AS invalid_metrics_json FROM burnin_observations;"
```

## 14. Lifecycle

### 14.1 One signal's ordered event history

```bash
SIGNAL_ID="..."; sqlite3 -readonly -header -column "$DB" "SELECT event_ts,COALESCE(lifecycle_seq,999999) AS lifecycle_seq,event_type,lifecycle_state,state,decision,reject_reason,cancel_reason,failure_reason,order_id,trade_id,lifecycle_id,execution_ctx_missing FROM trade_lifecycle_events WHERE signal_id='$SIGNAL_ID' ORDER BY event_ts,COALESCE(lifecycle_seq,999999),id;"
```

The requested compact lifecycle view:

```bash
SIGNAL_ID="..."; sqlite3 -readonly -header -column "$DB" "SELECT event_ts,lifecycle_seq,event_type,lifecycle_state,decision,reject_reason,order_id,trade_id AS position_id FROM trade_lifecycle_events WHERE signal_id='$SIGNAL_ID' ORDER BY event_ts,COALESCE(lifecycle_seq,999999),id;"
```

### 14.2 Latest events and terminal follow-up

```bash
sqlite3 -readonly -header -column "$DB" "SELECT event_ts,event_type,lifecycle_state,signal_id,order_id,trade_id,reject_reason,cancel_reason,failure_reason FROM trade_lifecycle_events ORDER BY event_ts DESC,id DESC LIMIT 100;"
```

Signals with `SIGNAL_CREATED` but no later terminal event. This is a review candidate, not automatically a violation for a still-open/waiting signal:

```bash
sqlite3 -readonly -header -column "$DB" "WITH e AS (SELECT signal_id,MAX(CASE WHEN lifecycle_state='SIGNAL_CREATED' THEN 1 ELSE 0 END) AS created,MAX(CASE WHEN lifecycle_state IN ('SIGNAL_REJECTED','TP_HIT','SL_HIT','CANCELLED','OPEN_AT_END','ENTRY_TIMEOUT','POSITION_CLOSED') THEN 1 ELSE 0 END) AS terminal,MAX(event_ts) AS last_event_ts FROM trade_lifecycle_events GROUP BY signal_id) SELECT signal_id,last_event_ts FROM e WHERE created=1 AND terminal=0 ORDER BY last_event_ts;"
```

Rejected events and accepted/order paths:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT lifecycle_state,event_type,decision,reject_reason,COUNT(*) AS n FROM trade_lifecycle_events WHERE lifecycle_state='SIGNAL_REJECTED' GROUP BY lifecycle_state,event_type,decision,reject_reason ORDER BY n DESC; SELECT lifecycle_state,event_type,COUNT(*) AS n FROM trade_lifecycle_events WHERE lifecycle_state IN ('SIGNAL_VALIDATED','WAITING_ENTRY_ZONE','ENTRY_TRIGGERED','ORDER_PLACED','PARTIAL_FILL','FILLED','POSITION_OPENED','TP_HIT','SL_HIT','CANCELLED','OPEN_AT_END') GROUP BY lifecycle_state,event_type ORDER BY n DESC;"
```

Duplicate lifecycle sequence candidates, limited to explicitly populated sequence numbers:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT signal_id,lifecycle_seq,COUNT(*) AS n,MIN(event_ts) AS first_ts,MAX(event_ts) AS last_ts FROM trade_lifecycle_events WHERE lifecycle_seq IS NOT NULL GROUP BY signal_id,lifecycle_seq HAVING COUNT(*)>1 ORDER BY n DESC,signal_id,lifecycle_seq;"
```

Orphan lifecycle events and signals with no lifecycle evidence:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT l.signal_id,COUNT(*) AS lifecycle_rows FROM trade_lifecycle_events l LEFT JOIN signals s ON s.signal_id=l.signal_id WHERE s.signal_id IS NULL GROUP BY l.signal_id ORDER BY lifecycle_rows DESC; SELECT s.signal_id,s.symbol,s.created_at FROM signals s LEFT JOIN trade_lifecycle_events l ON l.signal_id=s.signal_id WHERE l.signal_id IS NULL ORDER BY s.created_at;"
```

### 14.3 Lifecycle/export reconciliation for a campaign

```bash
sqlite3 -readonly -header -column "$DB" "WITH cs AS (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') AS signal_id FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID') SELECT COUNT(DISTINCT cs.signal_id) AS campaign_signals,COUNT(DISTINCT l.signal_id) AS signals_with_lifecycle,COUNT(DISTINCT CASE WHEN l.signal_id IS NULL THEN cs.signal_id END) AS signals_without_lifecycle,COUNT(*) AS lifecycle_rows FROM cs LEFT JOIN trade_lifecycle_events l ON l.signal_id=cs.signal_id;"
```

## 15. Evidence quality

### 15.1 Decision evidence availability and parity

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COUNT(*) AS decision_evidence_rows,COUNT(DISTINCT mode) AS modes,COUNT(DISTINCT decision) AS decisions,COUNT(DISTINCT lifecycle_state_after) AS lifecycle_states,COUNT(*)-COUNT(score) AS missing_score,COUNT(*)-COUNT(raw_rr) AS missing_raw_rr,COUNT(*)-COUNT(effective_rr) AS missing_effective_rr,COUNT(*)-COUNT(spread_pct) AS missing_spread,COUNT(*)-COUNT(expected_slippage_pct) AS missing_slippage,COUNT(*)-COUNT(liquidity_score) AS missing_liquidity FROM decision_evidence WHERE run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') OR signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID');"
```

Evidence by mode and decision:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT mode,decision,COUNT(*) AS n,COUNT(DISTINCT signal_id) AS signals,COUNT(*)-COUNT(effective_rr) AS missing_effective_rr,COUNT(*)-COUNT(unavailable_fields) AS missing_unavailable_fields FROM decision_evidence GROUP BY mode,decision ORDER BY mode,decision;"
```

Campaign evidence join coverage. `run_id` is the only direct burn-in-style key on `decision_evidence`; signal ID is the fallback bridge.

```bash
sqlite3 -readonly -header -column "$DB" "WITH cs AS (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') AS signal_id FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID'),ce AS (SELECT DISTINCT e.evidence_id,e.signal_id,e.run_id FROM decision_evidence e LEFT JOIN cs ON cs.signal_id=e.signal_id WHERE e.run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') OR cs.signal_id IS NOT NULL) SELECT COUNT(*) AS evidence_rows,COUNT(signal_id) AS evidence_with_signal,COUNT(run_id) AS evidence_with_run FROM ce;"
```

Parity mismatch and incomplete-field checks:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COUNT(*) AS parity_mismatches FROM order_decisions WHERE signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID') AND (UPPER(COALESCE(parity_result,'')) LIKE '%MISMATCH%' OR UPPER(COALESCE(reject_reason,''))='DECISION_PARITY_MISMATCH'); SELECT COUNT(*) AS evidence_parity_mismatches FROM decision_evidence WHERE (UPPER(COALESCE(reject_reason,''))='DECISION_PARITY_MISMATCH' OR UPPER(COALESCE(diagnostics_json,'')) LIKE '%DECISION_PARITY_MISMATCH%') AND (run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') OR signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID'));"
```

Duplicate and orphan evidence IDs:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT evidence_id,COUNT(*) AS n FROM decision_evidence GROUP BY evidence_id HAVING COUNT(*)>1; SELECT e.evidence_id,e.signal_id,e.lifecycle_id FROM decision_evidence e LEFT JOIN signals s ON s.signal_id=e.signal_id LEFT JOIN trade_lifecycle_events l ON l.lifecycle_id=e.lifecycle_id WHERE s.signal_id IS NULL AND l.lifecycle_id IS NULL;"
```

Lifecycle without decision evidence and evidence without lifecycle are coverage comparisons, not automatic corruption findings:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COUNT(DISTINCT l.signal_id) AS lifecycle_signals_without_evidence FROM trade_lifecycle_events l LEFT JOIN decision_evidence e ON e.signal_id=l.signal_id WHERE e.signal_id IS NULL; SELECT COUNT(DISTINCT e.signal_id) AS evidence_signals_without_lifecycle FROM decision_evidence e LEFT JOIN trade_lifecycle_events l ON l.signal_id=e.signal_id WHERE e.signal_id IS NOT NULL AND l.signal_id IS NULL;"
```

### 15.2 Other evidence surfaces

```bash
sqlite3 -readonly -header -column "$DB" "SELECT evidence_type,COUNT(*) AS n,SUM(evidence_complete=1) AS complete,MIN(decision_time) AS first_decision_time,MAX(resolved_at) AS last_resolved_at FROM expectancy_evidence WHERE campaign_id='$CID' OR run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') GROUP BY evidence_type ORDER BY n DESC; SELECT COUNT(*) AS rejected_review_rows,SUM(evidence_complete=1) AS complete_reviews FROM rejected_signal_reviews WHERE signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID');"
```

## 16. Resolver

### 16.1 Pending backlog by status

```bash
sqlite3 -readonly -header -column "$DB" "SELECT status,COUNT(*) AS n,MIN(due_at) AS oldest_due_at,MAX(due_at) AS newest_due_at,SUM(evidence_complete=1) AS evidence_complete FROM burnin_pending_reject_labels WHERE campaign_id='$CID' GROUP BY status ORDER BY status;"
```

PENDING/READY/RESOLVING rows overdue at the database clock:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT pending_label_id,reject_decision_id,signal_id,symbol,side,reject_reason,status,due_at,ROUND((julianday('now')-julianday(due_at))*86400.0,1) AS overdue_seconds,last_error,claimed_at FROM burnin_pending_reject_labels WHERE campaign_id='$CID' AND status IN ('PENDING','READY','RESOLVING') ORDER BY due_at;"
```

Oldest pending age and due-at distribution:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT MIN(created_at) AS oldest_created_at,MIN(due_at) AS oldest_due_at,MAX(created_at) AS newest_created_at,SUM(status IN ('PENDING','READY','RESOLVING')) AS open_backlog,SUM(status IN ('PENDING','READY','RESOLVING') AND due_at<=datetime('now')) AS overdue_backlog FROM burnin_pending_reject_labels WHERE campaign_id='$CID';"
```

### 16.2 Resolver failures and outcome reconciliation

```bash
sqlite3 -readonly -header -column "$DB" "SELECT status,last_error,COUNT(*) AS n FROM burnin_pending_reject_labels WHERE campaign_id='$CID' AND status IN ('FAILED','EXPIRED') GROUP BY status,last_error ORDER BY n DESC; SELECT COUNT(*) AS outcomes_without_pending FROM burnin_reject_outcomes ro LEFT JOIN burnin_pending_reject_labels p ON p.reject_decision_id=json_extract(ro.payload_json,'$.reject_decision_id') AND p.campaign_id='$CID' WHERE ro.burnin_run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') AND p.pending_label_id IS NULL;"
```

## 17. Qualification

Latest snapshot with all gate statuses and JSON evidence:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT q.qualification_id,q.campaign_id,q.burnin_run_id,q.release_id,q.generated_at,q.status,q.sample_status,q.expectancy_status,q.execution_status,q.regime_status,q.reject_quality_status,q.calibration_status,q.drawdown_status,q.concentration_status,q.reconciliation_status,q.evidence_completeness_status,q.blockers_json,q.warnings_json,q.thresholds_json,q.metrics_json,q.evidence_hash,q.aggregate_evidence_hash FROM burnin_qualification_snapshots q WHERE q.campaign_id='$CID' OR q.burnin_run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') ORDER BY q.generated_at DESC,q.id DESC LIMIT 1;"
```

Historical qualification snapshots:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,expectancy_status,execution_status,regime_status,reject_quality_status,calibration_status,drawdown_status,concentration_status,reconciliation_status,evidence_completeness_status,evidence_hash FROM burnin_qualification_snapshots WHERE campaign_id='$CID' OR burnin_run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') ORDER BY generated_at DESC,id DESC;"
```

Extract common qualification inputs when the producer has emitted those JSON keys. Null means the key is not present; it is not zero.

```bash
sqlite3 -readonly -header -column "$DB" "WITH q AS (SELECT * FROM burnin_qualification_snapshots WHERE campaign_id='$CID' OR burnin_run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') ORDER BY generated_at DESC,id DESC LIMIT 1) SELECT qualification_id,status,generated_at,json_extract(thresholds_json,'$.minimum_duration_seconds') AS min_duration_seconds,json_extract(metrics_json,'$.observed_duration_seconds') AS observed_duration_seconds,json_extract(thresholds_json,'$.minimum_decisions') AS min_decisions,json_extract(metrics_json,'$.sample_count') AS sample_count,json_extract(thresholds_json,'$.minimum_accepted_trades') AS min_accepted_trades,json_extract(metrics_json,'$.accepted_count') AS accepted_count,json_extract(thresholds_json,'$.minimum_closed_trades') AS min_closed_trades,json_extract(metrics_json,'$.closed_trade_count') AS closed_trade_count,json_extract(metrics_json,'$.reject_forward_outcomes') AS reject_forward_outcomes,json_extract(metrics_json,'$.expectancy_confidence') AS expectancy_confidence,json_extract(metrics_json,'$.cost_drag') AS cost_drag,json_extract(metrics_json,'$.regime_coverage') AS regime_coverage,json_extract(metrics_json,'$.reject_quality') AS reject_quality,json_extract(metrics_json,'$.calibration') AS calibration,json_extract(metrics_json,'$.execution_evidence') AS execution_evidence,json_extract(metrics_json,'$.concentration') AS concentration,json_extract(metrics_json,'$.operator_ack') AS operator_ack,json_extract(metrics_json,'$.release_gate') AS release_gate,json_extract(metrics_json,'$.rollback') AS rollback,json_extract(metrics_json,'$.runbook') AS runbook FROM q;"
```

Blocker list as one row per JSON array element, when JSON1 is available:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT q.qualification_id,q.generated_at,j.value AS blocker FROM burnin_qualification_snapshots q,json_each(CASE WHEN json_valid(q.blockers_json) THEN q.blockers_json ELSE '[]' END) j WHERE q.campaign_id='$CID' ORDER BY q.generated_at DESC;"
```

## 18. Runtime health

### 18.1 Heartbeat and scan freshness

Latest PAPER heartbeat and age in seconds. Timestamps must be ISO-8601 values SQLite can parse; otherwise `age_seconds` is null and freshness is unavailable.

```bash
sqlite3 -readonly -header -column "$DB" "SELECT id,runtime_instance_id,execution_mode,heartbeat_ts,ROUND((julianday('now')-julianday(heartbeat_ts))*86400.0,1) AS age_seconds,runtime_state,last_scan_ts,last_decision_ts,active_positions_count,pending_orders_count,evidence_status,payload_json FROM runtime_heartbeats WHERE UPPER(execution_mode)='PAPER' ORDER BY id DESC LIMIT 1;"
```

Runtime state, kill switch, recovery, and exchange reconciliation:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT timestamp,created_at,instance_id,startup_id,process_id,campaign_id,burnin_run_id,release_id,mode,actual_mode,runtime_status,heartbeat_age_sec,kill_switch_active,kill_switch_reason,active_position_count,pending_order_count,orphan_order_count,orphan_position_count,exchange_connectivity_status,exchange_read_only_status,reconciliation_status,reconciliation_mismatch_count,recovery_action_required,fail_closed_reason,last_error FROM runtime_state_snapshots ORDER BY id DESC LIMIT 10;"
```

### 18.2 Failures, recovery, and provider incidents

```bash
sqlite3 -readonly -header -column "$DB" "SELECT 'runtime_recovery' AS surface,status,reason,event_ts AS ts,diagnostics_json FROM runtime_recovery_events ORDER BY id DESC LIMIT 30; SELECT 'exchange_reconciliation',status,CAST(mismatch_count AS TEXT),event_ts,diagnostics_json FROM exchange_reconciliation_events ORDER BY id DESC LIMIT 30; SELECT 'burnin_ops_incident',status,incident_type,detected_at,details_json FROM burnin_ops_incidents WHERE campaign_id='$CID' ORDER BY id DESC LIMIT 30; SELECT 'reconciliation_incident',remediation_status,incident_type,created_at,forensic_payload FROM reconciliation_incidents ORDER BY id DESC LIMIT 30;"
```

Current control state and recent control audit:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT id,mode_requested,mode_running,kill_switch_active,kill_switch_source,kill_switch_updated_at,runtime_status,last_error,updated_at FROM runtime_control_state WHERE id=1; SELECT event_ts,action,requested_mode,previous_mode,success,reason,source,kill_switch_active,readiness_status,operator_acknowledged FROM runtime_control_audit_events ORDER BY event_ts DESC LIMIT 30;"
```

### 18.3 Restart count and lineage

```bash
sqlite3 -readonly -header -column "$DB" "SELECT campaign_id,worker_pid,worker_started_at,restart_count,active_run_id,last_heartbeat_at,last_operator_activity_at,last_error FROM burnin_campaigns WHERE campaign_id='$CID'; SELECT instance_id,startup_id,process_id,campaign_id,burnin_run_id,release_id,last_start_time,last_shutdown_time,runtime_status FROM runtime_state_snapshots WHERE campaign_id='$CID' ORDER BY id DESC LIMIT 20;"
```

## 19. Integrity and orphan checks

Only the following are hard uniqueness checks when the current schema enforces them. Other queries are explicitly labeled review candidates because business semantics may allow the row to be temporarily absent.

### 19.1 Duplicate identities

```bash
sqlite3 -readonly -header -column "$DB" "SELECT decision_id,COUNT(*) AS n FROM order_decisions GROUP BY decision_id HAVING COUNT(*)>1; SELECT signal_id,COUNT(*) AS n FROM signals WHERE signal_id IS NOT NULL GROUP BY signal_id HAVING COUNT(*)>1; SELECT pending_label_id,COUNT(*) AS n FROM burnin_pending_reject_labels GROUP BY pending_label_id HAVING COUNT(*)>1; SELECT reject_decision_id,COUNT(*) AS n FROM burnin_pending_reject_labels GROUP BY reject_decision_id HAVING COUNT(*)>1; SELECT reject_outcome_id,COUNT(*) AS n FROM burnin_reject_outcomes GROUP BY reject_outcome_id HAVING COUNT(*)>1; SELECT event_id,COUNT(*) AS n FROM trade_lifecycle_events GROUP BY event_id HAVING COUNT(*)>1; SELECT campaign_id,continuation_sequence,COUNT(*) AS n FROM burnin_campaign_runs GROUP BY campaign_id,continuation_sequence HAVING COUNT(*)>1;"
```

Duplicate lifecycle sequence numbers are only candidates when `lifecycle_seq` is non-null; see [lifecycle](#143-latest-events-and-terminal-follow-up).

### 19.2 Orphan joins

Pending reject labels without their canonical decision:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT p.pending_label_id,p.reject_decision_id,p.signal_id,p.campaign_id,p.burnin_run_id,p.status FROM burnin_pending_reject_labels p LEFT JOIN order_decisions d ON d.decision_id=p.reject_decision_id WHERE d.decision_id IS NULL AND p.campaign_id='$CID' ORDER BY p.created_at;"
```

Reject outcomes without a pending label. This uses the explicit JSON identity written by the resolver; do not infer ownership from the `rout_` prefix alone.

```bash
sqlite3 -readonly -header -column "$DB" "SELECT ro.reject_outcome_id,ro.burnin_run_id,json_extract(ro.payload_json,'$.campaign_id') AS campaign_id,json_extract(ro.payload_json,'$.reject_decision_id') AS reject_decision_id,json_extract(ro.payload_json,'$.pending_label_id') AS pending_label_id FROM burnin_reject_outcomes ro LEFT JOIN burnin_pending_reject_labels p ON p.pending_label_id=json_extract(ro.payload_json,'$.pending_label_id') AND p.reject_decision_id=json_extract(ro.payload_json,'$.reject_decision_id') WHERE ro.burnin_run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id='$CID') AND p.pending_label_id IS NULL;"
```

Accepted decision without downstream position is a review candidate, not a guaranteed defect:

```bash
sqlite3 -readonly -header -column "$DB" "WITH accepted AS (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') AS signal_id FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,'')) IN ('ACCEPT','ACCEPTED')) SELECT a.signal_id FROM accepted a LEFT JOIN burnin_pending_position_outcomes p ON p.signal_id=a.signal_id AND p.campaign_id='$CID' LEFT JOIN positions pos ON pos.signal_id=a.signal_id WHERE p.signal_id IS NULL AND pos.signal_id IS NULL;"
```

Closed position without a canonical trade outcome is also a review candidate. Runtime positions lack a campaign key, so this is a cross-surface diagnostic, not a definitive campaign violation:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT p.position_id,p.signal_id,p.status,p.updated_at FROM positions p LEFT JOIN burnin_pending_position_outcomes bp ON bp.signal_id=p.signal_id AND bp.campaign_id='$CID' LEFT JOIN burnin_trade_outcomes bt ON bt.trade_id=bp.trade_id WHERE UPPER(COALESCE(p.status,'')) IN ('CLOSED','POSITION_CLOSED','EXITED') AND bt.outcome_id IS NULL;"
```

Rejected observation without pending label (eligible PAPER rejects only) and pending label without a canonical burn-in observation:

```bash
sqlite3 -readonly -header -column "$DB" "WITH r AS (SELECT DISTINCT json_extract(o.metrics_json,'$.reject_decision_id') AS reject_decision_id FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID' AND UPPER(COALESCE(o.decision,''))='REJECTED') SELECT r.reject_decision_id FROM r LEFT JOIN burnin_pending_reject_labels p ON p.reject_decision_id=r.reject_decision_id AND p.campaign_id='$CID' WHERE p.pending_label_id IS NULL; SELECT p.pending_label_id,p.reject_decision_id FROM burnin_pending_reject_labels p LEFT JOIN burnin_observations o ON o.burnin_run_id=p.burnin_run_id AND json_extract(o.metrics_json,'$.reject_decision_id')=p.reject_decision_id WHERE p.campaign_id='$CID' AND o.observation_id IS NULL;"
```

### 19.3 Schema and foreign-key metadata

The main campaign schema has logical joins and generally no declared foreign keys. Verify a target DB rather than assuming:

```bash
sqlite3 -readonly -header -column "$DB" "PRAGMA foreign_key_list(trade_lifecycle_events); PRAGMA foreign_key_list(burnin_pending_reject_labels); PRAGMA foreign_key_list(state_direction_shadow_outcomes);"
```

## 20. Symbol, regime, and side concentration

### 20.1 Decision concentration

```bash
sqlite3 -readonly -header -column "$DB" "WITH x AS (SELECT json_extract(o.metrics_json,'$.signal_id') AS signal_id,COALESCE(json_extract(o.metrics_json,'$.symbol'),o.symbol,'UNKNOWN') AS symbol,COALESCE(s.side,json_extract(o.metrics_json,'$.side'),'UNKNOWN') AS side,COALESCE(o.regime,'UNKNOWN') AS regime,UPPER(COALESCE(o.decision,'')) AS decision FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id LEFT JOIN signals s ON s.signal_id=json_extract(o.metrics_json,'$.signal_id') WHERE cr.campaign_id='$CID') SELECT symbol,decision,COUNT(DISTINCT signal_id) AS n,ROUND(100.0*COUNT(DISTINCT signal_id)/SUM(COUNT(DISTINCT signal_id)) OVER (),2) AS pct FROM x GROUP BY symbol,decision ORDER BY n DESC;"
```

Standalone side concentration query:

```bash
sqlite3 -readonly -header -column "$DB" "WITH x AS (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') AS signal_id,COALESCE(s.side,json_extract(o.metrics_json,'$.side'),'UNKNOWN') AS side FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id LEFT JOIN signals s ON s.signal_id=json_extract(o.metrics_json,'$.signal_id') WHERE cr.campaign_id='$CID') SELECT side,COUNT(*) AS n,ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER (),2) AS pct FROM x GROUP BY side ORDER BY n DESC;"
```

Regime distribution and accepted/rejected concentration:

```bash
sqlite3 -readonly -header -column "$DB" "WITH x AS (SELECT COALESCE(o.regime,json_extract(o.metrics_json,'$.mtf.regime.regime'),'UNKNOWN') AS regime,UPPER(COALESCE(o.decision,'')) AS decision FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID') SELECT regime,decision,COUNT(*) AS n,ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER (PARTITION BY decision),2) AS pct_within_decision FROM x GROUP BY regime,decision ORDER BY decision,n DESC;"
```

Trade concentration from realized outcomes:

```bash
sqlite3 -readonly -header -column "$DB" "WITH t AS (SELECT symbol,COUNT(*) AS n FROM burnin_trade_outcomes t JOIN burnin_campaign_runs cr ON cr.burnin_run_id=t.burnin_run_id WHERE cr.campaign_id='$CID' GROUP BY symbol) SELECT symbol,n,ROUND(100.0*n/SUM(n) OVER (),2) AS pct FROM t ORDER BY n DESC;"
```

## 21. Configuration and threshold verification

### 21.1 Supported names and current `.env` values

Search only environment-style files and exclude runtime data. This is read-only:

```bash
rg -n --hidden --glob '.env*' --glob '!data/**' '^(ALPHAFORGE_MIN_SIGNAL_SCORE|ALPHAFORGE_MIN_RR|MIN_EFFECTIVE_RR|ALPHAFORGE_MIN_EFFECTIVE_RR|ALPHAFORGE_MAX_SPREAD_PCT|ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT|ALPHAFORGE_PAPER_FEE_BPS|ALPHAFORGE_PAPER_EXECUTION_LATENCY_MS|ALPHAFORGE_MAX_TOTAL_COST_PCT|ALPHAFORGE_MIN_LIQUIDITY_SCORE|ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT|MIN_LIQUIDITY_USD|ALPHAFORGE_REJECT_FORWARD_HORIZON_BARS|ALPHAFORGE_REGIME_TIMEFRAME|ALPHAFORGE_SETUP_TIMEFRAME|ALPHAFORGE_EXECUTION_TIMEFRAME|ALPHAFORGE_MTF_GUIDED_SIGNAL_GENERATION_ENABLED|ALPHAFORGE_ENABLE_SHADOW_MODE|ALPHAFORGE_ENABLE_STATE_DIRECTION_SHADOW_EVALUATION|ALPHAFORGE_MAX_TRADES_GLOBAL_PER_DAY|ALPHAFORGE_MAX_TRADES_SYMBOL_PER_DAY|ALPHAFORGE_BACKTEST_MAX_TRADES)=' .
```

Canonical registry findings:

- `ALPHAFORGE_MIN_SIGNAL_SCORE` is the supported score threshold. `ALPHAFORGE_MIN_TRADE_SCORE` is listed as a reserved/unsupported candidate and has no safe production consumer in the current registry/tests.
- `ALPHAFORGE_MIN_RR` is raw RR. `MIN_EFFECTIVE_RR` is the canonical execution-adjusted RR key; `ALPHAFORGE_MIN_EFFECTIVE_RR` is a deprecated alias.
- PAPER cost/config names include `ALPHAFORGE_PAPER_FEE_BPS`, `ALPHAFORGE_PAPER_EXECUTION_LATENCY_MS`, spread/slippage/funding/liquidity limits, and MTF timeframe names above.
- `PAPER_CANDIDATE_NOTIONAL` appears in a config-to-diagnostics mapping, but is not a registered environment setting in `config_registry.py`. Do not treat it as a supported `.env` control without tracing the actual runtime consumer.

### 21.2 Campaign-frozen provenance

The `.env` is mutable external state. The campaign row is the safe source for the identity/config hashes that the running process attached to:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT campaign_id,release_id,git_commit,config_hash,strategy_config_hash,execution_cost_config_hash,universe_hash,symbols_json,intervals_json,source_provenance_json,created_at,started_at,active_run_id FROM burnin_campaigns WHERE campaign_id='$CID';"
```

Run-level frozen provenance and counters:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT burnin_run_id,release_id,execution_mode,git_commit,config_hash,strategy_config_hash,universe_hash,source_provenance_json,symbols_json,intervals_json,start_time,end_time,status FROM burnin_runs WHERE burnin_run_id='$RID';"
```

Inspect runtime-effective config/provenance if it was emitted in JSON snapshots:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT timestamp,campaign_id,burnin_run_id,release_id,json_extract(diagnostics_json,'$.config') AS config_json,json_extract(diagnostics_json,'$.config_hash') AS diagnostics_config_hash,json_extract(diagnostics_json,'$.strategy_config_hash') AS diagnostics_strategy_config_hash FROM runtime_state_snapshots WHERE campaign_id='$CID' ORDER BY id DESC LIMIT 10;"
```

Changing `.env` does not retroactively change an already-running process or its campaign-frozen hashes. Do not restart a campaign to pick up a value during an audit.

## 22. zsh-safe query patterns

Use one-line commands when possible. The recurring `zsh: parse error near ')'` failure is usually caused by a broken quote, a shell comment pasted inside a multiline command, an unquoted variable, or a command substitution split across lines.

Rules:

- Quote the database path: `"$DB"`.
- Keep SQL inside one pair of double quotes for simple shell-expanded `$CID`/`$RID` filters.
- Keep SQL literals in single quotes inside that double-quoted SQL string.
- Do not put `#` comments inside a copied SQL command.
- If a query contains literal shell `$` characters, use a single-quoted shell heredoc only when the command is not being pasted into an active runtime context; the one-line form is safer for this cheat sheet.
- `$CID` and `$RID` should remain shell-expanded in the examples. If the ID is untrusted or contains a quote, use a parameterized client instead of interpolation.
- Do not use `sqlite3` without `-readonly` for audit work.

Copy/paste-safe examples:

```bash
sqlite3 -readonly -header -column "$DB" "SELECT COUNT(*) AS n FROM order_decisions WHERE decision='REJECTED' AND signal_id IN (SELECT DISTINCT json_extract(o.metrics_json,'$.signal_id') FROM burnin_observations o JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id WHERE cr.campaign_id='$CID');"
sqlite3 -readonly -header -column "$DB" "SELECT * FROM burnin_runs WHERE burnin_run_id='$RID';"
```

For a multiline SQL editor, store only SQL in a file, then invoke it read-only:

```bash
sqlite3 -readonly -header -column "$DB" < /path/to/read-only-query.sql
```

Do not combine shell assignments, comments, SQL, and `sqlite3` dot-commands in one pasted block unless you have verified the quoting in the current shell.

## 23. Common troubleshooting

### Why are accepted decisions zero?

Check the decision vocabulary (`ACCEPT` versus `ACCEPTED`), canonical burn-in observations, MTF/score rejects, and whether you accidentally counted only `orders` or `positions`. Then inspect the [reject](#9-reject-analysis), [score](#11-score-rr-expectancy-and-adaptive-state), and [MTF](#13-mtf) queries. A zero accepted count is a valid result; never manufacture accepted rows from theoretical candidates.

### Why are reject outcomes zero?

Check pending status, `due_at`, market-window completeness, execution-cost fields, resolver errors, and campaign/run ownership. `burnin_reject_outcomes` legitimately remains empty while the forward horizon is not mature or labels are incomplete.

### Why is `decision_evidence` empty?

The table is present in the canonical schema and is read by live-readiness checks, but current source inspection found no production insert writer under `src/alphaforge`. It may be an export/readiness surface or an implementation gap for the selected runtime path. Report its existence, row count, lifecycle coverage, and readiness result separately; use `trade_lifecycle_events`, `order_decisions`, and burn-in evidence for the direct runtime audit.

### Why do global rows not match campaign rows?

Core runtime tables have no direct campaign key. Use `burnin_campaign_runs`, observation JSON signal IDs, pending label identities, pending position identities, and runtime snapshot lineage. Do not filter a global table by a timestamp range and call that campaign isolation.

### Why is the heartbeat stale?

Compare `runtime_heartbeats.heartbeat_ts` and `last_scan_ts` with `runtime_state_snapshots.runtime_status`, `process_id`, `campaigns.last_heartbeat_at`, recovery events, and provider/reconciliation incidents. Do not attach to or restart the process as part of a SQL audit.

### Why is a field NULL or zero?

Check `execution_ctx_missing`, JSON `evidence_status`, source/status fields, `unavailable_fields`, and the suspicious-constant query. AlphaForge requires unavailable execution inputs to be explicit; a zero is not a safe substitute for missing spread, slippage, funding, liquidity, or latency.

## 24. Query index

| Need | Section |
|---|---|
| Discover a safe DB | [Quick start](#3-quick-start) |
| Show all tables/columns/indexes | [Database discovery](#81-database-discovery-and-schema-metadata) |
| Latest campaign status | [Campaign queries](#82-list-campaigns-runs-release-and-qualification) |
| Why zero ACCEPTED? | [Reject analysis](#9-reject-analysis) + [Score/RR](#11-score-rr-expectancy-and-adaptive-state) + [MTF](#13-mtf) |
| Reject reason distribution | [Reject reason distribution](#92-reject-reason-distribution-and-concentration) |
| Why LOW_SCORE? | [LOW_SCORE statistics](#93-reject-score-distribution-and-low_score-statistics) |
| Why MTF rejects? | [MTF](#13-mtf) |
| Pending resolver backlog | [Resolver](#16-resolver) |
| Closed PnL | [Closed trade counts and PnL](#103-closed-trade-counts-and-pnl) |
| Open positions | [Open and closed positions](#102-open-and-closed-positions) |
| Evidence missing | [Evidence quality](#15-evidence-quality) |
| Qualification blockers | [Qualification](#17-qualification) |
| Runtime alive? | [Runtime health](#18-runtime-health) |
| Find orphan reject labels | [Integrity/orphans](#192-orphan-joins) |
| Score/RR variability | [Score/RR](#11-score-rr-expectancy-and-adaptive-state) |
| Execution fields missing/fake-zero | [Execution quality](#12-execution-quality) |
| What threshold was used? | [Configuration verification](#21-configuration-and-threshold-verification) |
| Campaign/run lineage | [Canonical relationships](#7-canonical-relationships-and-joins) |
