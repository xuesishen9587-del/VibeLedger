# Transition and acceptance

Status: **Implementation-ready plan, 2026-09-05. No deployment is authorized by this
document alone.** The architecture review changes documentation only. Production
fresh cutover remains future work after the household accepts the simplified system.

Read [product rules](../../TARGET_DOMAIN_MODEL.md) and [CONTRACTS](CONTRACTS.md) first.
This plan replaces the previous Phase 0–14 and Phase 12.5 implementation/test plans.
Historical acceptance establishes a useful baseline; it does not certify the new design.

## 1. What was reviewed and what exists

Baseline inspected: `refactor/astra-simplify-architecture`, commit `3ac0ed6`.
The user's handoff says the local checkout is current, staging runtime and the
real-device Expense Shortcut have passed, and production fresh cutover has not happened.
This review did not independently rerun those deployed acceptances.

| Evidence in the checkout | Finding and architecture consequence |
|---|---|
| `ai-ledger-backend/app/main.py`, `app/api/routes/`, `app/services/`, `app/repositories/` | A substantial FastAPI application exists with expense, statement, reconciliation, credit, installment, investment, correction, auth and reporting routes. Root documents calling it an unbuilt prototype were stale. |
| `migrations/0001_*.sql` through `0009_*.sql` | 20 application tables plus migration metadata; observed balances coexist with mutable account_state, seven transaction types, links, two snapshot tables, stored P&L, installment schedules and reconciliation evidence/candidates. |
| `app/services/expense_service.py`, `gemini_service.py`, routes `expenses.py`/`ingestion.py` | Useful image validation, device/key receipts, saved responses, 0.85 field gates, confirm/revise/reject, and typed Gemini transport. Expense writes also invoke the ledger, FX settlement and installment plans; the route holds a transaction around extraction. Keep the interface, simplify internals and shorten locks. |
| `app/services/ledger_service.py`, `snapshot_service.py` | Expenses and adjustments mutate projections; snapshots anchor historical reconstruction and drive reconciliation. Those dependencies disappear when observed wealth and captured spending are independent. |
| `app/services/statement_service.py`, `reconciliation_service.py`, `app/domain/reconciliation/` | Matching, candidate resolution, residual thresholds, transfer/refund/settlement and installment handling serve a much broader completeness promise than the household needs. Remove the whole active path rather than hiding it behind the Dashboard. |
| `app/services/investment_service.py`, `repositories/investments.py` | Investment snapshots, inferred committed transfer totals and persisted period gains share reconciliation/projection responsibilities. Retain Decimal formula knowledge; require explicit complete flow inputs and calculate gains from valid pairs. |
| `app/services/dashboard_service.py` | Wealth reads ledger_balance and spending can use settlement legs. Rewrite report sources; preserve the useful separation of gains from household income. |
| `ai-ledger-dashboard/app.py`, `api_client.py`, `dashboard_controller.py` | Dashboard already uses REST, not direct SQL. Eight navigation destinations and extensive candidate/correction UI expose engine structure. Retain framework/client/error handling and replace with four user pages. |
| `app/auth/`, `api/deps.py`, `tests/integration/test_household_authorization_db.py` | Existing device and browser auth boundaries are valuable. Ingestion actions currently require device auth, even though Dashboard has browser-auth review UI; the new contract makes browser household review explicit. |
| `app/config.py`, `app/auth/browser_verifier.py` | AUTH_JWKS_URL exists but current verifier uses configured static key. Consumer login and JWKS verification are implementation work, not a capability to assume. |
| `migrations/runner.py`, `scripts/bootstrap_staging.py`, Dockerfiles, deployment runbook | Checksummed migrations, Supabase extension handling, explicit schemas, probes and deployment structure are reusable. Existing bootstrap assumes account_state and needs replacement. |
| Unit/API/DB/concurrency tests, `.github/workflows/backend-ci.yml`, Dashboard tests | Useful safety and persistence test infrastructure exists. Current CI runs backend unit, PostgreSQL, migration and concurrency groups; Dashboard tests need to become a required CI job. |
| Prior TARGET_DOMAIN_MODEL and six architecture documents | Detailed Phase 12.5 account-risk/category/asset-capture contracts were design work. Risk, descriptions and multi-account capture are not implemented by migrations 0001–0009: account institution still exists; risk/description and asset-capture route are absent. Do not apply the old proposed 0010 as a prerequisite. |

