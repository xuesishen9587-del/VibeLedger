# Simplified implementation contract

Status: **Target specification, not current API/schema behavior.** Product rules are
in [TARGET_DOMAIN_MODEL](../../TARGET_DOMAIN_MODEL.md). Rollout and test gates are
in [IMPLEMENTATION_PLAN](IMPLEMENTATION_PLAN.md). Do not apply this contract by
editing already-applied migrations or silently changing the accepted staging service.

## 1. Backend boundaries

Keep the existing `app/api`, `services`, `repositories`, `domain`, and `auth` layout.
Routes authenticate and validate transport, services own a use case and transaction
boundary, repositories contain household-scoped SQL, and domain functions handle
money, capture validation and reporting formulas. No generic workflow framework,
event sourcing, ledger engine, or abstract repository hierarchy is required.

The target has expense capture, balance capture, ordinary record editing, and
reports. Gemini extracts proposed fields or revises a draft. It never writes SQL,
selects risk, creates accounts, changes authorization, synthesizes financial events,
or decides that unexplained balance growth is profit. Manual operations work without AI.

## 2. PostgreSQL contract

### Shared conventions

Use UUID business IDs, TIMESTAMPTZ timestamps and DATE business dates. Common
mutable-record columns are `created_at`, `updated_at`, `row_version BIGINT NOT NULL
DEFAULT 0`. Financial records also carry `household_id`, `created_by_user_id`,
nullable `created_by_device_id`, and `source_request_id` for the command receipt.
These fields are server supplied. Defaults for timestamps are `now()`.

Amounts/balances use `NUMERIC(20,6)`, rates `NUMERIC(24,12)`. Validate finite values
(reject NaN/infinity), bounds and currency precision before SQL. Support CNY, SGD,
USD, EUR (2 decimals) and JPY (0) initially; other codes require a tested minor-unit
definition rather than silently assuming 2. Financial JSON inputs/outputs are decimal
strings, including zero; confidence may be a float because it is not money.
Reject excess minor units on entered/extracted original amounts; rounding is for
FX conversion and explicitly displayed approximate observations after user review.
Do not silently round a malformed receipt into a different purchase amount.

All household-owned reference tables expose `UNIQUE(household_id,id)`. Use composite
foreign keys `(household_id, referenced_id)` for accounts, categories, requests,
transactions and snapshots so cross-household relationships fail in PostgreSQL too.
Optional owners/actors must be members of the same household (composite membership
FK where stored with household). Device/user ownership is revalidated in services.
Do not accept household IDs in request bodies as authorization.

Financial references use `ON DELETE RESTRICT`. No hard-delete financial API. All
queries, including nested IDs and history, include household scope. The runtime role
has DML only on its private schema and no schema DDL privileges. It cannot mutate
audit history. Bootstrap/migrations use a separate operator role. Financial tables
must not be exposed/granted to Supabase anon/authenticated Data API clients.

### Table inventory (13 application tables, plus schema_migrations)

These are the complete target tables, not additional tables alongside the old ledger.

| Table | Required fields and rules |
|---|---|
| `households` | Retain id, name, reporting_currency, status, timestamps. Replace ledger_start_date with `started_on DATE`; add `timezone TEXT` default Asia/Singapore. Currency fixed after first financial record. |
| `users` | Retain id, auth_subject unique, email, display_name, status, timestamps. No new password store. |
| `household_members` | Retain household_id/user_id composite PK, role owner/member, joined_at. One active household per user for this product; reject ambiguous membership as today. |
| `devices` | Retain user_id, name, platform, unique token_hash, active/revoked, client_version, created_at, last_seen_at, revoked_at. Plaintext token returned only once, never stored. |
| `accounts` | id, household_id, name, `balance_scope TEXT NOT NULL`, account_type cash/savings/investment/credit, currency, nullable owner_user_id, risk_level, `opened_on DATE`, nullable `closed_on DATE`, status active/closed/cancelled, common mutable columns. |
| `account_aliases` | Retain id, account_id; add household_id. alias_text, normalized_alias, status active/inactive, common mutable columns. Unique active normalized_alias per account; overlap across accounts is allowed and means ambiguity. |
| `categories` | id, household_id, name, category_type expense/income, description TEXT, `is_fallback BOOLEAN` default false, status active/inactive, common mutable columns. One active fallback per type; fallback cannot be archived or have type changed. |
| `ingestion_requests` | The one durable capture/command receipt mechanism, detailed below. |
| `transactions` | Simplified spending/income records, detailed below. |
| `account_snapshots` | The one balance observation model, detailed below. |
| `investment_period_inputs` | Explicit flow totals between two snapshots; no stored derived P&L. |
| `fx_quotes` | Immutable accepted reference quotes by currency pair and effective date; shared public-rate cache, no household data. |
| `audit_events` | Minimal append-only record history, detailed below. |

