# VibeLedger Legacy Documentation Archive

> **LEGACY ARCHIVE ONLY / HISTORICAL REFERENCE**  
> 
> These documents describe historical and current legacy behavior from earlier iterations of VibeLedger.
> 
> **They DO NOT represent target architecture.**  
> **Do not implement new features from them.**  
> 
> For all target development, consult:
> 1. [`TARGET_DOMAIN_MODEL.md`](../../TARGET_DOMAIN_MODEL.md) (approved business and domain source of truth)
> 2. [`docs/architecture/README.md`](../architecture/README.md) (target architecture contracts, schemas, and plans)

---

## Documents in this Archive

1. [`development_doc.md`](./development_doc.md)  
   - Historical design document detailing the initial two-table (`accounts` + `transactions`) model, legacy single `/api/record` endpoint, `TABLE_SUFFIX` isolation, and early Streamlit prototype.
2. [`remediation_summary.md`](./remediation_summary.md)  
   - Remediation log for historical Issues 01–05 (idempotency, atomic balance update, cross-currency validation, investment adjustment typing, and credit card statement window calculations).

---

## Obsolete / Legacy Concepts Index

The following patterns from these legacy documents are explicitly obsolete in the target architecture:

| Legacy Concept | Why It Is Obsolete | Target Replacement |
|---|---|---|
| `accounts.current_balance` as source of truth | Mutable scalar loses dated evidence | Latest active dated balance observation; no account_state projection |
| Generic `adjustment` transaction type | Mixes unrelated meanings | Balance observations independent of spending; no balancing transactions |
| Investment adjustment mapped into income | Distorts spending/income reporting | Gain derived from observation pairs with explicitly complete capital-flow inputs |
| Future installment transactions created immediately | Confuses spending and debt | Full purchase-date spending once, total debt separately observed; no schedules |
| `TABLE_SUFFIX` for environment isolation | Pollutes table names and DDL | Independent database / schema configuration via `DATABASE_URL` / `DB_SCHEMA` |
| Startup `database.init_db()` DDL migration | Uncontrolled migration on app boot | Versioned standalone migration scripts |
| Dashboard direct PostgreSQL access | Bypasses business rules and duplicates logic | Dashboard consumes Backend REST APIs exclusively |
| Hard-coded account / category `Literal` in Python | Requires code deployment for configuration changes | Normalized database tables (`accounts`, `categories`, `account_aliases`) |