### Retain / change / remove map

Paths below are relative to `ai-ledger-backend/` unless prefixed otherwise.

| Component | Action |
|---|---|
| `app/main.py`, config/db helpers, dependency/error wiring | Retain and reduce registered routes; readiness must recognize the new migration lineage. |
| `app/auth/`, identity repositories, device routes | Retain, extend browser login verification, keep household/device authorization regressions. |
| `app/domain/money.py` | Retain Decimal foundation; add finite/bounds and explicit supported-minor-unit validation, fix currency-specific formatting where needed. |
| `app/services/gemini_service.py` | Retain static-schema adapter; pass category descriptions and account scopes; add intent guards and balance extraction. Make model/version explicit config; verify actual SDK/model compatibility in staging. |
| `app/services/expense_service.py`, ingestion repository/routes | Refactor around short durable receipts and simple transactions. Retain request wire fields, display_summary, plain-text key recovery and natural-language correction. |
| `app/services/transaction_service.py`, transactions repository/routes | Keep CRUD concepts; replace balance compensation and preview/commit with one version-checked mutation and audit. |
| Account/category repositories/routes | Retain CRUD/alias/auth patterns. Add scope/risk/fallback/lifetimes; remove billing/institution/linkage constraints. |
| `snapshot_service.py`, `investment_service.py`, snapshots repository | Replace with small balance and flow-input use cases. Reuse validation, not projection/reconciliation orchestration. |
| `dashboard_service.py`, reference FX adapter | Refactor to observation and original-expense queries, freshness/completeness and dated reference quotes. |
| `audit.py` repository | Retain insert-only history pattern; reduce actions/metadata and scope reads to record detail. |
| `statement_service.py`, `statement_parser.py`, `reconciliation_service.py`, `app/domain/reconciliation/` | Remove from target imports/routes/package/tests when replacement slice is complete. No statement background path or PDF dependencies. |
| `ledger_service.py`, legacy transaction effect functions | Remove once transaction CRUD no longer calls them. Do not retain a no-op projection engine. |
| Credit-card/installment services, repositories, routes and domain schedules | Remove; credit becomes a signed balance and installments a purchase-date expense. |
| `work_queue_service.py`, work queue repository/route | Replace with a simple scoped ingestion-request list; no new work table/service. |
| `investment_pnl_periods`, `account_state`, `credit_card_snapshots`, `transaction_links`, installment/reconciliation/statement tables | Omit from fresh target schema. Do not keep dual financial truths. |
| `ai-ledger-dashboard/app.py`, `api_client.py`, `time_utils.py` | Retain Streamlit/REST foundation; split pages into small modules while replacing their behavior. |
| `ai-ledger-dashboard/dashboard_controller.py` | Remove candidate-formatting/reconciliation helpers; no reason to port them to the new Review page. |
| Backend root `main.py`, `database.py`, `db_migration.py`, legacy `Dockerfile`; Dashboard `src/streamlit_app.py` | Legacy runtime entry points; exclude from simplified image, remove from active tree after replacement acceptance. Git preserves history. |
| `test_client.py`, `test_idempotency.py`, backend root legacy tests | Never run against inherited remote credentials. Retire with legacy runtime; do not count them as target tests. |

The existing untracked `ai-ledger-backend/cloudbuild.phase12.yaml` is user workspace
content. The architecture review leaves it untouched. A future implementer must
inspect ownership/purpose before modifying or adopting it.

## 2. Transition strategy: fresh schema and isolated staging

There is no historical-data migration requirement. Use a **fresh simplified schema**
and separate staging service revisions/URLs. Do not write a destructive upgrade of
the accepted staging schema or a migration from legacy production financial data.
Keep the existing accepted staging service available as the baseline until the new
end-to-end slice passes. No dual write, shadow ledger, data synchronization or
long-lived feature-flag matrix.

