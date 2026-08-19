# VibeLedger API Contract

> Status: **Frozen Target API Contract (Final consistency review complete)**
>
> Authority:
>
> 1. `TARGET_DOMAIN_MODEL.md` — business source of truth  
> 2. `docs/architecture/PHYSICAL_SCHEMA.md` — persistence contract  
> 3. This document — public application/API contract
>
> Scope: Product v1 target API. Legacy `/api/record` is not the target interface.
>
> Core rule: **Backend is the only business-rule layer. Dashboard and iOS Shortcuts never write PostgreSQL directly.**

---

# 1. API Conventions

## 1.1 Base path

All target endpoints use:

```text
/api/v1
```

Example:

```text
POST /api/v1/expenses
GET  /api/v1/accounts
```

Legacy endpoints may temporarily coexist during migration, but new clients MUST use `/api/v1`.

---

## 1.2 Response Envelope Conventions

Product v1 avoids unnecessary universal wrapping envelopes:

- **Error responses**: use top-level `{"error": { "code": "...", "message": "...", "details": ... }}`
- **Workflow endpoints** (Ingestion, Reconciliation, Confirmation): return workflow states at top level (e.g. `{"status": "committed | needs_confirmation | needs_review", ...}`)
- **Resource / List endpoints**: return resource representation or collection object directly (e.g. `{"id": "...", ...}` or `{"items": [...], "next_cursor": null}`)

---

## 1.3 Content types

JSON endpoints:

```http
Content-Type: application/json
Accept: application/json
```

Statement upload:

```http
Content-Type: multipart/form-data
```

Shortcut image capture may use Base64 in JSON during Product v1 for simplicity.

---

## 1.3 Money serialization

All monetary and FX values MUST be serialized as **decimal strings**, never JSON floating-point numbers.

Correct:

```json
{
  "amount": "268.00",
  "fx_rate": "7.245100000000"
}
```

Incorrect:

```json
{
  "amount": 268.0
}
```

Backend maps JSON decimal strings to Python `Decimal` and PostgreSQL `NUMERIC`.

---

## 1.4 Currency

Currency fields use uppercase 3-letter codes:

```text
CNY
USD
SGD
JPY
EUR
```

---

## 1.5 Dates and timestamps

Business date:

```text
YYYY-MM-DD
```

Timestamp:

```text
RFC3339 / ISO 8601 with timezone
2026-08-19T09:45:00+08:00
```

Shortcut SHOULD send `captured_at` with local timezone.

---

## 1.6 IDs

All public business IDs are UUID strings.

Example:

```text
8f6253e0-2b5e-49c8-9b57-b41cc83428f0
```

---

## 1.7 Pagination

Collection endpoints use cursor pagination when potentially large:

```text
?limit=50&cursor=...
```

Response:

```json
{
  "items": [],
  "next_cursor": null
}
```

Default:

```text
limit = 50
max   = 200
```

---

# 2. Authentication

Two authentication modes exist.

## 2.1 iOS Shortcut / device authentication

Each iPhone has its own device token.

Header:

```http
Authorization: Bearer <device-token>
```

Backend:

1. hashes incoming token;
2. finds active row in `devices`;
3. resolves the owning user;
4. resolves household membership;
5. updates `last_seen_at`.

Raw device tokens MUST never be stored or logged.

---

## 2.2 Dashboard authentication

Dashboard users authenticate through browser/user authentication.

Recommended model:

```text
external identity provider / Supabase Auth / equivalent
        ↓
Backend receives authenticated user subject
        ↓
users.auth_subject
        ↓
household membership
```

Dashboard MUST NOT hold database credentials.

---

# 3. Standard Response Model

## 3.1 Response structure conventions

Endpoints do NOT use a universal `{"data": {}}` wrapping envelope. Instead, response shapes follow strict semantic conventions:

### Resource endpoints
Return the resource object directly at the top level:

```json
{
  "id": "8f6253e0-2b5e-49c8-9b57-b41cc83428f0",
  "name": "Checking Account",
  "account_type": "cash",
  "currency": "CNY",
  "status": "active"
}
```

### Collection / List endpoints
Return items array and pagination cursors at the top level:

```json
{
  "items": [
    { "id": "uuid", "occurred_on": "2026-08-19", "from_amount": "268.00" }
  ],
  "next_cursor": null
}
```

### Workflow / Ingestion endpoints
Return top-level `status` and workflow fields so clients (e.g. iOS Shortcuts) can branch directly:

```json
{
  "status": "committed",
  "request_id": "8f6253e0-2b5e-49c8-9b57-b41cc83428f0",
  "transaction_id": "9a7384c1-1e2f-4a5b-8c9d-0e1f2a3b4c5d"
}
```

---

## 3.2 Error envelope

All errors use a structured, top-level `error` object:

```json
{
  "error": {
    "code": "ACCOUNT_NOT_FOUND",
    "message": "No unique account could be resolved.",
    "retryable": false,
    "details": {}
  }
}
```

`message` is user-displayable but concise.

`code` is stable and machine-readable.

---

## 3.3 HTTP status rules

```text
200 OK
  successful read / idempotent replay / workflow response

201 Created
  resource created

202 Accepted
  async processing accepted

400 Bad Request
  invalid business request

401 Unauthorized
  bad/missing authentication

403 Forbidden
  authenticated but not allowed

404 Not Found
  resource absent

409 Conflict
  idempotency conflict / stale version / already committed conflict

422 Unprocessable Entity
  structurally valid request but deterministic validation fails

429 Too Many Requests
  rate limit

500 Internal Server Error
  unexpected backend error

503 Service Unavailable
  temporary dependency unavailable
```

Do not convert HTTP errors into HTTP 200 with an `"error"` body.

---

# 4. Shared Workflow Statuses

## 4.1 Ingestion request status

```text
received
processing
needs_confirmation
committed
rejected
failed
```

## 4.2 Reconciliation batch status

```text
processing
ready
needs_review
committed
rejected
failed
```

---

# 5. Shortcut Expense API

Expense Shortcut is a dedicated expense-only entry point.

It MUST NOT ask AI to classify whether the screenshot is expense/transfer/investment.

---

## 5.1 Create expense request

```http
POST /api/v1/expenses
Authorization: Bearer <device-token>
Content-Type: application/json
```

Request:

```json
{
  "idempotency_key": "VL-TY-20260819T094500-483928174",
  "captured_at": "2026-08-19T09:45:00+08:00",
  "client_version": "ios-shortcut-2.0",
  "image": {
    "mime_type": "image/jpeg",
    "base64": "<base64>"
  },
  "note": "optional user note"
}
```

Constraints:

```text
idempotency_key required
captured_at required
image required
supported MIME: image/jpeg, image/png, image/heic if backend supports decoding
maximum decoded image size configured server-side
```

Backend pipeline:

```text
authenticate device
↓
insert/recover ingestion_request
↓
validate idempotency hash
↓
AI expense-only extraction
↓
deterministic validation
↓
committed OR needs_confirmation
```

---

## 5.2 High-confidence response (One-off expense)

Standard one-off expense:

```json
{
  "status": "committed",
  "request_id": "uuid",
  "transaction_id": "uuid",
  "payment_mode": "one_off",
  "display_summary": "¥268.00 · 京东\nICBC Visa · Digital & Gadgets\n2026-08-19"
}
```

Foreign-currency credit card purchase (e.g. 10,000 JPY on USD Visa):

```json
{
  "status": "committed",
  "request_id": "uuid",
  "transaction_id": "uuid",
  "payment_mode": "one_off",
  "original_amount": "10000.00",
  "original_currency": "JPY",
  "from_amount": "68.90",
  "from_currency": "USD",
  "account_leg_status": "estimated",
  "display_summary": "10,000 JPY (est. $68.90) · Tokyo Store\nUSD Visa · Travel\n2026-08-19"
}
```

Shortcut:

```text
show display_summary
clear local pending key
finish
```

---

## 5.3 High-confidence response (Installment plan)

When the screenshot represents an installment purchase:

```json
{
  "status": "committed",
  "request_id": "uuid",
  "installment_plan_id": "uuid",
  "payment_mode": "installment",
  "total_amount": "12000.00",
  "currency": "CNY",
  "total_periods": 12,
  "merchant": "Apple",
  "display_summary": "¥12,000.00 · Apple (12期分期计划已建立)\nICBC Visa\n2026-08-19"
}
```

Note:
- Creates `installment_plans` and 12 `installment_periods` schedule rows.
- No future `transactions` or immediate account debt mutations are created.
- The first expense transaction is recognized when the first installment appears on the Statement.

---

## 5.4 Low-confidence response

```json
{
  "status": "needs_confirmation",
  "request_id": "uuid",
  "draft": {
    "occurred_on": "2026-08-19",
    "merchant": "京东",
    "original_amount": "268.00",
    "original_currency": "CNY",
    "from_account": {
      "id": "uuid",
      "name": "ICBC_Visa_Credit"
    },
    "category": {
      "id": "uuid",
      "name": "Digital & Gadgets"
    }
  },
  "warnings": [
    {
      "code": "LOW_ACCOUNT_CONFIDENCE",
      "message": "支付账户识别置信度较低。"
    }
  ],
  "display_summary": "⚠️ 请确认\n¥268.00 · 京东\nICBC Visa · Digital & Gadgets"
}
```

No `transaction` row exists yet.

No `account_state` is changed.

---

# 6. Shortcut Request Recovery / Idempotency

## 6.1 Recover by client idempotency key

```http
GET /api/v1/ingestion-requests/by-key/{idempotency_key}
Authorization: Bearer <device-token>
```

