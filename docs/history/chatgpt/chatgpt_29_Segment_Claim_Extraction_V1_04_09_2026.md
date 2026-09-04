# MIP — Segment Claim Extraction v1

Date: 2026-09-04

## Status

PASS — additive segment-aware claim-extraction vertical slice is verified
through claim embeddings and relation-context compatibility.

This checkpoint does NOT enable segment processing in the live scheduler and
does NOT unblock relation production.

## Schema decision

Migration `041_add_segment_claim_scope.sql` extends the existing
`claim_extraction_runs` model instead of introducing parallel segment-specific
claim tables.

Contract:

- whole-content run:
  - `content_id` = canonical parent content;
  - `segment_id IS NULL`.
- segment run:
  - `content_id` remains parent provenance;
  - `segment_id` identifies `content_segments.segment_id`.
- claims remain in the existing `claims` table.
- evidence offsets are relative to the evidence selected by run scope:
  - whole-content -> `content_items.text_content`;
  - segment -> `content_segments.text_content`.

The old whole-content uniqueness constraint was replaced by two partial unique
indexes so content-level and segment-level attempt sequences cannot collide.

Migration was transactionally dry-run first and then applied successfully.
Existing historical claim runs remained whole-content runs.

## Whole-content compatibility

Commit `ecd5168` makes the existing live whole-content claim worker explicitly
ignore segment runs in terminal/attempt history:

`r.segment_id IS NULL`

This preserves the previous whole-content retry/idempotency semantics after the
schema extension.

## Segment claim worker

Commit `32a4efa` adds:

`experiments/segment_claim_extraction/segment_claim_extract_worker.py`

v1 characteristics:

- point-run only via `--segment-id`;
- routing version 1;
- eligible routing decisions: `analyze|maybe`;
- reuses existing Mamay model registry;
- reuses claim-extractor prompt v2;
- reuses existing deterministic offset resolver and validator;
- no scheduler;
- no backlog scan;
- no parallel Mamay inference;
- valid run + claims persisted atomically;
- terminal valid/invalid run makes the same segment identity idempotent.

## Live vertical-slice measurement

First live pilot used one routed `analyze` segment with 636 characters.

Result:

- status: `valid`;
- claims: 8;
- latency: 65,690 ms;
- finish_reason: `stop`;
- completion_tokens: 977;
- markdown fence stripped: yes;
- deterministic offsets resolved: 8;
- offset not_found: 0;
- offset ambiguous: 0;
- persisted claims: 8/8;
- evidence grounding against segment text: 8/8.

Immediate rerun returned `SKIP`.

Persistence remained:

- runs: 1;
- valid runs: 1;
- recorded claims: 8;
- persisted claims: 8.

## Relation-context compatibility

Before this change the relation worker always constructed claim context from
`content_items.text_content`.

That was invalid for segment claims because their offsets are relative to
`content_segments.text_content`.

Commit `7a8d65a` changes evidence resolution:

- `segment_id IS NULL` -> content text;
- `segment_id IS NOT NULL` -> segment text.

Read-only regression verification used one historical whole-content claim and
one new segment claim.

Both resolved contexts contained their exact persisted `evidence_span`.

Result: PASS for both scopes.

## Claim embeddings

The existing claim embedding worker needs no segment-specific fork because its
input contract is only:

`claims.claim_text -> embedding`

The 8 pilot segment claims were embedded using the existing BGE-M3 registry.

Measured coverage:

- target claims: 8;
- target embeddings: 8;
- coverage: 8/8.

## Candidate-space compatibility

A read-only targeted scan compared the 8 segment claims against the current
claim embedding corpus using the same cross-contour eligibility rule as the
existing candidate generator.

Measured corpus:

- claim embeddings: 29,920;
- segment target claims: 8;
- eligible external claims per target: 21,112;
- same-parent survivors: 0.

Best cosine scores across the 8 targets ranged approximately from 0.593 to
0.919.

Conclusion:

- parent `content_id` provenance remains compatible with existing contour
  resolution;
- segment claims do not leak into same-parent relation candidates under the
  current cross-contour rule;
- no segment-specific candidate schema or generator fork is required.

The targeted scan was diagnostic only. Its results MUST NOT be persisted as
`candidate_version=1`, because that version represents the existing
full-corpus/global-ranking contract.

## Existing relation-stage constraint

The current candidate generator remains a separate pre-existing scalability
problem: it computes a full corpus similarity matrix and iterates all unordered
pairs.

Segment claims do not materially create this problem; they only enter the
existing corpus contract.

Relation production therefore remains blocked by the already-known candidate
scaling and semantic-calibration work. This checkpoint does not reopen or
override those decisions.

## Current accepted path

Verified additive path:

`occurrence_content`
→ `content_segments`
→ `segment_embeddings`
→ `segment_routing_decisions`
→ segment claim extraction
→ existing `claims`
→ existing `claim_embeddings`
→ relation-compatible claim context

`segment != event` remains an invariant.

## Next engineering junction

The vertical slice is complete.

Before scheduling the new path, define the minimal bounded live-processing
policy for:

- occurrence full-text extraction;
- segmentation;
- segment embeddings;
- segment routing;
- segment claim extraction.

In particular, segment claim live eligibility (`analyze` only versus
`analyze|maybe`) must be an explicit operational decision rather than inherited
accidentally from the point-run pilot.

Relation production remains outside that next step.
