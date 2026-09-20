# Expense Gemini live acceptance

CI substitutes the external Gemini call. Passing CI does **not** establish that the
deployed Gemini model accepts the wire schema. Run this opt-in check from the same
release image/environment and network as the hosted backend, with its configured
`GEMINI_API_KEY`. Never paste the key into a command, log, ticket or screenshot.
The production default remains `gemini-3.5-flash-lite`; check any deployment
`GEMINI_MODEL` override separately.

## Direct transport check (no ledger writes)

Use one consented, redacted JPEG/PNG screenshot showing a successful expense, a
readable full transaction date, explicit currency, amount and merchant. For example,
a test receipt showing Demo Cafe, SGD 12.50, 2026-09-20. Supply its actual visible
facts below. The image is sent to the real Gemini API; it is not committed to Git.

From `ai-ledger-backend`, using the installed backend Python environment:

```sh
python scripts/smoke_expense_gemini.py --image /private/expense.png --amount 12.50 --currency SGD --date 2026-09-20 --merchant "Demo Cafe" --revision
```

The command uses the production `GeminiService`, checks extracted facts, intent and
date evidence, and verifies that a synthetic revision changes only the amount.
It exits nonzero on failure. Output contains a generated diagnostic UUID and a
pass/fail summary, never the image, note, model response or credentials. It does
not open a database connection or create/confirm an ingestion request. A pass proves
live transport plus local parsing for that fixture, not all screenshot formats.

## Hosted iPhone Shortcut check

Use a dedicated acceptance household/device: this path can save a real expense.
Deploy the reviewed fix through the existing staging release process, then run the
current iPhone Shortcut once with the representative screenshot. Preserve the
Shortcut's new idempotency key and returned request ID. Verify:

1. `POST /api/v1/expenses` yields `committed` for sufficiently clear evidence, or
   `needs_confirmation` for conservative review, rather than a dependency failure.
2. Inspect the amount, currency, business date, merchant and intent in the UI.
   Review required fields before confirmation. Do not accept guessed facts.
3. Recover with `GET /api/v1/ingestion-requests/by-key/{key}`. Repeating the exact
   request/key must return the existing result without a second Gemini call or
   transaction. Do not generate a fresh key for an uncertain network result.
4. For revision acceptance, use a pending draft's existing correction-note action
   (`POST /api/v1/ingestion-requests/{id}/revise`). It must update only the requested
   fields, remain pending, and preserve the saved draft/version on dependency failure.

Record release SHA, configured model, request ID, pass/fail and whether a transaction
was saved. Do not attach bearer tokens, images, raw responses or financial prompts.
The old failed request `aabbc4e2-27e1-46e3-a31b-c27d46da511d` remains a durable failed
receipt: replaying that key will not rerun recognition after deployment. First verify
its terminal state and absence of a transaction; a new intentional acceptance attempt
uses a new key. No automatic retry or production cutover is part of this procedure.

## Safe server diagnosis

Filter backend warning logs by `gemini_failure` and the returned `request_id`.
Extraction and revision include the durable receipt UUID; direct smoke uses its
printed diagnostic UUID. Log fields are limited to operation, request ID, category,
phase, exception class and numeric upstream HTTP status:

- `upstream`: Gemini/SDK transport failure, including schema rejection (often 400),
  rate limits (429), or service unavailability (503).
- `timeout`: SDK connect/read/pool timeout, upstream 408/504, or the capture deadline.
  `phase` distinguishes where it occurred.
- `response_parse`: missing/non-JSON response.
- `response_validation`: decoded JSON failed the local Pydantic/domain result model.
- `domain_validation`: later capture/revision business conversion failed.
- `configuration`: the service has no API key configured.

Exception messages, tracebacks, response bodies, headers and prompts are deliberately
excluded. A 400 alone does not prove a schema defect; reproduce with the direct smoke
to narrow the cause. Do not enable HTTP/SDK debug logging to investigate private data.
The public API remains generic (`CAPTURE_DEPENDENCY_UNAVAILABLE`) and preserves the
existing receipt recovery and revision atomicity behavior.
