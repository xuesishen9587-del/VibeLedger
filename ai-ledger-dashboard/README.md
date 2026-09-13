---
title: Ai Ledger Dashboard
emoji: 🚀
colorFrom: red
colorTo: red
sdk: streamlit
tags:
- streamlit
pinned: false
short_description: Streamlit ledger dashboard
license: mit
app_file: app.py
---

# VibeLedger Dashboard

VibeLedger Dashboard is a presentation-layer Streamlit frontend that communicates exclusively with the backend service via authenticated `/api/v1/*` REST APIs.

The Dashboard has four pages: Wealth, Spending, Review, and Settings. It includes
dated balances, monthly spending schedules, selected-account statement import,
investment interval estimates/confirmed flows, and email/password login through
Supabase Auth. See [login setup and hosted acceptance](../docs/deployment/SUPABASE_AUTH_SETUP.md).
S3/S4 hosted household and independent acceptance remain outstanding.

---

## Key Characteristics

1. **Zero Database Access**: The Dashboard maintains no direct PostgreSQL connections, holds no database credentials, and executes no SQL queries.
2. **REST API Client**: All financial operations, domain calculations, and asset/liability aggregations are performed by the backend service.
3. **Browser Authentication**: Uses per-session Supabase Auth login, refresh and logout; the backend verifies signed tokens and household membership.
4. **Optimistic Concurrency**: Version checks and durable command keys protect edits and interrupted saves.

---

## Configuration

Set the following environment variables:
- `BACKEND_URL`: URL of the VibeLedger Backend service (defaults to `http://localhost:8000`).
- `SUPABASE_URL`: HTTPS URL of the configured Supabase project.
- `SUPABASE_PUBLISHABLE_KEY`: Public project key beginning `sb_publishable_`; never a secret/service-role key.
- `DASHBOARD_TIMEZONE`: IANA timezone used for local date/time display and snapshot timestamp generation (defaults to `Asia/Singapore`).

---

## Running the Dashboard

```bash
pip install -r requirements.txt
streamlit run app.py
```