Preserve applied migration files byte-for-byte. Add the simplified baseline under
`migrations/simplified/0001_simplified.sql`, then ordinary additive migrations there.
Make the simplified runtime/runner explicitly select that lineage; do not glob both
directories. It must reject a schema containing the previous migration lineage,
including on readiness. A test-only migration-directory parameter can support
building the new baseline while existing code is still present; the final runtime
has one selected lineage, not two interchangeable financial models.

The old root migration files can remain historical, unused by the simplified runner;
their old architecture-path comments describe their original contract and must not
be edited to fix links, because doing so breaks checksum acceptance. This does not
make their schema the new target. A separate explicit operator command bootstraps
the fresh schema's household/users/categories/accounts. No startup DDL, balances
defaulted to zero, or opening-balance transactions.

Each delivery below is a bounded implementation/review unit. The accepted staging
service remains unchanged while these local slices are assembled; intermediate
commits need not expose a half-working Dashboard on its URL. For GPT-5/Antigravity,
give the agent the three canonical documents plus the named slice and acceptance
IDs. Ask it to implement, test, and report results for that slice, not reinterpret
the entire architecture. Review against product invariants, not the old test count.

### S0 — Freeze the working boundary and runnable baseline

* Capture sanitized current request/response fixtures for high-confidence expense,
  uncertain draft, confirm, natural-language/structured revise, reject and by-key replay.
* Document the actual Shortcut export/actions and which fields it reads. The repository
  contains backend contract code and device-storage instructions, but not a complete
  versioned Shortcut export. Obtain/export it during device acceptance; do not invent
  an export or claim its behavior from the old plan alone.
* Record the current deployed image/revision, migration checksums and current local
  baseline test result without credentials. Record baseline failures separately.
* Freeze tests for the one-request normal path and unknown-outcome recovery, including
  a delayed original POST after GET returned 404. Add the cancellation behavior as
  an explicit target fixture, not as a false claim about the existing Shortcut.

Exit: accepted one-off wire fixtures and baseline are reviewable; no runtime change.

### S1 — Fresh database, identities and settings

* Add the 13-table baseline and strict lineage selection, migrations/readiness tests,
  least-privilege runtime role setup and explicit idempotent seed script.
* Retain users/membership/devices; implement account/category scope, risk, fallback,
  lifetime and audit constraints. Seed 15 expense categories and 3 income categories
  with the descriptions in TARGET_DOMAIN_MODEL.
* Add request receipt infrastructure for device/browser scopes, version checks and
  short household-scoped write lock. Avoid exposing it as a generic product workflow.

Exit: DB-01, SEC-01, HIST-01 and account/category portions of UI-01 pass locally.
Do not modify accepted staging/production identities or reuse device tokens across schemas.

### S2 — Spending capture, recovery and edits

* Retain the expense endpoint body/normal result, simplify its financial write, keep
  natural-language revisions and deterministic validation.
* Add durable reserve/extract/finalize/cancel behavior; unknown account/category are
  nonblocking metadata where the purchase is clear. Add non-expense intent guards.
* Implement manual transactions, linked/unlinked refunds, one-step edit/void, frozen
  original-currency reporting conversion and its missing-rate path.
* Replace installment plans with full purchase spending; remove ledger calls and
  from/to settlement legs from this use case.

Exit: CAP-01 through CAP-05, SPEND-01, FX-01, SEC-01 and HIST-01 pass. A real-device
smoke test on a new isolated service may begin, but does not replace final acceptance.

### S3 — Balance updates, wealth and risk

* Implement the single snapshot path, manual multi-account Save, correction/void and
  version/head checks; no reconciliation batch is created.
* Implement screenshot balances including credit debts, row selection, overlap/total
  validation and explicit unknown rows. Add all-or-nothing capture confirmation.
* Replace overview/freshness reads with last observations, account lifetimes and
  honest completeness; implement risk and step history queries.

Exit: BAL-01 through BAL-05 and WEALTH-01 pass, independent of spending completeness.

### S4 — Investment inputs and complete Dashboard

