---
title: Ai Ledger Backend
emoji: 📉
colorFrom: pink
colorTo: indigo
sdk: docker
pinned: false
license: mit
---

# VibeLedger Backend

Check out the configuration reference at https://huggingface.co/docs/hub/spaces-config-reference

---

## Current Status & Legacy Implementation

> **NOTICE**: The code currently in this directory represents the **legacy prototype implementation** (FastAPI with single `/api/record` endpoint, two-table schema, and startup `init_db()` DDL).
> 
> **Do not use this README or legacy codebase as the target business specification.**  
> Target architecture has been frozen and will be implemented incrementally per the roadmap.

---

## Target Architecture References

For target business rules, schemas, API contracts, and implementation sequencing, refer to:
- [`TARGET_DOMAIN_MODEL.md`](../TARGET_DOMAIN_MODEL.md) — Approved business & domain truth
- [`docs/architecture/PHYSICAL_SCHEMA.md`](../docs/architecture/PHYSICAL_SCHEMA.md) — Target PostgreSQL persistence contract
- [`docs/architecture/API_CONTRACT.md`](../docs/architecture/API_CONTRACT.md) — Target `/api/v1` REST API contract
- [`docs/architecture/RECONCILIATION_ENGINE.md`](../docs/architecture/RECONCILIATION_ENGINE.md) — Target reconciliation and matching engine
- [`docs/architecture/IMPLEMENTATION_PLAN.md`](../docs/architecture/IMPLEMENTATION_PLAN.md) — Implementation roadmap
- [`docs/architecture/TEST_PLAN.md`](../docs/architecture/TEST_PLAN.md) — Target verification & test plan

For historical prototype documentation, see [`docs/legacy/`](../docs/legacy/README.md).