Four identity tables, three account/category tables, and six record/support tables
are sufficient. Table count is a consequence of the workflows, not an optimization
target; do not combine unrelated facts merely to reduce it further.

Account constraints: credit risk must be null; other risk values are very_low/low/
medium/high/null. Active has no closed_on; closed has closed_on >= opened_on;
cancelled has no financial references. Enforce closure zero/no-later-observation
and immutable type/currency in services under the finance-write lock. Account names
are case-insensitively unique among active accounts; categories within active type.
Do not require a balance when creating an account.

### ingestion_requests

Keep the name because the working Shortcut uses it. Add browser support directly;
do not provision fake devices for Dashboard sessions.

| Column | Contract |
|---|---|
| id, household_id, user_id | Required UUIDs; creator user must belong to household |
| device_id | Nullable, required only for device requests |
| actor_scope | Server-generated `device:<uuid>` or `user:<uuid>`; browser identity is stable across session refresh |
| idempotency_key | 8..200 characters, unique `(household_id,actor_scope,idempotency_key)` |
| request_kind | expense / balance_capture / command |
| operation | Method + canonical resource path; included in request hash to prevent cross-endpoint key reuse |
| request_hash | SHA256 of canonical validated input including image digest, operation, timestamps and notes; null only for a cancel-before-arrival tombstone |
| image_sha256 | Nullable digest, for expense duplicate warnings; never image bytes |
| status | processing / needs_confirmation / committed / rejected / failed |
| captured_at, client_version | Nullable except captured_at required for image captures |
| draft_payload | Versioned, validated expense or balance draft JSON; never raw model output |
| response_payload, response_http_status | Saved replay response and its HTTP status; no secrets |
| failure_code | Nullable stable code |
| row_version | Monotonic for draft edits/state changes |
| last_editor_scope | Actor scope of latest draft edit; used by bodyless Shortcut confirmation |
| created_at, updated_at, committed_at | committed_at is non-null iff committed |

`processing` and `needs_confirmation` contain zero financial records from this
request. `committed` contains the full result. `rejected` and `failed` contain no
financial result and are terminal; late workers cannot revive them. A SQL CHECK
enforces state vocabulary and committed_at consistency. `received` is unnecessary.
Commands are inserted and completed in one database transaction, never left pending.
Cancel-before-arrival uses request_kind=command, operation=cancel and a null request_hash;
its rejected state applies to that key regardless of a later arriving operation.

### transactions

Required: id, household_id, transaction_type expense/refund/cash_income, occurred_on,
date_source receipt/capture_date/manual, original_amount > 0, original_currency,
category_id, source shortcut/dashboard_manual, status committed/voided, common
mutable and financial provenance columns. Nullable: occurred_at, account_id,
merchant, merchant_normalized, remarks, refund_of_transaction_id.

Keep payment_mode one_off/installment as descriptive metadata on expenses only;
the amount is always the full purchase. No installment plan/period references.
Reporting fields: reporting_amount, reporting_currency, reporting_fx_rate,
reporting_fx_as_of DATE, reporting_fx_source, reporting_fx_locked_at. All null when
conversion is unavailable; all set together otherwise. Same-currency rate is 1.
Amount/date/currency edits clear and recompute this set; category/merchant edits do not.

Voids have non-null deleted_at, deleted_by_user_id, delete_reason; committed rows
have all three null. `refund_of_transaction_id` is allowed only on refunds, cannot
reference self, and references a committed expense in the household. Linked refund
sum and currency/category-type rules are enforced under the shared write lock.
Categories are expense-type for expense/refund, income-type for cash_income.
User-explicit edits may retain historical inactive accounts/categories, but cannot
newly assign an inactive reference. Type changes require void/recreate, not a PATCH.

Remove posted_on, from/to accounts/amounts/currencies, settlement status, statement
verification status, statement_batch_id, effective settlement FX and generic links.
Constraint: at most one transaction per source_request_id. Refund commands use
their own receipts. No transaction value feeds wealth.

### account_snapshots

