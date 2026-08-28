# Integration-test performance evidence

Date: 2026-08-28

Branch: `infra/test-performance`

Starting `main`: `0570e9f44b3d15cd1aea29245a9eba04339f7a78`

## Outcome

The 262-test PostgreSQL suite keeps the same test inventory and real database,
HTTP, commit, rollback, locking, and concurrency behavior. On the Singapore
Codex Windows host, its profiled wall time fell from 131.840 seconds to 54.062
seconds (59.0%). The lightweight developer runner completed it in 55.955
seconds.

The standard recommendation remains: run PostgreSQL 17 on the same machine as
the test process. Pooling makes connection setup less wasteful, but it cannot
remove the thousands of real SQL round trips that make a remote database an
inappropriate integration-test target.

## Environment boundaries

These results come from three physically different environments and must not
be treated as interchangeable.

| Environment | Location/path | Database evidence | Full integration evidence |
| --- | --- | --- | ---: |
| GitHub Actions | Ubuntu runner, PostgreSQL 17 service container on localhost | Workflow source; not profiled | 262 tests / 30.096 s / OK |
| Singapore Codex baseline | Windows, Python 3.10.20, portable PostgreSQL 17.11 on `127.0.0.1` | Measured locally, SSL off | 262 tests / 131.840 s / OK |
| Singapore Codex optimized | Same host and database | Measured locally, test-only pool size 16 | 262 tests / 54.062 s / OK |
| China Antigravity | Different physical computer, VPN with TUN enabled | Host classification and RTT not yet captured | 262 tests / 2630.873 s / OK |

The Codex process initially had no `.env`, `DATABASE_URL`, `DB_SCHEMA`, or
`ENVIRONMENT` configuration. Therefore there was no pre-existing "Singapore
current DB" to benchmark. Docker was not installed on this host, so the
CI-equivalent local measurement used the official PostgreSQL 17.11 Windows
binaries instead. The committed Docker Compose service provides the repeatable
PostgreSQL 17 workflow for machines with Docker.

## Latest Antigravity evidence

This evidence was supplied by the user on 2026-08-28. The original run's wall
clock start/end timestamp was not captured, so none is invented here. Durations
and commands are recorded exactly as supplied.

| Suite | Exact command | Result | Failures | Errors | Skips |
| --- | --- | --- | ---: | ---: | ---: |
| Backend full integration | `python -m unittest discover -s tests/integration -p "test_*.py" -v` | Ran 262 tests in 2630.873 s, OK | 0 | 0 | 0 |
| Backend unit | `python -m unittest discover tests/unit` | Ran 149 tests in 0.232 s, OK | 0 | 0 | 0 |
| Dashboard | `python -m unittest discover tests` | Ran 36 tests in 0.386 s, OK | 0 | 0 | 0 |

The fast non-DB suites and very slow DB suite isolate the problem to the
integration path, but do not by themselves prove which database Antigravity
used. This report accepts the supplied result as the final Antigravity evidence;
no additional run on that machine is required.

## Baseline diagnostics

Sanitized local database diagnostics:

| Metric | Count | Total | Mean | Median | p95 | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| DNS resolution | 1 | 0.004579 s | — | — | — | — |
| TCP connect | 10 | 0.023107 s | 0.002311 s | 0.000334 s | 0.009947 s | 0.009947 s |
| PostgreSQL connect | 10 | 0.732150 s | 0.073215 s | 0.053212 s | 0.159361 s | 0.159361 s |
| Repeated `SELECT 1` | 100 | 0.007043 s | 0.000070 s | 0.000053 s | 0.000103 s | 0.000409 s |

The database was classified `local`, port 5432, SSL not in use, PostgreSQL
server version number 170011. Even on localhost, a fresh Windows PostgreSQL
connection cost about 58–73 ms while an established-connection `SELECT 1`
round trip cost about 0.07 ms.

## Full-suite before/after profile

Both profile runs discovered the identical inventory SHA-256:
`5bd12f3ce17ba35d98f2ff1101ae7b8b5764b8eae40e2ff870f73d7abeed6729`.

