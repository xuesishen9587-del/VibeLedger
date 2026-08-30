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
6. **Device-Local Token & Pending Key Storage**: Staging device token and pending idempotency state are stored exclusively in device-local storage (`On My iPhone/VibeLedger/device-token.txt` and `On My iPhone/VibeLedger/pending-key.txt`). Never use iCloud Drive. Pending state is plain text, not JSON.

---

## 1. Staging Setup Runbook (Steps 1 – 15)

### Step 1: Provision Isolated Staging Database
Create a dedicated PostgreSQL database instance or an isolated schema within your Supabase project (e.g. `vibeledger_staging`).
Use the verified Supabase Session Pooler endpoint on port `5432` with SSL enabled (`sslmode=require`):
`aws-1-ap-southeast-1.pooler.supabase.com:5432`

Ensure required extensions (`pgcrypto`, `pg_trgm`, `citext`) are installable by the database user.

### Step 2: Configure Staging Environment Variables
In `ai-ledger-backend/`, prepare the staging `.env` file:

```bash
cat << "EOF" > .env
ENVIRONMENT=staging
DATABASE_URL=postgresql://postgres.<PROJECT_REF>:<PASSWORD>@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require
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

# Operator-Side Staging Bootstrap Configuration (Used only for local scripts: bootstrap_staging.py, generate_staging_browser_token.py)
STAGING_LEDGER_START_DATE=2026-08-01
STAGING_OWNER_AUTH_SUBJECT=staging_owner_user
EOF
```

### Step 3: Execute Target Database Migrations
Run the target migration runner against the staging database schema:

```bash
cd ai-ledger-backend
ENVIRONMENT=staging \
DATABASE_URL="postgresql://postgres.<PROJECT_REF>:<PASSWORD>@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require" \
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

*Initial Bootstrap Expected Output*:
```json
{
  "household_id": "<HOUSEHOLD_UUID>",
  "owner_user_id": "<OWNER_UUID>",
  "accounts_created": 5,
  "accounts_verified": 0,
  "aliases_created": 12,
  "aliases_verified": 0,
  "categories_created": 11,
  "categories_verified": 0
}
```

*Rerun Verification Expected Output (Idempotent Consistency)*:
```json
{
  "household_id": "<HOUSEHOLD_UUID>",
  "owner_user_id": "<OWNER_UUID>",
  "accounts_created": 0,
  "accounts_verified": 5,
  "aliases_created": 0,
  "aliases_verified": 12,
  "categories_created": 0,
  "categories_verified": 11
}
```

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

#### Authoritative Staging Runtime
- **Platform**: Google Cloud Run
- **Region**: `asia-southeast1`
- **Docker Image**: Built from `Dockerfile.target`
- **Container Port**: `7860`
- **Runtime Environment Variables**:
  - `ENVIRONMENT=staging`
  - `DB_SCHEMA=vibeledger_staging`
  - `AUTH_ALGORITHMS=["HS256"]`
  - `AUTH_ISSUER=vibeledger-staging`
  - `AUTH_AUDIENCE=vibeledger-api`
  - `MAX_EXPENSE_IMAGE_BYTES=10485760`
  - `MAX_STATEMENT_PDF_BYTES=20971520`
  - `FX_API_BASE_URL=https://api.frankfurter.app`
  - `FX_HTTP_TIMEOUT_SECONDS=5.0`
  *(Note: `STAGING_LEDGER_START_DATE` and `STAGING_OWNER_AUTH_SUBJECT` are operator-side staging variables used exclusively for local bootstrap and token generation scripts; they are not required by the continuously running target API service).*
- **Secret Injection (Google Cloud Secret Manager)**:
  - `DATABASE_URL` (Supabase Session Pooler `...:5432/postgres?sslmode=require`)
  - `GEMINI_API_KEY`
  - `AUTH_PUBLIC_KEY`
  *(All secrets injected directly into Cloud Run container environment; never committed to repo).*

#### Local Container Smoke Test (Optional)
```bash
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

#### Authoritative Staging Runtime
- **Platform**: Google Cloud Run
- **Region**: `asia-southeast1`
- **Docker Image**: Built from `ai-ledger-dashboard/Dockerfile`
- **Container Port**: `8501`
- **Configured Environment Variables**:
  - `BACKEND_URL=https://<STAGING_BACKEND_HOST>`
  - `DASHBOARD_TIMEZONE=Asia/Singapore`
- **Strict Architecture Invariant — Zero Direct Database Access**:
  Dashboard has **zero direct PostgreSQL access** and operates strictly via backend REST API. The following secrets and environment variables are **STRICTLY PROHIBITED** from being injected into the Dashboard:
  - `DATABASE_URL`
  - `GEMINI_API_KEY`
  - `AUTH_PUBLIC_KEY`
  - `AUTH_TOKEN`

#### Local Dashboard Smoke Test (Optional)
```bash
cd ../ai-ledger-dashboard
docker build -t vibeledger-dashboard-staging .
docker run -d -p 8501:8501 \
  -e BACKEND_URL=https://<STAGING_BACKEND_HOST> \
  -e DASHBOARD_TIMEZONE=Asia/Singapore \
  vibeledger-dashboard-staging
```