Required: id, household_id, account_id, `as_of TIMESTAMPTZ`, `time_basis` explicit/
capture/date_only, signed balance, currency, source screenshot/manual, status
active/voided, common financial provenance and mutable columns. Nullable notes,
`replaces_snapshot_id`, voided_at, voided_by_user_id, void_reason. Currency equals
account currency; times lie inside account lifetime and are not in the future.
No snapshot type, reconciliation ID, is_authoritative flag, or credit-specific row.

Financial values on an active observation are immutable. Correction atomically
voids the original and inserts a new observation with `replaces_snapshot_id`; the
old record remains inspectable. A plain void inserts no replacement and reveals
the previous current observation. Void/correction requires expected_version and reason.
Do not void/revise a closure zero unless account closure is preserved or explicitly
reopened in that operation.

Unique active `(household_id,account_id,as_of)`. An independent update at the same
timestamp requires an explicit correction or changed timestamp; never last-writer
wins. Unique `(source_request_id,account_id)` allows one row per account per capture.
Multiple accounts can share the same request and timestamp.

For current screenshots without a visible balance time, use captured_at and label
time_basis=capture. Historical date-only balances use that date's end in household
timezone, labelled date_only; today's date-only current screen uses captured_at.
Do not claim exact intraday ordering from a date-only source. Duplicate/backdated
checks use stored instants and recorded precision; conflicting same-date imprecise
observations need review rather than invented seconds. P&L uses actual snapshot
boundaries as displayed to the user, not an assumed complete calendar day.

### investment_period_inputs

id, household_id, account_id, opening_snapshot_id, closing_snapshot_id,
contributions_amount >= 0, withdrawals_amount >= 0, `confirmed_by_user_id`,
confirmed_at, notes nullable, common mutable/provenance columns. Both amounts are
required even when zero. Unique opening/closing pair. Currency is the account's;
do not duplicate gain or a “confirmed P&L” projection here.

Write only for active consecutive snapshots of the same investment account with
closing.as_of > opening.as_of. Use `(opening.as_of,closing.as_of]` as the labelled
flow interval; users must account for source timing/precision. Contributions and
withdrawals must cover the entire interval. No row means unknown, not zero.
On reads, recheck active IDs and adjacency; ineligible old pairs appear only in
history, never in aggregates. Edits to totals use expected_version and audit.
Add status active/voided, nullable voided_at and void_reason. Voiding an input
withdraws the completeness assertion and returns that interval's gain to unknown.
PUT on an existing pair requires its version and can explicitly reactivate it with
new complete totals; keep the unique pair and prior values in audit. Reads use only
active inputs. This is also how a user corrects “no movement” to “not sure”.

### fx_quotes and reporting conversion

Columns: from_currency, to_currency, rate_as_of DATE, rate > 0, source TEXT,
fetched_at TIMESTAMPTZ. PK `(from_currency,to_currency,rate_as_of)`. Quotes are
insert-once; no replacing a rate already used for historical reporting. Define
direction as **units of to_currency for 1 from_currency**, consistently everywhere.
Retain the existing provider adapter but return the provider's effective date, not
merely the requested date. Do not implement a new FX service.

At capture/entry, try one bounded lookup for the expense business date, falling
back to the latest available quote on or before it, at most 7 calendar days old.
Freeze the reporting fields when obtained. If unavailable, commit the original
expense with reporting fields null and an informational warning. Capture succeeds.

`POST /reports/refresh-fx` retries only missing conversions and fills those fields
once under version checks, recording an audit action. It can also refresh current
wealth quotes. Dashboard invokes it at most once per session/day when rates are
missing; an explicit Refresh retries sooner. No scheduler or worker is required.
Read APIs do not mutate financial records or call Gemini.

Wealth/report reads may fetch/cache public quotes outside DB locks with bounded
timeouts. Current wealth can use older cached rates during outage but must show
their age; over 7 days is stale FX. Historical reports allow only quotes no later
than T and within 7 days; otherwise that currency is missing. Preserve native
amounts and known converted subtotals, return missing counts and null complete totals.
No fatal whole-dashboard error when a single currency cannot be converted.

### audit_events

Retain the append-only table/trigger pattern: bigint identity id, household_id,
actor_user_id, optional actor_device_id, source_request_id, entity_type, entity_id,
action, before_data, after_data, reason, created_at. Actions: create, update, void,
replace, close, reopen, confirm_flows, fill_reporting_fx. No reconciliation metadata,
raw AI responses, full screenshots or bearer values. Audit only actual persisted
record changes, not every read or successful retry. Change and history commit
together. No event replay to rebuild financial records; normal tables are truth.

