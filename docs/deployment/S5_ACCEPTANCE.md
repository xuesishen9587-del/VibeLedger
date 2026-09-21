# S5 acceptance evidence

Status: **S5 complete for isolated staging under the owner-approved manual scope.**
No known critical integrity/auth/recovery blocker remains. PR #17 is ready for
review; it is not merged. S6/production cutover remains unauthorized.


Accepted starting commit: `278f143ab3fa4ab24f5a543a11f8405fce051ebf`.
Manual boundary: user-attested single-user acceptance, one clear expense Shortcut
capture, real MariBank 88-row PDF; second-user/20-capture goals explicitly waived
by the owner on 2026-09-20. Shortcut version was not recorded. No further manual
sampling is required by that instruction. S6 remains unauthorized.

## Local regression results

- Backend unit: 132 passed, including signed OIDC issuer/audience/email/expiry/signature rejection.
- Backend integration: 205 passed, including household timezone and interrupted-job/read-only-freshness cases.
- Migration: 5 passed; concurrency: 9 passed.
- Retained fallback Dashboard: 78 passed.
- Web unit: 14 passed; server: 3 passed; browser: 17 passed.
- Browser → same-origin proxy → JWT backend → PostgreSQL: 1 passed.
- Local Docker Hub image resolution failed (EOF); the same Dockerfile built successfully
  in Cloud Build. Container CI verifies the exact image; local PostgreSQL dump/restore
  and original receipt/statement/schedule replay fingerprints passed.

See DEPLOYMENT_READINESS.md for retired-runtime replacement coverage and
STAGING_DEPLOYMENT.md for operation/recovery instructions.


## Committed implementation and CI

Implementation: `522dabb14fd278b01aef0a02f5ded609b29a3ed4`.
The follow-up acceptance commit changes documentation and strengthens the container
wrong-schema probe only; deployed application source is identical to this commit.

All implementation CI runs succeeded:
[PR Backend CI](https://github.com/xuesishen9587-del/VibeLedger/actions/runs/35550676213),
[push Backend CI](https://github.com/xuesishen9587-del/VibeLedger/actions/runs/35550673964),
[PR Web CI](https://github.com/xuesishen9587-del/VibeLedger/actions/runs/35550676245),
[push Web CI](https://github.com/xuesishen9587-del/VibeLedger/actions/runs/35550673944).
Backend container smoke passed actual built-image imports/probes, PostgreSQL
backup/restore and original-key replay. Web CI passed the real API/PostgreSQL
browser test and built Web image smoke. The latest follow-up SHA's checks remain
visible on PR #17; do not infer them from this implementation-SHA evidence.

## Exact isolated staging evidence — 2026-09-21

Project `vibeledger-staging`, region `asia-southeast1`, 100% traffic to:

| Service | Ready revision | Immutable image digest |
|---|---|---|
| vibeledger-s34acc-backend | vibeledger-s34acc-backend-00008-jcg | sha256:3bf0dea587d36a1d8ade7b03b8936e2119573d0be664bfd908326ccb7b991fb0 |
| vibeledger-s34acc-web | vibeledger-s34acc-web-00005-lbz | sha256:10e09cb4603fe45779286022072250eea6cf30e4a405df0608b7d12fdf671cc3 |

Image registry prefix: `asia-southeast1-docker.pkg.dev/vibeledger-staging/vibeledger-staging/`;
image repository names match service names. Build IDs:
backend `b14de9ce-318a-42e5-8838-69894b290b5c`, web `43f48c12-bacb-4bf5-a9be-abbcf347854b`.

- Schema `vibeledger_s34acc_20260914`, unchanged simplified baseline (16 application tables).
- `0001_simplified.sql` SHA256: `bf8cc2ea6c46dd9ce66389784e499c1bbd7ff756ef532d1d9b23aa83d186651c`.
- Hosted `/ready`: 200, database ok, Gemini ok; readiness validates the migration checksum.
- `GEMINI_MODEL=gemini-3.5-flash-lite`; existing ES256/JWKS configuration preserved.
- Backend concurrency 8, maximum instances 2; no extra worker service.
- Hosted OpenAPI equals the checked-in generated contract exactly.
- Finance without credentials: 401. Internal run without credentials/with invalid
  device token: 401. Retired reconciliation/work-queue paths: 404.
- Hosted web `/health`, entry HTML and public-only `/config`: 200.

## Daily operation acceptance

`vibeledger-s34acc-daily` is ENABLED, daily Singapore 00:15, dedicated SA with only
selected-service run.invoker, exact backend URL audience and bounded retries.
Two manual trigger requests were issued. Scheduler recorded successful 200 deliveries
at `2026-09-21T01:27:23.538957427Z`, `01:27:53.318689420Z`,
`01:28:24.983147381Z`, and `01:28:31.232805984Z`; final job status is success (`{}`).
At-least-once delivery is expected. Duplicate/missed-day/partial-run financial behavior
is proved by the DB and restore tests; HTTP success alone is not claimed as that proof.
Next scheduled run at inspection: `2026-09-21T16:15:04.925155Z`.

Unchanged old hosted revisions: backend-staging `00003-m7n`, dashboard-staging
`00001-rql`, s2fr-backend `00001-55q`, s34acc-dashboard `00001-vsp`.
No old service, accepted schema, household record or production environment was reset.
The dump/restore proof used synthetic local/CI data, not a production-data backup.

## Limits accepted by the owner

One-user/one-clear-capture sampling is narrower than the original plan's two-user/
20-sample target. The user explicitly accepts this; it is not hidden or reported as
full sample completion. Exact Shortcut release identifier was not provided. No new
live Gemini calls or new household manual tests were necessary for S5 runtime changes.