* Implement explicit interval flow inputs, derived gains, invalidation on changed
  snapshot pairs, date-range coverage and native-currency reports.
* Replace eight Dashboard destinations with Wealth/Spending/Review/Settings. Put
  investments in Wealth, history in record detail, and draft review in one form/table.
* Implement two-user Supabase Auth login/refresh/logout and pinned JWKS verification;
  keep device provisioning/revocation usable. Remove token-pasting from daily UI.
* Add automatic bounded session refresh of missing FX and explicit retry display.
  Never show unknown data as zero or label net captured income as household savings.

Exit: INV-01, UI-01, SEC-02, full report fixtures and Dashboard tests pass.

### S5 — Remove superseded runtime, accept staging

* Remove old routes/imports/dependencies and obsolete test suites only alongside
  replacement invariant coverage. Exclude legacy source from images. Trim config,
  error enums, serializers and API client methods that existed only for removed paths.
* Make one target entry point/image per service; update runbooks and CI. No active
  code references account_state or reconciliation/statement/installment persistence.
* Deploy only to the explicitly selected isolated staging environment as a separate
  authorized implementation action. Run the acceptance below on the exact image
  digests/schema/Shortcut version; collect signed-off evidence from both household users.

Exit: all acceptance IDs pass, no known critical financial-integrity/auth/recovery
failure, current architecture matches generated OpenAPI/migrations, and both people
can complete the household flows without developer assistance.

### S6 — Later, separately authorized production fresh cutover

Prepare a reviewable cutover sheet with exact images/versions, fresh DB/schema,
secrets configuration, two user identities, devices, starter accounts and rollback
procedure. Production is not part of this architecture task.

Before switching: export/back up existing environments, verify restore to an isolated
schema, drain/recover pending keys on the old endpoint, then change endpoint/token
together per device. Old keys must not be replayed into the new schema. Introduce
opening observations by normal balance updates; don't import legacy transactions.
Check each configured asset and liability, including non-overlap and full card debt.

Rollback before new financial writes can simply return clients to the previous
accepted environment. After new writes, preserve the new database/receipts, stop
capture or use a known-compatible new-schema image; do not point old ledger code
at the simplified schema or discard real entries. Export and deliberately recover
any records needed before a cross-schema rollback. No destructive down migration.
Production migration permissions must be an explicit reviewed operator path; do
not weaken the test safety guard that rejects ENVIRONMENT=production.

## 3. Test approach

Keep unittest and real PostgreSQL fixtures; do not replace the test framework or
mock away uniqueness, row locks, foreign keys and rollback. Unit tests cover pure
money, interpretation and report rules. API+DB tests cover the small use cases.
Concurrency tests use two real connections and synchronization barriers, not sleeps
or timing assumptions. Gemini and FX are deterministic injected fixtures in CI.
One small real-provider/device suite runs in staging, not on every PR.

Keep tests for image validation, money, auth/household isolation, idempotency/recovery,
rollback, migration safety, and audit immutability. Rewrite tests that assume balance
mutation. Retire tests whose only purpose was scoring, residual thresholds, statement
settlement, schedules or generic candidate transitions. Never preserve old behavior
just to keep an obsolete test green; do not delete a reliability invariant with it.

Use the existing Docker PostgreSQL harness and `scripts/run_local_integration.ps1`.
Before running, inspect the harness/config and explicitly point to disposable local
PostgreSQL with `ENVIRONMENT=test` and `DB_SCHEMA=vibeledger_test_<identifier>`.
Reject known staging/production DB endpoints as well as protected schemas, even if
ENVIRONMENT has been overridden. Do not load inherited `.env` credentials. The old
helper setting ENVIRONMENT=test by itself is not proof that a remote DB is safe.

Existing commands, to run from their respective component directories with safe
test-only environment configuration after dependencies are installed:

```text
# ai-ledger-backend
python -m unittest discover -s tests/unit -p "test_*.py"
python scripts/run_integration_tests.py
python -m unittest discover -s tests/migration -p "test_*.py"
python -m unittest discover -s tests/concurrency -p "test_*.py"

# ai-ledger-dashboard
python -m unittest discover -s tests -p "test_*.py"
```

