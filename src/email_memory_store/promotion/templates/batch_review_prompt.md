# Batch Review Prompt Template

This runtime-local template can be customized for future batch-review prompt generation.

Default intent:
- compare candidates within the batch
- promote sparsely
- preserve durable-memory bias
- keep output structured and audit-friendly

If future code uses this template directly, prefer loading the runtime-seeded copy under:
- `<root>/config/promotion/templates/batch_review_prompt.md`