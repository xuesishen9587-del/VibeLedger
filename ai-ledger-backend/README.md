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

VibeLedger Backend provides the target REST API (`/api/v1/*`), health & readiness probes (`/health`, `/ready`), background statement processing, Gemini multimodal expense ingestion, and deterministic ledger accounting.

---

## Target FastAPI Application

The target backend application is implemented in `app/` and exposed via `app.main:app`:
- **API Version**: `v1` (`/api/v1/*`)
- **Probes**:
  - `GET /health` — Service identity and status (`vibeledger-api`)
  - `GET /ready` — Database connection, schema migration status, SHA256 checksum verification, and Gemini client status
- **Container Definition**: `Dockerfile.target` (`uvicorn app.main:app --host 0.0.0.0 --port 7860`)

> **Note on Legacy Runtime**: `Dockerfile` and `main.py` remain preserved for the legacy prototype (`/api/record`) until Phase 13 production fresh cutover.

---

## Deployment & Staging Runbook

For complete instructions on configuring, migrating, bootstrapping, and deploying the target backend in an isolated staging environment, refer to:
- [`docs/deployment/STAGING_DEPLOYMENT.md`](../docs/deployment/STAGING_DEPLOYMENT.md)

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