Receipts and minimal financial history are retained with the household data; no
short TTL that allows old pending keys to create expenses again. Safe schema
backup/restore includes receipts. Large temporary AI/image content is never stored.

### Indexes and transaction boundaries

Required indexes beyond PK/uniques: transactions(household_id,occurred_on DESC,id),
transactions(household_id,refund_of_transaction_id) for active refunds;
account_snapshots(household_id,account_id,as_of DESC) for active observations;
ingestion_requests(household_id,status,created_at); audit_events(household_id,
entity_type,entity_id,id DESC). Ordinary text-normalized duplicate checks need no
trigram extension. Retain aliases without fuzzy global searches.

For this two-person household, serialize short financial writes with
`SELECT id FROM households WHERE id = ... FOR UPDATE`. Acquire receipt lock first,
then household lock, then any record locks in stable ID order. Never call Gemini,
FX, email or another network service while holding these locks. This is simpler
than the current account_state multi-row projection protocol. Other households
and all read requests remain independent.

Under that lock, revalidate references, expected versions, duplicate signals,
snapshot uniqueness, refund limits and investment adjacency. Commit financial
records, history, and the saved receipt response in one transaction. Validation
failure or injected SQL failure produces no partial financial changes.

## 3. Capture, retry and correction protocol

### Durable capture without a job system

1. Authenticate; validate size/type/dimensions, timestamp and input shape. Hash the
   normalized payload. Retain JPEG/PNG byte/decoder checks and 10 MiB decoded limit;
   also cap encoded body and decoded pixels (20 megapixels). Reject before AI.
2. In a short transaction reserve `(scope,key)` as processing. On collision compare
   hashes and replay current state; **only the successful inserter calls Gemini**.
   Null-hash cancelled-key tombstones always replay rejection.
3. Release connection/locks while extracting. Use explicit typed transport models
   and fixed-field confidence objects; no dynamic dictionaries in Gemini schemas.
   Limit a balance capture to 50 rows. Screenshots/notes are untrusted content.
   Expense transport adds `intent` (expense/refund/transfer/repayment/failed/pending/
   unknown), intent confidence, and evidence that a date-less payment is current.
   The backend requires expense intent for auto-save; missing intent confidence is
   conservative. Unknown account/category follows product fallback rules instead
   of causing a financial uncertainty gate.
4. Finish under receipt + household locks. Confirm receipt is still processing,
   then revalidate current configuration. Save either one draft with warnings or
   all financial facts + audit + committed response. A cancellation wins if it
   obtained the receipt lock first; a late model result then writes nothing.
5. A provider failure becomes terminal failed, with zero financial changes. A
   process killed before terminal persistence can leave processing; recovery may
   cancel and recapture. Do not silently reclaim or retry processing requests.

No detached background task, lease table, retry worker, or durable screenshot store.
Set an explicit overall capture deadline (initially 45 seconds; provider timeout
inside it) and return a retryable error on exhaustion. Tune to the accepted iPhone
network behavior in staging. A connection loss does not prove whether commit occurred.

### Idempotency versus duplicate purchases

Same key + same payload returns saved result/state without another AI call or write.
Same key + changed payload/operation returns 409 IDEMPOTENCY_KEY_REUSE. Keys are
device-scoped, not globally unique across both phones. New-key transactions may
still represent the same real purchase; idempotency alone cannot detect that.

Before committing an expense, warn of a possible duplicate if either the same image
digest already produced an active expense, or exact normalized merchant + business
date + original amount/currency + same known account matches an active expense.
Check within the household under its write lock. Compare exact data, not fuzzy
scoring. A repeat signal creates needs_confirmation with matching record IDs; it
never auto-merges or deletes a legitimate second purchase. User confirmation of
the clearly labelled duplicate draft explicitly records another purchase. If a
new duplicate appeared after the shown draft, return its warning for fresh review.
No rule claims reliable semantic deduplication across differently cropped images.

### The pending-key recovery contract

Keep device-local `On My iPhone/VibeLedger/device-token.txt` and `pending-key.txt`;
the latter remains plain text. Do not require a JSON pending-payload file or iCloud.

* No pending key: create/persist key, capture, POST once. Display committed/draft result.
* Pending key: GET by-key on that device. Committed/rejected/failed are known terminal
  outcomes; clear only after presenting the outcome. A draft resumes correction/
  confirmation under the same request. A processing request is still unresolved.