The key is resolved inside the authenticated device scope.

Response examples.

Committed:

```json
{
  "status": "committed",
  "request_id": "uuid",
  "transaction_id": "uuid",
  "display_summary": "¥268.00 · 京东 已记账"
}
```

Needs confirmation:

```json
{
  "status": "needs_confirmation",
  "request_id": "uuid",
  "draft": {},
  "warnings": [],
  "display_summary": "..."
}
```

Not found:

```http
404
```

```json
{
  "error": {
    "code": "REQUEST_NOT_FOUND",
    "message": "The request was not received by the server.",
    "retryable": true,
    "details": {}
  }
}
```

This endpoint is the basis for Shortcut pending-key recovery.

---

## 6.2 Idempotency behavior

Same device + same key + same request hash:

```text
return existing state/result
do not call AI again when committed result is available
do not write duplicate transaction
```

Same device + same key + different request hash:

```http
409 Conflict
```

```json
{
  "error": {
    "code": "IDEMPOTENCY_KEY_REUSE",
    "message": "This idempotency key was already used for different content.",
    "retryable": false,
    "details": {}
  }
}
```

---

# 7. Confirmation / Revision

## 7.1 Confirm draft

```http
POST /api/v1/ingestion-requests/{request_id}/confirm
Authorization: Bearer <device-token>
```

Request:

```json
{}
```

Backend:

```text
lock ingestion_request
verify status = needs_confirmation
revalidate draft against current account/category config
lock account_state
commit transaction
update request -> committed
store replayable response
audit
```

Response:

```json
{
  "status": "committed",
  "request_id": "uuid",
  "transaction_id": "uuid",
  "display_summary": "..."
}
```

Repeated confirm after commit returns the committed result.

---

## 7.2 Revise with natural-language correction

```http
POST /api/v1/ingestion-requests/{request_id}/revise
Authorization: Bearer <device-token>
```

Request:

```json
{
  "correction_note": "支付卡是中行 Visa，不是工行 Visa"
}
```

Backend may reuse the original pending image/draft only while the request is still within its short processing lifetime.

Response:

```text
committed
or
needs_confirmation
```

The same `request_id` and same original client idempotency key remain in use.

---

## 7.3 Structured revision from Dashboard

Dashboard may edit deterministic fields directly.

```http
PATCH /api/v1/ingestion-requests/{request_id}/draft
```

Request example:

```json
{
  "occurred_on": "2026-08-19",
  "merchant": "京东",
  "original_amount": "268.00",
  "original_currency": "CNY",
  "from_account_id": "uuid",
  "category_id": "uuid"
}
```

Backend validates the complete resulting draft.

---

## 7.4 Reject request

```http
POST /api/v1/ingestion-requests/{request_id}/reject
```

Request:

```json
{
  "reason": "Not a valid expense screenshot"
}
```

Response:

```json
{
  "status": "rejected",
  "request_id": "uuid"
}
```

---

# 8. Optional Dedicated Transfer Capture

Transfer is intentionally a separate entry point.

```http
POST /api/v1/transfers
Authorization: Bearer <device-token or browser-user>
```

Request:

```json
{
  "idempotency_key": "optional-for-browser-required-for-device",
  "occurred_on": "2026-08-19",
  "from_account_id": "uuid",
  "to_account_id": "uuid",
  "from_amount": "7250.00",
  "from_currency": "CNY",
  "to_amount": "1000.00",
  "to_currency": "USD",
  "fee": {
    "amount": "20.00",
    "currency": "CNY",
    "category_id": "uuid"
  },
  "remarks": "optional"
}
```

Same-currency example:

```json
{
  "from_amount": "5000.00",
  "from_currency": "CNY",
  "to_amount": "5000.00",
  "to_currency": "CNY"
}
```

Backend computes and persists `effective_fx_rate`.

If one side of a cross-currency transfer is missing:

```http
422
```

```json
{
  "error": {
    "code": "CROSS_CURRENCY_MISSING_LEG",
    "message": "Both transfer amounts are required for a cross-currency transfer.",
    "retryable": false,
    "details": {}
  }
}
```

Fee creates a separate `fee` transaction within the same DB transaction.

---

# 9. Account Snapshot API

Account Snapshot is a dedicated authoritative observation entry.

---

## 9.1 Create snapshot

```http
POST /api/v1/accounts/{account_id}/snapshots
```

Request:

```json
{
  "idempotency_key": "optional browser / required device",
  "as_of": "2026-08-19T09:55:00+08:00",
  "balance": "82315.42",
  "currency": "CNY",
  "source": "dashboard_manual"
}
```

For Shortcut image recognition:

```json
{
  "idempotency_key": "...",
  "as_of": "2026-08-19T09:55:00+08:00",
  "image": {
    "mime_type": "image/jpeg",
    "base64": "..."
  },
  "source": "shortcut"
}
```

