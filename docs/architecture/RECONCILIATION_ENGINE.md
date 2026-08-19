# VibeLedger Reconciliation Engine

> Status: **Target reconciliation-engine contract**
>
> Authority:
>
> 1. `TARGET_DOMAIN_MODEL.md` — business source of truth
> 2. `docs/architecture/PHYSICAL_SCHEMA.md` — persistence contract
> 3. `docs/architecture/API_CONTRACT.md` — API/workflow contract
> 4. This document — reconciliation and matching rules
>
> Scope: Product v1.
>
> Core rule: **Parsing and matching may create evidence and candidates, but MUST NOT mutate the committed ledger until reconciliation commit.**

---

# 1. Goals

The reconciliation engine answers four questions:

```text
1. Which Statement lines correspond to transactions already in VibeLedger?
2. Which clear Statement lines represent missing transactions?
3. Which lines require human review?
4. After all known movements are applied, does the ledger agree with the authoritative account state?
```

The engine MUST support:

- ordinary bank accounts;
- savings accounts;
- credit cards;
- internal transfers;
- cross-currency transfers;
- refunds;
- fees;
- installments;
- account snapshots;
- investment total-value reconciliation;
- repeated Statement uploads without duplicate ledger entries.

The engine MUST prefer:

```text
false negative / needs_review
```

over:

```text
false positive / silently wrong ledger
```

---

# 2. Non-Goals

Product v1 does not attempt:

- perfect merchant identity resolution;
- transaction matching across arbitrary long time windows;
- securities trade / position reconciliation;
- tax-lot reconciliation;
- exact realized FX P&L;
- automated correction of ambiguous historical transactions;
- mandatory monthly closing;
- PDF duplicate detection.

---

# 3. Reconciliation Unit

Every reconciliation attempt is represented by:

```text
reconciliation_batch
```

A batch is scoped to exactly one account.

Supported batch types:

```text
statement
snapshot
manual
```

Lifecycle:

```text
processing
    ↓
ready
or
needs_review
    ↓
committed
```

Alternative terminal states:

```text
rejected
failed
```

A batch in `processing`, `ready`, or `needs_review` MUST NOT affect:

```text
transactions
account_state
account_snapshots
credit_card_snapshots
investment_pnl_periods
```

until commit.

---

# 4. High-Level Pipeline

Statement workflow:

```text
PDF upload
↓
Account is already known from upload endpoint
↓
Parse document
↓
Extract authoritative statement metadata
↓
Normalize statement lines
↓
Classify line semantics
↓
Generate existing-transaction candidates
↓
Resolve clear matches
↓
Generate transfer/refund/installment candidates
↓
Generate missing-transaction candidates
↓
Simulate candidate effects
↓
Calculate reconciliation residual
↓
Apply account-type-specific rules
↓
ready / needs_review
↓
atomic commit
```

Snapshot/manual workflow begins at:

```text
authoritative account observation
↓
calculate projected ledger state
↓
residual
↓
ready / needs_review
↓
atomic commit
```

---

# 5. Configuration Constants

The following are implementation constants, not end-user settings in Product v1:

```text
MATCH_DATE_WINDOW_DAYS       = 5
AUTO_MATCH_SCORE             = 80
AUTO_MATCH_MARGIN            = 15
MERCHANT_STRONG_SIMILARITY   = 0.80
MERCHANT_MEDIUM_SIMILARITY   = 0.60
MERCHANT_WEAK_SIMILARITY     = 0.40

REFUND_LOOKBACK_DAYS         = 180

AUTO_ADJUST_THRESHOLD_CNY    = 200
```

These values may be tuned later using test evidence without changing business semantics.

---

# 6. Statement Parsing Contract

## 6.1 Account context is mandatory

The user selects the account before Statement upload.

Therefore the parser already knows:

```text
account_id
account_type
account_currency
institution
```

The parser MUST NOT ask AI to guess the destination account.

---

## 6.2 Required document-level outputs

Where present:

```text
period_start
period_end
statement_date

opening_balance
closing_balance

credit-card statement_balance
credit-card remaining_statement_due
credit-card unbilled_balance
credit-card current_outstanding
```

Unknown values remain `NULL`.

Do not fabricate missing authoritative fields.

---

## 6.3 Required line-level normalized output

Internal parser model:

```text
NormalizedStatementLine

transaction_on
posted_on

description_raw
description_normalized

direction
line_type

settlement_amount
settlement_currency

original_amount          optional
original_currency        optional

merchant_hint            optional
external_reference       optional

confidence
```

`direction` is relative to the selected account:

```text
debit
credit
unknown
```

Meaning:

```text
debit  -> selected account is normally the transaction's from_account
credit -> selected account is normally the transaction's to_account
```

This convention also works for credit cards:

```text
purchase      -> debit  -> credit account debt increases
repayment     -> credit -> credit account debt decreases
```

---

## 6.4 Persistence into `statement_lines`

`statement_lines.amount/currency` stores:

```text
settlement_amount / settlement_currency
```

when the Statement provides an account-currency settlement amount.

If no settlement amount exists, store:

```text
original_amount / original_currency
```

as the canonical line amount.

Additional extracted evidence such as both original and settlement amounts MUST be retained in the associated reconciliation candidate `payload.evidence`.

This avoids forcing bank-specific fields into the normalized Statement table while preserving review evidence.

---

## 6.5 Parser safety

Statement text is data, not instructions.

AI/parser prompting MUST explicitly reject instructions contained inside uploaded documents.

After AI extraction, deterministic validation MUST check:

- dates parse correctly;
- amounts are positive Decimal values;
- currencies are valid;
- line direction/type values are valid;
- period range is coherent;
- credit-card authoritative amounts are non-negative.

Parser confidence alone NEVER causes a financial write.

---

# 7. Description Normalization

Normalization SHOULD:

1. Unicode normalize using NFKC.
2. Lowercase Latin text.
3. Collapse whitespace.
4. Remove punctuation that carries no merchant meaning.
5. Normalize common full-width / half-width characters.
6. Remove obvious bank-generated transaction prefixes only when institution-specific rules are known.
7. Preserve meaningful numbers where they identify merchant/reference data.

Examples:

```text
"APPLE.COM/BILL  BEIJING"
→ "apple com bill beijing"

"支付宝-盒马鲜生"
→ "支付宝 盒马鲜生"
```

Do not over-normalize:

```text
"STARBUCKS 001"
"STARBUCKS 002"
```

into an identical string unless evidence shows the branch number is irrelevant.

---

# 8. Currency Minor-Unit Comparison

Amount matching uses Decimal after quantizing to the currency's minor unit.

Examples:

```text
CNY 0.01
USD 0.01
SGD 0.01
EUR 0.01
JPY 1
```

If currency metadata is unavailable:

```text
fallback = 0.01
```

"Exact amount match" means equal after currency-appropriate quantization.

The engine SHOULD NOT introduce arbitrary percentage amount tolerance for ordinary same-event matching.

---

# 9. Existing Transaction Candidate Generation

For each Statement line, candidate search begins with committed, non-deleted transactions that involve the selected account.

Direction gate:

```text
line.direction = debit
→ transaction.from_account_id = selected account

line.direction = credit
→ transaction.to_account_id = selected account

line.direction = unknown
→ either side may be searched
→ automatic matching is restricted
```

Date window:

```text
abs(statement effective date - transaction.occurred_on) <= 5 days
```

Statement effective date:

```text
transaction_on if present
else posted_on
```

The selected account is a hard gate.

---

# 10. Comparable Amount Evidence

For the selected account side, the engine attempts these comparisons.

## 10.1 Settlement/account-leg match

Debit line:

```text
statement settlement amount/currency
↔ transaction.from_amount/from_currency
```

Credit line:

```text
statement settlement amount/currency
↔ transaction.to_amount/to_currency
```

This is the strongest amount evidence.

---

## 10.2 Original-currency match

When Statement contains original transaction amount:

```text
statement original_amount/original_currency
↔ transaction.original_amount/original_currency
```

This is particularly important for foreign-currency credit-card spending.

---

## 10.3 No directly comparable amount

A Shortcut may capture:

```text
10,000 JPY purchase
```

before the card Statement later settles it as:

```text
68.20 USD
```

If the existing transaction contains:

```text
original_amount = 10,000 JPY
from_amount     = NULL
```

and the Statement contains only:

```text
68.20 USD settlement
```

the candidate MUST NOT be rejected merely because no directly comparable amount exists.

It may still be matched using:

```text
account
date
merchant/description
transaction type
```

but requires stronger non-amount evidence.

If committed, Statement settlement data may fill the previously unknown account-leg settlement amount and freeze historical reporting FX.