* A 404 is **not proof no older network request can still arrive**. Never blindly
  discard that key and create another purchase. If the user abandons recovery, call
  POST by-key/{key}/cancel first. It atomically inserts a rejected tombstone if absent,
  or rejects processing/draft if present. If already committed, it returns the
  committed result instead. Only then may the Shortcut clear the key and recapture.
* On network/5xx ambiguity, keep the key and recover again. No automatic new key,
  fake success, or attempt to rebuild a different POST body with the old key.

This adds a cancellation action only to interrupted recovery. Normal capture and
existing confirm/revise/reject endpoints retain their paths and one-request fast path.
Cancel/reject of a committed request never voids its expense; void is a separate action.

### Draft editing and confirmation

There is only needs_confirmation, shared by expense and balance drafts. Either
household member using browser auth may review a household draft. A device may
access only its own requests. GET by-key always uses the requesting actor's scope;
GET by ID is how browser review opens a phone's draft.

Draft edits never change the original request hash/key, never create a new request,
and never auto-commit. Structured edits bypass Gemini. Natural-language edits use
only the saved draft and active reference data; validate the resulting allowed-field
patch. Discard model output if the draft changed while the call was running.

Browser edits/confirm/reject require expected_version. Preserve bodyless device
confirm and `{}`: it confirms the current draft under a lock when the latest editor
is the original device (initial extraction counts as that device). If a browser has
edited it, bodyless confirm returns 409 DRAFT_CHANGED; the user can finish in Dashboard
or revise through the phone before confirming. An explicit expected_version works
for either actor. Terminal confirm/reject retries return the existing terminal result,
even if expected_version is old. Do not confirm unseen concurrent browser changes.
PATCH and structured revise use omitted=unchanged, explicit null=clear nullable
field; clearing required financial fields leaves an invalid draft that cannot
confirm. Structured user choices supersede AI proposals, but not household/money
constraints. A confirm that discovers new blocking evidence returns the refreshed
draft and version, with no financial commit.

## 4. HTTP surface

Keep `/api/v1`, Bearer auth, decimal strings and the current error envelope:

```json
{"error":{"code":"ROW_VERSION_CONFLICT","message":"This record changed. Reload it.","retryable":false,"details":{}}}
```

Lists return `{items:[],next_cursor:null}`; limit default 50, max 200, stable cursor
ordering by business date/time then ID (history by audit id). Date filters `from`
and `to` are inclusive business dates, translated to `[local midnight,next midnight)`
for timestamps. Invalid/reversed ranges return 422. Reads return current row_version.
Money strings use currency minor units rather than formatting every currency to .2f.

All ordinary create/edit/void POST/PATCH/PUT writes require `Idempotency-Key` header
and persist a command receipt. Include operation/path/body in its hash. For a replay,
check the receipt before expected_version so a lost successful response can replay.
Command receipt status is terminal in the same transaction as its result. Exceptions:
capture creation uses its existing body key; draft actions use the original receipt
and version; cancellation uses the path key; device provisioning never stores/replays
plaintext tokens (retain one-time-return behavior; lost tokens require revoke/reprovision).

### Endpoint inventory

`member` means browser household member or valid household device unless narrowed.
No multi-household selection or generic entity endpoint is added.