### Step 11: Authenticate Dashboard Session
1. Open `https://<STAGING_DASHBOARD_HOST>`.
2. In the left sidebar expander **🔑 会话认证配置**, paste the `<STAGING_BROWSER_JWT>` generated in Step 6.
3. Click **更新会话 Token**.
4. Confirm Dashboard loads account overview and categories via backend REST API without error.

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
  "token": "<OPAQUE_DEVICE_TOKEN>"
}
```

### Step 13: Store Device Token & Pending Key in Device-Local Storage
1. On the test iPhone, create the directory `VibeLedger` under local storage:
   `On My iPhone/VibeLedger/` (do NOT use iCloud Drive).
2. Save the returned high-entropy opaque bearer `token` string into:
   `On My iPhone/VibeLedger/device-token.txt`
3. In Phase 12, the iOS Shortcut reads the token dynamically from this file, keeping tokens isolated per device and preventing hardcoding secrets in Shortcuts.
4. The Shortcut pending idempotency state is also maintained locally in:
   `On My iPhone/VibeLedger/pending-key.txt`
   *(Note: Pending state is plain text key storage, not JSON. Do not alter format).*

### Step 14: Test Device Bearer Authentication
Verify the provisioned device token on a safe read-only target endpoint:

```bash
curl -i https://<STAGING_BACKEND_HOST>/api/v1/accounts \
  -H "Authorization: Bearer <OPAQUE_DEVICE_TOKEN>"
```

*Expected Response*:
```http
HTTP/1.1 200 OK
Content-Type: application/json

{"items": [...]}
```

### Step 15: Declare Staging Gate Complete

```text
PHASE 11.5 STAGING DEPLOYED AND READY FOR PHASE 12
```

> [!IMPORTANT]
> This status declaration confirms exclusively that the **Phase 11.5 Staging Runtime & Environment Gate** has been deployed, verified, and accepted.
> - It **does NOT** mean Phase 12 Shortcut expense `POST` acceptance testing is completed.
> - It **does NOT** mean production cutover has been performed.

---

## 2. Completed Runtime Acceptance Evidence

All Phase 11.5 staging runtime verification gates have been executed and passed. No active secrets or credentials are recorded in this runbook:

| Step / Check Item | Acceptance Target | Result | Evidence / Notes |
| :--- | :--- | :--- | :--- |
| **Local Container Build** | `Dockerfile.target` build | **PASS** | Clean build with target entrypoint (`uvicorn app.main:app`) |
| **Local Service Health** | `GET /health` | **PASS** | HTTP 200 `{"status": "ok", "service": "vibeledger-api", "version": "1.0.0"}` |
| **Local Service Readiness** | `GET /ready` | **PASS** | HTTP 200 `{"status": "ok", "database": "ok", "gemini": "ok"}` |
| **Cloud Run External Health** | HTTPS `GET /health` | **PASS** | HTTP 200 on Google Cloud Run `asia-southeast1` external endpoint |
| **Cloud Run External Readiness** | HTTPS `GET /ready` | **PASS** | HTTP 200 `{"status": "ok", "database": "ok", "gemini": "ok"}` |
| **Browser JWT Auth** | `GET /api/v1/accounts` | **PASS** | HTTP 200 with HS256 HMAC staging Browser JWT |
| **Device Provisioning** | `POST /api/v1/devices` | **PASS** | HTTP 201 Created returning device record and opaque bearer token |
| **Fresh Device Bearer Auth** | `GET /api/v1/accounts` | **PASS** | HTTP 200 authenticated via provisioned `<OPAQUE_DEVICE_TOKEN>` |
| **iPhone Local Storage** | `device-token.txt` read | **PASS** | Verified local file read from `On My iPhone/VibeLedger/device-token.txt` |
| **iPhone Cellular Network Auth** | `GET /api/v1/accounts` | **PASS** | HTTP 200 authenticated request over iOS cellular network |
| **Staging Dashboard API Access** | Dashboard UI | **PASS** | Cloud Run Dashboard verified loading accounts/categories via Backend REST API |

---

## 3. Troubleshooting & Recovery

| Issue | Root Cause | Solution |
| :--- | :--- | :--- |
| `503 schema_not_ready` on `/ready` | Migrations missing or checksum mismatch in `schema_migrations` | Run `python -m migrations.runner` to apply missing scripts. |
| `401 Invalid browser credentials` | `AUTH_PUBLIC_KEY` secret mismatch or expired token | Regenerate token with `generate_staging_browser_token.py` using matching `AUTH_PUBLIC_KEY`. |
| `BootstrapConsistencyError` on bootstrap | Existing database row attributes conflict with seed config | Check conflicting field reported in error message; ensure natural keys are consistent. |
| `PermissionError: Execution is forbidden in production` | `ENVIRONMENT=production` set during staging commands | Verify `ENVIRONMENT=staging` is explicitly exported. |
