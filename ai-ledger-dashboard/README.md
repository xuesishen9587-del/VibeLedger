---
title: Ai Ledger Dashboard
emoji: 🚀
colorFrom: red
colorTo: red
sdk: streamlit
tags:
- streamlit
pinned: false
short_description: Streamlit ledger dashboard
license: mit
app_file: app.py
---

# VibeLedger Dashboard

---

## Current Status & Legacy Implementation

> **NOTICE**: The code currently in this directory represents the **legacy prototype implementation**.
> 
> **Important Architectural Note**:
> - Current Dashboard directly accesses PostgreSQL via psycopg2. **This is legacy behavior.**
> - In the target architecture, Dashboard will consume Backend REST APIs exclusively (`/api/v1/*`) and hold no direct database credentials.
> 
> **Do not use this README or legacy codebase as the target business specification.**

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