| Metric | Baseline | Shared schema only | Optimized | Change, baseline to optimized |
| --- | ---: | ---: | ---: | ---: |
| Tests run | 262 | 262 | 262 | unchanged |
| Failures / errors / skips | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | unchanged |
| Wall time | 131.839757 s | 125.301442 s | 54.061581 s | -77.778176 s (-59.0%) |
| Physical connections | 1,141 | 1,099 | 9 | -1,132 (-99.2%) |
| Physical connect time | 66.205408 s | 64.904960 s | 0.588918 s | -65.616490 s (-99.1%) |
| Connections during 436 HTTP requests | 787 | 787 | 7 physical / 787 logical | request isolation retained |
| Complete migration runs | 22 | 1 | 1 | -21 (-95.5%) |
| Migration time | 4.284985 s | 0.225272 s | 0.192965 s | -4.092020 s (-95.5%) |
| Schema drops | 22 | 1 | 1 | -21 |
| Schema-drop time | 3.405536 s | 0.174000 s | 0.094315 s | -3.311221 s |
| SQL execute calls | 15,528 | 14,742 | 14,742 | -786 (-5.1%) |
| SQL execute time | 41.489799 s | 37.688814 s | 31.593856 s | -9.895943 s |
| Cleanup/reset calls | 262 | 262 | 262 | unchanged |
| Cleanup/reset time | 36.430861 s | 35.949342 s | 37.228041 s | normal run variance; `TRUNCATE` retained |
| Fixture seed calls | 271 | 271 | 271 | unchanged |
| Fixture seed time | 8.509784 s | 8.053689 s | 2.537496 s | connection reuse reduces setup overhead |

The shared-schema-only full run improved 5.0%, which is measurable but much
smaller than connection reuse. Cached table discovery removes one
`information_schema` round trip per test schema after its first reset. Local
`TRUNCATE ... RESTART IDENTITY CASCADE` still dominates cleanup and is kept
because sequence reset and referential cleanup are correctness requirements.

The optimized profile still performed 1,099 logical checkouts. Each checkout
received an exclusive real PostgreSQL connection; up to nine physical
connections were needed during this serial suite because some scenarios use
real concurrent connections.

## Root-cause ranking

1. **Environment/network sensitivity in Antigravity is the leading cause.**
   Only the PostgreSQL suite is slow there: 2630.873 seconds versus 0.232 and
   0.386 seconds for the backend unit and Dashboard suites. The Antigravity DB
   host and RTT still need to be measured before calling it definitively remote.
2. **Fresh physical connections were the largest removable local cost.** The
   baseline opened 1,141 connections and spent 66.205 seconds establishing
   them. Of those, 787 occurred inside 436 HTTP requests.
3. **The harness is highly sensitive to DB round-trip latency.** The baseline
   observed 15,528 SQL executions in addition to 1,141 connects. As a simple
   sensitivity calculation, 16,669 synchronous DB operations at an additional
   100 ms each represent up to 1,666.9 seconds; at 150 ms they represent about
   2,500.4 seconds. Some operations overlap in concurrency tests, so this is a
   first-order estimate rather than an Antigravity RTT measurement.
4. **Windows-local cleanup is intrinsically material.** The 262 real
   `TRUNCATE ... RESTART IDENTITY CASCADE` resets cost 36–37 seconds. Removing
   them or replacing them with an outer rollback would invalidate test
   semantics and was rejected.
5. **Per-class schema migrations were secondary.** Twenty-two migrations plus
   drops cost about 7.69 seconds. A serial-process schema reduces this to one
   migration and one drop without changing per-test truncation.

Connection reuse does not make a remote database a good test target. The
optimized run still has 14,742 real SQL executions, so remote RTT would still
dominate. Local PostgreSQL 17 is the primary solution for Antigravity.

## What changed and why it is safe

- `scripts/profile_integration_tests.py` is opt-in and observes real
  `psycopg2`, SQL, migrations, cleanup, seeds, HTTP, and test timing. It emits
  only sanitized database metadata.