---

## 10.4 Contradictory comparable amount

If both sides contain directly comparable amounts and they differ beyond the minor-unit tolerance:

```text
candidate = rejected
```

Do not use merchant similarity to override an explicit amount contradiction.

---

# 11. Match Score

Hard gates are evaluated before score:

```text
same selected account
compatible direction
within normal ±5-day window
no contradictory comparable amount
```

Score maximum:

```text
100
```

Recommended components:

```text
Amount evidence                         40
Date proximity                          20
Merchant/description similarity         20
Transaction-type compatibility          10
Original-currency / extra evidence      10
```

---

## 11.1 Amount score

```text
exact selected-account settlement amount     40
exact original-currency amount                35
no comparable amount                           0
```

If both settlement and original evidence agree:

```text
amount component remains capped at 40
extra original evidence may contribute in extra-evidence score
```

---

## 11.2 Date score

```text
0 days difference  20
1 day              18
2 days              16
3 days              12
4 days               8
5 days               5
```

---

## 11.3 Merchant/description similarity

Use normalized text and PostgreSQL trigram similarity or an equivalent deterministic implementation.

```text
similarity >= 0.80  -> 20
similarity >= 0.60  -> 15
similarity >= 0.40  ->  8
otherwise           ->  0
```

If both merchant fields are absent:

```text
0
```

---

## 11.4 Type compatibility

Examples:

```text
statement expense  ↔ expense transaction          10
statement fee      ↔ fee transaction              10
statement refund   ↔ refund transaction           10
statement transfer ↔ transfer transaction         10

unknown ↔ any compatible direction                0
```

---

## 11.5 Extra evidence

Up to 10 points:

```text
original currency + amount independently agree
strong external reference agreement
institution-specific deterministic reference match
```

Do not award points based only on LLM confidence.

---

# 12. Automatic Match Decision

A line may auto-match only when all conditions hold:

```text
best candidate score >= 80
AND
best - second_best >= 15
AND
candidate is the line's unique best candidate
AND
the line is also that transaction's unique best line within the batch
AND
no mandatory-confirmation rule applies
```

The mutual-best rule prevents one existing transaction from being matched to two Statement lines.

If candidate score is high but the margin is insufficient:

```text
ambiguous
→ needs_review
```

If exactly one candidate exists but score is below threshold:

```text
needs_review
```

Do not auto-match merely because only one database row happens to exist.

---

# 13. Existing Statement-Confirmed Transaction

If an existing transaction is already:

```text
verification_status = statement_confirmed
```

and a newly uploaded Statement appears to represent the same event:

```text
match normally
```

This is the core replay-safe behavior for repeated Statement uploads.

Do not create a second transaction.

If the new Statement contradicts previously frozen authoritative settlement data:

```text
needs_review
reason = AUTHORITATIVE_DATA_CONFLICT
```

Never silently overwrite prior Statement-confirmed values.

---

# 14. Candidate Assignment Strategy

Matching MUST be deterministic.

Recommended algorithm:

```text
for each line:
    generate gated candidates
    calculate score
    sort descending

identify mutual unique best pairs

auto-accept only pairs satisfying threshold + margin

all conflicts:
    needs_review
```

A global Hungarian/maximum-weight solver is not required in Product v1 because the system intentionally favors conservative matching.

---

# 15. Statement Line Classification

`line_type` values:

```text
expense
income
transfer
refund
fee
unknown
```

Classification combines:

```text
bank-specific deterministic rules
description keywords
direction
AI extraction
```

Priority:

```text
deterministic bank/reference evidence
>
strong semantic description
>
AI classification
```

If semantics remain unclear:

```text
line_type = unknown
```

Mandatory review applies when the system cannot distinguish:

```text
refund
income
transfer
```

---

# 16. Missing Ordinary Expense

A Statement debit line may generate a missing `expense` candidate when:

```text
no existing transaction matches
AND
line is clearly ordinary merchant spending
AND
account is uniquely known
AND
amount/currency are clear
AND
line classification confidence is sufficient
```

Candidate:

```text
candidate_type = create_transaction
transaction_type = expense
```

Category:

- auto-assign only when category confidence is sufficiently clear;
- otherwise candidate requires review.

Statement itself may be authoritative enough for merchant/date/account/amount even when category is uncertain.

If category is required by the physical transaction contract and cannot be assigned confidently:

```text
needs_review
```

---

# 17. Missing Cash Income

A Statement credit may create `cash_income` only when income semantics are clear.

Examples:

```text
salary
bank interest
company reimbursement
gift income
professional fee
```

If a credit may instead be:

```text
internal transfer
refund
external transfer
```

it MUST NOT auto-create cash income.

Result:

```text
needs_review
```

---

# 18. Fee

A clear bank/card fee may create:

```text
transaction_type = fee
```

If fee is part of a cross-currency internal transfer:

```text
transfer transaction
+
separate fee transaction
```

Both are applied in the same reconciliation commit.

---

# 19. Internal Transfer Matching

Internal transfer is always one transaction with two explicit account legs.

---

## 19.1 First preference: match existing committed transfer

For a Statement transfer line, first search committed `transfer` transactions involving the selected account.

Use:

```text
account side
direction
amount/currency
±5 days
```

If unique clear match:

```text
match existing transfer
```

---

## 19.2 Search household counter-account evidence

If no committed transfer exists, search household accounts for an opposite movement.

Same-currency example:

```text
Account A Statement:
2026-08-10 debit  5,000 CNY

Account B:
2026-08-10 credit 5,000 CNY
```

Strong transfer evidence:

```text
opposite direction
same amount/currency
date within ±5 days
different household account
description compatible with transfer
```

Cross-currency example:

```text
Account A debit  7,250 CNY
Account B credit 1,000 USD
```

Requires:

```text
both explicit amounts
both currencies
plausible date alignment
```

Resulting transfer:

```text
from_amount       = 7250 CNY
to_amount         = 1000 USD
effective_fx_rate = 7.25
```

---

## 19.3 Counter-account ambiguity

If more than one household account could be the counterparty:

```text
needs_review
```

AI MUST NOT guess the destination account.

---

## 19.4 Statement only says "transfer out"

If Statement shows:

```text
TRANSFER OUT 5000 CNY
```

but no unique counter-account evidence exists:

```text
create transfer candidate
to_account = unresolved
needs_review
```

No committed transaction is created.

---

## 19.5 Two Statements containing the same transfer

Final ledger requirement:

```text
one transfer transaction
not two independent income/expense rows
```

Product v1 replay strategy:

1. whichever side is resolved and committed first creates the transfer;
2. the later Statement side matches that existing transfer;
3. no duplicate transaction is created.

If both Statement batches are simultaneously pending and neither side has a committed transfer yet:

```text
do not create two transfers
do not auto-commit either ambiguous side
```

At least one batch remains `needs_review` until the counter-account is resolved.

This intentionally avoids introducing cross-batch distributed commit complexity in Product v1.

---

# 20. Cross-Currency Transfer Rules

A cross-currency transfer candidate may commit only when both legs are known:

```text
from_account
from_amount
from_currency

to_account
to_amount
to_currency
```

`effective_fx_rate` is computed:

```text
from_amount / to_amount
```

If one leg is missing:

```text
needs_review
```

A public/current FX rate MUST NOT be used to invent the missing actual transferred amount.

Public FX may only be used for plausibility diagnostics.

---

# 21. Refund Matching

Refund is a new transaction linked to an earlier expense.

It is not a deletion or reversal of history.

---

## 21.1 Refund candidate search

For a clear refund credit line:

Search expenses:

```text
same household
compatible receiving/payment account context
original expense occurred before refund
lookback <= 180 days
merchant similarity
refund amount <= remaining refundable amount
```

Scoring is separate from ordinary ±5-day same-event matching.

---

## 21.2 Strong automatic refund link

May auto-link only when:

```text
line explicitly indicates refund
AND
exact/compatible amount
AND
strong merchant similarity
AND
only one plausible original expense
```

Otherwise:

```text
needs_review
```

---

## 21.3 Partial refund

Allowed:

```text
original expense 1000
refund 300
remaining refundable 700
```

The refund transaction is linked:

```text
refund_of -> original expense
```

---

## 21.4 Refund limit

Before commit, recompute:

```text
sum(committed non-voided refunds)
+
proposed refund
<=
original refundable amount
```

If exceeded:

```text
needs_review
```

No silent over-refund.

---

# 22. Installment Recognition

Future installment periods are schedules, not transactions.

For credit-card Statements, before creating a new ordinary expense candidate, search:

```text
active installment_plans
scheduled installment_periods
selected credit account
expected recognition month
matching scheduled amount
merchant similarity when available
```

If unique:

```text
candidate = recognize_installment
```

Commit:

```text
create expense transaction for this billed period
mark installment_period billed
link statement line
```

The first period begins when first seen on the card Statement.

The final period absorbs rounding remainder.

Do not create future expense transactions.

---

# 23. Credit-Card Foreign-Currency Expense Matching

Priority:

```text
1. original-currency exact match, if Statement exposes original amount
2. settlement account-currency exact match, if existing transaction has settlement leg
3. date + merchant + type, if settlement amount was not yet known
```

Example:

Shortcut:

```text
merchant = Tokyo Shop
original = 10,000 JPY
card     = USD Visa
from_amount = NULL
```

Statement:

```text
Tokyo Shop
settlement = 68.20 USD
```

If date and merchant strongly identify a unique transaction:

```text
match may be accepted
```

Commit may patch authoritative settlement fields:

```text
from_amount       = 68.20
from_currency     = USD
posted_on         = Statement posted date
verification_status = statement_confirmed
```

If Statement also provides original amount:

```text
original 10,000 JPY
```

it becomes strong independent evidence.

If Statement-provided original amount contradicts the captured original amount:

```text
needs_review
```

---

# 24. Historical Reporting FX Freeze

For foreign-currency spending before Statement settlement:

```text
reporting_amount may be NULL
```

Dashboard may display current/T-1 estimated CNY value.

When Statement provides authoritative settlement:

```text
freeze historical reporting value
```

The reconciliation candidate may populate:

```text
reporting_amount
reporting_currency
reporting_fx_rate
reporting_fx_locked_at
```

Credit-card repayment occurring later MUST NOT rewrite these fields.

Repayment FX belongs to the transfer transaction itself.

---

# 25. Ordinary Account Residual Calculation

For cash/savings accounts, reconciliation compares:

```text
authoritative account balance at T
vs
projected ledger balance at T after proposed candidates
```

Define:

```text
projected_balance(T)
```

as:

1. latest authoritative snapshot/opening anchor at or before `T`;
2. plus committed transaction effects after that anchor through `T`;
3. plus accepted candidate effects from the current batch through `T`;
4. excluding voided/deleted transactions.

Residual:

```text
residual =
authoritative_balance
-
projected_balance(T)
```

Signed residual means the adjustment needed to reach authoritative balance.

---

# 26. Historical Statement Reconciliation

Do not use today's `account_state.ledger_balance` directly for a historical Statement period.

Instead compute:

```text
ledger_balance_as_of(account_id, period_end)
```

using historical anchors and transactions.

When a historical missing transaction is committed:

```text
historical balance changes
AND
current account_state changes by the same transaction effect
```

This preserves both historical and current correctness.

---

# 27. Ordinary Auto-Adjustment

For non-investment accounts:

Convert residual to household reporting currency using the reconciliation-time reference FX when needed.

If:

```text
abs(residual_CNY) <= 200
```

and no unresolved semantic ambiguity exists:

```text
candidate_type = adjustment
transaction_type = reconciliation_adjustment
```

The adjustment:

- fixes account projection;
- is dated at authoritative reconciliation date;
- does not enter expense/income reporting;
- is fully audited.

If:

```text
abs(residual_CNY) > 200
```

then:

```text
batch = needs_review
```

No automatic adjustment.

---

# 28. Residual Is Evaluated Last

Residual MUST be calculated only after:

```text
existing matches
missing expenses/income
transfers
refunds
fees
installment recognition
```

have been resolved or simulated.

Never use a reconciliation adjustment to hide a clearly identifiable missing transaction.

Priority:

```text
explainable transaction
>
reconciliation adjustment
```

---

# 29. Credit-Card Reconciliation

Credit cards have more than one authoritative value.

Persist separately:

```text
statement_balance
remaining_statement_due
unbilled_balance
current_outstanding
```

Do not collapse them into one "credit-card balance".

---

## 29.1 Statement matching

Statement lines are matched/created using the same transaction engine.

Card purchase:

```text
selected credit account = from_account
```

Card repayment:

```text
selected credit account = to_account
transaction_type = transfer
```

Refund:

```text
selected credit account = to_account
transaction_type = refund
```

---

## 29.2 Statement-balance residual

When authoritative `statement_balance` exists, compare it with the reconstructed billed-cycle amount after candidate simulation.