Backend converts Snapshot submission into a reconciliation workflow:

```text
authoritative observation
↓
compare with account_state
↓
calculate residual
↓
automatic commit OR needs_review
```

---

## 9.2 Ordinary account small residual

Response:

```json
{
  "status": "committed",
  "batch_id": "uuid",
  "snapshot_id": "uuid",
  "residual_amount": "-47.00",
  "adjustment_transaction_id": "uuid"
}
```

For ordinary accounts:

```text
abs(residual_reporting_CNY) <= 200
```

may auto-create `reconciliation_adjustment`.

---

## 9.3 Large residual

```json
{
  "status": "needs_review",
  "batch_id": "uuid",
  "residual_amount": "-1850.00",
  "display_summary": "账户实际余额与账本相差 ¥1,850.00"
}
```

No committed ledger mutation occurs.

---

# 10. Investment Snapshot API

Investment snapshot remains separate from ordinary income.

> Note: In the implementation roadmap, general account snapshots are introduced in Implementation Phase 5, while dedicated Investment Snapshot and Investment P&L workflows are finalized in Implementation Phase 9.

```http
POST /api/v1/investment-accounts/{account_id}/snapshots
```

Request:

```json
{
  "idempotency_key": "optional browser / required device",
  "as_of": "2026-08-19T10:00:00+08:00",
  "total_asset_value": "160000.00",
  "currency": "CNY",
  "source": "dashboard_manual"
}
```

or image-based Shortcut capture.

Backend:

```text
find previous authoritative investment snapshot
↓
find known contributions / withdrawals
↓
calculate P&L
```

If capital flows are known:

```json
{
  "status": "committed",
  "snapshot_id": "uuid",
  "investment_pnl": {
    "period_id": "uuid",
    "pnl_amount": "10000.00",
    "currency": "CNY",
    "status": "confirmed"
  }
}
```

If capital movement is ambiguous:

```json
{
  "status": "needs_review",
  "batch_id": "uuid",
  "reason_code": "AMBIGUOUS_INVESTMENT_CAPITAL_FLOW",
  "display_summary": "投资账户较上次增加 ¥50,000，但未找到对应入金。"
}
```

No automatic ±200 investment adjustment is allowed.

---

# 11. Statement Upload API

Statement upload is optional and account-specific.

The user MUST select the account before upload.

---

## 11.1 Upload Statement

```http
POST /api/v1/accounts/{account_id}/statements
Content-Type: multipart/form-data
```

Fields:

```text
file              required PDF
password          optional, never persisted
period_start      optional
period_end        optional
```

Response may be synchronous for small documents or async for larger parsing.

Recommended:

```http
202 Accepted
```

```json
{
  "status": "processing",
  "batch_id": "uuid"
}
```

Rules:

- raw PDF is temporary only;
- password exists only for current parsing task;
- successful parse deletes PDF immediately;
- failed processing retention <=24h;
- repeated upload creates a new batch;
- no PDF deduplication requirement.

---

## 11.2 Get Statement/reconciliation status

```http
GET /api/v1/reconciliation-batches/{batch_id}
```

Processing:

```json
{
  "status": "processing",
  "batch_id": "uuid"
}
```

Ready and fully automatic:

```json
{
  "status": "ready",
  "batch_id": "uuid",
  "summary": {
    "line_count": 73,
    "matched_count": 69,
    "created_count": 3,
    "pending_count": 0,
    "residual_amount": "45.00",
    "currency": "CNY"
  }
}
```

Needs review:

```json
{
  "status": "needs_review",
  "batch_id": "uuid",
  "summary": {
    "line_count": 73,
    "matched_count": 68,
    "created_count": 2,
    "pending_count": 3,
    "residual_amount": "1850.00",
    "currency": "CNY"
  }
}
```

---

# 12. Reconciliation Preview

```http
GET /api/v1/reconciliation-batches/{batch_id}/preview
```

Response:

```json
{
  "batch": {
    "id": "uuid",
    "account_id": "uuid",
    "status": "needs_review",
    "period_start": "2026-07-01",
    "period_end": "2026-07-31"
  },
  "summary": {
    "matched_count": 68,
    "new_transaction_count": 2,
    "pending_count": 3,
    "residual_amount": "1850.00"
  },
  "candidates": [
    {
      "id": "uuid",
      "candidate_type": "match",
      "status": "needs_review",
      "statement_line_id": "uuid",
      "confidence": "0.7300",
      "reason_code": "MULTIPLE_TRANSACTION_MATCHES",
      "options": [
        {
          "transaction_id": "uuid",
          "display_summary": "..."
        }
      ]
    }
  ]
}
```

Preview is read-only.

---

# 13. Reconciliation Candidate Review

## 13.1 Accept candidate

```http
POST /api/v1/reconciliation-candidates/{candidate_id}/accept
```

