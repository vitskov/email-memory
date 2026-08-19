# Memory Promotion Rulebook

This rulebook governs promotion, rejection, demotion, and editing behavior for email-derived long-term memory candidates.

## Core principle

The email archive may be broad.
Long-term memory must be sparse, durable, and revisable.

## Promotion rules

Promote only when the candidate is likely to remain useful beyond the originating email.
Prefer:
- durable user-specific facts
- stable project direction
- recurring collaborator relationships
- ongoing obligations and commitments
- meaningful preferences and workflows
- repeated patterns across messages or threads
- messages from academic journals regarding an existing submission or/end review that is due

Reject:
- one-off logistics unless they change future behavior
- generic announcements
- routine mailing-list noise
- low-signal fragments lacking stable future relevance
- duplicates and near-duplicates

## Demotion rules

Demote previously promoted memory when later evidence shows the memory should no longer be trusted as active long-term knowledge.
Use demotion when the prior memory is:
- contradicted by newer evidence
- no longer current in a meaningful way
- based on a false or misleading interpretation
- superseded so strongly that keeping it as active memory is wasteful

Demotion should preserve provenance and reason.

## Edit / revision rules

Edit previously promoted memory when the memory remains relevant but its wording or abstraction must change.
Use editing when:
- the original memory was too vague
- the original memory was too specific and should be generalized
- the original memory missed essential context revealed later
- the fact remains materially true but should be rewritten

Edits should preserve:
- original provenance link to the email
- reason for revision
- revised text

## Evidence rules

Every demotion or edit should be grounded in explicit new evidence.
Acceptable evidence sources include:
- newer emails
- thread summaries
- structured extracted decisions/deadlines
- later user correction
- higher-confidence synthesis from broader context

## Provider rules

LLM provider choice must be abstracted from the promotion pipeline.
Current provider policy:
- `hermes-default` may omit an explicit model but currently remains plan-compatible only in this repo
- `codex-cli` requires an explicit model and currently executes via `codex exec --model <model>`
- `claude-code-cli` requires an explicit model and currently executes via `claude --model <model>`

Provider outputs should return strict JSON with a top-level `results` array.

## Batching rules

Because mailbox-scale review is expensive, LLM usage must be batched deterministically.
Batches must:
- preserve candidate order
- respect a maximum candidate count
- respect a maximum input character budget
- allow sparse acceptance rates
- remain auditable after planning

## Soul-file rules

The soul file is the LLM-facing operational rules prompt.
It should be derived from this rulebook and remain conservative by default.
If customized, it should still preserve:

- sparse promotion
- explicit rejection of noise
- durable-memory preference
- allowance for later demotion and revision

## Execution-output rules

Execution adapters should request structured decisions for each candidate.
Each decision should minimally include:
- source identifier
- action: promote, reject, demote, or edit
- memory text
- rationale

## Operational rules

When functionality changes, update:
- `README.md`
- `docs/USAGE_MANUAL.md`
- `docs/ARCHITECTURE_OVERVIEW.md`
- this packaged rulebook: `src/email_memory_store/promotion/rulebooks/MEMORY_PROMOTION_RULEBOOK.md`
- the runtime-seeded copy under `<root>/config/promotion/rulebooks/MEMORY_PROMOTION_RULEBOOK.md`
