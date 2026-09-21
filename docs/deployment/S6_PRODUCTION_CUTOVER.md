# S6 production fresh-cutover sheet

**Preparation only — not authorization to execute; S6 is not complete.**

Prepared against merged main `e5f3cbd7c14c6d7fb430b59e5090b41a5bc02cc3`, accepted S5 `a6aa1f660d5aa0e6393547be5fe1eb277e1d28bb` (same application tree). Canonical authority remains [domain model](../../TARGET_DOMAIN_MODEL.md), [contracts](../architecture/CONTRACTS.md), and the exact [S6 scope](../architecture/IMPLEMENTATION_PLAN.md#s6--later-separately-authorized-production-fresh-cutover). Commands below are future operator instructions, not actions performed by this preparation.

## 1. Discovered environment state

Read-only Cloud Run, Scheduler and secret-binding inspection on 2026-09-21 found the following in project `vibeledger-staging`, region `asia-southeast1`:

| Service | Current revision | Database/runtime role |
|---|---|---|
| `vibeledger-s34acc-backend` | `00008-jcg` | Accepted simplified staging; schema `vibeledger_s34acc_20260914` |
| `vibeledger-s34acc-web` | `00005-lbz` | Accepted React web |
| `vibeledger-s34acc-dashboard` | `00001-vsp` | Preserved Streamlit fallback |
| `vibeledger-backend-staging` | `00003-m7n` | Legacy schema `vibeledger_staging` |
| `vibeledger-dashboard-staging` | `00001-rql` | Legacy dashboard |
| `vibeledger-s2fr-backend` | `00001-55q` | Earlier simplified schema `vibeledger_s2fr_20260909` |

Accepted backend URL: `https://vibeledger-s34acc-backend-yggxaydsxa-as.a.run.app`; web: `https://vibeledger-s34acc-web-yggxaydsxa-as.a.run.app`.

Backend currently uses 1 CPU, 512 MiB, concurrency 8, maximum 2 instances, 300-second request timeout. Web uses 1 CPU, 512 MiB, concurrency 80, maximum 20 instances, 300-second timeout. Staging uses shared staging service accounts. Its DB binding is `vibeledger-s34acc-runtime-database-url:latest`, Gemini binding `vibeledger-staging-gemini-api-key:latest`. No secret payloads were inspected.

Supabase browser issuer is `https://uvecwlcnynfhmtccoagx.supabase.co/auth/v1`, audience `authenticated`, ES256 with remote JWKS. Gemini is `gemini-3.5-flash-lite`. Existing `vibeledger-s34acc-daily` is enabled at `15 0 * * *`, `Asia/Singapore`, with exact backend URL audience and `vibeledger-s34acc-scheduler@vibeledger-staging.iam.gserviceaccount.com`. It remains unchanged.

Accessible project names also include `gen-lang-client-0615238884` and `project-2053a573-f9d8-454a-832`; their presence does not establish the existing Gemini key's billing project. No production-named service was discovered in the inspected project. Private database target/roles, Auth users and phone state were not inspected. Historical S5 evidence does not add requirements to the owner-approved S6 scope amendment in section 14.

## 2. Target production architecture

Proposed smallest deployment: reuse the existing Cloud Run project/region and approved Supabase platform, with **new services, schema, DB roles, runtime identities, secrets and devices**. The historical cloud project name remains `vibeledger-staging`; this shares platform administration, billing and capacity. G0 must explicitly accept this tradeoff. The Auth URL alone does not establish the correct database host: verify privately before DDL. If separate platform isolation is required, revise this sheet before execution.

| Resource | Proposed exact value |
|---|---|
| Backend | `vibeledger-prod-backend`; accepted FastAPI image; port 7860; CPU 1; memory 512Mi; min 0/max 2; concurrency 8; timeout 300s |
| Web | `vibeledger-prod-web`; accepted React/Node proxy image; port 8080; CPU 1; memory 512Mi; min 0/max 2; concurrency 80; timeout 300s |
| Schema | `vibeledger_prod_v1`, fresh and not exposed through Supabase Data API |
| DB owner / runtime | `vibeledger_prod_owner` NOLOGIN / `vibeledger_prod_runtime` dedicated login |
| Cloud runtime SAs | `vibeledger-prod-backend`, `vibeledger-prod-web`, `vibeledger-prod-scheduler` in `vibeledger-staging.iam.gserviceaccount.com` |
| Secrets | `vibeledger-prod-database-url`, `vibeledger-prod-gemini-api-key`, pinned numeric versions |
| Scheduler | `vibeledger-prod-daily`, Singapore 00:15, initially paused after creation |

Use generated Cloud Run service URLs, discovered after deployment, then freeze them in the private release record. No guessed URL, staging identifier substitution, custom domain, extra worker or load balancer is needed. Public Cloud Run ingress is required by the accepted iPhone bearer-token and web proxy design; application auth protects financial routes, and separately verified Google OIDC protects Scheduler. Web receives no DB/Gemini secrets. Never attach legacy runtime to this schema.

## 3. Immutable release artifacts

Promote these accepted S5 digests unchanged; do not rebuild or resolve a moving tag:

```powershell
$Project = 'vibeledger-staging'
$Region = 'asia-southeast1'
$Backend = 'vibeledger-prod-backend'
$Web = 'vibeledger-prod-web'
$BackendImage = 'asia-southeast1-docker.pkg.dev/vibeledger-staging/vibeledger-staging/vibeledger-s34acc-backend@sha256:3bf0dea587d36a1d8ade7b03b8936e2119573d0be664bfd908326ccb7b991fb0'
$WebImage = 'asia-southeast1-docker.pkg.dev/vibeledger-staging/vibeledger-staging/vibeledger-s34acc-web@sha256:10e09cb4603fe45779286022072250eea6cf30e4a405df0608b7d12fdf671cc3'
$BackendSA = "vibeledger-prod-backend@$Project.iam.gserviceaccount.com"
$WebSA = "vibeledger-prod-web@$Project.iam.gserviceaccount.com"
$SchedulerSA = "vibeledger-prod-scheduler@$Project.iam.gserviceaccount.com"
$BaselineHash = 'bf8cc2ea6c46dd9ce66389784e499c1bbd7ff756ef532d1d9b23aa83d186651c'
if ((Get-FileHash ai-ledger-backend/migrations/simplified/0001_simplified.sql -Algorithm SHA256).Hash.ToLowerInvariant() -ne $BaselineHash) { throw 'Wrong baseline' }
gcloud artifacts docker images describe $BackendImage --project=$Project --format='value(image_summary.digest)'
gcloud artifacts docker images describe $WebImage --project=$Project --format='value(image_summary.digest)'
git diff a6aa1f660d5aa0e6393547be5fe1eb277e1d28bb e5f3cbd7c14c6d7fb430b59e5090b41a5bc02cc3 --exit-code
```

All snippets use PowerShell 7 from repository root. Check `$LASTEXITCODE` after **each** external command and stop on nonzero; do not paste the whole sheet as a script. Record the reviewed preparation commit as well as application SHA, both digests, resulting service revisions, migration checksum and numeric secret versions. Preserve these images against registry cleanup. Artifact absence or changed provenance blocks execution.

## 4. Fresh database/schema and migration

G1 requires a privately approved actual Supabase database/project, administrative operator, extension inventory and connection capacity. Use a direct PostgreSQL administrative connection for fresh-schema/bootstrap DDL. Runtime uses a dedicated direct or proven session-pooler connection; do not assume transaction pooling is compatible with session search paths. Pooler custom-role usernames may require `ROLE.PROJECT_REF`. Check the aggregate old/staging/production connection budget.

Configure private `PGSERVICEFILE`/`PGPASSFILE` outside the repository with `vl-prod-admin` and later `vl-prod-runtime`; protect their ACLs. Never put DSNs/passwords in command arguments, transcripts, this sheet or Git. `$ApprovedDatabase` and `$ApprovedOperator` below are private operator-verified values, not defaults.

```powershell
psql 'service=vl-prod-admin' -X -v ON_ERROR_STOP=1 -c 'SELECT current_database(),session_user; SELECT extname FROM pg_extension;'
psql 'service=vl-prod-admin' -X -v ON_ERROR_STOP=1 -c "SELECT nspname FROM pg_namespace WHERE nspname='vibeledger_prod_v1'; SELECT rolname FROM pg_roles WHERE rolname IN ('vibeledger_prod_owner','vibeledger_prod_runtime');"
# Both target queries must be empty. Verify the baseline hash from section 3 first.
psql 'service=vl-prod-admin' -X -v ON_ERROR_STOP=1 -v authorization=CREATE_NEW_VIBELEDGER_PROD_V1 -v "expected_database=$ApprovedDatabase" -v "expected_operator=$ApprovedOperator" -f docs/deployment/sql/s6_fresh_schema.sql
```

The reviewed [operator SQL](sql/s6_fresh_schema.sql) creates roles, schema and accepted simplified baseline in one transaction, checks required extensions (`pgcrypto`, `pg_trgm`, `citext`), records its checksum, and refuses any existing target. It grants runtime table SELECT/INSERT/UPDATE only, removes migration writes/audit updates, and denies browser roles schema access. No DELETE/TRUNCATE/DDL grants. Do not expose this schema in Supabase API settings. Review inherited/default grants and existing `PUBLIC` privileges on other schemas; a fresh role alone does not prove isolation.

Keep `validate_safety()` rejecting `ENVIRONMENT=production`. Do not run `migrations.runner`, test bootstrap, legacy migrations, or `staging_seed.example.json` against production, and do not falsify ENVIRONMENT to evade that guard. This explicit SQL is the S6 operator path. Failed/lost responses require inspection; never drop/recreate to make reruns succeed.

## 5. Household, users, categories and accounts

Privately confirm two distinct verified Supabase Auth UUIDs/email mappings, owner/member roles, household name, reporting currency and agreed `started_on`. Existing Auth identities may be reused intentionally; internal simplified user/household IDs must be newly generated. Do not copy old membership IDs or rows. The single-person S5 manual acceptance waiver remains accepted; configuring the second production identity does not require reopening that test campaign.

```powershell
psql 'service=vl-prod-admin' -X -v ON_ERROR_STOP=1 -v authorization=BOOTSTRAP_NEW_VIBELEDGER_PROD_V1 -v "expected_database=$ApprovedDatabase" -v "expected_operator=$ApprovedOperator" -f docs/deployment/sql/s6_bootstrap.sql
```

[Bootstrap SQL](sql/s6_bootstrap.sql) privately prompts for inputs, verifies both against confirmed `auth.users`, requires empty application tables, and atomically creates one household, two users/memberships and the accepted 18 category defaults (including both fallback categories). It creates no devices, accounts, schedules, transactions, observations or ingestion receipts.

After browser auth is verified, owner creates the approved real account list through normal UI/API, one account at a time with durable idempotency keys. Record account ID, owner, type, currency, risk, opening date, statement-import setting and **non-overlapping balance scope** privately. Do not accept generic Cash/Checking/Credit seed accounts without review. Include real assets, investments and liabilities; full card outstanding debt is not merely this month's statement due. Account/category metadata writes are allowed at G1, but financial writes are not. Do not create schedules yet.

## 6. Auth, Gemini, secrets and Scheduler configuration

Supabase web config uses `https://uvecwlcnynfhmtccoagx.supabase.co` and its approved publishable key. Verify password login, disabled unintended public signup, exact new web redirect URLs if needed by enabled flows, and both UUID mappings. Preserve all existing redirects/users/signing keys. React stores its browser session per origin and pending state per user; do not copy storage from staging. With a shared issuer, browser JWTs can intentionally authenticate in both environments when mapped; device credentials cannot.

After G1, create only the three new service accounts and two new secret resources. Provision unique production DB credentials and a production Gemini key under the approved billing/quota project. In private interactive psql use `\password vibeledger_prod_runtime` (masked input); enable LOGIN only after grants are verified. Upload secret values from ACL-protected files, outside Git, to Secret Manager; remove temporary plaintext after verification. Grant backend SA `roles/secretmanager.secretAccessor` on only these two secrets. Do not grant project-wide secret access or reuse staging DB credentials. Operator deploy/IAM permissions are temporary administrative permissions, not runtime grants.

```powershell
foreach ($Name in @('vibeledger-prod-backend','vibeledger-prod-web','vibeledger-prod-scheduler')) {
  gcloud iam service-accounts create $Name --project=$Project
  if ($LASTEXITCODE -ne 0) { throw 'Service account creation failed; inspect before retry' }
}
# Repeat for each new secret using its private file; do not print file contents.
gcloud secrets create vibeledger-prod-database-url --project=$Project --replication-policy=automatic
gcloud secrets versions add vibeledger-prod-database-url --project=$Project --data-file=$PrivateDatabaseSecretFile
gcloud secrets create vibeledger-prod-gemini-api-key --project=$Project --replication-policy=automatic
gcloud secrets versions add vibeledger-prod-gemini-api-key --project=$Project --data-file=$PrivateGeminiSecretFile
foreach ($Name in @('vibeledger-prod-database-url','vibeledger-prod-gemini-api-key')) {
  gcloud secrets add-iam-policy-binding $Name --project=$Project --member="serviceAccount:$BackendSA" --role=roles/secretmanager.secretAccessor
  if ($LASTEXITCODE -ne 0) { throw 'Secret grant failed' }
}
```

Set `$DatabaseSecretVersion`/`$GeminiSecretVersion` to the returned numeric versions. Create `$BackendConfigFile` privately with this non-secret YAML:

```yaml
ENVIRONMENT: production
DB_SCHEMA: vibeledger_prod_v1
AUTH_ISSUER: https://uvecwlcnynfhmtccoagx.supabase.co/auth/v1
AUTH_AUDIENCE: authenticated
AUTH_ALGORITHMS: '["ES256"]'
AUTH_JWKS_URL: https://uvecwlcnynfhmtccoagx.supabase.co/auth/v1/.well-known/jwks.json
GEMINI_MODEL: gemini-3.5-flash-lite
FX_API_BASE_URL: https://api.frankfurter.app
FX_HTTP_TIMEOUT_SECONDS: "5"
```

```powershell
gcloud run deploy $Backend --project=$Project --region=$Region --image=$BackendImage --service-account=$BackendSA --port=7860 --cpu=1 --memory=512Mi --min-instances=0 --max-instances=2 --concurrency=8 --timeout=300s --ingress=all --allow-unauthenticated --env-vars-file=$BackendConfigFile --set-secrets="DATABASE_URL=vibeledger-prod-database-url:$DatabaseSecretVersion,GEMINI_API_KEY=vibeledger-prod-gemini-api-key:$GeminiSecretVersion"
$ProdBackendUrl = gcloud run services describe $Backend --project=$Project --region=$Region --format='value(status.url)'
gcloud run services update $Backend --project=$Project --region=$Region --update-env-vars="SCHEDULER_AUDIENCE=$ProdBackendUrl,SCHEDULER_SERVICE_ACCOUNT=$SchedulerSA"
```

Create `$WebConfigFile` with `BACKEND_URL` equal to that exact URL, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` and `DASHBOARD_TIMEZONE: Asia/Singapore`. These names match `ai-ledger-web/server.mjs`; never include a Supabase service-role key.

```powershell
gcloud run deploy $Web --project=$Project --region=$Region --image=$WebImage --service-account=$WebSA --port=8080 --cpu=1 --memory=512Mi --min-instances=0 --max-instances=2 --concurrency=80 --timeout=300s --ingress=all --allow-unauthenticated --env-vars-file=$WebConfigFile
$ProdWebUrl = gcloud run services describe $Web --project=$Project --region=$Region --format='value(status.url)'
gcloud run services add-iam-policy-binding $Backend --project=$Project --region=$Region --member="serviceAccount:$SchedulerSA" --role=roles/run.invoker
# Zero spending schedules is a prerequisite: the brief create-to-pause window must be harmless.
gcloud scheduler jobs create http vibeledger-prod-daily --project=$Project --location=$Region --schedule='15 0 * * *' --time-zone=Asia/Singapore --uri="$ProdBackendUrl/internal/spending-schedules/run" --http-method=POST --oidc-service-account-email=$SchedulerSA --oidc-token-audience=$ProdBackendUrl --attempt-deadline=180s --max-retry-attempts=5 --min-backoff=30s --max-backoff=300s --max-retry-duration=3600s
gcloud scheduler jobs pause vibeledger-prod-daily --project=$Project --location=$Region
gcloud scheduler jobs run vibeledger-prod-daily --project=$Project --location=$Region
```

Check authenticated forced-run success with zero financial writes, then confirm the job remains PAUSED. The operator needs permission to act as the Scheduler SA; the Google-managed Scheduler service agent keeps its documented service-agent role. Do not grant token-creator broadly. Resume only after G3 acceptance, when approved real schedules can be created. Never change the staging job.

## 7. Device and iPhone Shortcut transition

Before G2, inspect each old Shortcut and browser's pending keys **against its original endpoint and original actor token**. Use `GET /api/v1/ingestion-requests/by-key/{key}`; resolve uncertain/pending requests there. If abandoned, use `POST /api/v1/ingestion-requests/by-key/{key}/cancel` and verify its terminal result. A committed receipt is not undone by cancellation. A not-found response alone does not rule out an in-flight request; cancellation establishes a tombstone. Retain terminal evidence privately; do not replay old keys into production.

Provision new devices through production browser `POST /api/v1/devices` with `device_name`, `platform`, `client_version`. Save the one-time returned token directly to the intended device's private storage; never paste it into this sheet/logs. If response is lost, inspect the production device list before provisioning another. Do not revoke existing devices. Verify each new device maps to its intended production user and old device bearer fails on production.

Duplicate the accepted Shortcut as a clearly named production Shortcut. Keep a separate local pending/token folder (for example `On My iPhone/VibeLedger-Production`) so switching URLs cannot reuse the old pending key or overwrite its token. Change **endpoint, token and pending-state namespace together**; retain the original disabled Shortcut for rollback. No real screenshot submission until G3. New requests use fresh UUID keys and existing recovery semantics. Do not add per-request preflight calls or change the accepted one-POST capture protocol.

## 8. Initial financial state

After G3, enter real current balances through normal Balance Update UI/API with their actual observation times, currencies, account versions and expected prior snapshot IDs. Review extraction drafts before confirmation. Missing accounts remain uninitialized; do not substitute zero. Full liabilities and non-overlapping scope must be checked with the owner. No synthetic opening transactions, old ledger history, copied staging balances or historical statement replay.

**The first committed balance observation is already a financial write**, even if there are zero transactions. It changes which rollback procedure applies. Capture the first receipt and account snapshot IDs privately for request recovery and verification.

## 9. Exact pre-cutover verification

Run section 3 artifact/hash checks; verify new service image digests/revisions, pinned secret version references, service accounts, schema and Scheduler audience against the private manifest. Do not dump complete environment/secret payloads.

```powershell
Invoke-RestMethod "$ProdBackendUrl/health"
Invoke-RestMethod "$ProdBackendUrl/ready"
Invoke-RestMethod "$ProdWebUrl/health"
$PublicConfig = Invoke-RestMethod "$ProdWebUrl/config"
if ($PublicConfig.supabaseUrl -ne 'https://uvecwlcnynfhmtccoagx.supabase.co' -or $PublicConfig.timezone -ne 'Asia/Singapore' -or -not $PublicConfig.supabaseKey.StartsWith('sb_publishable_')) { throw 'Web config mismatch' }
$NoAuth = Invoke-WebRequest "$ProdBackendUrl/api/v1/accounts" -SkipHttpErrorCheck
if ($NoAuth.StatusCode -ne 401) { throw 'Missing-auth gate failed' }
$NoOIDC = Invoke-WebRequest "$ProdBackendUrl/internal/spending-schedules/run" -Method Post -SkipHttpErrorCheck
if ($NoOIDC.StatusCode -ne 401) { throw 'Scheduler auth gate failed' }
gcloud run services describe $Backend --project=$Project --region=$Region --format='yaml(status.latestReadyRevisionName,status.url,spec.template.spec.serviceAccountName,spec.template.spec.containers.image)'
gcloud run services describe $Web --project=$Project --region=$Region --format='yaml(status.latestReadyRevisionName,status.url,spec.template.spec.serviceAccountName,spec.template.spec.containers.image)'
gcloud scheduler jobs describe vibeledger-prod-daily --project=$Project --location=$Region --format='yaml(state,schedule,timeZone,httpTarget.uri,httpTarget.oidcToken,attemptDeadline,retryConfig)'
psql 'service=vl-prod-admin' -X -v ON_ERROR_STOP=1 -c "SELECT migration_name,checksum_sha256 FROM vibeledger_prod_v1.schema_migrations; SELECT count(*) AS transactions FROM vibeledger_prod_v1.transactions; SELECT count(*) AS snapshots FROM vibeledger_prod_v1.account_snapshots; SELECT count(*) AS investment_inputs FROM vibeledger_prod_v1.investment_period_inputs; SELECT count(*) AS statement_lines FROM vibeledger_prod_v1.statement_lines; SELECT count(*) AS occurrences FROM vibeledger_prod_v1.schedule_occurrences; SELECT count(*) AS ingestion_requests FROM vibeledger_prod_v1.ingestion_requests;"
psql 'service=vl-prod-admin' -X -v ON_ERROR_STOP=1 -c "SELECT has_schema_privilege('anon','vibeledger_prod_v1','USAGE') AS anon_access,has_schema_privilege('authenticated','vibeledger_prod_v1','USAGE') AS browser_access,has_schema_privilege('vibeledger_prod_runtime','vibeledger_prod_v1','CREATE') AS runtime_ddl,has_table_privilege('vibeledger_prod_runtime','vibeledger_prod_v1.audit_events','UPDATE') AS audit_update;"
```

All four privilege results must be false; financial counts must be zero before G2. Metadata commands may already have terminal ingestion receipts: inspect their `request_kind`, `operation` and `status`, require them to match approved account/category provisioning, and require no processing/needs-confirmation or financial-write receipts. Also inspect runtime privileges on **every existing non-system schema**: no old ledger/schema access may remain via PUBLIC/inherited grants. Verify schema absent from Supabase exposed-schema settings. Confirm readiness is not merely a running process; its Gemini key check is not a live Gemini extraction.

In the new browser origin, verify HTTPS, login/logout/current identity, correct account/category lists, uninitialized wealth, and household membership authorization. Check web's server-side backend target via the narrowly selected deployment configuration. Using headers held only in memory, repeat accounts GET with an old device token (401), a new token (200) and verify `/internal/spending-schedules/run` rejects browser/device tokens (401). If an existing unrelated verified user is available, verify household access is forbidden; do not create an unapproved identity for testing. Compare production `/openapi.json` with accepted staging; no legacy reconciliation endpoints may return success.

Optional live Gemini **no-ledger-write** diagnostic using a consented representative screenshot and production key loaded privately into the process environment:

```powershell
ai-ledger-backend/.venv/Scripts/python.exe ai-ledger-backend/scripts/smoke_expense_gemini.py --image $PrivateScreenshot --amount $ExpectedAmount --currency $ExpectedCurrency --date $ExpectedDate --merchant $ExpectedMerchant --revision
```

Run this only in a private non-recorded shell: screenshot facts are sensitive arguments. Keep `GEMINI_MODEL=gemini-3.5-flash-lite`. The diagnostic verifies live transport and local validation without DB writes; safe diagnostic metadata only. It does not replace hosted phone acceptance. No raw response, screenshot, prompt, DSN or token belongs in evidence committed to Git.

## 10. Ordered execution sequence

1. **G0:** approve this plan, shared-platform choice, private resource/input manifest; record exact preparation commit and image digests.
2. Export current service revision/configuration references, Scheduler settings and secret-version references privately. Do not mutate old environments.
3. **G1:** authorize new resources only. Verify target absent, operator privileges/capacity; run fresh schema then bootstrap; establish new credentials, restricted runtime grants and numeric secret versions.
4. Deploy backend, freeze URL, configure exact Scheduler audience; deploy web; freeze URL and Auth settings. Create/pause/test new Scheduler with zero schedules. Verify all section 9 gates and create approved account metadata/new devices.
5. Review both identities, account scope, app revisions, empty financial state and old pending-key terminal evidence.
6. **G2:** authorize client endpoint/token/state transition. Stop use of old capture clients, switch each client as section 7; no screenshot submissions yet. Leave old services/credentials intact.
7. **G3:** authorize initial real balance observations and one new real expense. Follow sections 8/11. Only then create real schedules and resume the new daily job. Confirm its next scheduled run/recovery behavior with receipts, not repeated manual financial tests.
8. Record production verification and remaining issues. Retain staging/old environments. S6 completion requires a separate execution record, not this preparation document.

## 11. First real production financial-write acceptance

Record the first balance-observation receipt/snapshot and verify UI account values, as-of times, debt signs and wealth totals. Then submit **one genuinely new purchase** from the production iPhone Shortcut with a fresh key; do not replay the old accepted screenshot/PDF. Resolve any genuine review blocker through the normal flow.

Recover by the same key and confirm terminal receipt identity, exactly one associated transaction and one creation audit event. If retry is needed, use the identical original request/key, never a reconstructed body or new UUID. Confirm transaction type, amount/currency/date/category/source merchant and owner. An expense must not fabricate a new balance observation. For statements later, preserve document/occurrence identity and whole-statement atomicity; the accepted 88-row statement is not seed data.

Take before/after count deltas scoped to this receipt/account; do not print raw financial rows. Verify a second recovery returns the same result without extra records. A pending/uncertain result is not a failed write: stop, recover it and use after-write rollback safeguards. Never insert synthetic test purchases and then delete them to declare acceptance.

## 12. Rollback before first financial write

First prove there are no financial rows (`transactions`, `account_snapshots`, `investment_period_inputs`, `statement_lines`, `schedule_occurrences`), no active capture and no pending/uncertain receipt. Pause only `vibeledger-prod-daily`. Restore each client’s original endpoint/token/pending-state namespace as a unit. Preserve the fresh schema, provisioning records and new credentials for diagnosis; no destructive down migration. Existing old services remain available. If any financial write or uncertain request exists, section 13 applies instead.

## 13. Rollback after first financial write

Pause the new Scheduler and stop production clients. Preserve production data, receipts, keys, audit evidence and image/config references in place. There is no accepted global write-disable switch. If stopping clients does not reliably fence writers, explicitly authorize this **new production role only** fence:

```sql
ALTER ROLE vibeledger_prod_runtime NOLOGIN;
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
WHERE usename='vibeledger_prod_runtime' AND pid <> pg_backend_pid();
```

NOLOGIN alone does not stop existing pooled sessions. Do not apply this to a shared/staging/old role. After fixing the incident, deploy a reviewed simplified-schema-compatible image against the preserved schema, verify read-only gates, then explicitly restore LOGIN and recover existing keys before resuming clients/job. A legacy ledger image is never a rollback candidate for this schema.

Returning clients to an old schema after new writes needs a separately approved export/recovery decision covering every new committed/uncertain receipt and financial record. Do not silently discard, dual-write or replay them with new keys. This is incident-specific handling of existing records, not a required disaster-recovery capability or rehearsal. No destructive down migration.

## 14. Explicit owner-approved S6 scope amendment

The owner explicitly decides that **VibeLedger does not require backup, PITR,
disaster recovery, backup retention, or restore rehearsal as part of this project**.
This intentional product/operational scope decision supersedes earlier S6 backup
and restore requirements. It is not an unresolved blocker, deferred execution gate,
or work that future agents should reintroduce. There are no backup commands,
RPO/RTO targets, provider backup/PITR checks, restore rehearsals or backup-retention
requirements for S6.

`psql` remains the intended operator tool for the fresh schema and bootstrap SQL
in sections 4–5. The schema design and application runtime remain unchanged.
Normal idempotency-key recovery, atomic writes, preserving existing live data and
receipts after writes, and application rollback boundaries still apply; these do
not establish a backup or disaster-recovery requirement.

Preserve accepted staging and old services until production is verified. Later
decommission requires confirmed client transition, no unresolved old keys and
separate owner authorization. No fixed backup/service retention period is required
by this amendment, and no deletion or credential revocation is authorized here.

## 15. Explicit operator authorization points

| Gate | Concrete approval |
|---|---|
| G0 | Reviewed plan/artifacts/private manifest; shared project/Auth/DB choice |
| G1 | Create only new production resources, operator DDL/bootstrap, auth additions and metadata/device provisioning |
| G2 | Switch each client's endpoint, new token and isolated pending-state storage after old-key recovery |
| G3 | First real balance/expense writes; resume new Scheduler only after accepted results |
| Incident | Any write fence, DB-secret switch or cross-schema record handling after writes |
| Later | Old environment decommission or credential/key revocation, separately authorized |

The current request authorizes preparation and repository changes only. None of these execution gates has been granted by preparing this file.

## 16. Remaining blockers and unknowns

- Owner approval of proposed production names and shared-platform boundaries; actual production database host/project/operator and target absence.
- Private verified Auth identities and membership roles; approved account/scope list, start date and reporting currency; phone/Shortcut versions and pending-state inventory.
- Effective DB role privileges (including PUBLIC), extensions, provider connection budget.
- Approved Gemini billing/quota project/key, production secret numeric versions, deploy/IAM permissions and image availability at execution time.
- New service URLs/revisions and Auth redirect configuration, which cannot be known until G1 deployment; freeze and review before G2.
- Old-key drainage, live no-write checks, first-write acceptance and execution authorizations remain outstanding.

## 17. Readiness

**Ready for plan review; not ready for production execution. S6 remains incomplete.** This sheet provides an ordered path and a separate production operator SQL path without relaxing test guards. Resolve the pre-provisioning inputs in section 16 and obtain G0/G1 before creating hosted resources. Deployment-generated URLs/revisions and live checks are later G2 prerequisites, not prerequisites for creating those new services. The accepted S5 scope is preserved; no additional broad manual test campaign is inferred.

Preparation validation (2026-09-21): both accepted registry digests resolved successfully; accepted S5/main application diff was empty. The repeatable local PostgreSQL 17 rehearsal passed explicit authorization/database/operator refusal, missing-extension rollback, fresh creation, invalid-identity refusal, two-member/category bootstrap, repeat-execution refusal, empty financial tables and restricted runtime/browser-role privileges:

```powershell
ai-ledger-backend/.venv/Scripts/python.exe -X utf8 ai-ledger-backend/scripts/smoke_s6_operator_sql.py
git diff --check
```

The rehearsal creates and removes its own disposable Docker container and accepts no hosted DSN. These results do not prove Supabase administrative privileges or production capacity. No production mutation or live financial acceptance was performed. Backend/Web application CI was not rerun for this preparation-only documentation/operator-SQL change; no runtime, migration baseline or frontend behavior changed.

Platform references checked while preparing: [Cloud Run immutable deployments](https://docs.cloud.google.com/run/docs/deploying), [secret bindings](https://docs.cloud.google.com/run/docs/configuring/services/secrets), [Scheduler OIDC](https://docs.cloud.google.com/scheduler/docs/http-target-auth), [Supabase connection modes](https://supabase.com/docs/guides/database/connecting-to-postgres), [pooling limits](https://supabase.com/docs/guides/database/connecting-to-postgres/pooling-and-limits), [custom schema exposure](https://supabase.com/docs/guides/api/using-custom-schemas). Recheck effective configuration before execution; documentation does not establish this project's private state.