On Windows use PowerShell 7 (`pwsh.exe`), UTF-8, explicit paths. Every retained
required CI job must pass; add Dashboard tests to the aggregate required check.
Retarget `test_schema_parity.py`'s existing declared table/constraint manifest to
the new baseline. It currently mentions PHYSICAL_SCHEMA in a comment but does not
load that file, so this documentation consolidation does not require changing it
before schema implementation. Do not add runtime parsing of architecture prose.
Pin dependency versions as part of the accepted artifact; no floating deploy-time
upgrade should change the real-device result.

### Acceptance matrix

| ID | Scenario and concrete expected result | Layer |
|---|---|---|
| DB-01 | Fresh baseline has exactly the declared tables/constraints; second migration run is no-op; checksum or old-lineage mismatch fails; wrong household FK and invalid money/status fail; startup never runs DDL. | Migration + DB |
| CAP-01 | Clear 28.50 CNY purchase -> one POST, one transaction, one create audit, saved receipt. Retry same body/key returns same ID with no extra model call/write. Unknown account alone commits null; low category confidence commits Other. | API+DB + phone |
| CAP-02 | Missing/uncertain amount, currency, historic date, or ambiguous expense intent -> one draft, zero transactions/snapshots. Correct in natural language, confirm once, reject alternative draft, and recover; draft edits alone create no money records. Clear transfer/repayment/failed payment never becomes spending. | Unit + API + phone |
| CAP-03 | Concurrent same key, lost post-commit response, concurrent confirm, changed-body key reuse, two devices sharing textual key: at most one result per actor/key; different actors are independent. Image/exact-fact duplicate under new keys warns instead of silently merging. Two real same-price purchases can be confirmed separately. | DB concurrency |
| CAP-04 | Reserve then process crash -> processing remains recoverable/cancellable. Delayed original POST after 404 races with cancel: either exactly one committed result returned by cancel or a rejected tombstone blocking late commit. Never clear unknown pending key first. No raw image stored. | API concurrency + phone |
| CAP-05 | Gemini outage, malformed JSON, image corruption/oversize/decompression size, prompt injection and invalid account UUID -> no invented financial record; retry/review outcome is clear. DB failure during finalize rolls back records/history/response together. Revise-vs-confirm and browser-vs-bodyless-device conflicts cannot confirm unseen edits. | Unit + API+DB |
| SPEND-01 | Expenses 100 and 5 fee-as-expense, refund 30 -> gross 105, refunds 30, net 75. Original purchase persists. Linked refund exceeding remaining amount fails, including two simultaneous refunds; edit/void constraints hold. Transfer/card repayment creates no expense. A 12,000 installment purchase is 12,000 once, monthly principal creates none; no future records/plans. | Unit + API+DB |
| BAL-01 | First manual zero initializes a known zero without a transaction. Clear cash/savings/investment/debt screenshot produces observations under one request. Repeating key produces no extra observations; later dated update replaces current selection without changing spending. | API+DB |
| BAL-02 | Cash 10,000 + term 20,000 + funds 30,000 with displayed total 60,000 -> assets 60,000, not 120,000. Unknown/cropped scope cannot invent totals; real mismatch, duplicate account row, non-unique alias, currency mismatch and approximate balance need correction/review. Explicitly deselected rows have zero effects. | Extraction fixtures + API |
| BAL-03 | Debt 3,000 -> stored -3,000. A card's 1,000 monthly bill or 20,000 credit limit cannot replace total debt. Explicit 200 overpayment -> positive asset in unclassified risk. Remaining installment principal is included once, never added to an already-inclusive total. | Unit + screenshots |
| BAL-04 | Second row of a multi-account commit fails -> no selected snapshot/history/committed response saved. Concurrent head/configuration change -> draft/conflict with latest value, no stale overwrite. Backdated update leaves newer current value unchanged. | API+DB concurrency |
| BAL-05 | Correct/void latest and older snapshots; old facts remain in history, current selection recomputes, future times fail, equal-time collision fails, date-only ambiguity is visible. Close nonzero/uninitialized-with-records account fails; closing zero excludes future wealth while history remains correct. | API+DB |
| WEALTH-01 | Observed positive CNY accounts total 80,000, debt 3,000 -> assets 80,000, debt 3,000, net 77,000; positive risk buckets sum to 80,000. A new unknown account makes complete totals null with known subtotals retained. Stale values keep dates/cues. No accounts -> setup. Zero positive assets -> null risk percentages. Expense capture cannot move wealth. | Unit + API + UI |
| FX-01 | USD 10 expense at 7.2 -> frozen CNY 72; later 7.3 wealth quote doesn't rewrite spending. FX outage still records USD 10 with null conversion; refresh fills once with eligible dated rate. Missing/stale quotes are visible; no fabricated 1:1, zero, future historical rate or whole-page failure. JPY display has 0 decimals; unsupported precision/NaN/infinity rejected. | Unit + API+DB |
| INV-01 | 100,000 -> 160,000, contributions 50,000, withdrawals 0 -> gain 10,000; 100,000 -> 80,000 with withdrawal 25,000 -> gain 5,000. Explicit zero flows permits negative gain. Missing inputs/first snapshot -> unknown, wealth still saved. Reinvested income not counted as contribution. Insert middle snapshot or replace endpoint -> old input pair excluded, new periods unknown. Range-straddling periods never prorated; partial/multicurrency sums labelled. | Unit + API+DB + UI |
| HIST-01 | Every financial create/edit/void/replacement has actor/time/before/after in same transaction; replay creates no duplicate audit. Audit update/delete denied. History and original values remain accessible only to household members. | DB + API |
| SEC-01 | Missing/invalid/revoked device, disabled user, foreign household IDs, guessed draft/alias/refund IDs -> rejected. Device requests scoped to their owner; browser members can review either household member's drafts. No secret/image/model raw response in output/log/audit/receipt. | Unit + API+DB |
| SEC-02 | Each provisioned user logs in, refreshes, signs out; no cross-session token/client reuse. Wrong issuer/audience/algorithm/expired token rejected; rotated JWKS tested. Public signup/nonmembers have no financial access; Dashboard and publishable key cannot query finance tables directly. | Auth tests + staging |
| UI-01 | Four pages support balance capture/review, manual correction, risk, gains/unknown flow input, expense review/refund/void, category edits and device revoke. One Save per ordinary edit. Error/retry does not reset a pending key; incomplete values never render as 0 or 100% fresh. No SQL/DB secret, candidate UI, token-pasting or repayment forecasts. | Dashboard tests + household |
| OPS-01 | Exact target images boot with correct probes, private schema and bounded connection usage. DB/schema failure -> readiness 503; AI outage -> manual operations remain usable. Backup/restore into isolated schema preserves money/history/receipt replay. Old routes absent (404); no hidden engine import. | Container + staging |

