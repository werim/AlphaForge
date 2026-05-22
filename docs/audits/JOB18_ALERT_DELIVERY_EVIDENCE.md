# Job 18 — Persisted Alert-Delivery Evidence for LIVE Readiness

## Verdict

Job 18 closes the alert-delivery evidence gap in the LIVE readiness evaluator without enabling LIVE execution.

## Implemented contract

- A diagnostic, non-trading probe may be issued through `WebhookAlertDeliveryEvidenceProvider`.
- A Telegram-specific probe may be issued through `TelegramAlertDeliveryEvidenceProvider` using Telegram Bot API `sendMessage` semantics.
- The generic webhook probe accepts only an HTTPS destination and requires a matching acknowledgement identifier before evidence is marked `COMPLETE`.
- The Telegram adapter accepts evidence only when Telegram reports `ok=true` with a positive `result.message_id`; this proves platform acceptance of the diagnostic message, not that a human read it.
- `capture_alert_delivery_evidence(...)` persists only sanitized evidence fields into `live_alert_delivery_evidence`.
- `LiveReadinessEvaluator` no longer trusts an optimistic alert-delivery flag supplied in an observability snapshot. It replaces that input with the persisted evidence result.
- Missing, incomplete, invalid, future-dated or older-than-15-minute persisted evidence causes the explicit `alert_delivery_evidence` check and observability coverage to fail closed.

## Execution safety boundary

This change does not modify signal scoring, RR evaluation, candidate selection, market scanning, order submission, cancellation, position handling, or runtime trade frequency. Qualification consumes stored evidence; it does not emit a diagnostic probe during startup.

## Telegram evidence workflow

1. Set `ALPHAFORGE_ENABLE_TELEGRAM=true` locally only when an explicit delivery test is intended.
2. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` locally; never commit their values.
3. Call `capture_telegram_alert_delivery_evidence_from_env(engine)` outside LIVE startup.
4. Confirm that a diagnostic Telegram message arrived and that persisted evidence reports `alert_delivery_verified=true`.
5. Run LIVE qualification only after all other evidence gates are independently proven.
6. Repeat evidence capture when the stored proof has aged beyond the enforced freshness window.

## Telegram diagnostic message

The adapter sends a clearly labelled non-trading diagnostic message containing a generated probe identifier and an explicit statement that no order was submitted. Telegram credentials and chat identifiers are used only for outbound delivery and are not persisted in the readiness evidence payload.

## Generic evidence workflow

1. Construct a diagnostic provider for the intended alert destination.
2. Run `capture_alert_delivery_evidence(engine, provider)` outside LIVE startup.
3. Inspect the stored evidence outcome.
4. Run LIVE qualification only after all other evidence gates are independently proven.
5. Repeat evidence capture when the stored proof has aged beyond the enforced freshness window.

## Persistence safety

Positive evidence is written through the provider-capture path. The stored payload is allowlisted and excludes destination query material and request authorization material. A stored acknowledgement is not permanent proof: it is accepted only during the configured default 15-minute freshness window.

## Remaining LIVE blockers

A passing alert-delivery evidence check is not a LIVE enablement decision. Incident-persistence proof, rollback proof, adapter/readiness controls and all other existing qualification checks remain independent blockers.