Conceptually:

```text
predicted_statement_balance
=
billed expenses
+ billed fees
- billed refunds/credits
+ recognized installment periods
+ allowed cycle corrections
```

Do not subtract repayments made after Statement issuance from `statement_balance`.

Those affect:

```text
remaining_statement_due
```

instead.

Residual:

```text
statement_residual =
authoritative_statement_balance
-
predicted_statement_balance
```

For ordinary credit accounts, the <=200 CNY review threshold may apply to a genuine unexplained cycle residual.

---

## 29.3 Current outstanding

If Statement provides:

```text
current_outstanding
```

store it in `credit_card_snapshots`.

It may be compared to the card's current projected liability.

If Statement does not provide current outstanding:

```text
do not force account_state to -statement_balance
```

because unbilled spending may exist.

---

## 29.4 Remaining due

If the Statement provides authoritative remaining due:

```text
store it
```

Otherwise it may be derived for display from:

```text
statement_balance - qualifying repayments
```

but derived values are not treated as more authoritative than Statement data.

---

# 30. Credit-Card Repayment Matching

A repayment is internal transfer, not income.

For a credit line into a credit-card account:

Search household asset-account outflows:

```text
same/near date
compatible amount
compatible currency
transfer-like description
```

If unique:

```text
create/match one transfer
```

If repayment is cross currency:

```text
both actual legs required
```

If the card Statement shows only one side and no unique source account exists:

```text
needs_review
```

---

# 31. Snapshot Reconciliation

Snapshot batches contain no Statement lines.

Input:

```text
account
as_of
authoritative balance
```

Engine:

```text
calculate projected ledger balance as_of
↓
residual
↓
ordinary threshold rules
```

If small:

```text
ready
→ snapshot + reconciliation adjustment on commit
```

If large:

```text
needs_review
```

User may:

```text
accept direct adjustment
or
upload Statement to investigate
```

The system does not require Statement collection.

---

# 32. Opening Balance / First Reconciliation

An account requires an initial ledger anchor.

Preferred initialization order:

```text
1. explicit Statement opening balance
2. manual opening balance
3. authoritative Snapshot
```

---

## 32.1 Statement with opening balance

If the first usable Statement provides an opening balance:

```text
create opening_balance at period_start
then reconcile Statement period transactions
```

Opening balance is not income.

---

## 32.2 Only closing balance available

If no earlier anchor exists and only an authoritative closing balance is known:

```text
use that closing observation as the initial authoritative baseline
```

Do not fabricate historical income/expense merely to explain pre-ledger history.

Transactions before the chosen baseline are outside the ledger's tracked history.

---

# 33. Investment Reconciliation

Investment account reconciliation does not use the ordinary ±200 adjustment rule.

Product v1 extracts:

```text
total asset value
clear contributions
clear withdrawals
```

It ignores:

```text
individual positions
security purchases/sales
cost basis
```

---

## 33.1 First investment snapshot

The first authoritative investment valuation establishes the baseline.

```text
no investment P&L is created for time before the first baseline
```

---

## 33.2 Subsequent investment snapshot

Find:

```text
previous authoritative investment snapshot
```

Then identify committed capital flows between snapshots:

```text
contributions
withdrawals
```

Calculate:

```text
P&L =
closing_value
-
opening_value
-
contributions
+
withdrawals
```

Pending/provisional investment calculations belong strictly in `reconciliation_candidates.payload` (with `candidate_type = investment_pnl`) and MUST NOT insert uncommitted rows into `investment_pnl_periods` while the batch is in `processing`, `ready`, or `needs_review`. Only atomic batch commit creates confirmed `investment_pnl_periods` rows.

---

## 33.3 Ambiguous investment capital movement

Example:

```text
opening value = 100,000
closing value = 150,000
known contribution = 0
```

The engine MUST NOT assume:

```text
P&L = +50,000
```

if there is evidence or reasonable possibility of unrecorded capital movement.

Result:

```text
needs_review
reason = AMBIGUOUS_INVESTMENT_CAPITAL_FLOW
```

User may provide:

```text
capital contribution
capital withdrawal
or confirm no capital movement
```

Then P&L is recalculated.

---

## 33.4 Investment Statement

A Statement may provide:

```text
closing total asset value
clear deposits
clear withdrawals
```

Those deposits/withdrawals are matched to existing internal transfers where possible.