Optional body for selected target:

```json
{
  "target_transaction_id": "uuid"
}
```

---

## 13.2 Edit candidate

```http
PATCH /api/v1/reconciliation-candidates/{candidate_id}
```

Example:

```json
{
  "payload": {
    "transaction_type": "transfer",
    "from_account_id": "uuid",
    "to_account_id": "uuid",
    "from_amount": "5000.00",
    "from_currency": "CNY",
    "to_amount": "5000.00",
    "to_currency": "CNY"
  }
}
```

Backend validates any edited payload.

---

## 13.3 Reject / ignore candidate

```http
POST /api/v1/reconciliation-candidates/{candidate_id}/reject
```

Request:

```json
{
  "reason": "Not a household transaction"
}
```

---

# 14. Atomic Reconciliation Commit

```http
POST /api/v1/reconciliation-batches/{batch_id}/commit
```

Optional optimistic version:

```json
{
  "row_version": 3
}
```

Backend MUST:

```text
lock batch
lock affected account_state rows
re-read transactions
revalidate accepted matches
recompute residual
reapply threshold rules
apply all accepted candidate effects
create snapshots / CC snapshot / P&L
mark statement-confirmed transactions
mark candidates applied
mark batch committed
audit
```

All-or-nothing transaction.

Success:

```json
{
  "status": "committed",
  "batch_id": "uuid",
  "summary": {
    "matched_count": 69,
    "created_count": 3,
    "adjustment_amount": "45.00"
  }
}
```

Repeated commit:

```text
return existing committed result
no duplicate writes
```

If batch changed concurrently:

```http
409 Conflict
```

```json
{
  "error": {
    "code": "BATCH_VERSION_CONFLICT",
    "message": "The reconciliation batch changed. Reload before committing.",
    "retryable": true,
    "details": {}
  }
}
```

---

# 15. Statement Lines API

For Dashboard inspection only.

```http
GET /api/v1/reconciliation-batches/{batch_id}/statement-lines
```

Filters:

```text
?match_status=ambiguous
?line_type=transfer
```

Response:

```json
{
  "items": [
    {
      "id": "uuid",
      "transaction_on": "2026-07-17",
      "posted_on": "2026-07-19",
      "description": "TRANSFER OUT",
      "amount": "5000.00",
      "currency": "CNY",
      "direction": "debit",
      "line_type": "transfer",
      "match_status": "ambiguous",
      "matched_transaction_id": null
    }
  ]
}
```

---

# 16. Accounts API

## 16.1 List accounts

```http
GET /api/v1/accounts
```

Optional filters:

```text
?status=active
?account_type=credit
?owner_user_id=...
```

Response:

```json
{
  "items": [
    {
      "id": "uuid",
      "name": "ICBC_Visa_Credit",
      "institution": "ICBC",
      "account_type": "credit",
      "currency": "USD",
      "owner_user_id": "uuid",
      "billing_day": 31,
      "due_day": 25,
      "status": "active",
      "state": {
        "ledger_balance": "-1860.50",
        "last_authoritative_snapshot_at": "2026-08-01T00:00:00+08:00"
      }
    }
  ]
}
```

---

## 16.2 Create account

```http
POST /api/v1/accounts
```

Request:

```json
{
  "name": "DBS_USD",
  "institution": "DBS",
  "account_type": "cash",
  "currency": "USD",
  "owner_user_id": "uuid",
  "linked_cash_account_id": null
}
```

Response `201 Created`.

---

## 16.3 Update account

```http
PATCH /api/v1/accounts/{account_id}
```

Request:

```json
{
  "name": "DBS USD",
  "billing_day": null,
  "due_day": null,
  "row_version": 4
}
```

Immutable after history exists:

```text
currency
household_id
```

Account type changes after financial history SHOULD normally be rejected.

---

## 16.4 Deactivate account

```http
POST /api/v1/accounts/{account_id}/deactivate
```

No hard delete.

---

# 17. Account Alias API

```http
GET    /api/v1/accounts/{account_id}/aliases
POST   /api/v1/accounts/{account_id}/aliases
DELETE /api/v1/accounts/{account_id}/aliases/{alias_id}
```

Create:

```json
{
  "alias": "工行Visa"
}
```

DELETE means soft-delete/deactivate.

---

# 18. Categories API

## 18.1 List categories

```http
GET /api/v1/categories?type=expense&status=active
```

## 18.2 Create category

```http
POST /api/v1/categories
```

```json
{
  "name": "Child",
  "type": "expense"
}
```

## 18.3 Update / deactivate

```http
PATCH /api/v1/categories/{category_id}
POST  /api/v1/categories/{category_id}/deactivate
```

No hard deletion if referenced by history.

---

# 19. Transaction Read API

## 19.1 List transactions

```http
GET /api/v1/transactions
```

Filters:

```text
from
to
account_id
transaction_type
category_id
currency
verification_status
```

