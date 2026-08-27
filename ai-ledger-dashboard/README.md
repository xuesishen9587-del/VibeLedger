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

# VibeLedger Dashboard (Phase 11)

VibeLedger Dashboard is a presentation-layer Streamlit frontend that communicates exclusively with the backend service via authenticated `/api/v1/*` REST APIs.

---

## Key Characteristics

1. **Zero Database Access**: The Dashboard maintains no direct PostgreSQL connections, holds no database credentials, and executes no SQL queries.
2. **REST API Client**: All business operations, domain calculations, reconciliation flows, and asset/liability aggregations are performed by the backend service.
3. **Browser Authentication**: Uses JWT-based Browser Authentication (`Authorization: Bearer <JWT>`) aligned with Phase 10 specifications.
4. **Optimistic Concurrency**: Supports full preview and commit workflows for transaction corrections and reconciliation batches with row version conflict detection.

---

## Configuration

Set the following environment variables:
- `BACKEND_URL`: URL of the VibeLedger Backend service (defaults to `http://localhost:8000`).
- `AUTH_TOKEN`: Optional default Browser JWT Token for authentication.

---

## Running the Dashboard

```bash
pip install -r requirements.txt
streamlit run app.py
```