| Method / path after /api/v1 | Auth | Request / result |
|---|---|---|
| POST /expenses | device | Existing screenshot body; committed or expense draft |
| POST /balance-captures | member | Screenshot body; committed observations or balance draft |
| GET /ingestion-requests/by-key/{key} | member | Own scope; saved result, processing, draft, failed, or 404 |
| POST /ingestion-requests/by-key/{key}/cancel | member | Own scope; reject/tombstone or return already-terminal result |
| GET /ingestion-requests | browser | status=needs_confirmation filter; household Review list |
| GET /ingestion-requests/{id} | member | Browser household scope, device own scope; saved state/draft/version |
| POST /ingestion-requests/{id}/revise | member | Expense correction_note or existing structured fields; expected_version for browser |
| PATCH /ingestion-requests/{id}/draft | member | Structured expense fields or full replacement balance rows, expected_version |
| POST /ingestion-requests/{id}/confirm | member | Optional expected_version, with bodyless device rules above; terminal replay |
| POST /ingestion-requests/{id}/reject | member | Optional reason, browser expected_version; no finance effects |
| GET, POST /accounts | member | Filter status/type; create name, scope, type, currency, optional risk/owner/opened_on |
| GET, PATCH /accounts/{id} | member | Detail; expected_version + allowed metadata changes |
| POST /accounts/{id}/close | member | expected_version, closing_snapshot_id, closed_on; validate zero |
| POST /accounts/{id}/reopen | member | expected_version, reason; clear closed_on; historical inclusion is restated |
| POST /accounts/{id}/cancel | member | expected_version; only never-used accounts |
| GET, POST /accounts/{id}/aliases | member | alias text; list active/all; return stable alias ID |
| PATCH /accounts/{id}/aliases/{alias_id} | member | expected_version, text/status |
| GET, POST /categories | member | Filter type/status; create name/type/description |
| PATCH /categories/{id} | member | expected_version, name/description/status; type immutable after use |
| GET, POST /transactions | member | List filters date/type/account/category/merchant; manual entry fields below |
| GET, PATCH /transactions/{id} | member | Detail; expected_version + editable fields + optional reason |
| POST /transactions/{id}/void | member | expected_version, delete_reason |
| GET /accounts/{id}/snapshots | member | Dated history including optional voided records |
| POST /balance-updates | member | Manual selected observations, atomically saved |
| POST /snapshots/{id}/correct | member | expected_version, expected_latest_snapshot_id, expected_account_version, replacement balance/currency/as_of/time_basis, reason |
| POST /snapshots/{id}/void | member | expected_version, expected_latest_snapshot_id, expected_account_version, reason |
| PUT /investment-period-inputs | browser | Pair IDs, contribution/withdrawal totals, expected_version (null to create), notes |
| POST /investment-period-inputs/{id}/void | browser | expected_version, reason; gain returns to unknown |
| GET /reports/wealth | member | Optional as_of; observations, known/complete totals, risk, coverage |
| GET /reports/wealth-history | member | from/to; step points, missing/stale counts and dates |
| GET /reports/spending | member | from/to; gross/refunds/net, category/month/merchant breakdown, optional recorded income, FX coverage |
| GET /reports/investments | member | Optional account/from/to; interval values/flows/gains, unknown/gap reasons, native-currency subtotals |
| POST /reports/refresh-fx | browser | Optional from/to; fill only missing spending conversions and refresh current quotes; counts of filled/pending |
| GET /history | member | Required entity_type/entity_id, cursor; scoped minimal change history |
| GET, POST /devices; POST /devices/{id}/revoke | existing policy | Retain device request/response fields and ownership/owner administration |
| GET /health, GET /ready | public | Runtime and schema/dependency probes; /api/v1/health alias retained |

Patchable transaction fields are occurred_on, original_amount/currency, account_id,
category_id, merchant, remarks, refund_of_transaction_id. Reject unexpected fields.
Account/risk/status changes never silently alter transaction categories or observations.
Manual refund creation is POST /transactions with transaction_type=refund, optional
refund_of_transaction_id, date, positive original amount/currency and receiving
account_id. It uses the same original-currency convention as expenses; account_id
is metadata, not a settlement amount. No separate generic transaction-links API.
Ordinary PATCH bodies are flat `{expected_version, ...changed_fields, reason?}`;
omitted fields are unchanged and explicit null clears only nullable fields. Manual
POST /transactions requires transaction_type, occurred_on, original_amount/currency,
category_id; other descriptive fields are optional. source/date_source are set
server-side to dashboard_manual/manual. A changed business date through manual
correction also sets date_source=manual.

Account read results include current metadata/version and latest_snapshot (id,
balance, currency, as_of) or null. Account PATCH allows name, balance_scope, risk,
owner, and opened_on; type/currency only before any financial reference. Lifetime
edits must keep existing observations in bounds. Status changes use the explicit
close/reopen/cancel actions. Snapshot correction/void may include
`reopen_account:true` to reopen a closed account atomically under expected_account_version;
otherwise a broken closure invariant is rejected.

Historical account/category references may appear in reads after archival. Their
creation/assignment validations apply to newly selected references, not unrelated edits.
Include `row_version` in every mutable resource result. Missing/other-household IDs
return indistinguishable 404s; wrong auth mode returns 403.

### Preserve the working expense wire contract

```json
{
  "idempotency_key": "phone-generated-key",
  "captured_at": "2026-09-05T12:15:00+08:00",
  "client_version": "expense-shortcut-v2",
  "image": {"mime_type":"image/jpeg","base64":"<encoded image>"},
  "note": null
}
```

