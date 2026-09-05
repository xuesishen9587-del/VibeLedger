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

VibeLedger Backend implements `/api/v1/*`, `/health`, `/ready`, Gemini expense capture,
and the previous architecture's ledger, synchronous statement/reconciliation and
reporting workflows. The simplified architecture specified on 2026-09-05 is pending
implementation; it retains the expense interface and replaces those ledger workflows.

---

## Target FastAPI Application

The target backend application is implemented in `app/` and exposed via `app.main:app`:
- **API Version**: `v1` (`/api/v1/*`)
- **Probes**:
  - `GET /health` — Service identity and status (`vibeledger-api`)
  - `GET /ready` — Database connection, schema migration status, SHA256 checksum verification, and Gemini client status
- **Container Definition**: `Dockerfile.target` (`uvicorn app.main:app --host 0.0.0.0 --port 7860`)

> **Legacy runtime**: `Dockerfile` and root `main.py` serve the old `/api/record`
> prototype. The implemented staging application is `app.main:app` using
> `Dockerfile.target`. Do not confuse either current implementation with the pending
> simplified schema. Removal/cutover follows S0–S6 in the current implementation plan.

---

## Deployment & Staging Runbook

For the previous accepted staging setup and its historical runtime evidence, refer to
the following runbook. Update it during simplified implementation before using it
for the new schema; do not run it as a simplified deployment procedure:
- [`docs/deployment/STAGING_DEPLOYMENT.md`](../docs/deployment/STAGING_DEPLOYMENT.md)

---

## Target Architecture References

For target business rules, schemas, API contracts, and implementation sequencing, refer to:
- [`TARGET_DOMAIN_MODEL.md`](../TARGET_DOMAIN_MODEL.md) — Household workflows and reporting meaning
- [`CONTRACTS.md`](../docs/architecture/CONTRACTS.md) — Simplified database, APIs, retry, auth and runtime contract
- [`IMPLEMENTATION_PLAN.md`](../docs/architecture/IMPLEMENTATION_PLAN.md) — Code assessment, transition slices and acceptance matrix

For historical prototype documentation, see [`docs/legacy/`](../docs/legacy/README.md).
