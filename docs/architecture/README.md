# VibeLedger architecture

The **2026-09-05 simplified target** replaces the previous Phase 12.5 design.
Implementation has not yet been changed to match it. The accepted staging backend,
REST Dashboard and real iPhone expense experience provide the implementation base.

There are three canonical documents, each with one responsibility:

1. [TARGET_DOMAIN_MODEL](../../TARGET_DOMAIN_MODEL.md): user workflows, product rules,
   reporting meaning, simplifications and deliberate tradeoffs.
2. [CONTRACTS](CONTRACTS.md): schema, invariants, APIs, capture recovery, concurrency,
   AI/auth boundaries and runtime shape.
3. [IMPLEMENTATION_PLAN](IMPLEMENTATION_PLAN.md): repository assessment, keep/refactor/
   remove map, implementation slices S0–S6 and executable acceptance expectations.

Read them in that order. Product rules govern implementation meaning; CONTRACTS
makes them concrete; the plan orders delivery. Resolve contradictions by updating
these documents together, not by adding another parallel “freeze” specification.
[PROJECT_CONTEXT](../../PROJECT_CONTEXT.md) is the short status handoff, not another
domain contract. [The staging runbook](../deployment/STAGING_DEPLOYMENT.md) records
the previous accepted deployment and is not yet the simplified deployment procedure.

The core model is independent spending records and dated account balance observations.
Investment gains require explicitly known capital flows. There is no projected
account balance, statement/reconciliation engine, installment schedule, or monthly close.
Keep FastAPI, Streamlit, PostgreSQL, device idempotency, Decimal math and household auth.

## Documentation consolidation

| Previous document | Disposition |
|---|---|
| TARGET_DOMAIN_MODEL.md | Rewritten around household workflows and explicit reporting limits |
| PHYSICAL_SCHEMA.md and API_CONTRACT.md | Replaced by CONTRACTS.md so fields and behavior share one technical contract |
| RECONCILIATION_ENGINE.md | Removed; the target has no reconciliation engine |
| TEST_PLAN.md | Consolidated into IMPLEMENTATION_PLAN.md with slice exits and one acceptance matrix |
| IMPLEMENTATION_PLAN.md | Rewritten; old phase hierarchy replaced by bounded delivery slices |
| Root README.md and PROJECT_CONTEXT.md | Corrected to distinguish existing implementation, historical acceptance and pending simplified target |

The old architecture remains available in Git at `3ac0ed6` (before this rewrite),
for example with `git show 3ac0ed6:docs/architecture/API_CONTRACT.md`. It is historical
evidence, not an implementation dependency. Do not copy it into another active
specification directory. Comments in immutable old migrations retain their original
document references to preserve migration checksums.

Older [prototype documents](../legacy/README.md) remain historical only. No production
deployment, database migration, or runtime behavior change is performed by this review.