Normal success remains HTTP 200 with status, request_id, transaction_id,
payment_mode, display_summary. Additive fields (row_version, original amount/currency,
category/account, informational warnings) must not break old clients.

```json
{"status":"committed","request_id":"<uuid>","transaction_id":"<uuid>","payment_mode":"one_off","display_summary":"¥28.50 · Lunch\nDine · 2026-09-05"}
```

Expense drafts retain existing `draft.occurred_on`, `merchant`, `original_amount`,
`original_currency`, `from_account:{id,name}|null`, `category:{id,name}|null`,
`payment_mode`, `total_periods`, `remarks`, plus warnings `{code,message}` and
display_summary. `from_account` is a compatibility transport name mapped to nullable
transactions.account_id, not a ledger leg. Keep revise payload names including
from_account_id and correction_note. total_periods is optional display metadata;
it never produces schedules. A full installment purchase returns transaction_id,
not installment_plan_id; this changed exception needs explicit device acceptance.

Foreign-currency success uses original amount/currency and reference reporting data
when available; it no longer promises from_amount/estimated settlement debt. Normal
one-off keys/status fields remain stable. Capture processing returns HTTP 202 with
status, request_id, display_summary and Retry-After. Draft/terminal success is 200.
Provider failure returns 503 error envelope with request_id in details; GET by-key
returns `status:failed`, error code and display_summary. It is safe to recapture
with a new key only after this terminal outcome is known. Same-key POST replays it.

### Balance payloads and safe commit

POST /balance-captures uses the screenshot body above; request_kind is inferred
from the route. Gemini transport includes a list of rows with stable row_id,
label/account candidate, amount, currency, balance meaning (asset/debt/overpayment/
total/unsupported), as_of if visible, precision, field confidences, and totals
with explicitly covered row IDs. No dynamic object maps.

Service drafts normalize selectable rows to the following shape. The browser
PATCH replaces the whole rows list (max 50), preserving row IDs; omitted selected
rows require explicit selected=false so nothing silently disappears.

```json
{
  "expected_version": 0,
  "rows": [
    {"row_id":"row-1","selected":true,"account_id":"<uuid>","balance":"25000.00","currency":"CNY","as_of":"2026-09-05T10:00:00+08:00","time_basis":"capture","expected_latest_snapshot_id":null,"expected_account_version":0},
    {"row_id":"row-2","selected":true,"account_id":"<card-uuid>","balance":"-3000.00","currency":"CNY","as_of":"2026-09-05T10:00:00+08:00","time_basis":"capture","expected_latest_snapshot_id":"<uuid>","expected_account_version":0}
  ]
}
```

Signed normalized balance is always explicit in transport. UI debt inputs convert
to negative only under a clear debt label; no AI sign guess. Manual POST
/balance-updates uses `{observations:[...]}` with the same selected-row financial
fields (no row_id/selected), plus idempotency header. At least one and at most 50
unique accounts; all rows required and committed atomically.

The service attaches expected_latest_snapshot_id and expected_account_version when
preparing image drafts, even on automatic paths. Manual forms receive them from
account reads. Under the write lock, reject a changed head/configuration: image
capture returns a refreshed needs_confirmation draft, manual Save returns 409
BALANCE_CHANGED with current data. Do not overwrite a newer observation unseen.
Backdated entries still check that the user reviewed the current head, then insert
as historical. Corrections check both target version and current latest ID.

Totals are evidence, never stored financial rows. For exact full-precision lines
of identical scope/currency, require equality after quantization. If labels explicitly
show rounding with display units u for each of n components and the total, allow
absolute difference <= half the sum of all n+1 display units; record an informational
rounding warning. Require currency-minor-unit precision on balances themselves for
auto-save: “1.2万” is an approximate balance needing explicit manual confirmation.
Never use the old 200 CNY threshold or infer display rounding to excuse a mismatch.
Known incomplete scope yields an informational “total not comparable”; uncertain
overlap or an omitted relevant balance yields needs_confirmation. Unknown rows are
not silently skipped; the user may explicitly exclude them.

On confirm, revalidate all selected rows; no partial financial save. Response:
`{status,request_id,snapshots:[{id,account_id,balance,currency,as_of}],display_summary}`.
Unselected rows stay in sanitized draft history with reasons but have no financial
effect. An investment row commits its balance independently; optional flow entry
is the separate PUT and cannot roll back a valid balance update.

### Reports: explicit completeness contract

