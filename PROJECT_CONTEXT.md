# VibeLedger project handoff

Updated: **2026-09-12**.

## Authority and accepted baseline

1. [TARGET_DOMAIN_MODEL](TARGET_DOMAIN_MODEL.md)
2. [CONTRACTS](docs/architecture/CONTRACTS.md)
3. [IMPLEMENTATION_PLAN](docs/architecture/IMPLEMENTATION_PLAN.md)

**S1 and S2 are formally accepted**, per the user's 2026-09-11 handoff.
S2 real-device acceptance passed direct capture, confirm/revise, idempotent recovery,
404 cancellation tombstones, duplicates and real Gemini integration. Do not reopen
these stages without a genuine regression. This supersedes the prior handoff's
pending S2 acceptance notes. S2 code is committed through `298499c`, including
Dashboard full-price-expense conversion to installment schedules.

The original S3 implementation was committed and pushed as `4a06657` after the
2026-09-11 handoff. The 2026-09-12 continuation starts from that clean branch head.
No S3 independent review, live model acceptance or formal acceptance is claimed.

## Current stage: S3 — Balances, wealth, risk and statement import

Implemented in this checkpoint:

* `domain/balances.py` and `services/balance_service.py` provide one native signed
  observation path. Manual multi-account Save is atomic; zero is valid. Currency,
  account lifetime, future dates, same-time/date-only conflicts, current head and
  account versions are checked under the household lock. Correction voids the old
  record and inserts a replacement; void/reopen/closure guards preserve history.
  A later nonzero observation cannot silently replace a closed account's zero.
* `/balance-updates`, `/accounts/{id}/snapshots`, `/snapshots/{id}/correct|void`
  replace the old reconciliation-based snapshot route. Account reads now include
  their latest active observation. No balance operation writes spending or ledger
  adjustments. Snapshot and statement-line history is household scoped.
* `/reports/wealth` returns latest observations, known/null-complete totals,
  positive-asset risk buckets, missing/old observations and FX coverage. Current
  stale cached FX remains explicitly dated; historical stale/future FX is excluded.
  `/reports/wealth-history` carries forward observations at balance/lifetime events,
  including gaps before initialization; historical risk allocation is not asserted.
  Explicit FX refresh also prepares current wealth quotes without changing balances.
* `/balance-captures` uses typed Gemini extraction outside DB connections, existing
  durable receipts, conservative account/scope/debt/date gates, total evidence and
  explicit whole-row corrections/exclusions. Credit monthly bills do not become
  total debt. Browser/device confirmation rules are retained. Amounts and selected
  rows commit together. Sanitized row choices survive for history; raw images do not.
* `/accounts/{id}/statement-imports` accepts a bounded PDF and memory-only password,
  claims household/account/document identity, parses outside financial locks and
  always creates one preview. New-key repeated files return the canonical request.
  Temporary original PDFs are removed on success/failure. Limits: 20 MiB, 50 pages,
  1,000 lines and 120-second parse deadline; model transport records coverage signals.
* Statement evidence is immutable in `statement_lines`; edits live in the draft.
  Import supports expenses/fees, explicit unlinked refunds, nonspending skips,
  explicit duplicate links/separate purchases, provider IDs and schedule periods.
  Missing business dates never use posting dates. Wrong/uncertain account identity
  and partial coverage need explicit confirmation. Per-line `confirm_facts` prevents
  editing one UI page from confirming unseen low-confidence rows on another page.
* Statement Save rechecks duplicates and target versions under the shared lock;
  financial rows, optional balance, line outcomes, audit and receipt are atomic.
  Existing same-time/value observations can be explicitly reused. Source/date
  provenance stays `statement`, including first creation through a due period.
* Dashboard Wealth now offers honest totals, native dated balances, manual bulk
  updates, screenshot review, snapshot history/correction/void, and current FX retry.
  Statement import replaces the old reconciliation destination. One paginated
  preview supports line decisions and optional closing balance. Review also exposes
  balance/statement drafts. Unknown upload outcomes preserve the key for recovery
  or cancellation. S1 Settings and S2 spending remain available.

Narrow shared extensions: capture receipt reservation accepts a kind/operation
(defaults unchanged); ingestion dispatches typed balance/statement drafts; schedule
binding accepts statement provenance/item identity (Shortcut defaults unchanged).
The accepted 16-table baseline and migration files have **not changed**.

