# VibeLedger Staging Deployment Runbook

> **Phase 11.5 — Pre-production Deployment & Runtime Readiness**  
> **Target Environment**: Isolated Disposable Staging (`ENVIRONMENT=staging`)  
> **Authority**: `TARGET_DOMAIN_MODEL.md`, `docs/architecture/PHYSICAL_SCHEMA.md`, `docs/architecture/API_CONTRACT.md`

This document provides the complete, copy-pasteable operational runbook to deploy and verify an isolated VibeLedger Staging environment prior to Phase 12 real-device acceptance testing.

---

## 0. Safety Invariants

1. **Zero Production Contamination**: Staging must NEVER connect to production PostgreSQL instances, production schemas (`vibeledger_target`, `public`), or production identity providers.
2. **Strangler Docker Boundary**: Legacy production uses `Dockerfile` (`uvicorn main:app`). Staging uses `Dockerfile.target` (`uvicorn app.main:app`).
3. **HS256 HMAC Signing for Staging Auth**: Staging uses a symmetric shared secret for Browser JWT signing/verification (`AUTH_ALGORITHMS=["HS256"]`, secret min 32 chars). Production authentication architecture remains untouched.
4. **No Historical / Opening Balance Ingestion**: Staging bootstrap sets `account_state.initialized_at = NULL` and creates zero fake transactions.
5. **Private Staging Config Isolation**: Never put private account details in tracked Git files. Use uncommitted `scripts/staging_seed.local.json`.
6. **Device-Local Token Storage**: Staging device token and pending idempotency state are stored exclusively in device-local storage (`On My iPhone/VibeLedger/`). Never use iCloud Drive.

---

## 1. Staging Setup Runbook (Steps 1 – 15)

### Step 1: Provision Isolated Staging Database
Create a dedicated PostgreSQL database instance or an isolated schema within your Supabase project (e.g. `vibeledger_staging`).
Ensure required extensions (`pgcrypto`, `pg_trgm`, `citext`) are installable by the database user.

### Step 2: Configure Staging Environment Variables
In `ai-ledger-backend/`, prepare the staging `.env` file:

```bash
cat << "EOF" > .env
ENVIRONMENT=staging
DATABASE_URL=postgresql://postgres.staging:<PASSWORD>@aws-1-region.pooler.supabase.com:6543/postgres
DB_SCHEMA=vibeledger_staging
GEMINI_API_KEY=<STAGING_GEMINI_API_KEY>

# HS256 HMAC Signing Configuration for Staging Browser Auth (Min 32 characters)
AUTH_PUBLIC_KEY=<HIGH_ENTROPY_STAGING_SHARED_SECRET_AT_LEAST_32_CHARS>
AUTH_ALGORITHMS=["HS256"]
AUTH_ISSUER=vibeledger-staging
AUTH_AUDIENCE=vibeledger-api
AUTH_JWKS_URL=

MAX_EXPENSE_IMAGE_BYTES=10485760
MAX_STATEMENT_PDF_BYTES=20971520
FX_API_BASE_URL=https://api.frankfurter.app
FX_HTTP_TIMEOUT_SECONDS=5.0

# Staging Bootstrap Identity (Single source of truth in environment)
STAGING_LEDGER_START_DATE=2026-08-01
STAGING_OWNER_AUTH_SUBJECT=staging_owner_user
EOF
```

### Step 3: Execute Target Database Migrations
Run the target migration runner against the staging database schema:

```bash
cd ai-ledger-backend
ENVIRONMENT=staging \
DATABASE_URL="postgresql://postgres.staging:<PASSWORD>@aws-1-region.pooler.supabase.com:6543/postgres" \
DB_SCHEMA=vibeledger_staging \
python -m migrations.runner
```

*Expected Output*:
```text
LOG: Starting database migrations for schema: vibeledger_staging
RUN: Applying migration '0001_extensions.sql'...
SUCCESS: Successfully applied migration '0001_extensions.sql'
...
SUCCESS: Successfully applied migration '0009_indexes.sql'
```

### Step 4: Verify Migration Idempotency & Checksums
Rerun the migration command to verify idempotency and immutability:

```bash
python -m migrations.runner
```

*Expected Output*:
```text
SKIP: Migration '0001_extensions.sql' is already applied (checksum verified).
...
SKIP: Migration '0009_indexes.sql' is already applied (checksum verified).
```

### Step 5: Bootstrap Staging Identity, Accounts & Categories
1. Copy the sanitized example seed template to a local, untracked configuration file:
   ```bash
   cp scripts/staging_seed.example.json scripts/staging_seed.local.json
   ```
2. Edit `scripts/staging_seed.local.json` to customize account names, currencies, and categories for your staging testing.
3. Run the bootstrap tool pointing to your local configuration:
   ```bash
   python scripts/bootstrap_staging.py --config scripts/staging_seed.local.json
   ```

*Expected Output*:
```json
{
  "household_id": "<HOUSEHOLD_UUID>",
  "owner_user_id": "<OWNER_UUID>",
  "accounts_created": 5,
  "accounts_verified": 0,
  "aliases_created": 10,
  "aliases_verified": 0,
  "categories_created": 11,
  "categories_verified": 0
}
```
*Note: Rerunning this command validates natural keys and verifies consistency without creating duplicate entities.*

### Step 6: Generate Staging Browser JWT
Generate an HS256 HMAC-signed staging Browser JWT for the staging owner (reads `STAGING_OWNER_AUTH_SUBJECT` and `AUTH_PUBLIC_KEY` directly from environment):

```bash
python scripts/generate_staging_browser_token.py --exp-hours 168
```

