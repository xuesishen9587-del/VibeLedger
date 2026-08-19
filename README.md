# VibeLedger

> **AI-Powered Two-Person Household Ledger & Multi-Modal Asset Management**

---

## 1. About VibeLedger

VibeLedger is a modern, lightweight household financial ledger designed for a two-person household. 

- **Daily Bookkeeping**: Instant expense capture via iPhone Shortcuts using multi-modal AI (Gemini) structured recognition.
- **Authoritative Calibration**: Periodic reconciliation via bank/credit card Statement PDFs and Account Snapshots to eliminate ledger drift.
- **Household Focus**: Holistic family assets, liabilities, cash flow, and investment valuations without complex couple AA/settlement overhead.

---

## 2. Current Project Status

```text
Current Stage:
Target architecture is frozen.
Implementation has not started.
Repository is preparing for Implementation Phase 0/1.
```

- The code currently located in `ai-ledger-backend/` and `ai-ledger-dashboard/` represents a **legacy prototype**.
- The **target architecture is fully specified and frozen** in `TARGET_DOMAIN_MODEL.md` and `docs/architecture/*`.
- **Target development will proceed via incremental implementation phases** without mutating or migrating legacy production data.

---

## 3. Authoritative Documentation Reading Order

Future developers and AI Agents MUST consult documentation in this strict hierarchy:

1. [`TARGET_DOMAIN_MODEL.md`](./TARGET_DOMAIN_MODEL.md) — **Approved business & domain source of truth** (Principles, entities, rules, non-goals).
2. [`docs/architecture/PHYSICAL_SCHEMA.md`](./docs/architecture/PHYSICAL_SCHEMA.md) — **Target PostgreSQL persistence contract** (Tables, constraints, types, projections, audit log).
3. [`docs/architecture/API_CONTRACT.md`](./docs/architecture/API_CONTRACT.md) — **Target external REST API contract** (Endpoints, request/response models, error codes).
4. [`docs/architecture/RECONCILIATION_ENGINE.md`](./docs/architecture/RECONCILIATION_ENGINE.md) — **Target reconciliation & matching engine contract** (Scoring, fuzzy matching, replay safety).
5. [`docs/architecture/IMPLEMENTATION_PLAN.md`](./docs/architecture/IMPLEMENTATION_PLAN.md) — **Implementation roadmap and phase sequencing** (Phase 0 through Phase 14).
6. [`docs/architecture/TEST_PLAN.md`](./docs/architecture/TEST_PLAN.md) — **Verification contract & test matrix** (Determinism, safety invariants, regression suites).
7. [`PROJECT_CONTEXT.md`](./PROJECT_CONTEXT.md) — **Concise current-state index & Agent handoff guide**.
8. [`docs/legacy/*`](./docs/legacy/README.md) — **Historical archive reference only** (Describes legacy prototype behavior; not for new development).

For an overview of the architecture documents and terminology, see [`docs/architecture/README.md`](./docs/architecture/README.md).

---

## 4. Repository Structure

```text
.
├── README.md                      # Repository overview and quick start (this file)
├── PROJECT_CONTEXT.md             # Concise current Agent handoff & state index
├── TARGET_DOMAIN_MODEL.md         # Authoritative business domain specification
├── docs/
│   ├── architecture/              # Target architecture contracts (Frozen)
│   │   ├── README.md              # Architecture index, authority hierarchy, glossary
│   │   ├── PHYSICAL_SCHEMA.md     # PostgreSQL persistence specification
│   │   ├── API_CONTRACT.md        # Public HTTP API contract
│   │   ├── RECONCILIATION_ENGINE.md # Statement matching & reconciliation logic
│   │   ├── IMPLEMENTATION_PLAN.md # Phase-by-phase implementation sequencing
│   │   └── TEST_PLAN.md           # Automated test plan & regression invariants
│   └── legacy/                    # Archived historical documentation
│       ├── README.md              # Legacy archive index and obsolete concept mapping
│       ├── development_doc.md     # Early two-table prototype design document
│       └── remediation_summary.md # Issues 01-05 legacy remediation notes
├── ai-ledger-backend/             # FastAPI backend (currently legacy prototype)
└── ai-ledger-dashboard/           # Streamlit dashboard (currently legacy prototype)
```

---

## 5. Next Steps

- **Current Milestone**: Documentation Cleanup (Complete).
- **Next Phase**: **Implementation Phase 0 (Architecture Freeze & Test Safety)** followed by **Implementation Phase 1 (Database Foundation)** as outlined in [`docs/architecture/IMPLEMENTATION_PLAN.md`](./docs/architecture/IMPLEMENTATION_PLAN.md).