Missing explicit capital flows create transfer candidates.

Security-level trades are ignored in Product v1.

---

# 34. Candidate Lifecycle

Candidate status:

```text
proposed
needs_review
accepted
rejected
applied
```

Engine-created high-confidence candidates may become:

```text
accepted
```

during preview generation.

Ambiguous candidates:

```text
needs_review
```

Dashboard user actions may change them to:

```text
accepted
rejected
```

Only batch commit changes:

```text
accepted -> applied
```

---

# 35. Batch Readiness

A batch may become `ready` only if:

```text
no candidate remains needs_review
no unresolved account exists
no cross-currency leg is missing
no multiple-match ambiguity exists
no authoritative-data conflict exists
no investment capital-flow ambiguity exists
residual passes account-type rules
```

Otherwise:

```text
needs_review
```

---

# 36. Atomic Commit Algorithm

Pseudo-code:

```text
BEGIN

SELECT reconciliation_batch
FOR UPDATE

if batch already committed:
    return committed result

verify batch is ready or manually resolved

lock all affected account_state rows
ORDER BY account_id
FOR UPDATE

re-read committed transactions relevant to this batch

re-run critical match conflict checks
re-run refund limits
re-run transfer-leg validation
re-run installment state validation
recompute projected balance
recompute residual
re-evaluate <=200 threshold
re-evaluate investment capital flows

if validation now fails:
    rollback
    batch must be regenerated/reviewed

apply accepted candidates:
    patch verification metadata on existing transactions
    create missing transactions
    create transfers
    create refunds + links
    create fee transactions
    recognize installment periods
    create reconciliation adjustment if permitted

create authoritative account snapshot where applicable
create credit-card snapshot where applicable
create investment P&L where applicable

update account_state projection

mark statement lines matched
mark accepted candidates applied
mark batch committed

append audit events

COMMIT
```

No partial commit is allowed.

---

# 37. Revalidation at Commit Is Mandatory

Preview is not a lock.

Between:

```text
preview
and
commit
```

a Shortcut may create a new transaction.

Therefore commit MUST re-read the ledger after acquiring account locks.

Example:

```text
10:00 Statement preview says ¥268 expense is missing
10:01 Shortcut creates ¥268 transaction
10:02 user commits Statement
```

Correct behavior:

```text
reconciliation sees new transaction
matches it
does NOT create duplicate expense
```

---

# 38. Repeated Statement Upload / Replay Safety

The system intentionally does not identify duplicate PDF files.

Each upload creates a fresh batch.

Replay safety comes from the ledger.

Example first upload:

```text
70 matched
2 missing expenses created
1 reconciliation adjustment
```

Second upload of same Statement:

```text
the 2 formerly missing expenses now match existing transactions
the prior adjustment is already part of historical ledger state
residual becomes zero/expected
no duplicate rows are created
```

Therefore:

```text
PDF deduplication is optional
ledger reconciliation idempotency is mandatory
```

---

# 39. Business Duplicate Detection vs Reconciliation Matching

These are separate concerns.

Client idempotency:

```text
same request retry
```

is handled by:

```text
ingestion_requests
```

Statement matching:

```text
same real-world event from independent evidence
```

is handled by this reconciliation engine.

Semantic duplicate detection MUST NOT replace either mechanism.

---

# 40. Statement-Confirmed Verification

When a Statement uniquely confirms an existing transaction, commit may update:

```text
verification_status = statement_confirmed
posted_on
authoritative settlement leg
historical reporting FX freeze
```

It MUST NOT rewrite unrelated user-entered data without evidence.

Merchant display text may remain the original user/AI value while normalized Statement evidence stays in the audit/reconciliation trail.

---

# 41. Modification of Statement-Confirmed History

Any later change affecting:

```text
amount
currency
account
transaction type
occurred date
```

on a `statement_confirmed` transaction requires explicit confirmation.

The system must preserve:

```text
before state
after state
reason
actor
audit event
```

---

# 42. Reason Codes

Minimum reconciliation reason codes:

```text
NO_MATCH
LOW_MATCH_SCORE
MULTIPLE_TRANSACTION_MATCHES
TRANSACTION_ALREADY_CLAIMED
AMOUNT_CONFLICT
ORIGINAL_AMOUNT_CONFLICT
DATE_OUTSIDE_WINDOW
MERCHANT_WEAK_MATCH

ACCOUNT_UNRESOLVED
COUNTER_ACCOUNT_UNRESOLVED
CROSS_CURRENCY_MISSING_LEG

TYPE_AMBIGUOUS
INCOME_TRANSFER_REFUND_AMBIGUOUS

REFUND_ORIGINAL_NOT_FOUND
MULTIPLE_REFUND_ORIGINALS
REFUND_EXCEEDS_ORIGINAL

INSTALLMENT_PLAN_AMBIGUOUS

RECONCILIATION_RESIDUAL_TOO_LARGE
AUTHORITATIVE_DATA_CONFLICT

AMBIGUOUS_INVESTMENT_CAPITAL_FLOW
```

These should align with API error/review codes.

---

# 43. Observability

For every batch record:

```text
line_count
matched_count
created_count
pending_count
residual_amount
adjustment_amount
processing duration
parser version
matching-engine version
```

Do not log:

```text
raw PDF bytes
PDF password
device token
full bank document text unless explicitly required for debugging
```

Sensitive raw extraction logs should be avoided by default.

---

# 44. Deterministic Test Matrix

Minimum matching tests:

## Existing expense

```text
same amount
date +0 / +1 / +5
merchant exact / fuzzy
unique candidate
→ auto match
```

## Ambiguous expense

```text
two same-amount candidates
similar date
→ needs_review
```

## Date outside window

```text
same merchant + amount
date +6
→ not ordinary auto-match
```

## Amount contradiction

```text
same date + merchant
explicit comparable amount differs
→ reject candidate
```

## Foreign credit card

```text
Shortcut has JPY original only
Statement has USD settlement
strong merchant/date unique
→ match and fill settlement
```

## Internal transfer

```text
A -5000
existing transfer A->B
→ match
```

## Repeated transfer Statement

```text
second account Statement sees +5000
existing A->B transfer
→ same transfer, no duplicate
```

## Transfer ambiguity

```text
A -5000
B +5000
C +5000
→ needs_review
```

## Refund

```text
expense 1000
refund 300
→ link refund_of
```

## Over-refund

```text
existing refunds 800
new refund 300
original 1000
→ block/review
```

## Installment

```text
scheduled period 1000
Statement bill 1000
→ create current-period expense only
```

## Small residual

```text
ordinary account residual 47 CNY
→ auto adjustment
```

## Large residual

```text
ordinary account residual 1850 CNY
→ needs_review
```

## Investment

```text
100k -> 160k
50k contribution
→ pnl 10k
```

## Ambiguous investment

```text
100k -> 150k
capital movement unknown
→ needs_review
```

## Replay Statement

```text
upload same Statement twice
→ second batch creates no duplicate financial facts
```

## Concurrent Shortcut

```text
preview says transaction missing
Shortcut inserts before commit
→ commit revalidates and matches new transaction
```

---

# 45. Implementation Structure

Recommended backend modules:

```text
domain/
  reconciliation/
    models.py
    normalizer.py
    classifier.py
    matcher.py
    scoring.py
    transfers.py
    refunds.py
    installments.py
    residuals.py
    investments.py
    commit.py

services/
  statement_parser.py
  reconciliation_service.py

repositories/
  transaction_repository.py
  reconciliation_repository.py
  snapshot_repository.py
```

Do not put the reconciliation engine back into one large `database.py`.

---

# 46. Matching Engine Versioning

Persist an engine version with batch metadata/audit, for example:

```text
reconciliation_engine_version = "1"
parser_version = "1"
```

A future algorithm change must not silently reinterpret already committed history.

Re-running a new Statement may use the new engine, but committed historical results remain auditable.

---

# 47. Final Invariants

The implementation MUST preserve these rules:

```text
1 Statement line does not automatically equal 1 new transaction.

Existing Shortcut transaction should be matched before creating anything.

A transaction cannot be claimed twice by two lines in the same account batch.

A transfer is one transaction with two account legs.

A refund is a separate transaction linked to its original expense.

Future installments are schedules, not committed expenses.

Investment valuation change is not cash income.

Small unexplained ordinary-account residual may become adjustment.

Large/ambiguous residual must be reviewed.

Parsing/preview never mutates committed ledger.

Commit is atomic and revalidates against the latest ledger.

Repeated Statement upload must not duplicate financial facts.
```

If implementation convenience conflicts with these invariants, the reconciliation contract wins.
