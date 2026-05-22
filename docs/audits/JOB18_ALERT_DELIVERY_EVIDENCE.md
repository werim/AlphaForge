# Job 18 — Persisted Alert-Delivery Evidence for LIVE Readiness

## Verdict

Job 18 closes the alert-delivery evidence gap in the LIVE readiness evaluator without enabling LIVE execution.

## Implemented contract

- A diagnostic, non-trading probe may be issued through `WebhookAlertDeliveryEvidenceProvider`.
- The probe accepts only an HTTPS destination and requires a matching acknowledgement identifier before evidence is marked `COMPLETE`.
- `capture_alert_delivery_evidence(...)` persists only sanitized evidence fields into `live_alert_delivery_evidence`.
- `LiveReadinessEvaluator` no longer trusts an optimistic alert-delivery flag supplied in an observability snapshot. It replaces that input with the persisted evidence result.
- Missing, incomplete, invalid, future-dated or older-than-15-minute persisted evidence causes the explicit `alert_delivery_evidence` check and observability coverage to fail closed.

## Execution safety boundary

This change does not modify signal scoring, RR evaluation, candidate selection, market scanning, order submission, cancellation, position handling, or runtime trade frequency. Qualification consumes stored evidence; it does not emit a diagnostic probe during startup.

## Evidence workflow

1. Construct a diagnostic provider for the intended alert destination.
2. Run `capture_alert_delivery_evidence(engine, provider)` outside LIVE startup.
3. Inspect the stored evidence outcome.
4. Run LIVE qualification only after all other evidence gates are independently proven.
5. Repeat evidence capture when the stored proof has aged beyond the enforced freshness window.

## Persistence safety

Positive evidence is written through the provider-capture path. The stored payload is allowlisted and excludes destination query material and request authorization material. A stored acknowledgement is not permanent proof: it is accepted only during the configured default 15-minute freshness window.

## Remaining LIVE blockers

A passing alert-delivery evidence check is not a LIVE enablement decision. Incident-persistence proof, rollback proof, adapter/readiness controls and all other existing qualification checks remain independent blockers.