Example:

```text
GET /api/v1/transactions?from=2026-08-01&to=2026-08-31&account_id=...
```

Response fields include:

```json
{
  "id": "uuid",
  "transaction_type": "expense",
  "occurred_on": "2026-08-19",
  "posted_on": "2026-08-21",
  "merchant": "京东",
  "original_amount": "268.00",
  "original_currency": "CNY",
  "from_account": {},
  "category": {},
  "verification_status": "statement_confirmed",
  "reporting_amount": "268.00",
  "reporting_currency": "CNY"
}
```

---

## 19.2 Get transaction

```http
GET /api/v1/transactions/{transaction_id}
```

May include:

```text
links
audit summary
statement verification reference
```

---

# 20. Transaction Correction / Soft Delete

Direct mutation of committed financial facts is controlled.

## 20.1 Update transaction

```http
PATCH /api/v1/transactions/{transaction_id}
```

Only fields allowed by policy.

Request includes:

```json
{
  "row_version": 5,
  "merchant": "Corrected merchant"
}
```

Any change affecting account, amount, date, currency or type must execute as a domain correction transaction with account-state re-projection and audit.

If `verification_status = statement_confirmed`, backend requires explicit confirmation workflow.

---

## 20.2 Void / Soft delete

Canonical financial deletion endpoint:

```http
POST /api/v1/transactions/{transaction_id}/void
```

Request:

```json
{
  "expected_version": 5,
  "delete_reason": "Duplicate manual entry"
}
```

Response:

```json
{
  "status": "voided",
  "transaction_id": "uuid",
  "deleted_at": "2026-08-19T10:30:00+08:00",
  "delete_reason": "Duplicate manual entry",
  "account_balance_restored": true
}
```

Backend atomically:

```text
lock transaction FOR UPDATE
verify status == 'committed'
verify row_version == expected_version
lock affected account_state FOR UPDATE
reverse projection (apply inverse effect to ledger_balance)
update status = 'voided', deleted_at = now(), delete_reason = ...
write audit_events (action = 'void')
```

---

# 21. Refund API

Refund is not deletion.

```http
POST /api/v1/transactions/{transaction_id}/refunds
```

Request:

```json
{
  "occurred_on": "2026-08-25",
  "amount": "300.00",
  "currency": "CNY",
  "to_account_id": "uuid",
  "remarks": "Partial refund"
}
```

Backend creates:

```text
refund transaction
+
transaction_link(refund_of)
```

Partial refunds allowed.

If total refund would exceed remaining refundable amount:

```http
422
```

```json
{
  "error": {
    "code": "REFUND_EXCEEDS_ORIGINAL",
    "message": "Refund amount exceeds the remaining refundable amount.",
    "retryable": false,
    "details": {}
  }
}
```

---

# 22. Credit Card Snapshot / View API

## 22.1 Current credit-card state

```http
GET /api/v1/credit-cards/{account_id}/state
```

Response:

```json
{
  "account_id": "uuid",
  "currency": "USD",
  "latest_snapshot": {
    "as_of": "2026-08-18T00:00:00+08:00",
    "statement_balance": "1260.00",
    "remaining_statement_due": "860.00",
    "unbilled_balance": "600.50",
    "current_outstanding": "1460.50"
  }
}
```

---

# 23. Installment API

## 23.1 List plans

```http
GET /api/v1/installments
```

## 23.2 Get plan

```http
GET /api/v1/installments/{plan_id}
```

Response:

```json
{
  "id": "uuid",
  "merchant": "Apple",
  "total_periods": 12,
  "status": "active",
  "periods": [
    {
      "period_no": 1,
      "recognition_month": "2026-09-01",
      "scheduled_amount": "1000.00",
      "status": "billed",
      "expense_transaction_id": "uuid"
    },
    {
      "period_no": 2,
      "recognition_month": "2026-10-01",
      "scheduled_amount": "1000.00",
      "status": "scheduled",
      "expense_transaction_id": null
    }
  ]
}
```

Future scheduled periods are not transactions until billed.

Product v1 has no early-payoff endpoint.

---

# 24. Investment Reporting API

```http
GET /api/v1/investment-accounts/{account_id}/performance
```

Optional:

```text
?from=2026-01-01&to=2026-12-31
```

Response:

```json
{
  "account_id": "uuid",
  "currency": "CNY",
  "periods": [
    {
      "period_start": "2026-07-31T00:00:00+08:00",
      "period_end": "2026-08-31T00:00:00+08:00",
      "opening_value": "100000.00",
      "closing_value": "160000.00",
      "contributions": "50000.00",
      "withdrawals": "0.00",
      "pnl_amount": "10000.00",
      "status": "confirmed"
    }
  ]
}
```

---

# 25. Dashboard Summary API

Dashboard should consume aggregated backend endpoints instead of reproducing accounting calculations.