### Household acceptance session

Both users run the real Shortcut on Wi-Fi and cellular, including a normal expense,
uncertain expense, natural-language correction and interrupted recovery. Use the
same sanitized request fixtures in CI, then a small consented real screenshot set
for live Gemini. Observe at least 20 representative clear purchase captures across
both users: target >=90% correct automatic capture, no silent wrong amount/currency
or non-expense entry. Include card/Alipay, new merchant and foreign currency cases.
Report actual sample results and timings rather than treating that small set as a
statistical guarantee. The accepted baseline's median/p95 and one-request count
are the latency reference; investigate regressions before changing the Shortcut UX.

Then update distinct bank/Alipay/brokerage balances, capture a card debt, review an
ambiguous row, edit a mistake, and provide or skip investment flows. Ask each user
to identify total assets, debts, net worth, unclassified risk, this month's spending,
and which amounts are old or unknown. They should not need to understand a batch,
candidate, reconciliation engine, JWT, or migration to use the product.

Evidence per gate: commit/image digests, schema/lineage, test commands and pass/fail
summary, sanitized fixture IDs, Shortcut version, observed request counts/timings,
open defects and user acceptance outcome. Never record access tokens, account numbers,
raw household screenshots or live DSNs in Git. All required gates must pass before
proposing production cutover. A docs-only review is not a runtime acceptance result.
