# #622 — Canonical universe evidence storage contract

## Problem and source measurements

On 2026-10-08 the #344 dynamic-universe PAPER campaign at SHA `034b643e`
grew from 303.6 MiB main + 7.7 MiB WAL to 494.7 MiB main + 7.7 MiB WAL
during early runtime. SQLite `dbstat` attributed 241.6 MiB to
`universe_selection_candidates` and 208.9 MiB to
`universe_selection_cycles` at the second measurement. These are **two
representations of the same per-cycle candidate evidence**, not proof of
an additional 208.9 MiB of unique decisions.

The host had approximately 127 GiB free, but the canonical #620 per-database
high watermark remains 1 GiB. Increasing this threshold, deleting protected
history, or changing trading gates to silence storage warnings is not a fix.

## Storage versions

The canonical economic schema, evidence hash, cycle ID, and candidate selection
algorithm are **unchanged**.

- **Legacy:** `universe_selection_cycles.payload_json` stores the full
  `SelectedUniverse.as_dict()`, including candidates. Existing persisted rows
  remain untouched; exact legacy replays are recognized.
- **candidate-rows-v2:** the same `payload_json` stores versioned decision-time
  metadata, constraints, hashes, source provenance, and selected symbols, **but
  not the duplicate candidates array**. Full candidate evidence remains in
  immutable `universe_selection_candidates` rows under the identical
  `cycle_id`, ordered by `candidate_index`.
- Both versions use `load_persisted_universe_selection(conn, cycle_id)` to
  reconstruct the exact canonical `SelectedUniverse.as_dict()` structure.
  The loader recomputes the original SHA-256 evidence hash, universe/config
  hashes, cycle ID and selected-symbol projection; checks all candidate rows
  and provenance. Unknown formats and corrupt or missing evidence fail closed.
  It does not infer candidates from another scan or a later cycle.

No in-place migration, row replacement, soft fallback, or alternate economic
authority is permitted. The `_storage_codec` marker is a representation
version, **not** an economic schema-version bump.

## Replay and archival invariants

Each new cycle and candidate record remains immutable. Insert-on-conflict is
accepted only after the original exact payload and candidate rows are compared.
Historical full-payload records remain readable and are not rewritten.

For new compact records, #620's archive eligibility additionally invokes the
full canonical decoder before verified backup publication. `json_valid` and
candidate row counts alone are insufficient to prove an uncompromised payload.
Storage-pressure recovery must still fail closed and preserve reconciliation
and position management.

## Qualification is not yet demonstrated

Removing duplicated cycle payloads cuts one large source of disk amplification,
but each **distinct scan** still produces a new immutable set of candidate
rows. This patch is **not proof** that repeated 300-candidate scans over 3 or
7 days will remain below 1 GiB. Qualification requires a measured
write-amplification profile on an exact merged SHA/config, not extrapolation
from an unrelated database or arbitrary changes to the storage watermark.

Before a new #344 campaign: verify exact SHA, run protected regression/CI,
measure per-scan bytes for cycles/candidates/indexes/WAL, compare scan cadence
against duration, and demonstrate a safe sustained storage plan if the budget
still fails. The paused pre-#622 campaign is diagnostic evidence only; do not
resume it as qualification for the new code.