Wealth returns `as_of`, `reporting_currency`, `accounts` (snapshot ID/date, original
balance, converted amount or null, FX date, age), `known_assets`, `known_liabilities`,
`known_net_worth`, nullable `total_assets/total_liabilities/net_worth`, risk buckets
and `coverage:{complete,missing_account_ids,missing_fx_currencies,stale_account_ids,
stale_fx_currencies,oldest_observation_at,newest_observation_at}`. Complete means
all included accounts have usable observations/conversions, not that dates are
fresh or capture is exhaustive. Account universe comes from configured lifetimes;
no API can prove the users listed every household asset.

Spending returns gross_expenses, refunds, net_spending, recorded_income, category/
merchant/month breakdowns, native_currency_totals, and missing_conversion_count.
Complete reporting-currency aggregates are null when relevant conversions are
missing; parallel `known_*` amounts remain usable and labelled partial. Wealth
updates never affect these totals. No net_cash_flow or savings_rate field.

Investments returns every consecutive eligible snapshot pair, native balances,
flow inputs or null, `gain` or null, and reason `FIRST_OBSERVATION`, `FLOWS_UNKNOWN`,
`PAIR_CHANGED`, `OUTSIDE_RANGE`, or null. Aggregates name the actual included
intervals and currencies and report coverage/gaps; incomplete totals are null.
When an old pair becomes invalid, expose its old inputs in history and report
PAIR_CHANGED for affected adjacent intervals without valid inputs.

### Error vocabulary

Retain AUTH_REQUIRED, INVALID_CREDENTIALS, DEVICE_REVOKED, REQUEST_NOT_FOUND,
IDEMPOTENCY_KEY_REUSE, INVALID_IMAGE_PAYLOAD and existing safe envelope behavior.
Use 409 for ROW_VERSION_CONFLICT, DRAFT_CHANGED, BALANCE_CHANGED, SNAPSHOT_TIME_CONFLICT,
INVALID_REQUEST_STATE and REFUND_EXCEEDS_ORIGINAL. Use 422 for invalid money/date/
currency/reference shape. Cross-household IDs are 404. Provider/dependency failure
is 503 with retryable=true, never raw upstream exception text. Missing FX and
unknown investment gain are report coverage, not errors preventing native capture.

## 5. Authentication, deployment and runtime acceptance boundary

Reuse opaque device tokens, hashes/revocation, AuthContext, JWT claim validation and
household resolution. Replace the Dashboard token textbox with Supabase Auth login
for exactly two pre-provisioned users; disable public signup. Map verified sub to
existing users.auth_subject, then membership. Do not add custom password endpoints.

Dashboard keeps access/refresh tokens in that user's server-side session only,
refreshes on expiry, clears login/session state on logout, and never uses a shared
module-global authenticated Supabase/API client. A disconnected/new session may
require login again. Use Supabase publishable configuration, not a service-role key.
Backend pins the configured project's issuer, audience and asymmetric algorithms,
verifies against its JWKS, and refreshes keys with bounded caching on unknown kid.
The current AUTH_JWKS_URL setting alone is not an implemented verifier. Retain
injected/static verifiers only for tests and explicit isolated staging.

Supabase provides [password sign-in](https://supabase.com/docs/guides/auth/passwords)
and [asymmetric JWT signing keys](https://supabase.com/docs/guides/auth/signing-keys);
the concrete Python entry point is
[sign_in_with_password](https://supabase.com/docs/reference/python/auth-signinwithpassword).
Configure and test the project's actual key mode during implementation; do not
assume existing staging HMAC tokens are production login. Password recovery may
use operator-assisted reset for this two-person release; no new email service is required.

Keep Cloud Run services and Supabase in their current shape. Backend alone has DB
and Gemini secrets. Dashboard gets backend URL, Supabase URL/publishable key and
timezone. Bind configured Cloud Run container ports consistently; no platform move.
Cap connections/instances to the PostgreSQL connection budget. Reuse connection
helpers initially; introduce pooling only if observed connection pressure requires it.

Liveness is process-only. Readiness verifies DB access and the **selected target
migration lineage** and returns 503 for incompatible schema. Missing AI/FX reports
degraded capability without preventing manual entries or native-currency reads.
Readiness must not make paid Gemini requests or expose DSNs/secrets. Emit structured
request ID, duration, outcome, dependency error code and model version; never payloads.
Report capture latency and draft/error counts through existing Cloud Run logging;
no observability platform project is needed.