- `scripts/diagnose_test_database.py` measures DNS, TCP, PostgreSQL handshake,
  SSL use, and repeated `SELECT 1` without outputting a database URL or
  credentials.
- `tests/support/database_pool.py` is installed only by an explicit test
  runner. A logical checkout exclusively owns one real physical connection.
  Dirty transactions are rolled back before reuse, closed/broken connections
  are discarded, and `app.db.get_connection()` reapplies the validated
  `search_path` on every checkout.
- `VIBELEDGER_TEST_SHARED_SCHEMA=1` allows the serial integration process to
  migrate one isolated random schema. Every test still runs the original real
  `TRUNCATE ... RESTART IDENTITY CASCADE`, seed, and transaction lifecycle.
  The schema is dropped at process cleanup.
- The generated, safely quoted truncate statement is cached per migrated
  schema. No table, identity reset, or cascade behavior was removed.
- `scripts/run_integration_tests.py` is the optimized serial developer entry
  point. The existing GitHub Actions exact discovery command remains unchanged.
- `docker-compose.integration.yml` and `scripts/run_local_integration.ps1`
  provide a PostgreSQL 17 localhost service with ephemeral test data.

No production business code changed. No test was deleted, skipped, merged, or
converted to a mock. No assertion or integration scenario changed. No
repository, persistence, HTTP endpoint, or transaction boundary was mocked.
No global outer rollback was introduced. Migration and concurrency suites are
outside the shared integration runner and retain their independent lifecycles.

The shared schema is for one serial process only. Do not enable it across
parallel workers. A future xdist design would require a distinct migrated
schema per worker; parallelism was not added because the serial workflow is now
materially faster and CI is already approximately 30 seconds.

## Standard local workflow

From `ai-ledger-backend` on Windows with Docker and a Python environment whose
requirements are installed:

```powershell
pwsh -File scripts/run_local_integration.ps1 -Python .venv\Scripts\python.exe
```

The service stays up by default for a fast edit/test loop. Add `-StopAfter` to
remove the Compose service after the run, or use `-Port 55433` if 55432 is in
use. The script refuses to proceed without Docker, starts PostgreSQL 17, and
sets test-only environment variables in its own process.

The optimized runner can also be used with an already-running local test DB:

```powershell
$env:ENVIRONMENT = "test"
$env:DATABASE_URL = "postgresql://<local-test-user>:<local-test-password>@127.0.0.1:5432/<local-test-db>"
$env:DB_SCHEMA = "vibeledger_test_runner"
python scripts/run_integration_tests.py
```

Use only a disposable test database. The safety guard requires
`ENVIRONMENT=test` and a validated test schema.

The unchanged CI-equivalent command remains:

```powershell
python -m unittest discover -s tests/integration -p "test_*.py" -v
```

## Final local verification

All commands used Python 3.10.20. Database suites used the same local
PostgreSQL 17.11 instance and an explicitly disposable test schema.

| Suite | Exact command | Result |
| --- | --- | --- |
| Optimized developer integration | `python scripts/run_integration_tests.py --verbosity 1` | 262 tests in 55.955 s; OK; 0 failures/errors/skips |
| Existing CI integration | `python -m unittest discover -s tests/integration -p "test_*.py" -v` | 262 tests in 132.510 s; OK; 0 failures/errors/skips |
| Backend unit | `python -m unittest discover -s tests/unit -p "test_*.py" -v` | 149 tests in 0.986 s; OK; 0 failures/errors/skips |
| Migration | `python -m unittest discover -s tests/migration -p "test_*.py" -v` | 4 tests in 1.780 s; OK; 0 failures/errors/skips |
| Concurrency | `python -m unittest discover -s tests/concurrency -p "test_*.py" -v` | 15 tests in 11.034 s; OK; 0 failures/errors/skips |
| Dashboard | `python -m unittest discover tests` (from `ai-ledger-dashboard`) | 36 tests in 0.073 s; OK; 0 failures/errors/skips |

## Optional diagnostics