*Expected Output*:
```text
================================================================================
  VIBELEDGER STAGING BROWSER JWT (HS256 HMAC SIGNED — STAGING ONLY)             
================================================================================
Subject (sub) : staging_owner_user
Expires In    : 168 hours
Issuer (iss)  : vibeledger-staging
Audience (aud): vibeledger-api
--------------------------------------------------------------------------------
Generated Token:
<STAGING_BROWSER_JWT>
================================================================================
```

### Step 7: Build & Deploy Target Backend Container
Deploy the backend using `Dockerfile.target` to your staging host (e.g. Hugging Face Spaces or Docker):

```bash
# Docker local build & run example:
docker build -f Dockerfile.target -t vibeledger-backend-staging .
docker run -d -p 7860:7860 --env-file .env vibeledger-backend-staging
```

### Step 8: Verify Health Endpoint
Query the `/health` endpoint to verify service identity:

```bash
curl -i https://<STAGING_BACKEND_HOST>/health
```

*Expected Response*:
```http
HTTP/1.1 200 OK
Content-Type: application/json

{"status": "ok", "service": "vibeledger-api", "version": "1.0.0"}
```

### Step 9: Verify Authoritative Readiness Endpoint
Query the `/ready` endpoint to verify database schema readiness and Gemini configuration:

```bash
curl -i https://<STAGING_BACKEND_HOST>/ready
```

*Expected Response*:
```http
HTTP/1.1 200 OK
Content-Type: application/json

{"status": "ok", "database": "ok", "gemini": "ok"}
```
*(Full Phase 11.5 READY status requires `database=ok` AND `gemini=ok`).*

### Step 10: Deploy & Configure Staging Dashboard
Build and start the Dashboard container pointing exclusively to the staging Backend REST URL:

```bash
cd ../ai-ledger-dashboard
docker build -t vibeledger-dashboard-staging .
docker run -d -p 8501:8501 \
  -e BACKEND_URL=https://<STAGING_BACKEND_HOST> \
  vibeledger-dashboard-staging
```
*Note: Dashboard requires NO database credentials (`DATABASE_URL`).*

### Step 11: Authenticate Dashboard Session
1. Open `http://<STAGING_DASHBOARD_HOST>:8501`.
2. In the left sidebar expander **🔑 会话认证配置**, paste the `<STAGING_BROWSER_JWT>` generated in Step 6.
3. Click **更新会话 Token**.
4. Confirm Dashboard loads account overview and categories without error.

### Step 12: Provision Staging iPhone Device
Use the authenticated Browser JWT to provision a staging device token via REST API:

```bash
curl -X POST https://<STAGING_BACKEND_HOST>/api/v1/devices \
  -H "Authorization: Bearer <STAGING_BROWSER_JWT>" \
  -H "Content-Type: application/json" \
  -d '{
    "device_name": "Staging Tester iPhone",
    "platform": "ios_shortcuts",
    "client_version": "ios-shortcut-2.0"
  }'
```

*Expected Response*:
```json
{
  "device": {
    "device_id": "<DEVICE_UUID>",
    "user_id": "<OWNER_UUID>",
    "device_name": "Staging Tester iPhone",
    "platform": "ios_shortcuts",
    "status": "active",
    "client_version": "ios-shortcut-2.0",
    "created_at": "2026-08-28T15:55:00.000000+00:00",
    "last_seen_at": null,
    "revoked_at": null
  },
  "token": "devtok_<RANDOM_HIGH_ENTROPY_TOKEN_BYTES>"
}
```

### Step 13: Store Device Token in Device-Local Storage
1. On the test iPhone, create the directory `VibeLedger` under local storage:
   `On My iPhone/VibeLedger/` (do NOT use iCloud Drive).
2. Save the returned `token` string into:
   `On My iPhone/VibeLedger/device-token.txt`
3. In Phase 12, the iOS Shortcut reads the token dynamically from this file, keeping tokens isolated per device and preventing hardcoding secrets in Shortcuts.
4. The Shortcut pending idempotency state is also maintained locally in `On My iPhone/VibeLedger/pending-request.json`.

### Step 14: Test Device Bearer Authentication
Verify the provisioned device token on a safe read-only target endpoint:

```bash
curl -i https://<STAGING_BACKEND_HOST>/api/v1/accounts \
  -H "Authorization: Bearer devtok_<RANDOM_HIGH_ENTROPY_TOKEN_BYTES>"
```

*Expected Response*:
```http
HTTP/1.1 200 OK
Content-Type: application/json

{"items": [...]}
```

### Step 15: Declare Staging Ready for Phase 12
Once steps 1–14 have passed with live evidence, Phase 11.5 is fully complete and Phase 12 real-device Shortcut testing is unblocked.

---

## 2. Troubleshooting & Recovery

| Issue | Root Cause | Solution |
| :--- | :--- | :--- |
| `503 schema_not_ready` on `/ready` | Migrations missing or checksum mismatch in `schema_migrations` | Run `python -m migrations.runner` to apply missing scripts. |
| `401 Invalid browser credentials` | `AUTH_PUBLIC_KEY` secret mismatch or expired token | Regenerate token with `generate_staging_browser_token.py` using matching `AUTH_PUBLIC_KEY`. |
| `BootstrapConsistencyError` on bootstrap | Existing database row attributes conflict with seed config | Check conflicting field reported in error message; ensure natural keys are consistent. |
| `PermissionError: Execution is forbidden in production` | `ENVIRONMENT=production` set during staging commands | Verify `ENVIRONMENT=staging` is explicitly exported. |