## 2026-09-12 CI follow-up

The user pushed `9f9a4b4`. GitHub run
[34685754221](https://github.com/xuesishen9587-del/VibeLedger/actions/runs/34685754221)
(and the parallel run 34685753184) ran on that exact commit:

* Unit, Dashboard, Migration & Concurrency jobs passed.
* PostgreSQL Integration ran 170 tests: one failure and one error. The aggregate
  Backend CI check failed because of that job.
* `test_correction_void_history_and_closure_guard`: Python 3.10 rejects a UTC `Z`
  suffix emitted by Pydantic's JSON timestamp serialization. `instant` now converts
  a terminal `Z` to `+00:00` before parsing, preserving timezone validation.
* `test_voided_provider_evidence_cannot_auto_link_or_be_recreated`: statement
  preparation popped `actual_page_count` from the parser's dictionary. Reusing the
  same extraction then raised KeyError. Preparation now reads that field without
  mutating the caller's data and validates a separate mapping.

Local verification uses **Python 3.10.21**, matching CI, with explicit test-only
configuration and a loopback DSN. Added four unit regressions for Pydantic timestamp
round-trip, equivalent UTC/offset timestamps, rejection of naive/malformed dates,
and repeated preparation preserving page evidence and partial-coverage detection.
Running those regressions against the previous functions reproduced one failure
and two errors; with the fixes, **240 backend unit tests and 74 Dashboard tests
pass**. No migration, financial guard, or CI assertion was removed or weakened.

The two original real-DB tests remain the final regression checks. PostgreSQL is
still unavailable in this host; do not claim the corrected full integration suite
has passed until CI runs on the new commit. Command-line Git still has no push
credentials, and a fresh connected GitHub tree-write attempt returned HTTP 403.
The current follow-up must be pushed and CI rechecked; S3 acceptance,
live Gemini and household verification remain outstanding.

## Earlier 2026-09-12 continuation checkpoint (9f9a4b4)

At the time of the earlier checkpoint, **GitHub push was blocked** (the user has
now pushed it, as recorded above): command-line Git has no write credentials; the connected GitHub Git-tree
write returned `403 Resource not accessible by integration`, including with workflow
changes excluded. Do not assume remote sync or CI execution. A local commit and
recoverable Git bundle are prepared with this handoff; no deployment occurred.

Implemented:

* Statement imports reject voided transaction targets, including automatic
  provider-ID matching. Evidence remains reserved after voiding; explicit skip is
  available without recreating spending. Added DB regressions for voided provider
  evidence, explicit voided links, a target changed after preview, cancellation
  while PDF parsing is blocked, and conflicting same-provider rows rolling back.
* Balance totals referring to absent extracted components require explicit review.
  Uncertain total scope cannot become informational merely because a row was
  excluded. Known incomplete totals and explicit exclusions remain distinguishable.
  Tests preserve exact equality and the explicit display-rounding boundary.
* Wealth history now includes FX quote changes, historical quote expiry and
  observation staleness boundaries, even without a new balance observation.
  A new Dashboard date-range step chart separates complete and known-partial
  amounts, retains gaps and zero values, and exposes dated coverage in a table.
* Statement transaction targets support date/exact-merchant filtering and bounded
  50-record pagination beyond the old first-200 limit. Schedule targets paginate
  too. Existing selections survive search/page changes, unavailable targets are
  visible, and refresh is explicit. Save passes the selected target's version.
* Corrected a Python 3.10-incompatible nested f-string in statement preview.
* The existing complete CI workflow is configured to run on this experiment branch.
  It will only execute after a successful push; no deployment workflow was added.

Verification in the Linux continuation environment (Python 3.12.14):

| Check | Result |
|---|---|
| Backend unit discovery | **236 passed** (5 new review-guard tests) |
| Dashboard discovery | **74 passed** (7 new controller/figure/AppTest cases) |
| Integration / migration / concurrency | **Not run here**; no local PostgreSQL/Docker; installation unavailable |
| New real-DB regressions | 6 added; **unverified**, not included in any new pass count |
| Whitespace / compile checks | Passed on local Python 3.12 |
| S1/S2 and applied migrations | No migration or accepted-stage runtime edits |
| Live model / real household UI / independent review | Not performed; remain acceptance gates |

Environment changes are confined to local test dependencies. No inherited `.env`
was used for final testing; `ENVIRONMENT=test`, a loopback-only disposable DSN and
an explicit `vibeledger_test_*` schema were provided. No database was contacted by
the unit/UI suites. Test logs stay outside Git because baseline tests print fixture
JWTs. PowerShell and the prior Windows Python path do not exist in this Linux host.

Five-hour account usage is unavailable: the runtime Codex rate-limit read returned
401 Unauthorized. No percentage or exhaustion time is inferred. Restore repository
write access, push this reviewed checkpoint, and inspect all required CI jobs before
expanding S3 or claiming database acceptance. If CI finds a regression, fix it without
reopening accepted S1/S2 except where the regression actually requires it.

## Prior-session local verification

Disposable Docker PostgreSQL 17 on loopback port 55432; no remote financial database.

| Suite | Result |
|---|---|
| Backend unit discovery | 231 passed |
| Backend integration discovery | 164 passed (21 new S3 cases) |
| Migration discovery | 5 passed |
| Concurrency discovery | 9 passed; S3 concurrency also covered in integration |
| Dashboard discovery | 67 passed (2 new S3 AppTest cases) |

New S3 tests cover zero/partial wealth, stale versus missing FX, atomic rollback,
concurrent heads, backdates, corrections/closure protection, debt and total evidence,
unknown-row exclusion, changed configuration, statement duplicate identities,
concurrent imports, refund/skip decisions, balance-only imports, explicit observation
reuse, statement-to-schedule identity and unseen-page review protection. PDF unit
tests exercise encryption/password failures, page/byte limits, and temporary cleanup.
Models are faked in DB tests; local passing tests are not live model acceptance.

Use `ai-ledger-backend/venv_backend/Scripts/python.exe`, including for Dashboard.
Set ENVIRONMENT=test, DATABASE_URL to the local harness DSN, and DB_SCHEMA explicitly.
Run backend `scripts/run_integration_tests.py`; unittest discovery directories are
`tests/unit`, `tests/migration`, `tests/concurrency`, and Dashboard `tests`.
Do not run legacy root scripts against inherited .env credentials. Raw test logs
can contain fixture tokens; they were removed and must not be committed.

## Remaining S3 work and acceptance

1. Independent review and adversarial expansion of the new balance/statement paths.
   In particular, review total/rounding scope, duplicate provider contradictions,
   voided evidence, target-edit versus import races, and cancellation during PDF
   parsing. Reuse the canonical BAL/WEALTH/STMT acceptance matrix, not old test counts.
2. Live Gemini and real household balance screenshots/statements, encrypted PDFs,
   long statements, partial/scanned documents and measured timeouts. Local temporary
   cleanup is tested; hosted-resource/latency limits and prompt behavior need staging
   evidence. No new live model request or deployment was made in this session.
3. Finish UI acceptance/polish: the continuation adds the wealth-history chart and
   paged transaction/schedule target selection, with unit/AppTest checks. Real
   household chart interpretation, review clearing/retries, large target lists and
   draft navigation still need acceptance. Snapshot-reuse selection in a statement
   remains limited to its first 200 observations. Line editing is paged in groups
   of 25; unsaved form edits must be saved before changing pages/search criteria.
4. Validate document completeness messaging and account-scope overrides with users.
   Typed completeness signals cannot prove the model extracted every real row.
   The known missing-currency -> INVALID_AMOUNT UX issue remains non-blocking and
   unchanged in spending; new signed-balance validation distinguishes currency.
5. S4 investment input/estimated-gain UI, complete four-page navigation and consumer
   login remain future work. S5 removes remaining legacy report/investment/audit
   routes/pages and wires/deploys daily scheduler authentication. The application
   is not yet the completed simplified production release.

No new product decision blocks development. Product authority remains unchanged:
spending and balances are independent; absent investment flows imply estimated
zero-flow gains (S4); statements never recreate reconciliation or infer flows.

## Operating constraints

Use PowerShell 7 (`pwsh.exe`) and UTF-8. Preserve accepted migrations/S1/S2 unless a
real regression is found. Check account usage periodically. When the five-hour
allowance has **less than 10% remaining**, stop new feature work, finish verification
and handoff, and report completed/pending work before exhausting the allowance.
