# S5 isolated staging readiness

Current S6 preparation: [production cutover sheet](S6_PRODUCTION_CUTOVER.md).
Current status: S1–S5 are merged into `main` through PR #17 at
`e5f3cbd7c14c6d7fb430b59e5090b41a5bc02cc3`. S6 preparation is authorized;
only S6 production execution still requires separate authorization.

The S5 record below is historical. Its scope was `experiment/astra-simplified`,
PR #17, with accepted functional baseline `278f143ab3fa4ab24f5a543a11f8405fce051ebf`.

## Acceptance boundary

The owner attests to live expense, balance and statement Gemini acceptance,
MariBank 88-row statement import, investments, Owner UI, negative authentication,
device lifecycle, iPhone Shortcut and ES256 JWKS rotation/re-login. Manual sampling
was one person and one clear expense screenshot, plus the PDF. On 2026-09-20 the
owner explicitly declined further samples/second-user testing and accepted that
limit. These are user-attested results, not new S5 reruns. Exact Shortcut build/version
was not supplied; S5 does not change its API contract.

## S5 verification

- DB-01 / SEC-01 / SEC-02 / HIST-01: unchanged private schema, scoped commands,
  migration checksums, auth/device lifecycle, new Google OIDC boundary.
- CAP-01–05 / SPEND / SCHED-01–02 / STMT-01–02 / BAL / WEALTH / INV:
  retained S1–S4 integration, concurrency and browser suites protect accepted behavior.
- UI-01: catch-up precedes page reads; explicit failure/retry and current-through date.
- OPS-01: built-image import/probes, absent retired routes, AI-independent schedules,
  actual PostgreSQL dump/restore plus receipt/statement/occurrence replay fingerprints.

Local results and exact deployed digests/revisions are recorded in
[S5 acceptance](S5_ACCEPTANCE.md). Backend CI also runs generated OpenAPI drift,
unit, PostgreSQL integration, migration, concurrency, fallback Dashboard and container
restore smoke. Web CI runs build, unit/server, browser and real API/database acceptance.

## Retired runtime and replacement coverage

| Retired | Replacement invariant coverage |
|---|---|
| Reconciliation routes/engine/candidates/work queue | S3 statement atomic import, duplicate evidence, S2 saved metadata review |
| Ledger projection/account-state/billing/installment-plan runtime | S2 spending and monthly schedules; S3 dated observations/wealth |
| Old investment snapshot/P&L service | S4 investment estimates, explicit flow confirmation, pair invalidation |
| Old ingestion/transaction/snapshot repositories and root prototype entry point | Simplified S1–S4 services, receipts and concurrency suites |
| Old future_slices/legacy_regression tests and matching obsolete unit/UI tests | Current S1–S4 DB suites; image validation/Gemini transport tests; current browser/UI tests |
| Old Dashboard client reconciliation/correction/work-queue methods | Current request client plus current spending/balance/statement/investment controllers |

Retained intentionally: immutable old migration SQL in the repository (not executable
or shipped), Git history, the accepted Shortcut wire boundary, and the existing
Streamlit fallback with supported current-domain pages. Existing old hosted services
are preserved. No data reset, historical migration rewrite or new schema baseline.
