# Lifecycle Rules

Lifecycle accuracy is mandatory.

Expected lifecycle flow:
SIGNAL_CREATED
→ SIGNAL_VALIDATED
→ SIGNAL_REJECTED | WAITING_ENTRY_ZONE
→ ENTRY_TRIGGERED
→ ORDER_PLACED
→ PARTIAL_FILL
→ FILLED
→ TP_HIT / SL_HIT / CANCELLED

Do not:
- skip lifecycle states
- collapse lifecycle into CREATED
- force trades without validation
- hide rejected decisions