The credential-safe diagnostic remains available for any environment where a
future investigation is useful:

```powershell
python scripts/diagnose_test_database.py `
  --connect-samples 10 `
  --select-samples 100 `
  --output db-diagnostics.json
```

The profiler can compare an unoptimized representative run:

```powershell
Remove-Item Env:VIBELEDGER_TEST_SHARED_SCHEMA -ErrorAction SilentlyContinue
python scripts/profile_integration_tests.py `
  tests/integration/test_reconciliation_review_api_db.py `
  --pool-size 0 `
  --output review-baseline.json
```

and the optimized path:

```powershell
$env:VIBELEDGER_TEST_SHARED_SCHEMA = "1"
python scripts/profile_integration_tests.py `
  tests/integration/test_reconciliation_review_api_db.py `
  --pool-size 16 `
  --output review-optimized.json
```

The JSON reports intentionally contain only host classification (`local`,
`private_network`, `remote_dns`, or `remote_ip`), port, SSL state, timings,
counts, and test identifiers. They do not contain the hostname, username,
password, or full `DATABASE_URL`.

## Thirty slowest tests

Timings include setup and teardown and therefore show the connection-harness
improvement as well as intrinsic test work.

| # | Baseline test | Seconds | Optimized test | Seconds |
| ---: | --- | ---: | --- | ---: |
| 1 | `TestStatementApiDb.test_semantic_ambiguity_resolution_flow_and_guards` | 4.352736 | `TestStatementReconciliationDb.test_32_upload_size_limit_boundary` | 1.480562 |
| 2 | `TestStatementApiDb.test_11_candidate_mutation_bypass_regression` | 4.189634 | `TestStatementApiDb.test_semantic_ambiguity_resolution_flow_and_guards` | 0.482647 |
| 3 | `TestStatementApiDb.test_10_semantic_resolution_direction_enforcement` | 2.571282 | `TestCreditCardPhase8Db.test_10_twelve_period_installment_full_lifecycle` | 0.449756 |
| 4 | `TestStatementReconciliationDb.test_32_upload_size_limit_boundary` | 1.801295 | `TestStatementApiDb.test_11_candidate_mutation_bypass_regression` | 0.446999 |
| 5 | `TestAccountsApiDb.test_account_aliases_crud_and_conflict` | 1.596099 | `TestStatementApiDb.test_10_semantic_resolution_direction_enforcement` | 0.391579 |
| 6 | `TestStatementApiDb.test_candidate_category_patch_and_commit_workflow` | 1.364503 | `TestAccountsApiDb.test_account_aliases_crud_and_conflict` | 0.352725 |
| 7 | `TestStatementApiDb.test_ambiguous_match_options_and_review_workflow` | 1.325924 | `TestDashboardApiDb.test_dashboard_overview_endpoint` | 0.348125 |
| 8 | `TestReconciliationReviewApiDb.test_candidate_accept_workflow` | 1.306483 | `TestInvestmentPhase9Db.test_13_investment_candidate_review_flow_resolutions_and_readiness` | 0.312684 |
| 9 | `TestReconciliationReviewApiDb.test_candidate_patch_edit_workflow` | 1.273808 | `TestReconciliationReviewApiDb.test_candidate_patch_edit_workflow` | 0.310198 |
| 10 | `TestCategoriesApiDb.test_category_crud_and_validations` | 1.247859 | `TestCreditCardPhase8Db.test_06_credit_card_state_api_endpoint` | 0.300016 |
| 11 | `TestInvestmentApiDb.test_get_investment_performance_api_success_and_filters` | 1.181650 | `TestInvestmentPhase9Db.test_09_dashboard_investment_summary_isolated_from_cashflow` | 0.294219 |
| 12 | `TestReconciliationReviewApiDb.test_batch_commit_optimistic_concurrency_conflict` | 1.136510 | `TestSchemaParity.test_catalog_structural_contracts` | 0.293335 |
| 13 | `TestDashboardApiDb.test_installments_read_endpoints` | 1.128788 | `TestReconciliationReviewApiDb.test_accepted_transfer_candidate_patch_freeze` | 0.290821 |
| 14 | `TestAccountsApiDb.test_patch_account_nullable_fields_clearing_and_credit_validation` | 1.118489 | `TestInvestmentPhase9Db.test_12_ibkr_real_broker_semantics` | 0.276874 |
| 15 | `TestSnapshotReconciliationApiDb.test_20_exact_committed_replay_preserves_identifiers` | 1.098934 | `TestDashboardApiDb.test_credit_card_state_endpoint` | 0.270284 |
| 16 | `TestReconciliationReviewApiDb.test_accepted_transfer_candidate_patch_freeze` | 1.056592 | `TestDeviceManagementApiDb.test_list_devices_isolation_and_redaction` | 0.266179 |
| 17 | `TestDashboardApiDb.test_credit_card_state_endpoint` | 1.054553 | `TestDashboardApiDb.test_installments_read_endpoints` | 0.263774 |
| 18 | `TestSnapshotReconciliationApiDb.test_09_concurrent_stale_preview_revalidation_at_commit` | 1.023050 | `TestWorkQueueAndReadinessDb.test_readiness_endpoint` | 0.262771 |
| 19 | `TestSnapshotReconciliationApiDb.test_12_cross_household_isolation` | 1.000505 | `TestDashboardApiDb.test_dashboard_account_freshness_endpoint` | 0.262255 |
| 20 | `TestReconciliationReviewApiDb.test_get_statement_lines_filtering_and_isolation` | 0.969471 | `TestCreditCardPhase8Db.test_11_cycle_check_without_authoritative_balance_persists_after_candidate_resolution` | 0.261714 |
| 21 | `TestSnapshotReconciliationApiDb.test_19_threshold_evaluated_in_cny_independent_of_reporting_currency` | 0.959739 | `TestReconciliationReviewApiDb.test_candidate_reject_workflow` | 0.260741 |
| 22 | `TestSnapshotReconciliationApiDb.test_08_preview_endpoint_is_strictly_read_only` | 0.958425 | `TestDashboardApiDb.test_dashboard_cash_flow_endpoint` | 0.255334 |
| 23 | `TestSnapshotReconciliationApiDb.test_05_threshold_boundaries` | 0.935822 | `TestStatementReconciliationDb.test_36_manual_match_semantic_type_compatibility` | 0.254826 |
| 24 | `TestAccountsApiDb.test_create_account_validations` | 0.933422 | `TestAuthPhase10Db.test_audit_provenance_device_id_forwarding_statement` | 0.254543 |
| 25 | `TestSnapshotReconciliationApiDb.test_25_stale_reviewed_amount_becomes_another_large_amount_returns_needs_review` | 0.929303 | `TestStatementApiDb.test_ambiguous_match_options_and_review_workflow` | 0.253039 |
| 26 | `TestReconciliationReviewApiDb.test_candidate_accept_with_target_transaction` | 0.895177 | `TestReconciliationReviewApiDb.test_batch_commit_optimistic_concurrency_conflict` | 0.251269 |
| 27 | `TestAccountsApiDb.test_patch_account_immutability_rules` | 0.887160 | `TestInvestmentApiDb.test_create_first_manual_snapshot_api_success` | 0.250922 |
| 28 | `TestCategoriesApiDb.test_deactivated_category_historical_transaction_and_expense_rejection` | 0.882550 | `TestInvestmentPhase9Db.test_11_unrepresented_ledger_transfer_causes_needs_review_and_canonical_pnl` | 0.249514 |
| 29 | `TestSnapshotReconciliationApiDb.test_26_stale_reviewed_residual_becomes_zero_commits_snapshot_without_adjustment` | 0.823349 | `TestStatementApiDb.test_candidate_category_patch_and_commit_workflow` | 0.248608 |
| 30 | `TestSnapshotReconciliationApiDb.test_11_repeated_commit_is_replay_safe` | 0.822953 | `TestReconciliationReviewApiDb.test_candidate_accept_workflow` | 0.247607 |
