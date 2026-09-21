# Isolated S34 staging runbook

Current S6 preparation: [production cutover sheet](S6_PRODUCTION_CUTOVER.md).
The S5 record below is historical; production execution remains unauthorized.

## Authorized target

Project `vibeledger-staging`, region `asia-southeast1`.
Only `vibeledger-s34acc-backend` and `vibeledger-s34acc-web` receive S5 images.
Schema: `vibeledger_s34acc_20260914`, simplified lineage, unchanged baseline.
Preserve old services, data, identities and secrets. S6/production cutover and merge
require separate authorization. Active UI is the accepted React app; retained
Streamlit is a fallback and is not redeployed here.

## Build and deploy

1. Run Backend CI and Web CI. Generate/check OpenAPI with
   `python scripts/export_openapi.py --check` from `ai-ledger-backend`.
2. Build backend using `ai-ledger-backend/Dockerfile` and `cloudbuild.yaml` with
   `_IMAGE` set to the isolated backend repository plus the reviewed commit SHA.
   Build web using `ai-ledger-web/Dockerfile`. Both contexts exclude local files.
3. Record immutable registry digests before deploying. Use `gcloud run deploy`
   with explicit project, region, existing service and digest. Preserve existing
   secret/config bindings. Do not run a fresh bootstrap or replace the schema.
4. Keep backend concurrency 8 and maximum 2 instances for this household staging
   candidate (at most 16 concurrent handlers; ordinarily one active DB session per
   handler, with headroom for operator tasks). Raise only after checking the
   provider's actual connection allowance; do not assume the Cloud Run default 80.
5. Verify `/health`, `/ready`, generated `/openapi.json`, unauthorized financial
   routes, retired routes returning 404, and web `/health`/login.
6. Record exact ready revisions, image digests, traffic and schema in S5_ACCEPTANCE.
   Existing ES256/JWKS settings and `gemini-3.5-flash-lite` remain unchanged.

## Daily schedules

- Cloud Scheduler API enabled in this project.
- Dedicated SA: `vibeledger-s34acc-scheduler@vibeledger-staging.iam.gserviceaccount.com`.
- Grant `roles/run.invoker` on **only** the selected backend. No owner/editor or
  database role is granted to this identity. The Google-managed Scheduler service
  agent uses its normal service-agent role to mint OIDC tokens.
- Job `vibeledger-s34acc-daily`, location `asia-southeast1`: `15 0 * * *`,
  timezone `Asia/Singapore`, POST `{}` to the backend's
  `/internal/spending-schedules/run`.
- Set exact OIDC audience and backend `SCHEDULER_AUDIENCE` to the canonical backend
  service URL. Set `SCHEDULER_SERVICE_ACCOUNT` to the dedicated email above.
- Attempt deadline 180 seconds; retry count 5; minimum backoff 30 seconds, maximum
  300 seconds; retry duration 3600 seconds. Each household computes its own local
  date even though the trigger runs at Singapore 00:15.
- Trigger twice with `gcloud scheduler jobs run` for acceptance and check completed
  Scheduler logs plus backend request statuses. Never copy tokens into diagnostics.
- Partial process interruption is safe: committed period receipts are replayed;
  unfinished periods resume next attempt/day. No separate worker.
- If the job fails, inspect Scheduler execution status, Cloud Run readiness and
  sanitized error types. Restore service/config, then run the same job. Browser
  Spending/Review also performs a durable catch-up and exposes failure/retry.
  Report GETs never create financial records.

OIDC implementation follows [Google's HTTP target authentication guidance](https://docs.cloud.google.com/scheduler/docs/http-target-auth)
and [Google Auth token verification](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.id_token.html).

## Backup/restore and rollback

Use the database provider's protected backup/PITR, or PostgreSQL 17 `pg_dump` with
credentials supplied through a protected environment/secret mechanism. Scope to
the selected private schema when sharing a database. Keep backups encrypted with
restricted access; never put dumps, connection strings or command output containing
financial records in Git/CI logs. Record timestamp and baseline checksum separately.

Restore into a **new isolated database**, never over the accepted schema. Install
required extensions, restore schema/data/constraints/triggers, verify migration
checksum and counts/identities for ingestion_requests, statement_lines,
schedule_occurrences, transactions and audit_events. Check stored response payloads,
actor/key scopes, document hashes, period numbers and statement transaction links.
Replay original keys/import confirmations and the daily job; counts/identities must
stay unchanged. Do not reset receipts or regenerate IDs to make restore succeed.

`scripts/container_smoke.py` automates an actual PostgreSQL dump/restore on disposable
containers using `scripts/s5_restore_fixture.py`. It compares full-row fingerprints
before/after restoring, then retries the original statement key/confirmation and
schedule run. It also verifies startup with Gemini absent. This proof contains only
synthetic data; it is not a claim that a production backup was taken.

For application rollback, pause the daily Scheduler job first and route the two
selected services back to the recorded prior accepted revisions. No schema changes
are required by S5. Preserve newly created receipts/data. Investigate with read-only
queries and restore into a separate recovery candidate if needed. Never drop the
accepted schema or modify the old hosted services as a rollback shortcut.