## 25.1 Household overview

```http
GET /api/v1/dashboard/overview
```

Response:

```json
{
  "as_of": "2026-08-19T10:10:00+08:00",
  "reporting_currency": "CNY",
  "total_assets": "2138420.00",
  "total_liabilities": "28640.00",
  "net_worth": "2109780.00",
  "data_freshness": {
    "confirmed_within_30d_ratio": "0.9300",
    "confirmed_within_90d_ratio": "0.9900"
  }
}
```

---

## 25.2 Cash-flow summary

```http
GET /api/v1/dashboard/cash-flow?from=2026-08-01&to=2026-08-31
```

Response:

```json
{
  "cash_income": "42000.00",
  "expense": "18600.00",
  "refund": "300.00",
  "net_cash_flow": "23700.00",
  "reporting_currency": "CNY"
}
```

Investment P&L is excluded.

Transfers are excluded.

Reconciliation adjustments are excluded.

---

## 25.3 Investment summary

```http
GET /api/v1/dashboard/investments?from=2026-01-01&to=2026-12-31
```

Returns investment P&L separately from household income.

---

## 25.4 Account freshness

```http
GET /api/v1/dashboard/account-freshness
```

Response:

```json
{
  "items": [
    {
      "account_id": "uuid",
      "account_name": "ICBC Debit",
      "last_authoritative_snapshot_at": "2026-08-17T00:00:00+08:00",
      "age_days": 2,
      "freshness": "fresh"
    }
  ]
}
```

---

# 26. Reconciliation Work Queue

Dashboard pending work:

```http
GET /api/v1/work-queue
```

Optional:

```text
?type=ingestion
?type=reconciliation
```

Response:

```json
{
  "items": [
    {
      "work_type": "reconciliation",
      "id": "uuid",
      "status": "needs_review",
      "summary": "ICBC July Statement · 3 items need review"
    },
    {
      "work_type": "ingestion",
      "id": "uuid",
      "status": "needs_confirmation",
      "summary": "¥268 · 京东 · account uncertain"
    }
  ]
}
```

---

# 27. Audit API

Read-only for Dashboard.

```http
GET /api/v1/audit-events
```

Filters:

```text
entity_type
entity_id
from
to
actor_user_id
```

No API exists to update/delete audit events.

---

# 28. Historical Transaction Correction & Void API

Used by Dashboard for explicit corrections to historical or Statement-confirmed transactions without mutating raw Statement evidence.

## 28.1 Preview correction

```http
POST /api/v1/transactions/{transaction_id}/corrections/preview
```

Request:

```json
{
  "occurred_on": "2026-08-18",
  "category_id": "uuid",
  "merchant": "Apple Store Ginza",
  "remarks": "Updated purchase detail",
  "from_amount": "68.20"
}
```

Response:

```json
{
  "transaction_id": "uuid",
  "expected_version": 3,
  "is_statement_confirmed": true,
  "proposed_changes": {
    "occurred_on": "2026-08-18",
    "category_id": "uuid",
    "merchant": "Apple Store Ginza",
    "from_amount": "68.20"
  },
  "account_state_deltas": [
    {
      "account_id": "uuid",
      "account_name": "USD Visa",
      "current_balance": "-1500.00",
      "delta": "-0.70",
      "projected_balance": "-1500.70"
    }
  ],
  "requires_confirmation": true
}
```

---

## 28.2 Commit correction

```http
POST /api/v1/transactions/{transaction_id}/corrections/commit
```

Request:

```json
{
  "expected_version": 3,
  "changes": {
    "occurred_on": "2026-08-18",
    "category_id": "uuid",
    "merchant": "Apple Store Ginza",
    "from_amount": "68.20"
  },
  "reason": "Corrected merchant and foreign exchange settlement discrepancy"
}
```

Response: returns updated Transaction resource.

Optimistic concurrency error (if modified concurrently): `409 Conflict` (`ROW_VERSION_CONFLICT`).

---

## 28.3 Void transaction

```http
POST /api/v1/transactions/{transaction_id}/void
```

Request:

```json
{
  "expected_version": 3,
  "delete_reason": "Duplicate manual transaction recorded by user"
}
```

Response:

```json
{
  "status": "voided",
  "transaction_id": "uuid",
  "deleted_at": "2026-08-19T10:30:00+08:00",
  "delete_reason": "Duplicate manual transaction recorded by user",
  "account_balance_restored": true
}
```

---

# 29. Health / Readiness

## 29.1 Liveness

```http
GET /health
```

Response:

```json
{
  "status": "ok"
}
```

This does not require Gemini.

---

## 29.2 Readiness

```http
GET /ready
```

Checks:

```text
database connectivity
required schema version
critical configuration
```

Gemini availability may be reported separately but SHOULD NOT make all read-only Dashboard routes unavailable.

Example:

```json
{
  "status": "degraded",
  "database": "ok",
  "gemini": "unavailable"
}
```

---

# 30. Stable Error Codes

Minimum Product v1 set:

```text
AUTH_REQUIRED
DEVICE_REVOKED

INVALID_REQUEST
INVALID_CURRENCY
INVALID_AMOUNT
INVALID_DATE

ACCOUNT_NOT_FOUND
ACCOUNT_AMBIGUOUS
ACCOUNT_INACTIVE
ACCOUNT_CURRENCY_MISMATCH

CATEGORY_NOT_FOUND
CATEGORY_INACTIVE

IDEMPOTENCY_KEY_REUSE
REQUEST_NOT_FOUND
REQUEST_ALREADY_REJECTED

LOW_CONFIDENCE
NEEDS_CONFIRMATION

CROSS_CURRENCY_MISSING_LEG
INVALID_TRANSFER

REFUND_EXCEEDS_ORIGINAL

STATEMENT_PARSE_FAILED
STATEMENT_PASSWORD_REQUIRED
STATEMENT_PASSWORD_INVALID

MULTIPLE_TRANSACTION_MATCHES
RECONCILIATION_RESIDUAL_TOO_LARGE
BATCH_VERSION_CONFLICT
BATCH_ALREADY_COMMITTED

AMBIGUOUS_INVESTMENT_CAPITAL_FLOW

ROW_VERSION_CONFLICT

RATE_LIMITED
DEPENDENCY_UNAVAILABLE
INTERNAL_ERROR
```

Client logic SHOULD branch on `code`, not human-readable `message`.

---

# 31. Human Review & Confirmation Rules

### 31.1 Ingestion `needs_confirmation` triggers
Ingestion requests (e.g. Shortcut expenses) require user confirmation before financial commit when:
- account is unresolved or multiple account aliases match;
- cross-currency transfer is missing one of its two actual legs;
- AI extraction confidence is low;
- new merchant or ambiguous category requires human confirmation.

### 31.2 Reconciliation `needs_review` triggers
Reconciliation batches and candidates require human review when:
- Statement line has multiple ambiguous transaction matches;
- ordinary-account unexplained residual $> 200\text{ CNY}$;
- investment account capital movement is ambiguous or unverified;
- estimated settlement deviation is suspiciously high ($> 20\%$);
- Statement original currency contradicts captured original currency (`ORIGINAL_AMOUNT_CONFLICT`).

### 31.3 Safe Automatic Resolution
- Ingestion auto-commits when: ordinary expense, unique account, unambiguous amount/currency, valid category, and confidence is high.
- Reconciliation auto-matches when: candidate score $\ge 80$, margin $\ge 15$, mutual-best uniqueness holds, and residual $\le 200\text{ CNY}$ (for ordinary accounts).

---

# 32. API Transaction Boundaries

API boundaries MUST preserve the DB transaction rules in `PHYSICAL_SCHEMA.md`.

## 32.1 Expense commit

One DB transaction:

```text
ingestion request
+ transaction
+ account_state update
+ audit
+ replayable response
```

## 32.2 Transfer commit

One DB transaction:

```text
transfer
+ both account_state rows
+ fee transaction if any
+ audit
```

## 32.3 Confirmation

No financial write before confirmation.

## 32.4 Reconciliation

Preview and candidate review are staged.

`POST /reconciliation-batches/{id}/commit` is the only operation that applies the whole batch to the committed ledger.

---

# 33. What Must Not Leak Into Clients

Clients MUST NOT need to know:

```text
PostgreSQL table names
TABLE_SUFFIX
row-lock strategy
investment P&L formula implementation internals
matching score formula
Gemini model name
hard-coded account/category lists
database credentials
```

Clients consume stable domain APIs only.

---

# 34. Legacy API Migration

Current legacy:

```text
POST /api/record
```

is not the target interface.

Migration approach:

```text
Phase A:
  keep legacy /api/record operational

Phase B:
  introduce /api/v1/expenses
  migrate Shortcut

Phase C:
  introduce Dashboard APIs
  migrate Dashboard off direct DB

Phase D:
  remove legacy /api/record and duplicated DB logic
```

Do not preserve legacy request shapes if they compromise the target domain model.

---

# 35. Implementation Rule for Agents

This document defines the external application contract.

An implementation Agent MUST:

1. preserve endpoint semantics even if internal modules change;
2. keep monetary JSON values as strings;
3. use stable machine-readable error codes;
4. keep idempotency at request level;
5. never insert pending AI drafts into committed transactions;
6. never let Dashboard bypass Backend business rules;
7. never make Statement parsing partially mutate the ledger;
8. keep investment P&L separate from cash income;
9. keep Snapshot separate from Transaction;
10. keep internal transfer as one transaction with two explicit legs.

If a technical implementation detail is not specified here and does not change business semantics, choose a reasonable default and document it rather than asking the user.
