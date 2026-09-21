# S5 acceptance evidence

Status: implementation under final validation. This file is updated with exact
commits, CI, deployed digests/revisions and scheduler evidence before S5 completion.

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
